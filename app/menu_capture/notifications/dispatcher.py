"""异步非阻塞告警分发器：负责去重、冷静期、滑动窗口限频、失败退避重试与 Hub 转发。"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from menu_capture.notifications.channels import DeliveryResult, NotificationChannel, SmtpEmailChannel
from menu_capture.notifications.models import AlertEvent, EmailNotificationConfig, utc_now_iso
from menu_capture.notifications.store import NotificationStore

if TYPE_CHECKING:
    from menu_capture.core.config import ConfigStore

logger = logging.getLogger(__name__)

DEFAULT_RETRY_DELAYS: tuple[float, ...] = (10.0, 30.0, 90.0)


class NotificationDispatcher:
    """统一告警分发器；所有外部发送均在后台线程执行，绝不阻塞采集主循环或 HTTP 请求。"""

    def __init__(
        self,
        configs: ConfigStore,
        store: NotificationStore,
        *,
        channel_factory: Callable[[EmailNotificationConfig, str], NotificationChannel] | None = None,
        forward_to_hub: Callable[[AlertEvent], bool] | None = None,
        suppress_check: Callable[[AlertEvent], bool] | None = None,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        self.configs = configs
        self.store = store
        self._channel_factory = channel_factory or (lambda cfg, secret: SmtpEmailChannel(cfg, secret))
        self._forward_to_hub = forward_to_hub
        self._suppress_check = suppress_check
        self._retry_delays = retry_delays
        self._queue: queue.Queue[AlertEvent | None] = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_loop, name="notification-dispatcher", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def emit(self, event: AlertEvent) -> bool:
        """非阻塞投递告警事件；队列满或已停止时记录日志并返回 False，不抛出异常影响业务。"""
        if self._stop.is_set():
            return False
        self.start()
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            logger.warning("notification_queue_full event_type=%s dedupe_key=%s", event.event_type, event.dedupe_key)
            return False

    def send_test_email(self, *, device_id: str = "", device_name: str = "") -> dict:
        """同步发送测试邮件，供设置页即时校验 SMTP 连通性与授权码有效性。"""
        cfg = self.configs.load().notifications.email
        secret = self.configs.email_secret()
        effective_recipients = self.resolve_recipients(cfg, device_id) if device_id else list(cfg.recipients)
        effective_cfg = cfg.model_copy(update={"recipients": effective_recipients})
        event = AlertEvent(
            dedupe_key=f"test:email:{device_id}" if device_id else "test:email",
            event_type="batch_completed_summary",
            severity="info",
            title="邮件告警通道连通性测试",
            body="这是一封来自 Menu Capture 统一通知中心的测试邮件。收到此邮件说明发件配置、收件路由与授权码均已生效。",
            device_id=device_id,
            device_name=device_name or "当前主机",
        )
        channel = self._channel_factory(effective_cfg, secret)
        result = channel.send(event)
        sent_at = utc_now_iso() if result.ok else ""
        self.store.record(
            event,
            channel=channel.name,
            status="sent" if result.ok else "failed",
            attempts=1,
            error=result.error,
            sent_at=sent_at,
            recipients=effective_recipients,
        )
        return {
            "ok": result.ok,
            "error": result.error,
            "recipients": effective_recipients,
            "sent_at": sent_at,
        }

    def resolve_recipients(self, cfg: EmailNotificationConfig, device_id: str) -> list[str]:
        """按设备解析最终收件人：设备配置了专属邮箱时优先使用，按开关决定是否抄送全局收件人；否则回退全局。"""
        worker_recipients = self.store.get_worker_recipients(device_id) if device_id else []
        if not worker_recipients:
            return list(cfg.recipients)
        merged = list(worker_recipients)
        if cfg.cc_global_on_worker_override:
            seen = {item.lower() for item in merged}
            for item in cfg.recipients:
                if item.lower() not in seen:
                    seen.add(item.lower())
                    merged.append(item)
        return merged

    def deliver_now(self, event: AlertEvent) -> DeliveryResult:
        """同步执行一次带去重、限频与退避重试的投递（供后台线程和单测调用）。"""
        if self._suppress_check and self._suppress_check(event):
            self.store.record(event, channel="email", status="suppressed", error="软件更新窗口期抑制告警")
            return DeliveryResult(ok=False, retryable=False, error="suppressed")

        # Worker 共享模式下优先转发至 Hub 统一发信；若 Hub 成功接收则本机不再重复发信。
        if self._forward_to_hub is not None:
            try:
                if self._forward_to_hub(event):
                    self.store.record(event, channel="email", status="forwarded", attempts=1, sent_at=utc_now_iso())
                    return DeliveryResult(ok=True)
            except Exception as exc:
                logger.warning("notification_hub_forward_failed err=%s", type(exc).__name__)

        cfg = self.configs.load().notifications.email
        if not cfg.enabled:
            return DeliveryResult(ok=False, retryable=False, error="disabled")
        if event.event_type not in cfg.enabled_events:
            return DeliveryResult(ok=False, retryable=False, error="event_disabled")

        now = datetime.now(timezone.utc)
        last_sent = self.store.last_sent_time("email", event.dedupe_key)
        if last_sent is not None and (now - last_sent) < timedelta(minutes=cfg.cooldown_minutes):
            self.store.record(
                event,
                channel="email",
                status="suppressed",
                error=f"冷静期内去重（{cfg.cooldown_minutes} 分钟）",
            )
            return DeliveryResult(ok=False, retryable=False, error="cooldown")

        hour_ago = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        if self.store.count_sent_since("email", hour_ago) >= cfg.hourly_limit:
            self.store.record(event, channel="email", status="rate_limited", error=f"已达每小时上限（{cfg.hourly_limit} 封）")
            return DeliveryResult(ok=False, retryable=False, error="hourly_limit")

        day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds")
        if self.store.count_sent_since("email", day_ago) >= cfg.daily_limit:
            self.store.record(event, channel="email", status="rate_limited", error=f"已达每日上限（{cfg.daily_limit} 封）")
            return DeliveryResult(ok=False, retryable=False, error="daily_limit")

        secret = self.configs.email_secret()
        effective_recipients = self.resolve_recipients(cfg, event.device_id)
        effective_cfg = cfg.model_copy(update={"recipients": effective_recipients})
        channel = self._channel_factory(effective_cfg, secret)
        max_attempts = 1 + len(self._retry_delays)
        last_result = DeliveryResult(ok=False, retryable=False, error="未知错误")
        for attempt in range(1, max_attempts + 1):
            last_result = channel.send(event)
            if last_result.ok:
                self.store.record(
                    event,
                    channel=channel.name,
                    status="sent",
                    attempts=attempt,
                    sent_at=utc_now_iso(),
                    recipients=effective_recipients,
                )
                return last_result
            if not last_result.retryable or attempt >= max_attempts:
                self.store.record(
                    event,
                    channel=channel.name,
                    status="failed",
                    attempts=attempt,
                    error=last_result.error,
                    recipients=effective_recipients,
                )
                logger.warning(
                    "notification_email_failed event_type=%s attempts=%d retryable=%s err=%s",
                    event.event_type,
                    attempt,
                    last_result.retryable,
                    last_result.error,
                )
                return last_result
            delay = self._retry_delays[attempt - 1]
            if self._stop.wait(timeout=delay):
                self.store.record(
                    event,
                    channel=channel.name,
                    status="failed",
                    attempts=attempt,
                    error="服务关闭中断重试",
                    recipients=effective_recipients,
                )
                return DeliveryResult(ok=False, retryable=False, error="stopped")
        return last_result

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                break
            try:
                self.deliver_now(event)
            except Exception as exc:
                logger.warning("notification_dispatch_error event_type=%s err=%s", event.event_type, type(exc).__name__)
