from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.server import create_app
from menu_capture.store import TaskStore


class FakeDesktop:
    def permissions(self):
        return {"accessibility": True, "screen_recording": True}

    def windows(self):
        return []

    def stop_requested(self):
        return False


def config_payload(root: Path, key: str) -> dict:
    return {
        "input_dir": str(root / "inbox"),
        "output_dir": str(root / "output"),
        "model": {
            "provider": "openai_compatible",
            "base_url": "https://vision.example/v1",
            "model": "vision-test",
            "api_key": key,
        },
    }


def test_restart_recovers_interrupted_task_without_replaying_actions(tmp_path: Path) -> None:
    source = tmp_path / "inbox" / "Cafe" / "qr.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), "white").save(source)
    store = TaskStore(tmp_path / "data/tasks.sqlite3")
    store.scan(tmp_path / "inbox")
    task = store.tasks()[0]
    store.update(task["id"], status="running", stage="clicking")
    app = create_app(tmp_path)
    assert app.state.store.get(task["id"])["status"] == "needs_review"
    assert not app.state.runner.running
    assert app.state.runner.run_directory is None


def test_current_material_root_excludes_original_photo_tasks(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    for name in ("inbox", "old-originals"):
        image = tmp_path / name / "Cafe" / "qr.png"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), "white").save(image)
        app.state.store.scan(tmp_path / name)
    app.state.configs.save(config_payload(tmp_path, "test-key"))
    client = TestClient(app, base_url="http://127.0.0.1")
    tasks = client.get("/api/status").json()["tasks"]
    assert len(tasks) == 1
    assert "old-originals" not in tasks[0]["source_path"]
    assert len(app.state.store.tasks()) == 2  # 历史仍保留在唯一状态库中。


def test_config_endpoint_requires_token_and_never_returns_api_key(tmp_path: Path) -> None:
    app = create_app(tmp_path, desktop=FakeDesktop())
    client = TestClient(app, base_url="http://127.0.0.1")
    secret = "do-not-return-this-secret"

    unauthorized = client.post("/api/config", json=config_payload(tmp_path, secret))
    assert unauthorized.status_code == 403
    assert secret not in unauthorized.text

    csrf = client.post(
        "/api/config",
        json=config_payload(tmp_path, secret),
        headers={"X-Capture-Token": app.state.token, "Origin": "http://evil.example"},
    )
    assert csrf.status_code == 403
    assert secret not in csrf.text

    saved = client.post(
        "/api/config",
        json=config_payload(tmp_path, secret),
        headers={"X-Capture-Token": app.state.token, "Origin": "http://127.0.0.1"},
    )
    assert saved.status_code == 200
    assert secret not in saved.text
    assert saved.json()["model"]["api_key_configured"] is True

    public = client.get("/api/config")
    assert public.status_code == 200
    assert secret not in public.text
    assert public.json()["model"]["api_key_configured"] is True


def test_artifact_endpoint_rejects_traversal_outside_task_output(tmp_path: Path) -> None:
    app = create_app(tmp_path, desktop=FakeDesktop())
    client = TestClient(app, base_url="http://127.0.0.1")
    inbox = tmp_path / "inbox"
    source = inbox / "Cafe" / "menu.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), "white").save(source)
    task = app.state.store
    task.scan(inbox)
    record = task.tasks()[0]
    output = tmp_path / "output" / record["id"]
    output.mkdir(parents=True)
    task.update(record["id"], output_dir=str(output))
    outside = tmp_path / "outside-secret.json"
    outside.write_text('{"private":"outside"}', encoding="utf-8")

    traversal = quote("../outside-secret.json", safe="")
    response = client.get(f"/api/artifact/{record['id']}/{traversal}")
    assert response.status_code == 404
    assert "outside" not in response.text

    link = output / "linked.json"
    link.symlink_to(outside)
    symlink_response = client.get(f"/api/artifact/{record['id']}/linked.json")
    assert symlink_response.status_code == 404
    assert "outside" not in symlink_response.text
