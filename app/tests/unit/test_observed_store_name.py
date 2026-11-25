"""店铺名称伴随提取：协议事件、独立持久化与共享结果装配。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from zipfile import ZipFile

from PIL import Image

from menu_capture.agent.ui_tars_trial import _record_observation
from menu_capture.shared.shared import _observed_store_name
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
