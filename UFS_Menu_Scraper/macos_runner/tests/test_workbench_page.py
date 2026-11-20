"""覆盖共享工作台分页、统计口径和跨页勾选的真实 HTTP 契约。"""
import base64

from fastapi.testclient import TestClient

from menu_capture.server import create_app


def test_shared_overview_pages_without_resolving_task_paths(tmp_path, monkeypatch):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    materials = app.state.materials
    source = tmp_path / "published-fixture"
    source.mkdir()
    monkeypatch.setattr(app.state.configs, "load", lambda: type("Config", (), {
        "input_dir": str(source), "output_dir": "",
    })())
    monkeypatch.setattr(materials, "active_release", lambda _root: {"snapshot_id": "latest-a", "batch_id": "a"})
    rows = [
        dict(id=f"task-{n:03}", source_name=f"门店 {n:03}", source_path=f"/fake/input/qr-{n:03}.png",
             snapshot_id="latest-a" if n < 60 else "latest-b", updated_at=f"2026-11-20T12:00:{n % 60:02}",
             status="needs_review" if n in {1, 2} else "pending", reject_reason="other" if n == 2 else "",
             runnable=True)
        for n in range(125)
    ]
    monkeypatch.setattr(materials, "effective_capture_tasks", lambda: rows)
    # 任何旧任务路径扫描都会抛错。旧 API 的历史运行分支仍可读取任务表。
    def legacy_tasks():
        return [{"id": "legacy", "source_path": object()}]
    monkeypatch.setattr(app.state.store, "tasks", legacy_tasks)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        page = client.get("/api/status?queue_page=1&include_events=false").json()
        assert len(page["tasks"]) == 50
        assert page["queue"] == {"page": 1, "pages": 3, "total": 125, "selected": []}
        assert page["counts"] == {"pending": 123, "needs_review": 2, "attention": 1, "rejected": 1}
        assert page["events"] == []
        search = client.get("/api/status", params={
            "queue_page": 9, "queue_q": "qr-001", "queue_filter": "review", "selected": "task-080",
        }).json()
        assert [task["id"] for task in search["tasks"]] == ["task-001"]
        assert search["queue"]["page"] == 1
        assert search["queue"]["selected"][0]["id"] == "task-080"
        assert search["counts"] == page["counts"]
        current = client.get("/api/status?queue_page=2&queue_scope=current").json()
        assert len(current["tasks"]) == 10 and current["queue"]["total"] == 60
        assert len(client.get("/api/status").json()["tasks"]) == 125


def test_closed_assignment_history_can_be_omitted(tmp_path):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    with app.state.store.connect() as db:
        db.execute("INSERT INTO shared_workers VALUES(?,?,?,?,?)", ("a", "A 机", "fake-hash", "", 0))
        db.execute("INSERT INTO shared_assignments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("old", "task", "a", "batch", "测试批次", "门店", "v1", "snap", "hash", ".png", "completed", ""))
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        assert client.get("/api/shared/status?include_history=false").json()["assignments"] == []
        assert [item["id"] for item in client.get("/api/shared/status").json()["assignments"]] == ["old"]


def test_hub_active_tasks_ignores_migrated_desktop_run(tmp_path, monkeypatch):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    store = app.state.store
    store.create_run("legacy-run", "full", ["old-task"])
    store.update_run("legacy-run", status="blocked")

    def unexpected_read():
        raise AssertionError("共享轮询不应读取旧采集轮次或扫描门店文件路径")

    monkeypatch.setattr(store, "latest_run", unexpected_read)
    monkeypatch.setattr(store, "tasks", unexpected_read)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        assert client.get("/api/tasks/active").json() == {"tasks": []}


def test_material_batch_options_preserve_active_selection_and_stage(tmp_path, monkeypatch):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    materials = app.state.materials
    active_root = materials.published_root / "snap-a"
    stamp = "2026-11-20T12:00:00+08:00"
    with app.state.store.connect() as db:
        for batch_id, deleted_at in [("a", ""), ("trash", stamp)]:
            db.execute("INSERT INTO material_batches(id,name,source_kind,source_root,status,created_at,updated_at,"
                       "deleted_at,capture_rounds) VALUES(?,?,?,?,?,?,?,?,?)",
                       (batch_id, "验收批次", "local", str(tmp_path), "completed", stamp, stamp, deleted_at, 2))
        db.execute("INSERT INTO material_versions VALUES(?,?,?,?,?,?,?)", ("v1", "a", "首版", "snap-a", "", stamp, 3))
        db.execute("INSERT INTO material_selection VALUES(1,'v1')")
    monkeypatch.setattr(app.state.configs, "load", lambda: type("Config", (), {"input_dir": str(active_root)})())

    def unexpected_summary(_row):
        raise AssertionError("下拉框不应生成完整批次摘要")

    monkeypatch.setattr(materials, "_batch_summary", unexpected_summary)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        options = client.get("/api/materials/batches?view=options").json()["batches"]
        assert [(row["batch_id"], row["name"], row["current_active"], row["workflow"]["stage"])
                for row in options] == [("a", "验收批次", True, "rerun")]
