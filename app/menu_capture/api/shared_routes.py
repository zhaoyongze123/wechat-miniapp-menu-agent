"""共享工作台的管理员接口与采集端协议，浏览器会话和电脑凭证各自鉴权。"""

from pathlib import Path
from typing import Annotated, Literal
import os
import re
import tempfile

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from menu_capture.api.schemas import SharedAssignmentsResponse, SharedStatusResponse
from menu_capture.shared.shared import MAX_RESULT_SIZE, SharedHub, WorkerUnauthorized, safe_artifact
from menu_capture.shared.shared_history import HistoryReport
from menu_capture.shared.shared_worker import SharedWorker


def register_shared_routes(app: FastAPI, role: str, hub: SharedHub | None,
                           worker: SharedWorker | None, control_lock) -> None:
    worker_capabilities = {"project_scope", "run_history", "run_summary", "software_status"}

    def capabilities(request: Request) -> set[str]:
        value = request.headers.get("x-worker-capabilities", "")
        if not value:
            return set()
        tokens = [item.strip() for item in value.split(",")]
        if (len(value) > 160 or not tokens or any(not item for item in tokens)
                or len(tokens) != len(set(tokens)) or set(tokens) - worker_capabilities):
            raise ValueError("采集端能力声明无效")
        return set(tokens)

    @app.get("/healthz")
    def health():
        return {"ok": True, "role": role}

    @app.get("/api/shared/status", response_model=SharedStatusResponse)
    def shared_status(include_history: bool = True, project_id: str | None = None):
        if hub:
            status = hub.status(include_history=include_history, project_id=project_id)
            if project_id is None:
                # 角色检测只需返回基础运行状态；项目分配内容必须经项目参数请求。
                status["assignments"] = []
            return status
        if worker:
            return worker.status(project_id)
        return {"role": "standalone"}

    if worker:
        @app.post("/api/shared/connect")
        def connect(payload: dict):
            if set(payload) != {"url", "code", "name"}:
                raise ValueError("配对参数无效")
            with control_lock:
                return worker.connect(payload["url"], payload["code"], payload["name"])

    if not hub:
        return

    def computer(request: Request) -> str:
        method, _, token = request.headers.get("authorization", "").partition(" ")
        if method.casefold() != "bearer":
            raise WorkerUnauthorized("请输入采集端凭证")
        return hub.authenticate(token)

    @app.post("/api/shared/pairing")
    def pairing():
        return hub.pairing_code()

    @app.patch("/api/shared/workers/{worker_id}")
    def edit_worker(worker_id: str, payload: dict):
        if set(payload) != {"name", "description"}:
            raise ValueError("设备编辑参数无效")
        with control_lock:
            return hub.edit_worker(worker_id, payload["name"], payload["description"])

    @app.post("/api/shared/workers/{worker_id}/revoke")
    def revoke_worker(worker_id: str):
        with control_lock:
            return hub.revoke_worker(worker_id)

    @app.post("/api/shared/workers/{worker_id}/archive")
    def archive_worker(worker_id: str):
        with control_lock:
            return hub.archive_worker(worker_id)

    @app.get("/api/shared/update-rollout")
    def update_rollout():
        return hub.update_rollout()

    @app.put("/api/shared/update-rollout")
    def save_update_rollout(payload: dict):
        # 分批推广只改变分发范围；实际安装仍在各设备本地确认，不扩大远程控制权限。
        if set(payload) != {"mode", "worker_ids"}:
            raise ValueError("分批更新参数无效")
        with control_lock:
            return hub.set_update_rollout(payload["mode"], payload["worker_ids"])

    @app.get("/api/shared/assignments", response_model=SharedAssignmentsResponse)
    def assignment_records(worker_id: Annotated[str, Query(max_length=64)] = "",
                           phase: Literal["", "assigned", "ready", "completed", "reclaimed", "history"] = "",
                           q: Annotated[str, Query(max_length=240)] = "",
                           page: Annotated[int, Query(ge=1)] = 1,
                           size: Annotated[int, Query(ge=1, le=100)] = 30,
                           project_id: Annotated[str, Query(min_length=1, max_length=128)] = ...):
        return hub.assignment_records(worker_id=worker_id, phase=phase, query=q, page=page, size=size,
                                      project_id=project_id)

    @app.post("/api/shared/assignments/reclaim")
    def reclaim_many(payload: dict):
        if set(payload) != {"assignment_ids", "project_id"} or not isinstance(payload["project_id"], str) or not payload["project_id"]:
            raise ValueError("回收参数无效")
        with control_lock:
            return hub.reclaim_many(payload["assignment_ids"], project_id=payload["project_id"])

    @app.post("/api/shared/assignments")
    def assign(payload: dict):
        if (set(payload) != {"worker_id", "task_ids", "project_id"}
                or not isinstance(payload["worker_id"], str)
                or not isinstance(payload["project_id"], str) or not payload["project_id"]):
            raise ValueError("分配参数无效")
        with control_lock:
            return hub.assign(payload["worker_id"], payload["task_ids"], project_id=payload["project_id"])

    @app.post("/api/shared/assignments/{assignment_id}/reclaim")
    def reclaim(assignment_id: str, project_id: Annotated[str, Query(min_length=1, max_length=128)]):
        with control_lock:
            return hub.reclaim(assignment_id, project_id=project_id)

    def result_dir(assignment_id: str, project_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", assignment_id):
            raise HTTPException(404, "采集结果不存在")
        with hub.store.connect() as db:
            if not db.execute(
                    "SELECT 1 FROM shared_results r JOIN shared_assignments a ON a.id=r.assignment_id "
                    "WHERE r.assignment_id=? AND a.project_id=?", (assignment_id, project_id)
            ).fetchone():
                raise HTTPException(404, "采集结果不存在")
        return (hub.root / assignment_id).resolve()

    @app.get("/api/shared/results/{assignment_id}/files")
    def result_files(assignment_id: str,
                     project_id: Annotated[str, Query(min_length=1, max_length=128)]):
        directory = result_dir(assignment_id, project_id)
        return {"files": [p.relative_to(directory).as_posix() for p in sorted(directory.rglob("*"))
                          if p.is_file() and p.suffix.lower() in {".json", ".png"}]}

    @app.get("/api/shared/results/{assignment_id}/files/{relative:path}")
    def result_file(assignment_id: str, relative: str,
                    project_id: Annotated[str, Query(min_length=1, max_length=128)]):
        directory = result_dir(assignment_id, project_id)
        path = directory.joinpath(*safe_artifact(relative).parts).resolve()
        if directory not in path.parents or path.suffix.lower() not in {".png", ".json"} or not path.is_file():
            raise HTTPException(404, "采集文件不存在")
        return FileResponse(path)

    @app.post("/api/shared/worker/pair")
    def pair(payload: dict):
        if set(payload) != {"code", "name"} or not isinstance(payload["name"], str):
            raise ValueError("配对参数无效")
        return hub.pair(payload["code"], payload["name"])

    @app.post("/api/shared/worker/sync")
    def sync(request: Request, payload: dict):
        worker_id = computer(request)
        required = {"ready_ids", "pending_uploads"}
        if not required <= payload.keys() <= required | {"run_summary", "software"}:
            raise ValueError("同步参数无效")
        offered = capabilities(request)
        with control_lock:
            return hub.sync(
                worker_id, payload["ready_ids"], payload["pending_uploads"],
                payload.get("run_summary"), payload.get("software"), offered,
            )

    @app.put("/api/shared/worker/run-history")
    def run_history(request: Request, payload: HistoryReport):
        return hub.record_run_history(computer(request), payload, capabilities=capabilities(request))

    @app.get("/api/shared/worker/assignments/{assignment_id}/source")
    def source(request: Request, assignment_id: str):
        return FileResponse(hub.source(computer(request), assignment_id))

    @app.put("/api/shared/worker/assignments/{assignment_id}/result")
    async def receive(request: Request, assignment_id: str):
        worker_id = await run_in_threadpool(computer, request)
        await run_in_threadpool(capabilities, request)
        await run_in_threadpool(hub.assignment, worker_id, assignment_id)
        if request.headers.get("content-type") != "application/zip":
            raise HTTPException(415, "采集结果须为 ZIP")
        length = request.headers.get("content-length")
        if length is not None and (not length.isdecimal() or len(length) > 10 or int(length) > MAX_RESULT_SIZE):
            raise HTTPException(413, "采集结果超过上传大小限制")
        fd, filename = tempfile.mkstemp(prefix=".incoming-", dir=hub.root)
        file = Path(filename)
        try:
            count = 0
            with os.fdopen(fd, "wb") as stream:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > MAX_RESULT_SIZE:
                        raise HTTPException(413, "采集结果超过上传大小限制")
                    await run_in_threadpool(stream.write, chunk)
            if not count or (length is not None and count != int(length)):
                raise ValueError("采集结果上传不完整")

            def commit():
                with control_lock:
                    return hub.receive(worker_id, assignment_id, file)

            return await run_in_threadpool(commit)
        finally:
            file.unlink(missing_ok=True)
