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
import json
import logging
import os
import socket
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image
from starlette.background import BackgroundTask

from menu_capture.core.config import ConfigStore, ROOT
from menu_capture.capture.capture_outputs import batch_output_dir
from menu_capture.quality.failures import describe as describe_failure
from menu_capture.capture.capture_history import ACTIVE_RUN_STATUSES, capture_history_row, history_timestamp
from menu_capture.capture.capture_results import capture_outcome, state_counts, store_state, waiting_for_materials
from menu_capture.storage.store import TaskStore
from menu_capture.capture.uipath_runner import UiPathRunner
from menu_capture.capture.robot_profile import ProfileStore
from menu_capture.desktop.image_tools import image_ocr, qr_boxes, prepare_clipboard_image, prepare_clipboard_text
from menu_capture.materials.materials import MaterialService
from menu_capture.materials.rejections import RejectionService
from menu_capture.core.environment import EnvironmentDiagnostics
from menu_capture.core.connections import ConnectionManager
from menu_capture.storage.debug_log import DebugLog
from menu_capture.vision.model_usage import UsageLedger
from menu_capture.updates.updates import UpdateService
from menu_capture.updates.update_install import InstallController
from menu_capture.shared.hub_update import HubInstallProxy
from menu_capture.annotation.annotation import AnnotationSession, RunnerExecutor
from menu_capture.core.instance_lock import InstanceLockedError, instance_lock
from menu_capture.shared.shared import SharedConflict, SharedHub, WorkerUnauthorized
from menu_capture.shared.delivery import DeliveryService
from menu_capture.api.shared_routes import register_shared_routes
from menu_capture.shared.shared_worker import SharedWorker
from menu_capture.projects.service import LEGACY_ID, ProjectNotFound, get_project, resolve_batch_project, resolve_task_project
from menu_capture.api.workbench_page import workbench_page
from menu_capture.api.context import ApiContext
from menu_capture.api.routers import (
    annotation as annotation_routes,
    delivery as delivery_routes,
    environment as environment_routes,
    model_usage as model_usage_routes,
    projects as project_routes,
    rejections as rejection_routes,
    reviews as review_routes,
    updates as update_routes,
)
from menu_capture.api.routers.reviews import REVIEW_RERUN_LIMIT

logger = logging.getLogger(__name__)


def _rejection_store_code(text: str) -> str:
    """提取门店编号（如 E02412），用于素材不合格与采集不合格按门店去重。"""
    name = Path(text or "").name
    match = re.match(r"^([A-Za-z]{1,4}\d+)", name)
    return (match.group(1) if match else name).upper()


def _project_record(store: TaskStore, project_id: str) -> dict:
    with store.connect() as db:
        return get_project(db, project_id)


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
    project_dir = ROOT.parent.parent / "compat/uipath"
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
    materials = MaterialService(root / "data", configs, store, role=role)
    hub = SharedHub(store, materials) if role == "hub" else None
    worker = SharedWorker(root, store, runner.output_lock, control_lock) if role == "worker" else None
    # 合格成品目录只在 Hub 维护：宿主机 bind mount 的交付根由运维显式配置，未配置时只读降级。
    delivery = None
    if role == "hub":
        try:
            delivery_poll = float(os.environ.get("MENU_CAPTURE_DELIVERY_POLL_SECONDS") or 5)
        except ValueError:
            delivery_poll = 5.0
        delivery = DeliveryService(
            root / "data",
            root=str(os.environ.get("MENU_CAPTURE_DELIVERY_ROOT") or "").strip(),
            host_path=str(os.environ.get("MENU_CAPTURE_DELIVERY_HOST_PATH") or "").strip(),
            task_provider=materials.effective_capture_tasks,
            project_provider=lambda project_id: _project_record(store, project_id),
            poll_seconds=delivery_poll,
        )
    if worker:
        runner.assignment_source = worker.source_root
        runner.project_binding_provider = worker.output_binding
        store.bind_assignments = worker.bind_run
    # Hub 的名称来自已同步的 shared_results，Worker/单机来自本地观察表；导出与清单复用同一入口。
    rejections = RejectionService(store, materials, current_only=role == "hub",
                                  observed_names=hub.observed_store_name if hub else None)
    # 已配对的共享采集端由工作台下发门店来源与版本，诊断据此把本机素材目录标记为不适用。
    environment = EnvironmentDiagnostics(
        configs, runner, project_dir,
        shared_mode_provider=(lambda: bool(worker.connection)) if worker is not None else None,
    )
    connections = ConnectionManager(environment, control_lock, root)
    annotations = AnnotationSession(configs, store, profiles, RunnerExecutor(runner),
                                    root=root if role != "standalone" else root.parent.parent, ocr_reader=image_ocr,
                                    rejector=rejections.mark_annotation_task,
                                    usage_ledger=model_usage)
    # 自动安装只在 Windows 采集端且运维显式启用后可用；执行器在独立进程中完成安装与回滚。
    # Hub 则由宿主机上的镜像更新执行器完成，容器只负责授权、转发与状态代理。
    if role == "hub":
        install = HubInstallProxy(
            base_url=str(os.environ.get("HUB_UPDATE_URL") or ""),
            token=str(os.environ.get("HUB_UPDATE_TOKEN") or ""),
            latest_provider=lambda: (updates.status_no_check().get("latest") or {}),
            # 与 Windows 执行器同一契约：后台调度提交镜像前消费授权，取消与消费互斥。
            authorization_check=updates.claim_install,
            authorization_release=updates.release_install_claim,
        )
        updates.install_capability = lambda: install.supported
        # 仅当宿主执行器在 /status 明确声明 OCI 能力时，才跳过完整归档下载改走分层拉取。
        # 读取的是代理缓存的快照，不发起网络请求，可在更新状态锁内安全调用。
        updates.oci_delivery_provider = lambda: install.oci_supported
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
            # 后台调度在真正拉起进程前消费授权；用户取消与消费共用更新锁，只会有一个成功。
            authorization_check=updates.claim_install,
            authorization_release=updates.release_install_claim,
            # 与清理共享文件系统互斥，避免清理删除安装正在读取的产物。
            filesystem_lock=updates.filesystem_lock,
        )
    # 上次进程可能在安装中途退出；启动时先收尾遗留事务，避免界面永久停在 installing。
    install.recover()
    # 安装/回滚期间禁止清理历史版本，避免删掉正在使用或待回滚的目标目录。
    updates.cleanup_guard = lambda: (
        (True, "安装或回滚进行中，请完成后再清理") if install.active() else (False, "")
    )
    # 清理在持有文件系统锁时只能用锁外只读探测复查安装状态。
    updates.install_active_provider = install.active_nonblocking
    # 安装状态上报给 Hub；执行器未接入时返回空，不推测状态。
    updates.install_status_provider = lambda: str(install.status().get("status") or "")
    # 一次授权更新的进度需要执行器的目标版本、消息与错误；只读取真实快照。
    updates.install_snapshot_provider = install.status
    # “空闲后更新”：执行器自行校验空闲与闸门，监控只负责在安全时机尝试启动。
    updates.schedule_installer = lambda version: install.start(
        version, version, require_authorization=True,
    )
    updates.resume_schedule()
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
        if delivery:
            delivery.start()
        if worker:
            worker.start()
            if sys.platform == "win32":
                connections.connect()
        try:
            yield
        finally:
            if delivery:
                delivery.stop()
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
        # 先按请求类型与方法短路：普通 GET 不做任何安装闸门探测，避免事件循环上的文件读取。
        if request.method not in {"GET", "HEAD"} and (
            path in {"/api/start", "/api/scan", "/api/resume", "/api/capture/start",
                     "/api/ui-tars/trial", "/api/manual/capture"}
            or path.startswith("/api/annotation/")
        ) and install.active_nonblocking():
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

    def project_scope(project_id: str | None = None, *, write: bool = False) -> str:
        """把 UI scope 与当前角色的可证明项目集合对齐。"""
        if role == "hub" and write and not project_id:
            raise HTTPException(400, "请选择项目")
        if role == "worker":
            if project_id is None:
                if worker and not worker._hub_supports_project_scope:
                    return LEGACY_ID
                raise HTTPException(400, "请选择项目")
            project = next((item for item in (worker.projects() if worker else [])
                            if item.get("id") == project_id), None)
            if project is None:
                raise HTTPException(404, "项目不存在或该采集端没有分配记录")
            if write and project.get("archived_at"):
                raise HTTPException(409, "项目已归档，不能执行采集")
            return project_id
        project_id = project_id or LEGACY_ID
        try:
            with store.connect() as db:
                project = get_project(db, project_id)
                if write and project["archived_at"]:
                    raise HTTPException(409, "项目已归档，不能写入")
        except ProjectNotFound:
            raise HTTPException(404, "项目不存在") from None
        if role == "standalone" and project_id != LEGACY_ID:
            raise HTTPException(404, "本机模式只支持历史兼容项目")
        return project_id

    def batch_scope(batch_id: str, project_id: str | None = None, *, write: bool = False,
                    include_deleted: bool = False) -> str:
        """资源路由可省略 scope 以兼容旧链接，但必须从批次 ID 反查真实归属。"""
        if role == "worker":
            if not project_id:
                raise HTTPException(400, "请选择项目")
            actual = False
            if worker:
                with store.connect() as db:
                    actual = bool(db.execute(
                        "SELECT 1 FROM worker_assignments WHERE batch_id=? AND project_id=? "
                        "AND worker_id=? LIMIT 1", (batch_id, project_id, worker._current_worker_id())
                    ).fetchone())
            if not actual:
                raise HTTPException(404, "素材批次不属于当前项目")
            return project_id
        try:
            with store.connect() as db:
                owner = resolve_batch_project(db, batch_id, include_deleted=include_deleted)
                if project_id and owner["id"] != project_id:
                    raise HTTPException(404, "素材批次不属于当前项目")
                if write and owner["archived_at"]:
                    raise HTTPException(409, "项目已归档，不能写入")
                if role == "standalone" and owner["id"] != LEGACY_ID:
                    raise HTTPException(404, "本机模式只支持历史兼容项目")
                return owner["id"]
        except ProjectNotFound:
            raise HTTPException(404, "素材批次不存在") from None

    def task_scope(task_id: str, project_id: str | None = None, *, write: bool = False) -> str:
        """只允许访问能由批次、轮次或当前设备分配事实证明归属的任务。"""
        scope = project_scope(project_id, write=write)
        if role == "worker":
            with store.connect() as db:
                if worker and worker._hub_supports_project_scope:
                    owned = db.execute(
                        "SELECT 1 FROM worker_assignments WHERE task_id=? AND project_id=? AND worker_id=? LIMIT 1",
                        (task_id, scope, worker._current_worker_id()),
                    ).fetchone()
                else:
                    owned = db.execute(
                        "SELECT 1 FROM worker_assignments WHERE task_id=? AND (worker_id='' OR worker_id=?) LIMIT 1",
                        (task_id, worker._current_worker_id() if worker else ""),
                    ).fetchone()
            if not owned:
                raise HTTPException(404, "任务不属于当前项目或采集端")
            return scope
        with store.connect() as db:
            owner = resolve_task_project(db, task_id)
            if owner and owner["id"] == scope:
                return scope
        if role == "standalone":
            try:
                store.get(task_id)
                return scope
            except ValueError:
                current = runner.current_task or {}
                if current.get("id") == task_id:
                    return scope
        raise HTTPException(404, "任务不属于当前项目或归属待核验")

    def task_project_id(task_id: str) -> str | None:
        if worker:
            with store.connect() as db:
                rows = db.execute(
                    "SELECT DISTINCT project_id FROM worker_assignments WHERE task_id=? AND worker_id=?",
                    (task_id, worker._current_worker_id()),
                ).fetchall()
            projects = {str(row[0] or "") for row in rows}
            if worker._hub_supports_project_scope:
                return next(iter(projects)) if len(projects) == 1 and "" not in projects else None
            return LEGACY_ID if rows else None
        with store.connect() as db:
            owner = resolve_task_project(db, task_id)
        if owner:
            return owner["id"] if role != "standalone" or owner["id"] == LEGACY_ID else None
        if role == "standalone":
            try:
                store.get(task_id)
                # 独立模式只有一个历史兼容工作区；旧的本机任务仍能读取和处置，
                # 但不会把这条临时兼容判断写回未知历史归属。
                return LEGACY_ID
            except ValueError:
                return None
        return None

    def run_project_matches(run: dict | None, project_id: str) -> bool:
        if not run:
            return False
        owner = str(run.get("project_id") or "")
        if owner:
            return owner == project_id
        if role == "standalone":
            return project_id == LEGACY_ID
        return bool(run.get("task_ids")) and {task_project_id(task_id) for task_id in run["task_ids"]} == {project_id}

    def require_run_scope(project_id: str) -> dict:
        run = store.latest_run()
        if not run_project_matches(run, project_id):
            raise HTTPException(404, "采集轮次不属于当前项目或归属待核验")
        return run

    rejections.task_project_provider = task_project_id
    if hub:
        rejections.observed_names = hub.observed_store_name

    # 各业务域通过 ApiContext 注入依赖后自行注册路由，server.py 不再持有这些 handler。
    ctx = ApiContext(
        updates=updates,
        install=install,
        model_usage=model_usage,
        environment=environment,
        connections=connections,
        runner=runner,
        annotations=annotations,
        rejections=rejections,
        store=store,
        materials=materials,
        hub=hub,
        worker=worker,
        delivery=delivery,
        control_lock=control_lock,
        idle=idle,
        project_scope=project_scope,
        batch_scope=batch_scope,
        task_scope=task_scope,
        role=role,
    )
    update_routes.register(app, ctx)
    model_usage_routes.register(app, ctx)
    environment_routes.register(app, ctx)
    rejection_routes.register(app, ctx)
    review_routes.register(app, ctx)
    annotation_routes.register(app, ctx)
    delivery_routes.register(app, ctx)

    def project_archive_blockers(db, project_id: str) -> list[str]:
        """在项目归档写事务内读取全部仍可继续推进的业务记录。"""
        reasons = []
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        active_run_statuses = tuple(sorted(ACTIVE_RUN_STATUSES))
        if "material_batches" in tables:
            if db.execute("SELECT 1 FROM material_batches WHERE project_id=? AND status='uploading' AND deleted_at='' LIMIT 1",
                          (project_id,)).fetchone():
                reasons.append("存在未完成的素材上传")
            if "material_jobs" in tables and db.execute(
                "SELECT 1 FROM material_jobs j JOIN material_batches b ON b.id=j.batch_id "
                "WHERE b.project_id=? AND j.status IN ('queued','running','pause_requested','paused','interrupted') LIMIT 1",
                (project_id,),
            ).fetchone():
                reasons.append("存在未结束的素材分析任务")
        if "runs" in tables:
            marks = ",".join("?" for _ in active_run_statuses)
            run_owner = "r.project_id=?" if "project_id" in {row[1] for row in db.execute("PRAGMA table_info(runs)")} else "0"
            predicates = [run_owner]
            params: list[object] = [project_id] if run_owner != "0" else []
            if "material_batches" in tables and "material_batch_id" in {row[1] for row in db.execute("PRAGMA table_info(runs)")}:
                predicates.append("EXISTS (SELECT 1 FROM material_batches b WHERE b.id=r.material_batch_id AND b.project_id=?)")
                params.append(project_id)
            if "capture_run_results" in tables:
                result_columns = {row[1] for row in db.execute("PRAGMA table_info(capture_run_results)")}
                if "project_id" in result_columns:
                    predicates.append("EXISTS (SELECT 1 FROM capture_run_results c WHERE c.run_id=r.id AND c.project_id=?)")
                    params.append(project_id)
                if "batch_id" in result_columns and "material_batches" in tables:
                    predicates.append("EXISTS (SELECT 1 FROM capture_run_results c JOIN material_batches b "
                                      "ON b.id=c.batch_id WHERE c.run_id=r.id AND b.project_id=?)")
                    params.append(project_id)
            sql = f"SELECT 1 FROM runs r WHERE r.status IN ({marks}) AND ({' OR '.join(predicates)}) LIMIT 1"
            if db.execute(sql, (*active_run_statuses, *params)).fetchone():
                reasons.append("存在运行中或可恢复的采集轮次")
        if "shared_assignments" in tables:
            assignment_cols = {row[1] for row in db.execute("PRAGMA table_info(shared_assignments)")}
            project_clause = "a.project_id=?" if "project_id" in assignment_cols else "0"
            assignment_params: list[object] = [project_id] if project_clause != "0" else []
            if "material_batches" in tables:
                project_clause += " OR EXISTS (SELECT 1 FROM material_batches b WHERE b.id=a.batch_id AND b.project_id=?)"
                assignment_params.append(project_id)
            active_assignment = db.execute(
                "SELECT 1 FROM shared_assignments a WHERE a.status IN ('assigned','ready') "
                f"AND ({project_clause}) LIMIT 1", assignment_params,
            ).fetchone()
            if active_assignment:
                reasons.append("存在尚未结束的采集端分配")
            if "shared_workers" in tables and db.execute(
                "SELECT 1 FROM shared_workers w WHERE w.pending_uploads>0 AND "
                "(EXISTS (SELECT 1 FROM shared_assignments a WHERE a.worker_id=w.id AND a.status IN ('assigned','ready') "
                f"AND ({project_clause})) OR EXISTS (SELECT 1 FROM shared_assignments a WHERE a.worker_id=w.id "
                "AND a.status IN ('assigned','ready') AND COALESCE(a.project_id,'')='')) LIMIT 1",
                assignment_params,
            ).fetchone():
                reasons.append("存在未确认归属的采集端待上传结果")
        if runner.running:
            current = store.get_run(str(getattr(runner, "run_id", "") or ""))
            if not current or not current.get("project_id") or current.get("project_id") == project_id:
                reasons.append("采集执行器仍处于运行状态")
        if annotations.active:
            # 标注会话尚未保存项目快照，故暂时阻止任何项目归档。
            reasons.append("存在项目归属尚未固定的人工标注会话")
        return list(dict.fromkeys(reasons))

    project_routes.register(app, store=store, role=role, archive_blockers=project_archive_blockers,
                            control_lock=control_lock, worker=worker)

    # React 是唯一业务前端：入口 HTML 与构建资源都来自 web/react/。
    # 资源只按 Vite manifest 白名单返回，避免把构建目录里的其他文件暴露出去。
    react_root = ROOT / "web" / "react"

    def react_bundle() -> tuple[str, dict[str, Path], str]:
        """读取一次 manifest，返回入口 HTML、允许对外提供的资源清单与入口文件名。"""
        try:
            manifest = json.loads((react_root / ".vite" / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("前端控制台尚未构建，请先运行 web-react 的 npm run build") from error
        entries = [item for item in manifest.values()
                   if isinstance(item, dict) and item.get("isEntry")] if isinstance(manifest, dict) else []
        if len(entries) != 1 or not isinstance(entries[0].get("file"), str):
            raise RuntimeError("前端构建清单缺少唯一入口文件")
        entry = entries[0]
        assets: dict[str, Path] = {}
        names = [entry["file"], *[item for item in entry.get("css", []) if isinstance(item, str)]]
        for name in names:
            relative = PurePosixPath(name)
            # 只接受 assets/<文件名>，拒绝绝对路径、..、嵌套目录与隐藏文件。
            if (relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "assets"
                    or relative.parts[1].startswith(".")):
                raise RuntimeError("前端构建清单包含非法资源路径")
            target = react_root.joinpath(*relative.parts)
            if not target.is_file():
                raise RuntimeError(f"前端构建产物缺失: {name}")
            assets[relative.parts[1]] = target
        try:
            html = (react_root / "index.html").read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError("前端入口 HTML 缺失") from error
        return html, assets, entry["file"]

    @app.get("/", response_class=HTMLResponse)
    def index():
        try:
            html, _, _ = react_bundle()
        except RuntimeError as error:
            return HTMLResponse(str(error), status_code=503)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/remote-control", response_class=HTMLResponse)
    def remote_control_page():
        if worker is None:
            raise HTTPException(404, "半自动控制仅在 Windows 采集端可用")
        return index()

    @app.get("/assets/{name}")
    def react_assets(name: str):
        """前端静态资源：按 manifest 白名单返回内容哈希文件，长期缓存。"""
        try:
            _, assets, _ = react_bundle()
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
        target = assets.get(name)
        media_types = {
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".woff2": "font/woff2",
            ".png": "image/png",
        }
        media_type = media_types.get(target.suffix.lower()) if target else None
        if target is None or media_type is None:
            raise HTTPException(404, "资源不存在")
        return FileResponse(target, media_type=media_type,
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/favicon.svg")
    def favicon():
        target = react_root / "favicon.svg"
        if not target.is_file():
            raise HTTPException(404, "资源不存在")
        return FileResponse(target, media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/version")
    def version_info():
        # 发布版本由安装启动器或镜像环境的 MENU_CAPTURE_VERSION 提供；
        # 入口哈希文件名即前端构建 ID，便于升级/回滚后核对实际运行产物。
        frontend = ""
        try:
            _, _, entry = react_bundle()
            frontend = PurePosixPath(entry).name
        except RuntimeError:
            frontend = ""
        version = os.environ.get("MENU_CAPTURE_VERSION", "")
        commit = ""
        # 老守护进程升级时仍在内存中执行旧启动脚本，可能没有传入版本环境变量。
        # 从本次运行目录的发布清单补齐，绝不根据其他版本目录推断；提交号用于确认运行的就是已安装构建。
        try:
            release = json.loads((ROOT.parent / "release-manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            release = {}
        if isinstance(release, dict):
            if worker:
                version = str(release.get("version") or version)
            commit = str(release.get("git_commit") or "")
        return {"version": version, "frontend": frontend, "commit": commit}

    @app.get("/api/config")
    def configuration():
        return configs.public()

    @app.post("/api/select-directory")
    def select_directory(payload: dict):
        kind = payload.get("kind")
        prompts = {
            "materials": "选择素材目录",
            "input": "选择输入目录",
            "output": "选择输出目录",
        }
        if kind not in prompts:
            raise ValueError("目录类型无效")
        prompt = prompts[kind]
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],
                capture_output=True, text=True, timeout=900,
            )
            if result.returncode:
                if "-128" in result.stderr:
                    return {"cancelled": True}
                raise HTTPException(500, "系统目录选择器打开失败")
            selected = result.stdout.strip()
        elif os.name == "nt":
            script = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
                f"$dialog.Description = '{prompt}'; "
                "$dialog.ShowNewFolderButton = $true; "
                "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ [Console]::Write($dialog.SelectedPath) }"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
            )
            if result.returncode:
                raise HTTPException(500, "系统目录选择器打开失败")
            selected = result.stdout.strip()
            if not selected:
                return {"cancelled": True}
        else:
            raise HTTPException(501, "当前系统暂不支持原生目录选择")
        path = Path(selected).expanduser().resolve()
        if not path.is_dir():
            raise HTTPException(400, "所选目录不可用")
        return {"cancelled": False, "path": str(path)}

    @app.get("/api/session")
    def local_session(request: Request):
        # 远程控制与本机控制台使用不同权限范围的令牌。
        return {"token": remote_token if request.state.remote_control else token}

    @app.post("/api/config")
    def configure(payload: dict):
        with control_lock:
            idle()
            previous_model = connections._fingerprint()
            result = configs.save(payload)
            debug_log.configure(result["debug_logging"]["max_storage_mb"])
            runner.invalidate_readiness()
            if previous_model != connections._fingerprint():
                environment.invalidate()
            else:
                environment.invalidate("uipath_desktop")
            connections.invalidate()
            return result

    def _run_progress(run: dict) -> dict:
        """本运行的真实进度：按 run_actions 里已出结果的任务统计。

        恢复/阻塞后 current_index 会从 0 重新计数，不能再用它算“已处理”，
        否则前端进度会倒退、任务中心显示 3/300 这类错误数字。
        """
        ids = list(run.get("task_ids") or [])
        if run.get("status") in {"completed", "failed", "stopped"}:
            # 终态运行的全部门店都已处理，必须带回 done_ids，否则统计会把完成项误算成需检查。
            return {"ids": ids, "processed": len(ids), "done_ids": ids}
        try:
            with store.connect() as db:
                rows = db.execute(
                    "SELECT DISTINCT task_id FROM run_actions WHERE run_id=? AND result!='running'",
                    (run["id"],)).fetchall()
        except Exception:
            rows = []
        done = [row["task_id"] for row in rows if row["task_id"] in set(ids)]
        if not done:
            done = ids[:min(max(int(run.get("current_index") or 0), 0), len(ids))]
        return {"ids": ids, "processed": len(done), "done_ids": done}

    @app.get("/api/status")
    def status(queue_page: Annotated[int | None, Query(ge=1)] = None,
               queue_size: Annotated[int, Query(ge=1, le=100)] = 50,
               queue_scope: Literal["all", "current"] = "all",
               queue_batch: Annotated[str, Query(max_length=128)] = "",
               queue_q: Annotated[str, Query(max_length=300)] = "",
               queue_filter: Literal["all", "pending", "running", "captured", "rejected", "review", "returned"] = "all",
               queue_sort: Literal["source", "recent", "status"] = "source",
               selected: Annotated[str, Query(max_length=9000)] = "",
               include_events: bool = True, project_id: str | None = None):
        # 任务表保留历史；更换成清洗后的目录时，只展示当前素材集，避免旧照片继续可选。
        settings = configs.load()
        scope = project_scope(project_id)
        if queue_batch and queue_batch != "all":
            batch_scope(queue_batch, scope)
        input_dir = settings.input_dir
        current_root = Path(input_dir).expanduser().resolve() if input_dir else None
        release = materials.active_release(current_root or Path(), project_id=scope)
        paged = hub is not None and queue_page is not None
        if hub:
            # 共享模式按各批次最新发布版本取数，不需要逐条解析旧任务的文件路径。
            tasks = materials.effective_capture_tasks(project_id=scope)
            ready = {"ready": True, "issues": [], "launch_mode": "shared"}
        else:
            if worker:
                tasks = worker.tasks(project_id=scope)
            else:
                tasks = [task for task in store.tasks()
                         if current_root and current_root in Path(task["source_path"]).resolve().parents]
            ready = runner.last_readiness or {"ready": None, "issues": ["环境诊断可在设置中手动查看"],
                                              "launch_mode": profiles.load()["launch_mode"]}
        all_tasks = [] if paged else tasks
        if sys.platform == "win32" and not hub:
            ready = ready | {"launch_mode": "native"}
        run = None if paged else store.latest_run()
        if run and not run_project_matches(run, scope):
            run = None
        if run:
            by_id = {task["id"]: task for task in all_tasks}
            ids = run["task_ids"]
            progress = _run_progress(run)
            processed = progress["processed"]
            processed_tasks = [by_id[task_id] for task_id in progress.get("done_ids", []) if task_id in by_id]
            captured = sum(task.get("status") == "captured" for task in processed_tasks)
            unqualified = sum(task.get("status") != "captured" and bool(task.get("reject_reason"))
                              for task in processed_tasks)
            # 跨批次重跑没有单一素材批次可展示：按任务来源快照数量识别。
            snapshots = {Path(by_id[task_id]["source_path"]).parent.parent.name
                         for task_id in ids if task_id in by_id}
            run.update(processed=processed, captured=captured, unqualified=unqualified,
                       review=processed-captured-unqualified,
                       remaining=len(ids)-processed, total=len(ids),
                       processed_ids=progress.get("done_ids", ids[:processed]),
                       cross_batch=len(snapshots) > 1, batch_count=len(snapshots),
                       task_list=[{"id": task_id, "name": by_id.get(task_id, {}).get("source_name", ""),
                                   "status": by_id.get(task_id, {}).get("status", "")} for task_id in ids],
                       next_name=by_id.get(ids[processed], {}).get("source_name", "") if processed < len(ids) else "")
        if run:
            recorded = store.capture_run_results([run["id"]]).get(run["id"], [])
            if any(row["outcome"] for row in recorded):
                outcomes = Counter(row["outcome"] for row in recorded)
                processed_ids = [row["task_id"] for row in recorded if row["outcome"]]
                run.update(processed=len(processed_ids), processed_ids=processed_ids,
                           result_by_task={row["task_id"]: row["outcome"] for row in recorded if row["outcome"]},
                           captured=outcomes["captured"], review=outcomes["review"],
                           unqualified=outcomes["rejected"], remaining=len(run["task_ids"])-len(processed_ids))
        tasks = [task | {"capture_outcome": capture_outcome(task)} for task in tasks]
        # 素材清洗拒绝的门店可能没有采集任务，控制台必须与不合格清单同口径计入。
        material_rejected = 0
        if release:
            with store.connect() as db:
                rows = db.execute(
                    "SELECT store_json FROM material_decisions WHERE batch_id=? AND decision='skipped'",
                    (release["batch_id"],),
                ).fetchall()
            task_codes = {_rejection_store_code(task.get("source_name")) for task in tasks if task.get("reject_reason")
                          and (not paged or task.get("snapshot_id") == release["snapshot_id"])}
            material_codes = set()
            for row in rows:
                try:
                    data = json.loads(row["store_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    data = {}
                code = _rejection_store_code(data.get("source_folder") or data.get("name") or "")
                if code:
                    material_codes.add(code)
            material_rejected = len(material_codes - task_codes)
        # 全部批次口径与批次管理列表保持一致：只算未删除批次（回收站不计），
        # 并扣除已经有采集不合格记录的同一门店，避免同一家店被计两次。
        with store.connect() as db:
            all_rows = db.execute(
                "SELECT d.store_json FROM material_decisions d "
                "JOIN material_batches b ON b.id=d.batch_id "
                "WHERE d.decision='skipped' AND b.deleted_at='' AND b.project_id=?", (scope,)
            ).fetchall()
        capture_codes = {_rejection_store_code(task.get("source_name")) for task in store.rejections()
                         if task_project_id(task["id"]) == scope}
        all_material_codes = set()
        for row in all_rows:
            try:
                data = json.loads(row["store_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                data = {}
            code = _rejection_store_code(data.get("source_folder") or data.get("name") or "")
            if code:
                all_material_codes.add(code)
        material_rejected_all = len(all_material_codes - capture_codes)
        launch_mode = ready.get("launch_mode", "manual")
        engine, engine_message = {
            "shared": ("共享工作台", "请将门店分配给已配对的 Windows 电脑"),
            "native": ("Windows 原生", "本机 Windows 桌面采集已就绪"),
            "cli": ("UiPath", "已就绪，点击开始后由 UiPath CLI 自动启动 Main.xaml"),
        }.get(launch_mode, ("UiPath", "已就绪，等待手动在 Studio 运行 Main.xaml"))
        queue = workbench_page(
            tasks, snapshot=(release["snapshot_id"] if release else "") if queue_scope == "current" else None,
            query=queue_q, status=queue_filter, sort=queue_sort, page=queue_page, size=queue_size,
            selected_ids=set(selected.split(",")), batch_id=queue_batch,
        ) if paged else None
        events = store.events() if include_events else []
        if role != "standalone":
            events = [event for event in events
                      if event.get("task_id") and task_project_id(str(event["task_id"])) == scope]
        output_path = ""
        if settings.output_dir:
            if role == "worker" and worker:
                project = next((item for item in worker.projects() if item["id"] == scope), None)
                if project:
                    output_path = str(batch_output_dir(settings.output_dir, "0" * 32,
                                                       project_code=project["code"]).parent)
            elif hub and release:
                with store.connect() as db:
                    project_code = get_project(db, scope)["code"]
                output_path = str(batch_output_dir(settings.output_dir, release["batch_id"],
                                                   release.get("batch_name", ""), project_code=project_code))
            elif release:
                output_path = str(batch_output_dir(settings.output_dir, release["batch_id"],
                                                   release.get("batch_name", "")))
            else:
                output_path = str(Path(settings.output_dir).expanduser().resolve())
        return {"execution_engine": engine, "launch_mode": launch_mode,
                "engine_ready": ready["ready"], "engine_message": "；".join(ready["issues"]) or engine_message,
                "robot_readiness": ready, "run_directory": str(getattr(runner, "run_directory", "") or ""),
                "running": runner.running, "stop_requested": runner.stop.is_set(),
                "run": run, "current_task": runner.current_task if run else None,
                "material_release": release,
                "batch_output_dir": output_path,
                "input_name": "本机分配" if worker else (current_root.name if current_root else ""),
                "tasks": queue["records"] if queue else [task | describe_failure(task.get("error_code", ""), task.get("stage", ""), task.get("message", "")) | {"store_state": store_state(task)} for task in tasks],
                "counts": queue["counts"] if queue else state_counts(tasks),
                **({"queue": {key: value for key, value in queue.items() if key not in {"records", "counts"}}} if queue else {}),
                "material_rejected": material_rejected, "material_rejected_all": material_rejected_all,
                "events": events,
                "manual": runner.manual_state() if runner.running and run else None}

    @app.get("/api/capture/queue")
    def capture_queue(batch_ids: str = "all", q: str = "", project_id: str | None = None):
        """跨批次有效任务清单：列表、统计、全选和开跑共用这一份数据。"""
        scope = project_scope(project_id)
        wanted = None if batch_ids in {"", "all"} else [value for value in batch_ids.split(",") if value]
        records = worker.tasks(project_id=scope) if worker else materials.effective_capture_tasks(project_id=scope)
        if wanted is not None:
            for batch_id in wanted:
                batch_scope(batch_id, scope)
            records = [row for row in records if row["batch_id"] in set(wanted)]
        needle = q.strip().casefold()
        if needle:
            records = [row for row in records
                       if needle in (row["source_name"] + " " + row["batch_name"]).casefold()]
        batches = {}
        for row in records:
            item = batches.setdefault(row["batch_id"], {"batch_id": row["batch_id"], "batch_name": row["batch_name"],
                                                        "version": row["version"], "stores": 0})
            item["stores"] += 1
            item["version"] = max(item["version"], row["version"])
        enriched = [row | describe_failure(row.get("reject_reason") or row.get("error_code", ""),
                                           row.get("stage", ""), row.get("message", ""))
                    for row in records]
        return {
            "records": enriched,
            "batches": sorted(batches.values(), key=lambda item: item["batch_name"]),
            "summary": {"stores": len(enriched),
                        "runnable": sum(bool(row["runnable"]) for row in enriched),
                        "batches": len(batches)},
        }

    @app.get("/api/materials/batches")
    def material_batches(view: Literal["full", "options"] = "full", project_id: str | None = None):
        scope = project_scope(project_id)
        return {"batches": materials.batch_options(project_id=scope) if view == "options"
                else materials.list_batches(project_id=scope)}

    @app.get("/api/materials/batch-management")
    def manage_material_batches(deleted: bool = False, project_id: str | None = None):
        scope = project_scope(project_id)
        return {"batches": materials.batch_management.list(deleted=deleted, project_id=scope)}

    @app.post("/api/materials/batches")
    def import_local_materials(payload: dict):
        if set(payload) - {"input_dir", "name", "description", "project_id"} or not isinstance(payload.get("name"), str):
            raise ValueError("请填写批次名称")
        if not isinstance(payload.get("input_dir"), str) or not payload["input_dir"].strip():
            raise ValueError("请选择素材目录")
        scope = project_scope(payload.get("project_id"), write=True)
        with control_lock:
            idle()
            return materials.create_local_batch(Path(payload["input_dir"]),
                                                name=payload["name"], description=payload.get("description", ""),
                                                project_id=scope)

    @app.post("/api/materials/uploads")
    def begin_material_upload(payload: dict):
        if set(payload) - {"name", "description", "project_id"} or not isinstance(payload.get("name"), str):
            raise ValueError("请填写批次名称")
        scope = project_scope(payload.get("project_id"), write=True)
        with control_lock:
            idle()
            return materials.begin_upload(name=payload["name"], description=payload.get("description", ""),
                                          project_id=scope)

    @app.patch("/api/materials/batches/{batch_id}")
    def edit_material_batch(batch_id: str, payload: dict, project_id: str | None = None):
        if set(payload) - {"name", "description"} or not isinstance(payload.get("name"), str):
            raise ValueError("请填写批次名称")
        scope = batch_scope(batch_id, project_id, write=True)
        with control_lock:
            return materials.batch_management.update(batch_id, name=payload["name"],
                                                     description=payload.get("description", ""), project_id=scope)

    @app.delete("/api/materials/batches/{batch_id}")
    def delete_material_batch(batch_id: str, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True)
        with control_lock:
            try:
                if hub:
                    hub.require_unassigned(batch_id)
                return {"ok": True, "batch": materials.batch_management.delete(batch_id, project_id=scope)}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/batches/{batch_id}/restore")
    def restore_material_batch(batch_id: str, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True, include_deleted=True)
        with control_lock:
            return materials.batch_management.restore(batch_id, project_id=scope)

    @app.delete("/api/materials/batches/{batch_id}/permanent")
    def purge_material_batch(batch_id: str, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True, include_deleted=True)
        with control_lock:
            try:
                if hub:
                    hub.require_unassigned(batch_id)
                return {"ok": True, **materials.batch_management.purge(batch_id, project_id=scope)}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/selection/clear")
    def clear_material_selection(payload: dict):
        if set(payload) - {"batch_id", "project_id"}:
            raise ValueError("清除选择参数无效")
        scope = project_scope(payload.get("project_id"), write=True)
        with control_lock:
            idle()
            try:
                result = materials.batch_management.clear_selection(batch_id=payload.get("batch_id"), project_id=scope)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            runner.invalidate_readiness()
            return result

    @app.put("/api/materials/uploads/{batch_id}/file")
    async def upload_material_file(batch_id: str, path: str, request: Request, project_id: str | None = None):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 50 * 1024 * 1024:
                raise HTTPException(413, "单张图片不能超过 50 MB")
            data.extend(chunk)
        with control_lock:
            scope = batch_scope(batch_id, project_id, write=True)
            return materials.add_upload_file(batch_id, path, bytes(data), project_id=scope)

    @app.post("/api/materials/uploads/{batch_id}/finish")
    def finish_material_upload(batch_id: str, project_id: str | None = None):
        with control_lock:
            scope = batch_scope(batch_id, project_id, write=True)
            return materials.finish_upload(batch_id, project_id=scope)

    @app.post("/api/materials/uploads/{batch_id}/abort")
    def abort_material_upload(batch_id: str, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True)
        with control_lock:
            return materials.abort_upload(batch_id, project_id=scope)

    @app.post("/api/materials/batches/{batch_id}/analyze")
    def analyze_materials(batch_id: str, project_id: str | None = None):
        with control_lock:
            scope = batch_scope(batch_id, project_id, write=True)
            try:
                return materials.start_analysis(batch_id, project_id=scope)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/batches/{batch_id}/reanalyze")
    def reanalyze_materials(batch_id: str, payload: dict, project_id: str | None = None):
        store_ids = payload.get("store_ids")
        if not isinstance(store_ids, list) or not store_ids or any(not isinstance(item, str) for item in store_ids):
            raise HTTPException(422, "store_ids 必须是非空字符串数组") from None
        if len(store_ids) > 2000:
            raise HTTPException(422, "单次最多重新分析 2000 家门店") from None
        with control_lock:
            scope = batch_scope(batch_id, project_id, write=True)
            try:
                return materials.start_reanalysis(batch_id, store_ids, project_id=scope)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None

    @app.post("/api/materials/batches/{batch_id}/retry-failed")
    def retry_failed_materials(batch_id: str, project_id: str | None = None):
        with control_lock:
            scope = batch_scope(batch_id, project_id, write=True)
            try:
                return materials.retry_failed(batch_id, project_id=scope)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None

    def _batch_name(batch_id: str) -> str:
        return materials.batch_name(batch_id)

    @app.get("/api/materials/jobs")
    def material_jobs(active: bool = True, batch_id: str = "", limit: int = 20,
                      project_id: str | None = None):
        scope = project_scope(project_id)
        if batch_id:
            batch_scope(batch_id, scope)
        jobs = materials.list_jobs(active=active, batch_id=batch_id, limit=max(1, min(100, limit)),
                                   project_id=scope)
        return {"jobs": [{**job, "batch_name": job.get("batch_name") or _batch_name(job["batch_id"])}
                         for job in jobs]}

    @app.get("/api/materials/jobs/{job_id}")
    def material_job(job_id: str, events: bool = True, project_id: str | None = None):
        scope = project_scope(project_id)
        try:
            return materials.job_detail(job_id, events=events, project_id=scope)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/materials/jobs/{job_id}/pause")
    def pause_material_job(job_id: str, project_id: str | None = None):
        with control_lock:
            scope = project_scope(project_id, write=True)
            try:
                return materials.pause_job(job_id, project_id=scope)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/jobs/{job_id}/resume")
    def resume_material_job(job_id: str, project_id: str | None = None):
        with control_lock:
            scope = project_scope(project_id, write=True)
            try:
                return materials.resume_job(job_id, project_id=scope)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/jobs/{job_id}/cancel")
    def cancel_material_job(job_id: str, project_id: str | None = None):
        with control_lock:
            scope = project_scope(project_id, write=True)
            try:
                return materials.cancel_job(job_id, project_id=scope)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.get("/api/tasks/active")
    def active_tasks(project_id: str | None = None):
        """任务中心唯一入口：把素材分析与采集运行统一成同一结构，前端不再自行合并两个接口。"""
        scope = project_scope(project_id)
        items = []
        jobs = materials.list_jobs(active=True, limit=20, project_id=scope)
        seen = {job["id"] for job in jobs}
        jobs += [job for job in materials.jobs.history(100, project_id=scope)
                 if job["id"] not in seen and job.get("status") not in
                 {"queued", "running", "pause_requested", "paused", "interrupted"}]
        for job in jobs:
            items.append({
                "source": "materials",
                "id": job["id"],
                "title": job.get("batch_name") or _batch_name(job["batch_id"]),
                "kind": job["kind"],
                "status": job["status"],
                "progress": {"done": job["done_stores"], "total": job["total_stores"]},
                "extra": {"batch_id": job["batch_id"], "queue_position": job.get("queue_position", 0),
                          "eta_seconds": job.get("eta_seconds", 0), "stage": job.get("stage", ""),
                          "current": job.get("current_store_name", ""), "failed": job["failed_stores"]},
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
            })
        # 共享后台不执行桌面采集。迁移过来的本地轮次留在历史页，采集端分配以共享状态为准。
        run = None if hub else store.latest_run()
        if run and not run_project_matches(run, scope):
            run = None
        status = ""
        if run:
            status = "running" if runner.running else {"completed": "completed", "failed": "failed",
                                                       "stopped": "paused", "interrupted": "interrupted"}.get(run["status"], run["status"])
            # 与素材任务一致：只展示运行态或最近结束的任务，历史批次不常驻任务中心。
            cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
            if status in {"completed", "failed"} and run["updated_at"] < cutoff:
                run = None
        if run:
            setting = configs.load()
            current_root = Path(setting.input_dir).expanduser().resolve() if setting.input_dir else None
            tasks = worker.tasks(project_id=scope) if worker else [
                task for task in store.tasks()
                if current_root and current_root in Path(task["source_path"]).resolve().parents
            ]
            by_id = {task["id"]: task for task in tasks}
            ids = run["task_ids"]
            progress = _run_progress(run)
            processed = progress["processed"]
            snapshots = {Path(by_id[task_id]["source_path"]).parent.parent.name
                         for task_id in ids if task_id in by_id}
            items.append({
                "source": "capture",
                "id": run["id"],
                "title": run.get("name") or (f"待检查重跑 · 跨 {len(snapshots)} 个批次" if len(snapshots) > 1
                                             else (current_root.name if current_root else run["mode"])),
                "kind": run["mode"],
                "status": status,
                "progress": {"done": processed, "total": len(ids)},
                "extra": {"run_id": run["id"], "current": by_id.get(ids[processed], {}).get("source_name", "")
                          if processed < len(ids) else "", "next": by_id.get(ids[processed], {}).get("source_name", "")
                          if processed < len(ids) else "", "mode": run["mode"],
                          "stop_requested": runner.stop.is_set()},
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
            })
        if hub:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
            for row in hub.history_records(shortcut_since=cutoff.isoformat(timespec="seconds"), project_id=scope):
                if row["status"] not in ACTIVE_RUN_STATUSES and history_timestamp(row["updated_at"]) < cutoff.timestamp():
                    continue
                items.append({
                    "source": "capture", "id": row["id"], "title": row["title"],
                    "kind": row["mode"], "status": row["status"],
                    "progress": {"done": row["counts"]["processed"], "total": row["counts"]["total"]},
                    "extra": {"worker_id": row["worker_id"], "worker_name": row["worker_name"],
                              "synced_at": row["synced_at"]},
                    "created_at": row["created_at"], "updated_at": row["updated_at"],
                })
        return {"tasks": items}

    def capture_history_task_index(project_id: str | None = None) -> dict:
        # 在采集端按分配记录补充旧轮次来源；在共享后台按当前发布快照提取批次。
        tasks = {task["id"]: task for task in store.tasks()}
        if worker:
            with store.connect() as db:
                if worker._hub_supports_project_scope:
                    owner_filter = "worker_id=? AND project_id=?"
                    params = (worker._current_worker_id(), project_id)
                else:
                    owner_filter = "(worker_id='' OR worker_id=?)"
                    params = (worker._current_worker_id(),)
                assigned = db.execute("SELECT task_id,batch_id,batch_name,revision AS version,project_id,project_code,project_name "
                                      "FROM worker_assignments WHERE " + owner_filter + " ORDER BY rowid",
                                      params)
                assigned_ids = set()
                for row in assigned:
                    if row["task_id"] in tasks:
                        tasks[row["task_id"]].update(dict(row))
                        assigned_ids.add(row["task_id"])
                tasks = {task_id: task for task_id, task in tasks.items() if task_id in assigned_ids}
        else:
            for row in materials.effective_capture_tasks(project_id=project_id):
                if row["id"] in tasks:
                    tasks[row["id"]] = row
            if project_id is not None:
                tasks = {task_id: task for task_id, task in tasks.items()
                         if task_project_id(task_id) == project_id}
        return tasks

    def scoped_capture_history(run: dict, result_rows: list[dict], task_index: dict, project_id: str) -> dict | None:
        """按轮次/逐店持久快照裁切历史；未知项目归属不借用当前 UI 选择。"""
        ids = list(run.get("task_ids") or [])
        run_owner = str(run.get("project_id") or "")
        standalone_legacy = role == "standalone" and project_id == LEGACY_ID and not run_owner
        if run_owner == project_id or standalone_legacy:
            selected_ids = ids
            selected_results = []
            for row in result_rows:
                if row.get("project_id") in (None, "", project_id):
                    selected_results.append(row | {"project_id": project_id})
        else:
            selected_results = [row for row in result_rows if row.get("project_id") == project_id]
            selected_ids = [task_id for task_id in ids
                            if any(row.get("task_id") == task_id for row in selected_results)]
        if not selected_ids:
            return None
        by_id = {row.get("task_id"): row for row in selected_results}
        selected = {**run, "task_ids": selected_ids, "project_id": project_id}
        if run_owner != project_id and not standalone_legacy:
            selected["material_batch_id"] = ""
        record = capture_history_row(selected, selected_results, task_index)
        for task in record.get("tasks", []):
            task["project_id"] = project_id
            result = by_id.get(task["task_id"], {})
            task["batch_id"] = result.get("batch_id", "")
        record["project_id"] = project_id
        return record

    @app.get("/api/tasks/history")
    def task_history(kind: str = "all", status: str = "", q: str = "", page: int = 1,
                     page_size: int = 20, worker_id: str = "", project_id: str | None = None):
        """任务中心历史页：采集运行与素材分析任务统一成同一结构，按时间倒序分页。"""
        page = max(1, int(page))
        page_size = min(100, max(5, int(page_size)))
        scope = project_scope(project_id)
        rows: list[dict] = []
        if kind in ("all", "capture"):
            # 单机历史没有多项目边界；保留旧版无 project_id 轮次的读取兼容。
            runs = store.run_history(500, project_id=None if role == "standalone" else scope) if not worker_id else []
            results = store.capture_run_results([run["id"] for run in runs])
            task_index = capture_history_task_index(scope) if runs else {}
            for run in runs:
                item = scoped_capture_history(run, results.get(run["id"], []), task_index, scope)
                if item is None:
                    continue
                item.pop("tasks")
                rows.append(item)
            if hub:
                rows.extend(hub.history_records(worker_id=worker_id, project_id=scope))
        if kind in ("all", "materials") and not worker_id:
            for job in materials.jobs.history(500, project_id=scope):
                rows.append({
                    "kind": "materials",
                    "id": job["id"],
                    "title": _batch_name(job["batch_id"]),
                    "batch_id": job["batch_id"],
                    "batch_name": _batch_name(job["batch_id"]),
                    "mode": job.get("kind", ""),
                    "status": job.get("status", ""),
                    "created_at": job.get("created_at", ""),
                    "updated_at": job.get("updated_at", ""),
                    "message": job.get("message", "") or job.get("error", ""),
                    "counts": {
                        "total": int(job.get("total_stores") or 0),
                        "done": int(job.get("done_stores") or 0),
                        "attention": 0,
                        "failed": int(job.get("failed_stores") or 0),
                    },
                })
        active = ACTIVE_RUN_STATUSES
        aborted = {"terminated", "stopped"}
        errors = {"failed", "cancelled"}
        if status:
            matched = {"running": active, "failed": errors, "terminated": aborted}.get(status, {status})
            rows = [row for row in rows if row["status"] in matched]
        normalized = q.strip().casefold()
        if normalized:
            rows = [row for row in rows
                    if normalized in (" ".join([row["title"], row["batch_name"], row["id"], row.get("worker_name", ""),
                                           *(batch["name"] for batch in row.get("batches", []))])).casefold()]
        rows.sort(key=lambda row: (history_timestamp(row["created_at"]), row["id"]), reverse=True)
        total = len(rows)
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(page, page_count)
        start = (page - 1) * page_size
        return {
            "records": rows[start:start + page_size],
            "summary": {
                "total": total,
                "running": sum(row["status"] in active for row in rows),
                "completed": sum(row["status"] == "completed" for row in rows),
                "failed": sum(row["status"] in errors for row in rows),
                "terminated": sum(row["status"] in aborted for row in rows),
            },
            "page": page,
            "page_size": page_size,
            "page_count": page_count,
        }

    @app.get("/api/tasks/history/{kind}/{item_id}")
    def task_history_detail(kind: str, item_id: str, project_id: str | None = None):
        """历史任务详情：采集运行返回逐店状态，素材分析返回阶段事件。"""
        scope = project_scope(project_id)
        if kind == "capture":
            if hub and ":" in item_id:
                worker_id, run_id = item_id.split(":", 1)
                record = hub.history_detail(worker_id, run_id, project_id=scope)
                if record is not None:
                    return record
                raise HTTPException(404, "采集电脑的轮次尚未同步或不存在")
            run = store.get_run(item_id)
            if run is None:
                raise HTTPException(404, "运行记录不存在")
            results = store.capture_run_results([item_id]).get(item_id, [])
            index = capture_history_task_index(scope)
            record = scoped_capture_history(run, results, index, scope)
            if record is None:
                raise HTTPException(404, "运行记录不属于当前项目或归属待核验")
            # 伴随提取的门店名称按轮次独立保存，补进逐店明细；未观察到的任务为空字符串。
            observed = store.observed_store_names(
                item_id, project_id=None if role == "standalone" else scope)
            for task in record.get("tasks") or []:
                task["observed_store_name"] = observed.get(str(task.get("task_id") or ""), "")
            return record
        if kind == "materials":
            job = materials.job_detail(item_id, events=True, project_id=scope)
            # 与列表行保持同一口径：进度来自 job 行本身，不能返回空 counts 让前端显示 0/0。
            return {"kind": "materials", "id": item_id, "title": _batch_name(job["batch_id"]),
                    "batch_id": job["batch_id"], "batch_name": _batch_name(job["batch_id"]),
                    "mode": job.get("kind", ""), "status": job.get("status", ""),
                    "created_at": job.get("created_at", ""), "updated_at": job.get("updated_at", ""),
                    "message": job.get("message", "") or job.get("error", ""),
                    "counts": {
                        "total": int(job.get("total_stores") or 0),
                        "done": int(job.get("done_stores") or 0),
                        "attention": 0,
                        "failed": int(job.get("failed_stores") or 0),
                    },
                    "events": job.get("events", [])}
        raise HTTPException(404, "任务类型不存在")

    @app.get("/api/tasks/observed-store-name/{task_id}")
    def observed_store_name(task_id: str, project_id: str | None = None):
        """按原始任务编号导出最近一次伴随提取的门店名称，含未采集成功的轮次。"""
        scope = project_scope(project_id)
        ctx.task_scope(task_id, scope)
        if worker:
            with store.connect() as db:
                if worker._hub_supports_project_scope:
                    assigned = db.execute("SELECT run_id FROM worker_assignments WHERE task_id=? AND project_id=? "
                                          "AND worker_id=? ORDER BY rowid DESC LIMIT 1",
                                          (task_id, scope, worker._current_worker_id())).fetchone()
                else:
                    assigned = db.execute("SELECT run_id FROM worker_assignments WHERE task_id=? "
                                          "AND (worker_id='' OR worker_id=?) ORDER BY rowid DESC LIMIT 1",
                                          (task_id, worker._current_worker_id())).fetchone()
            record = store.observed_store_name_record(assigned["run_id"], task_id) if assigned and assigned["run_id"] else {}
            if record and record.get("store_name"):
                return record | {"project_id": scope}
            raise HTTPException(404, "该任务暂无门店名称观察记录")
        elif hub:
            with store.connect() as db:
                valid = db.execute("SELECT 1 FROM capture_run_results WHERE task_id=? AND project_id=? LIMIT 1",
                                   (task_id, scope)).fetchone()
            if not valid:
                raise HTTPException(404, "该任务不属于当前项目或归属待核验")
            name = hub.observed_store_name(task_id, scope)
            if not name:
                raise HTTPException(404, "该任务暂无门店名称观察记录")
            return {"task_id": task_id, "project_id": scope, "store_name": name}
        else:
            record = store.observed_store_name_latest(
                task_id, project_id=None if role == "standalone" else scope)
        if not record:
            raise HTTPException(404, "该任务暂无门店名称观察记录")
        return record

    @app.patch("/api/tasks/history/capture/{run_id}")
    def rename_capture_run(run_id: str, payload: dict, project_id: str | None = None):
        """只改任务展示名称；ID、进度、执行时间、批次和历史结果保持原字段。"""
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValueError("任务名称无效")
        name = name.strip()
        if not 1 <= len(name) <= 64:
            raise ValueError("任务名称需为 1 到 64 个字符")
        if store.get_run(run_id) is None:
            raise HTTPException(404, "运行记录不存在")
        scope = project_scope(project_id, write=True)
        run = store.get_run(run_id)
        results = store.capture_run_results([run_id]).get(run_id, [])
        if scoped_capture_history(run, results, capture_history_task_index(scope), scope) is None:
            raise HTTPException(404, "运行记录不属于当前项目")
        store.rename_run(run_id, name)
        results = store.capture_run_results([run_id]).get(run_id, [])
        record = scoped_capture_history(store.get_run(run_id), results, capture_history_task_index(), scope)
        return record

    @app.get("/api/materials/batches/{batch_id}")
    def material_batch(batch_id: str, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id)
        return materials.get_batch(batch_id, project_id=scope)

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}")
    def material_store(batch_id: str, store_id: str, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id)
        return materials.get_store(batch_id, store_id, project_id=scope)

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/uploads")
    def begin_material_store_upload(batch_id: str, store_id: str, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True)
        with control_lock:
            idle()
            return materials.begin_store_upload(batch_id, store_id, project_id=scope)

    @app.put("/api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}/file")
    async def upload_material_store_file(batch_id: str, store_id: str, upload_id: str, filename: str,
                                        request: Request, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True)
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 50 * 1024 * 1024:
                raise HTTPException(413, "单张图片不能超过 50 MB")
            data.extend(chunk)
        with control_lock:
            idle()
            return materials.add_store_upload_file(batch_id, store_id, upload_id, filename, bytes(data),
                                                   project_id=scope)

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}/commit")
    def commit_material_store_upload(batch_id: str, store_id: str, upload_id: str, payload: dict,
                                    project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True)
        if set(payload) - {"mode", "files"} or "mode" not in payload or "files" not in payload:
            raise ValueError("补图提交参数无效")
        with control_lock:
            idle()
            return materials.commit_store_upload(batch_id, store_id, upload_id, payload["mode"], payload["files"],
                                                 project_id=scope)

    @app.delete("/api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}")
    def abort_material_store_upload(batch_id: str, store_id: str, upload_id: str,
                                    project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True)
        with control_lock:
            idle()
            materials.abort_store_upload(batch_id, store_id, upload_id, project_id=scope)
            return {"ok": True}

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/reanalyze")
    def reanalyze_material_store(batch_id: str, store_id: str, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True)
        with control_lock:
            idle()
            return materials.reanalyze_store(batch_id, store_id, project_id=scope)

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}/images/{image_index}")
    def material_image(batch_id: str, store_id: str, image_index: int, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id)
        result = materials.preview_original(batch_id, store_id, image_index, project_id=scope)
        return Response(result["bytes"], media_type=result["media_type"])

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}/images/{image_index}/candidates/{candidate_index}")
    def material_candidate(batch_id: str, store_id: str, image_index: int, candidate_index: int,
                           project_id: str | None = None):
        scope = batch_scope(batch_id, project_id)
        result = materials.preview_candidate(batch_id, store_id, image_index, candidate_index, project_id=scope)
        return Response(result["bytes"], media_type=result["media_type"])

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}/selected")
    def material_selected_candidate(batch_id: str, store_id: str, project_id: str | None = None):
        """当前门店正在使用的候选二维码；未选码的门店返回 404，不猜测。"""
        scope = batch_scope(batch_id, project_id)
        record = materials.get_store(batch_id, store_id, project_id=scope)
        selected = record.get("selected") or {}
        if selected.get("image_index") is None or selected.get("candidate_index") is None:
            raise HTTPException(404, "该门店尚未选择二维码")
        result = materials.preview_candidate(batch_id, store_id, int(selected["image_index"]),
                                             int(selected["candidate_index"]), project_id=scope)
        return Response(result["bytes"], media_type=result["media_type"],
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/decision")
    def decide_material_store(batch_id: str, store_id: str, payload: dict, project_id: str | None = None):
        allowed = {"decision", "image_index", "candidate_index", "box", "reject_reason", "reject_detail"}
        if set(payload) - allowed or "decision" not in payload:
            raise ValueError("确认参数无效")
        if payload["decision"] == "skip" and not payload.get("reject_reason"):
            raise ValueError("请选择不合格原因")
        with control_lock:
            scope = batch_scope(batch_id, project_id, write=True)
            try:
                return materials.decide_store(batch_id, store_id, **payload, project_id=scope)
            except Exception as exc:
                box = payload.get("box") if isinstance(payload.get("box"), dict) else {}
                logger.warning(
                    "decision failed batch=%s store=%s decision=%s image=%s candidate=%s "
                    "box=%s error=%r",
                    batch_id, store_id, payload.get("decision"), payload.get("image_index"),
                    payload.get("candidate_index"),
                    {key: box.get(key) for key in ("x", "y", "width", "height")},
                    exc,
                )
                raise

    @app.post("/api/materials/batches/{batch_id}/accept-recommendations")
    def accept_material_recommendations(batch_id: str, project_id: str | None = None):
        with control_lock:
            scope = batch_scope(batch_id, project_id, write=True)
            return materials.accept_recommendations(batch_id, project_id=scope)

    @app.post("/api/materials/batches/{batch_id}/publish")
    def publish_materials(batch_id: str, payload: dict | None = None, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True)
        with control_lock:
            fields = payload or {}
            materials.versions.validate(fields.get("name"), fields.get("version"), fields.get("description", ""))
            run = store.latest_run()
            can_activate = not runner.running and not (run and run["status"] in {"paused", "blocked", "interrupted"})
            result = materials.publish_snapshot(
                batch_id, fields.get("description", ""), name=fields["name"], version=fields["version"],
                activate=can_activate, project_id=scope,
            )
            if can_activate:
                runner.invalidate_readiness()
            if hub:
                hub.reconcile_assignments()
            return result

    @app.get("/api/materials/rework")
    def material_rework(batch_ids: str = "all", q: str = "", project_id: str | None = None):
        """跨批次返工清单：待处理 / 不合格 / 已选码待发布。"""
        scope = project_scope(project_id)
        wanted = None if batch_ids in {"", "all"} else [value for value in batch_ids.split(",") if value]
        records = materials.rework_records(wanted, project_id=scope)
        needle = q.strip().casefold()
        if needle:
            records = [row for row in records
                       if needle in (row["store_name"] + " " + row["batch_name"]).casefold()]
        return {
            "records": records,
            "summary": {
                "stores": len(records),
                "pending": sum(row["category"] == "pending" for row in records),
                "rejected": sum(row["category"] == "rejected" for row in records),
                "unpublished": sum(row["category"] == "unpublished" for row in records),
                "batches": len({row["batch_id"] for row in records}),
            },
        }

    @app.post("/api/materials/rework/return")
    def return_material_rework(payload: dict, project_id: str | None = None):
        """打回选错码的门店：恢复待处理，等待人工在原批次重新选码。"""
        if set(payload) != {"batch_id", "store_id"}:
            raise ValueError("打回参数无效")
        scope = batch_scope(str(payload["batch_id"]), project_id, write=True)
        with control_lock:
            idle()
            result = materials.return_rework_store(str(payload["batch_id"]), str(payload["store_id"]),
                                                   project_id=scope)
            if hub:
                hub.reconcile_assignments()
            return result

    @app.post("/api/materials/rework/publish")
    def publish_material_rework(payload: dict, project_id: str | None = None):
        """统一发布已处理门店：按原批次分组，各自在最新快照上增量更新。"""
        payload = payload or {}
        if set(payload) - {"store_ids", "description"}:
            raise ValueError("发布参数无效")
        scope = project_scope(project_id, write=True)
        store_ids = payload.get("store_ids")
        if not isinstance(store_ids, list) or not store_ids:
            raise ValueError("请勾选要发布的门店")
        groups: dict[str, list[str]] = {}
        for value in store_ids:
            if not isinstance(value, str) or ":" not in value:
                raise ValueError("门店发布标识无效")
            batch_id, _, store_id = value.partition(":")
            if not batch_id or not store_id:
                raise ValueError("门店发布标识无效")
            batch_scope(batch_id, scope, write=True)
            groups.setdefault(batch_id, []).append(store_id)
        names = {batch["batch_id"]: batch.get("name", "") for batch in materials.list_batches(project_id=scope)}
        description = str(payload.get("description") or "跨批次返工发布")
        results, errors = [], []
        with control_lock:
            idle()
            for batch_id, ids in groups.items():
                try:
                    result = materials.publish_snapshot(batch_id, description, store_ids=ids, activate=False,
                                                        project_id=scope)
                    if hub:
                        hub.reconcile_assignments()
                    results.append({
                        "batch_id": batch_id, "batch_name": names.get(batch_id, ""),
                        "snapshot_id": result["snapshot_id"],
                        "version": result["release"]["version"],
                        "already_published": result["already_published"],
                        "task_scan": result["task_scan"],
                        "store_updates": result["store_updates"],
                    })
                except Exception as exc:
                    errors.append({"batch_id": batch_id, "batch_name": names.get(batch_id, ""),
                                   "error": str(exc)})
        return {"results": results, "errors": errors}

    @app.post("/api/materials/batches/{batch_id}/activate")
    def activate_material_version(batch_id: str, payload: dict, project_id: str | None = None):
        scope = batch_scope(batch_id, project_id, write=True)
        with control_lock:
            idle()
            run = store.latest_run()
            if run and run["status"] in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "请先继续或终止当前采集，再切换素材版本")
            result = materials.activate_version(batch_id, payload.get("version", ""), project_id=scope)
            runner.invalidate_readiness()
            return result

    @app.post("/api/scan")
    def scan(project_id: str | None = None):
        with control_lock:
            idle()
            scope = project_scope(project_id, write=True)
            config = configs.load()
            if not config.input_dir:
                raise HTTPException(400, "请先选择用于采集的发布版本")
            selected_release = materials.active_release(Path(config.input_dir).expanduser().resolve(), project_id=scope)
            if selected_release is None or Path(config.input_dir).name != selected_release["snapshot_id"]:
                raise HTTPException(409, "当前素材目录不属于所选项目的已发布版本")
            if ((Path(config.input_dir) / ".materials-snapshot.json").is_file()
                    and store.material_binding(config.input_dir) is None):
                raise HTTPException(400, "素材版本已移入回收站或尚未发布，请重新选择采集版本")
            try:
                result = store.scan(Path(config.input_dir))
                runner.invalidate_readiness()
            except OSError:
                raise HTTPException(400, "素材目录不存在或无法读取") from None
            environment.invalidate("uipath_desktop")
            connections.invalidate()
            store.event(f"扫描完成：新增 {result['added']} 个素材任务")
            root_dir = Path(config.input_dir).expanduser().resolve()
            current_total = sum(root_dir in Path(task["source_path"]).parents for task in store.tasks())
            return {**result, "current_total": current_total,
                    "material_release": materials.active_release(root_dir, project_id=scope)}
 
    @app.post("/api/start")
    def start(payload: dict | None = None):
        with control_lock:
            idle()
            payload = payload or {}
            if set(payload) - {"project_id"}:
                raise ValueError("开跑参数无效")
            scope = project_scope(payload.get("project_id"), write=True)
            # 接口层先做运行前检查，避免环境未就绪时返回 200 后才在后台失败。
            preflight = environment.snapshot().get("preflight") or {}
            if preflight and not preflight.get("ready", True):
                blockers = "；".join(preflight.get("blockers") or []) or "运行环境未就绪"
                raise HTTPException(409, blockers)
            run = store.latest_run()
            if run and run["status"] in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "请先继续或终止当前批次")
            runner.start(project_id=scope)
            return status(project_id=scope)

    @app.post("/api/resume")
    def resume(payload: dict | None = None):
        with control_lock:
            idle()
            payload = payload or {}
            if set(payload) - {"mode", "project_id"}:
                raise ValueError("恢复参数无效")
            scope = project_scope(payload.get("project_id"), write=True)
            run = store.latest_run()
            if not run or run["status"] not in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "当前没有可恢复的暂停或阻塞批次")
            if not run_project_matches(run, scope):
                raise HTTPException(404, "该采集轮次不属于当前项目或归属待核验")
            mode = payload.get("mode", "")
            if mode and (mode != "manual" or sys.platform != "win32"
                            or run.get("capture_mode") != "auto" or run["status"] != "paused"):
                raise ValueError("只有已暂停的 Windows 全自动批次可以切换半自动")
            profile = profiles.load()
            # 批量采集按本轮锁定的模式恢复，不再受当前机器人连接方式影响。
            if not run.get("capture_mode") and profile.get("mode") != run["mode"]:
                raise HTTPException(409, "采集方式与原批次不一致；请恢复原采集方式后继续")
            remaining = run["task_ids"][run["current_index"]:]
            if not remaining:
                raise HTTPException(409, "此批次没有尚未完成的门店")
            try:
                if worker:
                    worker.check_start(remaining, project_id=scope)
                if mode:
                    runner.start(resume_run=run, capture_mode=mode, project_id=scope)
                else:
                    runner.start(resume_run=run, project_id=scope)
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from None
            return status(project_id=scope)

    @app.post("/api/ui-tars/trial")
    def start_ui_tars_trial(payload: dict | None = None):
        payload = payload or {}
        scope = project_scope(payload.get("project_id"), write=True)
        task_id = payload.get("task_id")
        task_ids = payload.get("task_ids")
        if set(payload) <= {"task_ids", "project_id"} and task_ids is not None:
            if not isinstance(task_ids, list) or not task_ids or any(
                not isinstance(value, str) or not value.strip() for value in task_ids
            ):
                raise ValueError("全自动试验需要有效的门店任务列表")
            with control_lock:
                idle()
                selected = [value.strip() for value in task_ids]
                for selected_id in selected:
                    task_scope(selected_id, scope, write=True)
                return runner.start(ui_tars_trial=True, trial_task_ids=selected, project_id=scope)
        if set(payload) - {"task_id", "project_id"} or (task_id is not None and (not isinstance(task_id, str) or not task_id.strip())):
            raise ValueError("试验需要指定已有的门店任务")
        with control_lock:
            idle()
            if task_id:
                task_scope(task_id.strip(), scope, write=True)
                return runner.start(ui_tars_trial=True, trial_task_id=task_id.strip(), project_id=scope)
            return runner.start(ui_tars_trial=True, project_id=scope)

    @app.post("/api/capture/start")
    def start_capture(payload: dict | None = None):
        """统一开跑入口：本轮模式与任务清单在这里锁定，两种模式共用同一批门店。"""
        payload = payload or {}
        if set(payload) - {"mode", "task_ids", "name", "project_id"}:
            raise ValueError("开跑参数无效")
        scope = project_scope(payload.get("project_id"), write=True)
        mode = payload.get("mode")
        if mode not in {"auto", "manual"}:
            raise ValueError("请选择全自动 Agent 或半自动人工")
        name = payload.get("name", "")
        if not isinstance(name, str) or len(name.strip()) > 80:
            raise ValueError("任务名称无效")
        task_ids = payload.get("task_ids")
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= REVIEW_RERUN_LIMIT:
            raise ValueError(f"请选择 1 到 {REVIEW_RERUN_LIMIT} 家门店")
        if any(not isinstance(value, str) or not value.strip() for value in task_ids):
            raise ValueError("门店任务编号无效")
        if mode == "manual" and sys.platform != "win32":
            raise HTTPException(501, "半自动人工首版仅支持 Windows")
        unique_ids = list(dict.fromkeys(value.strip() for value in task_ids))
        with control_lock:
            idle()
            run = store.latest_run()
            if run and run["status"] in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "请先继续或终止当前批次")
            try:
                for task_id in unique_ids:
                    task_scope(task_id, scope, write=True)
                if worker:
                    worker.check_start(unique_ids, project_id=scope)
                result = runner.start(ui_tars_trial=True, trial_task_ids=unique_ids,
                                      cross_batch=True, capture_mode=mode, run_name=name.strip(),
                                      project_id=scope)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(400, str(exc)) from None
            if worker:
                return {**result, "status": status(project_id=scope)}
            try:
                # 采集轮次与审核失效跟随实际开跑，失败不影响已启动的批次。
                materials.register_capture_round(unique_ids, project_id=scope)
            except Exception:
                logger.exception("记录采集轮次失败")
            return {**result, "status": status(project_id=scope)}

    def manual_device() -> tuple[str, str]:
        local_name = socket.gethostname()
        connection = worker.connection if worker else {}
        return connection.get("id") or local_name, connection.get("name") or local_name

    def _manual_action(request: Request, kind: str, payload: dict) -> dict:
        scope = project_scope(payload.get("project_id"), write=True)
        expected = payload.get("context")
        manual = runner.manual_state()
        if request.state.remote_control or expected is not None:
            keys = {"device_id", "run_id", "task_id", "wait_id"}
            if (not isinstance(expected, dict) or set(expected) != keys
                    or any(not isinstance(expected[key], str) or not expected[key] for key in keys)
                    or expected["device_id"] != manual_device()[0]):
                raise HTTPException(409, "采集端或门店上下文已失效，请重读当前状态")

        # 旧版 Hub 不下发项目快照，半自动会话也没有可关联的本地 runs 行。
        # 仅在旧协议且客户端回传的完整上下文仍与当前等待状态一致时，按单工作区兼容。
        legacy_manual_session = False
        if role == "worker" and worker and not worker._hub_supports_project_scope and manual:
            if (not isinstance(expected, dict)
                    or any(expected[key] != manual.get(key) for key in ("run_id", "task_id", "wait_id"))):
                raise HTTPException(409, "采集端或门店上下文已失效，请重读当前状态")
            legacy_manual_session = True
        if not legacy_manual_session:
            require_run_scope(scope)
        current_task = runner.current_task or {}
        if current_task.get("id") and not legacy_manual_session:
            task_scope(str(current_task["id"]), scope, write=True)
        try:
            result = runner.manual_action(kind, str(payload.get("reason") or ""),
                                          str(payload.get("detail") or ""), expected=expected)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if request.state.remote_control:
            return result | {"manual": runner.manual_state()}
        return result | {"manual": runner.manual_state(), "status": status(project_id=scope)}

    @app.get("/api/manual/state")
    def manual_state(project_id: str | None = None):
        scope = project_scope(project_id)
        device_id, device_name = manual_device()
        run = store.latest_run()
        visible = run if run_project_matches(run, scope) else None
        manual = runner.manual_state()
        if role == "standalone" and not visible and manual:
            # 独立模式旧半自动会话可能没有持久轮次快照，只有本机一个工作区可读。
            visible = {"name": "", "status": ""}
        if role == "worker" and worker and not worker._hub_supports_project_scope and not visible and manual:
            # 旧 Hub 的单工作区协议没有项目快照，旧半自动会话继续按 legacy 本地读取。
            visible = {"name": "", "status": ""}
        return {"device_id": device_id, "device_name": device_name,
                "manual": manual if visible else {},
                "run_name": (visible.get("name") or "半自动采集") if visible else "",
                "run_status": visible["status"] if visible else "",
                "remote_url": f"https://{remote_control_host}/remote-control" if remote_control_host else ""}

    @app.post("/api/manual/capture")
    def manual_capture(request: Request, payload: dict | None = None):
        payload = payload or {}
        if set(payload) - {"context", "project_id"}:
            raise ValueError("截图参数无效")
        with control_lock:
            return _manual_action(request, "capture", payload)

    @app.post("/api/manual/reject")
    def manual_reject(request: Request, payload: dict | None = None):
        payload = payload or {}
        if set(payload) - {"reason", "detail", "context", "project_id"}:
            raise ValueError("不合格参数无效")
        with control_lock:
            return _manual_action(request, "reject", payload)

    @app.post("/api/manual/skip")
    def manual_skip(request: Request, payload: dict | None = None):
        payload = payload or {}
        if set(payload) - {"context", "project_id"}:
            raise ValueError("跳过参数无效")
        with control_lock:
            return _manual_action(request, "skip", payload)

    @app.post("/api/manual/reason")
    def manual_reason(payload: dict):
        if set(payload) - {"reason", "project_id"} or "reason" not in payload:
            raise ValueError("不合格原因参数无效")
        scope = project_scope(payload.get("project_id"), write=True)
        require_run_scope(scope)
        return {"ok": True, "reason": runner.set_manual_reason(str(payload["reason"]))}

    @app.get("/api/robot/profile")
    def robot_profile():
        return profiles.load()

    @app.post("/api/robot/profile")
    def save_robot_profile(payload: dict):
        with control_lock:
            idle()
            previous = profiles.load()
            result = profiles.save(payload)
            runner.invalidate_readiness()
            # 任务勾选变化不影响桌面权限探测；只有窗口目标/模式改变才让实测结果过期。
            relevant = ("mode", "wechat_selector", "miniapp_selector")
            if any(previous.get(key) != result.get(key) for key in relevant):
                environment.invalidate("uipath_desktop")
                connections.invalidate()
            return result

    @app.post("/api/stop")
    def stop(payload: dict | None = None):
        with control_lock:
            payload = payload or {}
            if set(payload) - {"project_id"}:
                raise ValueError("暂停参数无效")
            scope = project_scope(payload.get("project_id"), write=True)
            run = require_run_scope(scope)
            if run and run["mode"] == "attached":
                raise HTTPException(409, "单店采集可以终止后重试")
            runner.pause()
            return status(project_id=scope)

    @app.post("/api/terminate")
    def terminate(payload: dict | None = None):
        with control_lock:
            payload = payload or {}
            if set(payload) - {"project_id"}:
                raise ValueError("终止参数无效")
            scope = project_scope(payload.get("project_id"), write=True)
            require_run_scope(scope)
            runner.terminate()
            return status(project_id=scope)

    @app.get("/api/tasks/{task_id}/source")
    def task_source_image(task_id: str, project_id: str | None = None):
        """任务当前使用的二维码裁剪图；不合格详情用它展示“当前使用的候选码”。"""
        task_scope(task_id, project_id)
        task = store.get(task_id)
        path = Path(task["source_path"]).expanduser()
        if not path.is_file():
            raise HTTPException(404, "素材文件不存在")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.get("/api/tasks/{task_id}/last-frame")
    def task_last_frame(task_id: str, project_id: str | None = None):
        """待检查门店的“最后一帧”：取运行期目录里该任务最新的一张画面。

        待检查任务没有发布截图，动作记录也可能因调试日志轮转丢失证据字段；
        运行目录（.uipath-runs / .uipath-staging）仍保留当时的画面，用它兜底。
        """
        if not re.fullmatch(r"[0-9a-f]{20}", task_id):
            raise HTTPException(404, "任务编号无效")
        task_scope(task_id, project_id)
        task = store.get(task_id)
        runtime = Path(configs.load().output_dir).expanduser().resolve().parent
        candidates: list[Path] = []
        runs = runtime / ".uipath-runs"
        if runs.is_dir():
            candidates.extend(runs.glob(f"*/ui-tars-trial/{task_id}/**/*.png"))
            candidates.extend(runs.glob(f"*/{task_id}/**/*.png"))
        staging = runtime / ".uipath-staging"
        if staging.is_dir():
            candidates.extend(staging.glob(f"{task_id}/**/*.png"))
        output = Path(task.get("output_dir") or "")
        if task.get("output_dir") and output.is_dir():
            candidates.extend(output.glob("*.png"))
        files = [path for path in candidates if path.is_file()]
        if not files:
            raise HTTPException(404, "没有可用截图")
        # 只取最近一次尝试的画面：旧批次的成功页和本轮收尾截图都不能冒充当前失败画面。
        def attempt(path: Path) -> Path:
            parts = path.parts
            if "ui-tars-trial" in parts:
                return Path(*parts[:parts.index("ui-tars-trial") + 1])
            for marker in (".uipath-runs", ".uipath-staging"):
                if marker in parts:
                    return Path(*parts[:parts.index(marker) + 2])
            return path.parent

        groups: dict[Path, list[Path]] = {}
        for path in files:
            groups.setdefault(attempt(path), []).append(path)
        latest = max(groups, key=lambda key: max(path.stat().st_mtime for path in groups[key]))
        scoped = groups[latest]
        cleanup = re.compile(r"(after-close|^wechat-|^desktop|zzz-failure)", re.I)
        usable = [path for path in scoped if not cleanup.search(path.name)] or scoped
        newest = max(usable, key=lambda path: path.stat().st_mtime)
        return FileResponse(newest, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/tasks/{task_id}/actions")
    def task_actions(task_id: str, before: int | None = None, project_id: str | None = None):
        task_scope(task_id, project_id)
        actions = store.actions(task_id, before)
        for row in actions:
            compact = row.get("details") or {}
            if not compact.get("debug_enabled"):
                continue
            record = debug_log.action_record(row["id"])
            if record:
                row["details"] = record.get("details", compact) | {"debug_state": "ready"}
                row["debug_state"] = "ready"
            else:
                row["debug_state"] = debug_log.action_state(row["id"])
                row["details"] = compact | {"debug_state": row["debug_state"]}
        return {"actions": actions}

    @app.get("/api/debug/summary")
    def debug_summary():
        limit = configs.load().debug_logging.max_storage_mb
        return debug_log.summary(limit)

    @app.get("/api/debug/runs/{run_id}/export")
    def export_debug_run(run_id: str, project_id: str | None = None):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise HTTPException(400, "运行编号无效")
        scope = project_scope(project_id)
        if not run_project_matches(store.get_run(run_id), scope):
            raise HTTPException(404, "采集轮次不属于当前项目或归属待核验")
        handle = tempfile.NamedTemporaryFile(prefix="menu-capture-debug-", suffix=".zip", delete=False)
        path = Path(handle.name)
        handle.close()
        try:
            debug_log.export_run(run_id, path)
        except Exception:
            path.unlink(missing_ok=True)
            raise HTTPException(500, "调试包导出失败") from None
        return FileResponse(
            path, media_type="application/zip", filename=f"menu-capture-{run_id[:8]}-debug.zip",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @app.post("/api/debug/clear")
    def clear_debug_logs():
        with control_lock:
            idle()
            return debug_log.clear()

    @app.post("/api/materials/batches/{batch_id}/stage")
    def set_material_batch_stage(batch_id: str, payload: dict, project_id: str | None = None):
        with control_lock:
            scope = batch_scope(batch_id, payload.get("project_id", project_id), write=True)
            if set(payload) - {"stage", "project_id"}:
                raise ValueError("批次阶段参数无效")
            return materials.set_stage(batch_id, payload.get("stage", ""), project_id=scope)

    @app.post("/api/materials/batches/{batch_id}/approve")
    def approve_material_batch(batch_id: str, payload: dict | None = None, project_id: str | None = None):
        with control_lock:
            payload = payload or {}
            if set(payload) - {"project_id"}:
                raise ValueError("审核参数无效")
            scope = batch_scope(batch_id, payload.get("project_id", project_id), write=True)
            if hub:
                hub.require_unassigned(batch_id)
            return materials.set_approved(batch_id, True, project_id=scope)

    @app.post("/api/materials/batches/{batch_id}/approve/undo")
    def unapprove_material_batch(batch_id: str, payload: dict | None = None, project_id: str | None = None):
        with control_lock:
            payload = payload or {}
            if set(payload) - {"project_id"}:
                raise ValueError("撤销审核参数无效")
            scope = batch_scope(batch_id, payload.get("project_id", project_id), write=True)
            return materials.set_approved(batch_id, False, project_id=scope)

    @app.get("/api/action-evidence/{name}")
    def action_evidence(name: str, project_id: str | None = None):
        if not re.fullmatch(r"[0-9a-f]{32}\.png", name):
            raise HTTPException(404)
        scope = project_scope(project_id)
        with store.connect() as db:
            rows = db.execute("SELECT DISTINCT task_id FROM run_actions WHERE details LIKE ?",
                              ("%" + name + "%",)).fetchall()
        if not any(task_project_id(str(row["task_id"])) == scope for row in rows):
            raise HTTPException(404)
        path = store.path.parent / "action-evidence" / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.post("/api/tasks/{task_id}/retry")
    def retry(task_id: str, payload: dict | None = None):
        with control_lock:
            idle()
            payload = payload or {}
            if set(payload) - {"project_id"}:
                raise ValueError("重试参数无效")
            task_scope(task_id, payload.get("project_id"), write=True)
            if hub:
                hub.protect_task(task_id)
            task = store.get(task_id)
            if waiting_for_materials(task):
                raise HTTPException(409, "该门店待重新选码并发布")
            if task["status"] not in {"needs_review", "needs_retry", "error", "blocked", "captured"}:
                raise ValueError("此任务还没有可重试或重新采集的结果")
            # 重试开始不等于问题已经解决；不合格记录保留到新一轮成功采集。
            store.update(task_id, status="pending", stage="queued", message="已重新排队；旧输出保留")
            return {"ok": True}

    @app.post("/api/tasks/{task_id}/mark-review")
    def mark_task_review(task_id: str, payload: dict | None = None):
        """人工把已有采集结果的门店改为待检查；不合格记录必须先撤销，避免状态自相矛盾。"""
        with control_lock:
            idle()
            payload = payload or {}
            if set(payload) - {"project_id"}:
                raise ValueError("待检查参数无效")
            task_scope(task_id, payload.get("project_id"), write=True)
            if hub:
                hub.protect_task(task_id)
            task = store.get(task_id)
            if task.get("reject_reason"):
                raise ValueError("请先撤销不合格标记，再改为待检查")
            if task["status"] == "needs_review":
                return {"ok": True, "task": task}
            if task["status"] not in {"captured", "needs_retry", "error", "blocked"}:
                raise ValueError("该门店还没有采集结果，不能改为待检查")
            store.update(task_id, status="needs_review")
            return {"ok": True, "task": store.get(task_id)}

    @app.post("/api/reveal")
    def reveal(payload: dict):
        if set(payload) - {"path", "project_id"}:
            raise ValueError("打开目录参数无效")
        config = configs.load()
        if payload.get("path") not in {"input", "output"}:
            raise ValueError("只能打开素材目录或结果目录")
        scope = project_scope(payload.get("project_id"))
        selected = config.input_dir if payload["path"] == "input" else config.output_dir
        if not selected.strip():
            raise ValueError("请先选择采集素材版本" if payload["path"] == "input" else "请先设置输出目录")
        if payload["path"] == "output" and worker:
            project = next((item for item in worker.projects() if item["id"] == scope), None)
            if project is None:
                raise HTTPException(404, "项目不存在或该采集端没有分配记录")
            path = batch_output_dir(selected, "0" * 32, project_code=project["code"]).parent
        else:
            release = materials.active_release(Path(config.input_dir).resolve(), project_id=scope) \
                if config.input_dir else None
            path = (batch_output_dir(selected, release["batch_id"], release.get("batch_name", ""))
                    if payload["path"] == "output" and release and role == "standalone" else Path(selected))
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.run(["/usr/bin/open", str(path)], check=True)
        return {"ok": True}

    @app.get("/api/artifacts/{task_id}")
    def artifacts(task_id: str, project_id: str | None = None):
        with runner.output_lock:
            task_scope(task_id, project_id)
            task = store.get(task_id)
            if not task["output_dir"]:
                return {"files": []}
            folder = Path(task["output_dir"]).resolve()
            return {"files": [str(p.relative_to(folder)) for p in sorted(folder.rglob("*"))
                              if p.is_file() and p.suffix.lower() in {".png", ".json"}]}

    @app.get("/api/source/{task_id}")
    def source_image(task_id: str, project_id: str | None = None):
        task_scope(task_id, project_id)
        task = store.get(task_id)
        source = Path(task["source_path"]).resolve()
        if worker and worker.source_root(task_id) and source.is_file():
            return FileResponse(source)
        input_dir = configs.load().input_dir
        if not input_dir.strip():
            raise HTTPException(404, "尚未选择采集素材版本")
        directory = Path(input_dir).resolve()
        if directory not in source.parents or not source.is_file() or source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(404, "素材不在当前输入目录中")
        return FileResponse(source)

    @app.get("/api/artifact/{task_id}/{relative_path:path}")
    def artifact(task_id: str, relative_path: str, project_id: str | None = None):
        with runner.output_lock:
            task_scope(task_id, project_id)
            task = store.get(task_id)
            if not task["output_dir"]:
                raise HTTPException(404, "此任务还没有输出")
            directory = Path(task["output_dir"]).resolve()
            path = (directory / relative_path).resolve()
            if directory not in path.parents or not path.is_file() or path.suffix.lower() not in {".png", ".json"}:
                raise HTTPException(404, "输出文件不存在")
            # 在同一锁内读取，避免流式响应延迟打开时目录已被另一个素材版本替换。
            return Response(path.read_bytes(), media_type="image/png" if path.suffix.lower() == ".png" else "application/json")

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
