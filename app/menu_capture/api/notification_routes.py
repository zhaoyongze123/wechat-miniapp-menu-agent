"""统一通知中心后端路由：邮件通道配置、设备独立收件人路由、测试发送与 Worker 告警上报协议。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query, Request
from pydantic import ValidationError
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

    def _authenticate_worker(request: Request) -> str:
        if hub is None:
            raise WorkerUnauthorized("当前非共享后台")
        method, _, token = request.headers.get("authorization", "").partition(" ")
        if method.casefold() != "bearer":
            raise WorkerUnauthorized("请输入采集端凭证")
        return hub.authenticate(token)

    def _validation_error_message(exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            errors = exc.errors()
            if errors:
                msg = str(errors[0].get("msg") or "参数格式无效")
                if msg.startswith("Value error, "):
                    msg = msg[len("Value error, "):]
                return msg
        return str(exc) or "参数格式无效"

    def _worker_name_by_id(worker_id: str) -> str:
        if hub is None or not worker_id:
            return ""
        with hub.store.connect() as db:
            row = db.execute("SELECT name FROM shared_workers WHERE id=?", (worker_id,)).fetchone()
            return str(row["name"] or "") if row else ""

    def _hub_worker_recipients_list() -> list[dict]:
        if hub is None:
            return []
        saved = notification_store.list_worker_recipients()
        rows: list[dict] = []
        with hub.store.connect() as db:
            workers = db.execute(
                """
                SELECT w.id, w.name
                FROM shared_workers w
                LEFT JOIN shared_worker_profiles p ON p.worker_id = w.id
                WHERE coalesce(p.revoked_at, '') = '' AND coalesce(p.archived_at, '') = ''
                ORDER BY w.name, w.id
                """
            ).fetchall()
        for item in workers:
            wid = str(item["id"])
            rows.append({
                "worker_id": wid,
                "worker_name": str(item["name"] or wid[:8]),
                "recipients": saved.get(wid, []),
            })
        return rows

    @app.get("/api/notifications/email")
    async def get_email_notification(device_id: Annotated[str, Query(max_length=64)] = ""):
        email_cfg = configs.public_email_notification()
        deliveries = notification_store.recent(channel="email", limit=20, device_id=device_id if hub else "")
        hub_email_enabled: bool | None = None
        hub_cc_global: bool | None = None
        hub_global_recipients_count: int | None = None
        hub_synced = False
        worker_recipients: list[dict] = []

        if hub is not None:
            worker_recipients = _hub_worker_recipients_list()
        elif worker is not None:
            remote = await run_in_threadpool(worker.fetch_email_settings)
            if remote is not None:
                hub_synced = True
                hub_email_enabled = bool(remote.get("hub_enabled"))
                if "cc_global_on_worker_override" in remote:
                    hub_cc_global = bool(remote.get("cc_global_on_worker_override"))
                if "global_recipients_count" in remote:
                    hub_global_recipients_count = int(remote.get("global_recipients_count") or 0)
                remote_recipients = remote.get("recipients")
                if isinstance(remote_recipients, list):
                    email_cfg["recipients"] = [str(item) for item in remote_recipients if isinstance(item, str)]
                remote_deliveries = remote.get("deliveries")
                if isinstance(remote_deliveries, list):
                    deliveries = remote_deliveries

        return {
            "email": email_cfg,
            "deliveries": deliveries,
            "event_labels": ALERT_EVENT_LABELS,
            "role": role,
            "managed_by_hub": role == "worker",
            "hub_synced": hub_synced,
            "hub_email_enabled": hub_email_enabled,
            "hub_cc_global": hub_cc_global,
            "hub_global_recipients_count": hub_global_recipients_count,
            "worker_recipients": worker_recipients,
        }

    @app.put("/api/notifications/email")
    async def update_email_notification(payload: dict):
        try:
            with control_lock:
                email_cfg = configs.save_email_notification(payload)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(400, _validation_error_message(exc)) from exc
        hub_synced = False
        hub_email_enabled: bool | None = None
        hub_cc_global: bool | None = None
        hub_global_recipients_count: int | None = None
        deliveries = notification_store.recent(channel="email", limit=20)
        if worker is not None:
            hub_synced = await run_in_threadpool(worker.update_email_recipients, list(email_cfg["recipients"]))
            if hub_synced:
                remote = await run_in_threadpool(worker.fetch_email_settings)
                if remote is not None:
                    hub_email_enabled = bool(remote.get("hub_enabled"))
                    if "cc_global_on_worker_override" in remote:
                        hub_cc_global = bool(remote.get("cc_global_on_worker_override"))
                    if "global_recipients_count" in remote:
                        hub_global_recipients_count = int(remote.get("global_recipients_count") or 0)
                    remote_deliveries = remote.get("deliveries")
                    if isinstance(remote_deliveries, list):
                        deliveries = remote_deliveries
        return {
            "email": email_cfg,
            "deliveries": deliveries,
            "event_labels": ALERT_EVENT_LABELS,
            "role": role,
            "managed_by_hub": role == "worker",
            "hub_synced": hub_synced,
            "hub_email_enabled": hub_email_enabled,
            "hub_cc_global": hub_cc_global,
            "hub_global_recipients_count": hub_global_recipients_count,
            "worker_recipients": _hub_worker_recipients_list() if hub is not None else [],
        }

    @app.post("/api/notifications/email/test")
    async def test_email_notification(payload: dict | None = Body(default=None)):
        body = payload if isinstance(payload, dict) else {}
        target_worker_id = str(body.get("worker_id") or "").strip()
        local_only = bool(body.get("local_only"))

        if role == "worker" and worker is not None and not local_only:
            remote = await run_in_threadpool(worker.send_test_email)
            if remote is None:
                raise HTTPException(
                    400,
                    "当前未连接到共享后台 Hub，无法经由 Hub 发送测试邮件；请检查连接或展开“本机断联兜底 SMTP”测试本机发信。",
                )
            if not remote.get("ok"):
                raise HTTPException(400, str(remote.get("error") or "经由 Hub 发送测试邮件失败"))
            return remote

        if hub is not None and target_worker_id:
            worker_name = _worker_name_by_id(target_worker_id)
            result = await run_in_threadpool(
                notifications.send_test_email,
                device_id=target_worker_id,
                device_name=worker_name or target_worker_id[:8],
            )
        else:
            result = await run_in_threadpool(notifications.send_test_email, device_name=_current_device_name())
        if not result["ok"]:
            raise HTTPException(400, result["error"] or "测试邮件发送失败")
        return result

    if hub is not None:
        @app.put("/api/notifications/email/workers/{worker_id}")
        def update_hub_worker_recipients(worker_id: str, payload: dict):
            recipients = payload.get("recipients")
            if not isinstance(recipients, list):
                raise HTTPException(400, "收件人列表格式无效")
            try:
                with control_lock:
                    saved = notification_store.set_worker_recipients(worker_id, [str(x) for x in recipients])
            except (ValidationError, ValueError) as exc:
                raise HTTPException(400, _validation_error_message(exc)) from exc
            return {
                "worker_id": worker_id,
                "recipients": saved,
                "worker_recipients": _hub_worker_recipients_list(),
            }

        @app.get("/api/shared/worker/email-settings")
        def worker_get_email_settings(request: Request):
            worker_id = _authenticate_worker(request)
            cfg = configs.load().notifications.email
            return {
                "worker_id": worker_id,
                "recipients": notification_store.get_worker_recipients(worker_id),
                "hub_enabled": bool(cfg.enabled and configs.email_secret()),
                "cc_global_on_worker_override": cfg.cc_global_on_worker_override,
                "global_recipients_count": len(cfg.recipients),
                "deliveries": notification_store.recent(channel="email", limit=20, device_id=worker_id),
            }

        @app.put("/api/shared/worker/email-settings")
        def worker_put_email_settings(request: Request, payload: dict):
            worker_id = _authenticate_worker(request)
            recipients = payload.get("recipients")
            if not isinstance(recipients, list):
                raise HTTPException(400, "收件人列表格式无效")
            try:
                with control_lock:
                    saved = notification_store.set_worker_recipients(worker_id, [str(x) for x in recipients])
            except (ValidationError, ValueError) as exc:
                raise HTTPException(400, _validation_error_message(exc)) from exc
            return {"worker_id": worker_id, "recipients": saved}

        @app.post("/api/shared/worker/email-test")
        def worker_send_test_email(request: Request):
            worker_id = _authenticate_worker(request)
            worker_name = _worker_name_by_id(worker_id)
            result = notifications.send_test_email(
                device_id=worker_id,
                device_name=worker_name or worker_id[:8],
            )
            if not result["ok"]:
                raise HTTPException(400, result["error"] or "测试邮件发送失败")
            return result

        @app.post("/api/shared/worker/alerts")
        def receive_worker_alert(request: Request, payload: dict):
            worker_id = _authenticate_worker(request)
            worker_name = _worker_name_by_id(worker_id)
            event = AlertEvent.model_validate(payload)
            enriched = event.model_copy(
                update={
                    "device_id": worker_id,
                    "device_name": worker_name or event.device_name or worker_id[:8],
                }
            )
            accepted = notifications.emit(enriched)
            return {"accepted": accepted, "event_id": enriched.event_id}
