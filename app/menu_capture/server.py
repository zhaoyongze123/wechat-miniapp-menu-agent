"""本机控制台的 HTTP 层：FastAPI 路由、令牌校验与静态资源。

职责边界：
- 只做请求解析、鉴权、参数校验和响应组装，业务规则全部下沉到各 service；
- 统一在这里维护 REST 契约，前端与其他脚本只通过 HTTP 操作，不直接改数据库；
- 静态资源每次请求从 web/ 读取，替换前端文件后无需重启服务。
"""
from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from PIL import Image

from menu_capture.core.config import ConfigStore, ROOT, WORKSPACE_ROOT
from menu_capture.storage.store import TaskStore
from menu_capture.capture.runner import UiPathRunner
from menu_capture.desktop.robot_profile import ProfileStore
from menu_capture.vision.image_tools import image_ocr, qr_boxes, prepare_clipboard_image, prepare_clipboard_text
from .materials import MaterialService
from .quality.rejections import RejectionService
from menu_capture.core.environment import EnvironmentDiagnostics
from menu_capture.core.connections import ConnectionManager
from menu_capture.core.debug_log import DebugLog
from menu_capture.agent.model_usage import UsageLedger
from menu_capture.updates.service import UpdateService
from menu_capture.updates.install import InstallController
from menu_capture.updates.hub_proxy import HubInstallProxy
from menu_capture.annotation.session import AnnotationSession, RunnerExecutor
from menu_capture.core.instance_lock import InstanceLockedError, instance_lock
from menu_capture.shared.hub import SharedConflict, SharedHub, WorkerUnauthorized
from .api.shared_routes import register_shared_routes
from menu_capture.shared.worker import SharedWorker
from .api.update_routes import register_update_routes
from .api.model_usage import create_model_usage_router
from .api.task_center import create_task_center_router
from .capture.task_center import TaskCenterService
from .api.frontend import register_frontend_routes
from .api.settings import register_settings_routes
from .api.capture import register_capture_routes
from .api.materials import register_materials_routes
from .api.quality import register_quality_routes
from .api.evidence import register_evidence_routes
from .api.annotation import register_annotation_routes

logger = logging.getLogger(__name__)


def create_app(root: Path = ROOT, desktop=None, *, role: str = "standalone",
               admin_password: str = "", allowed_hosts: tuple[str, ...] = (),
               remote_control_host: str = "", remote_control_password: str = "") -> FastAPI:
    if role not in {"standalone", "hub", "worker"} or (role == "hub" and not admin_password):
        raise ValueError("共享后台需要指定有效角色和管理员密码")
    # 无论通过脚本、python -m 或 ASGI 工厂启动，都只在本进程内禁用环境代理。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    configs = ConfigStore(root, managed=role != "standalone" or bool(os.environ.get("MENU_CAPTURE_HOME")))
    # root 是可写数据目录（可能由 MENU_CAPTURE_HOME 指定）；程序目录必须单独传入，
    # 否则安装目录/历史版本清点会错误地从数据目录反推。
    updates = UpdateService(root, role=role, app_root=ROOT)
    store = TaskStore(root / "data/tasks.sqlite3")
    model_usage = UsageLedger(store)
    debug_log = DebugLog(root / "data", configs.load().debug_logging.max_storage_mb)
    profile_name = "uipath.windows.profile.json" if sys.platform == "win32" else "uipath.profile.json"
    profiles = ProfileStore(root / profile_name)
    if profiles.path.exists():
        profiles.load()  # 启动时完成旧预览定位字段的单次清理。
    project_dir = WORKSPACE_ROOT / "compat" / "uipath"
    runner = UiPathRunner(configs, store, profile_path=profiles.path,
                         project_dir=project_dir, clipboard_preparer=prepare_clipboard_image,
                         clipboard_text_preparer=prepare_clipboard_text,
                         ocr_reader=image_ocr, qr_detector=qr_boxes, debug_log=debug_log,
                         usage_ledger=model_usage)
    # 先恢复目录替换事务，再把非事务中的中断任务标为待检查，避免新结果覆盖旧结果后被误判回滚。
    runner.recover_publishes()
    # 服务重启后不能沿用“运行中”状态，更不能自动重放上一次的桌面动作。
    store.recover()
    control_lock = threading.RLock()
    materials = MaterialService(root / "data", configs, store)
    hub = SharedHub(store, materials) if role == "hub" else None
    worker = SharedWorker(root, store, runner.output_lock, control_lock) if role == "worker" else None
    if worker:
        runner.assignment_source = worker.source_root
        store.bind_assignments = worker.bind_run
    rejections = RejectionService(store, materials, current_only=role == "hub")
    environment = EnvironmentDiagnostics(configs, runner, project_dir)
    connections = ConnectionManager(environment, control_lock, root)
    annotations = AnnotationSession(configs, store, profiles, RunnerExecutor(runner),
                                    root=root if role != "standalone" else WORKSPACE_ROOT, ocr_reader=image_ocr,
                                    rejector=rejections.mark_annotation_task,
                                    usage_ledger=model_usage)
    # 自动安装只在 Windows 采集端且运维显式启用后可用；执行器在独立进程中完成安装与回滚。
    # Hub 则由宿主机上的镜像更新执行器完成，容器只负责授权、转发与状态代理。
    if role == "hub":
        install = HubInstallProxy(
            base_url=str(os.environ.get("HUB_UPDATE_URL") or ""),
            token=str(os.environ.get("HUB_UPDATE_TOKEN") or ""),
            latest_provider=lambda: (updates.status_no_check().get("latest") or {}),
        )
        updates.install_capability = lambda: install.supported
        # 手动回滚接口尚未开放；失败时由执行器自动回滚。
        updates.rollback_capability = lambda: False
    else:
        install = InstallController(
            download_dir=updates.download_dir,
            install_root=updates.install_root(),
            data_dir=updates.data_dir,
            role=role,
            platform=updates.platform,
            arch=updates.local_arch,
            port=int(os.environ.get("MENU_CAPTURE_PORT", "8765")),
            latest_provider=lambda: (updates.status_no_check().get("latest") or {}),
            artifact_provider=updates.download_status,
            current_version=updates.current_version,
            idle_check=lambda: (
                (True, "") if not (runner.running or annotations.active)
                else (False, "采集或人工标注正在执行，请先结束或暂停后再安装")
            ),
            supported=updates.install_supported(),
        )
    # 上次进程可能在安装中途退出；启动时先收尾遗留事务，避免界面永久停在 installing。
    install.recover()
    # 安装/回滚期间禁止清理历史版本，避免删掉正在使用或待回滚的目标目录。
    updates.cleanup_guard = lambda: (
        (True, "安装或回滚进行中，请完成后再清理") if install.active() else (False, "")
    )
    # 安装状态上报给 Hub；执行器未接入时返回空，不推测状态。
    updates.install_status_provider = lambda: str(install.status().get("status") or "")
    if hub is not None:
        # Hub 集中检查 GitHub 后向 Worker 分发元数据，避免每台设备重复请求。
        hub.update_metadata = updates.release_target
    if worker is not None:
        worker.software_provider = updates.worker_software_status
        # Worker 优先采用 Hub 分发的新鲜元数据；Hub 未启用或过期时自动回退本机检查。
        updates.hub_target_provider = lambda: worker.hub_update_target
    # 采集与人工标注的启动入口与安装共用同一闸门，闭合「检查空闲 → 写任务」之间的竞态窗口。
    runner.update_gate = install.gate
    runner.update_blocked = install.active
    annotations.update_gate = install.gate
    annotations.update_blocked = install.active
    token = secrets.token_urlsafe(32)
    def _warm_local_ocr() -> None:
        """后台预热 Apple Vision：首次 OCR 要加载框架约 3-4 秒，别让它落在第一次截图之后。"""
        handle, name = tempfile.mkstemp(prefix="ocr-warmup-", suffix=".png")
        os.close(handle)
        try:
            Image.new("RGB", (32, 32), "white").save(name)
            image_ocr(Path(name))
        except Exception:
            pass
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass

    @asynccontextmanager
    async def lifespan(app):
        if role != "hub":
            threading.Thread(target=_warm_local_ocr, name="ocr-warmup", daemon=True).start()
        # 常驻 1s 巡检：心跳回收 + 单通道调度；不依赖任何请求触发。
        materials.start_job_runner()
        if worker:
            worker.start()
            if sys.platform == "win32":
                connections.connect()
        try:
            yield
        finally:
            if worker:
                worker.close()
            try:
                materials.shutdown()
            except Exception:
                pass
            try:
                annotations.shutdown()
            except Exception:
                pass
            connections.close()
            debug_log.close()

    remote_control_host = remote_control_host.strip().lower()
    if bool(remote_control_host) != bool(remote_control_password) or (
        remote_control_host and (role != "worker" or len(remote_control_password) < 16
                                 or not re.fullmatch(r"[a-z0-9.-]+\.[a-z0-9.-]+", remote_control_host))
    ):
        raise ValueError("远程半自动控制需要 Worker 的 HTTPS 主机名和至少 16 字符的独立密码")
    remote_token = secrets.token_urlsafe(32) if remote_control_host else ""

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runner, app.state.configs, app.state.store = runner, configs, store
    app.state.debug_log = debug_log
    app.state.model_usage = model_usage
    app.state.token = token
    app.state.materials = materials
    app.state.environment = environment
    app.state.connections = connections
    app.state.annotations = annotations
    app.state.shared_hub = hub
    app.state.shared_worker = worker
    app.state.updates_install = install
    app.include_router(create_model_usage_router(model_usage))
    register_shared_routes(app, role, hub, worker, control_lock)

    def basic_authorized(header: str, expected_user: bytes, expected_password: str) -> bool:
        method, _, credentials = header.partition(" ")
        if method.casefold() != "basic" or len(credentials) > 4096:
            return False
        try:
            username, _, password = base64.b64decode(credentials, validate=True).partition(b":")
        except (ValueError, binascii.Error):
            return False
        return username == expected_user and hmac.compare_digest(password, expected_password.encode())

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        authority = request.headers.get("host", "").lower()
        host = authority.split(":", 1)[0]
        path = request.url.path
        worker_protocol = role == "hub" and path.startswith("/api/shared/worker/")
        hosts = {"127.0.0.1", "localhost"} | (set(allowed_hosts) if hub else set())
        is_remote = bool(remote_control_host and host == remote_control_host and request.url.scheme == "https")
        request.state.remote_control = is_remote
        if is_remote:
            read_only = request.method in {"GET", "HEAD"} and (
                path in {"/remote-control", "/api/session", "/api/manual/state", "/favicon.svg"} or path.startswith("/assets/")
            )
            command = request.method == "POST" and path in {"/api/manual/capture", "/api/manual/reject"}
            if not (read_only or command):
                return JSONResponse({"detail": "远程访问仅开放半自动控制"}, status_code=403)
            if not basic_authorized(request.headers.get("authorization", ""), b"operator", remote_control_password):
                return JSONResponse({"detail": "请输入采集端操作员账号"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="Menu Capture Operator"'})
        elif host not in hosts or (worker and request.url.scheme == "https"):
            return JSONResponse({"detail": "只允许本机访问或已配置的远程控制入口"}, status_code=403)
        if hub and not worker_protocol and path != "/healthz" and not basic_authorized(
            request.headers.get("authorization", ""), b"admin", admin_password
        ):
            return JSONResponse({"detail": "请输入共享工作台管理员账号"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Menu Capture"'})
        if hub and (path in {"/api/start", "/api/scan", "/api/resume", "/api/ui-tars/trial", "/api/capture/start",
                             "/api/reviews/pending/rerun", "/api/stop", "/api/terminate", "/api/select-directory",
                             "/api/reveal", "/api/doctor"}
                    or (request.method not in {"GET", "HEAD"}
                        and path.startswith(("/api/annotation/", "/api/manual/", "/api/robot/",
                                             "/api/environment", "/api/connections")))):
            return JSONResponse({"detail": "请在已配对的 Windows 采集端执行此操作"}, status_code=409)
        if worker and request.method not in {"GET", "HEAD"} and (
            path in {"/api/start", "/api/scan", "/api/ui-tars/trial"}
            or path.startswith(("/api/materials/", "/api/reviews/", "/api/rejections/"))
            or re.fullmatch(r"/api/tasks/[^/]+/(reject|retry|mark-review|rejection/undo)", path)
        ):
            return JSONResponse({"detail": "请在共享工作台管理素材和审核结果"}, status_code=409)
        # 安装期间禁止发起新的采集/标注，避免切换版本时仍有写入任务。
        if install.active() and request.method not in {"GET", "HEAD"} and (
            path in {"/api/start", "/api/scan", "/api/resume", "/api/capture/start",
                     "/api/ui-tars/trial", "/api/manual/capture"}
            or path.startswith("/api/annotation/")
        ):
            return JSONResponse({"detail": "更新安装进行中，已暂停新的采集操作"}, status_code=409)
        if request.method not in {"GET", "HEAD"} and not worker_protocol:
            supplied = request.headers.get("x-capture-token", "")
            origin = request.headers.get("origin")
            try:
                schemes = {"https"} if is_remote else ({"http", "https"} if hub else {"http"})
                origin_ok = (not origin and not is_remote) or (bool(origin) and urlsplit(origin).netloc.lower() == authority
                                                               and urlsplit(origin).scheme in schemes)
            except ValueError:
                origin_ok = False
            expected_token = remote_token if is_remote else token
            if not hmac.compare_digest(supplied.encode(), expected_token.encode()) or not origin_ok:
                return JSONResponse({"detail": "本机操作令牌无效，请刷新控制台"}, status_code=403)
        response = await call_next(request)
        # 内容哈希资源由路由给出 immutable 缓存，其余响应一律 no-store。
        if not (request.url.path.startswith("/assets/") and response.status_code == 200):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; connect-src 'self'"
        return response

    @app.exception_handler(SharedConflict)
    async def shared_conflict(request, exc):
        body = {"detail": str(exc)}
        if exc.conflicts is not None:
            body["conflicts"] = exc.conflicts
        return JSONResponse(body, status_code=409)

    @app.exception_handler(WorkerUnauthorized)
    async def worker_unauthorized(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        # 不序列化 Pydantic 的 input/context 字段，配置校验错误也不能泄露密钥。
        message = str(exc)
        if hasattr(exc, "errors"):
            message = "; ".join(".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors(include_input=False, include_context=False))
        return JSONResponse({"detail": message[:700]}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        return JSONResponse({"detail": "请求参数无效，请刷新页面重试"}, status_code=422)

    @app.exception_handler(OSError)
    async def file_error(request, exc):
        return JSONResponse({"detail": "本地路径不存在、无法读取或磁盘不可用，请检查素材目录"}, status_code=400)

    def idle():
        if runner.running:
            raise HTTPException(409, "采集运行中，请先暂停再修改配置或素材队列")
        if annotations.active:
            raise HTTPException(409, "人工标注进行中，请先结束标注再修改配置")


    # React 是唯一业务前端：入口 HTML 与构建资源都来自 web/react/。
    # 资源只按 Vite manifest 白名单返回，避免把构建目录里的其他文件暴露出去。
    react_root = ROOT / "web" / "react"


    register_update_routes(app, updates, install)


    task_center = TaskCenterService(store=store, materials=materials, hub=hub, worker=worker,
                                    runner=runner, configs=configs)
    app.state.task_center = task_center
    app.include_router(create_task_center_router(task_center))


 


    register_frontend_routes(
        app,
        react_root=react_root,
        worker=worker,
    )
    register_settings_routes(
        app,
        configs=configs,
        connections=connections,
        control_lock=control_lock,
        debug_log=debug_log,
        environment=environment,
        idle=idle,
        profiles=profiles,
        remote_token=remote_token,
        runner=runner,
        token=token,
    )
    register_capture_routes(
        app,
        configs=configs,
        connections=connections,
        control_lock=control_lock,
        environment=environment,
        hub=hub,
        idle=idle,
        materials=materials,
        profiles=profiles,
        remote_control_host=remote_control_host,
        runner=runner,
        store=store,
        task_center=task_center,
        worker=worker,
    )
    register_materials_routes(
        app,
        control_lock=control_lock,
        hub=hub,
        idle=idle,
        materials=materials,
        runner=runner,
        store=store,
    )
    register_quality_routes(
        app,
        control_lock=control_lock,
        hub=hub,
        idle=idle,
        materials=materials,
        rejections=rejections,
        runner=runner,
        store=store,
    )
    register_evidence_routes(
        app,
        configs=configs,
        debug_log=debug_log,
        materials=materials,
        runner=runner,
        store=store,
        worker=worker,
    )
    register_annotation_routes(
        app,
        annotations=annotations,
        control_lock=control_lock,
        runner=runner,
        worker=worker,
    )
    return app


def main():
    import uvicorn
    home = Path(os.environ.get("MENU_CAPTURE_HOME") or ROOT).expanduser().resolve()
    role = os.environ.get("MENU_CAPTURE_ROLE", "standalone")
    host = os.environ.get("MENU_CAPTURE_BIND", "127.0.0.1") if role == "hub" else "127.0.0.1"
    port = int(os.environ.get("MENU_CAPTURE_PORT", "8765"))
    password = os.environ.get("MENU_CAPTURE_ADMIN_PASSWORD", "")
    allowed_hosts = tuple(value.strip().lower() for value in os.environ.get("MENU_CAPTURE_ALLOWED_HOSTS", "").split(",")
                          if value.strip())
    home.joinpath("data").mkdir(parents=True, exist_ok=True)
    # 进程锁防止两个服务实例同时接管鼠标；不用新增桌面代理进程。
    try:
        with instance_lock(home / "data/runner.lock"):
            os.umask(0o077)
            app = create_app(
                home, role=role, admin_password=password, allowed_hosts=allowed_hosts,
                remote_control_host=os.environ.get("MENU_CAPTURE_REMOTE_CONTROL_HOST", ""),
                remote_control_password=os.environ.get("MENU_CAPTURE_REMOTE_CONTROL_PASSWORD", ""),
            )
            app.state.debug_log.install_service_logging()
            uvicorn.run(app, host=host, port=port, access_log=False, log_config=None)
    except InstanceLockedError:
        raise SystemExit("本机控制台已运行，请打开 http://127.0.0.1:8765")


if __name__ == "__main__":
    main()
