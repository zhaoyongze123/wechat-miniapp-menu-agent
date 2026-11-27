from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import httpx

from menu_capture.shared.shared import worker_connection_state
from menu_capture.shared.shared_worker import OFFLINE_FAILURE_THRESHOLD, SharedWorker
from menu_capture.storage.store import TaskStore


def test_worker_connection_state_thresholds():
    now = datetime(2026, 11, 27, 10, 0, 0, tzinfo=timezone.utc)
    assert worker_connection_state((now - timedelta(seconds=10)).isoformat(), now=now) == ("online", 10)
    assert worker_connection_state((now - timedelta(seconds=30)).isoformat(), now=now) == ("online", 30)
    assert worker_connection_state((now - timedelta(seconds=45)).isoformat(), now=now) == ("degraded", 45)
    assert worker_connection_state((now - timedelta(seconds=90)).isoformat(), now=now) == ("degraded", 90)
    assert worker_connection_state((now - timedelta(seconds=91)).isoformat(), now=now) == ("offline", 91)
    assert worker_connection_state("", now=now) == ("offline", None)
    assert worker_connection_state("invalid", now=now) == ("offline", None)


class _FlakyClient:
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ConnectTimeout("timeout")
        return _OkResponse({
            "assignments": [],
            "worker_name": "测试机",
            "supports_run_summary": False,
            "supports_software_status": False,
            "supports_run_history": False,
            "supports_project_scope": False,
        })

    def close(self):
        pass


class _OkResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_shared_worker_hysteresis_degraded_to_offline_and_recover(tmp_path):
    store = TaskStore(tmp_path / "data" / "tasks.sqlite3")
    client = _FlakyClient(fail_times=OFFLINE_FAILURE_THRESHOLD)
    worker = SharedWorker(tmp_path, store, threading.Lock(), threading.Lock(), client=client)
    worker.connection = {"url": "http://127.0.0.1:8766", "token": "tok", "id": "w1", "name": "测试机"}

    # 前两次网络抖动记为 degraded，避免单次瞬断误报离线。
    worker.poll()
    assert worker.status()["connection_state"] == "degraded"
    assert worker.status()["consecutive_failures"] == 1

    worker.poll()
    assert worker.status()["connection_state"] == "degraded"
    assert worker.status()["consecutive_failures"] == 2

    # 达到连续失败阈值后确认离线。
    worker.poll()
    assert worker.status()["connection_state"] == "offline"
    assert worker.status()["consecutive_failures"] == OFFLINE_FAILURE_THRESHOLD

    # 恢复连通后单次成功即回到 online 并清零连续失败计数。
    worker.poll()
    status = worker.status()
    assert status["online"] is True
    assert status["connection_state"] == "online"
    assert status["consecutive_failures"] == 0
    assert status["last_connected_at"]
