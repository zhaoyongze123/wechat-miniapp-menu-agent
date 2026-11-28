"""统一服务端告警通知模块：负责渠道抽象、去重限频、异步重试与邮件投递。"""

from menu_capture.notifications.channels import DeliveryResult, NotificationChannel, SmtpEmailChannel
from menu_capture.notifications.dispatcher import NotificationDispatcher
from menu_capture.notifications.models import (
    ALERT_EVENT_LABELS,
    DEFAULT_ENABLED_EMAIL_EVENTS,
    AlertEvent,
    AlertEventType,
    EmailNotificationConfig,
    NotificationsConfig,
)
from menu_capture.notifications.store import NotificationStore

__all__ = [
    "ALERT_EVENT_LABELS",
    "AlertEvent",
    "AlertEventType",
    "DEFAULT_ENABLED_EMAIL_EVENTS",
    "DeliveryResult",
    "EmailNotificationConfig",
    "NotificationChannel",
    "NotificationDispatcher",
    "NotificationStore",
    "NotificationsConfig",
    "SmtpEmailChannel",
]
