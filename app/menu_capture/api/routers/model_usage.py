"""模型用量域：只读统计查询。"""
from __future__ import annotations

from fastapi import FastAPI

from menu_capture.api.context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    model_usage = ctx.model_usage

    @app.get("/api/model-usage")
    def model_usage_report(date_from: str | None = None, date_to: str | None = None,
                           group_by: str = "day", source: str = "all", run_id: str = "",
                           task_id: str = "", provider: str = "", model: str = "",
                           page: int = 1, page_size: int = 30):
        # 统计查询只读本地计量表，绝不触发模型、环境检测或微信操作。
        return model_usage.query(date_from=date_from, date_to=date_to, group_by=group_by,
                                 source=source, run_id=run_id, task_id=task_id,
                                 provider=provider, model=model, page=page, page_size=page_size)
