"""通知渠道抽象与成熟标准库 SMTP 邮件通道实现。"""

from __future__ import annotations

import html
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

from menu_capture.notifications.models import ALERT_EVENT_LABELS, AlertEvent, EmailNotificationConfig

SEVERITY_LABELS = {
    "critical": "严重告警 · 需人工介入",
    "warning": "异常提醒 · 请尽快排查",
    "info": "运行通知",
}

SEVERITY_COLORS = {
    "critical": "#c62828",
    "warning": "#d97706",
    "info": "#2563eb",
}


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    retryable: bool = False
    error: str = ""


class NotificationChannel(Protocol):
    name: str

    def send(self, event: AlertEvent) -> DeliveryResult: ...


def _sanitize_error(message: str, secret: str) -> str:
    text = (message or "").strip()
    if secret and secret in text:
        text = text.replace(secret, "[已隐藏]")
    return text[:280]


def build_email_message(event: AlertEvent, config: EmailNotificationConfig) -> EmailMessage:
    """构造同时包含纯文本与简洁 HTML 的告警邮件。"""
    event_label = ALERT_EVENT_LABELS.get(event.event_type, event.event_type)
    severity_label = SEVERITY_LABELS.get(event.severity, event.severity)
    prefix = "[紧急告警]" if event.severity == "critical" else ("[异常提醒]" if event.severity == "warning" else "[通知]")
    subject = f"{prefix} Menu Capture · {event.title}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Menu Capture 告警中心", config.sender))
    msg["To"] = ", ".join(config.recipients)

    meta_rows = [
        ("告警类型", event_label),
        ("严重级别", severity_label),
        ("发生时间", event.occurred_at),
    ]
    if event.device_name or event.device_id:
        device_desc = event.device_name or event.device_id
        if event.device_name and event.device_id:
            device_desc = f"{event.device_name} ({event.device_id[:8]})"
        meta_rows.append(("采集设备", device_desc))
    if event.project_id:
        meta_rows.append(("项目标识", event.project_id))
    if event.run_id:
        meta_rows.append(("采集轮次", event.run_id))

    plain_lines = [
        f"Menu Capture 通知：{event.title}",
        "-" * 42,
        *[f"{k}：{v}" for k, v in meta_rows],
        "",
        "详情说明：",
        event.body,
        "",
        "排查建议：请登录对应采集主机或共享工作台核查微信登录状态、主窗口焦点及任务中心详情，确认环境恢复后再继续采集。",
    ]
    msg.set_content("\n".join(plain_lines), subtype="plain", charset="utf-8")

    accent = SEVERITY_COLORS.get(event.severity, "#2563eb")
    rows_html = "".join(
        f"<tr><td style='padding:6px 12px 6px 0;color:#64748b;white-space:nowrap;'>{html.escape(k)}</td>"
        f"<td style='padding:6px 0;color:#0f172a;'>{html.escape(v)}</td></tr>"
        for k, v in meta_rows
    )
    html_body = (
        "<!doctype html><html><body style='margin:0;padding:24px;background:#f8fafc;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;'>"
        "<div style='max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;"
        "border-radius:10px;overflow:hidden;'>"
        f"<div style='padding:16px 20px;border-bottom:1px solid #e2e8f0;border-top:4px solid {accent};'>"
        f"<div style='font-size:12px;color:{accent};font-weight:600;'>{html.escape(severity_label)}</div>"
        f"<h2 style='margin:6px 0 0;font-size:18px;color:#0f172a;'>{html.escape(event.title)}</h2>"
        "</div>"
        "<div style='padding:20px;'>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px;'>{rows_html}</table>"
        "<div style='margin-top:16px;padding:12px 14px;background:#f8fafc;border-radius:8px;"
        f"font-size:14px;line-height:1.6;color:#1e293b;white-space:pre-wrap;'>{html.escape(event.body)}</div>"
        "<p style='margin:16px 0 0;font-size:12px;color:#64748b;line-height:1.6;'>"
        "请登录采集主机或共享工作台核查微信状态与任务中心详情，确认环境恢复后再继续批量采集。"
        "</p></div></div></body></html>"
    )
    msg.add_alternative(html_body, subtype="html", charset="utf-8")
    return msg


class SmtpEmailChannel:
    """基于 Python 标准库 smtplib 的 SMTP over SSL/STARTTLS 邮件发送通道（零新增依赖）。"""

    name = "email"

    def __init__(self, config: EmailNotificationConfig, secret: str, *, timeout: float = 15.0) -> None:
        self.config = config
        self.secret = secret
        self.timeout = timeout

    def send(self, event: AlertEvent) -> DeliveryResult:
        if not self.config.smtp_host:
            return DeliveryResult(ok=False, retryable=False, error="未配置 SMTP 服务器地址")
        if not self.config.sender:
            return DeliveryResult(ok=False, retryable=False, error="未配置发件人邮箱")
        if not self.config.recipients:
            return DeliveryResult(ok=False, retryable=False, error="未配置收件人邮箱")
        username = self.config.username or self.config.sender
        if not self.secret:
            return DeliveryResult(ok=False, retryable=False, error="未配置发件邮箱授权码/密码")

        message = build_email_message(event, self.config)
        context = ssl.create_default_context()
        try:
            if self.config.use_ssl:
                with smtplib.SMTP_SSL(
                    self.config.smtp_host,
                    self.config.smtp_port,
                    timeout=self.timeout,
                    context=context,
                ) as client:
                    client.login(username, self.secret)
                    client.send_message(message)
            else:
                with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=self.timeout) as client:
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                    client.login(username, self.secret)
                    client.send_message(message)
            return DeliveryResult(ok=True)
        except smtplib.SMTPAuthenticationError as exc:
            return DeliveryResult(
                ok=False,
                retryable=False,
                error=f"SMTP 认证失败，请检查发件邮箱与授权码（{_sanitize_error(str(exc), self.secret)}）",
            )
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            return DeliveryResult(
                ok=False,
                retryable=False,
                error=f"邮件地址被服务器拒绝（{_sanitize_error(str(exc), self.secret)}）",
            )
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            return DeliveryResult(
                ok=False,
                retryable=True,
                error=f"SMTP 发送异常（{type(exc).__name__}：{_sanitize_error(str(exc), self.secret)}）",
            )
