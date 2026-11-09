from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.server import create_app


def _png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 180), "white").save(path, format="PNG")


def _setup(tmp_path: Path):
    app = create_app(tmp_path)
    # 素材分析只走本地假 inspector/OCR，不访问 UI、模型或网络。
    app.state.materials.inspector = lambda _path: [{
        "box": {"x": 35, "y": 25, "width": 90, "height": 90},
        "payload": "local-test-payload",
    }]
    app.state.materials.ocr_reader = lambda _path: [{
        "text": "扫码点餐", "x": 20, "y": 125, "width": 120, "height": 24,
    }]
    client = TestClient(app, base_url="http://127.0.0.1")
    return app, client, {"X-Capture-Token": app.state.token}


def _create(client: TestClient, headers: dict, source: Path, *, name="素材批次", description="描述") -> str:
    response = client.post("/api/materials/batches", json={
        "input_dir": str(source), "name": name, "description": description,
    }, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["batch_id"]


def _analyze(client: TestClient, headers: dict, batch_id: str) -> dict:
    response = client.post(f"/api/materials/batches/{batch_id}/analyze", json={}, headers=headers)
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = client.get(f"/api/materials/batches/{batch_id}").json()
        if record["status"] not in {"queued", "analyzing"}:
            return record
        time.sleep(.01)
    raise AssertionError("素材分析 worker 在 5 秒内未结束")


def _publish(client: TestClient, headers: dict, batch_id: str) -> dict:
    record = _analyze(client, headers, batch_id)
    assert record["stores"], record
    store = record["stores"][0]
    decision = client.post(
        f"/api/materials/batches/{batch_id}/stores/{store['store_id']}/decision",
        json={"decision": "confirm", "image_index": 0, "candidate_index": 0},
        headers=headers,
    )
    assert decision.status_code == 200, decision.text
    published = client.post(
        f"/api/materials/batches/{batch_id}/publish",
        json={"name": "测试发布", "version": "v1.0.0", "description": "集成测试"},
        headers=headers,
    )
    assert published.status_code == 200, published.text
    return published.json()


def _management(client: TestClient, *, deleted=False) -> list[dict]:
    response = client.get("/api/materials/batch-management", params={"deleted": str(deleted).lower()})
    assert response.status_code == 200, response.text
    return response.json()["batches"]


def test_batch_metadata_create_and_edit_preserves_identity_and_analysis(tmp_path: Path):
    _app, client, headers = _setup(tmp_path)
    source = tmp_path / "originals"
    original = source / "门店甲" / "菜单.png"
    _png(original)
    batch_id = _create(client, headers, source, name="第一批", description="初始描述")
    analyzed = _analyze(client, headers, batch_id)
    before_store = analyzed["stores"][0]

    updated = client.patch(
        f"/api/materials/batches/{batch_id}",
        json={"name": "改名后的批次", "description": "更新说明"}, headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "改名后的批次"
    assert updated.json()["description"] == "更新说明"
    after = client.get(f"/api/materials/batches/{batch_id}").json()
    assert after["batch_id"] == batch_id
    assert after["status"] == analyzed["status"]
    assert after["stores"] == [before_store]
    assert original.is_file()
    assert _management(client)[0]["name"] == "改名后的批次"


@pytest.mark.parametrize("payload", [
    {"input_dir": "", "name": "   ", "description": ""},
    {"input_dir": "", "name": "x" * 81, "description": ""},
    {"input_dir": "", "name": "valid", "description": "x" * 501},
])
def test_batch_metadata_create_rejects_invalid_name_or_description(tmp_path: Path, payload: dict):
    _app, client, headers = _setup(tmp_path)
    source = tmp_path / "originals"
    _png(source / "Shop" / "code.png")
    response = client.post("/api/materials/batches", json={**payload, "input_dir":str(source)}, headers=headers)
    assert response.status_code == 400


def test_upload_creation_requires_valid_metadata(tmp_path: Path):
    _app, client, headers = _setup(tmp_path)
    for payload in (
        {"name": "", "description": ""},
        {"name": "x" * 81, "description": ""},
        {"name": "valid", "description": "x" * 501},
    ):
        response = client.post("/api/materials/uploads", json=payload, headers=headers)
        assert response.status_code == 400
    created = client.post("/api/materials/uploads", json={"name": "上传批次", "description": "备注"}, headers=headers)
    assert created.status_code == 200, created.text


def test_soft_delete_hides_batch_and_restore_preserves_review_state(tmp_path: Path):
    _app, client, headers = _setup(tmp_path)
    source = tmp_path / "originals"
    _png(source / "门店甲" / "菜单.png")
    batch_id = _create(client, headers, source)
    analyzed = _analyze(client, headers, batch_id)
    before_store = analyzed["stores"][0]

    deleted = client.delete(f"/api/materials/batches/{batch_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["batch"]["deleted_at"]
    assert batch_id not in {item["batch_id"] for item in client.get("/api/materials/batches").json()["batches"]}
    trash = _management(client, deleted=True)
    assert any(item["batch_id"] == batch_id and item["can_purge"] for item in trash)
    for suffix, method, payload in (
        ("/analyze", "post", {}),
        ("/publish", "post", {"name": "禁止发布", "version": "v1.0.0", "description": ""}),
        ("/activate", "post", {"version": "v1.0.0"}),
    ):
        response = getattr(client, method)(f"/api/materials/batches/{batch_id}{suffix}", json=payload, headers=headers)
        assert response.status_code in {400, 404, 409}
    store_id = before_store["store_id"]
    supplement = client.post(
        f"/api/materials/batches/{batch_id}/stores/{store_id}/uploads", json={}, headers=headers,
    )
    assert supplement.status_code in {400, 404, 409}

    restored = client.post(f"/api/materials/batches/{batch_id}/restore", json={}, headers=headers)
    assert restored.status_code == 200, restored.text
    after = client.get(f"/api/materials/batches/{batch_id}").json()
    assert after["batch_id"] == batch_id
    assert after["stores"] == [before_store]
    assert any(item["batch_id"] == batch_id for item in client.get("/api/materials/batches").json()["batches"])


def test_soft_deleted_batch_is_excluded_from_rejection_summary_until_restored(tmp_path: Path):
    _app, client, headers = _setup(tmp_path)
    source = tmp_path / "originals"
    _png(source / "门店甲" / "菜单.png")
    batch_id = _create(client, headers, source)
    analyzed = _analyze(client, headers, batch_id)
    store_id = analyzed["stores"][0]["store_id"]
    rejected = client.post(
        f"/api/materials/batches/{batch_id}/stores/{store_id}/decision",
        json={"decision": "skip", "reject_reason": "blur"}, headers=headers,
    )
    assert rejected.status_code == 200, rejected.text
    before = client.get("/api/rejections", params={"source": "materials", "batch_id": batch_id}).json()
    assert before["summary"]["records"] == 1

    assert client.delete(f"/api/materials/batches/{batch_id}", headers=headers).status_code == 200
    hidden = client.get("/api/rejections", params={"source": "materials", "batch_id": batch_id}).json()
    assert hidden["summary"]["records"] == 0
    assert client.post(f"/api/materials/batches/{batch_id}/restore", json={}, headers=headers).status_code == 200
    restored = client.get("/api/rejections", params={"source": "materials", "batch_id": batch_id}).json()
    assert restored["summary"]["records"] == 1


def test_delete_is_blocked_by_upload_analysis_and_recoverable_run_reference(tmp_path: Path):
    app, client, headers = _setup(tmp_path)
    uploading = client.post("/api/materials/uploads", json={"name": "上传中", "description": ""}, headers=headers)
    assert uploading.status_code == 200, uploading.text
    upload_id = uploading.json()["batch_id"]
    assert next(row for row in _management(client) if row["batch_id"] == upload_id)["can_delete"] is False

    source = tmp_path / "originals"
    _png(source / "门店甲" / "菜单.png")
    batch_id = _create(client, headers, source)
    # 运行记录关系由同一 TaskStore 持久化，状态用 paused 覆盖可恢复引用。
    run_id = "paused-reference"
    app.state.store.create_run(run_id, "queue", [])
    with app.state.store.connect() as db:
        db.execute("UPDATE runs SET material_batch_id=?,status='paused' WHERE id=?", (batch_id, run_id))
    manager_row = next(row for row in _management(client) if row["batch_id"] == batch_id)
    assert manager_row["can_delete"] is False
    assert manager_row["delete_block_reason"]
    denied = client.delete(f"/api/materials/batches/{batch_id}", headers=headers)
    assert denied.status_code == 409


@pytest.mark.parametrize("run_status", ["running", "paused"])
def test_clear_selection_blocks_running_or_paused_run_and_idle_clear_keeps_material_snapshot(tmp_path: Path, run_status: str):
    app, client, headers = _setup(tmp_path)
    source = tmp_path / "originals"
    _png(source / "门店甲" / "菜单.png")
    batch_id = _create(client, headers, source)
    published = _publish(client, headers, batch_id)
    snapshot = Path(published["input_dir"])
    assert snapshot.is_dir()
    active_row = next(row for row in _management(client) if row["batch_id"] == batch_id)
    assert active_row["current_active"] is True
    assert active_row["can_delete"] is False
    denied_delete = client.delete(f"/api/materials/batches/{batch_id}", headers=headers)
    assert denied_delete.status_code == 409

    run_id = f"{run_status}-selection-reference"
    app.state.store.create_run(run_id, "queue", [])
    with app.state.store.connect() as db:
        db.execute("UPDATE runs SET material_batch_id=?,status=? WHERE id=?", (batch_id, run_status, run_id))
    blocked = client.post("/api/materials/selection/clear", json={}, headers=headers)
    assert blocked.status_code == 409

    with app.state.store.connect() as db:
        db.execute("UPDATE runs SET status='completed' WHERE id=?", (run_id,))
    cleared = client.post("/api/materials/selection/clear", json={}, headers=headers)
    assert cleared.status_code == 200, cleared.text
    assert snapshot.is_dir()
    assert client.get("/api/materials/batches").json()["batches"]


def test_purge_removes_managed_analysis_but_keeps_local_originals(tmp_path: Path):
    _app, client, headers = _setup(tmp_path)
    source = tmp_path / "originals"
    original = source / "门店甲" / "菜单.png"
    _png(original)
    batch_id = _create(client, headers, source)
    _analyze(client, headers, batch_id)
    managed_analysis = tmp_path / "data" / "materials" / "batches" / batch_id
    assert managed_analysis.exists()

    deleted = client.delete(f"/api/materials/batches/{batch_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    purged = client.delete(f"/api/materials/batches/{batch_id}/permanent", headers=headers)
    assert purged.status_code == 200, purged.text
    assert purged.json()["ok"] is True
    assert not managed_analysis.exists()
    assert original.is_file()
    assert not any(row["batch_id"] == batch_id for row in _management(client, deleted=True))


def test_purge_rejects_any_run_history_reference(tmp_path: Path):
    app, client, headers = _setup(tmp_path)
    source = tmp_path / "originals"
    _png(source / "门店甲" / "菜单.png")
    batch_id = _create(client, headers, source)
    deleted = client.delete(f"/api/materials/batches/{batch_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    run_id = "completed-reference"
    app.state.store.create_run(run_id, "queue", [])
    with app.state.store.connect() as db:
        db.execute("UPDATE runs SET material_batch_id=?,status='completed' WHERE id=?", (batch_id, run_id))
    row = next(item for item in _management(client, deleted=True) if item["batch_id"] == batch_id)
    assert row["can_purge"] is False
    response = client.delete(f"/api/materials/batches/{batch_id}/permanent", headers=headers)
    assert response.status_code == 409
    assert any(item["batch_id"] == batch_id for item in _management(client, deleted=True))


@pytest.mark.parametrize('route', ['/api/materials/batches', '/api/materials/uploads'])
def test_api_rejects_missing_or_null_name(tmp_path, route):
    _app, client, headers = _setup(tmp_path)
    source = tmp_path / 'originals'
    _png(source / 'Shop' / 'code.png')
    for extra in ({}, {'name': None}, {'name': 123}):
        payload = {**extra, **({'input_dir':str(source)} if route.endswith('/batches') else {})}
        assert client.post(route, json=payload, headers=headers).status_code == 400
    assert _management(client) == []


def test_empty_selection_stays_empty_and_cannot_open_working_directory(tmp_path, monkeypatch):
    app, client, headers = _setup(tmp_path)
    app.state.configs.save({'input_dir':'', 'output_dir':str(tmp_path / 'results')})
    assert app.state.configs.load().input_dir == ''
    status = client.get('/api/status').json()
    assert status['tasks'] == [] and status['material_release'] is None
    assert client.post('/api/scan', json={}, headers=headers).status_code == 400
    monkeypatch.setattr('menu_capture.server.subprocess.run', lambda *a,**k: pytest.fail('不应打开进程工作目录'))
    assert client.post('/api/reveal', json={'path':'input'}, headers=headers).status_code == 400


def test_aborted_upload_can_be_managed_and_purged(tmp_path):
    app, client, headers = _setup(tmp_path)
    batch_id = client.post('/api/materials/uploads', json={'name':'上传中断'}, headers=headers).json()['batch_id']
    response = client.post(f'/api/materials/uploads/{batch_id}/abort', json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()['status'] == 'failed'
    assert client.delete(f'/api/materials/batches/{batch_id}', headers=headers).status_code == 200
    assert client.delete(f'/api/materials/batches/{batch_id}/permanent', headers=headers).status_code == 200


@pytest.mark.parametrize('committed', [False, True])
def test_purge_journal_recovers_interruption_without_touching_originals(tmp_path, committed):
    import json
    from menu_capture.batch_management import BatchManager
    app, client, headers = _setup(tmp_path)
    source = tmp_path / 'originals'
    original = source / 'Shop' / 'code.png'
    _png(original)
    batch_id = _create(client, headers, source)
    _analyze(client, headers, batch_id)
    assert client.delete(f'/api/materials/batches/{batch_id}', headers=headers).status_code == 200
    service = app.state.materials
    staging = service.material_root / '.purge' / (batch_id + '-test')
    staging.mkdir(parents=True)
    paths = []
    for root in (service.upload_root, service.batch_root):
        path = root / batch_id
        target = staging / root.name
        paths.append({'original':str(path), 'staged':str(target)})
        if path.exists(): path.rename(target)
    (staging / 'journal.json').write_text(json.dumps({'batch_id':batch_id, 'phase':'moved', 'paths':paths}))
    if committed:
        with service.task_store.connect() as db:
            db.execute('DELETE FROM material_batches WHERE id=?', (batch_id,))
    BatchManager(service)
    assert not staging.exists()
    assert original.is_file()
    assert (service.batch_root / batch_id).exists() is (not committed)


def test_purge_refuses_managed_symlink(tmp_path):
    app, client, headers = _setup(tmp_path)
    source = tmp_path / 'originals'
    original = source / 'Shop' / 'code.png'
    _png(original)
    batch_id = _create(client, headers, source)
    managed = app.state.materials.batch_root / batch_id
    managed.mkdir(parents=True, exist_ok=True)
    (managed / 'external').symlink_to(source, target_is_directory=True)
    assert client.delete(f'/api/materials/batches/{batch_id}', headers=headers).status_code == 200
    denied = client.delete(f'/api/materials/batches/{batch_id}/permanent', headers=headers)
    assert denied.status_code == 409 and '符号链接' in denied.text
    assert original.is_file()
