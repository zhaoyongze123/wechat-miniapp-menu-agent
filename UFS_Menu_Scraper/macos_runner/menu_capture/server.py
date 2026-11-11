from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image
from starlette.background import BackgroundTask

from .config import ConfigStore, ROOT
from .capture_outputs import batch_output_dir
from .failures import describe as describe_failure
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


def create_app(root: Path = ROOT, desktop=None) -> FastAPI:
    # 无论通过脚本、python -m 或 ASGI 工厂启动，都只在本进程内禁用环境代理。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    configs = ConfigStore(root)
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
    rejections = RejectionService(store, materials)
    environment = EnvironmentDiagnostics(configs, runner, project_dir)
    connections = ConnectionManager(environment, control_lock, root)
    annotations = AnnotationSession(configs, store, profiles, RunnerExecutor(runner),
                                    root=root.parent.parent, ocr_reader=image_ocr,
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
        threading.Thread(target=_warm_local_ocr, name="ocr-warmup", daemon=True).start()
        # 常驻 1s 巡检：心跳回收 + 单通道调度；不依赖任何请求触发。
        materials.start_job_runner()
        try:
            yield
        finally:
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

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runner, app.state.configs, app.state.store = runner, configs, store
    app.state.debug_log = debug_log
    app.state.model_usage = model_usage
    app.state.token = token
    app.state.materials = materials
    app.state.environment = environment
    app.state.connections = connections
    app.state.annotations = annotations

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"detail": "只允许本机访问"}, status_code=403)
        if request.method != "GET":
            supplied = request.headers.get("x-capture-token", "")
            origin = request.headers.get("origin")
            if not hmac.compare_digest(supplied, token) or (origin and origin != f"http://{request.headers.get('host')}"):
                return JSONResponse({"detail": "本机操作令牌无效，请刷新控制台"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; connect-src 'self'"
        return response

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

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (ROOT / "web/index.html").read_text(encoding="utf-8")
        return html.replace("__CAPTURE_TOKEN__", token)

    @app.get("/assets/{name}")
    def material_assets(name: str):
        if name not in {"materials.js", "materials.css", "workspace.css", "model-usage.js", "model-usage.css",
                        "annotation.js", "annotation.css", "task-center.js", "task-center.css"}:
            raise HTTPException(404, "资源不存在")
        return FileResponse(ROOT / "web" / name, headers={"Cache-Control": "no-cache"})

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
    def local_session():
        # 服务恢复后同源页面换取新令牌；与首页一样由浏览器同源策略保护。
        return {"token": token}

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

    @app.get("/api/status")
    def status():
        # 任务表保留历史；更换成清洗后的目录时，只展示当前素材集，避免旧照片继续可选。
        settings = configs.load()
        input_dir = settings.input_dir
        current_root = Path(input_dir).expanduser().resolve() if input_dir else None
        release = materials.active_release(current_root) if current_root else None
        all_tasks = store.tasks()
        tasks = [task | describe_failure(task.get("error_code", "")) for task in all_tasks
                 if current_root and current_root in Path(task["source_path"]).resolve().parents]
        ready = runner.last_readiness or {"ready": None, "issues": ["环境诊断可在设置中手动查看"], "launch_mode": profiles.load()["launch_mode"]}
        run = store.latest_run()
        if run:
            by_id = {task["id"]: task for task in all_tasks}
            ids = run["task_ids"]
            processed = min(max(run["current_index"], 0), len(ids))
            processed_tasks = [by_id.get(task_id, {}) for task_id in ids[:processed]]
            captured = sum(task.get("status") == "captured" for task in processed_tasks)
            unqualified = sum(task.get("status") != "captured" and bool(task.get("reject_reason"))
                              for task in processed_tasks)
            run.update(processed=processed, captured=captured, unqualified=unqualified,
                       review=processed-captured-unqualified,
                       remaining=len(ids)-processed,
                       next_name=by_id.get(ids[processed], {}).get("source_name", "") if processed < len(ids) else "")
        launch_mode = ready.get("launch_mode", "manual")
        return {"execution_engine": "UiPath", "launch_mode": launch_mode, "engine_ready": ready["ready"], "engine_message": "；".join(ready["issues"]) if ready["issues"] else ("已就绪，点击开始后由 UiPath CLI 自动启动 Main.xaml" if launch_mode == "cli" else "已就绪，等待手动在 Studio 运行 Main.xaml"),
                "robot_readiness": ready, "run_directory": str(getattr(runner, "run_directory", "") or ""),
                "running": runner.running, "stop_requested": runner.stop.is_set(),
                "run": run, "current_task": runner.current_task,
                "material_release": release,
                "batch_output_dir": (str(batch_output_dir(settings.output_dir,
                                                          release["batch_id"] if release else "",
                                                          release.get("batch_name", "") if release else ""))
                                     if settings.output_dir else ""),
                "input_name": current_root.name if current_root else "",
                "tasks": tasks, "counts": dict(Counter(t["status"] for t in tasks)), "events": store.events()}

    @app.get("/api/materials/batches")
    def material_batches():
        return {"batches": materials.list_batches()}

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
        run = store.latest_run()
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
            processed = min(max(run["current_index"], 0), len(ids))
            items.append({
                "source": "capture",
                "id": run["id"],
                "title": current_root.name if current_root else run["mode"],
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
        return {"tasks": items}

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

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/decision")
    def decide_material_store(batch_id: str, store_id: str, payload: dict):
        allowed = {"decision", "image_index", "candidate_index", "box", "reject_reason", "reject_detail"}
        if set(payload) - allowed or "decision" not in payload:
            raise ValueError("确认参数无效")
        if payload["decision"] == "skip" and not payload.get("reject_reason"):
            raise ValueError("请选择不合格原因")
        return materials.decide_store(batch_id, store_id, **payload)

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
            return result

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
    def resume():
        with control_lock:
            idle()
            run = store.latest_run()
            if not run or run["status"] not in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "当前没有可恢复的暂停或阻塞批次")
            profile = profiles.load()
            if profile.get("mode") != run["mode"]:
                raise HTTPException(409, "采集方式与原批次不一致；请恢复原采集方式后继续")
            remaining = run["task_ids"][run["current_index"]:]
            if not remaining:
                raise HTTPException(409, "此批次没有尚未完成的门店")
            try:
                runner.start(resume_run=run)
            except RuntimeError as exc:
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
    def list_rejections(source: str = "all", reason: str = "all", q: str = "", batch_id: str = "all"):
        return rejections.list(source, reason, q, batch_id)

    @app.get("/api/rejections/export.xlsx")
    def export_rejections(source: str = "all", reason: str = "all", q: str = "", batch_id: str = "all"):
        content = rejections.export_xlsx(source, reason, q, batch_id)
        filename = "不合格门店清单.xlsx"
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=unqualified-stores.xlsx; filename*=UTF-8''{quote(filename)}"},
        )

    @app.post("/api/tasks/{task_id}/reject")
    def reject_task(task_id: str, payload: dict):
        if set(payload) - {"reason", "detail"} or "reason" not in payload:
            raise ValueError("不合格原因参数无效")
        with control_lock:
            idle()
            return {"ok": True, "task": rejections.mark_task(task_id, payload["reason"], payload.get("detail", ""))}

    @app.post("/api/tasks/{task_id}/rejection/undo")
    def undo_task_rejection(task_id: str):
        with control_lock:
            idle()
            return {"ok": True, "task": rejections.undo_task(task_id)}

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
            task = store.get(task_id)
            if task["status"] not in {"needs_review", "needs_retry", "error", "blocked", "captured"}:
                raise ValueError("此任务还没有可重试或重新采集的结果")
            # 重试开始不等于问题已经解决；不合格记录保留到新一轮成功采集。
            store.update(task_id, status="pending", stage="queued", message="已重新排队；旧输出保留")
            return {"ok": True}

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
            return annotations.reject(payload["reason"], str(payload.get("detail", "")))

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
    ROOT.joinpath("data").mkdir(exist_ok=True)
    # 进程锁防止两个服务实例同时接管鼠标；不用新增桌面代理进程。
    try:
        with instance_lock(ROOT / "data/runner.lock"):
            os.umask(0o077)
            app = create_app()
            app.state.debug_log.install_service_logging()
            uvicorn.run(app, host="127.0.0.1", port=8765, access_log=False, log_config=None)
    except InstanceLockedError:
        raise SystemExit("本机控制台已运行，请打开 http://127.0.0.1:8765")


if __name__ == "__main__":
    main()
