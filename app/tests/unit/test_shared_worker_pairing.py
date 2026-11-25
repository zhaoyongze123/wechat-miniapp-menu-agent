"""共享采集端重新配对：凭证原子替换、旧结果保护与旧轮询响应隔离。"""
from __future__ import annotations

import json
from base64 import b64encode
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from menu_capture.api.server import create_app

URL = "http://127.0.0.1"
AUTH = {"Authorization": "Basic " + b64encode(b"admin:isolated-test").decode()}
STAMP = "2026-11-20T12:00:00+08:00"


@contextmanager
def _hub_and_worker(tmp_path):
    hub_app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    worker_app = create_app(tmp_path / "worker", role="worker")
    hub, worker = hub_app.state.shared_hub, worker_app.state.shared_worker
    with TestClient(hub_app, base_url=URL, headers=AUTH) as client:
        calls: list[tuple[str, str]] = []

        def transport(request):
            calls.append((request.method, request.url.path))
            identity = {"Authorization": request.headers["Authorization"]} if "Authorization" in request.headers else {}
            if request.method == "GET":
                result = client.get(request.url.path, headers=identity)
            elif request.method == "PUT":
                result = client.put(request.url.path, content=request.read(), headers=identity)
            else:
                result = client.post(request.url.path, json=json.loads(request.read()), headers=identity)
            return httpx.Response(result.status_code, content=result.content)

        worker._http.close()
        worker._http = httpx.Client(transport=httpx.MockTransport(transport))
        yield hub, worker, calls


def _seed(worker, *, assignment_id: str, status: str, worker_id: str = "", with_zip: bool = False) -> None:
    with worker.store.connect() as db:
        db.execute("""
            INSERT INTO worker_assignments (id,task_id,batch_id,batch_name,store,revision,snapshot_id,
                source_hash,source_suffix,status,created_at,worker_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (assignment_id, "task-" + assignment_id, "batch-1", "批次一", "甲门店", "v1", "c" * 64,
              "d" * 64, ".png", status, STAMP, worker_id))
    if with_zip:
        (worker.outbox / f"{assignment_id}.zip").write_bytes(b"PK-result")


def _row(worker, assignment_id: str) -> dict:
    with worker.store.connect() as db:
        return dict(db.execute("SELECT * FROM worker_assignments WHERE id=?", (assignment_id,)).fetchone())


def test_failed_repair_keeps_invalid_credential_and_local_results(tmp_path):
    with _hub_and_worker(tmp_path) as (hub, worker, _calls):
        worker.connect(URL, hub.pairing_code()["code"], "A 机")
        old_id = worker.connection["id"]
        _seed(worker, assignment_id="as-1", status="pending_upload", with_zip=True)
        hub.revoke_worker(old_id)
        worker.poll()
        assert worker.status()["credential_invalid"] is True

        before = json.loads(worker.credentials.read_text(encoding="utf-8"))
        # 配对失败不得清空失效凭证，也不得动本机队列。
        with pytest.raises(ValueError, match="工作台拒绝配对"):
            worker.connect(URL, "bad-code", "A 机")

        assert worker.connection["invalid"] is True
        assert worker.connection["id"] == old_id
        assert json.loads(worker.credentials.read_text(encoding="utf-8")) == before
        assert _row(worker, "as-1")["status"] == "pending_upload"
        assert (worker.outbox / "as-1.zip").exists()


def test_successful_repair_preserves_old_results_without_uploading_them(tmp_path):
    with _hub_and_worker(tmp_path) as (hub, worker, calls):
        worker.connect(URL, hub.pairing_code()["code"], "A 机")
        old_id = worker.connection["id"]
        # 旧版本数据库里的分配没有归属列，重新配对时应显式归属到旧身份。
        _seed(worker, assignment_id="as-old", status="pending_upload", worker_id="", with_zip=True)
        hub.revoke_worker(old_id)
        worker.poll()
        assert worker.status()["credential_invalid"] is True

        worker.connect(URL, hub.pairing_code()["code"], "A 机")
        assert worker.connection["id"] != old_id
        status = worker.status()
        assert status["orphaned_pending"] == 1
        assert "无法再用新凭证补传" in status["notice"]
        assert _row(worker, "as-old")["worker_id"] == old_id
        assert _row(worker, "as-old")["status"] == "reclaimed"
        assert (worker.outbox / "as-old.zip").exists()

        calls.clear()
        worker.poll()
        # 新凭证绝不尝试上传旧身份的结果，也不会无限重试必然被拒绝的请求。
        assert not any(method == "PUT" for method, _path in calls)
        assert _row(worker, "as-old")["status"] == "reclaimed"


def test_late_sync_response_from_old_credential_is_ignored(tmp_path):
    with _hub_and_worker(tmp_path) as (hub, worker, _calls):
        worker.connect(URL, hub.pairing_code()["code"], "A 机")
        old_connection = worker.connection
        _seed(worker, assignment_id="as-ready", status="ready", worker_id=old_connection["id"])
        new_connection = {"url": URL, "token": "new-token", "name": "B 机", "id": "b" * 32}

        def late_success(url, **_kwargs):
            # 模拟轮询请求在途时完成了重新配对。
            worker.connection = new_connection
            return httpx.Response(200, json={"assignments": [], "states": {}, "batches": [],
                                             "supports_run_summary": False, "supports_software_status": False},
                                  request=httpx.Request("POST", url))

        worker._http = SimpleNamespace(post=late_success)
        assert worker._heartbeat(old_connection) == set()

        # 旧凭证的迟到成功响应不能改写新连接的任务状态。
        assert worker.connection is new_connection
        assert _row(worker, "as-ready")["status"] == "ready"
        assert worker.connection.get("invalid") is None


def test_late_poll_failure_does_not_invalidate_new_credential(tmp_path):
    with _hub_and_worker(tmp_path) as (hub, worker, _calls):
        worker.connect(URL, hub.pairing_code()["code"], "A 机")
        new_connection = {"url": URL, "token": "new-token", "name": "B 机", "id": "b" * 32}

        def late_failure(_connection):
            worker.connection = new_connection
            raise httpx.ConnectError("stale request failed")

        worker._heartbeat = late_failure
        worker.poll()

        assert worker.connection is new_connection
        assert not worker.connection.get("invalid")
        assert worker.online is True


def test_result_rejected_for_old_identity_stops_retry_and_keeps_zip(tmp_path):
    with _hub_and_worker(tmp_path) as (hub, worker, _calls):
        worker.connect(URL, hub.pairing_code()["code"], "A 机")
        _seed(worker, assignment_id="as-foreign", status="pending_upload",
              worker_id=worker.connection["id"], with_zip=True)

        def rejected(url, **_kwargs):
            return httpx.Response(401, json={"detail": "分配不属于此采集端"},
                                  request=httpx.Request("PUT", url))

        worker._http = SimpleNamespace(put=rejected)
        worker._upload_pending(worker.connection)

        assert _row(worker, "as-foreign")["status"] == "reclaimed"
        assert (worker.outbox / "as-foreign.zip").exists()
        assert "旧设备身份" in worker.error
