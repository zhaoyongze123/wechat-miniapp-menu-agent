"""人工标注域：标注会话的状态、增删样本与画面读取。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from menu_capture.api.context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    annotations, runner, worker = ctx.annotations, ctx.runner, ctx.worker
    control_lock = ctx.control_lock

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
