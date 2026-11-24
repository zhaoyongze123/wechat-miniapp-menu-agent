from io import BytesIO
from pathlib import Path
import time

from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.server import create_app


def image_bytes():
    stream = BytesIO()
    Image.new("RGB", (200, 200), "white").save(stream, format="PNG")
    return stream.getvalue()


def test_task_center_pause_resume_and_events(tmp_path: Path):
    import threading

    app = create_app(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow_inspector(_path):
        entered.set()
        assert release.wait(timeout=5)
        return []

    app.state.materials.inspector = slow_inspector
    source = tmp_path / "inbox"
    for name in ("店甲", "店乙"):
        folder = source / name
        folder.mkdir(parents=True)
        # 2 张才进入方形检测；单图门店按整图候选，不调用检测器。
        Image.new("RGB", (200, 200), "white").save(folder / "a.png", format="PNG")
        Image.new("RGB", (200, 200), "gray").save(folder / "b.png", format="PNG")
    batch = app.state.materials.create_local_batch(source)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-Capture-Token": app.state.token}
    base = f"/api/materials/batches/{batch['batch_id']}"

    assert client.post(base + "/analyze", json={}, headers=headers).status_code == 200
    assert entered.wait(timeout=5)
    tasks = client.get("/api/tasks/active").json()["tasks"]
    material_task = next(task for task in tasks if task["source"] == "materials")
    assert material_task["status"] in {"running", "pause_requested"}
    job_id = material_task["id"]

    assert client.post(f"/api/materials/jobs/{job_id}/pause", json={}, headers=headers).status_code == 200
    release.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = client.get(f"/api/materials/jobs/{job_id}").json()
        if detail["status"] == "paused":
            break
        time.sleep(.02)
    assert detail["status"] == "paused"

    assert client.post(f"/api/materials/jobs/{job_id}/resume", json={}, headers=headers).status_code == 200
    while time.monotonic() < deadline + 10:
        record = client.get(base).json()
        if record["status"] == "completed" and record["job"]["status"] == "completed":
            break
        time.sleep(.02)
    assert record["status"] == "completed"
    events = client.get(f"/api/materials/jobs/{job_id}?events=1").json()["events"]
    assert any("暂停" in event["message"] or "恢复" in event["message"] for event in events)


def test_directory_upload_review_publish_and_idempotency(tmp_path: Path):
    app = create_app(tmp_path)
    app.state.materials.inspector = lambda _: [{
        "box": {"x": 20, "y": 20, "width": 100, "height": 100}, "payload": "local-private-code"
    }]
    app.state.materials.ocr_reader = lambda _: [{
        "text": "扫码点餐", "x": 30, "y": 130, "width": 100, "height": 20
    }]
    app.state.configs.save({"input_dir": str(tmp_path / "original-inbox"),
                            "output_dir": str(tmp_path / "screenshots")})
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-Capture-Token": app.state.token}
    assert client.post("/api/materials/uploads").status_code == 403
    batch = client.post("/api/materials/uploads", json={"name": "上传测试批次"}, headers=headers).json()["batch_id"]
    prefix = f"/api/materials/uploads/{batch}"
    rejected = client.put(prefix + "/file", params={"path": "../escape.png"}, content=image_bytes(), headers=headers)
    assert rejected.status_code == 400
    uploaded = client.put(prefix + "/file", params={"path": "新素材/门店甲/图片.png"}, content=image_bytes(), headers=headers)
    assert uploaded.status_code == 200
    assert client.post(prefix + "/finish", json={}, headers=headers).status_code == 200
    base = f"/api/materials/batches/{batch}"
    assert client.post(base + "/analyze", json={}, headers=headers).status_code == 200
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = client.get(base).json()
        job = record.get("job")
        if job and job["status"] == "completed" and record["status"] == "completed":
            break
        time.sleep(.01)
    assert record["status"] == "completed"
    assert record["summary"]["confirmed_stores"] == 0
    # 自动推荐还未确认时不可发布，也不会提前生成采集任务。
    assert client.post(base + "/publish", json={}, headers=headers).status_code == 400
    assert client.get("/api/status").json()["tasks"] == []
    sid = record["stores"][0]["store_id"]
    store_base = base + f"/stores/{sid}"
    detail = client.get(store_base).json()
    assert detail["source_name"] == "门店甲"
    assert "local-private-code" not in str(detail)
    assert client.get(store_base + "/images/0/candidates/0").headers["content-type"] == "image/png"
    confirmed = client.post(store_base + "/decision", json={"decision": "confirm", "image_index": 0, "candidate_index": 0}, headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["decision"] == "confirmed"
    payload = {"name": "首批门店", "version": "v1.0.0", "description": "首次发布"}
    published = client.post(base + "/publish", json=payload, headers=headers)
    assert published.status_code == 200
    assert published.json()["task_scan"]["added"] == 1
    assert published.json()["release"]["batch_name"] == "首批门店"
    assert published.json()["release"]["version"] == "v1.0.0"
    tasks = client.get("/api/status").json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["source_name"] == "门店甲"
    assert client.post(base + "/publish", json=payload, headers=headers).json()["already_published"] is True
    assert len(client.get("/api/status").json()["tasks"]) == 1
    assert client.get("/assets/secrets.local.json").status_code == 404
    class RunningThread:
        def is_alive(self):
            return True
    app.state.runner.thread = RunningThread()
    second = client.post(base + "/publish", json={**payload, "version": "v1.0.1", "description": "新版"},
                         headers=headers)
    assert second.status_code == 200
    assert second.json()["release"]["active"] is False
    assert client.get("/api/status").json()["material_release"]["version"] == "v1.0.0"
    assert client.post(base + "/activate", json={"version": "v1.0.1"}, headers=headers).status_code == 409
    app.state.runner.thread = None
    assert client.post(base + "/activate", json={"version": "v1.0.1"}, headers=headers).status_code == 200
    assert client.get("/api/status").json()["material_release"]["version"] == "v1.0.1"
    assert len(client.get(base).json()["releases"]) == 2
    assert client.post(store_base + "/decision", json={"decision": "skip"}, headers=headers).status_code == 400
    assert client.post(store_base + "/decision", json={"decision": "skip", "reject_reason": "other"},
                       headers=headers).status_code == 400
    response = client.post(store_base + "/decision",
                           json={"decision": "skip", "reject_reason": "invalid_qr"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["rejection"]["label"] == "二维码无效"
    review = client.get(base).json()
    assert review["summary"]["skipped_stores"] == 1
    assert review["stores"][0]["rejection"]["code"] == "invalid_qr"
    assert review["has_changes"] is True
    response = client.post(store_base + "/decision", json={"decision": "pending"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["rejection"] is None
    assert client.get(base).json()["summary"]["pending_stores"] == 1




def test_invalid_publish_does_not_change_current_input_during_capture(tmp_path: Path):
    app = create_app(tmp_path)
    class RunningThread:
        def is_alive(self):
            return True
    app.state.runner.thread = RunningThread()
    client = TestClient(app, base_url="http://127.0.0.1")
    original = app.state.configs.load().input_dir
    response = client.post("/api/materials/batches/nonexistent/publish", json={},
                           headers={"X-Capture-Token": app.state.token})
    assert response.status_code == 400
    assert app.state.configs.load().input_dir == original


def test_task_center_includes_capture_run_and_hides_stale(tmp_path: Path):
    """采集任务复用同一任务中心；结束超过 10 分钟的历史任务不再常驻。"""
    app = create_app(tmp_path)
    store = app.state.store
    run = store.create_run("run-capture-1", "capture", [], "测试采集")
    client = TestClient(app, base_url="http://127.0.0.1")

    tasks = client.get("/api/tasks/active").json()["tasks"]
    capture = next(task for task in tasks if task["source"] == "capture")
    assert capture["id"] == run["id"]
    assert capture["status"] == "running"

    with store.connect() as db:
        db.execute("UPDATE runs SET status='completed',updated_at='2000-01-01T00:00:00+08:00' WHERE id=?", (run["id"],))
    tasks = client.get("/api/tasks/active").json()["tasks"]
    assert all(task["source"] != "capture" for task in tasks)
