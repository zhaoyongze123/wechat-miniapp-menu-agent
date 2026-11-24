"""共享工作台分配、断网恢复和版本并发的服务端集成验证。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import httpx
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from menu_capture.server import create_app


ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:isolated-test").decode()}


def client_for(app, *, admin=False):
    return TestClient(app, base_url="http://127.0.0.1", headers=ADMIN if admin else {})


def session(client):
    return {"x-capture-token": client.get("/api/session").json()["token"]}


def png(path: Path, color="white") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 160), color).save(path)
    return path


def batch(app, folder, names, *, publish_names=None):
    materials = app.state.materials
    materials.inspector = lambda _path: [
        {"box": {"x": 30, "y": 20, "width": 100, "height": 100},
         "payload": "https://wxaurl.cn/TEST"}
    ]
    materials.ocr_reader = lambda _path: []
    for index, name in enumerate(names):
        png(folder / name / "code.png", ("red", "green", "blue", "white")[index])
    entry = materials.create_local_batch(folder)
    batch_id = entry["batch_id"]
    materials.start_analysis(batch_id)
    from test_materials import _wait
    finished = _wait(materials, batch_id)
    assert finished["status"] == "completed", finished
    for item in finished["stores"]:
        if publish_names is None or item["source_name"] in publish_names:
            materials.decide_store(batch_id, item["store_id"], "confirm", image_index=0, candidate_index=0)
    materials.publish_snapshot(batch_id, description="首发", activate=False)
    return finished


class Link:
    """模拟一条可中断的 HTTP 链路，后台接口及两个工作目录使用真实 SQLite。"""

    def __init__(self, hub):
        self.hub = hub
        self.available = True
        self.drop_result_response = False

    def request(self, request: httpx.Request):
        if not self.available:
            raise httpx.ConnectError("测试断网", request=request)
        response = self.hub.request(request.method, str(request.url), headers=request.headers, content=request.read())
        if request.method == "PUT" and response.is_success and self.drop_result_response:
            self.drop_result_response = False
            raise httpx.ConnectError("后台已写入但响应丢失", request=request)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    def install(self, worker):
        worker._http.close()
        worker._http = httpx.Client(transport=httpx.MockTransport(self.request))


def pair(hub, worker, name, link):
    link.install(worker)
    code = hub.post("/api/shared/pairing", headers=session(hub)).json()["code"]
    worker.connect("http://127.0.0.1", code, name)
    return worker.connection["id"]


def assign(hub, worker_id, tasks):
    return hub.post("/api/shared/assignments", headers=session(hub),
                    json={"worker_id": worker_id, "task_ids": tasks})


def fake_store_result(worker, task_id, output):
    png(output / "page-01.png", "green")
    worker.store.complete_capture(task_id, output, "已采集 1 张")


def test_cached_source_without_assignment_is_registered_again(tmp_path):
    hub_app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    worker_app = create_app(tmp_path / "computer", role="worker")
    worker = worker_app.state.shared_worker
    with client_for(hub_app, admin=True) as hub:
        batch(hub_app, tmp_path / "raw", ("门店 A",))
        task = hub.get("/api/capture/queue").json()["records"][0]
        worker_id = pair(hub, worker, "电脑 A", Link(hub))
        assigned = assign(hub, worker_id, [task["id"]]).json()["assignments"][0]
        worker.poll()

        cached_path = worker._source_path(assigned)
        assert cached_path.is_file()
        # 模拟缓存已落盘但本地 assignment 注册记录缺失，例如首次同步中途退出。
        with worker.store.connect() as db:
            db.execute("DELETE FROM worker_assignments WHERE id=?", (assigned["id"],))

        worker._ensure_source(assigned)

        with worker.store.connect() as db:
            local = db.execute(
                "SELECT status FROM worker_assignments WHERE id=?", (assigned["id"],)
            ).fetchone()
        assert local is not None and local["status"] == "ready"
        assert worker.store.get(task["id"])["source_path"] == str(cached_path)
    worker.close()


def test_new_assignment_waits_for_old_run_before_replacing_task_path(tmp_path):
    hub_app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    worker_app = create_app(tmp_path / "computer", role="worker")
    worker = worker_app.state.shared_worker
    with client_for(hub_app, admin=True) as hub:
        batch(hub_app, tmp_path / "raw", ("门店 A",))
        task = hub.get("/api/capture/queue").json()["records"][0]
        worker_id = pair(hub, worker, "电脑 A", Link(hub))
        old = assign(hub, worker_id, [task["id"]]).json()["assignments"][0]
        worker.poll()
        old_path = worker.store.get(task["id"])["source_path"]
        worker.store.create_run("old-run", "automatic", [task["id"]])

        reclaimed = hub.post(
            "/api/shared/assignments/" + old["id"] + "/reclaim", headers=session(hub)
        )
        assert reclaimed.status_code == 200
        fresh = assign(hub, worker_id, [task["id"]]).json()["assignments"][0]
        assert fresh["id"] != old["id"]

        worker.poll()
        assert worker.store.get(task["id"])["source_path"] == old_path
        with worker.store.connect() as db:
            assert db.execute(
                "SELECT 1 FROM worker_assignments WHERE id=?", (fresh["id"],)
            ).fetchone() is None
        assert worker._source_path(fresh).is_file()

        worker.store.update_run("old-run", status="completed")
        worker.poll()

        with worker.store.connect() as db:
            local = db.execute(
                "SELECT status FROM worker_assignments WHERE id=?", (fresh["id"],)
            ).fetchone()
        assert local is not None and local["status"] == "ready"
        assert worker.store.get(task["id"])["source_path"] == str(worker._source_path(fresh))
    worker.close()


def test_two_computers_continue_multiple_stores_offline_and_retry_after_restart(tmp_path):
    hub_app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    b_root = tmp_path / "computer-b"
    b_app = create_app(b_root, role="worker")
    a_app = create_app(tmp_path / "computer-a", role="worker")
    b = b_app.state.shared_worker
    with client_for(hub_app, admin=True) as hub:
        batch(hub_app, tmp_path / "raw-x", ("门店 A", "门店 B"))
        batch(hub_app, tmp_path / "raw-y", ("门店 C",))
        records = hub.get("/api/capture/queue").json()["records"]
        ids = {task["source_name"]: task["id"] for task in records}
        a = a_app.state.shared_worker
        a_id = pair(hub, a, "电脑 A", Link(hub))
        link = Link(hub)
        b_id = pair(hub, b, "电脑 B", link)
        assert assign(hub, a_id, [ids["门店 A"]]).status_code == 200
        assert assign(hub, b_id, [ids["门店 A"], ids["门店 C"]]).status_code == 409
        assert len(hub.get("/api/shared/status").json()["assignments"]) == 1
        remote_tasks = [ids["门店 B"], ids["门店 C"]]
        assert assign(hub, b_id, remote_tasks).status_code == 200
        a.poll()
        b.poll()
        b.poll()
        assert all(b.source_root(task_id) for task_id in remote_tasks)
        worker_view = client_for(b_app).get("/api/status").json()
        assert {row["id"] for row in worker_view["tasks"]} == set(remote_tasks)
        assert not a.source_root(ids["门店 B"])
        target = next(row for row in hub.get("/api/shared/status").json()["assignments"] if row["worker_id"] == b_id)
        unauthenticated = client_for(hub_app)
        assert unauthenticated.get("/").status_code == 401
        assert unauthenticated.get("/healthz").status_code == 200
        assert hub.post("/api/capture/start", headers=session(hub),
                        json={"mode": "manual", "task_ids": remote_tasks}).status_code == 409
        forbidden = hub.get(f"/api/shared/worker/assignments/{target['id']}/source", headers=a._headers)
        assert forbidden.status_code == 401

        link.available = False
        b.check_start(remote_tasks)
        b.store.create_run("offline-run", "automatic", remote_tasks, capture_mode="manual")
        for index, task_id in enumerate(remote_tasks, 1):
            fake_store_result(b, task_id, b_root / "output" / task_id)
            b.store.update_run("offline-run", current_index=index)
            b.poll()
        assert b.status()["pending_uploads"] == 2 and not b.status()["online"]
        assert all(hub_app.state.store.get(task_id)["status"] == "pending" for task_id in remote_tasks)
        b.close()

        restarted = create_app(b_root, role="worker").state.shared_worker
        link.install(restarted)
        assert restarted.status()["pending_uploads"] == 2
        link.available = True
        link.drop_result_response = True
        restarted.poll()
        assert restarted.status()["pending_uploads"] == 2  # 第一个结果已落库，但响应丢失。
        restarted.poll()
        assert restarted.status()["pending_uploads"] == 0 and restarted.status()["online"]
        for task_id in remote_tasks:
            task = hub_app.state.store.get(task_id)
            assert task["status"] == "captured"
            assert hub.get(f"/api/artifact/{task_id}/page-01.png").status_code == 200
        with hub_app.state.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM shared_results").fetchone()[0] == 2
        restarted.close()
        a.close()


def result_zip(assignment, status="needs_review", reason="store_closed"):
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("result.json", json.dumps({
            "task_id": assignment["task_id"], "snapshot_id": assignment["snapshot_id"],
            "source_hash": assignment["source_hash"], "status": status, "reject_reason": reason,
            "stage": "manual_rejected", "message": "已判定不合格",
        }))
        evidence = io.BytesIO()
        Image.new("RGB", (10, 10), "white").save(evidence, format="PNG")
        archive.writestr("evidence.png", evidence.getvalue())
    return data.getvalue()


def test_rework_rejects_active_store_allows_other_store_and_keeps_late_result_history(tmp_path):
    app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    first = create_app(tmp_path / "first", role="worker").state.shared_worker
    second = create_app(tmp_path / "second", role="worker").state.shared_worker
    with client_for(app, admin=True) as hub:
        release = batch(app, tmp_path / "raw", ("门店 A", "门店 B"), publish_names={"门店 A"})
        batch_id = release["batch_id"]
        tasks = {row["source_name"]: row for row in app.state.materials.effective_capture_tasks()}
        stores = {row["source_name"]: row["store_id"] for row in release["stores"]}
        first_id, second_id = pair(hub, first, "电脑 A", Link(hub)), pair(hub, second, "电脑 B", Link(hub))
        task_id = tasks["门店 A"]["id"]
        # 两台同时尝试领取同一家店，只有一条生效。
        with ThreadPoolExecutor(max_workers=2) as pool:
            a_call = pool.submit(assign, hub, first_id, [task_id])
            b_call = pool.submit(assign, hub, second_id, [task_id])
        replies = [a_call.result(), b_call.result()]
        assert sorted(reply.status_code for reply in replies) == [200, 409]
        owner = first if replies[0].status_code == 200 else second
        other = second if owner is first else first
        old = next(reply.json()["assignments"][0] for reply in replies if reply.status_code == 200)

        context = session(hub)
        active_rework = hub.post("/api/materials/rework/publish", headers=context,
                                  json={"store_ids": [f"{batch_id}:{stores['门店 A']}"]}).json()
        assert active_rework["results"] == [] and "仍由采集端占用" in active_rework["errors"][0]["error"]
        # 门店 B 换版可照常进行，门店 A 的原始分配继续独占。
        app.state.materials.decide_store(batch_id, stores["门店 B"], "confirm", image_index=0, candidate_index=0)
        other_rework = hub.post("/api/materials/rework/publish", headers=context,
                                 json={"store_ids": [f"{batch_id}:{stores['门店 B']}"]}).json()
        assert other_rework["results"] and other_rework["errors"] == []
        current_id = next(row["id"] for row in app.state.materials.effective_capture_tasks()
                          if row["source_name"] == "门店 A")
        assert assign(hub, other.connection["id"], [current_id]).status_code == 409
        assert hub.post(f"/api/shared/assignments/{old['id']}/reclaim", headers=context).status_code == 200
        fresh = assign(hub, other.connection["id"], [current_id]).json()["assignments"][0]
        assert fresh["id"] != old["id"]

        def upload(worker, entry, content):
            return hub.put(f"/api/shared/worker/assignments/{entry['id']}/result",
                           headers=worker._headers | {"Content-Type": "application/zip"}, content=content)

        empty = upload(owner, old, b"not a zip")
        assert empty.status_code == 400
        late = result_zip(old)
        assert upload(owner, old, late).json()["disposition"] == "history"
        assert upload(owner, old, late).json()["disposition"] == "history"
        assert app.state.store.get(current_id)["status"] == "pending"
        assert hub.get(f"/api/shared/results/{old['id']}/files/evidence.png").status_code == 200
        assert upload(other, fresh, result_zip(fresh)).json()["disposition"] == "accepted"
        assert app.state.store.get(current_id)["reject_reason"] == "store_closed"
        assert hub.get(f"/api/rejections/tasks/{current_id}/evidence").status_code == 200
    first.close()
    second.close()


@pytest.mark.parametrize("filename", ["../escape.png", "artifacts/../escape.png", "artifacts/a\\b.png"])
def test_result_archive_rejects_invalid_paths(tmp_path, filename):
    hub = create_app(tmp_path, role="hub", admin_password="isolated-test").state.shared_hub
    archive = tmp_path / "input.zip"
    with ZipFile(archive, "w") as output:
        entry = ZipInfo(filename)
        entry.filename = filename  # 伪造真实入站 ZIP，保留 Windows 写包时默认换掉的分隔符。
        output.writestr(entry, "not an image")
    with pytest.raises(ValueError, match="文件名"):
        hub._unpack(archive, tmp_path / "unpack")


def test_bulk_rework_publishes_each_original_batch_and_inherits_untouched_results(tmp_path):
    app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    worker = create_app(tmp_path / "computer", role="worker").state.shared_worker
    with client_for(app, admin=True) as hub:
        batches = [batch(app, tmp_path / f"raw-{i}", (f"已有店 {i}", f"返工店 {i}"),
                         publish_names={f"已有店 {i}"}) for i in (1, 2)]
        materials = app.state.materials
        original = materials.effective_capture_tasks()
        for entry in original:
            output = tmp_path / "saved" / entry["id"]
            png(output / "page-01.png")
            app.state.store.complete_capture(entry["id"], output, "已有有效结果")
        selected = []
        context = session(hub)
        for entry in batches:
            batch_id = entry["batch_id"]
            item = next(store for store in entry["stores"] if store["source_name"].startswith("返工"))
            store_id = item["store_id"]
            materials.decide_store(batch_id, store_id, "skip", reject_reason="not_ordering")
            returned = hub.post("/api/materials/rework/return", headers=context,
                                json={"batch_id": batch_id, "store_id": store_id})
            assert returned.status_code == 200
            materials.decide_store(batch_id, store_id, "confirm", image_index=0, candidate_index=0)
            selected.append(f"{batch_id}:{store_id}")
        published = hub.post("/api/materials/rework/publish", headers=context, json={"store_ids": selected}).json()
        assert not published["errors"] and len(published["results"]) == 2
        records = hub.get("/api/capture/queue?batch_ids=all").json()["records"]
        expected = {entry["batch_id"] for entry in batches}
        assert {row["batch_id"] for row in records} == expected
        inherited = {row["inherited_from"] for row in records if row["status"] == "captured"}
        assert inherited == {row["id"] for row in original}
        new_tasks = [row["id"] for row in records if row["runnable"]]
        assert len(new_tasks) == 2
        paired = pair(hub, worker, "复采", Link(hub))
        dispatched = assign(hub, paired, new_tasks).json()["assignments"]
        assert {row["batch_id"] for row in dispatched} == expected
    worker.close()


def test_legacy_windows_backup_is_verified_without_changing_source(tmp_path):
    backup_home = tmp_path / "legacy"
    data = backup_home / "data"
    outputs = tmp_path / "legacy-output"
    snapshot = "a" * 64
    legacy_home = r"D:\menu-agent\UFS_Menu_Scraper\macos_runner"
    legacy_output = r"D:\门店截图"
    task = png(data / "materials" / "published" / snapshot / "门店 A" / "二维码.png")
    screenshot = png(outputs / "门店 A" / "page-01.png")
    digest = hashlib.sha256(task.read_bytes()).hexdigest()
    marker = task.parent.parent / ".materials-snapshot.json"
    marker.write_text(json.dumps({
        "snapshot_id": snapshot,
        "artifacts": [{"path": "门店 A/二维码.png", "sha256": digest}],
    }), encoding="utf-8")
    source_path = legacy_home + "\\data\\materials\\published\\" + snapshot + "\\门店 A\\二维码.png"
    with sqlite3.connect(data / "tasks.sqlite3") as db:
        db.executescript("""
            CREATE TABLE tasks (source_path,source_hash,output_dir,status);
            CREATE TABLE material_batches (id,deleted_at,source_root,summary_json,manifest_json);
            CREATE TABLE material_versions (snapshot_id,batch_id);
        """)
        db.execute("INSERT INTO tasks VALUES(?,?,?,?)",
                   (source_path, digest, legacy_output + "\\门店 A", "captured"))
        db.execute("INSERT INTO material_batches VALUES(?,?,?,?,?)",
                   ("batch-1", "", legacy_home + "\\data", "{}", "{}"))
        db.execute("INSERT INTO material_versions VALUES(?,?)", (snapshot, "batch-1"))
    src_hash = hashlib.sha256((data / "tasks.sqlite3").read_bytes()).hexdigest()
    script = Path(__file__).resolve().parents[3] / "deploy/migrate_legacy.py"
    destination = tmp_path / "converted"
    command = [sys.executable, str(script), "--backup-home", str(backup_home), "--backup-output", str(outputs),
               "--legacy-home", legacy_home, "--legacy-output", legacy_output, "--destination", str(destination)]
    rehearsal = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True)
    report = json.loads(rehearsal.stdout)
    assert report["rows"] == {"tasks": 1, "material_batches": 1, "material_versions": 1}
    assert report["checked_qr_files"] == report["captured_screenshot_files"] == 1
    assert (destination / "output" / "门店 A" / "page-01.png").read_bytes() == screenshot.read_bytes()
    assert hashlib.sha256((data / "tasks.sqlite3").read_bytes()).hexdigest() == src_hash


def test_run_history_keeps_device_identity_and_recovers_after_restart(tmp_path):
    hub_app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    a_root = tmp_path / "computer-a"
    a = create_app(a_root, role="worker").state.shared_worker
    b = create_app(tmp_path / "computer-b", role="worker").state.shared_worker
    with client_for(hub_app, admin=True) as hub:
        a_link = Link(hub)
        a_id = pair(hub, a, "电脑 A", a_link)
        b_id = pair(hub, b, "电脑 B", Link(hub))
        a.store.create_run("same-id", "automatic", [], capture_mode="auto", name="第一轮")
        b.store.create_run("same-id", "automatic", [], capture_mode="manual", name="第二轮")
        a_report = a.history.pending_report()
        a.poll()
        b.poll()

        result = hub.get("/api/tasks/history", params={"kind": "capture"}).json()
        assert {row["id"] for row in result["records"]} == {f"{a_id}:same-id", f"{b_id}:same-id"}
        assert result["summary"]["total"] == result["summary"]["running"] == 2

        a_link.available = False
        a.store.update_run("same-id", status="completed")
        a.store.rename_run("same-id", "离线改名")
        a.poll()
        assert a.history.pending_report()["run"]["status"] == "completed"
        a.close()

        a = create_app(a_root, role="worker").state.shared_worker
        a_link.available = True
        a_link.install(a)
        a.poll()
        assert a.history.pending_report() is None

        old_retry = hub.put("/api/shared/worker/run-history", headers=a._headers, json=a_report)
        assert old_retry.status_code == 200
        assert hub.put("/api/shared/worker/run-history", json=a_report).status_code == 401

        filtered = hub.get("/api/tasks/history", params={"kind": "capture", "worker_id": a_id}).json()
        assert filtered["summary"]["total"] == filtered["summary"]["completed"] == 1
        detail_url = f"/api/tasks/history/capture/{a_id}:same-id"
        detail = hub.get(detail_url).json()
        assert detail["title"] == "离线改名"
        assert detail["worker_name"] == "电脑 A"
        assert detail["can_rename"] is False
        assert detail["synced_at"]

        hub_app.state.shared_hub.edit_worker(a_id, "电脑 A 改名", "")
        assert hub.get(detail_url).json()["worker_name"] == "电脑 A 改名"
    a.close()
    b.close()


def test_run_history_preserves_per_store_outcomes(tmp_path):
    hub_app = create_app(tmp_path / "hub", role="hub", admin_password="isolated-test")
    worker_app = create_app(tmp_path / "worker", role="worker")
    worker = worker_app.state.shared_worker
    with client_for(hub_app, admin=True) as hub:
        created = batch(hub_app, tmp_path / "raw", ("门店 A", "门店 B"))
        ids = [row["id"] for row in hub.get("/api/capture/queue").json()["records"]]
        worker_id = pair(hub, worker, "电脑 A", Link(hub))
        assert assign(hub, worker_id, ids).status_code == 200
        worker.poll()
        worker.store.create_run("test-run", "automatic", ids, capture_mode="auto")
        worker.store.update(ids[0], capture_run_id="test-run", attempt_outcome="review",
                            status="needs_review", message="需要人工复核")
        worker.store.update_run("test-run", current_index=1)
        worker.poll()

        detail = hub.get(f"/api/tasks/history/capture/{worker_id}:test-run").json()
        assert detail["counts"]["processed"] == 1
        assert detail["counts"]["attention"] == 1
        assert detail["batches"][0]["id"] == created["batch_id"]
        results = {row["task_id"]: row for row in detail["tasks"]}
        assert results[ids[0]]["status"] == "review"
        assert results[ids[1]]["label"] == "未处理"
        materials = hub.get("/api/tasks/history", params={"kind": "materials"}).json()["records"]
        assert materials and all(row["kind"] == "materials" for row in materials)
        assert hub.get("/api/tasks/active").json()["tasks"][-1]["id"] == f"{worker_id}:test-run"
    worker.close()
