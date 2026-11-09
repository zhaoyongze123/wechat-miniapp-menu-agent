from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT
from .failures import classify as classify_failure


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class TaskStore:
    """SQLite 是任务状态唯一来源；图片和来源清单是可追溯的输出工件。"""

    def __init__(self, path: Path = ROOT / "data/tasks.sqlite3"):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, source_name TEXT NOT NULL, source_path TEXT NOT NULL,
                    source_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    stage TEXT NOT NULL DEFAULT 'queued', message TEXT NOT NULL DEFAULT '',
                    output_dir TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    reject_reason TEXT NOT NULL DEFAULT '', reject_detail TEXT NOT NULL DEFAULT '',
                    rejected_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                    level TEXT NOT NULL, message TEXT NOT NULL, task_id TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL,
                    task_ids TEXT NOT NULL, current_index INTEGER NOT NULL DEFAULT 0,
                    current_task_id TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, attempt_id TEXT NOT NULL, stage TEXT NOT NULL,
                    step INTEGER, action TEXT NOT NULL, started_at TEXT NOT NULL,
                    elapsed_ms INTEGER, result TEXT NOT NULL, details TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_actions_task ON run_actions(task_id, id);
                CREATE TABLE IF NOT EXISTS publish_transactions (
                    task_id TEXT PRIMARY KEY, output_root TEXT NOT NULL, staging TEXT NOT NULL,
                    destination TEXT NOT NULL, backup TEXT NOT NULL, had_previous INTEGER NOT NULL,
                    phase TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)

        with self.connect() as db:
            for column in ("material_version_id", "material_batch_id", "material_version", "material_snapshot"):
                if column not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            if "inherited_from" not in {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN inherited_from TEXT NOT NULL DEFAULT ''")
            task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            for column in ("reject_reason", "reject_detail", "rejected_at"):
                if column not in task_columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            if "source_key" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
            if "error_code" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN error_code TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS tasks_source_path_hash ON tasks(source_path,source_hash)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS tasks_source_key_unique ON tasks(source_key) WHERE source_key!=''")
            # 旧任务没有原因码：按既有阶段与文字补齐一次，历史失败也能按三类分流。
            for row in db.execute(
                    "SELECT id,stage,message FROM tasks WHERE status='needs_review' AND error_code=''").fetchall():
                db.execute("UPDATE tasks SET error_code=? WHERE id=?",
                           (classify_failure(row["stage"], row["message"]), row["id"]))

    def start_action(self, run_id, task_id, attempt_id, stage, step, action, details) -> int:
        with self.connect() as db:
            return db.execute(
                "INSERT INTO run_actions(run_id,task_id,attempt_id,stage,step,action,started_at,result,details)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, attempt_id, stage, step, action, now(), "running",
                 json.dumps(details, ensure_ascii=False)),
            ).lastrowid

    def finish_action(self, action_id, elapsed_ms, result, details) -> None:
        with self.connect() as db:
            db.execute("UPDATE run_actions SET elapsed_ms=?,result=?,details=? WHERE id=?",
                       (elapsed_ms, result, json.dumps(details, ensure_ascii=False), action_id))

    def actions(self, task_id: str, before: int | None = None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM run_actions WHERE task_id=? AND id<? ORDER BY id DESC LIMIT 100",
                (task_id, before if before is not None else 2**63 - 1),
            ).fetchall()
        return [dict(row) | {"details": json.loads(row["details"])} for row in rows]

    def trim_action_history(self, keep: int = 20000) -> None:
        """仅在运行结束时回收旧的已完成动作索引，不触碰任务结果和运行状态。"""
        with self.connect() as db:
            cutoff = db.execute("SELECT id FROM run_actions ORDER BY id DESC LIMIT 1 OFFSET ?",
                                (max(1, keep) - 1,)).fetchone()
            if cutoff:
                db.execute("DELETE FROM run_actions WHERE id<? AND result!='running'", (cutoff["id"],))

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _source_namespace(source: Path) -> str:
        """发布快照使用不可变版本标识；普通目录保留各自绝对路径命名空间。"""
        marker = source / ".materials-snapshot.json"
        if not marker.exists():
            return "raw:" + str(source)
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"素材快照标记无效: {marker}") from exc
        snapshot_id = data.get("snapshot_id") if isinstance(data, dict) else None
        if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", snapshot_id) or snapshot_id != source.name:
            raise ValueError(f"素材快照标记与目录不匹配: {marker}")
        return "published_snapshot:" + snapshot_id

    @staticmethod
    def scan_identity(source: Path, relative_path: str | Path, digest: str,
                      namespace: str | None = None) -> str:
        """提供扫描任务稳定键，供扫描与受控修复共用同一身份规则。"""
        namespace = namespace if namespace is not None else TaskStore._source_namespace(source)
        identity = namespace + "\0" + Path(relative_path).as_posix() + "\0" + digest
        return hashlib.sha256(identity.encode()).hexdigest()

    def scan(self, source: Path, *, reuse_sources: list[Path] | None = None) -> dict:
        source = source.expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ValueError("素材路径必须是文件夹")
        namespace = self._source_namespace(source)
        added = 0
        reusable = []
        for root in reuse_sources or []:
            if root.resolve() != source:
                reusable.append(root.resolve())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for image in sorted(source.rglob("*")):
                if image.is_symlink() or not image.is_file() or image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    continue
                resolved = image.resolve()
                if source not in resolved.parents:
                    continue
                digest = hashlib.sha256(image.read_bytes()).hexdigest()
                relative = image.relative_to(source)
                source_key = self.scan_identity(source, relative, digest, namespace)
                task_id = source_key[:20]
                name = image.parent.name
                current = db.execute("SELECT * FROM tasks WHERE source_key=?", (source_key,)).fetchone()
                count = 0
                if current is None:
                    # 先按原路径精确接管旧任务；路径迁移只允许在已校验的发布快照域内按相对路径和内容匹配。
                    candidates = db.execute("SELECT * FROM tasks WHERE source_path=? AND source_hash=? AND source_key=''",
                                            (str(image), digest)).fetchall()
                    if not candidates and namespace.startswith("published_snapshot:"):
                        suffix = relative.parts
                        snapshot_id = namespace.removeprefix("published_snapshot:")
                        candidates = [row for row in db.execute(
                            "SELECT * FROM tasks WHERE source_hash=? AND source_key=''", (digest,)
                        ).fetchall() if len(Path(row["source_path"]).parts) > len(suffix)
                            and Path(row["source_path"]).parts[-len(suffix):] == suffix
                            and Path(row["source_path"]).parts[-len(suffix) - 1] == snapshot_id]
                    if len(candidates) > 1:
                        raise ValueError(f"存在多个可接管的历史任务，请先显式修复: {relative}")
                    if candidates:
                        current = candidates[0]
                        db.execute("UPDATE tasks SET source_key=?,source_path=? WHERE id=?",
                                   (source_key, str(image), current["id"]))
                    else:
                        count = db.execute(
                            "INSERT OR IGNORE INTO tasks(id,source_name,source_path,source_hash,source_key,updated_at) VALUES(?,?,?,?,?,?)",
                            (task_id, name, str(image), digest, source_key, now()),
                        ).rowcount
                        current = db.execute("SELECT * FROM tasks WHERE source_key=?", (source_key,)).fetchone()
                    if current is None:
                        raise ValueError(f"新任务 ID 冲突，请先显式修复: {relative}")
                if current["source_path"] != str(image):
                    db.execute("UPDATE tasks SET source_path=? WHERE id=?", (str(image), current["id"]))
                added += count
                current = db.execute("SELECT * FROM tasks WHERE source_key=?", (source_key,)).fetchone()
                if current["status"] == "captured" and current["output_dir"]:
                    latest = db.execute("SELECT source_hash FROM tasks WHERE output_dir=? AND status='captured' ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                                        (current["output_dir"],)).fetchone()
                    if (not latest or latest["source_hash"] != digest
                            or not any(Path(current["output_dir"]).glob("page-*.png"))):
                        db.execute("UPDATE tasks SET status='pending',stage='queued',message=? WHERE id=?",
                                   ("此前结果已被替换或移除，请重新采集", current["id"]))
                if count and reusable:
                    # 仅继承同一清洗批次中、同门店且二维码字节相同的成功结果。
                    for old_root in reusable:
                        old_path = old_root / image.relative_to(source)
                        old = db.execute(
                            "SELECT * FROM tasks WHERE source_path=? AND source_hash=? AND status='captured'",
                            (str(old_path), digest),
                        ).fetchone()
                        latest = db.execute("SELECT source_hash FROM tasks WHERE output_dir=? AND status='captured' ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                                            (old["output_dir"],)).fetchone() if old else None
                        if (old and latest and latest["source_hash"] == digest and old["output_dir"]
                                and any(Path(old["output_dir"]).glob("page-*.png"))):
                            db.execute("UPDATE tasks SET status='captured',stage='captured',message=?,output_dir=?,inherited_from=? WHERE id=?",
                                       ("沿用此前相同二维码的采集结果", old["output_dir"], old["id"], current["id"]))
                            break
            total = db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        return {"added": added, "total": total}

    def tasks(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY source_path,id")]

    def rejections(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT t.*, a.run_id, r.material_batch_id,
                    b.name AS material_batch_name
                FROM tasks t
                LEFT JOIN run_actions a ON a.id=(
                    SELECT MAX(latest.id) FROM run_actions latest WHERE latest.task_id=t.id
                )
                LEFT JOIN runs r ON r.id=a.run_id
                LEFT JOIN material_batches b ON b.id=r.material_batch_id
                WHERE t.reject_reason<>'' AND (b.id IS NULL OR b.deleted_at='')
                ORDER BY t.rejected_at DESC,t.id
            """).fetchall()
        return [dict(row) for row in rows]

    def get(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("任务不存在")
        return dict(row)

    def next(self, source: Path) -> dict | None:
        root = source.expanduser().resolve()
        for task in self.tasks():
            if task["status"] == "pending" and root in Path(task["source_path"]).parents:
                return task
        return None

    def update(self, task_id: str, **changes) -> None:
        allowed = {"status", "stage", "message", "output_dir", "reject_reason", "reject_detail", "rejected_at", "error_code"}
        if not changes or not set(changes) <= allowed:
            raise ValueError("无效的任务状态字段")
        if changes.get("status") == "captured":
            # 成功采集是采集阶段问题的解决事实；重试排队时继续保留原不合格记录。
            changes.setdefault("reject_reason", "")
            changes.setdefault("reject_detail", "")
            changes.setdefault("rejected_at", "")
            # 下一轮成功采集后，旧失败原因不能继续留在任务行上。
            changes.setdefault("error_code", "")
        changes["updated_at"] = now()
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?",
                (*changes.values(), task_id),
            )

    def create_run(self, run_id: str, mode: str, task_ids: list[str], message: str = "批次已启动", *, material: dict | None = None) -> dict:
        stamp = now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,mode,status,task_ids,current_index,current_task_id,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, mode, "running", json.dumps(task_ids, ensure_ascii=False), 0,
                 task_ids[0] if task_ids else "", message, stamp, stamp),
            )
            if material:
                db.execute("UPDATE runs SET material_version_id=?,material_batch_id=?,material_version=?,material_snapshot=? WHERE id=?",
                           (material["id"], material["batch_id"], material["version"], material["snapshot_id"], run_id))
        return self.get_run(run_id)

    def material_binding(self, source: str) -> dict | None:
        if not source or not (Path(source) / ".materials-snapshot.json").is_file():
            return None
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_versions'").fetchone():
                return None
            row = db.execute("SELECT v.* FROM material_selection s JOIN material_versions v ON v.id=s.version_id "
                             "JOIN material_batches b ON b.id=v.batch_id WHERE s.singleton=1 AND b.deleted_at=''").fetchone()
            if not row or Path(source).name != row["snapshot_id"]:
                row = db.execute("SELECT v.* FROM material_versions v JOIN material_batches b ON b.id=v.batch_id "
                                 "WHERE v.snapshot_id=? AND b.deleted_at='' ORDER BY v.rowid DESC LIMIT 1",
                                 (Path(source).name,)).fetchone()
            if not row:
                return None
        return dict(row)

    def complete_capture(self, task_id: str, destination: Path, message: str) -> None:
        """同一结果路径替换后，旧二维码版本不能再读取新结果。"""
        path = str(destination)
        stamp = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT source_hash FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise ValueError("任务不存在")
            db.execute(
                """UPDATE tasks SET output_dir='',
                    stage=CASE WHEN status='captured' THEN 'queued' ELSE stage END,
                    message=CASE WHEN status='captured' THEN '同批次门店已有新素材的截图，请重新采集此版本' ELSE message END,
                    status=CASE WHEN status='captured' THEN 'pending' ELSE status END
                    WHERE output_dir=? AND source_hash!=?""",
                (path, task["source_hash"]),
            )
            db.execute(
                """UPDATE tasks SET status='captured',stage='captured',message=?,output_dir=?,
                    error_code='',reject_reason='',reject_detail='',rejected_at='',updated_at=? WHERE id=?""",
                (message, path, stamp, task_id),
            )
            db.execute("DELETE FROM publish_transactions WHERE task_id=?", (task_id,))

    def update_run(self, run_id: str, **changes) -> None:
        allowed = {"status", "current_index", "current_task_id", "message"}
        if not changes or not set(changes) <= allowed:
            raise ValueError("无效的批次状态字段")
        changes["updated_at"] = now()
        with self.connect() as db:
            db.execute("UPDATE runs SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?",
                       (*changes.values(), run_id))

    def request_run_state(self, run_id: str, status: str, message: str) -> None:
        # 批次可能在请求抵达时刚好结束，终态由工作线程保留。
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?,message=?,updated_at=? WHERE id=?"
                " AND status IN ('running','resuming','pause_requested')",
                (status, message, now(), run_id),
            )

    @staticmethod
    def _run_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        try:
            value["task_ids"] = json.loads(value["task_ids"])
        except (ValueError, TypeError):
            value["task_ids"] = []
        return value

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run_dict(row)

    def latest_run(self) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return self._run_dict(row)

    def begin_publish(self, task_id: str, output_root: Path, staging: Path, destination: Path,
                      backup: Path, had_previous: bool) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO publish_transactions(task_id,output_root,staging,destination,backup,had_previous,phase,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, str(output_root), str(staging), str(destination), str(backup), int(had_previous), "prepared", now()),
            )

    def update_publish(self, task_id: str, phase: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE publish_transactions SET phase=?,updated_at=? WHERE task_id=?", (phase, now(), task_id))

    def publish_transactions(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM publish_transactions ORDER BY updated_at")]

    def finish_publish(self, task_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM publish_transactions WHERE task_id=?", (task_id,))

    def recover(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE tasks SET status='needs_review',message='上次执行中断，请检查现有截图后重试',updated_at=? WHERE status='running'", (now(),))
            db.execute("UPDATE runs SET status='interrupted',message='服务上次中断；当前门店需检查，恢复前会重新确认微信安全起点',updated_at=? WHERE status IN ('running','pause_requested','resuming','terminating')", (now(),))

    def event(self, message: str, level: str = "info", task_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events(time,level,message,task_id) VALUES(?,?,?,?)", (now(), level, message, task_id))

    def events(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in reversed(list(db.execute("SELECT time,level,message,task_id FROM events ORDER BY id DESC LIMIT 150")))]
