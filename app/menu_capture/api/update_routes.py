"""软件更新与安装 API 路由；安全规则由 UpdateService 和 InstallController 执行。"""

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query


def register_update_routes(app: FastAPI, updates, install) -> None:
    """注册更新端点，保持安装确认、更新闸门和服务层错误映射不变。"""

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

    @app.post("/api/updates/install")
    def updates_install_start(payload: dict):
        # confirm 必须等于目标版本，避免误触把生产采集端切到非预期版本。
        try:
            return install.start(str(payload.get("version") or ""), str(payload.get("confirm") or ""))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
