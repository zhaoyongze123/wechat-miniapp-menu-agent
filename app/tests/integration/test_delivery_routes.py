import base64
from pathlib import Path

from fastapi.testclient import TestClient

from menu_capture.api.server import create_app

ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}


def test_delivery_routes_require_hub_and_honor_configuration(tmp_path: Path, monkeypatch):
    # 采集端不具备合格成品目录职责，路由必须直接拒绝而不是返回空列表。
    worker = create_app(tmp_path / "worker", role="worker")
    worker_client = TestClient(worker, base_url="http://127.0.0.1")
    assert worker_client.get("/api/delivery/status").status_code == 404

    # 未配置宿主机挂载时只读降级：不创建目录，也不提供假的开目录能力。
    bare = create_app(tmp_path / "hub-bare", role="hub", admin_password="isolated-test")
    bare_client = TestClient(bare, base_url="http://127.0.0.1", headers=ADMIN)
    bare_status = bare_client.get("/api/delivery/status").json()
    assert bare_status["configured"] is False and bare_status["state"] == "unconfigured"
    reveal = bare_client.post("/api/delivery/reveal", headers={"x-capture-token": bare.state.token})
    assert reveal.status_code == 409

    mount = tmp_path / "host"
    mount.mkdir()
    monkeypatch.setenv("MENU_CAPTURE_DELIVERY_ROOT", str(mount / "合格"))
    monkeypatch.setenv("MENU_CAPTURE_DELIVERY_HOST_PATH", "D:/MenuCapture/合格")
    app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    client = TestClient(app, base_url="http://127.0.0.1", headers=ADMIN)

    status = client.get("/api/delivery/status").json()
    assert status["configured"] is True
    assert status["host_path"] == "D:/MenuCapture/合格"
    assert status["state"] == "initializing"

    stores = client.get("/api/delivery/stores").json()
    assert stores["total"] == 0 and stores["items"] == []
