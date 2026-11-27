"""覆盖共享工作台分页、统计口径和跨页勾选的真实 HTTP 契约。"""
import base64
import json

from fastapi.testclient import TestClient

from menu_capture.api import server
from menu_capture.api.server import create_app
from menu_capture.projects.service import LEGACY_ID, create_project, ensure_legacy_project


def test_shared_overview_pages_without_resolving_task_paths(tmp_path, monkeypatch):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    with app.state.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_legacy_project(db)
    materials = app.state.materials
    source = tmp_path / "published-fixture"
    source.mkdir()
    monkeypatch.setattr(app.state.configs, "load", lambda: type("Config", (), {
        "input_dir": str(source), "output_dir": "",
    })())
    monkeypatch.setattr(materials, "active_release", lambda _root, **_kwargs:
                        {"snapshot_id": "latest-a", "batch_id": "a"})
    rows = [
        dict(id=f"task-{n:03}", source_name=f"门店 {n:03}", source_path=f"/fake/input/qr-{n:03}.png",
             snapshot_id="latest-a" if n < 60 else "latest-b", updated_at=f"2026-11-20T12:00:{n % 60:02}",
             status="needs_review" if n in {1, 2} else "pending", reject_reason="other" if n == 2 else "",
             runnable=True)
        for n in range(125)
    ]
    monkeypatch.setattr(materials, "effective_capture_tasks", lambda **_kwargs: rows)
    # 任何旧任务路径扫描都会抛错。旧 API 的历史运行分支仍可读取任务表。
    def legacy_tasks():
        return [{"id": "legacy", "source_path": object()}]
    monkeypatch.setattr(app.state.store, "tasks", legacy_tasks)

    def unexpected_rejections():
        raise AssertionError("没有素材不合格门店时不应读取采集不合格历史")
    monkeypatch.setattr(app.state.store, "rejections", unexpected_rejections)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        page = client.get("/api/status?queue_page=1&include_events=false").json()
        assert len(page["tasks"]) == 50
        assert page["queue"] == {"page": 1, "pages": 3, "total": 125, "selected": []}
        assert page["counts"] == {"rejected": 1, "returned": 0, "review": 1, "captured": 0,
                                  "running": 0, "pending": 123, "needs_review": 1, "attention": 1}
        assert sum(page["counts"][key] for key in ("rejected", "returned", "review", "captured",
                                                   "running", "pending")) == 125
        assert page["events"] == []
        search = client.get("/api/status", params={
            "queue_page": 9, "queue_q": "qr-001", "queue_filter": "review", "selected": "task-080",
        }).json()
        assert [task["id"] for task in search["tasks"]] == ["task-001"]
        assert search["queue"]["page"] == 1
        assert search["queue"]["selected"][0]["id"] == "task-080"
        assert search["counts"]["review"] == 1
        assert search["counts"]["pending"] == search["counts"]["rejected"] == 0
        current = client.get("/api/status?queue_page=2&queue_scope=current").json()
        assert len(current["tasks"]) == 10 and current["queue"]["total"] == 60
        assert len(client.get("/api/status").json()["tasks"]) == 125


def test_hub_status_deduplicates_only_matching_rejections_in_same_project(tmp_path, monkeypatch):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    with app.state.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_legacy_project(db)
        other = create_project(db, "other", "另一项目")["id"]
        db.execute("INSERT INTO material_batches(id,source_kind,source_root,status,created_at,updated_at,project_id)"
                   " VALUES('batch','local','','completed','t','t',?)", (LEGACY_ID,))
        db.executemany(
            "INSERT INTO material_decisions(batch_id,store_id,source_folder,source_name,analysis_status,"
            "decision,store_json,updated_at) VALUES('batch',?,?,?,'completed','skipped',?,'t')",
            [(code, code, code, json.dumps({"source_folder": code})) for code in ("E1001", "E1002", "E1003")],
        )
        db.executemany(
            "INSERT INTO capture_run_results(run_id,task_id,store_name,project_id) VALUES('run',?,'',?)",
            [("same-project", LEGACY_ID), ("other-project", other)],
        )
    monkeypatch.setattr(app.state.store, "rejections", lambda: [
        {"id": "same-project", "source_name": "E1001-同项目采集不合格"},
        {"id": "other-project", "source_name": "E1002-其他项目采集不合格"},
        {"id": "unknown", "source_name": "E1003-归属待核验"},
        {"id": "unrelated", "source_name": "E2001-无对应素材不合格"},
    ])
    requested = []
    resolve = server.resolve_task_projects

    def track_resolver(db, task_ids):
        requested.extend(task_ids)
        return resolve(db, task_ids)

    monkeypatch.setattr(server, "resolve_task_projects", track_resolver)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        response = client.get("/api/status?queue_page=1&include_events=false")
    assert response.status_code == 200
    assert response.json()["material_rejected_all"] == 2
    assert requested == ["same-project", "other-project", "unknown"]


def test_closed_assignment_history_can_be_omitted(tmp_path):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    with app.state.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_legacy_project(db)
        db.execute("INSERT INTO shared_workers VALUES(?,?,?,?,?)", ("a", "A 机", "fake-hash", "", 0))
        db.execute("INSERT INTO shared_assignments(id,task_id,worker_id,batch_id,batch_name,store,revision,snapshot_id,"
                   "source_hash,source_suffix,status,created_at,project_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("old", "task", "a", "batch", "测试批次", "门店", "v1", "snap", "hash", ".png", "completed", "",
                    LEGACY_ID))
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        assert client.get("/api/shared/status?include_history=false").json()["assignments"] == []
        assert [item["id"] for item in client.get("/api/shared/status", params={"project_id": LEGACY_ID})
                .json()["assignments"]] == ["old"]


def test_hub_active_tasks_ignores_migrated_desktop_run(tmp_path, monkeypatch):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    with app.state.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_legacy_project(db)
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
        db.execute("BEGIN IMMEDIATE")
        ensure_legacy_project(db)
        for batch_id, deleted_at in [("a", ""), ("trash", stamp)]:
            db.execute("INSERT INTO material_batches(id,name,source_kind,source_root,status,created_at,updated_at,"
                       "deleted_at,capture_rounds,project_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (batch_id, "验收批次", "local", str(tmp_path), "completed", stamp, stamp, deleted_at, 2,
                        LEGACY_ID))
        db.execute("INSERT INTO material_versions VALUES(?,?,?,?,?,?,?)", ("v1", "a", "首版", "snap-a", "", stamp, 3))
        db.execute("INSERT INTO project_material_selection VALUES(?, 'v1')", (LEGACY_ID,))
    monkeypatch.setattr(app.state.configs, "load", lambda: type("Config", (), {"input_dir": str(active_root)})())

    def unexpected_summary(_row):
        raise AssertionError("下拉框不应生成完整批次摘要")

    monkeypatch.setattr(materials, "_batch_summary", unexpected_summary)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}
    with TestClient(app, base_url="http://127.0.0.1", headers=auth) as client:
        options = client.get("/api/materials/batches?view=options").json()["batches"]
        assert [(row["batch_id"], row["name"], row["current_active"], row["workflow"]["stage"])
                for row in options] == [("a", "验收批次", True, "rerun")]
