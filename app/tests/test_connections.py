from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from menu_capture.config import ModelConfig
from menu_capture.connections import ConnectionManager
from menu_capture.server import create_app


SECRET = "test-secret-must-not-be-persisted"
BASE_URL = "https://model.invalid/v1"


class FakeRunner:
    def __init__(self) -> None:
        self.running = False


class FakeConfigs:
    def load(self):
        return SimpleNamespace(model=ModelConfig(base_url=BASE_URL, model="vision-test"))

    @staticmethod
    def key() -> str:
        return SECRET


def check(check_id: str, status: str = "ready", summary: str = "可用") -> dict:
    return {
        "id": check_id,
        "name": check_id,
        "status": status,
        "summary": summary,
        "detail": "",
        "checked_at": "2026-11-03T10:00:00+08:00",
        "actions": [],
    }


class FakeEnvironment:
    """隔离真实模型、macOS 权限探测和 UiPath 进程的连接管理器替身。"""

    def __init__(self, *, preflight_ready: bool = True, model_configured: bool = True) -> None:
        self.runner = FakeRunner()
        self.configs = FakeConfigs()
        self._lock = threading.RLock()
        self._tests: dict[str, dict] = {}
        self.preflight_ready = preflight_ready
        self.model_configured = model_configured
        self.model_results: list[dict] = []
        self.model_calls = 0
        self.desktop_calls = 0
        self.raise_on_check: Exception | None = None

    def _model_configured(self) -> bool:
        return self.model_configured

    def invalidate(self, test_id: str | None = None) -> None:
        if test_id:
            self._tests.pop(test_id, None)
        else:
            self._tests.clear()

    def check(self, **_kwargs) -> dict:
        if self.raise_on_check:
            raise self.raise_on_check
        return self.snapshot()

    def run_test(self, test_id: str) -> dict:
        if test_id == "model_service":
            self.model_calls += 1
            result = self.model_results.pop(0) if self.model_results else {
                "status": "ready", "summary": "模型已连接", "detail": "fake result"
            }
            self._tests["model_service"] = {
                "at": time.monotonic(),
                "checked_at": "2026-11-03T10:00:00+08:00",
                "model_service": dict(result),
            }
        elif test_id == "uipath_desktop":
            self.desktop_calls += 1
        else:
            raise AssertionError(f"Unexpected test requested: {test_id}")
        return self.snapshot()

    def snapshot(self, **_kwargs) -> dict:
        model = self._tests.get("model_service", {}).get("model_service")
        checks = [
            check("wechat_process"), check("wechat_login"),
            check("uipath_cli"), check("uipath_project"),
            check("uipath_accessibility"), check("uipath_screenshot"),
            check("uipath_system_accessibility"),
            check("model_service", model["status"], model["summary"]) if model else
            check("model_service", "unknown", "模型尚未测试"),
            check("input_directory", "ready" if self.preflight_ready else "error", "素材目录"),
            check("output_directory", "ready" if self.preflight_ready else "error", "输出目录"),
            check("capture_profile", "ready" if self.preflight_ready else "error", "任务配置"),
        ]
        blockers = [] if self.preflight_ready else ["素材目录：不可读", "任务配置：未就绪"]
        return {
            "checked_at": "2026-11-03T10:00:00+08:00",
            "checks": checks,
            "preflight": {"ready": self.preflight_ready, "blockers": blockers},
        }

    def assert_preflight(self) -> dict:
        state = self.snapshot()
        if not state["preflight"]["ready"]:
            raise RuntimeError("运行前检查未通过：" + "；".join(state["preflight"]["blockers"]))
        return state


def make_manager(tmp_path: Path, **kwargs) -> tuple[ConnectionManager, FakeEnvironment]:
    environment = FakeEnvironment(**kwargs)
    manager = ConnectionManager(environment, threading.RLock(), tmp_path)
    return manager, environment


def wait_for_worker(manager: ConnectionManager) -> None:
    worker = manager._worker
    assert worker is not None
    worker.join(timeout=5)
    assert not worker.is_alive(), "connection worker did not finish"


def test_connect_is_single_flight_and_does_not_touch_running_capture(tmp_path: Path, monkeypatch) -> None:
    manager, environment = make_manager(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_connect() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=3)

    monkeypatch.setattr(manager, "_connect", blocked_connect)
    manager.connect()
    assert entered.wait(timeout=2)
    worker = manager._worker
    manager.connect(force=True)
    assert manager._worker is worker
    assert calls == 1
    release.set()
    wait_for_worker(manager)

    environment.runner.running = True
    previous_worker = manager._worker
    manager.connect(force=True)
    assert manager._worker is previous_worker
    assert environment.runner.running is True
    assert calls == 1


def test_force_reconnect_reuses_recent_successful_model_test(tmp_path: Path) -> None:
    manager, environment = make_manager(tmp_path)
    environment._tests["model_service"] = {
        "at": time.monotonic(),
        "checked_at": "2026-11-03T10:00:00+08:00",
        "model_service": {"status": "ready", "summary": "模型已连接", "detail": "cached"},
    }

    manager.connect(force=True)
    wait_for_worker(manager)

    assert environment.model_calls == 0
    assert environment.desktop_calls == 1


@pytest.mark.parametrize(
    ("results", "expected_calls"),
    [
        ([{"status": "error", "summary": "暂时不可达", "transient": True}] * 3, 3),
        ([{"status": "error", "summary": "认证失败", "transient": False}] * 3, 1),
    ],
)
def test_model_retries_only_temporary_failures(
    tmp_path: Path, monkeypatch, results: list[dict], expected_calls: int
) -> None:
    manager, environment = make_manager(tmp_path)
    environment.model_results = list(results)
    monkeypatch.setattr(manager._stop, "wait", lambda _seconds: False)

    manager._connect()

    assert environment.model_calls == expected_calls


def test_three_transient_failures_cool_down_until_force_reconnect(tmp_path: Path, monkeypatch) -> None:
    manager, environment = make_manager(tmp_path)
    failure = {"status": "error", "summary": "暂时不可达", "transient": True}
    environment.model_results = [dict(failure) for _ in range(6)]
    monkeypatch.setattr(manager._stop, "wait", lambda _seconds: False)

    manager._connect()
    assert environment.model_calls == 3

    manager.connect()
    assert manager._worker is None
    assert environment.model_calls == 3

    manager.connect(force=True)
    wait_for_worker(manager)
    assert environment.model_calls == 6


def test_system_permission_ready_with_runtime_error_reports_connection_failure(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    checks = [
        check("wechat_process"), check("wechat_login"), check("uipath_cli"), check("uipath_project"),
        check("uipath_accessibility", "error", "运行时无法访问"), check("uipath_screenshot"),
        check("uipath_system_accessibility"), check("model_service"),
    ]

    engine = next(module for module in manager._modules({"checks": checks, "checked_at": "now"}, checking=False)
                  if module["id"] == "engine")

    assert engine["status"] == "error"
    assert "执行连接失败" in engine["summary"]
    assert "需要授权" not in engine["summary"]


def test_module_readiness_is_separate_from_task_preflight(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, preflight_ready=False)
    model_ready = {
        "at": time.monotonic(), "checked_at": "2026-11-03T10:00:00+08:00",
        "model_service": {"status": "ready", "summary": "模型已连接", "detail": "cached"},
    }
    manager.environment._tests["model_service"] = model_ready

    snapshot = manager.snapshot()

    assert snapshot["status"] == "ready"
    assert [module["status"] for module in snapshot["modules"]] == ["ready", "ready", "ready"]
    assert snapshot["preflight"] == {"ready": True, "blockers": []}
    assert manager.environment.snapshot()["preflight"] == {
        "ready": False, "blockers": ["素材目录：不可读", "任务配置：未就绪"]
    }
    with pytest.raises(RuntimeError, match="运行前检查未通过"):
        manager.environment.assert_preflight()


def test_persisted_model_cache_restores_without_secret_and_avoids_retest(tmp_path: Path) -> None:
    manager, environment = make_manager(tmp_path)
    environment._tests["model_service"] = {
        "at": time.monotonic(),
        "checked_at": "2026-11-03T10:00:00+08:00",
        "model_service": {"status": "ready", "summary": "模型已连接", "detail": "cached"},
    }
    manager._persist()
    payload = manager.path.read_text(encoding="utf-8")
    persisted = json.loads(payload)

    assert SECRET not in payload
    assert BASE_URL not in payload
    assert len(persisted["model"]["fingerprint"]) == 64
    assert persisted["model"]["fingerprint"] == hashlib.sha256(
        (json.dumps(environment.configs.load().model.model_dump(), sort_keys=True) + SECRET).encode()
    ).hexdigest()

    restored_environment = FakeEnvironment()
    restored = ConnectionManager(restored_environment, threading.RLock(), tmp_path)
    assert restored_environment._tests["model_service"]["model_service"]["status"] == "ready"
    restored.connect(force=True)
    wait_for_worker(restored)

    assert restored_environment.model_calls == 0


def test_background_connection_exception_is_visible_in_diagnostics(tmp_path: Path) -> None:
    manager, environment = make_manager(tmp_path)
    environment.raise_on_check = RuntimeError("fake failure with private detail")

    manager.connect(force=True)
    wait_for_worker(manager)
    snapshot = manager.snapshot()

    assert manager.busy is False
    assert snapshot["diagnostics"]["stage"] == "连接失败"
    event = snapshot["diagnostics"]["events"][-1]
    assert event["status"] == "error"
    assert "RuntimeError" in event["message"]
    assert "private detail" not in event["message"]


def test_start_endpoint_blocks_on_real_environment_preflight(tmp_path: Path, monkeypatch) -> None:
    app = create_app(tmp_path)

    def blocked_snapshot(*, refresh: bool = False) -> dict:
        return {"preflight": {"ready": False, "blockers": ["输出目录：不可写"]}}

    monkeypatch.setattr(app.state.environment, "snapshot", blocked_snapshot)
    start = Mock()
    monkeypatch.setattr(app.state.runner, "start", start)
    client = TestClient(app, base_url="http://127.0.0.1")

    response = client.post("/api/start", headers={"X-Capture-Token": app.state.token})

    assert response.status_code == 409
    assert "输出目录：不可写" in response.json()["detail"]
    start.assert_not_called()


def test_config_directory_change_preserves_model_cache_and_does_not_connect(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(tmp_path)
    app.state.configs.save({
        "input_dir": str(tmp_path / "inbox-before"),
        "output_dir": str(tmp_path / "output-before"),
        "model": {
            "provider": "openai_compatible",
            "base_url": BASE_URL,
            "model": "vision-test",
            "api_key": SECRET,
        },
    })
    model_result = {
        "at": time.monotonic(),
        "checked_at": "2026-11-03T10:00:00+08:00",
        "model_service": {"status": "ready", "summary": "模型已连接", "detail": "cached"},
    }
    app.state.environment._tests["model_service"] = model_result
    previous_fingerprint = app.state.connections._fingerprint()
    connect = Mock(side_effect=AssertionError("config save must not launch connection worker"))
    monkeypatch.setattr(app.state.connections, "connect", connect)

    payload = app.state.configs.public()
    payload["input_dir"] = str(tmp_path / "inbox-after")
    payload["output_dir"] = str(tmp_path / "output-after")
    response = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/config", json=payload, headers={"X-Capture-Token": app.state.token}
    )

    assert response.status_code == 200
    assert app.state.connections._fingerprint() == previous_fingerprint
    assert app.state.environment._tests["model_service"] is model_result
    assert app.state.configs.key() == SECRET
    connect.assert_not_called()
