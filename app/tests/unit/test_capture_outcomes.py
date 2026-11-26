"""实际 HTTP 验证三种采集结论、历史冻结与旧码重新发布保护。"""
from base64 import b64encode
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.api.server import create_app
from menu_capture.projects.service import LEGACY_ID, ensure_legacy_project


STAMP = "2026-11-20T12:00:00+08:00"
AUTH = {"Authorization": "Basic " + b64encode(b"admin:isolated-test").decode()}


def seed_batch(app, snap, batch_id, version):
    with app.state.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_legacy_project(db)
        db.execute("INSERT OR IGNORE INTO material_batches(id,name,source_kind,source_root,status,created_at,updated_at)"
                   " VALUES(?,?,?,?,?,?,?)",
                   (batch_id, batch_id, "local", str(app.state.materials.published_root), "completed", STAMP, STAMP))
        db.execute("UPDATE material_batches SET project_id=? WHERE id=?", (LEGACY_ID, batch_id))
        db.execute("INSERT INTO material_versions VALUES(?,?,?,?,?,?,?)",
                   (f"{batch_id}-{version}", batch_id, version, snap, "", STAMP, 1))
    root = app.state.materials.published_root / snap
    root.mkdir(parents=True, exist_ok=True)
    (root / ".materials-snapshot.json").write_text(json.dumps({"snapshot_id": snap, "stores": []}), encoding="utf-8")
    return root


def save_qr(root, name, color="white"):
    qr = root / name / "qr.png"
    qr.parent.mkdir(parents=True)
    Image.new("RGB", (20, 20), color).save(qr)
    return qr


def test_capture_history_preserves_rejected_and_review_when_later_run_succeeds(tmp_path):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    store = app.state.store
    first = seed_batch(app, "a"*64, "第一批", "v1")
    second = seed_batch(app, "b"*64, "第二批", "v2")
    first_path = save_qr(first, "E01 原店")
    save_qr(first, "E02 不合格")
    save_qr(second, "E01 不同批次")
    store.scan(first)
    store.scan(second)
    ids = {task["source_name"]: task["id"] for task in store.tasks()}
    success, rejected, review = (ids[name] for name in ("E01 原店", "E02 不合格", "E01 不同批次"))
    store.create_run("original", "automatic", [success, rejected, review])
    store.begin_publish(success, first, first_path.parent, first_path.parent / "output", first / "backup",
                        False, run_id="original")
    store.complete_capture(success, first_path.parent / "output", "已保存截图")
    store.update(rejected, capture_run_id="original", attempt_outcome="rejected",
                 status="needs_review", stage="manual_rejected", reject_reason="not_ordering")
    store.update(review, capture_run_id="original", attempt_outcome="review", status="needs_review",
                 message="第一轮网络异常")
    store.update_run("original", status="completed")
    store.create_run("retry", "automatic", [review])
    store.update(review, capture_run_id="retry", attempt_outcome="captured", status="captured")
    store.update_run("retry", status="terminated")

    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as client:
        history = client.get("/api/tasks/history?kind=capture").json()
        original = next(row for row in history["records"] if row["id"] == "original")
        counts = original["counts"]
        assert (counts["done"], counts["attention"], counts["rejected"], counts["unknown"]) == (1, 1, 1, 0)
        assert [(item["name"], item["versions"]) for item in original["batches"]] == [
            ("第一批", ["v1"]), ("第二批", ["v2"])]
        assert history["summary"]["terminated"] == 1
        assert len(client.get("/api/tasks/history?status=terminated").json()["records"]) == 1
        detail = client.get("/api/tasks/history/capture/original").json()
        outcomes = {row["task_id"]: row["status"] for row in detail["tasks"]}
        assert outcomes == {success: "captured", rejected: "rejected", review: "review"}
        assert not client.get("/api/reviews/pending").json()["records"]
        rejected_rows = client.get("/api/rejections?source=capture").json()["records"]
        assert [row["task_id"] for row in rejected_rows] == [rejected]


def test_same_qr_stays_frozen_across_publish_and_changed_qr_can_be_assigned(tmp_path):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    store = app.state.store
    name = "待修门店"
    first = seed_batch(app, "a"*64, "第一批", "v1")
    save_qr(first, name)
    store.scan(first)
    old_id = store.tasks()[0]["id"]
    store.update(old_id, status="blocked", stage="returned_to_materials")
    second = seed_batch(app, "b"*64, "第一批", "v2")
    save_qr(second, name)
    store.scan(second, reuse_sources=[first])
    frozen = app.state.materials.effective_capture_tasks()[0]
    assert frozen["stage"] == "returned_to_materials" and not frozen["runnable"]
    with store.connect() as db:
        db.execute("INSERT INTO shared_workers VALUES(?,?,?,?,?)", ("device", "采集机", "local-test", STAMP, 0))
    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as client:
        headers = {"X-Capture-Token": client.get("/api/session").json()["token"]}
        response = client.post("/api/shared/assignments", headers=headers,
                               json={"worker_id": "device", "task_ids": [frozen["id"]],
                                     "project_id": LEGACY_ID})
        assert response.status_code == 409
        assert client.post(f"/api/tasks/{frozen['id']}/retry", headers=headers,
                           json={"project_id": LEGACY_ID}).status_code == 409
        assert not client.get("/api/reviews/pending").json()["records"]
        third = seed_batch(app, "c"*64, "第一批", "v3")
        save_qr(third, name, color="black")
        store.scan(third, reuse_sources=[second, first])
        eligible = app.state.materials.effective_capture_tasks()[0]
        assert eligible["runnable"]
        response = client.post("/api/shared/assignments", headers=headers,
                               json={"worker_id": "device", "task_ids": [eligible["id"]],
                                     "project_id": LEGACY_ID})
        assert response.status_code == 200


def test_republish_keeps_non_captured_states_for_unchanged_qr(tmp_path):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    store = app.state.store
    first = seed_batch(app, "d"*64, "返工批次", "v1")
    for name in ("待检查门店", "不合格门店", "待重试门店", "不可采门店"):
        save_qr(first, name)
    store.scan(first)
    ids = {task["source_name"]: task["id"] for task in store.tasks()}
    store.update(ids["待检查门店"], status="needs_review", stage="captured", message="请人工复核")
    store.update(ids["不合格门店"], status="needs_review", reject_reason="not_ordering", rejected_at=STAMP)
    store.update(ids["待重试门店"], status="error", stage="capture", error_code="agent_timeout", message="超时")
    store.update(ids["不可采门店"], status="blocked", stage="queued", error_code="store_unavailable")
    second = seed_batch(app, "e"*64, "返工批次", "v2")
    for name in ("待检查门店", "不合格门店", "待重试门店", "不可采门店"):
        save_qr(second, name)
    store.scan(second, reuse_sources=[first])
    latest = {task["source_name"]: task for task in app.state.materials.effective_capture_tasks()}
    assert latest["待检查门店"]["status"] == "needs_review"
    assert latest["待检查门店"]["message"] == "请人工复核"
    assert latest["不合格门店"]["reject_reason"] == "not_ordering"
    assert latest["待重试门店"]["error_code"] == "agent_timeout"
    assert latest["不可采门店"]["status"] == "blocked"
    assert all(task["inherited_from"] == ids[name] for name, task in latest.items())
    # 二维码内容变化后必须重新排队，旧审核结论不能继续生效。
    third = seed_batch(app, "f"*64, "返工批次", "v3")
    for name in ("待检查门店", "不合格门店", "待重试门店", "不可采门店"):
        save_qr(third, name, color="black")
    store.scan(third, reuse_sources=[second, first])
    changed = {task["source_name"]: task for task in app.state.materials.effective_capture_tasks()}
    assert {task["status"] for task in changed.values()} == {"pending"}
    assert changed["不合格门店"]["reject_reason"] == ""


def test_review_scope_keeps_distinct_stores_with_same_display_prefix(tmp_path):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    one = seed_batch(app, "1"*64, "批次 A", "v1")
    two = seed_batch(app, "2"*64, "批次 B", "v1")
    save_qr(one, "E01 甲门店")
    save_qr(two, "E01 乙门店")
    for folder in (one, two):
        app.state.store.scan(folder)
    for row in app.state.store.tasks():
        app.state.store.update(row["id"], status="needs_review")
    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as client:
        response = client.get("/api/reviews/pending").json()
        assert response["summary"]["stores"] == 2


def test_hub_batch_scope_preserves_selection_and_conflicts_are_atomic(tmp_path):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    store = app.state.store
    for snap, batch, name in (("a"*64, "第一批", "甲门店"), ("b"*64, "第二批", "乙门店")):
        root = seed_batch(app, snap, batch, "v1")
        save_qr(root, name)
        store.scan(root)
    ids = {row["source_name"]: row["id"] for row in store.tasks()}
    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as client:
        headers = {"X-Capture-Token": client.get("/api/session").json()["token"]}

        def post(path, payload=None):
            return client.post(path, headers=headers, json=payload)

        def assign(device, tasks):
            return post("/api/shared/assignments", {"worker_id": device, "task_ids": tasks,
                                                     "project_id": LEGACY_ID})

        hub = app.state.shared_hub
        a = hub.pair(post("/api/shared/pairing").json()["code"], "A 机")
        b = hub.pair(post("/api/shared/pairing").json()["code"], "B 机")
        status = client.get("/api/status", params={
            "queue_page": 1, "queue_batch": "第二批", "selected": ids["甲门店"],
        }).json()
        queue = status["queue"]
        assert {row["id"] for row in queue["batches"]} == {"第一批", "第二批"}
        assert [row["id"] for row in status["tasks"]] == [ids["乙门店"]]
        assert [row["id"] for row in queue["selected"]] == [ids["甲门店"]]

        assert assign(a["id"], [ids["甲门店"]]).status_code == 200
        response = assign(b["id"], [ids["甲门店"], ids["乙门店"], "旧版本任务"])
        assert response.status_code == 409
        assert {row["task_id"]: row for row in response.json()["conflicts"]} == {
            ids["甲门店"]: {"task_id": ids["甲门店"], "reason": "assigned", "worker_name": "A 机"},
            "旧版本任务": {"task_id": "旧版本任务", "reason": "revision"},
        }
        assert len(hub.status()["assignments"]) == 1
        assert assign(b["id"], [ids["乙门店"]]).status_code == 200


def test_worker_reports_latest_run_and_can_fall_back_to_older_hub(tmp_path):
    import httpx

    hub_app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    worker_app = create_app(tmp_path / "worker", role="worker")
    hub, worker = hub_app.state.shared_hub, worker_app.state.shared_worker
    with hub.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_legacy_project(db)
    mode = "new"
    payloads = []
    with TestClient(hub_app, base_url="http://127.0.0.1", headers=AUTH) as client:
        def transport(request):
            payload = json.loads(request.read())
            if request.url.path.endswith("/sync"):
                payloads.append(payload)
            if mode == "old" and "run_summary" in payload:
                return httpx.Response(400, json={"detail": "同步参数无效"})
            result = client.request(request.method, request.url.path, json=payload,
                                 headers={"Authorization": request.headers["Authorization"]}
                                 if "Authorization" in request.headers else {})
            body = result.json()
            if mode == "old":
                body.pop("supports_run_summary", None)
                body.pop("supports_run_history", None)
            return httpx.Response(result.status_code, json=body)

        worker._http.close()
        worker._http = httpx.Client(transport=httpx.MockTransport(transport))
        worker.connect("http://127.0.0.1", hub.pairing_code()["code"], "A 机")
        worker.store.create_run("run", "automatic", ["a", "b"], capture_mode="auto")
        worker.store.update_run("run", status="running", current_index=1)
        worker.poll()
        worker.poll()
        # 此测试的旧轮次任务没有分配快照，后台不能把无归属摘要显示到任何项目。
        report = hub.status(project_id=LEGACY_ID)["workers"][0]["last_run"]
        assert report is None
        with hub.store.connect() as db:
            saved = db.execute("SELECT status,project_id FROM shared_worker_runs WHERE worker_id=?",
                               (worker.connection["id"],)).fetchone()
        assert saved["status"] == "running" and saved["project_id"] == ""

        mode = "old"
        worker.poll()
        assert "run_summary" in payloads[-2] and "run_summary" not in payloads[-1]
        assert worker.online
        mode = "new"
        worker.store.update_run("run", status="completed", current_index=2)
        worker.poll()
        worker.poll()
        report = hub.status(project_id=LEGACY_ID)["workers"][0]["last_run"]
        assert report is None


def test_device_profile_revoke_remove_and_paginated_records_keep_identity(tmp_path):
    app = create_app(tmp_path, role="hub", admin_password="isolated-test")
    hub, store = app.state.shared_hub, app.state.store
    root = seed_batch(app, "d"*64, "测试批次", "v1")
    save_qr(root, "甲门店")
    save_qr(root, "乙门店", color="black")
    store.scan(root)
    task_ids = [row["id"] for row in store.tasks()]
    device = hub.pair(hub.pairing_code()["code"], "旧名称")
    device_id = device["id"]
    route = f"/api/shared/workers/{device_id}"
    assigned = hub.assign(device_id, task_ids, project_id=LEGACY_ID)["assignments"]
    # 模拟已经有较多历史记录，设备任务列表仍可读到 200 条以前的记录。
    with store.connect() as db:
        db.executemany("""INSERT INTO shared_assignments(id,task_id,worker_id,batch_id,batch_name,store,revision,
            snapshot_id,source_hash,source_suffix,status,created_at,project_id,project_code,project_name)
            VALUES(:id,:task_id,:worker_id,:batch_id,:batch_name,:store,:revision,:snapshot_id,:source_hash,
             :source_suffix,'completed',:created_at,:project_id,:project_code,:project_name)""",
            (assigned[0] | {"id": f"{n:032x}", "store": f"历史门店{n}", "created_at": STAMP} for n in range(205)))

    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as client:
        headers = {"X-Capture-Token": client.get("/api/session").json()["token"]}
        updated = client.patch(route, headers=headers, json={"name": " 采集机 A ", "description": " 办公室 "})
        assert updated.json() == {"id": device_id, "name": "采集机 A", "description": "办公室"}
        device_status = hub.status()["workers"][0]
        assert device_status["id"] == device_id and device_status["paired_at"]
        assert client.post(route + "/revoke", headers=headers).status_code == 409
        assert client.post(route + "/archive", headers=headers).status_code == 409
        listing = client.get("/api/shared/assignments", params={
            "worker_id": device_id, "phase": "completed", "page": 7, "size": 30,
            "project_id": LEGACY_ID,
        }).json()
        assert listing["total"] == 205 and len(listing["records"]) == 25
        assert {row["worker_name"] for row in listing["records"]} == {"采集机 A"}
        matches = client.get("/api/shared/assignments", params={"worker_id": device_id, "q": "乙门店",
                                                                "project_id": LEGACY_ID}).json()
        assert matches["total"] == 1

        batch = "/api/shared/assignments/reclaim"
        ids = [row["id"] for row in assigned]
        malformed = client.post(batch, headers=headers, json={"assignment_ids": [ids[0], "not-an-id"],
                                                              "project_id": LEGACY_ID})
        assert malformed.status_code == 400
        mixed = client.post(batch, headers=headers, json={"assignment_ids": [ids[0], "f"*32],
                                                          "project_id": LEGACY_ID})
        assert mixed.status_code == 409
        # 项目范围内批量回收采用整批校验；未知 ID 与越项目 ID 一样不暴露部分结果，也不部分写入。
        assert sum(row["status"] in {"assigned", "ready"} for row in hub.status()["assignments"]) == 2
        first_reclaimed = client.post(batch, headers=headers, json={"assignment_ids": [ids[0]],
                                                                    "project_id": LEGACY_ID}).json()
        assert first_reclaimed["count"] == 1
        assert first_reclaimed["results"][0]["status"] == "reclaimed"
        assert sum(row["status"] in {"assigned", "ready"} for row in hub.status()["assignments"]) == 1
        reclaimed = client.post(batch, headers=headers, json={"assignment_ids": ids,
                                                             "project_id": LEGACY_ID}).json()
        assert reclaimed["count"] == 1 and {row["id"] for row in reclaimed["tasks"]} == {assigned[1]["task_id"]}
        assert next(item for item in reclaimed["results"] if item["id"] == ids[0])["status"] == "failed"
        revoked = client.post(route + "/revoke", headers=headers)
        assert revoked.json()["revoked_at"]
        identity = {"Authorization": "Bearer " + device["token"]}
        assert client.post("/api/shared/worker/sync", headers=identity,
                           json={"ready_ids": [], "pending_uploads": 0}).status_code == 401
        assert client.post("/api/shared/assignments", headers=headers,
                           json={"worker_id": device_id, "task_ids": task_ids,
                                 "project_id": LEGACY_ID}).status_code == 409
        assert client.post(route + "/archive", headers=headers).json()["archived_at"]
        records = client.get("/api/shared/assignments", params={"worker_id": device_id, "phase": "reclaimed",
                                                                 "project_id": LEGACY_ID}).json()
        assert {row["id"] for row in records["records"]} == set(ids)


def test_qr_cache_reuses_verified_bytes_but_refreshes_corrupt_or_changed_qr(tmp_path):
    import httpx

    hub_app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    worker_app = create_app(tmp_path / "worker", role="worker")
    hub, worker = hub_app.state.shared_hub, worker_app.state.shared_worker
    first = seed_batch(hub_app, "e"*64, "缓存批次", "v1")
    save_qr(first, "甲门店")
    hub_app.state.store.scan(first)
    task_id = hub_app.state.store.tasks()[0]["id"]
    downloads = []
    with TestClient(hub_app, base_url="http://127.0.0.1", headers=AUTH) as client:
        def transport(request):
            identity = {"Authorization": request.headers["Authorization"]} if "Authorization" in request.headers else {}
            if request.method == "GET":
                downloads.append(request.url.path)
                result = client.get(request.url.path, headers=identity)
            else:
                result = client.post(request.url.path, json=json.loads(request.read()), headers=identity)
            return httpx.Response(result.status_code, content=result.content)

        worker._http.close()
        worker._http = httpx.Client(transport=httpx.MockTransport(transport))
        worker.connect("http://127.0.0.1", hub.pairing_code()["code"], "旧名称")
        identifier = worker.connection["id"]
        first_assignment = hub.assign(identifier, [task_id], project_id=LEGACY_ID)["assignments"][0]
        worker.poll()
        assert len(downloads) == 1 and worker.source_root(task_id)
        hub.edit_worker(identifier, "新名称", "")
        hub.reclaim(first_assignment["id"])
        second_assignment = hub.assign(identifier, [task_id], project_id=LEGACY_ID)["assignments"][0]
        worker.poll()
        assert second_assignment["id"] != first_assignment["id"]
        assert len(downloads) == 1 and worker.source_root(task_id)
        assert worker.status()["name"] == "新名称"

        for cached in (first_assignment, second_assignment):
            worker._source_path(cached).write_bytes(b"corrupt")
        hub.reclaim(second_assignment["id"])
        third_assignment = hub.assign(identifier, [task_id], project_id=LEGACY_ID)["assignments"][0]
        worker.poll()
        assert len(downloads) == 2
        assert worker._source_path(third_assignment).read_bytes() == (first / "甲门店/qr.png").read_bytes()

        hub.reclaim(third_assignment["id"])
        second = seed_batch(hub_app, "f"*64, "缓存批次", "v2")
        new_file = save_qr(second, "甲门店", color="black")
        hub_app.state.store.scan(second, reuse_sources=[first])
        new_id = hub_app.state.materials.effective_capture_tasks()[0]["id"]
        fourth_assignment = hub.assign(identifier, [new_id], project_id=LEGACY_ID)["assignments"][0]
        worker.poll()
        assert len(downloads) == 3
        assert worker._source_path(fourth_assignment).read_bytes() == new_file.read_bytes()
        hub.reclaim(fourth_assignment["id"])
        hub.revoke_worker(identifier)
        worker.poll()
        assert worker.status()["credential_invalid"]
        assert worker.credentials.exists()
        assert worker._source_path(fourth_assignment).is_file()


def test_invalid_credential_can_pair_again_while_valid_one_is_rejected(tmp_path):
    import httpx

    hub_app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    worker_app = create_app(tmp_path / "worker", role="worker")
    hub, worker = hub_app.state.shared_hub, worker_app.state.shared_worker
    with TestClient(hub_app, base_url="http://127.0.0.1", headers=AUTH) as client:
        def transport(request):
            identity = {"Authorization": request.headers["Authorization"]} if "Authorization" in request.headers else {}
            if request.method == "GET":
                result = client.get(request.url.path, headers=identity)
            else:
                result = client.post(request.url.path, json=json.loads(request.read()), headers=identity)
            return httpx.Response(result.status_code, content=result.content)

        worker._http.close()
        worker._http = httpx.Client(transport=httpx.MockTransport(transport))
        url = "http://127.0.0.1"
        worker.connect(url, hub.pairing_code()["code"], "A 机")
        first_identifier = worker.connection["id"]
        # 有效凭证仍不允许覆盖配对，避免误操作把正常采集端换绑。
        with pytest.raises(ValueError, match="此采集端已配对"):
            worker.connect(url, hub.pairing_code()["code"], "A 机")

        hub.revoke_worker(first_identifier)
        worker.poll()
        assert worker.status()["credential_invalid"]
        assert worker.credentials.exists()

        # 失效凭证在重新配对成功后才被原子替换；失败时保留原状态便于排查。
        worker.connect(url, hub.pairing_code()["code"], "A 机")
        status = worker.status()
        assert status["paired"] is True and status["credential_invalid"] is False
        assert worker.connection["id"] != first_identifier
        assert json.loads(worker.credentials.read_text(encoding="utf-8"))["token"] == worker.connection["token"]
