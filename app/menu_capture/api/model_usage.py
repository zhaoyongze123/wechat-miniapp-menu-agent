"""模型用量查询 HTTP 路由；统计与筛选规则由 UsageLedger 负责。"""
from __future__ import annotations

from fastapi import APIRouter


def create_model_usage_router(model_usage) -> APIRouter:
    router = APIRouter()

    @router.get("/api/model-usage")
    def model_usage_report(date_from: str | None = None, date_to: str | None = None,
                           group_by: str = "day", source: str = "all", run_id: str = "",
                           task_id: str = "", provider: str = "", model: str = "",
                           page: int = 1, page_size: int = 30):
        # 查询只读本地计量表，不触发模型、环境检测或微信操作。
        return model_usage.query(date_from=date_from, date_to=date_to, group_by=group_by,
                                 source=source, run_id=run_id, task_id=task_id,
                                 provider=provider, model=model, page=page, page_size=page_size)

    return router
