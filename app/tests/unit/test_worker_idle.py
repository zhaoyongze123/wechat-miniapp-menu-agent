from __future__ import annotations

from fastapi.testclient import TestClient

from menu_capture.api.server import create_app


def test_worker_idle_probe_reads_only_execution_flags(monkeypatch, tmp_path):
    app = create_app(tmp_path, role="worker")
    client = TestClient(app, base_url="http://127.0.0.1")

    def unexpected_history_read():
        raise AssertionError("轻量空闲探针不应读取任务历史")

    monkeypatch.setattr(app.state.store, "tasks", unexpected_history_read)
    response = client.get("/api/worker/idle")
    assert response.status_code == 200
    assert response.json() == {
        "running": False,
        "annotation_active": False,
        "idle": True,
    }


def test_worker_idle_probe_is_false_while_capture_or_annotation_is_active(monkeypatch, tmp_path):
    app = create_app(tmp_path, role="worker")
    client = TestClient(app, base_url="http://127.0.0.1")

    class ActiveThread:
        @staticmethod
        def is_alive():
            return True

    monkeypatch.setattr(app.state.runner, "thread", ActiveThread())
    assert client.get("/api/worker/idle").json() == {
        "running": True,
        "annotation_active": False,
        "idle": False,
    }

    monkeypatch.setattr(app.state.runner, "thread", None)
    app.state.annotations.session_id = "test-session"
    app.state.annotations.status = "running"
    assert client.get("/api/worker/idle").json() == {
        "running": False,
        "annotation_active": True,
        "idle": False,
    }


def test_worker_idle_probe_is_not_registered_for_standalone(tmp_path):
    app = create_app(tmp_path, role="standalone")
    client = TestClient(app, base_url="http://127.0.0.1")

    assert client.get("/api/worker/idle").status_code == 404
