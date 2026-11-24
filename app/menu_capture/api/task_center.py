"""任务中心 HTTP 路由；列表聚合与历史规则由 TaskCenterService 负责。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..capture.task_center import TaskCenterService, TaskHistoryNotFound


def create_task_center_router(service: TaskCenterService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/tasks/active")
    def active_tasks():
        return service.active_tasks()

    @router.get("/api/tasks/history")
    def task_history(kind: str = "all", status: str = "", q: str = "", page: int = 1,
                     page_size: int = 20, worker_id: str = ""):
        return service.history(kind=kind, status=status, q=q, page=page,
                               page_size=page_size, worker_id=worker_id)

    @router.get("/api/tasks/history/{kind}/{item_id}")
    def task_history_detail(kind: str, item_id: str):
        try:
            return service.detail(kind, item_id)
        except TaskHistoryNotFound as exc:
            raise HTTPException(404, str(exc)) from None

    @router.patch("/api/tasks/history/capture/{run_id}")
    def rename_capture_run(run_id: str, payload: dict):
        try:
            return service.rename_capture_run(run_id, payload)
        except TaskHistoryNotFound as exc:
            raise HTTPException(404, str(exc)) from None

    return router
