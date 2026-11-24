"""Hub 更新代理：只验证与执行器的契约、状态映射与拒绝路径。"""

from __future__ import annotations

import time

import pytest

from menu_capture.shared.hub_update import HubInstallProxy

LATEST = {
    "version": "0.2.4",
    "assets": [
        {"kind": "windows_installer", "name": "MenuCapture-0.2.4.exe", "size": 10,
         "sha256": "b" * 64, "url": "https://github.com/x/releases/download/v0.2.4/a.exe"},
        {"kind": "hub_image", "name": "hub-image-0.2.4.tar.gz", "size": 1024,
         "sha256": "a" * 64, "url": "https://github.com/x/releases/download/v0.2.4/hub-image-0.2.4.tar.gz"},
    ],
}


class FakeUpdater:
    def __init__(self, status: dict | None = None, error: Exception | None = None) -> None:
        self.status_payload = status or {
            "supported": True, "active": False, "status": "idle",
            "target_version": "", "previous_version": "", "message": "", "error": "",
        }
        self.error = error
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, payload: dict | None, timeout: float) -> dict:
        self.calls.append((method, path, payload))
        if self.error:
            raise self.error
        if path == "/apply":
            # 模拟执行器：收到任务后把它记录为已完成，后续 /status 读到的就是终态。
            self.status_payload = {**self.status_payload, "status": "completed", "active": False,
                                   "target_version": (payload or {}).get("version", "")}
        return dict(self.status_payload)


def make_proxy(fake: FakeUpdater, **kwargs) -> HubInstallProxy:
    return HubInstallProxy(
        base_url="http://127.0.0.1:8767",
        token="t" * 32,
        latest_provider=lambda: LATEST,
        request=fake,
        **kwargs,
    )


def test_unconfigured_proxy_reports_reason_and_rejects() -> None:
    proxy = HubInstallProxy(base_url="", token="", latest_provider=lambda: LATEST)

    status = proxy.status()
    assert status["supported"] is False
    assert "HUB_UPDATE_URL" in status["reason"]
    assert proxy.active() is False
    with pytest.raises(ValueError, match="HUB_UPDATE_URL"):
        proxy.start("0.2.4", "0.2.4")


def test_status_maps_executor_journal_and_caches() -> None:
    fake = FakeUpdater({"supported": True, "active": True, "status": "switching",
                        "target_version": "0.2.4", "previous_version": "0.2.3",
                        "message": "正在切换", "error": ""})
    proxy = make_proxy(fake)

    first = proxy.status()
    second = proxy.status()

    assert first["active"] is True
    assert first["reason"] == ""
    assert first["status"] == "switching"
    assert len(fake.calls) == 1
    assert second["target_version"] == "0.2.4"


def test_unreachable_executor_degrades_without_blocking_collection() -> None:
    fake = FakeUpdater(error=OSError("connection refused"))
    proxy = make_proxy(fake)

    status = proxy.status()

    assert status["supported"] is True
    assert status["active"] is False
    assert "暂不可达" in status["reason"]


def test_start_requires_latest_version_and_confirm() -> None:
    fake = FakeUpdater()
    proxy = make_proxy(fake)

    with pytest.raises(ValueError, match="confirm"):
        proxy.start("0.2.4", "0.2.3")
    with pytest.raises(ValueError, match="不是当前最新版本"):
        proxy.start("0.2.2", "0.2.2")


def test_start_rejects_asset_without_checksum() -> None:
    latest = {"version": "0.2.4", "assets": [{"kind": "hub_image", "size": 1, "sha256": "", "url": "https://x"}]}
    fake = FakeUpdater()
    proxy = HubInstallProxy(base_url="http://127.0.0.1:8767", token="t" * 32,
                            latest_provider=lambda: latest, request=fake)

    with pytest.raises(ValueError, match="校验信息"):
        proxy.start("0.2.4", "0.2.4")


def test_start_submits_verified_asset_to_executor() -> None:
    fake = FakeUpdater()
    proxy = make_proxy(fake)

    status = proxy.start("0.2.4", "0.2.4")

    assert status["status"] == "starting"
    assert status["active"] is True
    # 提交线程是异步的，等它把 completed 写回缓存。
    for _ in range(50):
        if any(call[1] == "/apply" for call in fake.calls):
            break
        time.sleep(0.02)
    method, path, payload = next(call for call in fake.calls if call[1] == "/apply")
    assert method == "POST" and path == "/apply"
    assert payload == {
        "version": "0.2.4",
        "confirm": "0.2.4",
        "sha256": "a" * 64,
        "size": 1024,
        "url": "https://github.com/x/releases/download/v0.2.4/hub-image-0.2.4.tar.gz",
    }
    for _ in range(50):
        if proxy.status(force=True)["status"] == "completed":
            break
        time.sleep(0.02)
    assert proxy.status(force=True)["status"] == "completed"
