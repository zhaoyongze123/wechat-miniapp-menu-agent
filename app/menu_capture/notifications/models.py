"""邮件通知配置与告警事件契约模型。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AlertEventType = Literal[
    "wechat_logged_out",
    "wechat_window_unavailable",
    "batch_circuit_breaker_tripped",
    "worker_offline_sustained",
    "worker_sync_credential_invalid",
    "batch_completed_summary",
]

AlertSeverity = Literal["critical", "warning", "info"]

DEFAULT_ENABLED_EMAIL_EVENTS: tuple[AlertEventType, ...] = (
    "wechat_logged_out",
    "wechat_window_unavailable",
    "batch_circuit_breaker_tripped",
    "worker_offline_sustained",
    "worker_sync_credential_invalid",
)

ALERT_EVENT_LABELS: dict[str, str] = {
    "wechat_logged_out": "微信退出登录",
    "wechat_window_unavailable": "微信主窗口不可用或阻塞暂停",
    "batch_circuit_breaker_tripped": "批量采集连续故障熔断",
    "worker_offline_sustained": "采集电脑持续离线",
    "worker_sync_credential_invalid": "共享后台凭证失效或同步中断",
    "batch_completed_summary": "批量采集完成汇总",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailNotificationConfig(_StrictModel):
    """邮件告警渠道非敏感配置（持久化在 config.local.json，授权码/密钥单独存 secrets.local.json）。"""

    enabled: bool = False
    provider: Literal["smtp", "http_api"] = "smtp"
    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    use_ssl: bool = True
    sender: str = ""
    username: str = ""
    recipients: list[str] = Field(default_factory=list)
    # Worker 配置了专属收件人时，是否同时抄送 Hub 全局默认收件人（默认开启防漏报）。
    cc_global_on_worker_override: bool = True
    cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    hourly_limit: int = Field(default=10, ge=1, le=100)
    daily_limit: int = Field(default=40, ge=1, le=500)
    enabled_events: list[AlertEventType] = Field(default_factory=lambda: list(DEFAULT_ENABLED_EMAIL_EVENTS))
    # 预留云邮件 HTTP API 驱动地址（如阿里云 DirectMail / Resend），Phase 1 默认使用 smtp。
    http_api_url: str = ""

    @field_validator("smtp_host", "sender", "username", "http_api_url")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("sender")
    @classmethod
    def _valid_sender(cls, value: str) -> str:
        if value and not _EMAIL_RE.match(value):
            raise ValueError(f"发件人邮箱格式无效：{value}")
        return value

    @field_validator("recipients")
    @classmethod
    def _valid_recipients(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in values:
            item = raw.strip()
            if not item:
                continue
            if not _EMAIL_RE.match(item):
                raise ValueError(f"收件人邮箱格式无效：{item}")
            lower = item.lower()
            if lower not in seen:
                seen.add(lower)
                cleaned.append(item)
        if len(cleaned) > 20:
            raise ValueError("收件人邮箱最多配置 20 个")
        return cleaned

    @field_validator("enabled_events")
    @classmethod
    def _dedupe_events(cls, values: list[AlertEventType]) -> list[AlertEventType]:
        seen: set[str] = set()
        result: list[AlertEventType] = []
        for item in values:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result


class NotificationsConfig(_StrictModel):
    email: EmailNotificationConfig = Field(default_factory=EmailNotificationConfig)


class AlertEvent(_StrictModel):
    """跨 Hub / Worker / 单机模式的统一告警事件载体。"""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    dedupe_key: str
    event_type: AlertEventType
    severity: AlertSeverity = "critical"
    title: str
    body: str
    device_id: str = ""
    device_name: str = ""
    project_id: str = ""
    run_id: str = ""
    occurred_at: str = Field(default_factory=utc_now_iso)
    extra: dict[str, str] = Field(default_factory=dict)
