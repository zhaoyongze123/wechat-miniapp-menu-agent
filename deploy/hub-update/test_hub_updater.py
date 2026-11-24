"""Hub 宿主机更新执行器单测：只覆盖契约与事务编排，真实 docker 由 CI 验收。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import hub_updater
import pytest
from hub_updater import (
    Config,
    HubUpdater,
    UpdateError,
    _client_allowed,
    validate_payload,
)

TOKEN = "t" * 32


def make_config(tmp_path: Path) -> Config:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    return Config({
        "HUB_UPDATE_TOKEN": TOKEN,
        "HUB_UPDATE_COMPOSE_FILE": str(compose),
        "HUB_UPDATE_PROJECT": "menu-capture",
        "HUB_UPDATE_STATE_DIR": str(tmp_path / "state"),
        "HUB_UPDATE_ARCHIVE_DIR": str(tmp_path / "archives"),
        "HUB_UPDATE_DATA_VOLUME": "menu-capture_hub-data",
        "HUB_UPDATE_HEALTH_TIMEOUT": "1",
    })


def archive_request(tmp_path: Path, version: str = "0.2.4", content: bytes = b"image-bytes") -> dict:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = f"hub-image-{version}.tar.gz"
    (archive_dir / name).write_bytes(content)
    return {
        "version": version,
        "confirm": version,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "archive": name,
    }


class FakeDocker:
    def __init__(self, current: str = "menu-capture-hub:0.2.3", loaded: str = "menu-capture-hub:0.2.4") -> None:
        self.current = current
        self.loaded = loaded
        self.fail_switch = False
        self.switched: list[str] = []
        self.fail_load = False

    def run(self, args: list[str], timeout: float) -> tuple[int, str]:
        command = args[:2]
        if command == ["docker", "volume"]:
            return 0, "[]"
        if command == ["docker", "load"]:
            if self.fail_load:
                return 1, "invalid archive"
            return 0, f"Loaded image: {self.loaded}"
        if command == ["docker", "compose"] and "ps" in args:
            return 0, "cid-1"
        if command == ["docker", "inspect"]:
            if "Health" in args[3]:
                return 0, "healthy"
            return 0, self.current
        if command == ["docker", "image"]:
            return 0, "[]"
        raise AssertionError(f"unexpected command: {args}")

    def run_env(self, args: list[str], timeout: float, env: dict[str, str]) -> tuple[int, str]:
        if self.fail_switch:
            return 1, "compose failed"
        self.current = env["MENU_CAPTURE_IMAGE"]
        self.switched.append(self.current)
        return 0, ""


def make_updater(tmp_path: Path, docker: FakeDocker, *, healthy: Callable[[str], dict]) -> HubUpdater:
    return HubUpdater(make_config(tmp_path), run=docker.run, run_env=docker.run_env,
                      health=healthy, sleep=lambda _seconds: None)


def test_apply_switches_and_records_previous(tmp_path: Path) -> None:
    docker = FakeDocker()
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})

    status = updater.apply(archive_request(tmp_path))

    assert status["status"] == "completed"
    assert status["target_version"] == "0.2.4"
    assert status["previous_version"] == "0.2.3"
    assert docker.switched == ["menu-capture-hub:0.2.4"]
    assert (tmp_path / "state" / "hub-update-audit.jsonl").is_file()


def test_apply_health_failure_rolls_back(tmp_path: Path) -> None:
    docker = FakeDocker()
    calls = {"count": 0}

    def healthy(version: str) -> dict:
        calls["count"] += 1
        if version == "0.2.4":
            return {"ok": False, "version": "", "detail": "版本接口不可用"}
        return {"ok": True, "version": version}

    status = make_updater(tmp_path, docker, healthy=healthy).apply(archive_request(tmp_path))

    assert status["status"] == "rolled_back"
    assert docker.switched == ["menu-capture-hub:0.2.4", "menu-capture-hub:0.2.3"]
    journal = updater_journal(tmp_path)
    assert "回滚" in journal["message"]
    assert journal["error"]


def test_apply_rejects_downgrade(tmp_path: Path) -> None:
    docker = FakeDocker(current="menu-capture-hub:0.2.5")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})

    with pytest.raises(UpdateError, match="禁止降级"):
        updater.apply(archive_request(tmp_path))
    assert docker.switched == []


def test_apply_rejects_checksum_mismatch(tmp_path: Path) -> None:
    request = archive_request(tmp_path)
    request["sha256"] = "0" * 64
    updater = make_updater(tmp_path, FakeDocker(), healthy=lambda version: {"ok": True, "version": version})

    with pytest.raises(UpdateError, match="SHA-256"):
        updater.apply(request)
    assert updater.journal()["status"] == "failed"
    assert updater.journal()["error"]


def test_load_must_contain_expected_tag(tmp_path: Path) -> None:
    docker = FakeDocker(loaded="menu-capture-hub:latest")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})

    with pytest.raises(UpdateError, match="归档未包含"):
        updater.apply(archive_request(tmp_path))
    assert docker.switched == []


def test_rollback_uses_previous_version(tmp_path: Path) -> None:
    docker = FakeDocker(current="menu-capture-hub:0.2.4")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})
    updater.journal_path.parent.mkdir(parents=True, exist_ok=True)
    hub_updater._write_json(updater.journal_path, {
        "status": "completed", "target_version": "0.2.4", "previous_version": "0.2.3", "pid": 0,
    })

    status = updater.rollback({"version": "0.2.3", "confirm": "0.2.3"})

    assert status["status"] == "rolled_back"
    assert docker.switched == ["menu-capture-hub:0.2.3"]


def test_rollback_rejects_unrecorded_version(tmp_path: Path) -> None:
    updater = make_updater(tmp_path, FakeDocker(), healthy=lambda version: {"ok": True, "version": version})

    with pytest.raises(UpdateError, match="没有可回滚"):
        updater.rollback({"version": "0.2.0", "confirm": "0.2.0"})


def test_active_transaction_blocks_second_apply(tmp_path: Path) -> None:
    updater = make_updater(tmp_path, FakeDocker(), healthy=lambda version: {"ok": True, "version": version})
    updater.journal_path.parent.mkdir(parents=True, exist_ok=True)
    hub_updater._write_json(updater.journal_path, {"status": "switching", "pid": os.getpid()})

    with pytest.raises(UpdateError, match="正在执行"):
        updater.apply(archive_request(tmp_path))


def test_stale_active_journal_is_resolved(tmp_path: Path) -> None:
    docker = FakeDocker(current="menu-capture-hub:0.2.4")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})
    updater.journal_path.parent.mkdir(parents=True, exist_ok=True)
    hub_updater._write_json(updater.journal_path, {
        "status": "switching", "target_version": "0.2.4", "pid": 999999,
    })

    status = updater.status()

    assert status["status"] == "completed"
    assert "0.2.4" in status["message"]


@pytest.mark.parametrize("payload", [
    {"version": "0.2", "confirm": "0.2", "sha256": "a" * 64, "size": 1, "archive": "a.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.3", "sha256": "a" * 64, "size": 1, "archive": "a.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 63, "size": 1, "archive": "a.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 0, "archive": "a.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1, "archive": "a.tar.gz", "url": "https://github.com/x"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1, "archive": "a.tar.gz", "extra": 1},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1,
     "url": "http://github.com/releases/download/v0.2.4/hub-image-0.2.4.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1,
     "url": "https://evil.example.com/releases/download/v0.2.4/hub-image-0.2.4.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1,
     "url": "https://github.com/owner/repo/archive/main.zip"},
])
def test_validate_payload_rejects_invalid(tmp_path: Path, payload: dict) -> None:
    with pytest.raises(UpdateError):
        validate_payload(payload, make_config(tmp_path))


def test_validate_accepts_release_url(tmp_path: Path) -> None:
    payload = {
        "version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 10,
        "url": "https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/releases/download/v0.2.4/hub-image-0.2.4.tar.gz",
    }
    assert validate_payload(payload, make_config(tmp_path))["version"] == "0.2.4"


def test_validate_rejects_archive_traversal(tmp_path: Path) -> None:
    payload = {
        "version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 10,
        "archive": "../hub-image-0.2.4.tar.gz",
    }
    with pytest.raises(UpdateError, match="文件名"):
        validate_payload(payload, make_config(tmp_path))


def test_client_allowlist() -> None:
    assert _client_allowed("127.0.0.1", ["127.0.0.1/32"])
    assert _client_allowed("172.18.0.1", ["172.16.0.0/12"])
    assert not _client_allowed("10.0.0.5", ["127.0.0.1/32"])
    assert not _client_allowed("not-an-ip", ["0.0.0.0/0"])


def updater_journal(tmp_path: Path) -> dict:
    import json

    return json.loads((tmp_path / "state" / "hub-update.json").read_text(encoding="utf-8"))
