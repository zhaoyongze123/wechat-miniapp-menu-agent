"""软件更新域：版本检查、下载、清理与安装状态。"""
from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query

from menu_capture.api.context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    updates, install = ctx.updates, ctx.install

    @app.get("/api/updates/status")
    def updates_status():
        return updates.status()

    @app.post("/api/updates/check")
    def updates_check():
        return updates.check(force=True)

    @app.put("/api/updates/preferences")
    def updates_preferences(payload: dict):
        return updates.save_preferences(payload)

    @app.get("/api/updates/history")
    def updates_history(limit: Annotated[int, Query(ge=1, le=200)] = 50):
        return updates.history(limit)

    @app.post("/api/updates/download")
    def updates_download():
        try:
            return updates.start_download()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/updates/download/cancel")
    def updates_download_cancel():
        return updates.cancel_download()

    @app.post("/api/updates/update")
    def updates_update(payload: dict | None = None):
        # 一次授权：自动下载、等待空闲、安装、重启与健康检查，不再要求二次确认。
        try:
            return updates.authorize_update(payload or {})
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/updates/update/cancel")
    def updates_update_cancel():
        try:
            return updates.cancel_update()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/updates/cleanup")
    def updates_cleanup_preview():
        # 只读预览：不删除任何文件，供界面展示可释放空间与保护原因。
        return updates.storage_preview()

    @app.post("/api/updates/cleanup")
    def updates_cleanup(payload: dict):
        # 破坏性操作必须提交 confirm；安装/下载进行中由服务端闸门拒绝。
        try:
            return updates.cleanup(payload)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/updates/install")
    def updates_install_status():
        return install.status()

    @app.post("/api/updates/install/schedule")
    def updates_install_schedule():
        # 只安排空闲后安装：不结束采集，执行器在真正切换前再次校验空闲。
        try:
            return updates.schedule_install()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/updates/install/schedule/cancel")
    def updates_install_schedule_cancel():
        return updates.cancel_install_schedule()

    @app.post("/api/updates/install")
    def updates_install_start(payload: dict):
        # confirm 必须等于目标版本，避免误触把生产采集端切到非预期版本。
        try:
            return install.start(str(payload.get("version") or ""), str(payload.get("confirm") or ""))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
