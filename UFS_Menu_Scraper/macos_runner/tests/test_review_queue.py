from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.server import create_app


def _make_source(root: Path) -> Path:
    source = root / "inbox"
    for name in ("店甲", "店乙"):
        folder = source / name
        folder.mkdir(parents=True)
        Image.new("RGB", (200, 200), "white").save(folder / "a.png")
        Image.new("RGB", (200, 200), "gray").save(folder / "b.png")
    return source


def test_batch_workflow_stage_labels(tmp_path: Path):
    app = create_app(tmp_path)
    service = app.state.materials
    assert service.workflow_stage({})["label"] == "未采集"
    assert service.workflow_stage({"capture_rounds": 1})["stage"] == "first_run"
    assert service.workflow_stage({"capture_rounds": 2})["stage"] == "rerun"
    approved = service.workflow_stage({"capture_rounds": 3, "approved_at": "2026-11-18T10:00:00"})
    assert approved["stage"] == "approved" and approved["label"] == "人工审核通过（可建档）"
    manual = service.workflow_stage({"capture_rounds": 0, "manual_stage": "rerun"})
    assert manual["stage"] == "rerun" and manual["source"] == "manual"
    assert service.workflow_stage({"capture_rounds": 0})["source"] == "auto"


def test_pending_reviews_include_batch_and_approval(tmp_path: Path):
    app = create_app(tmp_path)
    source = _make_source(tmp_path)
    batch = app.state.materials.create_local_batch(source)
    app.state.store.scan(source)
    with app.state.store.connect() as db:
        task_id = db.execute("SELECT id FROM tasks ORDER BY id").fetchone()["id"]
        db.execute(
            "UPDATE tasks SET status='needs_review',error_code='agent_runtime',message=? WHERE id=?",
            ("未找到可操作的目标窗口", task_id),
        )
    client = TestClient(app, base_url="http://127.0.0.1")
    data = client.get("/api/reviews/pending").json()
    assert data["summary"]["stores"] == 1
    record = data["records"][0]
    assert record["task_id"] == task_id
    assert record["batch_id"] == batch["batch_id"]
    assert record["batch_name"] == app.state.materials._batch_row(batch["batch_id"])["name"]
    assert record["workflow"]["stage"] == "not_started"

    headers = {"X-Capture-Token": app.state.token}
    approved = client.post(
        f"/api/materials/batches/{batch['batch_id']}/approve", json={}, headers=headers)
    assert approved.status_code == 200
    assert approved.json()["workflow"]["stage"] == "approved"

    # 未发布批次没有可执行的已发布快照，重跑轮次不应被计入。
    app.state.materials.set_approved(batch["batch_id"], False)
    assert app.state.materials.register_capture_round([task_id]) == []
    assert app.state.materials.get_batch(batch["batch_id"])["workflow"]["stage"] == "not_started"
    with app.state.store.connect() as db:
        db.execute("UPDATE material_batches SET capture_rounds=1 WHERE id=?", (batch["batch_id"],))
    assert app.state.materials.get_batch(batch["batch_id"])["workflow"]["stage"] == "first_run"

    # 人工覆盖优先级高于轮次；传空字符串恢复自动判断。
    manual = client.post(
        f"/api/materials/batches/{batch['batch_id']}/stage",
        json={"stage": "rerun"}, headers=headers)
    assert manual.status_code == 200
    assert manual.json()["workflow"]["source"] == "manual"
    assert manual.json()["workflow"]["stage"] == "rerun"
    auto = client.post(
        f"/api/materials/batches/{batch['batch_id']}/stage",
        json={"stage": ""}, headers=headers)
    assert auto.json()["workflow"]["stage"] == "first_run"


def test_task_history_lists_capture_runs(tmp_path: Path):
    app = create_app(tmp_path)
    source = _make_source(tmp_path)
    app.state.store.scan(source)
    with app.state.store.connect() as db:
        task_id = db.execute("SELECT id FROM tasks ORDER BY id").fetchone()["id"]
        db.execute("UPDATE tasks SET status='captured' WHERE id=?", (task_id,))
    app.state.store.create_run("run-history-1", "full", [task_id])
    # 历史按当轮留存结果统计，不再从门店当前状态回算。
    app.state.store.update(task_id, capture_run_id="run-history-1", attempt_outcome="captured",
                           status="captured")
    app.state.store.update_run("run-history-1", status="completed")
    client = TestClient(app, base_url="http://127.0.0.1")
    data = client.get("/api/tasks/history").json()
    assert data["summary"]["total"] == 1
    record = data["records"][0]
    assert record["kind"] == "capture" and record["id"] == "run-history-1"
    assert record["counts"]["done"] == 1
    detail = client.get("/api/tasks/history/capture/run-history-1").json()
    assert len(detail["tasks"]) == 1
    assert detail["tasks"][0]["label"] == "已截图待复核"
