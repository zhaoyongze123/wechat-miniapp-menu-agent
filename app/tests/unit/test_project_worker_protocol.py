"""Worker 项目快照、配对隔离与严格离线协议兼容。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx
import pytest

from menu_capture.shared.shared_history import HistoryReport
from menu_capture.shared.shared_worker import SharedWorker, WORKER_CAPABILITIES
from menu_capture.storage.store import TaskStore


STAMP = "2026-11-26T12:00:00+08:00"
PROJECT_A = {"id": "project-a", "code": "q3", "name": "第三季度", "archived_at": ""}


def _worker(root, *, worker_id="worker-a", project_scope=True):
    store = TaskStore(root / "data" / "tasks.sqlite3")
    worker = SharedWorker(root, store, threading.Lock(), threading.RLock())
    worker.connection = {"url": "https://hub.example", "token": "token", "id": worker_id,
                         "supports_project_scope": project_scope}
    worker._hub_supports_project_scope = project_scope
    return worker


def _assignment(assignment_id="a" * 32, *, project_id="project-a", project_code="q3",
                project_name="第三季度"):
    return {
        "id": assignment_id, "task_id": "task-" + assignment_id, "batch_id": "batch-1",
        "batch_name": "共用批次名", "store": "同名门店", "revision": "v1",
        "snapshot_id": "c" * 64, "source_hash": "d" * 64, "source_suffix": ".png",
        "created_at": STAMP, "project_id": project_id, "project_code": project_code,
        "project_name": project_name,
    }


def test_history_dto_accepts_optional_project_snapshots_but_remains_strict():
    report = {
        "generation": 1,
        "run": {
            "id": "run-1", "name": "第一轮", "mode": "automatic", "capture_mode": "auto",
            "status": "completed", "task_ids": ["task-1"], "current_index": 1,
            "current_task_id": "", "message": "", "material_batch_id": "", "created_at": STAMP,
            "updated_at": STAMP, "project_id": "project-a",
        },
        "results": [{
            "task_id": "task-1", "store_name": "同名门店", "batch_id": "batch-1",
            "batch_name": "共用批次名", "version": "v1", "outcome": "captured",
            "reject_reason": "", "message": "", "project_id": "project-a",
        }],
    }

    assert HistoryReport.model_validate(report).run.project_id == "project-a"
    legacy = {**report, "run": {key: value for key, value in report["run"].items() if key != "project_id"},
              "results": [{key: value for key, value in report["results"][0].items()
                           if key != "project_id"}]}
    assert HistoryReport.model_validate(legacy).run.project_id is None
    with pytest.raises(ValueError):
        HistoryReport.model_validate({**legacy, "run": legacy["run"] | {"current_project": "project-a"}})


def test_worker_project_catalogue_is_scoped_to_pairing_identity(tmp_path):
    worker = _worker(tmp_path)
    worker._store_shared_state({
        "supports_project_scope": True,
        "projects": [PROJECT_A],
        "assignments": [_assignment()],
        "batches": [{"batch_id": "batch-1", "project_id": "project-a", "snapshot_id": "c" * 64}],
        "states": {},
    }, "worker-a")

    assert worker.projects() == [PROJECT_A]
    worker.connection = worker.connection | {"id": "worker-b"}
    assert worker.projects() == []
    worker.close()


def test_worker_rejects_cross_project_batch_and_old_hub_project_fields(tmp_path):
    worker = _worker(tmp_path)
    worker._cache_source = lambda *_args: None
    worker._download(_assignment(), worker.connection)
    with pytest.raises(ValueError, match="项目归属"):
        worker._download(_assignment("b" * 32, project_id="project-b", project_code="q4",
                                     project_name="第四季度"), worker.connection)

    legacy = _worker(tmp_path / "legacy", worker_id="worker-legacy", project_scope=False)
    legacy._cache_source = lambda *_args: None
    with pytest.raises(ValueError, match="未协商"):
        legacy._download(_assignment(), legacy.connection)
    worker.close()
    legacy.close()


def test_project_sync_backfills_same_legacy_assignment_by_authoritative_id(tmp_path):
    worker = _worker(tmp_path, project_scope=False)
    worker._cache_source = lambda *_args: None
    assigned = _assignment()
    legacy_assignment = {key: value for key, value in assigned.items()
                         if key not in {"project_id", "project_code", "project_name"}}
    worker._download(legacy_assignment, worker.connection)

    worker.connection["supports_project_scope"] = True
    worker._hub_supports_project_scope = True
    worker._store_shared_state({
        "supports_project_scope": True, "projects": [PROJECT_A], "assignments": [assigned],
        "batches": [{"batch_id": "batch-1", "project_id": "project-a", "snapshot_id": "c" * 64}],
        "states": {},
    }, "worker-a")

    with worker.store.connect() as db:
        row = db.execute("SELECT worker_id,project_id,project_code,project_name "
                         "FROM worker_assignments WHERE id=?", (assigned["id"],)).fetchone()
    assert tuple(row) == ("worker-a", "project-a", "q3", "第三季度")
    worker.close()


def test_worker_start_and_run_binding_revalidate_project_assignment(tmp_path):
    worker = _worker(tmp_path)
    worker._cache_source = lambda *_args: None
    assigned = _assignment()
    worker._download(assigned, worker.connection)
    source = worker._source_path(assigned)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"checked source")

    worker.check_start([assigned["task_id"]], "project-a")
    with pytest.raises(ValueError, match="项目"):
        worker.check_start([assigned["task_id"]], "project-b")

    worker.store.bind_assignments = worker.bind_run
    run = worker.store.create_run("run-1", "automatic", [assigned["task_id"]],
                                  capture_mode="auto", project_id="project-a")
    with worker.store.connect() as db:
        bound = db.execute("SELECT run_id FROM worker_assignments WHERE id=?", (assigned["id"],)).fetchone()
    assert run["project_id"] == "project-a"
    assert bound["run_id"] == "run-1"
    worker.close()


def test_run_summary_reads_persisted_project_snapshot_not_ui_selection(tmp_path):
    worker = _worker(tmp_path)
    worker.store.create_run("run-1", "automatic", ["task-a"], capture_mode="auto")
    worker.store.update_run("run-1", status="running", current_index=1)
    with worker.store.connect() as db:
        db.execute("UPDATE runs SET project_id='project-a' WHERE id='run-1'")

    assert worker._run_summary(include_project_scope=True, worker_id="worker-a")["project_id"] == "project-a"
    with worker.store.connect() as db:
        db.execute("UPDATE runs SET project_id='' WHERE id='run-1'")
    summary = worker._run_summary(include_project_scope=True, worker_id="worker-a")
    assert summary["project_id"] is None
    worker.close()


def test_run_history_adds_per_store_project_ids_only_after_capability(tmp_path):
    worker = _worker(tmp_path)
    worker.store.create_run("run-1", "automatic", ["task-a", "task-b"], capture_mode="auto")
    worker.store.update_run("run-1", status="completed", current_index=2)
    with worker.store.connect() as db:
        db.execute("UPDATE runs SET project_id='' WHERE id='run-1'")
        db.execute("UPDATE worker_run_delivery SET worker_id='worker-a' WHERE run_id='run-1'")
        db.executemany("""
            INSERT INTO capture_run_results(run_id,task_id,store_name,batch_id,batch_name,version,outcome,
                reject_reason,message,project_id)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (("run-1", "task-a", "同名门店 A", "batch-1", "共用批次名", "v1", "captured", "", "", "project-a"),
              ("run-1", "task-b", "同名门店 B", "batch-2", "共用批次名", "v1", "captured", "", "", "project-b")))

    sent = []

    def put(url, *, headers, json, timeout):
        sent.append({"url": url, "headers": headers, "report": json})
        return httpx.Response(200, json={"acknowledged": json["generation"]},
                              request=httpx.Request("PUT", url))

    worker._http = SimpleNamespace(put=put, close=lambda: None)
    worker._hub_supports_history = True
    worker._sync_run_history(worker.connection)

    assert sent[0]["headers"]["X-Worker-Capabilities"] == WORKER_CAPABILITIES
    assert sent[0]["report"]["run"]["project_id"] is None
    assert {row["task_id"]: row["project_id"] for row in sent[0]["report"]["results"]} == {
        "task-a": "project-a", "task-b": "project-b",
    }

    worker.connection["supports_project_scope"] = False
    worker._hub_supports_history = True
    worker.store.update_run("run-1", message="offline metadata update")
    worker._sync_run_history(worker.connection)
    # 旧 Hub 路径仍发送严格的旧 DTO 形状；能力提示头不改变旧请求体。
    assert sent[1]["headers"]["X-Worker-Capabilities"] == WORKER_CAPABILITIES
    assert "project_id" not in sent[1]["report"]["run"]
    assert all("project_id" not in row for row in sent[1]["report"]["results"])
    worker.close()


def test_pending_history_stays_bound_to_original_pairing_identity(tmp_path):
    worker = _worker(tmp_path, worker_id="worker-current", project_scope=False)
    worker.store.create_run("run-old", "automatic", [], capture_mode="auto")
    worker.store.create_run("run-current", "automatic", [], capture_mode="auto")
    with worker.store.connect() as db:
        db.execute("UPDATE worker_run_delivery SET worker_id='worker-old' WHERE run_id='run-old'")
        db.execute("UPDATE worker_run_delivery SET worker_id='worker-current' WHERE run_id='run-current'")

    report = worker.history.pending_report(worker_id="worker-current")

    assert report["run"]["id"] == "run-current"
    assert worker.history.pending_report(worker_id="worker-old")["run"]["id"] == "run-old"
    worker.close()
