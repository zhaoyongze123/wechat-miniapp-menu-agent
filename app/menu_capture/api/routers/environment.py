"""环境诊断域：环境检查、连接探测与系统设置入口。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from menu_capture.api.context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    environment, connections, runner = ctx.environment, ctx.connections, ctx.runner
    control_lock, idle = ctx.control_lock, ctx.idle

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
