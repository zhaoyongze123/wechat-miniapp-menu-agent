"""人工标注域：标注会话的状态、增删样本与画面读取。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from menu_capture.api.context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    annotations, runner, worker = ctx.annotations, ctx.runner, ctx.worker
    control_lock = ctx.control_lock

    def session_scope(project_id: str | None) -> str:
        scope = ctx.project_scope(project_id, write=True)
        if annotations.active and annotations.project_id != scope:
            raise HTTPException(409, "人工标注会话属于其他项目，请先结束原会话")
        return scope

    def public_state(scope: str) -> dict:
        state = annotations.state()
        if not state.get("session_id") or state.get("project_id") == scope:
            return state
        # 单个桌面执行器跨项目共享；其他项目只获知设备被占用，不暴露任务、截图与标注内容。
        state.update({
            "session_id": None, "project_id": scope, "active": False, "paused": False,
            "status": "idle", "message": "", "task_ids": [], "index": 0, "total": 0,
            "task": None, "frame": None, "suggestion": None, "decision": None,
            "steps": [], "current_steps": [], "last_click": None, "completed": [],
            "archived": [], "skipped": [], "rejected": [], "started_at": None,
            "finished_at": None, "busy_elsewhere": annotations.active,
        })
        return state

    def payload_scope(payload: dict | None) -> tuple[dict, str]:
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("标注参数无效")
        return payload, session_scope(payload.get("project_id"))

    @app.get("/api/annotation/state")
    def annotation_state(project_id: str | None = None):
        return public_state(ctx.project_scope(project_id))

    @app.get("/api/annotation/history")
    def annotation_history(task_id: str, project_id: str | None = None):
        try:
            scope = ctx.project_scope(project_id)
            ctx.task_scope(task_id, scope)
            return annotations.history(task_id, project_id=scope)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/annotation/start")
    def annotation_start(payload: dict):
        if set(payload) - {"task_ids", "project_id"} or not isinstance(payload.get("task_ids"), list):
            raise ValueError("请选择要标注的门店")
        task_ids = payload["task_ids"]
        if not all(isinstance(item, str) for item in task_ids):
            raise ValueError("门店编号无效")
        with control_lock:
            scope = ctx.project_scope(payload.get("project_id"), write=True)
            if not task_ids:
                raise ValueError("请选择要标注的门店")
            for task_id in task_ids:
                ctx.task_scope(task_id, scope, write=True)
            if runner.running:
                raise HTTPException(409, "自动采集运行中，请先暂停或终止后再进入人工标注")
            if worker:
                worker.check_start(task_ids, project_id=scope)
            return annotations.start(task_ids, project_id=scope)

    @app.post("/api/annotation/pause")
    def annotation_pause(payload: dict | None = None):
        payload, scope = payload_scope(payload)
        if set(payload) - {"project_id"}:
            raise ValueError("暂停不需要其他参数")
        with control_lock:
            annotations.pause()
            return public_state(scope)

    @app.post("/api/annotation/resume")
    def annotation_resume(payload: dict | None = None):
        payload, scope = payload_scope(payload)
        if set(payload) - {"project_id"}:
            raise ValueError("继续不需要其他参数")
        with control_lock:
            annotations.resume()
            return public_state(scope)

    @app.post("/api/annotation/decide")
    def annotation_decide(payload: dict | None = None):
        payload, scope = payload_scope(payload)
        if set(payload) - {"project_id"}:
            raise ValueError("生成建议不需要其他参数")
        with control_lock:
            annotations.decide()
            return public_state(scope)

    @app.post("/api/annotation/click")
    def annotation_click(payload: dict):
        allowed = {"box", "category", "layout", "page_state", "target", "hard_negative", "note"}
        if set(payload) - (allowed | {"project_id"}) or "box" not in payload:
            raise ValueError("点击参数无效")
        payload, scope = payload_scope(payload)
        with control_lock:
            annotations.click(
                payload["box"], str(payload.get("category", "")), str(payload.get("page_state", "")),
                str(payload.get("target", "")), payload.get("hard_negative", False),
                str(payload.get("note", "")), str(payload.get("layout", "")),
            )
            return public_state(scope)

    @app.post("/api/annotation/step")
    def annotation_step(payload: dict):
        allowed = {"step_index", "category", "layout", "page_state", "target", "hard_negative", "note"}
        if set(payload) - (allowed | {"project_id"}):
            raise ValueError("样本参数无效")
        payload, scope = payload_scope(payload)
        with control_lock:
            annotations.mark_step(
                payload.get("step_index"), payload.get("category"), payload.get("page_state"),
                payload.get("target"), payload.get("hard_negative"), payload.get("note"),
                payload.get("layout"),
            )
            return public_state(scope)

    @app.post("/api/annotation/complete")
    def annotation_complete(payload: dict | None = None):
        payload, scope = payload_scope(payload)
        if set(payload) - {"project_id"}:
            raise ValueError("完成标注不需要其他参数")
        with control_lock:
            annotations.complete()
            return public_state(scope)

    @app.post("/api/annotation/skip")
    def annotation_skip(payload: dict | None = None):
        reason = ""
        if payload:
            if set(payload) - {"reason", "project_id"}:
                raise ValueError("跳过参数无效")
            reason = str(payload.get("reason", ""))
        else:
            payload = {}
        payload, scope = payload_scope(payload)
        with control_lock:
            annotations.skip(reason)
            return public_state(scope)

    @app.post("/api/annotation/reject")
    def annotation_reject(payload: dict):
        if set(payload) - {"reason", "detail", "project_id"} or not isinstance(payload.get("reason"), str):
            raise ValueError("不合格原因无效")
        payload, scope = payload_scope(payload)
        with control_lock:
            current = annotations.current_task
            annotations.reject(payload["reason"], str(payload.get("detail", "")))
            if worker and current:
                worker.record_annotation(current["id"])
            return public_state(scope)

    @app.post("/api/annotation/retry")
    def annotation_retry(payload: dict | None = None):
        payload, scope = payload_scope(payload)
        if set(payload) - {"project_id"}:
            raise ValueError("重试不需要其他参数")
        with control_lock:
            annotations.retry()
            return public_state(scope)

    @app.post("/api/annotation/refresh")
    def annotation_refresh(payload: dict | None = None):
        payload, scope = payload_scope(payload)
        if set(payload) - {"project_id"}:
            raise ValueError("刷新画面不需要其他参数")
        with control_lock:
            annotations.refresh()
            return public_state(scope)

    @app.post("/api/annotation/stop")
    def annotation_stop(payload: dict | None = None):
        payload, scope = payload_scope(payload)
        if set(payload) - {"project_id"}:
            raise ValueError("结束标注不需要其他参数")
        with control_lock:
            annotations.stop()
            return public_state(scope)

    @app.get("/api/annotation/frame")
    def annotation_frame(session: str, name: str, project_id: str | None = None):
        try:
            path = annotations.frame_path(session, name)
        except FileNotFoundError:
            raise HTTPException(404, "标注截图不存在") from None
        scope = ctx.project_scope(project_id)
        if session == annotations.session_id:
            if annotations.project_id != scope:
                raise HTTPException(404, "标注截图不存在")
        else:
            try:
                snapshot = json.loads((Path(path).parent / "session.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise HTTPException(404, "标注截图不存在") from None
            owner = str(snapshot.get("project_id") or "")
            if owner != scope:
                task_ids = snapshot.get("task_ids") or []
                if owner or not task_ids:
                    raise HTTPException(404, "标注截图不存在")
                try:
                    for task_id in task_ids:
                        ctx.task_scope(str(task_id), scope)
                except HTTPException:
                    raise HTTPException(404, "标注截图不存在") from None
        return FileResponse(path, media_type="image/png")
