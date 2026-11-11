"""素材分析任务存储与调度（material_jobs）的独立单元测试。

不依赖 MaterialService 与二维码算法，只验证：
- 全局单通道与先来先跑；
- 崩溃/重启后的中断、恢复与心跳租约；
- 门店边界的暂停粒度；
- 单店重试、失败上限与并发不重复；
- 同批只能有一个未结束 job（含 interrupted）。
"""
from __future__ import annotations

import threading
import time

import pytest

from menu_capture import material_jobs
from menu_capture.material_jobs import JobRunner, JobStore
from menu_capture.store import TaskStore


def _store(tmp_path) -> JobStore:
    return JobStore(TaskStore(tmp_path / "tasks.sqlite3"))


def _items(count: int, prefix: str = "store") -> list[dict]:
    return [{"store_id": f"{index:04d}", "source_folder": f"{prefix}/{index}"} for index in range(count)]


def _wait(predicate, timeout: float = 10.0, message: str = "等待条件超时") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


def _age_heartbeat(store: JobStore, job_id: str) -> None:
    with store.task_store.connect() as db:
        db.execute("UPDATE material_jobs SET heartbeat_at='2000-01-01T00:00:00+08:00' WHERE id=?", (job_id,))


def test_jobs_run_one_at_a_time_in_creation_order(tmp_path) -> None:
    """全局单通道：第一个 job 未结束时，第二个只能排队。"""
    store = _store(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def unit(job, item, context):
        if job["batch_id"] == "batch-1":
            entered.set()
            assert release.wait(5), "测试未及时放行第一个任务"

    runner = JobRunner(store, unit)
    first = store.create("batch-1", "analyze", _items(2), concurrency=1)
    runner.tick()
    assert entered.wait(5), "第一个任务没有开始"

    second = store.create("batch-2", "analyze", _items(1), concurrency=1)
    runner.tick()
    assert store.get(first["id"])["status"] == "running"
    assert store.get(second["id"])["status"] == "queued"

    release.set()
    _wait(lambda: store.get(first["id"])["status"] == "completed")
    runner.tick()
    _wait(lambda: store.get(second["id"])["status"] == "completed")
    assert store.counts(second["id"])["done"] == 1


def test_interrupted_job_resumes_only_remaining_stores(tmp_path) -> None:
    """模拟进程崩溃：已完成的门店不重跑，恢复后只处理剩余 pending。"""
    store = _store(tmp_path)
    job = store.create("batch-1", "analyze", _items(4), concurrency=1)
    assert store.claim(job["id"], "crashed-owner")
    finished = store.claim_item(job["id"])
    store.finish_item(job["id"], finished["store_id"], True)

    _age_heartbeat(store, job["id"])
    assert store.recover_stale() == 1
    assert store.get(job["id"])["status"] == "interrupted"
    assert store.resume(job["id"]) is True

    processed: list[str] = []
    runner = JobRunner(store, lambda current_job, item, context: processed.append(item["store_id"]))
    runner.tick()
    _wait(lambda: store.get(job["id"])["status"] == "completed")

    assert sorted(processed) == ["0001", "0002", "0003"]
    assert store.counts(job["id"])["done"] == 4


def test_stale_lease_is_recovered_but_fresh_one_is_kept(tmp_path) -> None:
    """心跳过期的 running 任务回收为 interrupted；心跳新鲜的不动。"""
    store = _store(tmp_path)
    stale = store.create("batch-stale", "analyze", _items(1))
    fresh = store.create("batch-fresh", "analyze", _items(1))
    assert store.claim(stale["id"], "dead-owner") is True
    assert store.claim(fresh["id"], "alive-owner") is True

    _age_heartbeat(store, stale["id"])
    assert store.recover_stale(lease_seconds=120) == 1
    assert store.get(stale["id"])["status"] == "interrupted"
    assert store.get(fresh["id"])["status"] == "running"


def test_pause_takes_effect_at_store_boundary(tmp_path) -> None:
    """暂停只在门店边界生效：已开始的第 3 家会做完，之后不再取新门店。"""
    store = _store(tmp_path)

    def unit(job, item, context):
        if store.counts(job["id"])["done"] + 1 == 3:
            store.request_pause(job["id"])

    job = store.create("batch-1", "analyze", _items(5), concurrency=1)
    JobRunner(store, unit).tick()
    _wait(lambda: store.get(job["id"])["status"] == "paused")

    counts = store.counts(job["id"])
    assert counts["done"] == 3
    assert counts["running"] == 0
    assert counts["remaining"] == 2


def test_single_store_failure_retries_then_succeeds(tmp_path, monkeypatch) -> None:
    """第一次抛异常、第二次成功：item 记为 done 且 attempts==2，并留下重试事件。"""
    monkeypatch.setattr(material_jobs, "RETRY_DELAYS", (0, 0, 0))
    store = _store(tmp_path)
    attempts = {"count": 0}

    def unit(job, item, context):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ValueError("坏图")

    job = store.create("batch-1", "analyze", _items(1), concurrency=1)
    JobRunner(store, unit).tick()
    _wait(lambda: store.get(job["id"])["status"] == "completed")

    with store.task_store.connect() as db:
        row = db.execute(
            "SELECT status,attempts FROM material_job_items WHERE job_id=?", (job["id"],)
        ).fetchone()
    assert (row["status"], row["attempts"]) == ("done", 2)
    assert any(event["level"] == "warn" and "重试" in event["message"]
               for event in store.events(job["id"]))


def test_store_failing_all_attempts_is_counted_and_others_continue(tmp_path, monkeypatch) -> None:
    """连续 3 次异常只影响该门店；其余门店完成，结果仍可用（completed + 失败数）。"""
    monkeypatch.setattr(material_jobs, "RETRY_DELAYS", (0, 0, 0))
    store = _store(tmp_path)
    attempts: dict[str, int] = {}

    def unit(job, item, context):
        attempts[item["store_id"]] = attempts.get(item["store_id"], 0) + 1
        if item["store_id"] == "0000":
            raise RuntimeError("坏图")

    job = store.create("batch-1", "analyze", _items(2), concurrency=1)
    JobRunner(store, unit).tick()
    _wait(lambda: store.get(job["id"])["status"] in {"completed", "failed"})

    current = store.get(job["id"])
    # 部分失败不隐藏结果：job 保持 completed，前端据此提供“重试失败门店”。
    assert current["status"] == "completed"
    assert (current["done_stores"], current["failed_stores"]) == (1, 1)
    assert attempts["0000"] == 3


def test_two_workers_never_process_the_same_store(tmp_path) -> None:
    """100 家、2 路并发：每家只被处理一次，最终全部完成。"""
    store = _store(tmp_path)
    processed: list[str] = []
    lock = threading.Lock()

    def unit(job, item, context):
        time.sleep(0.001)
        with lock:
            processed.append(item["store_id"])

    job = store.create("batch-1", "analyze", _items(100), concurrency=2)
    JobRunner(store, unit).tick()
    _wait(lambda: store.get(job["id"])["status"] == "completed", timeout=20)

    assert store.counts(job["id"])["done"] == 100
    assert len(processed) == len(set(processed)) == 100


def test_open_job_unique_per_batch_including_interrupted(tmp_path) -> None:
    """同批只能有一个未结束 job；interrupted 也占位，重试入口先取消旧任务。"""
    store = _store(tmp_path)
    job = store.create("batch-1", "analyze", _items(1))
    with pytest.raises(ValueError, match="未结束"):
        store.create("batch-1", "reanalyze", _items(1))

    assert store.claim(job["id"], "owner") is True
    _age_heartbeat(store, job["id"])
    assert store.recover_stale() == 1
    assert store.get(job["id"])["status"] == "interrupted"
    with pytest.raises(ValueError, match="已中断"):
        store.create("batch-1", "reanalyze", _items(1))

    replacement = store.create_replacing("batch-1", "retry_failed", _items(1))
    assert store.get(job["id"])["status"] == "cancelled"
    assert replacement["status"] == "queued"


def test_same_second_jobs_keep_creation_order(tmp_path) -> None:
    """同一秒内连续创建的任务必须按 rowid 稳定排序，不能取到旧任务或插队。"""
    store = _store(tmp_path)
    first = store.create("batch-1", "analyze", _items(1))
    second = store.create("batch-2", "analyze", _items(1))
    assert store.queue_head()["id"] == first["id"]

    replacement = store.create_replacing("batch-1", "reanalyze", _items(1))
    assert store.latest("batch-1")["id"] == replacement["id"]
    assert store.latest("batch-1")["id"] != first["id"]
    assert store.get(second["id"])["status"] == "queued"

