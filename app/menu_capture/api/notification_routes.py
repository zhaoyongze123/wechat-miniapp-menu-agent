"""统一通知中心后端路由：邮件通道配置、测试发送与 Worker 告警上报协议。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from menu_capture.core.config import ConfigStore
from menu_capture.notifications.dispatcher import NotificationDispatcher
from menu_capture.notifications.models import ALERT_EVENT_LABELS, AlertEvent
from menu_capture.notifications.store import NotificationStore
from menu_capture.shared.shared import SharedHub, WorkerUnauthorized
from menu_capture.shared.shared_worker import SharedWorker


def register_notification_routes(
    app: FastAPI,
    *,
    role: str,
    configs: ConfigStore,
    notification_store: NotificationStore,
    notifications: NotificationDispatcher,
    hub: SharedHub | None,
    worker: SharedWorker | None,
    control_lock,
) -> None:
    def _current_device_name() -> str:
        if worker is not None:
            conn = worker.connection or {}
            return str(conn.get("name") or "采集端")
        if hub is not None:
            return "共享后台 Hub"
        return "本机工作台"

    @app.get("/api/notifications/email")
    def get_email_notification():
        return {
            "email": configs.public_email_notification(),
            "deliveries": notification_store.recent(channel="email", limit=15),
            "event_labels": ALERT_EVENT_LABELS,
            "role": role,
            "managed_by_hub": role == "worker",
        }

    @app.put("/api/notifications/email")
    def update_email_notification(payload: dict):
        with control_lock:
            email_cfg = configs.save_email_notification(payload)
        return {
            "email": email_cfg,
            "deliveries": notification_store.recent(channel="email", limit=15),
            "event_labels": ALERT_EVENT_LABELS,
            "role": role,
            "managed_by_hub": role == "worker",
        }

    @app.post("/api/notifications/email/test")
    async def test_email_notification():
        result = await run_in_threadpool(notifications.send_test_email, device_name=_current_device_name())
        if not result["ok"]:
            raise HTTPException(400, result["error"] or "测试邮件发送失败")
        return result

    if hub is not None:
        @app.post("/api/shared/worker/alerts")
        def receive_worker_alert(request: Request, payload: dict):
            method, _, token = request.headers.get("authorization", "").partition(" ")
            if method.casefold() != "bearer":
                raise WorkerUnauthorized("请输入采集端凭证")
            worker_id = hub.authenticate(token)
            worker_name = ""
            with hub.store.connect() as db:
                row = db.execute("SELECT name FROM shared_workers WHERE id=?", (worker_id,)).fetchone()
                if row:
                    worker_name = str(row["name"] or "")
            event = AlertEvent.model_validate(payload)
            enriched = event.model_copy(
                update={
                    "device_id": worker_id,
                    "device_name": worker_name or event.device_name or worker_id[:8],
                }
            )
            accepted = notifications.emit(enriched)
            return {"accepted": accepted, "event_id": enriched.event_id}
