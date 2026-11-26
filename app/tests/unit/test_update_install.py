"""Velopack 安装控制器：只验证业务安全边界与状态收尾，不模拟 SDK 内部行为。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from menu_capture.updates.update_install import InstallController, rollback_schema_matches, schema_fingerprint


class FakeBackend:
    def __init__(self, data_dir: Path, pending: dict | None = None):
        self.data_dir = data_dir
        self.pending = pending
        self.requests: list[str] = []
        self.cleared = 0
        self.apply_state: dict = {}

    def pending_restart(self):
        return dict(self.pending) if self.pending else None

    def request_apply(self, version: str) -> None:
        self.requests.append(version)

    def clear_apply_request(self) -> None:
        self.cleared += 1

    def read_apply_state(self) -> dict:
        return dict(self.apply_state)


def make_controller(tmp_path: Path, *, current: str = "0.3.4", idle: bool = True,
                    pending: dict | None = None, supported: bool = True):
    with closing(sqlite3.connect(tmp_path / "tasks.sqlite3")) as db:
        db.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY)")
        db.commit()
    backend = FakeBackend(tmp_path, pending or {"version": "0.4.0"})
    controller = InstallController(
        data_dir=tmp_path,
        backend=backend,
        latest_provider=lambda: {"eligible": True, "version": "0.4.0"},
        current_version=lambda: current,
        idle_check=lambda: (idle, "" if idle else "采集正在执行"),
        role="worker",
        platform="windows",
        supported=supported,
        authorization_check=lambda version: True,
        authorization_release=lambda version: None,
    )
    return controller, backend


def test_unsupported_outside_worker(tmp_path: Path):
    controller, _ = make_controller(tmp_path, supported=False)
    assert controller.supported is False
    with pytest.raises(ValueError, match="未启用"):
        controller.start("0.4.0", "0.4.0")


def test_start_requires_matching_confirmation_and_latest(tmp_path: Path):
    controller, _ = make_controller(tmp_path)
    with pytest.raises(ValueError, match="确认"):
        controller.start("0.4.0", "0.4.1")
    with pytest.raises(ValueError, match="最新版本"):
        controller.start("0.5.0", "0.5.0")


def test_start_requires_downloaded_package(tmp_path: Path):
    controller, backend = make_controller(tmp_path)
    backend.pending = None
    with pytest.raises(ValueError, match="尚未下载完成"):
        controller.start("0.4.0", "0.4.0")
    assert backend.requests == []


def test_start_requires_idle(tmp_path: Path):
    controller, backend = make_controller(tmp_path, idle=False)
    with pytest.raises(ValueError, match="采集正在执行"):
        controller.start("0.4.0", "0.4.0")
    assert backend.requests == []


def test_start_writes_restarting_and_apply_request(tmp_path: Path):
    controller, backend = make_controller(tmp_path)
    status = controller.start("0.4.0", "0.4.0")
    assert status["status"] == "restarting"
    assert status["target_version"] == "0.4.0"
    assert backend.requests == ["0.4.0"]
    journal = json.loads((tmp_path / "update-install.json").read_text(encoding="utf-8"))
    assert journal["previous_version"] == "0.3.4"
    assert (tmp_path / "update-schema-baseline.json").is_file()
    assert controller.active() is True


def test_refresh_marks_completed_when_target_is_running(tmp_path: Path):
    controller, backend = make_controller(tmp_path)
    controller.start("0.4.0", "0.4.0")
    controller.current_version = lambda: "0.4.0"
    assert controller.active() is False
    journal = json.loads((tmp_path / "update-install.json").read_text(encoding="utf-8"))
    assert journal["status"] == "completed"
    assert backend.cleared == 1


def test_refresh_marks_failed_when_launcher_reports_error(tmp_path: Path):
    controller, backend = make_controller(tmp_path)
    controller.start("0.4.0", "0.4.0")
    backend.apply_state = {"status": "failed", "message": "Worker 未正常退出", "error": "timeout"}
    assert controller.active() is False
    status = controller.status()
    assert status["status"] == "failed"
    assert "Worker 未正常退出" in status["message"]


def test_refresh_marks_stalled_after_timeout(tmp_path: Path):
    controller, _ = make_controller(tmp_path)
    controller.start("0.4.0", "0.4.0")
    journal = json.loads((tmp_path / "update-install.json").read_text(encoding="utf-8"))
    journal.update({"started_at": "2020-01-01T00:00:00+00:00", "updated_at": "2020-01-01T00:00:00+00:00"})
    (tmp_path / "update-install.json").write_text(json.dumps(journal), encoding="utf-8")
    assert controller.active() is False
    assert controller.status()["status"] == "failed"


def test_schema_missing_and_corrupt(tmp_path):
    assert schema_fingerprint(tmp_path) is None
    (tmp_path / "tasks.sqlite3").write_bytes(b"not a sqlite database")
    assert schema_fingerprint(tmp_path) is None
    assert not rollback_schema_matches(tmp_path, {"version": "0.3.4", "schema": None}, "0.3.4")


def test_schema_baseline_and_data(tmp_path):
    with closing(sqlite3.connect(tmp_path / "tasks.sqlite3")) as db:
        db.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        db.commit()
        before = schema_fingerprint(tmp_path)
        baseline = {"version": "0.3.4", "schema": json.loads(json.dumps(before))}
        db.execute("INSERT INTO tasks VALUES (1, 'test')")
        db.commit()
        assert rollback_schema_matches(tmp_path, baseline, "0.3.4")
        assert not rollback_schema_matches(tmp_path, baseline, "0.3.3")
        assert not rollback_schema_matches(tmp_path, {"version": "0.3.4", "schema": []}, "0.3.4")
        db.execute("CREATE INDEX by_name ON tasks(name)")
        db.commit()
        assert not rollback_schema_matches(tmp_path, baseline, "0.3.4")


def test_schema_version_change(tmp_path):
    with closing(sqlite3.connect(tmp_path / "tasks.sqlite3")) as db:
        db.execute("CREATE TABLE tasks (id INTEGER)")
        db.commit()
        before = schema_fingerprint(tmp_path)
        db.execute("PRAGMA user_version = 2")
        db.commit()
        assert before != schema_fingerprint(tmp_path)


def test_schema_empty_database(tmp_path):
    with closing(sqlite3.connect(tmp_path / "tasks.sqlite3")):
        pass
    assert schema_fingerprint(tmp_path) is None


def test_schema_uri_reserved_characters(tmp_path):
    root = tmp_path / "data # test"
    root.mkdir()
    with closing(sqlite3.connect(root / "tasks.sqlite3")) as db:
        db.execute("CREATE TABLE tasks (id INTEGER)")
        db.commit()
    assert schema_fingerprint(root) is not None


def test_start_rejects_invalid_schema_before_request(tmp_path):
    controller, backend = make_controller(tmp_path)
    (tmp_path / "tasks.sqlite3").write_bytes(b"broken")
    with pytest.raises(ValueError, match="无法读取有效数据库结构"):
        controller.start("0.4.0", "0.4.0")
    assert backend.requests == []
    assert not controller.active()
