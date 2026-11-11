"""素材分析任务的持久化存储与调度。

职责边界：
- 只负责 job/item/event 的生命周期与并发调度，不包含任何二维码分析算法；
- 运行态唯一事实源是 material_jobs.status，批次状态只表示“结果是否可用”；
- 所有状态迁移都用单条 UPDATE 的 rowcount 做 CAS，避免多线程/多实例读改写竞态。
"""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# 未结束状态：唯一索引与调度查询共用同一份定义，避免两处口径漂移。
OPEN_STATUSES = ("queued", "running", "pause_requested", "paused", "interrupted")
# 心跳超过该秒数视为进程已死；恢复时只回收到 interrupted，绝不自动重放。
DEFAULT_LEASE_SECONDS = 120
EVENT_KEEP = 500
# 单店异常后的重试等待：首次立即重试，之后 2s、5s；测试可替换为零等待。
RETRY_DELAYS = (0, 2, 5)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _iso_before(seconds: int) -> str:
    return (datetime.now(timezone.utc).astimezone() - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _new_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class JobStore:
    """material_jobs / material_job_items / material_job_events 的读写入口。"""

    def __init__(self, task_store):
        self.task_store = task_store
        self._init_schema()

    def _init_schema(self) -> None:
        with self.task_store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS material_jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_stores INTEGER NOT NULL DEFAULT 0,
                    done_stores INTEGER NOT NULL DEFAULT 0,
                    failed_stores INTEGER NOT NULL DEFAULT 0,
                    concurrency INTEGER NOT NULL DEFAULT 2,
                    current_store_id TEXT NOT NULL DEFAULT '',
                    current_store_name TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL DEFAULT '',
                    heartbeat_at TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS material_job_items (
                    job_id TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    source_folder TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, store_id)
                );
                CREATE TABLE IF NOT EXISTS material_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    time TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    store_id TEXT NOT NULL DEFAULT '',
                    store_name TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS material_jobs_queue ON material_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS material_job_items_queue ON material_job_items(job_id, status);
                CREATE INDEX IF NOT EXISTS material_job_events_job ON material_job_events(job_id, id DESC);
            """)
            # interrupted 也纳入唯一性约束：崩溃遗留的任务必须先恢复或取消，不能再新建同批任务。
            # SQLite 的部分索引 WHERE 不允许绑定参数，这里用内部常量拼字面量。
            statuses = ",".join(f"'{status}'" for status in OPEN_STATUSES)
            db.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS material_jobs_one_open_per_batch "
                f"ON material_jobs(batch_id) WHERE status IN ({statuses})"
            )

    # ---------------------------------------------------------------- 查询

    @staticmethod
    def _row(row) -> dict | None:
        return dict(row) if row is not None else None

    def get(self, job_id: str) -> dict | None:
        with self.task_store.connect() as db:
            job = self._row(db.execute("SELECT * FROM material_jobs WHERE id=?", (job_id,)).fetchone())
        if job is None:
            return None
        job["counts"] = self.counts(job_id)
        return job

    def open_job(self, batch_id: str) -> dict | None:
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        with self.task_store.connect() as db:
            row = db.execute(
                f"SELECT * FROM material_jobs WHERE batch_id=? AND status IN ({placeholders}) "
                f"ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (batch_id, *OPEN_STATUSES),
            ).fetchone()
        return self._row(row)

    def latest(self, batch_id: str) -> dict | None:
        with self.task_store.connect() as db:
            row = db.execute(
                # 同一秒内可能连续创建多个 job，必须用 rowid 兜底，否则会取到旧任务。
                "SELECT * FROM material_jobs WHERE batch_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
        job = self._row(row)
        if job is not None:
            job["counts"] = self.counts(job["id"])
        return job

    def queue_head(self) -> dict | None:
        """最老的 queued 任务；调度器只认领这个，保证全局单通道且先来先跑。"""
        with self.task_store.connect() as db:
            row = db.execute(
                "SELECT * FROM material_jobs WHERE status='queued' ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
        return self._row(row)

    def running_count(self) -> int:
        with self.task_store.connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM material_jobs WHERE status IN ('running','pause_requested')"
            ).fetchone()[0])

    def active(self, limit: int = 20) -> list[dict]:
        """任务中心数据源：未结束任务 + 最近结束任务，附带排队位次与预计剩余。"""
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        with self.task_store.connect() as db:
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM material_jobs WHERE status IN ({placeholders}) ORDER BY created_at, rowid LIMIT ?",
                (*OPEN_STATUSES, limit),
            )]
            queued = [row["id"] for row in db.execute(
                "SELECT id FROM material_jobs WHERE status='queued' ORDER BY created_at, rowid"
            )]
        positions = {job_id: index + 1 for index, job_id in enumerate(queued)}
        for row in rows:
            row["counts"] = self.counts(row["id"])
            row["queue_position"] = positions.get(row["id"], 0)
            row["eta_seconds"] = self.eta(row)
        return rows

    def recent_finished(self, minutes: int = 10, limit: int = 3) -> list[dict]:
        """最近结束的任务，供任务中心查看失败原因并直接重试。"""
        cutoff = _iso_before(minutes * 60)
        with self.task_store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM material_jobs WHERE status IN ('completed','failed','cancelled') "
                "AND finished_at<>'' AND finished_at>=? ORDER BY finished_at DESC, rowid DESC LIMIT ?",
                (cutoff, limit),
            )]
        for row in rows:
            row["counts"] = self.counts(row["id"])
            row["queue_position"] = 0
            row["eta_seconds"] = 0
        return rows

    def for_batch(self, batch_id: str, limit: int = 20) -> list[dict]:
        with self.task_store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM material_jobs WHERE batch_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (batch_id, limit),
            )]
        for row in rows:
            row["counts"] = self.counts(row["id"])
            row["eta_seconds"] = self.eta(row)
            row["queue_position"] = 0
        return rows

    def counts(self, job_id: str) -> dict:
        with self.task_store.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(status='done') AS done, SUM(status='failed') AS failed, "
                "SUM(status IN ('pending','running')) AS remaining, "
                "SUM(status='running') AS running "
                "FROM material_job_items WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return {key: int(row[key] or 0) for key in ("total", "done", "failed", "remaining", "running")}

    def eta(self, job: dict) -> int:
        """最近 20 家已完成的平均耗时 × 剩余门店数 ÷ 并发数；样本不足时返回 0。"""
        if job.get("status") not in ("running", "pause_requested", "queued", "paused", "interrupted"):
            return 0
        with self.task_store.connect() as db:
            row = db.execute(
                "SELECT AVG(seconds) AS avg_seconds FROM ("
                "  SELECT (julianday(finished_at) - julianday(started_at)) * 86400 AS seconds "
                "  FROM material_job_items WHERE job_id=? AND status IN ('done','failed') "
                "  AND started_at<>'' AND finished_at<>'' ORDER BY finished_at DESC LIMIT 20"
                ")",
                (job["id"],),
            ).fetchone()
        average = float(row["avg_seconds"] or 0)
        counts = job.get("counts") or self.counts(job["id"])
        concurrency = max(1, int(job.get("concurrency") or 1))
        return int(round(average * counts["remaining"] / concurrency)) if average > 0 else 0

    def events(self, job_id: str, limit: int = 50) -> list[dict]:
        with self.task_store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM material_job_events WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            )]

    def event(self, job_id: str, level: str, message: str, *,
              store_id: str = "", store_name: str = "") -> None:
        with self.task_store.connect() as db:
            db.execute(
                "INSERT INTO material_job_events(job_id,time,level,store_id,store_name,message) VALUES(?,?,?,?,?,?)",
                (job_id, _now(), level, store_id, store_name, message[:500]),
            )
            db.execute(
                "DELETE FROM material_job_events WHERE job_id=? AND id NOT IN "
                "(SELECT id FROM material_job_events WHERE job_id=? ORDER BY id DESC LIMIT ?)",
                (job_id, job_id, EVENT_KEEP),
            )

    # ---------------------------------------------------------------- 写入

    def create(self, batch_id: str, kind: str, items: list[dict], concurrency: int = 2) -> dict:
        """建 job + 批量写 items（单事务）。同批已有未结束任务时拒绝，唯一索引兜底。"""
        existing = self.open_job(batch_id)
        if existing is not None:
            raise ValueError(self._open_job_message(existing))
        stamp = _now()
        job_id = uuid.uuid4().hex
        with self.task_store.connect() as db:
            db.execute(
                "INSERT INTO material_jobs(id,batch_id,kind,status,total_stores,concurrency,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (job_id, batch_id, kind, "queued", len(items), max(1, min(4, int(concurrency))), stamp, stamp),
            )
            db.executemany(
                "INSERT OR IGNORE INTO material_job_items(job_id,store_id,source_folder,status,updated_at) "
                "VALUES(?,?,?,'pending',?)",
                [(job_id, item["store_id"], item["source_folder"], stamp) for item in items],
            )
        return self.get(job_id) or {}

    def create_replacing(self, batch_id: str, kind: str, items: list[dict], concurrency: int = 2) -> dict:
        """重试入口：先取消同批旧任务，再建新任务，避免 paused/interrupted 撞唯一索引。"""
        existing = self.open_job(batch_id)
        if existing is not None:
            self.cancel(existing["id"], message="由新的重试任务取代")
        return self.create(batch_id, kind, items, concurrency)

    @staticmethod
    def _open_job_message(job: dict) -> str:
        if job["status"] == "interrupted":
            return "该批次有已中断的任务，请先恢复或取消后再开始"
        return "该批次已有未结束的分析任务，请先暂停或取消"

    def claim(self, job_id: str, owner: str) -> bool:
        stamp = _now()
        with self.task_store.connect() as db:
            cursor = db.execute(
                "UPDATE material_jobs SET status='running',owner=?,heartbeat_at=?,started_at=?,updated_at=? "
                "WHERE id=? AND status='queued'",
                (owner, stamp, stamp, stamp, job_id),
            )
            return cursor.rowcount == 1

    def heartbeat(self, job_id: str, owner: str, **fields) -> bool:
        stamp = _now()
        assignments = ["heartbeat_at=?", "updated_at=?"]
        values: list = [stamp, stamp]
        for key in ("current_store_id", "current_store_name", "stage", "message"):
            if key in fields:
                assignments.append(f"{key}=?")
                values.append(str(fields[key])[:300])
        values.extend([job_id, owner])
        with self.task_store.connect() as db:
            cursor = db.execute(
                f"UPDATE material_jobs SET {','.join(assignments)} "
                "WHERE id=? AND owner=? AND status IN ('running','pause_requested')",
                values,
            )
            return cursor.rowcount == 1

    def request_pause(self, job_id: str) -> bool:
        """运行时置 pause_requested（门店边界生效）；排队中直接 paused。"""
        with self.task_store.connect() as db:
            cursor = db.execute(
                "UPDATE material_jobs SET status='pause_requested',updated_at=? WHERE id=? AND status='running'",
                (_now(), job_id),
            )
            if cursor.rowcount == 1:
                return True
            cursor = db.execute(
                "UPDATE material_jobs SET status='paused',finished_at='',updated_at=? WHERE id=? AND status='queued'",
                (_now(), job_id),
            )
            return cursor.rowcount == 1

    def resume(self, job_id: str) -> bool:
        """paused/interrupted → queued；未完成 item 回 pending，已完成保留。"""
        stamp = _now()
        with self.task_store.connect() as db:
            cursor = db.execute(
                "UPDATE material_jobs SET status='queued',owner='',finished_at='',error='',heartbeat_at='',updated_at=? "
                "WHERE id=? AND status IN ('paused','interrupted')",
                (stamp, job_id),
            )
            if cursor.rowcount != 1:
                return False
            db.execute(
                "UPDATE material_job_items SET status='pending',updated_at=? "
                "WHERE job_id=? AND status='running'",
                (stamp, job_id),
            )
            return True

    def cancel(self, job_id: str, message: str = "任务已取消") -> bool:
        stamp = _now()
        with self.task_store.connect() as db:
            cursor = db.execute(
                "UPDATE material_jobs SET status='cancelled',error=?,owner='',finished_at=?,updated_at=? "
                "WHERE id=? AND status IN ('queued','running','pause_requested','paused','interrupted')",
                (message[:300], stamp, stamp, job_id),
            )
            if cursor.rowcount != 1:
                return False
            db.execute(
                "UPDATE material_job_items SET status='failed',error='cancelled',updated_at=? "
                "WHERE job_id=? AND status IN ('pending','running')",
                (stamp, job_id),
            )
            return True

    def finish(self, job_id: str, status: str, error: str = "", message: str = "") -> None:
        stamp = _now()
        with self.task_store.connect() as db:
            db.execute(
                "UPDATE material_jobs SET status=?,error=?,message=?,finished_at=?,owner='',updated_at=? "
                "WHERE id=? AND status NOT IN ('cancelled')",
                (status, error[:300], message[:300], stamp, stamp, job_id),
            )
            counts = db.execute(
                "SELECT COUNT(*) AS total, SUM(status='done') AS done, SUM(status='failed') AS failed "
                "FROM material_job_items WHERE job_id=?",
                (job_id,),
            ).fetchone()
            db.execute(
                "UPDATE material_jobs SET done_stores=?,failed_stores=?,total_stores=? WHERE id=?",
                (int(counts["done"] or 0), int(counts["failed"] or 0), int(counts["total"] or 0), job_id),
            )

    def recover_stale(self, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> int:
        """只回收心跳过期的 running/pause_requested；启动时不无条件判死。"""
        cutoff = _iso_before(lease_seconds)
        with self.task_store.connect() as db:
            stale = [row["id"] for row in db.execute(
                "SELECT id FROM material_jobs WHERE status IN ('running','pause_requested') "
                "AND (heartbeat_at='' OR heartbeat_at<?)",
                (cutoff,),
            )]
            if not stale:
                return 0
            stamp = _now()
            db.executemany(
                "UPDATE material_jobs SET status='interrupted',owner='',error='心跳超时，任务已中断',updated_at=? WHERE id=?",
                [(stamp, job_id) for job_id in stale],
            )
            db.executemany(
                "UPDATE material_job_items SET status='pending',updated_at=? WHERE job_id=? AND status='running'",
                [(stamp, job_id) for job_id in stale],
            )
            return len(stale)

    # ---------------------------------------------------------------- items

    def claim_item(self, job_id: str) -> dict | None:
        stamp = _now()
        with self.task_store.connect() as db:
            row = db.execute(
                "SELECT store_id, source_folder FROM material_job_items "
                "WHERE job_id=? AND status='pending' ORDER BY rowid LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            cursor = db.execute(
                "UPDATE material_job_items SET status='running',attempts=attempts+1,started_at=?,updated_at=? "
                "WHERE job_id=? AND store_id=? AND status='pending'",
                (stamp, stamp, job_id, row["store_id"]),
            )
            if cursor.rowcount != 1:
                return None
        return {"store_id": row["store_id"], "source_folder": row["source_folder"]}

    def release_item(self, job_id: str, store_id: str) -> None:
        with self.task_store.connect() as db:
            db.execute(
                "UPDATE material_job_items SET status='pending',updated_at=? WHERE job_id=? AND store_id=? AND status='running'",
                (_now(), job_id, store_id),
            )

    def bump_attempt(self, job_id: str, store_id: str) -> None:
        """重试前累计尝试次数：claim_item 只记首次，连续重试也要在 item 上可见。"""
        with self.task_store.connect() as db:
            db.execute(
                "UPDATE material_job_items SET attempts=attempts+1,updated_at=? "
                "WHERE job_id=? AND store_id=? AND status='running'",
                (_now(), job_id, store_id),
            )

    def finish_item(self, job_id: str, store_id: str, ok: bool, error: str = "") -> None:
        stamp = _now()
        with self.task_store.connect() as db:
            db.execute(
                "UPDATE material_job_items SET status=?,error=?,finished_at=?,updated_at=? WHERE job_id=? AND store_id=?",
                ("done" if ok else "failed", error[:120], stamp, stamp, job_id, store_id),
            )
            counts = db.execute(
                "SELECT COUNT(*) AS total, SUM(status='done') AS done, SUM(status='failed') AS failed "
                "FROM material_job_items WHERE job_id=?",
                (job_id,),
            ).fetchone()
            db.execute(
                "UPDATE material_jobs SET done_stores=?,failed_stores=?,total_stores=?,updated_at=? WHERE id=?",
                (int(counts["done"] or 0), int(counts["failed"] or 0), int(counts["total"] or 0), stamp, job_id),
            )

    def failed_items(self, job_id: str) -> list[dict]:
        with self.task_store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT store_id, source_folder FROM material_job_items WHERE job_id=? AND status='failed'",
                (job_id,),
            )]


class JobRunner:
    """全局单通道调度器：同一时刻最多一个 running job，其余按创建顺序排队。

    unit(job, item, context) 由业务层注入；并发度在创建任务时写入 job.concurrency，
    调度时只读一次，保证同一任务内不会因设置变化中途抖动。
    """

    def __init__(self, store: JobStore, unit, *, prepare=None, on_finish=None):
        self.store = store
        self.unit = unit
        self.prepare = prepare
        self.on_finish = on_finish
        self.owner = _new_owner()
        self._stop = threading.Event()
        self._dispatch_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="material-job-runner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        """创建任务后立即尝试调度，最多只等巡检周期（1s）。"""
        if self._thread is None:
            self.tick()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            self.tick()

    def tick(self) -> None:
        try:
            self.store.recover_stale()
            self._dispatch()
        except Exception:
            # 调度器不能因单次数据库异常退出；下一轮继续。
            pass

    def _dispatch(self) -> None:
        with self._dispatch_lock:
            if self.store.running_count() > 0:
                return
            job = self.store.queue_head()
            if job is None or not self.store.claim(job["id"], self.owner):
                return
            threading.Thread(target=self._run_job, args=(job["id"],), name=f"material-job-{job['id'][:8]}",
                             daemon=True).start()

    def _run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        context = None
        if self.prepare is not None:
            try:
                context = self.prepare(job)
            except Exception as exc:
                self._finalize(job_id, error=f"任务准备失败（{type(exc).__name__}）", context=None)
                return
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat_loop, args=(job_id, stop_heartbeat),
                                     name=f"material-job-heartbeat-{job_id[:8]}", daemon=True)
        heartbeat.start()
        try:
            workers = max(1, min(4, int(job.get("concurrency") or 2)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="material-store") as pool:
                futures = [pool.submit(self._worker_loop, job_id, context) for _ in range(workers)]
                for future in futures:
                    future.result()
        except Exception as exc:
            self.store.event(job_id, "error", f"任务执行异常（{type(exc).__name__}）")
        finally:
            stop_heartbeat.set()
        self._finalize(job_id, context=context)

    def _heartbeat_loop(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(5.0):
            job = self.store.get(job_id)
            if job is None or job["status"] not in ("running", "pause_requested"):
                return
            self.store.heartbeat(job_id, self.owner)

    def _worker_loop(self, job_id: str, context) -> None:
        while True:
            job = self.store.get(job_id)
            if job is None or job["status"] not in ("running", "pause_requested"):
                return
            if job["status"] == "pause_requested":
                return
            item = self.store.claim_item(job_id)
            if item is None:
                return
            name = item["source_folder"].rstrip("/").split("/")[-1] or item["source_folder"]
            self.store.heartbeat(job_id, self.owner, current_store_id=item["store_id"],
                                 current_store_name=name, stage="检测与用途识别")
            error = ""
            for index, delay in enumerate(RETRY_DELAYS):
                if index:
                    retry_hint = f"{delay} 秒后重试" if delay else "立即重试"
                    self.store.event(job_id, "warn", f"{name}：处理失败（{error}），{retry_hint}",
                                     store_id=item["store_id"], store_name=name)
                    if delay and self._stop.wait(delay):
                        # 服务停止：把门店退回 pending，恢复后从这里继续。
                        self.store.release_item(job_id, item["store_id"])
                        return
                    # 每次重试都计入 attempts，界面与日志才能反映真实尝试次数。
                    self.store.bump_attempt(job_id, item["store_id"])
                try:
                    self.unit(job, item, context)
                    error = ""
                    break
                except Exception as exc:  # 单店任何异常都不能打断 worker，只记录受控短码
                    detail = getattr(exc, "code", None) or f"{type(exc).__name__}: {exc}"
                    error = detail[:200]
            ok = not error
            self.store.finish_item(job_id, item["store_id"], ok, error)
            if ok:
                self.store.event(job_id, "info", f"{name}：处理完成", store_id=item["store_id"], store_name=name)
            else:
                self.store.event(job_id, "error", f"{name}：处理失败（{error}）", store_id=item["store_id"], store_name=name)

    def _finalize(self, job_id: str, error: str = "", context=None) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        if job["status"] == "cancelled":
            # 取消也要走收尾回调：释放圆形码检测器等资源，只跳过状态改写。
            self._notify(job_id, context)
            return
        counts = job["counts"]
        if job["status"] == "pause_requested":
            status, message = "paused", "已在门店边界暂停"
        elif counts["remaining"] > 0 and error:
            status, message = "interrupted", error
        elif counts["total"] > 0 and counts["failed"] == counts["total"]:
            status, message = "failed", "所有门店处理失败"
        else:
            status = "completed"
            message = f"部分门店失败：{counts['failed']} 家" if counts["failed"] else "全部门店处理完成"
        self.store.finish(job_id, status, error=error, message=message)
        detail = f"{message}（{error}）" if error else message
        self.store.event(job_id, "info" if status == "completed" else "warn", detail)
        self._notify(job_id, context)

    def _notify(self, job_id: str, context) -> None:
        """收尾回调失败不能影响调度线程；取消与正常结束共用同一出口。"""
        if self.on_finish is None:
            return
        try:
            self.on_finish(self.store.get(job_id), context)
        except Exception:
            pass
