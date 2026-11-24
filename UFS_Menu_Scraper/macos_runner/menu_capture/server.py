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
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image
from starlette.background import BackgroundTask

from .config import ConfigStore, ROOT
from .capture_outputs import batch_output_dir
from .failures import describe as describe_failure
from .capture_history import ACTIVE_RUN_STATUSES, capture_history_row, history_timestamp
from .capture_results import capture_outcome, state_counts, store_state, waiting_for_materials
from .store import TaskStore
from .uipath_runner import UiPathRunner
from .robot_profile import ProfileStore
from .image_tools import image_ocr, qr_boxes, prepare_clipboard_image, prepare_clipboard_text
from .materials import MaterialService
from .rejections import RejectionService
from .environment import EnvironmentDiagnostics
from .connections import ConnectionManager
from .debug_log import DebugLog
from .model_usage import UsageLedger
from .annotation import AnnotationSession, RunnerExecutor
from .instance_lock import InstanceLockedError, instance_lock
from .shared import SharedConflict, SharedHub, WorkerUnauthorized
from .shared_routes import register_shared_routes
from .shared_worker import SharedWorker
from .workbench_page import workbench_page

logger = logging.getLogger(__name__)

# 跨批次待检查重跑的上限：仅用于一次人工批量操作，超过这个量级应拆成多轮。
REVIEW_RERUN_LIMIT = 300


def _rejection_store_code(text: str) -> str:
    """提取门店编号（如 E02412），用于素材不合格与采集不合格按门店去重。"""
    name = Path(text or "").name
    match = re.match(r"^([A-Za-z]{1,4}\d+)", name)
    return (match.group(1) if match else name).upper()


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
    store = TaskStore(root / "data/tasks.sqlite3")
    model_usage = UsageLedger(store)
    debug_log = DebugLog(root / "data", configs.load().debug_logging.max_storage_mb)
    profile_name = "uipath.windows.profile.json" if sys.platform == "win32" else "uipath.profile.json"
    profiles = ProfileStore(root / profile_name)
    if profiles.path.exists():
        profiles.load()  # 启动时完成旧预览定位字段的单次清理。
    project_dir = ROOT.parent.parent / "UFS_Menu_Capture_CrossPlatform"
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
                                    root=root if role != "standalone" else root.parent.parent, ocr_reader=image_ocr,
                                    rejector=rejections.mark_annotation_task,
                                    usage_ledger=model_usage)
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

    def review_queue(batch_ids: str, query: str) -> dict:
        """待检查清单：跨批次读取任务状态，同一门店只保留最新一条。"""
        # 与批次管理、工作台保持同一口径：只取未删除批次的最新发布快照任务，
        # 旧版本遗留的失败任务不进入清单，避免同一门店重复计数或被重跑旧素材。
        current_tasks = {row["id"]: row for row in materials.effective_capture_tasks()
                         if capture_outcome(row) == "review"}
        candidates = current_tasks.values()
        # 待检查入口只展示批次名和阶段。版本差异、素材计数留给批次管理页。
        with store.connect() as db:
            batches = [dict(row) for row in db.execute(
                "SELECT id,name,capture_rounds,manual_stage,approved_at FROM material_batches WHERE deleted_at=''"
            )]
        workflow = {batch["id"]: materials.workflow_stage(batch) for batch in batches}
        names = {batch["id"]: batch["name"] or batch["id"] for batch in batches}
        selected = set()
        if batch_ids and batch_ids != "all":
            selected = {value.strip() for value in batch_ids.split(",") if value.strip()}
        normalized = query.strip().casefold()
        records = []
        for task in candidates:
            batch_id = current_tasks[task["id"]]["batch_id"]
            if selected and batch_id not in selected:
                continue
            if normalized and normalized not in (task.get("source_name") or "").casefold():
                continue
            failure = describe_failure(task.get("error_code", ""), task.get("stage", ""),
                                       task.get("message", ""))
            records.append({
                "task_id": task["id"],
                "store_name": task["source_name"],
                "status": task["status"],
                "stage": task["stage"],
                "message": task["message"],
                "error_code": task.get("error_code", ""),
                "group": failure["group"],
                "label": failure["label"],
                "bucket": failure["bucket"],
                "bucket_label": failure["bucket_label"],
                "source_path": task["source_path"],
                "output_dir": task["output_dir"],
                "reject_reason": task.get("reject_reason", ""),
                "reject_detail": task.get("reject_detail", ""),
                "updated_at": task.get("updated_at", ""),
                "batch_id": batch_id,
                "batch_name": task.get("material_batch_name") or names.get(batch_id, ""),
                "workflow": workflow.get(batch_id, {}),
            })
        # 有效任务清单已按原批次和门店归属取最新版，保留显示编号相近的各条任务。
        records.sort(key=lambda row: (row["updated_at"], row["store_name"]), reverse=True)
        options = {}
        for record in records:
            if record["batch_id"]:
                options[record["batch_id"]] = {
                    "id": record["batch_id"],
                    "name": record["batch_name"] or record["batch_id"],
                    "workflow": record["workflow"],
                }
        return {
            "records": records,
            "batches": sorted(options.values(), key=lambda item: (item["name"], item["id"])),
            "summary": {
                "stores": len(records),
                "batches": len(options),
                "needs_review": sum(row["status"] == "needs_review" for row in records),
                # 清单里的门店都已落定，可以直接批量重跑；单独保留该字段方便前端展示。
                "retryable": len(records),
            },
        }


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
        return {"version": os.environ.get("MENU_CAPTURE_VERSION", ""), "frontend": frontend}

    @app.get("/api/model-usage")
    def model_usage_report(date_from: str | None = None, date_to: str | None = None,
                           group_by: str = "day", source: str = "all", run_id: str = "",
                           task_id: str = "", provider: str = "", model: str = "",
                           page: int = 1, page_size: int = 30):
        # 统计查询只读本地计量表，绝不触发模型、环境检测或微信操作。
        return model_usage.query(date_from=date_from, date_to=date_to, group_by=group_by,
                                 source=source, run_id=run_id, task_id=task_id,
                                 provider=provider, model=model, page=page, page_size=page_size)

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
               queue_filter: Literal["all", "pending", "running", "captured", "rejected", "review"] = "all",
               queue_sort: Literal["source", "recent", "status"] = "source",
               selected: Annotated[str, Query(max_length=9000)] = "",
               include_events: bool = True):
        # 任务表保留历史；更换成清洗后的目录时，只展示当前素材集，避免旧照片继续可选。
        settings = configs.load()
        input_dir = settings.input_dir
        current_root = Path(input_dir).expanduser().resolve() if input_dir else None
        release = materials.active_release(current_root) if current_root else None
        paged = hub is not None and queue_page is not None
        all_tasks = [] if paged else store.tasks()
        if hub:
            # 共享模式按各批次最新发布版本取数，不需要逐条解析旧任务的文件路径。
            tasks = materials.effective_capture_tasks()
            ready = {"ready": True, "issues": [], "launch_mode": "shared"}
        else:
            tasks = worker.tasks() if worker else [
                task for task in all_tasks if current_root and current_root in Path(task["source_path"]).resolve().parents
            ]
            ready = runner.last_readiness or {"ready": None, "issues": ["环境诊断可在设置中手动查看"],
                                              "launch_mode": profiles.load()["launch_mode"]}
        if sys.platform == "win32" and not hub:
            ready = ready | {"launch_mode": "native"}
        run = None if paged else store.latest_run()
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
                "JOIN material_batches b ON b.id=d.batch_id WHERE d.decision='skipped' AND b.deleted_at=''"
            ).fetchall()
        capture_codes = {_rejection_store_code(task.get("source_name")) for task in store.rejections()}
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
        return {"execution_engine": engine, "launch_mode": launch_mode,
                "engine_ready": ready["ready"], "engine_message": "；".join(ready["issues"]) or engine_message,
                "robot_readiness": ready, "run_directory": str(getattr(runner, "run_directory", "") or ""),
                "running": runner.running, "stop_requested": runner.stop.is_set(),
                "run": run, "current_task": runner.current_task,
                "material_release": release,
                "batch_output_dir": (str(batch_output_dir(settings.output_dir,
                                                          release["batch_id"] if release else "",
                                                          release.get("batch_name", "") if release else ""))
                                     if settings.output_dir else ""),
                "input_name": "本机分配" if worker else (current_root.name if current_root else ""),
                "tasks": queue["records"] if queue else [task | describe_failure(task.get("error_code", ""), task.get("stage", ""), task.get("message", "")) | {"store_state": store_state(task)} for task in tasks],
                "counts": queue["counts"] if queue else state_counts(tasks),
                **({"queue": {key: value for key, value in queue.items() if key not in {"records", "counts"}}} if queue else {}),
                "material_rejected": material_rejected, "material_rejected_all": material_rejected_all,
                "events": store.events() if include_events else [],
                "manual": runner.manual_state() if runner.running else None}

    @app.get("/api/capture/queue")
    def capture_queue(batch_ids: str = "all", q: str = ""):
        """跨批次有效任务清单：列表、统计、全选和开跑共用这一份数据。"""
        wanted = None if batch_ids in {"", "all"} else [value for value in batch_ids.split(",") if value]
        records = worker.tasks() if worker else materials.effective_capture_tasks()
        if wanted is not None:
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
    def material_batches(view: Literal["full", "options"] = "full"):
        return {"batches": materials.batch_options() if view == "options" else materials.list_batches()}

    @app.get("/api/materials/batch-management")
    def manage_material_batches(deleted: bool = False):
        return {"batches": materials.batch_management.list(deleted=deleted)}

    @app.post("/api/materials/batches")
    def import_local_materials(payload: dict):
        if set(payload) - {"input_dir", "name", "description"} or not isinstance(payload.get("name"), str):
            raise ValueError("请填写批次名称")
        if not isinstance(payload.get("input_dir"), str) or not payload["input_dir"].strip():
            raise ValueError("请选择素材目录")
        with control_lock:
            idle()
            return materials.create_local_batch(Path(payload["input_dir"]),
                                                name=payload["name"], description=payload.get("description", ""))

    @app.post("/api/materials/uploads")
    def begin_material_upload(payload: dict):
        if set(payload) - {"name", "description"} or not isinstance(payload.get("name"), str):
            raise ValueError("请填写批次名称")
        with control_lock:
            idle()
            return materials.begin_upload(name=payload["name"], description=payload.get("description", ""))

    @app.patch("/api/materials/batches/{batch_id}")
    def edit_material_batch(batch_id: str, payload: dict):
        if set(payload) - {"name", "description"} or not isinstance(payload.get("name"), str):
            raise ValueError("请填写批次名称")
        with control_lock:
            return materials.batch_management.update(batch_id, name=payload["name"], description=payload.get("description", ""))

    @app.delete("/api/materials/batches/{batch_id}")
    def delete_material_batch(batch_id: str):
        with control_lock:
            try:
                if hub:
                    hub.require_unassigned(batch_id)
                return {"ok": True, "batch": materials.batch_management.delete(batch_id)}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/batches/{batch_id}/restore")
    def restore_material_batch(batch_id: str):
        with control_lock:
            return materials.batch_management.restore(batch_id)

    @app.delete("/api/materials/batches/{batch_id}/permanent")
    def purge_material_batch(batch_id: str):
        with control_lock:
            try:
                if hub:
                    hub.require_unassigned(batch_id)
                return {"ok": True, **materials.batch_management.purge(batch_id)}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/selection/clear")
    def clear_material_selection(payload: dict):
        if set(payload) - {"batch_id"}:
            raise ValueError("清除选择参数无效")
        with control_lock:
            idle()
            try:
                result = materials.batch_management.clear_selection(batch_id=payload.get("batch_id"))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            runner.invalidate_readiness()
            return result

    @app.put("/api/materials/uploads/{batch_id}/file")
    async def upload_material_file(batch_id: str, path: str, request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 50 * 1024 * 1024:
                raise HTTPException(413, "单张图片不能超过 50 MB")
            data.extend(chunk)
        return materials.add_upload_file(batch_id, path, bytes(data))

    @app.post("/api/materials/uploads/{batch_id}/finish")
    def finish_material_upload(batch_id: str):
        return materials.finish_upload(batch_id)

    @app.post("/api/materials/uploads/{batch_id}/abort")
    def abort_material_upload(batch_id: str):
        with control_lock:
            return materials.abort_upload(batch_id)

    @app.post("/api/materials/batches/{batch_id}/analyze")
    def analyze_materials(batch_id: str):
        try:
            return materials.start_analysis(batch_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/batches/{batch_id}/reanalyze")
    def reanalyze_materials(batch_id: str, payload: dict):
        store_ids = payload.get("store_ids")
        if not isinstance(store_ids, list) or not store_ids or any(not isinstance(item, str) for item in store_ids):
            raise HTTPException(422, "store_ids 必须是非空字符串数组") from None
        if len(store_ids) > 2000:
            raise HTTPException(422, "单次最多重新分析 2000 家门店") from None
        try:
            return materials.start_reanalysis(batch_id, store_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/materials/batches/{batch_id}/retry-failed")
    def retry_failed_materials(batch_id: str):
        try:
            return materials.retry_failed(batch_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    def _batch_name(batch_id: str) -> str:
        return materials.batch_name(batch_id)

    @app.get("/api/materials/jobs")
    def material_jobs(active: bool = True, batch_id: str = "", limit: int = 20):
        jobs = materials.list_jobs(active=active, batch_id=batch_id, limit=max(1, min(100, limit)))
        return {"jobs": [{**job, "batch_name": job.get("batch_name") or _batch_name(job["batch_id"])}
                         for job in jobs]}

    @app.get("/api/materials/jobs/{job_id}")
    def material_job(job_id: str, events: bool = True):
        try:
            return materials.job_detail(job_id, events=events)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/materials/jobs/{job_id}/pause")
    def pause_material_job(job_id: str):
        try:
            return materials.pause_job(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/jobs/{job_id}/resume")
    def resume_material_job(job_id: str):
        try:
            return materials.resume_job(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/jobs/{job_id}/cancel")
    def cancel_material_job(job_id: str):
        try:
            return materials.cancel_job(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/tasks/active")
    def active_tasks():
        """任务中心唯一入口：把素材分析与采集运行统一成同一结构，前端不再自行合并两个接口。"""
        items = []
        jobs = materials.list_jobs(active=True, limit=20)
        seen = {job["id"] for job in jobs}
        jobs += [job for job in materials.jobs.recent_finished() if job["id"] not in seen]
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
            tasks = [task for task in store.tasks()
                     if current_root and current_root in Path(task["source_path"]).resolve().parents]
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
            for row in hub.history_records(active_only=True):
                if row["status"] not in ACTIVE_RUN_STATUSES:
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

    def capture_history_task_index() -> dict:
        # 在采集端按分配记录补充旧轮次来源；在共享后台按当前发布快照提取批次。
        tasks = {task["id"]: task for task in store.tasks()}
        if worker:
            with store.connect() as db:
                assigned = db.execute("SELECT task_id,batch_id,batch_name,revision AS version FROM worker_assignments ORDER BY rowid")
                for row in assigned:
                    if row["task_id"] in tasks:
                        tasks[row["task_id"]].update(dict(row))
        else:
            for row in materials.effective_capture_tasks():
                if row["id"] in tasks:
                    tasks[row["id"]] = row
        return tasks

    @app.get("/api/tasks/history")
    def task_history(kind: str = "all", status: str = "", q: str = "", page: int = 1,
                     page_size: int = 20, worker_id: str = ""):
        """任务中心历史页：采集运行与素材分析任务统一成同一结构，按时间倒序分页。"""
        page = max(1, int(page))
        page_size = min(100, max(5, int(page_size)))
        rows: list[dict] = []
        if kind in ("all", "capture"):
            runs = store.run_history(500) if not worker_id else []
            results = store.capture_run_results([run["id"] for run in runs])
            task_index = capture_history_task_index() if runs else {}
            for run in runs:
                item = capture_history_row(run, results.get(run["id"], []), task_index)
                item.pop("tasks")
                rows.append(item)
            if hub:
                rows.extend(hub.history_records(worker_id=worker_id))
        if kind in ("all", "materials") and not worker_id:
            for job in materials.jobs.history(500):
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
    def task_history_detail(kind: str, item_id: str):
        """历史任务详情：采集运行返回逐店状态，素材分析返回阶段事件。"""
        if kind == "capture":
            if hub and ":" in item_id:
                worker_id, run_id = item_id.split(":", 1)
                record = hub.history_detail(worker_id, run_id)
                if record is not None:
                    return record
                raise HTTPException(404, "采集电脑的轮次尚未同步或不存在")
            run = store.get_run(item_id)
            if run is None:
                raise HTTPException(404, "运行记录不存在")
            results = store.capture_run_results([item_id]).get(item_id, [])
            return capture_history_row(run, results, capture_history_task_index())
        if kind == "materials":
            job = materials.job_detail(item_id, events=True)
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

    @app.patch("/api/tasks/history/capture/{run_id}")
    def rename_capture_run(run_id: str, payload: dict):
        """只改任务展示名称；ID、进度、执行时间、批次和历史结果保持原字段。"""
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValueError("任务名称无效")
        name = name.strip()
        if not 1 <= len(name) <= 64:
            raise ValueError("任务名称需为 1 到 64 个字符")
        if store.get_run(run_id) is None:
            raise HTTPException(404, "运行记录不存在")
        store.rename_run(run_id, name)
        results = store.capture_run_results([run_id]).get(run_id, [])
        return capture_history_row(store.get_run(run_id), results, capture_history_task_index())

    @app.get("/api/materials/batches/{batch_id}")
    def material_batch(batch_id: str):
        return materials.get_batch(batch_id)

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}")
    def material_store(batch_id: str, store_id: str):
        return materials.get_store(batch_id, store_id)

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/uploads")
    def begin_material_store_upload(batch_id: str, store_id: str):
        with control_lock:
            idle()
            return materials.begin_store_upload(batch_id, store_id)

    @app.put("/api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}/file")
    async def upload_material_store_file(batch_id: str, store_id: str, upload_id: str, filename: str, request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 50 * 1024 * 1024:
                raise HTTPException(413, "单张图片不能超过 50 MB")
            data.extend(chunk)
        with control_lock:
            idle()
            return materials.add_store_upload_file(batch_id, store_id, upload_id, filename, bytes(data))

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}/commit")
    def commit_material_store_upload(batch_id: str, store_id: str, upload_id: str, payload: dict):
        if set(payload) - {"mode", "files"} or "mode" not in payload or "files" not in payload:
            raise ValueError("补图提交参数无效")
        with control_lock:
            idle()
            return materials.commit_store_upload(batch_id, store_id, upload_id, payload["mode"], payload["files"])

    @app.delete("/api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}")
    def abort_material_store_upload(batch_id: str, store_id: str, upload_id: str):
        with control_lock:
            idle()
            materials.abort_store_upload(batch_id, store_id, upload_id)
            return {"ok": True}

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/reanalyze")
    def reanalyze_material_store(batch_id: str, store_id: str):
        with control_lock:
            idle()
            return materials.reanalyze_store(batch_id, store_id)

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}/images/{image_index}")
    def material_image(batch_id: str, store_id: str, image_index: int):
        result = materials.preview_original(batch_id, store_id, image_index)
        return Response(result["bytes"], media_type=result["media_type"])

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}/images/{image_index}/candidates/{candidate_index}")
    def material_candidate(batch_id: str, store_id: str, image_index: int, candidate_index: int):
        result = materials.preview_candidate(batch_id, store_id, image_index, candidate_index)
        return Response(result["bytes"], media_type=result["media_type"])

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}/selected")
    def material_selected_candidate(batch_id: str, store_id: str):
        """当前门店正在使用的候选二维码；未选码的门店返回 404，不猜测。"""
        record = materials.get_store(batch_id, store_id)
        selected = record.get("selected") or {}
        if selected.get("image_index") is None or selected.get("candidate_index") is None:
            raise HTTPException(404, "该门店尚未选择二维码")
        result = materials.preview_candidate(batch_id, store_id, int(selected["image_index"]),
                                             int(selected["candidate_index"]))
        return Response(result["bytes"], media_type=result["media_type"],
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/decision")
    def decide_material_store(batch_id: str, store_id: str, payload: dict):
        allowed = {"decision", "image_index", "candidate_index", "box", "reject_reason", "reject_detail"}
        if set(payload) - allowed or "decision" not in payload:
            raise ValueError("确认参数无效")
        if payload["decision"] == "skip" and not payload.get("reject_reason"):
            raise ValueError("请选择不合格原因")
        try:
            return materials.decide_store(batch_id, store_id, **payload)
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
    def accept_material_recommendations(batch_id: str):
        return materials.accept_recommendations(batch_id)

    @app.post("/api/materials/batches/{batch_id}/publish")
    def publish_materials(batch_id: str, payload: dict | None = None):
        with control_lock:
            fields = payload or {}
            materials.versions.validate(fields.get("name"), fields.get("version"), fields.get("description", ""))
            run = store.latest_run()
            can_activate = not runner.running and not (run and run["status"] in {"paused", "blocked", "interrupted"})
            result = materials.publish_snapshot(
                batch_id, fields.get("description", ""), name=fields["name"], version=fields["version"],
                activate=can_activate,
            )
            if can_activate:
                runner.invalidate_readiness()
            if hub:
                hub.reconcile_assignments()
            return result

    @app.get("/api/materials/rework")
    def material_rework(batch_ids: str = "all", q: str = ""):
        """跨批次返工清单：待处理 / 不合格 / 已选码待发布。"""
        wanted = None if batch_ids in {"", "all"} else [value for value in batch_ids.split(",") if value]
        records = materials.rework_records(wanted)
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
    def return_material_rework(payload: dict):
        """打回选错码的门店：恢复待处理，等待人工在原批次重新选码。"""
        if set(payload) != {"batch_id", "store_id"}:
            raise ValueError("打回参数无效")
        with control_lock:
            idle()
            result = materials.return_rework_store(str(payload["batch_id"]), str(payload["store_id"]))
            if hub:
                hub.reconcile_assignments()
            return result

    @app.post("/api/materials/rework/publish")
    def publish_material_rework(payload: dict):
        """统一发布已处理门店：按原批次分组，各自在最新快照上增量更新。"""
        payload = payload or {}
        if set(payload) - {"store_ids", "description"}:
            raise ValueError("发布参数无效")
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
            groups.setdefault(batch_id, []).append(store_id)
        names = {batch["batch_id"]: batch.get("name", "") for batch in materials.list_batches()}
        description = str(payload.get("description") or "跨批次返工发布")
        results, errors = [], []
        with control_lock:
            idle()
            for batch_id, ids in groups.items():
                try:
                    result = materials.publish_snapshot(batch_id, description, store_ids=ids, activate=False)
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
    def activate_material_version(batch_id: str, payload: dict):
        with control_lock:
            idle()
            run = store.latest_run()
            if run and run["status"] in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "请先继续或终止当前采集，再切换素材版本")
            result = materials.activate_version(batch_id, payload.get("version", ""))
            runner.invalidate_readiness()
            return result

    @app.get("/api/doctor")
    def doctor():
        return environment.snapshot()

    @app.get("/api/environment")
    def environment_status():
        return environment.snapshot()

    @app.get("/api/connections")
    def connection_status():
        return connections.snapshot()

    @app.post("/api/connections/connect")
    def connection_start(payload: dict):
        if not isinstance(payload.get("force", False), bool):
            raise ValueError("force 必须是布尔值")
        if runner.running:
            raise HTTPException(409, "采集运行中，暂不能执行环境检测。")
        connections.connect(force=payload.get("force", True), wait=True)
        return connections.snapshot()

    @app.post("/api/environment/check")
    def environment_check():
        return environment.check()

    @app.post("/api/environment/test")
    def environment_test(payload: dict):
        with control_lock:
            idle()
            check_id = payload.get("id")
            if not isinstance(check_id, str):
                raise ValueError("测试项 ID 无效")
            try:
                return environment.run_test(check_id)
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/environment/settings")
    def environment_open_settings(payload: dict):
        with control_lock:
            idle()
            pane = payload.get("pane")
            if not isinstance(pane, str):
                raise ValueError("系统设置项目无效")
            try:
                return environment.open_settings(pane)
            except RuntimeError as exc:
                raise HTTPException(400, str(exc)) from None

    @app.post("/api/test-model")
    def test_model():
        with control_lock:
            idle()
            snapshot = environment.run_test("model_service")
            check = next(item for item in snapshot["checks"] if item["id"] == "model_service")
            return {"ok": check["status"] == "ready", "message": check["summary"], "checked_at": check["checked_at"]}

    @app.post("/api/scan")
    def scan():
        with control_lock:
            idle()
            config = configs.load()
            if not config.input_dir:
                raise HTTPException(400, "请先选择用于采集的发布版本")
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
            return {**result, "current_total": current_total, "material_release": materials.active_release(root_dir)}
 
    @app.post("/api/start")
    def start():
        with control_lock:
            idle()
            run = store.latest_run()
            if run and run["status"] in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "请先继续或终止当前批次")
            runner.start()
            return status()

    @app.post("/api/resume")
    def resume(payload: dict | None = None):
        with control_lock:
            idle()
            run = store.latest_run()
            if not run or run["status"] not in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "当前没有可恢复的暂停或阻塞批次")
            payload = payload or {}
            mode = payload.get("mode", "")
            if payload and (set(payload) != {"mode"} or mode != "manual" or sys.platform != "win32"
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
                    worker.check_start(remaining)
                if mode:
                    runner.start(resume_run=run, capture_mode=mode)
                else:
                    runner.start(resume_run=run)
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from None
            return status()

    @app.post("/api/ui-tars/trial")
    def start_ui_tars_trial(payload: dict | None = None):
        payload = payload or {}
        task_id = payload.get("task_id")
        task_ids = payload.get("task_ids")
        if set(payload) == {"task_ids"}:
            if not isinstance(task_ids, list) or not task_ids or any(
                not isinstance(value, str) or not value.strip() for value in task_ids
            ):
                raise ValueError("全自动试验需要有效的门店任务列表")
            with control_lock:
                idle()
                return runner.start(ui_tars_trial=True, trial_task_ids=[value.strip() for value in task_ids])
        if set(payload) - {"task_id"} or (payload and (not isinstance(task_id, str) or not task_id.strip())):
            raise ValueError("试验需要指定已有的门店任务")
        with control_lock:
            idle()
            if task_id:
                return runner.start(ui_tars_trial=True, trial_task_id=task_id.strip())
            return runner.start(ui_tars_trial=True)

    @app.post("/api/capture/start")
    def start_capture(payload: dict | None = None):
        """统一开跑入口：本轮模式与任务清单在这里锁定，两种模式共用同一批门店。"""
        payload = payload or {}
        if set(payload) - {"mode", "task_ids", "name"}:
            raise ValueError("开跑参数无效")
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
                if worker:
                    worker.check_start(unique_ids)
                result = runner.start(ui_tars_trial=True, trial_task_ids=unique_ids,
                                      cross_batch=True, capture_mode=mode, run_name=name.strip())
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(400, str(exc)) from None
            if worker:
                return {**result, "status": status()}
            try:
                # 采集轮次与审核失效跟随实际开跑，失败不影响已启动的批次。
                materials.register_capture_round(unique_ids)
            except Exception:
                logger.exception("记录采集轮次失败")
            return {**result, "status": status()}

    def manual_device() -> tuple[str, str]:
        local_name = socket.gethostname()
        connection = worker.connection if worker else {}
        return connection.get("id") or local_name, connection.get("name") or local_name

    def _manual_action(request: Request, kind: str, payload: dict) -> dict:
        expected = payload.get("context")
        if request.state.remote_control or expected is not None:
            keys = {"device_id", "run_id", "task_id", "wait_id"}
            if (not isinstance(expected, dict) or set(expected) != keys
                    or any(not isinstance(expected[key], str) or not expected[key] for key in keys)
                    or expected["device_id"] != manual_device()[0]):
                raise HTTPException(409, "采集端或门店上下文已失效，请重读当前状态")
        try:
            result = runner.manual_action(kind, str(payload.get("reason") or ""),
                                          str(payload.get("detail") or ""), expected=expected)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if request.state.remote_control:
            return result | {"manual": runner.manual_state()}
        return result | {"manual": runner.manual_state(), "status": status()}

    @app.get("/api/manual/state")
    def manual_state():
        device_id, device_name = manual_device()
        run = store.latest_run()
        return {"device_id": device_id, "device_name": device_name, "manual": runner.manual_state(),
                "run_name": run.get("name") or "半自动采集" if run else "",
                "run_status": run["status"] if run else "",
                "remote_url": f"https://{remote_control_host}/remote-control" if remote_control_host else ""}

    @app.post("/api/manual/capture")
    def manual_capture(request: Request, payload: dict | None = None):
        payload = payload or {}
        if set(payload) - {"context"}:
            raise ValueError("截图参数无效")
        with control_lock:
            return _manual_action(request, "capture", payload)

    @app.post("/api/manual/reject")
    def manual_reject(request: Request, payload: dict | None = None):
        payload = payload or {}
        if set(payload) - {"reason", "detail", "context"}:
            raise ValueError("不合格参数无效")
        with control_lock:
            return _manual_action(request, "reject", payload)

    @app.post("/api/manual/skip")
    def manual_skip(request: Request, payload: dict | None = None):
        payload = payload or {}
        if set(payload) - {"context"}:
            raise ValueError("跳过参数无效")
        with control_lock:
            return _manual_action(request, "skip", payload)

    @app.post("/api/manual/reason")
    def manual_reason(payload: dict):
        if set(payload) != {"reason"}:
            raise ValueError("不合格原因参数无效")
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
    def stop():
        with control_lock:
            run = store.latest_run()
            if run and run["mode"] == "attached":
                raise HTTPException(409, "单店采集可以终止后重试")
            runner.pause()
            return status()

    @app.post("/api/terminate")
    def terminate():
        with control_lock:
            runner.terminate()
            return status()

    @app.get("/api/tasks/{task_id}/source")
    def task_source_image(task_id: str):
        """任务当前使用的二维码裁剪图；不合格详情用它展示“当前使用的候选码”。"""
        task = store.get(task_id)
        path = Path(task["source_path"]).expanduser()
        if not path.is_file():
            raise HTTPException(404, "素材文件不存在")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.get("/api/tasks/{task_id}/last-frame")
    def task_last_frame(task_id: str):
        """待检查门店的“最后一帧”：取运行期目录里该任务最新的一张画面。

        待检查任务没有发布截图，动作记录也可能因调试日志轮转丢失证据字段；
        运行目录（.uipath-runs / .uipath-staging）仍保留当时的画面，用它兜底。
        """
        if not re.fullmatch(r"[0-9a-f]{20}", task_id):
            raise HTTPException(404, "任务编号无效")
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
    def task_actions(task_id: str, before: int | None = None):
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
    def export_debug_run(run_id: str):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise HTTPException(400, "运行编号无效")
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

    @app.get("/api/rejections")
    def list_rejections(source: str = "all", reason: str = "all", q: str = "", batch_id: str = "all",
                        reviewed: str = "all"):
        return rejections.list(source, reason, q, batch_id, reviewed)

    @app.get("/api/rejections/export.xlsx")
    def export_rejections(source: str = "all", reason: str = "all", q: str = "", batch_id: str = "all",
                          reviewed: str = "all"):
        content = rejections.export_xlsx(source, reason, q, batch_id, reviewed)
        filename = "不合格门店清单.xlsx"
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=unqualified-stores.xlsx; filename*=UTF-8''{quote(filename)}"},
        )

    @app.post("/api/rejections/review")
    def review_rejection(payload: dict):
        """人工复核不合格：只切换确认标记，不改变任务状态与原因。"""
        if set(payload) - {"source_type", "task_id", "batch_id", "store_id", "reviewed"}:
            raise ValueError("人工确认参数无效")
        if payload.get("source_type") not in {"capture", "materials"} or not isinstance(payload.get("reviewed"), bool):
            raise ValueError("人工确认参数无效")
        with control_lock:
            return rejections.mark_reviewed(
                payload["source_type"], task_id=str(payload.get("task_id") or ""),
                batch_id=str(payload.get("batch_id") or ""), store_id=str(payload.get("store_id") or ""),
                reviewed=payload["reviewed"],
            )

    @app.post("/api/rejections/undo-batch")
    def undo_rejections_batch(payload: dict):
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or set(payload) != {"items"}:
            raise ValueError("批量撤销参数无效")
        with control_lock:
            idle()
            return rejections.undo_many(items, protect_task=hub.protect_task if hub else None)

    @app.post("/api/tasks/{task_id}/reject")
    def reject_task(task_id: str, payload: dict):
        if set(payload) - {"reason", "detail"} or "reason" not in payload:
            raise ValueError("不合格原因参数无效")
        with control_lock:
            idle()
            if hub:
                hub.protect_task(task_id)
            return {"ok": True, "task": rejections.mark_task(task_id, payload["reason"], payload.get("detail", ""))}

    @app.post("/api/tasks/{task_id}/rejection/undo")
    def undo_task_rejection(task_id: str):
        with control_lock:
            idle()
            if hub:
                hub.protect_task(task_id)
            return {"ok": True, "task": rejections.undo_task(task_id)}

    @app.post("/api/rejections/tasks/{task_id}/return-to-materials")
    def return_rejection_to_materials(task_id: str):
        """打回当前门店，保留此前执行与迟到结果供回查。"""
        with control_lock:
            idle()
            result = rejections.return_to_materials(task_id)
            if hub:
                hub.reconcile_assignments()
            return result

    @app.get("/api/rejections/tasks/{task_id}/evidence")
    def rejection_evidence(task_id: str):
        task = store.get(task_id)
        name = str(task.get("reject_evidence") or "")
        if hub and Path(name).is_absolute():
            path = Path(name).resolve()
            if hub.root.resolve() not in path.parents or path.name != "evidence.png" or not path.is_file():
                raise HTTPException(404, "不合格证据不存在")
            return FileResponse(path, headers={"Cache-Control": "no-store"})
        if not re.fullmatch(r"[0-9a-f]{20}\.png", name):
            raise HTTPException(404)
        path = store.path.parent / "rejection-evidence" / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/reviews/pending")
    def pending_reviews(batch_ids: str = "all", q: str = ""):
        return review_queue(batch_ids, q)

    @app.post("/api/reviews/pending/rerun")
    def rerun_pending_reviews(payload: dict):
        task_ids = payload.get("task_ids")
        if (not isinstance(task_ids, list) or not task_ids or len(task_ids) > REVIEW_RERUN_LIMIT
                or any(not isinstance(value, str) or not value.strip() for value in task_ids)):
            raise ValueError(f"请选择 1 到 {REVIEW_RERUN_LIMIT} 个待检查任务")
        selected = [value.strip() for value in task_ids]
        if len(set(selected)) != len(selected):
            raise ValueError("待检查任务列表不能包含重复项")
        name = payload.get("name", "")
        if not isinstance(name, str) or len(name.strip()) > 80:
            raise ValueError("任务名称无效")
        with control_lock:
            idle()
            result = runner.start(ui_tars_trial=True, trial_task_ids=selected, cross_batch=True,
                                  capture_mode="auto", run_name=name.strip())
            # 启动成功后才累加轮次，避免校验失败也把批次标成“已重跑”。
            materials.register_capture_round(selected)
            return result

    @app.post("/api/reviews/approve")
    def approve_reviews(payload: dict):
        """最终按原批次审核通过；一次可提交多个批次，批次号与归属不变。"""
        batch_ids = payload.get("batch_ids") if isinstance(payload, dict) else None
        if (not isinstance(batch_ids, list) or not 1 <= len(batch_ids) <= 100
                or any(not isinstance(value, str) or not value.strip() for value in batch_ids)):
            raise ValueError("请选择 1 到 100 个原批次")
        results = []
        with control_lock:
            selected = list(dict.fromkeys(value.strip() for value in batch_ids))
            if hub:
                for batch_id in selected:
                    hub.require_unassigned(batch_id)
            for batch_id in selected:
                results.append(materials.set_approved(batch_id, True))
        return {"ok": True, "batches": results}

    @app.post("/api/materials/batches/{batch_id}/stage")
    def set_material_batch_stage(batch_id: str, payload: dict):
        with control_lock:
            return materials.set_stage(batch_id, payload.get("stage", ""))

    @app.post("/api/materials/batches/{batch_id}/approve")
    def approve_material_batch(batch_id: str):
        with control_lock:
            if hub:
                hub.require_unassigned(batch_id)
            return materials.set_approved(batch_id, True)

    @app.post("/api/materials/batches/{batch_id}/approve/undo")
    def unapprove_material_batch(batch_id: str):
        with control_lock:
            return materials.set_approved(batch_id, False)

    @app.post("/api/rejections/materials/{batch_id}/{store_id}/undo")
    def undo_material_rejection(batch_id: str, store_id: str):
        with control_lock:
            idle()
            current = materials.get_store(batch_id, store_id)
            if current["decision"] != "skipped":
                raise ValueError("该门店当前没有不合格标记")
            return materials.decide_store(batch_id, store_id, "pending")

    @app.get("/api/action-evidence/{name}")
    def action_evidence(name: str):
        if not re.fullmatch(r"[0-9a-f]{32}\.png", name):
            raise HTTPException(404)
        path = store.path.parent / "action-evidence" / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.post("/api/tasks/{task_id}/retry")
    def retry(task_id: str):
        with control_lock:
            idle()
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
    def mark_task_review(task_id: str):
        """人工把已有采集结果的门店改为待检查；不合格记录必须先撤销，避免状态自相矛盾。"""
        with control_lock:
            idle()
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
        config = configs.load()
        if payload.get("path") not in {"input", "output"}:
            raise ValueError("只能打开素材目录或结果目录")
        selected = config.input_dir if payload["path"] == "input" else config.output_dir
        if not selected.strip():
            raise ValueError("请先选择采集素材版本" if payload["path"] == "input" else "请先设置输出目录")
        release = materials.active_release(Path(config.input_dir).resolve()) if config.input_dir else None
        path = (batch_output_dir(selected, release["batch_id"], release.get("batch_name", ""))
                if payload["path"] == "output" and release else Path(selected))
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.run(["/usr/bin/open", str(path)], check=True)
        return {"ok": True}

    @app.get("/api/artifacts/{task_id}")
    def artifacts(task_id: str):
        with runner.output_lock:
            task = store.get(task_id)
            if not task["output_dir"]:
                return {"files": []}
            folder = Path(task["output_dir"]).resolve()
            return {"files": [str(p.relative_to(folder)) for p in sorted(folder.rglob("*"))
                              if p.is_file() and p.suffix.lower() in {".png", ".json"}]}

    @app.get("/api/source/{task_id}")
    def source_image(task_id: str):
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
    def artifact(task_id: str, relative_path: str):
        with runner.output_lock:
            task = store.get(task_id)
            if not task["output_dir"]:
                raise HTTPException(404, "此任务还没有输出")
            directory = Path(task["output_dir"]).resolve()
            path = (directory / relative_path).resolve()
            if directory not in path.parents or not path.is_file() or path.suffix.lower() not in {".png", ".json"}:
                raise HTTPException(404, "输出文件不存在")
            # 在同一锁内读取，避免流式响应延迟打开时目录已被另一个素材版本替换。
            return Response(path.read_bytes(), media_type="image/png" if path.suffix.lower() == ".png" else "application/json")

    @app.get("/api/annotation/state")
    def annotation_state():
        return annotations.state()

    @app.get("/api/annotation/history")
    def annotation_history(task_id: str):
        try:
            return annotations.history(task_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/annotation/start")
    def annotation_start(payload: dict):
        if set(payload) - {"task_ids"} or not isinstance(payload.get("task_ids"), list):
            raise ValueError("请选择要标注的门店")
        task_ids = payload["task_ids"]
        if not all(isinstance(item, str) for item in task_ids):
            raise ValueError("门店编号无效")
        with control_lock:
            if runner.running:
                raise HTTPException(409, "自动采集运行中，请先暂停或终止后再进入人工标注")
            if worker:
                worker.check_start(task_ids)
            return annotations.start(task_ids)

    @app.post("/api/annotation/pause")
    def annotation_pause(payload: dict | None = None):
        if payload:
            raise ValueError("暂停不需要参数")
        with control_lock:
            return annotations.pause()

    @app.post("/api/annotation/resume")
    def annotation_resume(payload: dict | None = None):
        if payload:
            raise ValueError("继续不需要参数")
        with control_lock:
            return annotations.resume()

    @app.post("/api/annotation/decide")
    def annotation_decide(payload: dict | None = None):
        if payload:
            raise ValueError("生成建议不需要参数")
        with control_lock:
            return annotations.decide()

    @app.post("/api/annotation/click")
    def annotation_click(payload: dict):
        allowed = {"box", "category", "layout", "page_state", "target", "hard_negative", "note"}
        if set(payload) - allowed or "box" not in payload:
            raise ValueError("点击参数无效")
        with control_lock:
            return annotations.click(
                payload["box"], str(payload.get("category", "")), str(payload.get("page_state", "")),
                str(payload.get("target", "")), payload.get("hard_negative", False),
                str(payload.get("note", "")), str(payload.get("layout", "")),
            )

    @app.post("/api/annotation/step")
    def annotation_step(payload: dict):
        allowed = {"step_index", "category", "layout", "page_state", "target", "hard_negative", "note"}
        if set(payload) - allowed:
            raise ValueError("样本参数无效")
        with control_lock:
            return annotations.mark_step(
                payload.get("step_index"), payload.get("category"), payload.get("page_state"),
                payload.get("target"), payload.get("hard_negative"), payload.get("note"),
                payload.get("layout"),
            )

    @app.post("/api/annotation/complete")
    def annotation_complete(payload: dict | None = None):
        if payload:
            raise ValueError("完成标注不需要参数")
        with control_lock:
            return annotations.complete()

    @app.post("/api/annotation/skip")
    def annotation_skip(payload: dict | None = None):
        reason = ""
        if payload:
            if set(payload) - {"reason"}:
                raise ValueError("跳过参数无效")
            reason = str(payload.get("reason", ""))
        with control_lock:
            return annotations.skip(reason)

    @app.post("/api/annotation/reject")
    def annotation_reject(payload: dict):
        if set(payload) - {"reason", "detail"} or not isinstance(payload.get("reason"), str):
            raise ValueError("不合格原因无效")
        with control_lock:
            current = annotations.current_task
            result = annotations.reject(payload["reason"], str(payload.get("detail", "")))
            if worker and current:
                worker.record_annotation(current["id"])
            return result

    @app.post("/api/annotation/retry")
    def annotation_retry(payload: dict | None = None):
        if payload:
            raise ValueError("重试不需要参数")
        with control_lock:
            return annotations.retry()

    @app.post("/api/annotation/refresh")
    def annotation_refresh(payload: dict | None = None):
        if payload:
            raise ValueError("刷新画面不需要参数")
        with control_lock:
            return annotations.refresh()

    @app.post("/api/annotation/stop")
    def annotation_stop(payload: dict | None = None):
        if payload:
            raise ValueError("结束标注不需要参数")
        with control_lock:
            return annotations.stop()

    @app.get("/api/annotation/frame")
    def annotation_frame(session: str, name: str):
        try:
            path = annotations.frame_path(session, name)
        except FileNotFoundError:
            raise HTTPException(404, "标注截图不存在") from None
        return FileResponse(path, media_type="image/png")

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
