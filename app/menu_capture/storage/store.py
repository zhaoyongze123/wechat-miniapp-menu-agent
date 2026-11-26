"""任务与运行的 SQLite 存储：tasks、runs、run_actions、events 的唯一事实源。

关键约定：
- 所有任务状态迁移都经过本模块，其他层不直接改表；
- 建表与增量迁移在初始化时幂等执行，兼容历史库；
- 查询只返回事实，不在这里做重试、去重等业务判断。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from menu_capture.core.config import ROOT
from menu_capture.quality.failures import classify as classify_failure


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class TaskStore:
    """SQLite 是任务状态唯一来源；图片和来源清单是可追溯的输出工件。"""

    def __init__(self, path: Path = ROOT / "data/tasks.sqlite3"):
        self.path = path
        self.bind_assignments = None
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
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS capture_run_results (
                    run_id TEXT NOT NULL, task_id TEXT NOT NULL, store_name TEXT NOT NULL,
                    batch_id TEXT NOT NULL DEFAULT '', batch_name TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '',
                    reject_reason TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(run_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS run_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, attempt_id TEXT NOT NULL, stage TEXT NOT NULL,
                    step INTEGER, action TEXT NOT NULL, started_at TEXT NOT NULL,
                    elapsed_ms INTEGER, result TEXT NOT NULL, details TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_actions_task ON run_actions(task_id, id);
                -- 状态接口按 run_id 聚合已处理任务；缺少该索引时每次轮询都会全表扫描 run_actions。
                CREATE INDEX IF NOT EXISTS run_actions_run ON run_actions(run_id, task_id);
                CREATE TABLE IF NOT EXISTS publish_transactions (
                    task_id TEXT PRIMARY KEY, output_root TEXT NOT NULL, staging TEXT NOT NULL,
                    destination TEXT NOT NULL, backup TEXT NOT NULL, had_previous INTEGER NOT NULL,
                    phase TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                -- GUI Agent 伴随提取的门店名称：按采集轮次 + 任务隔离，不占用采集结果字段；
                -- 空字符串表示模型显式撤销，保留行便于追溯“确认后又撤销”。
                CREATE TABLE IF NOT EXISTS observed_store_names (
                    run_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    store_name TEXT NOT NULL DEFAULT '', frame_id TEXT NOT NULL DEFAULT '',
                    frame TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, task_id)
                );
                CREATE INDEX IF NOT EXISTS observed_store_names_task
                    ON observed_store_names(task_id, updated_at);
            """)

        with self.connect() as db:
            # 项目归属只从轮次逐店的批次快照回填。旧任务若没有可核实批次，保持未知。
            for table, column in (("runs", "project_id"), ("capture_run_results", "project_id")):
                if column not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_batches'").fetchone():
                batch_columns = {row["name"] for row in db.execute("PRAGMA table_info(material_batches)")}
                if "project_id" in batch_columns:
                    db.execute("""UPDATE capture_run_results SET project_id=(
                        SELECT b.project_id FROM material_batches b
                        WHERE b.id=capture_run_results.batch_id)
                        WHERE project_id='' AND batch_id<>'' AND EXISTS (
                            SELECT 1 FROM material_batches b WHERE b.id=capture_run_results.batch_id
                              AND b.project_id IS NOT NULL AND b.project_id<>'')""")
                    db.execute("""UPDATE runs SET project_id=(
                        SELECT MIN(c.project_id) FROM capture_run_results c WHERE c.run_id=runs.id
                          AND c.project_id<>'')
                        WHERE project_id='' AND EXISTS (
                            SELECT 1 FROM capture_run_results c WHERE c.run_id=runs.id)
                          AND NOT EXISTS (
                            SELECT 1 FROM capture_run_results c WHERE c.run_id=runs.id
                              AND c.project_id='')
                          AND (SELECT COUNT(DISTINCT c.project_id) FROM capture_run_results c
                               WHERE c.run_id=runs.id AND c.project_id<>'')=1""")
            db.execute("CREATE INDEX IF NOT EXISTS capture_run_project ON capture_run_results(project_id,run_id)")
            for column in ("material_version_id", "material_batch_id", "material_version", "material_snapshot"):
                if column not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_batches'").fetchone():
                batch_columns = {row["name"] for row in db.execute("PRAGMA table_info(material_batches)")}
                if "project_id" in batch_columns:
                    db.execute("""UPDATE runs SET project_id=(SELECT b.project_id FROM material_batches b
                        WHERE b.id=runs.material_batch_id)
                        WHERE project_id='' AND material_batch_id<>'' AND EXISTS (
                            SELECT 1 FROM material_batches b WHERE b.id=runs.material_batch_id
                              AND b.project_id IS NOT NULL AND b.project_id<>'')""")
            if "run_id" not in {row["name"] for row in db.execute("PRAGMA table_info(publish_transactions)")}:
                db.execute("ALTER TABLE publish_transactions ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
            # 本轮采集模式：auto=全自动 Agent，manual=半自动人工；空值代表历史批次。
            if "capture_mode" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN capture_mode TEXT NOT NULL DEFAULT ''")
            # 任务名称：绑定批次时自动取批次名，跨批次重跑由用户起名；历史记录为空时按旧逻辑回退。
            if "name" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            if "inherited_from" not in {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN inherited_from TEXT NOT NULL DEFAULT ''")
            task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            # 历史归集：归档后不再进入不合格清单，但保留原始记录可追溯。
            if "archived_at" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT NOT NULL DEFAULT ''")
            for column in ("reject_reason", "reject_detail", "rejected_at", "reject_evidence", "reject_reviewed_at"):
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
        # WAL 让并发 worker 的短事务读写互不阻塞；busy_timeout 缓解写-写瞬时冲突。
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
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
                    # 同批次重发其他门店时，待修门店的旧码在新快照中仍不可采集。
                    blocked = any(db.execute(
                        "SELECT 1 FROM tasks WHERE source_path=? AND source_hash=? AND stage='returned_to_materials'",
                        (str(old_root / relative), digest),
                    ).fetchone() for old_root in reusable)
                    if blocked:
                        db.execute("UPDATE tasks SET status='blocked',stage='returned_to_materials',message=? WHERE id=?",
                                   ("待重新选码并发布", current["id"]))
                        continue
                    # 二维码字节未变时按门店继承此前状态；只有内容变化才重新排队。
                    for old_root in reusable:
                        old_path = old_root / image.relative_to(source)
                        old = db.execute(
                            "SELECT * FROM tasks WHERE source_path=? AND source_hash=? "
                            "ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                            (str(old_path), digest),
                        ).fetchone()
                        if old is None or old["status"] in {"pending", "running"}:
                            continue
                        if old["status"] == "captured":
                            latest = db.execute("SELECT source_hash FROM tasks WHERE output_dir=? AND status='captured' ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                                                (old["output_dir"],)).fetchone()
                            if not (latest and latest["source_hash"] == digest and old["output_dir"]
                                    and any(Path(old["output_dir"]).glob("page-*.png"))):
                                continue
                            db.execute("UPDATE tasks SET status='captured',stage='captured',message=?,output_dir=?,inherited_from=? WHERE id=?",
                                       ("沿用此前相同二维码的采集结果", old["output_dir"], old["id"], current["id"]))
                            break
                        # 待检查 / 待重试 / 失败 / 门店不可采 / 不合格等状态整体沿用，
                        # 复核证据和失败原因一并带过来，避免换个快照目录就丢失。
                        evidence = old["output_dir"] if old["output_dir"] and any(
                            Path(old["output_dir"]).glob("page-*.png")) else ""
                        db.execute(
                            "UPDATE tasks SET status=?,stage=?,message=?,error_code=?,reject_reason=?,"
                            "reject_detail=?,rejected_at=?,reject_evidence=?,reject_reviewed_at=?,"
                            "output_dir=?,inherited_from=? WHERE id=?",
                            (old["status"], old["stage"], old["message"], old["error_code"], old["reject_reason"],
                             old["reject_detail"], old["rejected_at"], old["reject_evidence"],
                             old["reject_reviewed_at"], evidence, old["id"], current["id"]))
                        break
            total = db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        return {"added": added, "total": total}

    def tasks(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY source_path,id")]

    def rejections(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT t.*, a.run_id, br.material_batch_id,
                    b.name AS material_batch_name
                FROM tasks t
                LEFT JOIN run_actions a ON a.id=(
                    SELECT MAX(latest.id) FROM run_actions latest WHERE latest.task_id=t.id
                )
                -- 取最近一次真正绑定素材批次的运行；后续未绑定批次的运行不应让记录重新出现在不合格清单里。
                LEFT JOIN runs br ON br.id=(
                    SELECT r2.id FROM run_actions a2 JOIN runs r2 ON r2.id=a2.run_id
                    WHERE a2.task_id=t.id AND r2.material_batch_id IS NOT NULL AND r2.material_batch_id<>''
                    ORDER BY a2.id DESC LIMIT 1
                )
                LEFT JOIN material_batches b ON b.id=br.material_batch_id
                WHERE t.reject_reason<>'' AND COALESCE(t.archived_at,'')='' AND (b.id IS NULL OR b.deleted_at='')
                ORDER BY t.rejected_at DESC,t.id
            """).fetchall()
            items = [dict(row) for row in rows]
            # 部分历史任务的运行没有写 material_batch_id；按来源路径反推批次，
            # 保证控制台与按批次筛选的不合格清单使用同一归属口径。
            self._bind_missing_batches(db, items)
        return [item for item in items if not item.pop("_deleted_batch", False)]

    def review_candidates(self) -> list[dict]:
        """跨批次的待检查任务：直接以任务状态为事实源，带上最近一次绑定的素材批次。"""
        with self.connect() as db:
            rows = db.execute("""
                SELECT t.*, a.run_id, br.material_batch_id,
                    b.name AS material_batch_name
                FROM tasks t
                LEFT JOIN run_actions a ON a.id=(
                    SELECT MAX(latest.id) FROM run_actions latest WHERE latest.task_id=t.id
                )
                LEFT JOIN runs br ON br.id=(
                    SELECT r2.id FROM run_actions a2 JOIN runs r2 ON r2.id=a2.run_id
                    WHERE a2.task_id=t.id AND r2.material_batch_id IS NOT NULL AND r2.material_batch_id<>''
                    ORDER BY a2.id DESC LIMIT 1
                )
                LEFT JOIN material_batches b ON b.id=br.material_batch_id
                WHERE t.status IN ('needs_review','needs_retry','error','blocked','failed')
                  AND COALESCE(t.reject_reason,'')=''
                  AND COALESCE(t.archived_at,'')=''
                  AND (b.id IS NULL OR b.deleted_at='')
                ORDER BY t.updated_at DESC,t.id
            """).fetchall()
            items = [dict(row) for row in rows]
            self._bind_missing_batches(db, items)
        return [item for item in items if not item.pop("_deleted_batch", False)]

    @staticmethod
    def _match_batch_path(mappings, path: str):
        """按来源路径匹配素材批次：发布快照路径含 snapshot_id，普通目录含 source_root。

        列表回填与打回素材清洗共用这一条规则，避免两处口径不一致。
        """
        return next((
            mapping for mapping in mappings
            if (mapping["source_root"] and mapping["source_root"] in path)
            or (mapping["snapshot_id"] and mapping["snapshot_id"] in path)
        ), None)

    @staticmethod
    def _bind_missing_batches(db, items: list[dict]) -> None:
        """按来源路径回填缺失的批次归属；删除中的批次标记为非当前记录。"""
        missing = [item for item in items if not item.get("material_batch_id")]
        if not missing:
            return
        mappings = db.execute(
            "SELECT b.id,b.name,b.source_root,b.deleted_at,v.snapshot_id "
            "FROM material_batches b LEFT JOIN material_versions v ON v.batch_id=b.id"
        ).fetchall()
        for item in missing:
            matched = TaskStore._match_batch_path(mappings, item.get("source_path") or "")
            if matched is None:
                continue
            if matched["deleted_at"]:
                item["_deleted_batch"] = True
                continue
            item["material_batch_id"] = matched["id"]
            item["material_batch_name"] = matched["name"]

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

    def task_material_binding(self, task_id: str) -> dict:
        """返回任务所属素材批次：优先运行记录，缺失时按来源路径回退。

        导入或被继承的任务没有带批次绑定的运行记录，但来源路径仍指向素材目录或
        发布快照；这里复用列表回填的同一规则，保证打回素材清洗与列表口径一致。
        """
        with self.connect() as db:
            row = db.execute("""
                SELECT r.material_batch_id AS batch_id, b.name AS batch_name,
                       b.deleted_at AS batch_deleted
                FROM run_actions a JOIN runs r ON r.id=a.run_id
                LEFT JOIN material_batches b ON b.id=r.material_batch_id
                WHERE a.task_id=? AND r.material_batch_id IS NOT NULL AND r.material_batch_id<>''
                ORDER BY a.id DESC LIMIT 1
            """, (task_id,)).fetchone()
            # 运行记录可能指向已删除批次；只有批次仍有效时才直接采用。
            if row and row["batch_name"] and not row["batch_deleted"]:
                return {"batch_id": row["batch_id"], "batch_name": row["batch_name"]}
            task = db.execute("SELECT source_path FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                return {"batch_id": "", "batch_name": ""}
            mappings = db.execute(
                "SELECT b.id,b.name,b.source_root,b.deleted_at,v.snapshot_id "
                "FROM material_batches b LEFT JOIN material_versions v ON v.batch_id=b.id"
            ).fetchall()
        matched = self._match_batch_path(mappings, task["source_path"] or "")
        if matched is None or matched["deleted_at"]:
            return {"batch_id": "", "batch_name": ""}
        return {"batch_id": matched["id"], "batch_name": matched["name"]}

    def update(self, task_id: str, *, capture_run_id: str = "",
               attempt_outcome: str = "", attempt_message: str = "", **changes) -> None:
        allowed = {"status", "stage", "message", "output_dir", "reject_reason", "reject_detail", "rejected_at",
                   "reject_evidence", "reject_reviewed_at", "error_code"}
        if not changes or not set(changes) <= allowed:
            raise ValueError("无效的任务状态字段")
        if changes.get("status") == "captured":
            # 成功采集是采集阶段问题的解决事实；重试排队时继续保留原不合格记录。
            changes.setdefault("reject_reason", "")
            changes.setdefault("reject_detail", "")
            changes.setdefault("rejected_at", "")
            changes.setdefault("reject_evidence", "")
            changes.setdefault("reject_reviewed_at", "")
            # 下一轮成功采集后，旧失败原因不能继续留在任务行上。
            changes.setdefault("error_code", "")
        changes["updated_at"] = now()
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?",
                (*changes.values(), task_id),
            )
            if capture_run_id and attempt_outcome:
                self._record_capture_result(db, capture_run_id, task_id, attempt_outcome,
                                            changes.get("reject_reason", ""),
                                            attempt_message or changes.get("message", ""))

    def create_run(self, run_id: str, mode: str, task_ids: list[str], message: str = "批次已启动", *, material: dict | None = None,
                   capture_mode: str = "", name: str = "", project_id: str = "") -> dict:
        stamp = now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,mode,status,task_ids,current_index,current_task_id,message,created_at,updated_at,capture_mode,name,project_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, mode, "running", json.dumps(task_ids, ensure_ascii=False), 0,
                 task_ids[0] if task_ids else "", message, stamp, stamp, capture_mode, name, project_id),
            )
            if self.bind_assignments:
                self.bind_assignments(db, run_id, task_ids)
            if material:
                db.execute("UPDATE runs SET material_version_id=?,material_batch_id=?,material_version=?,material_snapshot=? WHERE id=?",
                           (material["id"], material["batch_id"], material["version"], material["snapshot_id"], run_id))
            self._capture_run_members(db, run_id, task_ids, material, project_id=project_id)
        return self.get_run(run_id)

    @staticmethod
    def _capture_run_members(db, run_id: str, task_ids: list[str], material: dict | None,
                              *, project_id: str = "") -> None:
        """冻结本轮门店名和来源版本；后续同步、重跑或换版不会改变这份归属。"""
        has_worker = db.execute("SELECT 1 FROM sqlite_master WHERE name='worker_assignments'").fetchone()
        has_versions = db.execute("SELECT 1 FROM sqlite_master WHERE name='material_versions'").fetchone()
        assigned = {}
        if has_worker:
            worker_columns = {row["name"] for row in db.execute("PRAGMA table_info(worker_assignments)")}
            project_column = ",project_id" if "project_id" in worker_columns else ",'' AS project_id"
            assigned = {row["task_id"]: dict(row) for row in db.execute(
                "SELECT task_id,batch_id,batch_name,revision AS version" + project_column +
                " FROM worker_assignments WHERE run_id=? ORDER BY rowid",
                (run_id,))}
        versions = {}
        if has_versions:
            batch_columns = {row["name"] for row in db.execute("PRAGMA table_info(material_batches)")}
            project_column = "b.project_id" if "project_id" in batch_columns else "'' AS project_id"
            versions = {row["snapshot_id"]: dict(row) for row in db.execute(
                f"SELECT v.snapshot_id,v.batch_id,b.name AS batch_name,v.version,{project_column} FROM material_versions v "
                "JOIN material_batches b ON b.id=v.batch_id ORDER BY v.rowid")}
        rows = []
        # 一轮常规限制为 300 家，逐店查询还能兼容更早的自定义长批次。
        for task_id in dict.fromkeys(task_ids):
            task = db.execute("SELECT source_name,source_path FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                continue
            snapshot = Path(task["source_path"]).parent.parent.name
            context = assigned.get(task_id) or versions.get(snapshot) or material or {}
            owner = context.get("project_id") or project_id or ""
            rows.append((run_id, task_id, task["source_name"], context.get("batch_id", ""),
                         context.get("batch_name", ""), context.get("version", ""), owner))
        db.executemany("INSERT INTO capture_run_results(run_id,task_id,store_name,batch_id,batch_name,version,project_id)"
                       " VALUES(?,?,?,?,?,?,?)", rows)

    @staticmethod
    def _record_capture_result(db, run_id: str, task_id: str, outcome: str,
                               reject_reason: str, message: str) -> None:
        # 采集结果和门店状态同一事务写入。运行恢复可更新本轮同一家，历史轮次独立保存。
        db.execute("UPDATE capture_run_results SET outcome=?,reject_reason=?,message=? WHERE run_id=? AND task_id=?",
                   (outcome, reject_reason, message, run_id, task_id))

    def capture_run_results(self, run_ids: list[str]) -> dict[str, list[dict]]:
        if not run_ids:
            return {}
        indexed: dict[str, list[dict]] = {}
        placeholders = ",".join("?" for _ in run_ids)
        with self.connect() as db:
            for row in db.execute(f"SELECT * FROM capture_run_results WHERE run_id IN ({placeholders})", run_ids):
                indexed.setdefault(row["run_id"], []).append(dict(row))
        return indexed

    def record_observed_store_name(self, run_id: str, task_id: str, name: str,
                                   *, frame_id: str = "", frame: str = "") -> None:
        """写入 GUI Agent 伴随提取的门店名称。

        只按本次采集轮次 + 任务隔离：重跑或换素材版本使用新的 run_id，不会误继承旧结果。
        名称在收到时立即独立落库，后续会话重建、截图失败或任务失败都不清除（空值=显式撤销）。
        """
        run_id, task_id = str(run_id or ""), str(task_id or "")
        if not run_id or not task_id:
            # 不在采集轮次内的试验/手工导航没有轮次归属，只保留在报告工件里。
            return
        with self.connect() as db:
            db.execute(
                "INSERT INTO observed_store_names(run_id,task_id,store_name,frame_id,frame,updated_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(run_id,task_id) DO UPDATE SET store_name=excluded.store_name,"
                " frame_id=excluded.frame_id, frame=excluded.frame, updated_at=excluded.updated_at",
                (run_id, task_id, str(name or ""), str(frame_id or ""), str(frame or ""), now()),
            )

    def observed_store_name(self, run_id: str, task_id: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT store_name FROM observed_store_names WHERE run_id=? AND task_id=?",
                             (str(run_id or ""), str(task_id or ""))).fetchone()
        return str(row["store_name"] or "") if row else ""

    def observed_store_name_current(self, task_id: str, *, project_id: str | None = None) -> str:
        """按任务当前采集轮次读取名称，绝不回退到历史轮次。

        任务行不保存 run_id，当前轮次以最近一次包含该任务的运行为准（runs.updated_at）。
        当前轮次没有观察结果时返回空，避免历史或迟到名称掩盖本轮的显式撤销与换版。
        """
        task_id = str(task_id or "")
        if not task_id:
            return ""
        with self.connect() as db:
            project_filter = " AND c.project_id=?" if project_id is not None else ""
            params = (task_id, project_id) if project_id is not None else (task_id,)
            row = db.execute(
                "SELECT o.store_name FROM capture_run_results c"
                " JOIN runs r ON r.id=c.run_id"
                " LEFT JOIN observed_store_names o ON o.run_id=c.run_id AND o.task_id=c.task_id"
                " WHERE c.task_id=?" + project_filter + " ORDER BY r.updated_at DESC, r.rowid DESC LIMIT 1",
                params).fetchone()
        return str(row["store_name"] or "") if row else ""

    def observed_store_name_record(self, run_id: str, task_id: str) -> dict:
        """按轮次 + 任务取完整观察记录，供跨会话恢复名称时带出同一帧引用。"""
        with self.connect() as db:
            row = db.execute("SELECT * FROM observed_store_names WHERE run_id=? AND task_id=?",
                             (str(run_id or ""), str(task_id or ""))).fetchone()
        return dict(row) if row else {}

    def observed_store_names(self, run_id: str, *, project_id: str | None = None) -> dict[str, str]:
        """本轮各任务已确认的名称；显式撤销（空值）不出现在结果里。"""
        with self.connect() as db:
            if project_id is None:
                rows = db.execute("SELECT task_id,store_name FROM observed_store_names"
                                  " WHERE run_id=? AND store_name!=''", (str(run_id or ""),)).fetchall()
            else:
                rows = db.execute("SELECT o.task_id,o.store_name FROM observed_store_names o"
                                  " JOIN capture_run_results c ON c.run_id=o.run_id AND c.task_id=o.task_id"
                                  " WHERE o.run_id=? AND c.project_id=? AND o.store_name!=''",
                                  (str(run_id or ""), project_id)).fetchall()
        return {row["task_id"]: row["store_name"] for row in rows}

    def observed_store_name_latest(self, task_id: str, *, project_id: str | None = None) -> dict:
        """按原始任务编号查最近一次观察记录，含未采集成功的轮次。"""
        with self.connect() as db:
            project_filter = " AND c.project_id=?" if project_id is not None else ""
            params = (str(task_id or ""), project_id) if project_id is not None else (str(task_id or ""),)
            row = db.execute("SELECT o.* FROM observed_store_names o JOIN capture_run_results c"
                             " ON c.run_id=o.run_id AND c.task_id=o.task_id WHERE o.task_id=?" + project_filter
                             + " ORDER BY o.updated_at DESC,o.run_id DESC LIMIT 1", params).fetchone()
        return dict(row) if row else {}

    def material_binding(self, source: str) -> dict | None:
        if not source or not (Path(source) / ".materials-snapshot.json").is_file():
            return None
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_versions'").fetchone():
                return None
            row = db.execute("SELECT v.*, b.name AS batch_name FROM material_selection s "
                             "JOIN material_versions v ON v.id=s.version_id "
                             "JOIN material_batches b ON b.id=v.batch_id WHERE s.singleton=1 AND b.deleted_at=''").fetchone()
            if not row or Path(source).name != row["snapshot_id"]:
                row = db.execute("SELECT v.*, b.name AS batch_name FROM material_versions v "
                                 "JOIN material_batches b ON b.id=v.batch_id "
                                 "WHERE v.snapshot_id=? AND b.deleted_at='' ORDER BY v.rowid DESC LIMIT 1",
                                 (Path(source).name,)).fetchone()
            if not row:
                return None
        return dict(row)

    def material_batch_name(self, batch_id: str) -> str:
        """批次目录用用户起的批次名称；查不到名称时返回空串，由调用方回退到 ID。"""
        if not batch_id:
            return ""
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_batches'").fetchone():
                return ""
            row = db.execute("SELECT name FROM material_batches WHERE id=?", (batch_id,)).fetchone()
        return (row["name"] or "") if row else ""

    def default_run_name(self, task_ids: list[str]) -> str:
        """未指定任务名时的默认值：同一素材批次沿用批次名+版本，跨批次按重跑时间命名。"""
        bindings: list[tuple[str, str]] = []
        for task_id in task_ids:
            task = self.get(task_id)
            if not task:
                continue
            snapshot = Path(task["source_path"]).resolve().parent.parent
            binding = self.material_binding(str(snapshot))
            if binding:
                bindings.append((binding.get("batch_name") or "", binding.get("version") or ""))
        unique = list(dict.fromkeys(bindings))
        if len(unique) == 1 and unique[0][0]:
            return " · ".join(value for value in unique[0] if value)
        return "待检查重跑 · " + datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

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
                    error_code='',reject_reason='',reject_detail='',rejected_at='',reject_reviewed_at='',updated_at=? WHERE id=?""",
                (message, path, stamp, task_id),
            )
            publication = db.execute("SELECT run_id FROM publish_transactions WHERE task_id=?", (task_id,)).fetchone()
            if publication and publication["run_id"]:
                self._record_capture_result(db, publication["run_id"], task_id, "captured", "", message)
            db.execute("DELETE FROM publish_transactions WHERE task_id=?", (task_id,))

    def update_run(self, run_id: str, **changes) -> None:
        allowed = {"status", "current_index", "current_task_id", "message", "capture_mode", "project_id"}
        if not changes or not set(changes) <= allowed:
            raise ValueError("无效的批次状态字段")
        if "project_id" in changes:
            with self.connect() as db:
                row = db.execute("SELECT project_id FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    raise ValueError("批次不存在")
                if row["project_id"] and row["project_id"] != changes["project_id"]:
                    raise ValueError("采集轮次项目归属不能更改")
        changes["updated_at"] = now()
        with self.connect() as db:
            db.execute("UPDATE runs SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?",
                       (*changes.values(), run_id))

    def rename_run(self, run_id: str, name: str) -> None:
        """只改展示名称；进度、执行时间、批次和结果各自维护，避免与运行线程的写入互相覆盖。"""
        with self.connect() as db:
            db.execute("UPDATE runs SET name=? WHERE id=?", (name, run_id))

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

    def run_history(self, limit: int = 500, *, project_id: str | None = None) -> list[dict]:
        """按项目先过滤，再限量；旧归属未知的轮次不进入项目列表。"""
        with self.connect() as db:
            if project_id is None:
                rows = db.execute("SELECT * FROM runs ORDER BY created_at DESC,rowid DESC LIMIT ?",
                                  (max(1, int(limit)),)).fetchall()
            else:
                rows = db.execute("""SELECT r.* FROM runs r WHERE r.project_id=? OR EXISTS (
                    SELECT 1 FROM capture_run_results c WHERE c.run_id=r.id AND c.project_id=?)
                    ORDER BY r.created_at DESC,r.rowid DESC LIMIT ?""",
                                  (project_id, project_id, max(1, int(limit)))).fetchall()
        return [self._run_dict(row) for row in rows]

    def begin_publish(self, task_id: str, output_root: Path, staging: Path, destination: Path,
                      backup: Path, had_previous: bool, *, run_id: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO publish_transactions(task_id,output_root,staging,destination,backup,had_previous,phase,updated_at,run_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, str(output_root), str(staging), str(destination), str(backup), int(had_previous), "prepared", now(), run_id),
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
