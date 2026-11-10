from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.capture_outputs import batch_output_dir
from menu_capture.server import create_app


def _image(path: Path, color: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 18), color).save(path, format="PNG")
    return path


def _snapshot(parent: Path, snapshot_id: str, color: str) -> Path:
    root = parent / snapshot_id
    root.mkdir(parents=True)
    image = _image(root / "同一家店" / "二维码.png", color)
    (root / ".materials-snapshot.json").write_text(json.dumps({"snapshot_id": snapshot_id}), encoding="utf-8")
    return root


def _page(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path, format="PNG")


def test_batch_output_directory_uses_batch_name_instead_of_id(tmp_path: Path):
    root = tmp_path / "output"
    assert batch_output_dir(root, "a" * 32, "0514二维码（去重）") == \
        root.resolve() / "清洗批次" / "0514二维码（去重）"
    # 查不到名称的历史批次仍回退到 ID，保证旧结果可继续访问。
    assert batch_output_dir(root, "a" * 32) == root.resolve() / "清洗批次" / ("a" * 32)


def test_store_output_dir_resolves_batch_name(tmp_path: Path):
    app = create_app(tmp_path)
    root = tmp_path / "input" / "同一家店"
    _image(root / "二维码.png", "red")
    app.state.store.scan(root.parent)
    task = app.state.store.tasks()[0]

    destination = app.state.runner._store_output_dir(tmp_path / "output", task, "f" * 32, "100张测试素材")

    assert destination.parent.parent == (tmp_path / "output").resolve() / "清洗批次"
    assert destination.parent.name == "100张测试素材"
    assert destination.name == "同一家店"


def test_same_store_in_separate_cleaning_batches_has_separate_task_and_output(tmp_path: Path):
    app = create_app(tmp_path)
    first_root = _snapshot(tmp_path / "published", "a" * 64, "red")
    second_root = _snapshot(tmp_path / "published", "b" * 64, "blue")

    assert app.state.store.scan(first_root)["added"] == 1
    assert app.state.store.scan(second_root)["added"] == 1
    first, second = app.state.store.tasks()
    assert first["id"] != second["id"]
    assert first["source_name"] == second["source_name"] == "同一家店"
    assert first["source_hash"] != second["source_hash"]

    runner = app.state.runner
    staging_one = runner._staging_dir(tmp_path / "output", first, "first")
    staging_two = runner._staging_dir(tmp_path / "output", second, "second")
    _page(staging_one / "page-0001.png", "white")
    _page(staging_two / "page-0001.png", "black")
    output_one = runner._publish_store_output(tmp_path / "output", first, staging_one, batch_id="1" * 32)
    output_two = runner._publish_store_output(tmp_path / "output", second, staging_two, batch_id="2" * 32)

    assert output_one != output_two
    assert (output_one / "page-0001.png").is_file()
    assert (output_two / "page-0001.png").is_file()


def test_artifact_api_does_not_serve_replaced_qr_version_output(tmp_path: Path):
    app = create_app(tmp_path)
    old_root = _snapshot(tmp_path / "published", "c" * 64, "red")
    new_root = _snapshot(tmp_path / "published", "d" * 64, "blue")
    app.state.store.scan(old_root)
    app.state.store.scan(new_root)
    old_task, new_task = app.state.store.tasks()
    staging_root = tmp_path / "output" / ".uipath-staging"
    old_staging = staging_root / old_task["id"] / "old"
    new_staging = staging_root / new_task["id"] / "new"
    _page(old_staging / "page-0001.png", "red")
    _page(new_staging / "page-0001.png", "blue")

    app.state.runner._publish_store_output(tmp_path / "output", old_task, old_staging, batch_id="e" * 32)
    app.state.runner._publish_store_output(tmp_path / "output", new_task, new_staging, batch_id="e" * 32)

    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get(f"/api/artifact/{old_task['id']}/page-0001.png").status_code == 404
    new_response = client.get(f"/api/artifact/{new_task['id']}/page-0001.png")
    assert new_response.status_code == 200
    assert new_response.content == (app.state.store.get(new_task["id"])["output_dir"] and
                                    (Path(app.state.store.get(new_task["id"])["output_dir"]) / "page-0001.png").read_bytes())


def test_failed_publish_retry_preserves_previous_output(tmp_path: Path):
    app = create_app(tmp_path)
    root = tmp_path / "input" / "同一家店"
    source = _image(root / "二维码.png", "red")
    app.state.store.scan(root.parent)
    task = app.state.store.tasks()[0]
    destination = app.state.runner._store_output_dir(tmp_path / "output", task, "f" * 32)
    _page(destination / "page-0001.png", "red")
    app.state.store.update(task["id"], status="captured", stage="captured", output_dir=str(destination))
    staging = tmp_path / "output" / ".uipath-staging" / task["id"] / "retry"
    _page(staging / "page-0001.png", "blue")

    real_replace = __import__("os").replace
    failed = False

    def fail_new_directory_once(src, dst):
        nonlocal failed
        if not failed and Path(src) == staging and Path(dst) == destination:
            failed = True
            raise OSError("simulated publish failure")
        return real_replace(src, dst)

    with patch("menu_capture.uipath_runner.os.replace", side_effect=fail_new_directory_once):
        with pytest.raises(OSError, match="simulated publish failure"):
            app.state.runner._publish_store_output(tmp_path / "output", task, staging, batch_id="f" * 32)

    assert (destination / "page-0001.png").is_file()
    assert Image.open(destination / "page-0001.png").getpixel((0, 0)) == (255, 0, 0)
    assert app.state.store.get(task["id"])["output_dir"] == str(destination)


def test_explicit_trial_targets_existing_task_without_changing_blocked_legacy_run(tmp_path: Path, monkeypatch):
    app = create_app(tmp_path)
    source = _image(tmp_path / "published" / ("1" * 64) / "目标店" / "二维码.png", "white")
    (source.parent.parent / ".materials-snapshot.json").write_text(
        json.dumps({"snapshot_id": "1" * 64}), encoding="utf-8"
    )
    app.state.store.scan(source.parent.parent)
    task = app.state.store.tasks()[0]
    app.state.store.create_run("legacy-blocked", "queue", ["legacy-task"])
    with app.state.store.connect() as db:
        db.execute("UPDATE runs SET status='blocked',message='legacy failure' WHERE id='legacy-blocked'")
    before_run = app.state.store.get_run("legacy-blocked")

    called = {}

    def mock_start(**kwargs):
        called.update(kwargs)
        return {"ok": True, "running": True, "task_id": kwargs["trial_task_id"]}

    monkeypatch.setattr(app.state.runner, "start", mock_start)
    client = TestClient(app, base_url="http://127.0.0.1")
    response = client.post("/api/ui-tars/trial", json={"task_id": task["id"]},
                           headers={"X-Capture-Token": app.state.token})

    assert response.status_code == 200, response.text
    assert response.json()["task_id"] == task["id"]
    assert called == {"ui_tars_trial": True, "trial_task_id": task["id"]}
    assert app.state.store.get_run("legacy-blocked") == before_run
