from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.server import create_app
from menu_capture.rejections import RejectionService


def _source(root: Path) -> Path:
    source = root / "inbox"
    for name in ("店甲", "店乙"):
        folder = source / name
        folder.mkdir(parents=True)
        Image.new("RGB", (120, 120), "white").save(folder / "menu.png")
    return source


def test_batch_undo_reports_each_item_and_preserves_successes(tmp_path: Path):
    app = create_app(tmp_path)
    service = RejectionService(app.state.store, app.state.materials)
    source = _source(tmp_path)
    app.state.store.scan(source)
    ids = [row["id"] for row in app.state.store.tasks()]
    with app.state.store.connect() as db:
        db.execute("UPDATE tasks SET status='captured'")
    for task_id in ids:
        service.mark_task(task_id, "other", "需人工复核")

    client = TestClient(app, base_url="http://127.0.0.1")
    response = client.post("/api/rejections/undo-batch", json={"items": [
        {"source_type": "capture", "task_id": ids[0]},
        {"source_type": "capture", "task_id": "missing-task"},
    ]}, headers={"X-Capture-Token": app.state.token})

    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["ok"] for item in results] == [True, False]
    assert results[1]["error"] == "任务不存在"
    assert app.state.store.get(ids[0])["reject_reason"] == ""
    assert app.state.store.get(ids[1])["reject_reason"] == "other"


def test_rejection_review_filter_matches_export_scope(tmp_path: Path):
    app = create_app(tmp_path)
    service = RejectionService(app.state.store, app.state.materials)
    source = _source(tmp_path)
    app.state.store.scan(source)
    ids = [row["id"] for row in app.state.store.tasks()]
    with app.state.store.connect() as db:
        db.execute("UPDATE tasks SET status='captured'")
    for task_id in ids:
        service.mark_task(task_id, "other", "需人工复核")
    service.mark_reviewed("capture", task_id=ids[0], reviewed=True)
    client = TestClient(app, base_url="http://127.0.0.1")

    listed = client.get("/api/rejections?source=capture&reviewed=reviewed")
    exported = client.get("/api/rejections/export.xlsx?source=capture&reviewed=reviewed")

    assert listed.status_code == 200
    assert [row["task_id"] for row in listed.json()["records"]] == [ids[0]]
    assert exported.status_code == 200
    from io import BytesIO
    from openpyxl import load_workbook

    sheet = load_workbook(BytesIO(exported.content), read_only=True).active
    rows = list(sheet.iter_rows(values_only=True))
    assert len(rows) == 2
    assert rows[1][9] == ids[0]


def test_capture_rejection_reports_verified_material_target(tmp_path: Path):
    app = create_app(tmp_path)
    service = RejectionService(app.state.store, app.state.materials)
    source = _source(tmp_path)
    batch = app.state.materials.create_local_batch(source)
    app.state.store.scan(source)
    task = app.state.store.tasks()[0]
    with app.state.store.connect() as db:
        db.execute("UPDATE tasks SET status='captured'")
    service.mark_task(task["id"], "other", "需人工复核")
    client = TestClient(app, base_url="http://127.0.0.1")

    result = client.get("/api/rejections?source=capture").json()["records"][0]

    assert result["batch_id"] == batch["batch_id"]
    assert result["can_return_to_materials"] is False
    assert result["return_to_materials_disabled_reason"] == "原批次中无法唯一匹配该门店，无法安全打回"
