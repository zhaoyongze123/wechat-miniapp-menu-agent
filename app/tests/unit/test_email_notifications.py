"""统一通知中心邮件告警模块单测：配置凭据隔离、SMTP 通道、去重限频重试与 API 契约。"""

from __future__ import annotations

import smtplib
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from menu_capture.api.server import create_app
from menu_capture.core.config import ConfigStore
from menu_capture.notifications.channels import DeliveryResult, SmtpEmailChannel, build_email_message
from menu_capture.notifications.dispatcher import NotificationDispatcher
from menu_capture.notifications.models import AlertEvent, EmailNotificationConfig
from menu_capture.notifications.store import NotificationStore


def _sample_email_payload(**overrides) -> dict:
    base = {
        "enabled": True,
        "provider": "smtp",
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "use_ssl": True,
        "sender": "sender@163.com",
        "username": "sender@163.com",
        "recipients": [
            "zhaoyongze2023@gmail.com",
            "zhaoyongze2023@163.com",
            "2173058314@qq.com",
        ],
        "cooldown_minutes": 30,
        "hourly_limit": 10,
        "daily_limit": 40,
        "enabled_events": [
            "wechat_logged_out",
            "wechat_window_unavailable",
            "batch_circuit_breaker_tripped",
        ],
        "http_api_url": "",
        "secret": "my-smtp-auth-code",
        "clear_secret": False,
    }
    base.update(overrides)
    return base


def test_config_store_preserves_api_key_and_email_secret_independently(tmp_path: Path):
    configs = ConfigStore(tmp_path, managed=True)
    configs.save({"input_dir": "", "output_dir": str(tmp_path / "out"), "model": {"api_key": "sk-model-123"}})
    assert configs.key() == "sk-model-123"

    pub = configs.save_email_notification(_sample_email_payload())
    assert pub["enabled"] is True
    assert pub["secret_configured"] is True
    assert "secret" not in pub
    assert configs.email_secret() == "my-smtp-auth-code"
    assert configs.key() == "sk-model-123"

    # 再次保存常规设置不得覆盖已保存的邮件配置或授权码
    configs.save({"input_dir": "", "output_dir": str(tmp_path / "out"), "model": {"api_key": "sk-model-456"}})
    assert configs.key() == "sk-model-456"
    assert configs.email_secret() == "my-smtp-auth-code"
    assert configs.load().notifications.email.smtp_host == "smtp.163.com"


def test_smtp_channel_send_auth_error_and_secret_sanitization():
    cfg = EmailNotificationConfig.model_validate(
        {k: v for k, v in _sample_email_payload().items() if k not in {"secret", "clear_secret"}}
    )
    event = AlertEvent(
        dedupe_key="cb:run-1",
        event_type="batch_circuit_breaker_tripped",
        severity="critical",
        title="批量采集已触发熔断暂停",
        body="连续 3 家门店触发微信输入环境故障",
        device_name="共享端A机",
        run_id="run-1",
    )
    msg = build_email_message(event, cfg)
    assert "[紧急告警]" in msg["Subject"]
    assert "zhaoyongze2023@gmail.com" in msg["To"]
    assert "2173058314@qq.com" in msg["To"]

    with patch("smtplib.SMTP_SSL") as mock_ssl:
        client = MagicMock()
        mock_ssl.return_value.__enter__.return_value = client
        res = SmtpEmailChannel(cfg, "super-secret-code").send(event)
        assert res.ok is True
        client.login.assert_called_once_with("sender@163.com", "super-secret-code")
        client.send_message.assert_called_once()

    with patch("smtplib.SMTP_SSL") as mock_ssl:
        client = MagicMock()
        client.login.side_effect = smtplib.SMTPAuthenticationError(535, b"auth failed for super-secret-code")
        mock_ssl.return_value.__enter__.return_value = client
        res = SmtpEmailChannel(cfg, "super-secret-code").send(event)
        assert res.ok is False
        assert res.retryable is False
        assert "super-secret-code" not in res.error


def test_dispatcher_cooldown_rate_limit_retry_and_hub_forward(tmp_path: Path):
    configs = ConfigStore(tmp_path, managed=True)
    configs.save_email_notification(_sample_email_payload(hourly_limit=2, daily_limit=5))
    nstore = NotificationStore(tmp_path / "data" / "tasks.sqlite3")

    attempts: list[int] = []

    class FlakyChannel:
        name = "email"

        def send(self, event: AlertEvent) -> DeliveryResult:
            attempts.append(1)
            if len(attempts) == 1:
                return DeliveryResult(ok=False, retryable=True, error="temporary timeout")
            return DeliveryResult(ok=True)

    dispatcher = NotificationDispatcher(
        configs,
        nstore,
        channel_factory=lambda _c, _s: FlakyChannel(),
        retry_delays=(0.01, 0.01),
    )

    event1 = AlertEvent(
        dedupe_key="cb:run-a",
        event_type="batch_circuit_breaker_tripped",
        title="熔断测试",
        body="连续 3 家环境故障",
    )
    # 第 1 次失败后自动重试并在第 2 次成功
    r1 = dispatcher.deliver_now(event1)
    assert r1.ok is True
    assert len(attempts) == 2

    # 冷静期内同 dedupe_key 自动去重，不再发信
    r2 = dispatcher.deliver_now(event1)
    assert r2.ok is False
    assert r2.error == "cooldown"
    assert len(attempts) == 2

    # 不同 dedupe_key 第 2 封成功，第 3 封触发每小时上限 (hourly_limit=2)
    r3 = dispatcher.deliver_now(event1.model_copy(update={"dedupe_key": "cb:run-b"}))
    assert r3.ok is True
    r4 = dispatcher.deliver_now(event1.model_copy(update={"dedupe_key": "cb:run-c"}))
    assert r4.ok is False
    assert r4.error == "hourly_limit"

    # Worker 共享模式转发至 Hub 成功时，本机不重复发信
    forwarded: list[str] = []
    worker_dispatcher = NotificationDispatcher(
        configs,
        nstore,
        channel_factory=lambda _c, _s: FlakyChannel(),
        forward_to_hub=lambda ev: (forwarded.append(ev.dedupe_key) or True),
    )
    rf = worker_dispatcher.deliver_now(event1.model_copy(update={"dedupe_key": "cb:forward-1"}))
    assert rf.ok is True
    assert forwarded == ["cb:forward-1"]


def test_notification_api_routes_roundtrip(tmp_path: Path):
    app = create_app(root=tmp_path, role="standalone")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        sess = client.get("/api/session").json()
        headers = {"Origin": "http://127.0.0.1", "X-Capture-Token": sess["token"]}

        resp = client.get("/api/notifications/email")
        assert resp.status_code == 200
        assert resp.json()["email"]["enabled"] is False

        put_resp = client.put("/api/notifications/email", json=_sample_email_payload(), headers=headers)
        assert put_resp.status_code == 200
        body = put_resp.json()
        assert body["email"]["enabled"] is True
        assert body["email"]["secret_configured"] is True
        assert len(body["email"]["recipients"]) == 3

        with patch("smtplib.SMTP_SSL") as mock_ssl:
            mock_ssl.return_value.__enter__.return_value = MagicMock()
            test_resp = client.post("/api/notifications/email/test", headers=headers)
            assert test_resp.status_code == 200
            assert test_resp.json()["ok"] is True


def test_worker_recipient_routing_and_delivery_isolation(tmp_path: Path):
    configs = ConfigStore(tmp_path, managed=True)
    configs.save_email_notification(
        _sample_email_payload(
            recipients=["global@example.com"],
            cc_global_on_worker_override=True,
        )
    )
    nstore = NotificationStore(tmp_path / "data" / "tasks.sqlite3")
    nstore.set_worker_recipients("worker-xue", ["xue@example.com"])

    sent_targets: list[tuple[str, list[str]]] = []

    class CaptureChannel:
        name = "email"

        def __init__(self, cfg: EmailNotificationConfig) -> None:
            self.cfg = cfg

        def send(self, event: AlertEvent) -> DeliveryResult:
            sent_targets.append((event.device_id, list(self.cfg.recipients)))
            return DeliveryResult(ok=True)

    dispatcher = NotificationDispatcher(
        configs,
        nstore,
        channel_factory=lambda cfg, _s: CaptureChannel(cfg),
    )

    # 1. worker-xue 配置了专属邮箱且开启抄送全局 -> 发给 xue + global
    assert dispatcher.deliver_now(
        AlertEvent(
            dedupe_key="cb:xue-1",
            event_type="batch_circuit_breaker_tripped",
            title="xue 熔断",
            body="环境异常",
            device_id="worker-xue",
            device_name="xue",
        )
    ).ok is True
    assert sent_targets[-1] == ("worker-xue", ["xue@example.com", "global@example.com"])

    # 2. 关闭抄送全局 -> 仅发给 xue 专属邮箱
    configs.save_email_notification(
        _sample_email_payload(
            recipients=["global@example.com"],
            cc_global_on_worker_override=False,
        )
    )
    assert dispatcher.deliver_now(
        AlertEvent(
            dedupe_key="cb:xue-2",
            event_type="batch_circuit_breaker_tripped",
            title="xue 熔断 2",
            body="环境异常",
            device_id="worker-xue",
            device_name="xue",
        )
    ).ok is True
    assert sent_targets[-1] == ("worker-xue", ["xue@example.com"])

    # 3. worker-a 未配置专属邮箱 -> 自动回退全局收件人
    assert dispatcher.deliver_now(
        AlertEvent(
            dedupe_key="cb:a-1",
            event_type="batch_circuit_breaker_tripped",
            title="A机熔断",
            body="环境异常",
            device_id="worker-a",
            device_name="共享端A机",
        )
    ).ok is True
    assert sent_targets[-1] == ("worker-a", ["global@example.com"])

    # 4. 投递记录隔离：Hub 看全部 (3 条)，各 Worker 仅看自己
    assert len(nstore.recent(channel="email")) == 3
    xue_logs = nstore.recent(channel="email", device_id="worker-xue")
    a_logs = nstore.recent(channel="email", device_id="worker-a")
    assert len(xue_logs) == 2 and all(item["device_id"] == "worker-xue" for item in xue_logs)
    assert len(a_logs) == 1 and a_logs[0]["device_id"] == "worker-a"
