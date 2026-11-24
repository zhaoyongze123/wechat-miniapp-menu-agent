"""任务中心查询与历史聚合；HTTP 参数映射由 api.task_center 处理。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .history import ACTIVE_RUN_STATUSES, capture_history_row, history_timestamp


class TaskHistoryNotFound(LookupError):
    """任务历史资源不存在，供 HTTP 层映射为 404。"""


class TaskCenterService:
    """聚合任务中心的数据来源，保留现有列表与详情响应口径。"""

    def __init__(self, *, store, materials, hub, worker, runner, configs):
        self.store = store
        self.materials = materials
        self.hub = hub
        self.worker = worker
        self.runner = runner
        self.configs = configs

    def _batch_name(self, batch_id: str) -> str:
        return self.materials.batch_name(batch_id)

    def run_progress(self, run: dict) -> dict:
        """本运行的真实进度：按 run_actions 里已出结果的任务统计。"""
        ids = list(run.get("task_ids") or [])
        if run.get("status") in {"completed", "failed", "stopped"}:
            # 终态整轮已处理；恢复/阻塞后 current_index 会重置，不能用它统计已处理数。
            return {"ids": ids, "processed": len(ids), "done_ids": ids}
        try:
            with self.store.connect() as db:
                rows = db.execute(
                    "SELECT DISTINCT task_id FROM run_actions WHERE run_id=? AND result!='running'",
                    (run["id"],),
                ).fetchall()
        except Exception:
            rows = []
        done = [row["task_id"] for row in rows if row["task_id"] in set(ids)]
        if not done:
            done = ids[:min(max(int(run.get("current_index") or 0), 0), len(ids))]
        return {"ids": ids, "processed": len(done), "done_ids": done}

    def _capture_history_task_index(self) -> dict:
        # Worker 使用分配记录补齐任务来源；Hub/本机使用当前发布快照。
        tasks = {task["id"]: task for task in self.store.tasks()}
        if self.worker:
            with self.store.connect() as db:
                assigned = db.execute(
                    "SELECT task_id,batch_id,batch_name,revision AS version FROM worker_assignments ORDER BY rowid"
                )
                for row in assigned:
                    if row["task_id"] in tasks:
                        tasks[row["task_id"]].update(dict(row))
        else:
            for row in self.materials.effective_capture_tasks():
                if row["id"] in tasks:
                    tasks[row["id"]] = row
        return tasks

    def active_tasks(self) -> dict:
        """合并素材分析、当前采集运行和近期共享轮次。"""
        items = []
        jobs = self.materials.list_jobs(active=True, limit=20)
        seen = {job["id"] for job in jobs}
        jobs += [job for job in self.materials.jobs.recent_finished() if job["id"] not in seen]
        for job in jobs:
            items.append({
                "source": "materials",
                "id": job["id"],
                "title": job.get("batch_name") or self._batch_name(job["batch_id"]),
                "kind": job["kind"],
                "status": job["status"],
                "progress": {"done": job["done_stores"], "total": job["total_stores"]},
                "extra": {"batch_id": job["batch_id"], "queue_position": job.get("queue_position", 0),
                          "eta_seconds": job.get("eta_seconds", 0), "stage": job.get("stage", ""),
                          "current": job.get("current_store_name", ""), "failed": job["failed_stores"]},
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
            })

        # Hub 不执行桌面采集；迁移过来的本地轮次只在历史页展示。
        run = None if self.hub else self.store.latest_run()
        status = ""
        if run:
            status = "running" if self.runner.running else {
                "completed": "completed", "failed": "failed", "stopped": "paused",
                "interrupted": "interrupted",
            }.get(run["status"], run["status"])
            cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
            if status in {"completed", "failed"} and run["updated_at"] < cutoff:
                run = None
        if run:
            setting = self.configs.load()
            current_root = Path(setting.input_dir).expanduser().resolve() if setting.input_dir else None
            tasks = [task for task in self.store.tasks()
                     if current_root and current_root in Path(task["source_path"]).resolve().parents]
            by_id = {task["id"]: task for task in tasks}
            ids = run["task_ids"]
            processed = self.run_progress(run)["processed"]
            snapshots = {Path(by_id[task_id]["source_path"]).parent.parent.name
                         for task_id in ids if task_id in by_id}
            items.append({
                "source": "capture",
                "id": run["id"],
                "title": run.get("name") or (f"待检查重跑 · 跨 {len(snapshots)} 个批次" if len(snapshots) > 1
                                             else (current_root.name if current_root else run["mode"])),
                "kind": run["mode"],
                "status": status,
                "progress": {"done": processed, "total": len(ids)},
                "extra": {"run_id": run["id"], "current": by_id.get(ids[processed], {}).get("source_name", "")
                          if processed < len(ids) else "", "next": by_id.get(ids[processed], {}).get("source_name", "")
                          if processed < len(ids) else "", "mode": run["mode"],
                          "stop_requested": self.runner.stop.is_set()},
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
            })
        if self.hub:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
            for row in self.hub.history_records(shortcut_since=cutoff.isoformat(timespec="seconds")):
                if row["status"] not in ACTIVE_RUN_STATUSES and history_timestamp(row["updated_at"]) < cutoff.timestamp():
                    continue
                items.append({
                    "source": "capture", "id": row["id"], "title": row["title"],
                    "kind": row["mode"], "status": row["status"],
                    "progress": {"done": row["counts"]["processed"], "total": row["counts"]["total"]},
                    "extra": {"worker_id": row["worker_id"], "worker_name": row["worker_name"],
                              "synced_at": row["synced_at"]},
                    "created_at": row["created_at"], "updated_at": row["updated_at"],
                })
        return {"tasks": items}

    def history(self, *, kind: str = "all", status: str = "", q: str = "", page: int = 1,
                page_size: int = 20, worker_id: str = "") -> dict:
        page = max(1, int(page))
        page_size = min(100, max(5, int(page_size)))
        rows: list[dict] = []
        if kind in ("all", "capture"):
            runs = self.store.run_history(500) if not worker_id else []
            results = self.store.capture_run_results([run["id"] for run in runs])
            task_index = self._capture_history_task_index() if runs else {}
            for run in runs:
                item = capture_history_row(run, results.get(run["id"], []), task_index)
                item.pop("tasks")
                rows.append(item)
            if self.hub:
                rows.extend(self.hub.history_records(worker_id=worker_id))
        if kind in ("all", "materials") and not worker_id:
            for job in self.materials.jobs.history(500):
                rows.append({
                    "kind": "materials",
                    "id": job["id"],
                    "title": self._batch_name(job["batch_id"]),
                    "batch_id": job["batch_id"],
                    "batch_name": self._batch_name(job["batch_id"]),
                    "mode": job.get("kind", ""),
                    "status": job.get("status", ""),
                    "created_at": job.get("created_at", ""),
                    "updated_at": job.get("updated_at", ""),
                    "message": job.get("message", "") or job.get("error", ""),
                    "counts": {"total": int(job.get("total_stores") or 0),
                               "done": int(job.get("done_stores") or 0), "attention": 0,
                               "failed": int(job.get("failed_stores") or 0)},
                })
        active = ACTIVE_RUN_STATUSES
        aborted = {"terminated", "stopped"}
        errors = {"failed", "cancelled"}
        if status:
            matched = {"running": active, "failed": errors, "terminated": aborted}.get(status, {status})
            rows = [row for row in rows if row["status"] in matched]
        normalized = q.strip().casefold()
        if normalized:
            rows = [row for row in rows if normalized in (" ".join(
                [row["title"], row["batch_name"], row["id"], row.get("worker_name", ""),
                 *(batch["name"] for batch in row.get("batches", []))])).casefold()]
        rows.sort(key=lambda row: (history_timestamp(row["created_at"]), row["id"]), reverse=True)
        total = len(rows)
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(page, page_count)
        start = (page - 1) * page_size
        return {
            "records": rows[start:start + page_size],
            "summary": {"total": total, "running": sum(row["status"] in active for row in rows),
                        "completed": sum(row["status"] == "completed" for row in rows),
                        "failed": sum(row["status"] in errors for row in rows),
                        "terminated": sum(row["status"] in aborted for row in rows)},
            "page": page,
            "page_size": page_size,
            "page_count": page_count,
        }

    def detail(self, kind: str, item_id: str) -> dict:
        if kind == "capture":
            if self.hub and ":" in item_id:
                worker_id, run_id = item_id.split(":", 1)
                record = self.hub.history_detail(worker_id, run_id)
                if record is not None:
                    return record
                raise TaskHistoryNotFound("采集电脑的轮次尚未同步或不存在")
            run = self.store.get_run(item_id)
            if run is None:
                raise TaskHistoryNotFound("运行记录不存在")
            results = self.store.capture_run_results([item_id]).get(item_id, [])
            return capture_history_row(run, results, self._capture_history_task_index())
        if kind == "materials":
            job = self.materials.job_detail(item_id, events=True)
            return {
                "kind": "materials", "id": item_id, "title": self._batch_name(job["batch_id"]),
                "batch_id": job["batch_id"], "batch_name": self._batch_name(job["batch_id"]),
                "mode": job.get("kind", ""), "status": job.get("status", ""),
                "created_at": job.get("created_at", ""), "updated_at": job.get("updated_at", ""),
                "message": job.get("message", "") or job.get("error", ""),
                "counts": {"total": int(job.get("total_stores") or 0),
                           "done": int(job.get("done_stores") or 0), "attention": 0,
                           "failed": int(job.get("failed_stores") or 0)},
                "events": job.get("events", []),
            }
        raise TaskHistoryNotFound("任务类型不存在")

    def rename_capture_run(self, run_id: str, payload: dict) -> dict:
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValueError("任务名称无效")
        name = name.strip()
        if not 1 <= len(name) <= 64:
            raise ValueError("任务名称需为 1 到 64 个字符")
        if self.store.get_run(run_id) is None:
            raise TaskHistoryNotFound("运行记录不存在")
        self.store.rename_run(run_id, name)
        run = self.store.get_run(run_id)
        results = self.store.capture_run_results([run_id]).get(run_id, [])
        return capture_history_row(run, results, self._capture_history_task_index())
