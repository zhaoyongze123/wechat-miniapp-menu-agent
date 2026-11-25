"""店铺名称伴随提取：协议事件、独立持久化与共享结果装配。"""

from __future__ import annotations

import json
import threading
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image

from menu_capture.agent.ui_tars_trial import _record_observation
from menu_capture.api.server import create_app
from menu_capture.core.config import ConfigStore
from menu_capture.materials.materials import MaterialService
from menu_capture.materials.rejections import RejectionService
from menu_capture.shared.shared import SharedHub, _observed_store_name
from menu_capture.shared.shared_worker import SharedWorker
from menu_capture.storage.store import TaskStore


def test_observations_are_isolated_per_run_and_survive_revoke(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")

    store.record_observed_store_name("run-1", "task-1", "老王饭店", frame_id="2", frame="ui-tars-002.png")
    store.record_observed_store_name("run-1", "task-1", "老王饭店（二店）", frame_id="5", frame="ui-tars-005.png")
    store.record_observed_store_name("run-1", "task-2", "")
    store.record_observed_store_name("run-2", "task-1", "换版后的店名")
    # 空 run_id 不属于任何采集轮次，不落库。
    store.record_observed_store_name("", "task-1", "手工试验")

    assert store.observed_store_name("run-1", "task-1") == "老王饭店（二店）"
    assert store.observed_store_names("run-1") == {"task-1": "老王饭店（二店）"}
    # 新轮次不会继承旧轮次的名称。
    assert store.observed_store_name("run-2", "task-1") == "换版后的店名"
    latest = store.observed_store_name_latest("task-1")
    assert latest["run_id"] == "run-2" and latest["store_name"] == "换版后的店名"
    assert latest["frame"] == ""

    # 显式撤销保留空值行，但不再出现在本轮结果里。
    store.record_observed_store_name("run-1", "task-1", "")
    assert store.observed_store_name("run-1", "task-1") == ""
    assert store.observed_store_names("run-1") == {}


def test_record_accessor_keeps_name_and_frame_together(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.record_observed_store_name("run-1", "task-1", "老王饭店", frame_id="3", frame="ui-tars-003.png")

    record = store.observed_store_name_record("run-1", "task-1")
    assert (record["store_name"], record["frame_id"], record["frame"]) == ("老王饭店", "3", "ui-tars-003.png")
    assert store.observed_store_name_record("run-1", "missing") == {}


def test_observation_events_never_disturb_navigation() -> None:
    received: list[tuple[str, str]] = []
    _record_observation(lambda name, frame: received.append((name, frame)),
                        {"name": "老王饭店", "frame_id": "3"})
    assert received == [("老王饭店", "3")]
    # 旧协议、类型错误与回调异常都不能影响主动作。
    _record_observation(lambda name, frame: received.append((name, frame)), {"frame_id": "3"})
    _record_observation(lambda name, frame: received.append((name, frame)), {"name": 42})
    _record_observation(lambda name, frame: (_ for _ in ()).throw(RuntimeError("磁盘忙")),
                        {"name": "老王饭店", "frame_id": "4"})
    assert received == [("老王饭店", "3")]


def test_hub_observed_name_is_optional_and_defensive() -> None:
    assert _observed_store_name({"observed_store_name": " 老王\t饭店 "}) == "老王 饭店"
    assert _observed_store_name({}) == ""
    assert _observed_store_name({"observed_store_name": 42}) == ""
    assert _observed_store_name({"observed_store_name": "x" * 41}) == ""


def _worker(tmp_path: Path) -> SharedWorker:
    return SharedWorker(tmp_path / "worker", TaskStore(tmp_path / "tasks.sqlite3"),
                        threading.Lock(), threading.Lock())


def _task(tmp_path: Path) -> dict:
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (12, 8), "white").save(output / "page-01.png")
    return {"id": "task-1", "status": "captured", "output_dir": str(output),
            "stage": "", "message": "", "reject_reason": "", "reject_detail": "", "error_code": ""}


def _assignment(run_id: str) -> dict:
    return {"id": "a" * 32, "task_id": "task-1", "snapshot_id": "snap", "source_hash": "hash",
            "run_id": run_id}


def test_result_manifest_carries_observed_name_only_when_present(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    task = _task(tmp_path)

    worker._queue_result(_assignment("run-1"), task)
    with ZipFile(worker.outbox / ("a" * 32 + ".zip")) as archive:
        assert "observed_store_name" not in json.loads(archive.read("result.json"))

    worker.store.record_observed_store_name("run-1", "task-1", "老王饭店（二店）")
    assignment = {**_assignment("run-1"), "id": "b" * 32}
    worker._queue_result(assignment, task)
    with ZipFile(worker.outbox / ("b" * 32 + ".zip")) as archive:
        assert json.loads(archive.read("result.json"))["observed_store_name"] == "老王饭店（二店）"


def _rejected_task(app) -> dict:
    """构造一条未采集成功（标记不合格）的采集任务，验证导出仍能带上观察名称。"""
    source = Path(app.state.store.path).parent / "inbox" / "店甲"
    source.mkdir(parents=True)
    Image.new("RGB", (12, 8), "white").save(source / "menu.png")
    app.state.store.scan(source.parent)
    task = app.state.store.tasks()[0]
    with app.state.store.connect() as db:
        db.execute("UPDATE tasks SET status='captured'")
    RejectionService(app.state.store, app.state.materials).mark_task(task["id"], "other", "未采集成功")
    return task


def test_rejections_export_carries_observed_name_for_failed_capture(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    task = _rejected_task(app)
    app.state.store.create_run("run-1", "auto", [task["id"]])
    app.state.store.record_observed_store_name("run-1", task["id"], "实际观察店名")
    client = TestClient(app, base_url="http://127.0.0.1")

    exported = client.get("/api/rejections/export.xlsx?source=capture")

    assert exported.status_code == 200
    rows = list(load_workbook(BytesIO(exported.content), read_only=True).active.iter_rows(values_only=True))
    assert rows[0][-1] == "实际观察门店名称"
    assert rows[1][-1] == "实际观察店名"
    # 原始门店编号仍是主键列，名称只作附带信息追加在末尾。
    assert rows[1][9] == task["id"]


def test_rejections_export_consumes_injected_synced_names(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    task = _rejected_task(app)
    service = RejectionService(app.state.store, app.state.materials,
                               observed_names=lambda task_id: "Hub 同步店名" if task_id == task["id"] else "")

    assert service.list("capture")["records"][0]["observed_store_name"] == "Hub 同步店名"


def test_observed_name_read_failure_does_not_block_export(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    task = _rejected_task(app)

    def boom(_task_id: str) -> str:
        raise RuntimeError("数据库忙")

    service = RejectionService(app.state.store, app.state.materials, observed_names=boom)
    records = service.list("capture")["records"]
    assert records[0]["task_id"] == task["id"]
    assert records[0]["observed_store_name"] == ""
    assert service.export_xlsx("capture")[:2] == b"PK"  # 名称读取失败不影响导出本身


def _hub(tmp_path: Path) -> SharedHub:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    configs = ConfigStore(tmp_path / "config")
    configs.root.mkdir(parents=True, exist_ok=True)
    return SharedHub(store, MaterialService(tmp_path / "materials", configs, store))


def _seed_task(hub: SharedHub, task_id: str, *, inherited_from: str = "") -> None:
    with hub.store.connect() as db:
        db.execute(
            "INSERT INTO tasks(id,source_name,source_path,source_hash,updated_at,inherited_from)"
            " VALUES(?,?,?,?,?,?)",
            (task_id, task_id, f"/inbox/{task_id}.png", "hash", "2026-01-01T00:00:00+00:00", inherited_from),
        )


def _seed_result(hub: SharedHub, assignment_id: str, task_id: str, disposition: str,
                 observed: str, received_at: str) -> None:
    with hub.store.connect() as db:
        db.execute(
            "INSERT INTO shared_assignments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (assignment_id, task_id, "w" * 32, "b" * 32, "批次", task_id, "v1", "snap", "hash",
             ".png", "completed", "2026-01-01T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO shared_results(assignment_id,digest,disposition,status,message,"
            "received_at) VALUES(?,?,?,?,?,?)",
            (assignment_id, "d" * 64, disposition, "captured", "", received_at),
        )
        db.execute(
            "INSERT INTO shared_result_names(assignment_id,store_name) VALUES(?,?)",
            (assignment_id, observed),
        )


def test_hub_name_uses_current_accepted_result_only(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    _seed_task(hub, "task-1")
    # 历史（迟到/换版作废）结果带名称，当前没有 accepted 结果时不得回退到它。
    _seed_result(hub, "a" * 32, "task-1", "history", "历史旧名", "2026-01-02T00:00:00+00:00")
    assert hub.observed_store_name("task-1") == ""

    _seed_result(hub, "b" * 32, "task-1", "accepted", "当前有效店名", "2026-01-01T00:00:00+00:00")
    assert hub.observed_store_name("task-1") == "当前有效店名"

    # 采集端显式撤销：当前 accepted 结果名称为空，必须原样返回空，不能回退到更早的有效名称。
    _seed_result(hub, "c" * 32, "task-1", "accepted", "", "2026-01-03T00:00:00+00:00")
    assert hub.observed_store_name("task-1") == ""


def test_hub_name_follows_inherited_ownership(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    _seed_task(hub, "task-old")
    _seed_task(hub, "task-new", inherited_from="task-old")
    _seed_result(hub, "a" * 32, "task-old", "accepted", "换版继承店名", "2026-01-01T00:00:00+00:00")

    # 换版复用的结果落在原任务上，新任务通过 inherited_from 继承同一份有效名称。
    assert hub.observed_store_name("task-new") == "换版继承店名"
    assert hub.observed_store_name("missing") == ""


def test_hub_upgrade_keeps_legacy_results_insert_contract(tmp_path: Path) -> None:
    """新表应保存观察名称，不能改变 v0.3.0 的六列 INSERT VALUES 回滚契约。"""
    hub = _hub(tmp_path)
    with hub.store.connect() as db:
        columns = [row["name"] for row in db.execute("PRAGMA table_info(shared_results)")]
        assert columns == ["assignment_id", "digest", "disposition", "status", "message", "received_at"]
        db.execute("INSERT INTO shared_results VALUES(?,?,?,?,?,?)",
                   ("a" * 32, "d" * 64, "accepted", "captured", "", "2026-01-01T00:00:00+00:00"))


def test_hub_migrates_published_v032_names_without_losing_rollback_compatibility(tmp_path: Path) -> None:
    """已发布 v0.3.2 的七列数据库保留旧列，并迁移名称到独立表。"""
    store = TaskStore(tmp_path / "tasks.sqlite3")
    with store.connect() as db:
        db.execute("CREATE TABLE shared_results (assignment_id TEXT PRIMARY KEY,digest TEXT NOT NULL,"
                   "disposition TEXT NOT NULL,status TEXT NOT NULL,message TEXT NOT NULL,received_at TEXT NOT NULL,"
                   "observed_store_name TEXT NOT NULL DEFAULT '')")
        db.execute("INSERT INTO shared_results VALUES(?,?,?,?,?,?,?)",
                   ("a" * 32, "d" * 64, "accepted", "captured", "", "2026-01-01T00:00:00+00:00", "旧版本门店名"))
    configs = ConfigStore(tmp_path / "config")
    configs.root.mkdir(parents=True, exist_ok=True)
    hub = SharedHub(store, MaterialService(tmp_path / "materials", configs, store))
    with hub.store.connect() as db:
        row = db.execute("SELECT store_name FROM shared_result_names WHERE assignment_id=?", ("a" * 32,)).fetchone()
        columns = [item["name"] for item in db.execute("PRAGMA table_info(shared_results)")]
        assert row["store_name"] == "旧版本门店名"
        assert "observed_store_name" in columns
        # v0.3.2 的七列写入仍可回滚运行，且新版本通过独立表继续读取。
        db.execute("INSERT INTO shared_results VALUES(?,?,?,?,?,?,?)",
                   ("b" * 32, "e" * 64, "accepted", "captured", "", "2026-01-02T00:00:00+00:00", "新版本门店名"))


def test_store_names_export_covers_all_states_and_current_round(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    task = _rejected_task(app)
    app.state.store.create_run("run-0", "auto", [task["id"]])
    app.state.store.record_observed_store_name("run-0", task["id"], "历史轮次店名")
    app.state.store.create_run("run-1", "auto", [task["id"]])
    app.state.store.record_observed_store_name("run-1", task["id"], "本轮观察店名")

    client = TestClient(app, base_url="http://127.0.0.1")
    exported = client.get("/api/rejections/export-store-names.xlsx")

    assert exported.status_code == 200
    rows = list(load_workbook(BytesIO(exported.content), read_only=True).active.iter_rows(values_only=True))
    assert rows[0] == ("原始任务编号", "原始门店名称", "实际观察门店名称", "采集状态")
    by_id = {row[0]: row for row in rows[1:]}
    assert by_id[task["id"]] == (task["id"], task["source_name"], "本轮观察店名", "不合格")

