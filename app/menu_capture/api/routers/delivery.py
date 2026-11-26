"""合格成品目录域：Hub 把当前有效合格门店实时同步到宿主机交付目录。"""
from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query

from menu_capture.api.context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    delivery = ctx.delivery

    def require():
        if delivery is None:
            raise HTTPException(404, "当前角色不支持合格成品目录")
        return delivery

    @app.get("/api/delivery/status")
    def delivery_status(project_id: str | None = None):
        try:
            scope = ctx.project_scope(project_id)
            return require().status(scope)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/api/delivery/stores")
    def delivery_stores(
        page: Annotated[int, Query(ge=1)] = 1,
        size: Annotated[int, Query(ge=1, le=200)] = 50,
        q: str = "",
        batch_id: str = "all",
        project_id: str | None = None,
    ):
        try:
            scope = ctx.project_scope(project_id)
            if batch_id not in {"", "all"}:
                ctx.batch_scope(batch_id, scope)
            return require().stores(page=page, size=size, query=q, batch_id=batch_id,
                                    project_id=scope)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/delivery/sync")
    def delivery_sync(project_id: str | None = None):
        service = require()
        try:
            scope = ctx.project_scope(project_id, write=True)
            service.trigger(scope)
            return service.status(scope)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/delivery/reveal")
    def delivery_reveal(project_id: str | None = None):
        service = require()
        try:
            scope = ctx.project_scope(project_id)
            service.status(scope)
            return service.reveal(scope)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
