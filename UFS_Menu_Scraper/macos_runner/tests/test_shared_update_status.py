"""多设备集中更新：Worker 上报软件状态、Hub 汇总分发、Worker 采纳元数据。"""

from __future__ import annotations

import threading
import time

import pytest
from menu_capture.config import ConfigStore
from menu_capture.materials import MaterialService
from menu_capture.shared import SharedHub
from menu_capture.shared_worker import SharedWorker
from menu_capture.store import TaskStore
from menu_capture.updates import UpdateService

SOFTWARE = {
    "current_version": "0.2.2",
    "platform": "windows",
    "latest_version": "0.2.3",
    "update_available": True,
    "channel": "stable",
    "checked_at": "2026-11-24T01:00:00+00:00",
    "download_status": "completed",
    "install_status": "idle",
}


def make_hub(tmp_path) -> tuple[SharedHub, dict]:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    configs = ConfigStore(tmp_path / "config")
    configs.root.mkdir(parents=True, exist_ok=True)
    hub = SharedHub(store, MaterialService(tmp_path / "materials", configs, store))
    worker = hub.pair(hub.pairing_code()["code"], "设计电脑")
    return hub, worker


def test_hub_collects_worker_software_and_summarizes(tmp_path):
    hub, worker = make_hub(tmp_path)

    result = hub.sync(worker["id"], [], 0, software=SOFTWARE)
    assert result["supports_software_status"] is True
    assert result["update_target"] is None

    status = hub.status()
    row = next(item for item in status["workers"] if item["id"] == worker["id"])
    assert row["software"]["current_version"] == "0.2.2"
    assert row["software"]["update_available"] is True
    assert status["update_summary"]["reported"] == 1
    assert status["update_summary"]["update_available"] == 1
    assert status["update_summary"]["versions"] == {"0.2.2": 1}

    # 未上报的设备明确为 null，而不是空对象。
    other = hub.pair(hub.pairing_code()["code"], "另一台")
    rows = {item["id"]: item for item in hub.status()["workers"]}
    assert rows[other["id"]]["software"] is None
    assert hub.status()["update_summary"]["unreported"] == 1


def test_hub_distributes_fresh_target_but_not_stale(tmp_path):
    hub, worker = make_hub(tmp_path)
    latest = {
        "version": "0.2.3", "tag": "v0.2.3", "channel": "stable", "eligible": True,
        "assets": [], "min_supported_version": "",
    }
    hub.update_metadata = lambda: {"channel": "stable", "checked_at": time.time(), "latest": latest}
    result = hub.sync(worker["id"], [], 0, software=SOFTWARE)
    assert result["update_target"]["latest"]["version"] == "0.2.3"
    assert hub.status()["update_summary"]["target_version"] == "0.2.3"

    # 过期判断由元数据提供者（UpdateService.release_target）负责；不可用时同步不再分发。
    hub.update_metadata = lambda: None
    assert hub.sync(worker["id"], [], 0)["update_target"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {**SOFTWARE, "path": "C:/secret"},
        {**SOFTWARE, "update_available": "yes"},
        {**SOFTWARE, "current_version": "0.2.2\n0.2.3"},
    ],
)
def test_hub_rejects_invalid_software_report(tmp_path, payload):
    hub, worker = make_hub(tmp_path)
    with pytest.raises(ValueError):
        hub.sync(worker["id"], [], 0, software=payload)


def _target(version: str = "0.2.3", *, checked_at: float | None = None, channel: str = "stable") -> dict:
    prerelease = "-" in version
    return {
        "channel": channel,
        "checked_at": time.time() if checked_at is None else checked_at,
        "latest": {
            "version": version,
            "tag": f"v{version}",
            "channel": channel,
            "eligible": True,
            "min_supported_version": "",
            "assets": [{
                "kind": "windows_installer",
                "name": f"MenuCapture-{version}.exe",
                "size": 1024,
                "sha256": "a" * 64,
                "url": (
                    "https://github.com/zhaoyongze123/wechat-miniapp-menu-agent"
                    f"/releases/download/v{version}/MenuCapture-{version}.exe"
                ),
                "platform": "windows",
                "arch": "x64",
            }],
            "prerelease": prerelease,
        },
    }


def test_worker_adopts_fresh_hub_target_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("MENU_CAPTURE_VERSION", "0.2.2")

    def fail(*args, **kwargs):
        raise AssertionError("采纳分发结果时不应请求 GitHub")

    service = UpdateService(tmp_path, role="worker", fetcher=fail, platform="windows", machine="AMD64")
    service.hub_target_provider = lambda: _target()

    status = service.check(force=True)

    assert status["latest"]["version"] == "0.2.3"
    assert status["source"] == "hub"
    assert status["update_available"] is True


def test_worker_falls_back_when_hub_target_stale_or_channel_mismatch(tmp_path):
    calls: list[str] = []

    def fetcher(url, headers, timeout):
        calls.append(url)
        return 200, {}, b"[]"

    service = UpdateService(tmp_path, role="worker", fetcher=fetcher, platform="windows", machine="AMD64")
    service.hub_target_provider = lambda: _target(checked_at=time.time() - 7200)
    assert service.check(force=True)["source"] == "github"

    service.hub_target_provider = lambda: _target("0.3.0-beta.1", channel="preview")
    assert service.check(force=True)["source"] == "github"
    assert len(calls) == 2


def test_worker_does_not_adopt_prerelease_on_stable_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("MENU_CAPTURE_VERSION", "0.2.2")
    calls: list[str] = []

    def fetcher(url, headers, timeout):
        calls.append(url)
        return 200, {}, b"[]"

    service = UpdateService(tmp_path, role="worker", fetcher=fetcher, platform="windows", machine="AMD64")
    service.save_preferences({"channel": "stable", "auto_check": True})
    service.hub_target_provider = lambda: _target("0.3.0-beta.1", channel="stable")

    # 预发布分发结果被拒绝，回退本机检查；不会把 beta 写进稳定渠道缓存。
    status = service.check(force=True)
    assert calls and status["source"] == "github"
    assert status["latest"] is None


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.payloads: list[dict] = []

    def post(self, url, headers=None, json=None):
        self.payloads.append(dict(json or {}))
        return _FakeResponse(self.payload)


def test_worker_reports_software_only_after_hub_advertises_support(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    client = _FakeClient({
        "assignments": [], "states": {}, "supports_run_summary": True,
        "supports_software_status": True, "update_target": _target(),
    })
    worker = SharedWorker(tmp_path, store, threading.Lock(), threading.Lock(), client=client)
    worker.connection = {"url": "https://hub.example", "token": "t"}
    worker.software_provider = lambda: dict(SOFTWARE)

    # 首次同步不知道 Hub 能力，先不夹带 software；响应确认支持后下次上报。
    worker._heartbeat()
    assert "software" not in client.payloads[0]
    assert worker.hub_update_target["latest"]["version"] == "0.2.3"

    worker._heartbeat()
    assert client.payloads[1]["software"]["current_version"] == "0.2.2"
