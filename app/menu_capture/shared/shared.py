"""共享后台持有门店分配、采集结果和审核状态，采集端只提交已领取任务的结果。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from PIL import Image

from menu_capture.capture.capture_history import ACTIVE_RUN_STATUSES, capture_history_row
from menu_capture.capture.capture_results import store_state, waiting_for_materials
from menu_capture.materials.materials import REJECTION_REASONS, MaterialService
from menu_capture.storage.store import TaskStore
from menu_capture.projects.service import LEGACY_ID, ProjectConflict, ProjectNotFound, assert_batch_set, get_project
from menu_capture.shared.shared_history import HistoryReport

MAX_RESULT_SIZE = 1024**3
MAX_ARTIFACT_SIZE = 32 * 1024**2
MAX_SOURCE_SIZE = 32 * 1024**2
ACTIVE = ("assigned", "ready")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
# 与 action-schema.json observation.max_chars 一致；Hub 只做防御性校验，异常值按未提供处理。
OBSERVED_NAME_MAX_CHARS = 40
# 服务端统一设备在线判定窗口：避免客户端时钟偏差及 60 秒单阈值边界震荡。
WORKER_ONLINE_SECONDS = 30
WORKER_OFFLINE_SECONDS = 90


def worker_connection_state(last_seen: str | None, *, now: datetime | None = None) -> tuple[str, int | None]:
    """根据服务端时间计算设备连接态（online / degraded / offline）及心跳距今秒数。"""
    if not last_seen or not isinstance(last_seen, str):
        return "offline", None
    try:
        parsed = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
    except ValueError:
        return "offline", None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = max(0, int((current - parsed).total_seconds()))
    if age <= WORKER_ONLINE_SECONDS:
        return "online", age
    if age <= WORKER_OFFLINE_SECONDS:
        return "degraded", age
    return "offline", age


def _observed_store_name(manifest: dict) -> str:
    """读取可选副产物：类型错误、超长或空白一律视为未提供，不影响结果接收。"""
    value = manifest.get("observed_store_name")
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) > OBSERVED_NAME_MAX_CHARS:
        return ""
    return text


REPORTED_RUN_STATUSES = {"running", "pause_requested", "paused", "resuming", "blocked", "interrupted",
                         "terminating", "terminated", "completed", "failed"}


class SharedConflict(ValueError):
    """并发、版本或分配状态已变化，操作可以在刷新后重试。"""

    def __init__(self, message: str, *, conflicts: list[dict] | None = None):
        super().__init__(message)
        self.conflicts = conflicts


class WorkerUnauthorized(ValueError):
    """采集端凭证无效。"""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def safe_artifact(name: str) -> PurePosixPath:
    """约束上传目录与 Windows 可用的字符，防止同名覆盖及路径逃逸。"""
    parts = name.split("/")
    if not name or len(name) > 350 or any(
        not part or part in {".", ".."} or part.endswith((".", " "))
        or re.search(r'[:\\<>|?*"\x00-\x1f]', part)
        for part in parts
    ):
        raise ValueError("采集结果包含无效文件名")
    return PurePosixPath(name)


class SharedHub:
    """SQLite 事务决定分配归属。上传先完整校验落盘，再更新任务和结果账本。"""

    def __init__(self, store: TaskStore, materials: MaterialService):
        self.store = store
        self.materials = materials
        # 由服务端注入：返回 Hub 已校验且未过期的版本元数据，供 Worker 复用。
        self.update_metadata = None
        self.root = store.path.parent / "shared-results"
        self.root.mkdir(parents=True, exist_ok=True)
        # 上次进程中止的上传尚未进入结果事务，可以由客户端重新提交完整包。
        for candidate in self.root.glob(".incoming-*"):
            if candidate.is_file():
                candidate.unlink()
            else:
                shutil.rmtree(candidate)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shared_workers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    last_seen TEXT NOT NULL, pending_uploads INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shared_worker_profiles (
                    worker_id TEXT PRIMARY KEY REFERENCES shared_workers(id),
                    description TEXT NOT NULL DEFAULT '', paired_at TEXT NOT NULL DEFAULT '',
                    revoked_at TEXT NOT NULL DEFAULT '', archived_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS shared_worker_runs (
                    worker_id TEXT PRIMARY KEY REFERENCES shared_workers(id),
                    status TEXT NOT NULL, mode TEXT NOT NULL,
                    processed INTEGER NOT NULL, total INTEGER NOT NULL, reported_at TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS shared_worker_software (
                    worker_id TEXT PRIMARY KEY REFERENCES shared_workers(id),
                    current_version TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '',
                    latest_version TEXT NOT NULL DEFAULT '', update_available INTEGER NOT NULL DEFAULT 0,
                    channel TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL DEFAULT '',
                    download_status TEXT NOT NULL DEFAULT '', install_status TEXT NOT NULL DEFAULT '',
                    reported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_worker_capabilities (
                    worker_id TEXT PRIMARY KEY REFERENCES shared_workers(id),
                    capabilities TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_update_rollout (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    mode TEXT NOT NULL DEFAULT 'all',
                    worker_ids TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS shared_run_history (
                    worker_id TEXT NOT NULL REFERENCES shared_workers(id), run_id TEXT NOT NULL,
                    generation INTEGER NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL,
                    tasks TEXT NOT NULL, synced_at TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(worker_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS shared_run_status ON shared_run_history(status,synced_at);
                CREATE TABLE IF NOT EXISTS shared_pairing (
                    code_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_assignments (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL, batch_name TEXT NOT NULL, store TEXT NOT NULL,
                    store_id TEXT NOT NULL DEFAULT '',
                    revision TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL, source_suffix TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT '',
                    project_code TEXT NOT NULL DEFAULT '', project_name TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS shared_current_store
                    ON shared_assignments(batch_id, store) WHERE status IN ('assigned','ready');
                CREATE INDEX IF NOT EXISTS shared_worker_assignments ON shared_assignments(worker_id,status);
                CREATE INDEX IF NOT EXISTS shared_assignments_task_status ON shared_assignments(task_id,status);
                CREATE TABLE IF NOT EXISTS shared_results (
                    assignment_id TEXT PRIMARY KEY, digest TEXT NOT NULL, disposition TEXT NOT NULL,
                    status TEXT NOT NULL, message TEXT NOT NULL, received_at TEXT NOT NULL
                );
                -- 可选观察名称独立存储，避免给旧版 INSERT ... VALUES 增加列后破坏软件回滚。
                CREATE TABLE IF NOT EXISTS shared_result_names (
                    assignment_id TEXT PRIMARY KEY, store_name TEXT NOT NULL DEFAULT ''
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(shared_results)")}
            self._legacy_observed_store_column = "observed_store_name" in columns
            if self._legacy_observed_store_column:
                # 兼容已经运行过 v0.3.2 的数据库；保留旧列供回滚版本读取，同时迁移到独立表。
                db.execute(
                    "INSERT OR IGNORE INTO shared_result_names(assignment_id,store_name) "
                    "SELECT assignment_id,observed_store_name FROM shared_results"
                )
            from menu_capture.storage.migrations import Migration, apply_migrations

            def _migrate_shared_assignments_project_cols(conn) -> None:
                assignment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(shared_assignments)")}
                for column in ("project_id", "project_code", "project_name", "store_id"):
                    if column not in assignment_columns:
                        conn.execute(f"ALTER TABLE shared_assignments ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS shared_assignments_project_status "
                    "ON shared_assignments(project_id,status,created_at DESC)"
                )

            apply_migrations(
                db,
                [
                    Migration(
                        version="20261128_01_shared_assignments_project_cols_and_index",
                        description="为 shared_assignments 补齐项目与门店列并按顺序建立复合索引",
                        apply=_migrate_shared_assignments_project_cols,
                    ),
                ],
            )
            # 只依据稳定的批次外键回填既有分配；项目删除/改名不会改变已存下来的归属快照。
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone():
                db.execute("""UPDATE shared_assignments SET
                    project_id=(SELECT b.project_id FROM material_batches b WHERE b.id=shared_assignments.batch_id),
                    project_code=(SELECT p.code FROM material_batches b JOIN projects p ON p.id=b.project_id
                                  WHERE b.id=shared_assignments.batch_id),
                    project_name=(SELECT p.name FROM material_batches b JOIN projects p ON p.id=b.project_id
                                  WHERE b.id=shared_assignments.batch_id)
                    WHERE project_id='' AND EXISTS (SELECT 1 FROM material_batches b JOIN projects p
                        ON p.id=b.project_id WHERE b.id=shared_assignments.batch_id)""")
            history_columns = {row["name"] for row in db.execute("PRAGMA table_info(shared_run_history)")}
            if "project_id" not in history_columns:
                db.execute("ALTER TABLE shared_run_history ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
            run_columns = {row["name"] for row in db.execute("PRAGMA table_info(shared_worker_runs)")}
            if "project_id" not in run_columns:
                db.execute("ALTER TABLE shared_worker_runs ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")

    def status(self, *, include_history: bool = True, project_id: str | None = None) -> dict:
        with self.store.connect() as db:
            workers = [dict(row) for row in db.execute("""
                SELECT w.id,w.name,w.last_seen,w.pending_uploads,COALESCE(p.description,'') AS description,
                       COALESCE(p.paired_at,'') AS paired_at,COALESCE(p.revoked_at,'') AS revoked_at,
                       COALESCE(p.archived_at,'') AS archived_at
                FROM shared_workers w LEFT JOIN shared_worker_profiles p ON p.worker_id=w.id
                ORDER BY w.name,w.id
            """)]
            runs = {row["worker_id"]: dict(row) for row in db.execute("SELECT * FROM shared_worker_runs")}
            software = {row["worker_id"]: dict(row) for row in db.execute("SELECT * FROM shared_worker_software")}
            history_times = dict(db.execute("SELECT worker_id,MAX(synced_at) FROM shared_run_history GROUP BY worker_id"))
            rollout_row = db.execute(
                "SELECT mode,worker_ids,updated_at FROM shared_update_rollout WHERE id=1").fetchone()
            now_utc = datetime.now(timezone.utc)
            for worker in workers:
                state, age = worker_connection_state(worker.get("last_seen"), now=now_utc)
                worker["connection_state"] = state
                worker["last_seen_age_seconds"] = age
                last_run = runs.get(worker["id"])
                if project_id is None or not last_run or last_run.get("project_id") != project_id:
                    # 摘要没有可核验的本项目归属时不跨项目展示。
                    last_run = None
                worker["last_run"] = last_run
                worker["history_synced_at"] = history_times.get(worker["id"], "")
                row = software.get(worker["id"])
                # 未上报过的设备明确为 null，不用空对象冒充“已是最新”。
                worker["software"] = ({
                    "current_version": row["current_version"],
                    "platform": row["platform"],
                    "latest_version": row["latest_version"],
                    "update_available": bool(row["update_available"]),
                    "channel": row["channel"],
                    "checked_at": row["checked_at"],
                    "download_status": row["download_status"],
                    "install_status": row["install_status"],
                    "reported_at": row["reported_at"],
                } if row else None)
            assignment_scope = " AND a.project_id=?" if project_id is not None else ""
            assignment_params = (project_id,) if project_id is not None else ()
            if project_id is not None:
                get_project(db, project_id)
            assignments = [dict(row) for row in db.execute("""
                SELECT a.*, COALESCE(r.disposition, '') AS disposition, COALESCE(r.message, '') AS result_message,
                       COALESCE(n.store_name, '') AS observed_store_name
                FROM shared_assignments a LEFT JOIN shared_results r ON a.id=r.assignment_id
                LEFT JOIN shared_result_names n ON a.id=n.assignment_id
                WHERE a.status IN ('assigned','ready')""" + assignment_scope + " ORDER BY a.created_at DESC,a.id",
                assignment_params)]
            if include_history:
                assignments.extend(dict(row) for row in db.execute("""
                    SELECT a.*, COALESCE(r.disposition, '') AS disposition, COALESCE(r.message, '') AS result_message,
                           COALESCE(n.store_name, '') AS observed_store_name
                    FROM shared_assignments a LEFT JOIN shared_results r ON a.id=r.assignment_id
                    LEFT JOIN shared_result_names n ON a.id=n.assignment_id
                    WHERE a.status IN ('completed','reclaimed')""" + assignment_scope +
                    " ORDER BY a.created_at DESC,a.id LIMIT 200", assignment_params))
        if any(row["status"] in ACTIVE for row in assignments):
            current = {(task["batch_id"], task["source_name"]): task["id"]
                       for task in self.materials.effective_capture_tasks(project_id=project_id)}
            for row in assignments:
                if row["status"] in ACTIVE:
                    row["current_task_id"] = current.get((row["batch_id"], row["store"]), row["task_id"])
        target = self._update_target()
        rollout = self._rollout_payload(rollout_row, workers)
        target_version = str((target.get("latest") or {}).get("version") or "") if target else ""
        for worker in workers:
            # 设备是否在本次分发范围内，供设备页展示“本批目标”而不是让用户自行推断。
            worker["in_rollout"] = (not worker.get("archived_at") and not worker.get("revoked_at")
                                    and (rollout["mode"] == "all"
                                         or worker["id"] in rollout["worker_ids"]))
        reported = [worker["software"] for worker in workers if worker.get("software")]
        versions: dict[str, int] = {}
        for row in reported:
            key = row["current_version"] or "unknown"
            versions[key] = versions.get(key, 0) + 1
        update_summary = {
            "reported": len(reported),
            "unreported": len([w for w in workers if not w.get("archived_at") and not w.get("software")]),
            "update_available": sum(1 for row in reported if row["update_available"]),
            "target_version": target_version,
            "versions": versions,
            "rollout_mode": rollout["mode"],
            "rollout_scope": len([w for w in workers if w.get("in_rollout")]),
        }
        return {
            "role": "hub",
            "server_time": utc_stamp(),
            "workers": workers,
            "assignments": assignments,
            "update_summary": update_summary,
            # 分批推广只决定哪些设备收到目标版本；安装仍由各设备在本地确认。
            "update_rollout": {**rollout, "target_version": target_version},
            # 面板只展示目标版本摘要；完整资产元数据只在 Worker 同步响应中分发。
            "update_target": ({
                "version": target_version,
                "channel": str(target.get("channel") or ""),
                "checked_at": float(target.get("checked_at") or 0),
            } if target else None),
        }

    def _rollout_payload(self, row, workers: list[dict]) -> dict:
        """分批推广范围：all 覆盖所有在用设备，batch 只覆盖选中设备。"""
        mode = str(row["mode"]) if row else "all"
        try:
            selected = json.loads(row["worker_ids"]) if row else []
        except (TypeError, ValueError):
            selected = []
        known = {worker["id"] for worker in workers if not worker.get("archived_at") and not worker.get("revoked_at")}
        # 设备解除配对或移除后自动退出范围，不继续向已不在用的设备分发。
        ids = [worker_id for worker_id in selected if worker_id in known]
        if mode not in {"all", "batch"}:
            mode = "all"
        return {"mode": mode, "worker_ids": ids, "updated_at": str(row["updated_at"]) if row else ""}

    def update_rollout(self) -> dict:
        """设备页读取分批范围；目标版本仍来自本机已校验的更新元数据。"""
        with self.store.connect() as db:
            row = db.execute(
                "SELECT mode,worker_ids,updated_at FROM shared_update_rollout WHERE id=1").fetchone()
            workers = [dict(item) for item in db.execute("""
                SELECT w.id,w.name,COALESCE(p.archived_at,'') AS archived_at
                FROM shared_workers w LEFT JOIN shared_worker_profiles p ON p.worker_id=w.id
                ORDER BY w.name,w.id
            """)]
        rollout = self._rollout_payload(row, workers)
        target = self._update_target()
        return {
            **rollout,
            "target_version": str((target.get("latest") or {}).get("version") or "") if target else "",
            "workers": [worker for worker in workers if not worker["archived_at"]],
        }

    def set_update_rollout(self, mode: str, worker_ids: list[str]) -> dict:
        """保存分批范围；只接受当前在用设备，避免把已移除设备写进分发名单。"""
        if mode not in {"all", "batch"}:
            raise ValueError("分批模式无效")
        if (not isinstance(worker_ids, list) or len(worker_ids) > 200
                or any(not isinstance(value, str) or len(value) > 64 for value in worker_ids)):
            raise ValueError("分批设备无效")
        selected = list(dict.fromkeys(worker_ids))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            known = {row["id"] for row in db.execute("""
                SELECT w.id FROM shared_workers w LEFT JOIN shared_worker_profiles p ON p.worker_id=w.id
                WHERE COALESCE(p.archived_at,'')='' AND COALESCE(p.revoked_at,'')=''
            """)}
            if any(worker_id not in known for worker_id in selected):
                raise ValueError("分批设备不存在或已移除")
            if mode == "batch" and not selected:
                raise ValueError("分批推广至少选择一台设备")
            db.execute("""
                INSERT INTO shared_update_rollout(id,mode,worker_ids,updated_at) VALUES(1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,worker_ids=excluded.worker_ids,
                    updated_at=excluded.updated_at
            """, (mode, json.dumps(selected if mode == "batch" else []), utc_stamp()))
        return self.update_rollout()

    def _update_target(self) -> dict | None:
        """调用服务端注入的版本元数据提供者；未注入或异常时按不可用处理。"""
        if self.update_metadata is None:
            return None
        try:
            target = self.update_metadata()
        except Exception:  # noqa: BLE001 - 更新状态异常不能拖垮设备列表
            return None
        return target if isinstance(target, dict) else None

    def pairing_code(self) -> dict:
        code = secrets.token_urlsafe(9)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds")
        with self.store.connect() as db:
            db.execute("DELETE FROM shared_pairing WHERE expires_at<=?", (utc_stamp(),))
            db.execute("INSERT INTO shared_pairing VALUES(?,?)", (_token_hash(code), expires))
        return {"code": code, "expires_at": expires}

    @staticmethod
    def _worker_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64 or any(ord(char) < 32 for char in name):
            raise ValueError("电脑名称应为 1 至 64 个字符")
        return name.strip()

    def pair(self, code: str, name: str) -> dict:
        name = self._worker_name(name)
        if not isinstance(code, str) or not re.fullmatch(r"[\w-]{12}", code, flags=re.ASCII):
            raise ValueError("配对码无效或已过期")
        worker_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("DELETE FROM shared_pairing WHERE code_hash=? AND expires_at>?",
                              (_token_hash(code), utc_stamp()))
            if not used.rowcount:
                raise ValueError("配对码无效或已过期")
            stamp = utc_stamp()
            db.execute("INSERT INTO shared_workers VALUES(?,?,?,?,0)", (worker_id, name, _token_hash(token), stamp))
            db.execute("INSERT INTO shared_worker_profiles(worker_id,paired_at) VALUES(?,?)", (worker_id, stamp))
        return {"id": worker_id, "name": name, "token": token}

    @staticmethod
    def _worker(db, worker_id: str):
        worker = db.execute("""
            SELECT w.id,w.name,p.revoked_at,p.archived_at
            FROM shared_workers w LEFT JOIN shared_worker_profiles p ON p.worker_id=w.id WHERE w.id=?
        """, (worker_id,)).fetchone()
        if not worker:
            raise ValueError("采集电脑不存在")
        return worker

    def edit_worker(self, worker_id: str, name: str, description: str) -> dict:
        name = self._worker_name(name)
        if not isinstance(description, str) or len(description.strip()) > 500:
            raise ValueError("备注最多 500 个字符")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._worker(db, worker_id)["archived_at"]:
                raise SharedConflict("此设备已从设备列表移除")
            db.execute("UPDATE shared_workers SET name=? WHERE id=?", (name, worker_id))
            db.execute("""
                INSERT INTO shared_worker_profiles(worker_id,description) VALUES(?,?)
                ON CONFLICT(worker_id) DO UPDATE SET description=excluded.description
            """, (worker_id, description.strip()))
        return {"id": worker_id, "name": name, "description": description.strip()}

    def revoke_worker(self, worker_id: str) -> dict:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            worker = self._worker(db, worker_id)
            stamp = worker["revoked_at"] or utc_stamp()
            if not worker["revoked_at"]:
                if db.execute("""
                    SELECT 1 FROM shared_assignments WHERE worker_id=? AND status IN ('assigned','ready') LIMIT 1
                """, (worker_id,)).fetchone():
                    raise SharedConflict("还有未结束的分配，请等待同步或先回收任务")
                # 轮换旧凭证，即使临时回滚 Hub，也无法再使用原设备令牌。
                db.execute("UPDATE shared_workers SET token_hash=? WHERE id=?",
                           (_token_hash(secrets.token_urlsafe(32)), worker_id))
                db.execute("""
                    INSERT INTO shared_worker_profiles(worker_id,revoked_at) VALUES(?,?)
                    ON CONFLICT(worker_id) DO UPDATE SET revoked_at=excluded.revoked_at
                """, (worker_id, stamp))
        return {"id": worker_id, "revoked_at": stamp}

    def archive_worker(self, worker_id: str) -> dict:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            worker = self._worker(db, worker_id)
            if not worker["revoked_at"]:
                raise SharedConflict("请先解除配对，再从设备列表移除")
            stamp = worker["archived_at"] or utc_stamp()
            db.execute("UPDATE shared_worker_profiles SET archived_at=? WHERE worker_id=?", (stamp, worker_id))
        return {"id": worker_id, "archived_at": stamp}

    def assignment_records(self, *, worker_id: str, phase: str, query: str, page: int, size: int,
                           project_id: str | None = None) -> dict:
        where = ["1=1"]
        params = []
        if worker_id:
            where.append("a.worker_id=?")
            params.append(worker_id)
        if phase == "history":
            where.append("r.disposition='history'")
        elif phase:
            where.append("a.status=? AND COALESCE(r.disposition,'')<>'history'")
            params.append(phase)
        if query.strip():
            where.append("instr(lower(a.store || ' ' || a.batch_name || ' ' || a.revision || ' ' || a.task_id), lower(?))>0")
            params.append(query.strip())
        if project_id is not None:
            where.append("a.project_id=?")
            params.append(project_id)
        source = ("FROM shared_assignments a JOIN shared_workers w ON w.id=a.worker_id "
                  "LEFT JOIN shared_results r ON r.assignment_id=a.id "
                  "LEFT JOIN shared_result_names n ON n.assignment_id=a.id WHERE " + " AND ".join(where))
        with self.store.connect() as db:
            if project_id is not None:
                get_project(db, project_id)
            if worker_id:
                self._worker(db, worker_id)
            total = db.execute("SELECT COUNT(*) " + source, params).fetchone()[0]
            pages = max(1, (total + size - 1) // size)
            page = min(page, pages)
            records = [dict(row) for row in db.execute(
                "SELECT a.id,a.task_id,a.worker_id,w.name AS worker_name,a.store,a.store_id,a.batch_name,a.revision,a.status,"
                "COALESCE(r.disposition,'') AS disposition,COALESCE(r.message,'') AS result_message,"
                "COALESCE(n.store_name,'') AS observed_store_name " +
                source + " ORDER BY a.created_at DESC,a.id LIMIT ? OFFSET ?", (*params, size, (page - 1) * size))]
        return {"records": records, "total": total, "page": page, "pages": pages}

    def observed_store_name(self, task_id: str, project_id: str | None = None) -> str:
        """按原始任务编号取当前有效采集结果伴随的门店名称。

        只认当前有效结果的归属：任务自身或其 inherited_from 链上的原任务，且 disposition='accepted'
        的同步结果。空字符串表示采集端显式撤销，必须原样返回，不能回退到任何历史名称；
        history（迟到/换版/重采后作废）结果不参与，避免名称与当前结果错配。
        """
        task_id = str(task_id or "")
        if not task_id:
            return ""
        return self.observed_store_names((task_id,), project_id=project_id).get(task_id, "")

    def observed_store_names(self, task_ids, project_id: str | None = None) -> dict[str, str]:
        """批量按原始任务编号取当前有效采集结果伴随的门店名称，避免全量导出/预览产生 N+1 查库。"""
        unique = [str(tid) for tid in dict.fromkeys(str(t or "") for t in task_ids) if tid]
        if not unique:
            return {}
        with self.store.connect() as db:
            # 1. 批量展开 inherited_from 链（最多 5 层，带环保护）
            inherited_map: dict[str, str] = {}
            frontier = set(unique)
            for _ in range(5):
                to_fetch = [tid for tid in frontier if tid not in inherited_map]
                if not to_fetch:
                    break
                next_frontier: set[str] = set()
                for offset in range(0, len(to_fetch), 500):
                    chunk = to_fetch[offset : offset + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    for row in db.execute(f"SELECT id, inherited_from FROM tasks WHERE id IN ({placeholders})", chunk):
                        parent = str(row["inherited_from"] or "")
                        inherited_map[str(row["id"])] = parent
                        if parent and parent not in inherited_map:
                            next_frontier.add(parent)
                for tid in to_fetch:
                    inherited_map.setdefault(tid, "")
                frontier = next_frontier

            owners_by_task: dict[str, list[str]] = {}
            all_owners: set[str] = set()
            for tid in unique:
                chain: list[str] = []
                cur = tid
                for _ in range(5):
                    if not cur or cur in chain:
                        break
                    chain.append(cur)
                    cur = inherited_map.get(cur, "")
                owners_by_task[tid] = chain
                all_owners.update(chain)

            if not all_owners:
                return {tid: "" for tid in unique}

            # 2. 批量查询所有归属任务对应的最新 accepted 结果名称
            project_filter = " AND a.project_id=?" if project_id is not None else ""
            latest_by_owner: dict[str, tuple[str, str, str]] = {}
            owner_list = list(all_owners)
            for offset in range(0, len(owner_list), 500):
                chunk = owner_list[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                params = (*chunk, project_id) if project_id is not None else tuple(chunk)
                rows = db.execute(
                    "SELECT a.task_id, r.received_at, r.assignment_id, n.store_name AS observed_store_name"
                    " FROM shared_results r"
                    " JOIN shared_assignments a ON a.id=r.assignment_id"
                    " LEFT JOIN shared_result_names n ON n.assignment_id=r.assignment_id"
                    f" WHERE a.task_id IN ({placeholders}) AND r.disposition='accepted'"
                    + project_filter + " ORDER BY r.received_at DESC, r.assignment_id DESC",
                    params,
                ).fetchall()
                for row in rows:
                    owner_id = str(row["task_id"])
                    if owner_id not in latest_by_owner:
                        latest_by_owner[owner_id] = (
                            str(row["received_at"] or ""),
                            str(row["assignment_id"] or ""),
                            str(row["observed_store_name"] or ""),
                        )

        result: dict[str, str] = {}
        for tid, chain in owners_by_task.items():
            best: tuple[str, str, str] | None = None
            for owner_id in chain:
                cand = latest_by_owner.get(owner_id)
                if cand is not None and (best is None or (cand[0], cand[1]) > (best[0], best[1])):
                    best = cand
            result[tid] = best[2] if best is not None else ""
        return result

    def reclaim_many(self, assignment_ids: list[str], *, project_id: str | None = None) -> dict:
        if (not isinstance(assignment_ids, list) or not 1 <= len(assignment_ids) <= 300
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value) for value in assignment_ids)):
            raise ValueError("请选择 1 至 300 条待回收分配")
        ids = list(dict.fromkeys(assignment_ids))
        # 在写事务前解析可重新分配门店，避免提交回收后因读取素材资格失败而让调用方误以为整批失败。
        effective = self.materials.effective_capture_tasks()
        results = []
        reclaimed_rows = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ids)
            rows = {row["id"]: row for row in db.execute(
                "SELECT a.id,a.task_id,a.batch_id,a.store,a.status,COALESCE(r.disposition,'') AS disposition "
                "FROM shared_assignments a LEFT JOIN shared_results r ON r.assignment_id=a.id "
                f"WHERE a.id IN ({placeholders})", ids)}
            if project_id is not None:
                if len(rows) != len(ids):
                    raise SharedConflict("分配记录不存在或不在当前项目，本次操作未生效")
                self._assert_project_batches(db, project_id, [row["batch_id"] for row in rows.values()])
            for assignment_id in ids:
                row = rows.get(assignment_id)
                if row is None:
                    results.append({"id": assignment_id, "status": "failed", "message": "分配记录不存在"})
                elif row["status"] not in ACTIVE:
                    message = "已完成并确认，不可回收" if row["status"] == "completed" else "分配状态已变化，不可回收"
                    results.append({"id": assignment_id, "task_id": row["task_id"], "store": row["store"],
                                    "status": "failed", "message": message})
                elif row["disposition"] == "history":
                    results.append({"id": assignment_id, "task_id": row["task_id"], "store": row["store"],
                                    "status": "failed", "message": "已有结果已转为历史，不可回收"})
                else:
                    reclaimed_rows.append(row)
                    results.append({"id": assignment_id, "task_id": row["task_id"], "store": row["store"],
                                    "status": "reclaimed", "message": "已回收"})
            reclaim_ids = [row["id"] for row in reclaimed_rows]
            if reclaim_ids:
                reclaim_placeholders = ",".join("?" for _ in reclaim_ids)
                db.execute(
                    f"UPDATE shared_assignments SET status='reclaimed' WHERE status IN ('assigned','ready') AND id IN ({reclaim_placeholders})",
                    reclaim_ids)
        stores = {(row["batch_id"], row["store"]) for row in reclaimed_rows}
        tasks = [{key: task.get(key) for key in ("id", "source_name", "batch_name", "version")}
                 for task in effective
                 if (task["batch_id"], task["source_name"]) in stores and task["runnable"]
                 and not waiting_for_materials(task)]
        return {"count": len(reclaimed_rows), "tasks": tasks, "results": results}

    def authenticate(self, token: str) -> str:
        if not token or len(token) > 100:
            raise WorkerUnauthorized("采集端需要配对")
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM shared_workers WHERE token_hash=?",
                             (_token_hash(token),)).fetchone()
        if not row:
            raise WorkerUnauthorized("采集端凭证无效")
        return row["id"]

    def _source(self, task: dict) -> Path:
        source = Path(task["source_path"]).resolve()
        published = self.materials.published_root.resolve()
        if (source.parent.parent.parent != published
                or source.suffix.lower() not in IMAGE_SUFFIXES
                or not source.is_file()
                or source.stat().st_size > MAX_SOURCE_SIZE
                or sha256_file(source) != task["source_hash"]):
            raise SharedConflict("发布二维码已缺失或变化，请检查素材并重新分配")
        return source

    def assign(self, worker_id: str, task_ids: list[str], *, project_id: str | None = None) -> dict:
        if (not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 300
                or any(not isinstance(value, str) or not value for value in task_ids)):
            raise ValueError("请选择 1 至 300 家已发布门店")
        if not isinstance(project_id, str) or not project_id:
            raise SharedConflict("分配必须指定项目")
        effective = self.materials.effective_capture_tasks()
        tasks = {task["id"]: task for task in effective}
        selected = []
        conflicts = []
        to_verify = []
        for task_id in dict.fromkeys(task_ids):
            task = tasks.get(task_id)
            if task and waiting_for_materials(task):
                conflicts.append({"task_id": task_id, "reason": "materials"})
            elif not task:
                conflicts.append({"task_id": task_id, "reason": "revision"})
            elif not task["runnable"]:
                conflicts.append({"task_id": task_id, "reason": "unavailable"})
            else:
                to_verify.append(task)
        if len(to_verify) > 4:
            from concurrent.futures import ThreadPoolExecutor

            def _check_source(item: dict) -> tuple[dict, str | None]:
                try:
                    return item, self._source(item).suffix.lower()
                except SharedConflict:
                    return item, None

            with ThreadPoolExecutor(max_workers=min(8, len(to_verify))) as pool:
                for item, suffix in pool.map(_check_source, to_verify):
                    if suffix is None:
                        conflicts.append({"task_id": item["id"], "reason": "source"})
                    else:
                        selected.append((item, suffix))
        else:
            for item in to_verify:
                try:
                    selected.append((item, self._source(item).suffix.lower()))
                except SharedConflict:
                    conflicts.append({"task_id": item["id"], "reason": "source"})
        rows = []
        stamp = utc_stamp()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._worker(db, worker_id)["revoked_at"]:
                raise SharedConflict("采集电脑已解除配对，请选择有效设备")
            capabilities = self._worker_capabilities(db, worker_id)
            if project_id != LEGACY_ID and "project_scope" not in capabilities:
                raise SharedConflict("该采集端尚未协商项目隔离能力，暂不能接收新项目分配")
            if project_id is not None:
                self._assert_project_batches(db, project_id, [task["batch_id"] for task, _ in selected])
            project = get_project(db, project_id)
            if project["archived_at"]:
                raise SharedConflict("已归档项目不能新增分配")
            if any(task.get("project_id") != project_id for task, _ in selected):
                raise SharedConflict("所选门店不属于当前项目，本次分配未生效")
            # Reconcile must always see every project's effective tasks.
            self._reconcile_assignments(db, self._store_index(effective))
            # 批量读取活跃占用与同机批次快照，消除 300 家店循环 N+1 查询。
            active_owners: dict[tuple[str, str], str] = {}
            ongoing_snapshots: dict[str, str] = {}
            selected_batches = list(dict.fromkeys(task["batch_id"] for task, _ in selected))
            if selected_batches:
                placeholders = ",".join("?" for _ in selected_batches)
                for row in db.execute(
                    "SELECT a.batch_id,a.store,a.worker_id,a.snapshot_id,w.name "
                    "FROM shared_assignments a JOIN shared_workers w ON w.id=a.worker_id "
                    f"WHERE a.status IN ('assigned','ready') AND a.batch_id IN ({placeholders}) "
                    "ORDER BY a.rowid",
                    selected_batches,
                ):
                    active_owners[(row["batch_id"], row["store"])] = row["name"]
                    if row["worker_id"] == worker_id and row["batch_id"] not in ongoing_snapshots:
                        ongoing_snapshots[row["batch_id"]] = row["snapshot_id"]
            for task, _ in selected:
                owner_name = active_owners.get((task["batch_id"], task["source_name"]))
                if owner_name is not None:
                    conflicts.append({"task_id": task["id"], "reason": "assigned", "worker_name": owner_name})
            if conflicts:
                raise SharedConflict(f"{len(conflicts)} 家门店暂时无法分配，本次提交未生效", conflicts=conflicts)
            for task, suffix in selected:
                # 旧采集端只能为同批次缓存一个兼容快照标记。分配标记与物理二维码路径独立，
                # 存续分配使用同一标记，新旧素材有效性仍由当前门店任务身份校验。
                snap_id = ongoing_snapshots.get(task["batch_id"]) or task["batch_snapshot_id"]
                ongoing_snapshots.setdefault(task["batch_id"], snap_id)
                rows.append({
                    "id": secrets.token_hex(16), "task_id": task["id"], "worker_id": worker_id,
                    "batch_id": task["batch_id"], "batch_name": task["batch_name"], "store": task["source_name"],
                    "store_id": task.get("store_id") or task["id"],
                    "revision": task["version"], "snapshot_id": snap_id,
                    "source_hash": task["source_hash"], "source_suffix": suffix, "status": "assigned",
                    "created_at": stamp, "project_id": project["id"], "project_code": project["code"],
                    "project_name": project["name"],
                })
            if rows:
                db.executemany(
                    "INSERT INTO shared_assignments(id,task_id,worker_id,batch_id,batch_name,store,store_id,"
                    "revision,snapshot_id,source_hash,source_suffix,status,created_at,project_id,project_code,project_name) "
                    "VALUES(:id,:task_id,:worker_id,:batch_id,:batch_name,"
                    ":store,:store_id,:revision,:snapshot_id,:source_hash,:source_suffix,:status,:created_at,"
                    ":project_id,:project_code,:project_name)",
                    rows,
                )
            db.executemany(
                "UPDATE material_batches SET capture_rounds=capture_rounds+1,manual_stage='',"
                "approved_at='',updated_at=? WHERE id=? AND deleted_at=''",
                ((stamp, batch_id) for batch_id in dict.fromkeys(row["batch_id"] for row in rows)),
            )
        return {"assignments": rows}

    @staticmethod
    def _assert_project_batches(db, project_id: str, batch_ids: list[str]) -> None:
        try:
            assert_batch_set(db, project_id, batch_ids, write=True)
        except (ProjectConflict, ProjectNotFound) as exc:
            raise SharedConflict(str(exc)) from exc

    def reclaim(self, assignment_id: str, *, project_id: str | None = None) -> dict:
        with self.store.connect() as db:
            if project_id is not None:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT batch_id FROM shared_assignments WHERE id=?", (assignment_id,)).fetchone()
                if row is None:
                    raise SharedConflict("分配记录不存在或不在当前项目")
                self._assert_project_batches(db, project_id, [row["batch_id"]])
            changed = db.execute(
                "UPDATE shared_assignments SET status='reclaimed' WHERE id=? AND status IN ('assigned','ready')",
                (assignment_id,),
            )
            if not changed.rowcount:
                raise SharedConflict("这条分配已同步或已收回")
        return {"id": assignment_id, "status": "reclaimed"}

    def require_unassigned(self, batch_id: str, names: set[str] | None = None) -> None:
        with self.store.connect() as db:
            assigned = db.execute(
                "SELECT store FROM shared_assignments WHERE batch_id=? AND status IN ('assigned','ready')",
                (batch_id,),
            ).fetchall()
        conflicts = [row["store"] for row in assigned if names is None or row["store"] in names]
        if conflicts:
            raise SharedConflict(f"{'、'.join(conflicts[:4])} 仍由采集端占用，请等待同步或先收回")

    def protect_task(self, task_id: str) -> None:
        task = self.store.get(task_id)
        snapshot = Path(task["source_path"]).parent.parent
        binding = self.store.material_binding(str(snapshot))
        if binding:
            self.require_unassigned(binding["batch_id"], {task["source_name"]})

    def active_task_ids(self, task_ids: list[str]) -> set[str]:
        """仍被采集电脑占用（含待上传）的任务，用于阻止打回导致结果丢失。"""
        if not task_ids:
            return set()
        marks = ",".join("?" for _ in task_ids)
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT task_id FROM shared_assignments WHERE task_id IN ({marks}) "
                "AND status NOT IN ('completed','reclaimed')",
                task_ids,
            )
        return {row["task_id"] for row in rows}

    @staticmethod
    def _store_index(tasks: list[dict]) -> dict:
        return {(task["batch_id"], task["source_name"]): task for task in tasks}

    @staticmethod
    def _valid_current(assigned, task: dict | None) -> bool:
        if task is None or waiting_for_materials(task):
            return False
        if task["material_version"] is not None:
            return assigned["task_id"] == task["id"]
        return assigned["source_hash"] == task["source_hash"]

    def _reconcile_assignments(self, db, current: dict, worker_id: str = "") -> None:
        active = db.execute("""
            SELECT * FROM shared_assignments WHERE status IN ('assigned','ready') AND (?='' OR worker_id=?)
        """, (worker_id, worker_id)).fetchall()
        expired = []
        for row in active:
            task = current.get((row["batch_id"], row["store"]))
            if not self._valid_current(row, task) or store_state(task) in {"captured", "rejected", "returned"}:
                expired.append((row["id"],))
        db.executemany("UPDATE shared_assignments SET status='reclaimed' WHERE id=?", expired)

    def reconcile_assignments(self) -> None:
        current = self._store_index(self.materials.effective_capture_tasks())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile_assignments(db, current)

    @staticmethod
    def _software_row(software: dict | None) -> dict | None:
        """校验采集端上报的软件状态；字段只允许白名单，避免夹带路径或业务数据。"""
        if software is None:
            return None
        fields = ("current_version", "platform", "latest_version", "channel", "checked_at",
                  "download_status", "install_status")
        if not isinstance(software, dict) or set(software) != set(fields) | {"update_available"}:
            raise ValueError("采集端软件状态无效")
        if type(software["update_available"]) is not bool:
            raise ValueError("采集端软件状态无效")
        row = {}
        for key in fields:
            value = software[key]
            if (not isinstance(value, str) or len(value) > 64
                    or any(ord(char) < 32 for char in value)):
                raise ValueError("采集端软件状态无效")
            row[key] = value
        row["update_available"] = 1 if software["update_available"] else 0
        return row

    @staticmethod
    def _worker_capabilities(db, worker_id: str) -> set[str]:
        row = db.execute("SELECT capabilities FROM shared_worker_capabilities WHERE worker_id=?",
                         (worker_id,)).fetchone()
        try:
            values = json.loads(row["capabilities"]) if row else []
        except (TypeError, ValueError):
            values = []
        return {item for item in values if isinstance(item, str)}

    def sync(self, worker_id: str, ready_ids: list[str], pending_uploads: int,
             run_summary: dict | None = None, software: dict | None = None,
             capabilities: set[str] | None = None) -> dict:
        known_capabilities = {"project_scope", "run_history", "run_summary", "software_status"}
        capabilities = set(capabilities or ())
        if capabilities - known_capabilities:
            raise ValueError("采集端能力声明无效")
        project_capability = "project_scope" in capabilities
        if (not isinstance(ready_ids, list) or len(ready_ids) > 1000
                or any(not isinstance(value, str) or len(value) != 32 for value in ready_ids)
                or type(pending_uploads) is not int or not 0 <= pending_uploads <= 10000):
            raise ValueError("采集端同步参数无效")
        if run_summary is not None:
            required_summary = {"status", "mode", "processed", "total"}
            allowed_summary = required_summary | ({"project_id"} if project_capability else set())
            if (not isinstance(run_summary, dict) or not required_summary <= set(run_summary) <= allowed_summary
                    or not isinstance(run_summary["status"], str) or run_summary["status"] not in REPORTED_RUN_STATUSES
                    or run_summary["mode"] not in ("auto", "manual", "")
                    or any(type(run_summary[key]) is not int for key in ("processed", "total"))
                    or ("project_id" in run_summary and run_summary["project_id"] is not None
                        and (not isinstance(run_summary["project_id"], str) or len(run_summary["project_id"]) > 128))
                    or not 0 <= run_summary["processed"] <= run_summary["total"] <= 10000):
                raise ValueError("采集端轮次摘要无效")
        software_row = self._software_row(software)
        stamp = utc_stamp()
        current = self._store_index(self.materials.effective_capture_tasks())
        batch_snapshots = self.materials.latest_snapshot_map()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            worker = self._worker(db, worker_id)
            if worker["revoked_at"]:
                raise WorkerUnauthorized("采集电脑已解除配对")
            db.execute("""INSERT INTO shared_worker_capabilities(worker_id,capabilities,updated_at)
                VALUES(?,?,?) ON CONFLICT(worker_id) DO UPDATE SET
                capabilities=excluded.capabilities,updated_at=excluded.updated_at""",
                       (worker_id, json.dumps(sorted(capabilities)), stamp))
            if run_summary is not None and run_summary.get("project_id"):
                if not project_capability or not db.execute(
                    "SELECT 1 FROM shared_assignments WHERE worker_id=? AND project_id=? LIMIT 1",
                    (worker_id, run_summary["project_id"]),
                ).fetchone():
                    raise SharedConflict("轮次项目与该采集端已保存的分配快照不一致")
            rollout_row = db.execute(
                "SELECT mode,worker_ids FROM shared_update_rollout WHERE id=1").fetchone()
            db.execute("UPDATE shared_workers SET last_seen=?,pending_uploads=? WHERE id=?",
                       (stamp, pending_uploads, worker_id))
            if run_summary is not None:
                db.execute("""
                    INSERT INTO shared_worker_runs(worker_id,status,mode,processed,total,reported_at,project_id)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(worker_id) DO UPDATE SET status=excluded.status,mode=excluded.mode,
                        processed=excluded.processed,total=excluded.total,reported_at=excluded.reported_at,
                        project_id=excluded.project_id
                """, (worker_id, run_summary["status"], run_summary["mode"], run_summary["processed"],
                      run_summary["total"], stamp, str(run_summary.get("project_id") or "")))
            if software_row is not None:
                db.execute("""
                    INSERT INTO shared_worker_software(worker_id,current_version,platform,latest_version,
                        update_available,channel,checked_at,download_status,install_status,reported_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(worker_id) DO UPDATE SET current_version=excluded.current_version,
                        platform=excluded.platform,latest_version=excluded.latest_version,
                        update_available=excluded.update_available,channel=excluded.channel,
                        checked_at=excluded.checked_at,download_status=excluded.download_status,
                        install_status=excluded.install_status,reported_at=excluded.reported_at
                """, (worker_id, software_row["current_version"], software_row["platform"],
                      software_row["latest_version"], software_row["update_available"],
                      software_row["channel"], software_row["checked_at"],
                      software_row["download_status"], software_row["install_status"], stamp))
            self._reconcile_assignments(db, current, worker_id)
            db.executemany("UPDATE shared_assignments SET status='ready' WHERE id=? AND worker_id=? AND status='assigned'",
                           ((assignment_id, worker_id) for assignment_id in ready_ids))
            assignments = [dict(row) for row in db.execute("""
                SELECT * FROM shared_assignments WHERE worker_id=? AND status IN ('assigned','ready','completed')
                ORDER BY rowid
            """, (worker_id,))]
            project_ids = list(dict.fromkeys(row["project_id"] for row in assignments if row["project_id"]))
            project_catalogue = []
            if project_ids:
                marks = ",".join("?" for _ in project_ids)
                project_catalogue = [dict(row) for row in db.execute(
                    f"SELECT id,code,name,archived_at FROM projects WHERE id IN ({marks}) ORDER BY code,id",
                    project_ids)]
        rows = [row for row in assignments if row["status"] in ACTIVE]
        # 旧端只收到属于自己的批次。新协议额外带项目快照，避免 Worker 使用全局 batch 映射。
        supported_project_scope = project_capability and all(
            row["project_id"] and row["project_code"] and row["project_name"] for row in rows)
        if supported_project_scope:
            batch_snapshots = {row["batch_id"]: row["snapshot_id"] for row in rows}
            project_by_id = {project["id"]: project for project in project_catalogue}
            if any(row["project_id"] not in project_by_id for row in rows):
                supported_project_scope = False
        if not supported_project_scope:
            rows = [row for row in rows if row["status"] in ACTIVE]
            batch_ids = list(dict.fromkeys(row["batch_id"] for row in assignments))
            batch_snapshots = {batch_id: snapshot_id for batch_id, snapshot_id in
                               self.materials.latest_snapshot_map().items() if batch_id in batch_ids}
        response_assignments = assignments if not supported_project_scope else [
            row | {"store_id": row["store_id"] or row["task_id"]}
            for row in assignments if row["project_id"]]
        if not supported_project_scope:
            response_assignments = [{key: value for key, value in row.items()
                                     if key not in {"project_id", "project_code", "project_name"}}
                                    for row in response_assignments]
        projects_for_worker = project_catalogue if supported_project_scope else []
        states = {}
        for row in response_assignments:
            task = current.get((row["batch_id"], row["store"]))
            if self._valid_current(row, task):
                states[row["task_id"]] = {"state": store_state(task), "reject_reason": task["reject_reason"] or ""}
        rollout_mode = str(rollout_row["mode"]) if rollout_row else "all"
        try:
            rollout_ids = json.loads(rollout_row["worker_ids"]) if rollout_row else []
        except (TypeError, ValueError):
            rollout_ids = []
        # 分批模式下只有本批设备收到目标版本；其余设备保持本机检查，不被动升级。
        in_rollout = rollout_mode != "batch" or worker_id in rollout_ids
        batches = [{"batch_id": batch_id, "snapshot_id": snapshot_id}
                   for batch_id, snapshot_id in batch_snapshots.items()]
        if supported_project_scope:
            owner_by_batch = {row["batch_id"]: row["project_id"] for row in rows}
            batches = [{**batch, "project_id": owner_by_batch[batch["batch_id"]]} for batch in batches
                       if batch["batch_id"] in owner_by_batch]
        response = {"assignments": rows if supported_project_scope else response_assignments,
                "batches": batches,
                "states": states, "supports_run_summary": True, "supports_run_history": True,
                "supports_software_status": True,
                # Hub 已校验且未过期时分发完整元数据；Worker 会再次校验后才采纳。
                "update_target": self._update_target() if in_rollout else None,
                "update_rollout": {"mode": rollout_mode, "in_rollout": in_rollout},
                "worker_name": worker["name"]}
        if supported_project_scope:
            response["supports_project_scope"] = True
            response["projects"] = projects_for_worker
            response["assignments"] = response_assignments
        return response

    def record_run_history(self, worker_id: str, report: HistoryReport,
                           *, capabilities: set[str] | None = None) -> dict:
        capabilities = set(capabilities or ())
        run_data = report.run.model_dump()
        result_data = [row.model_dump() for row in report.results]
        has_project_payload = bool(run_data.get("project_id") or any(row.get("project_id") for row in result_data))
        if has_project_payload and "project_scope" not in capabilities:
            raise SharedConflict("采集端尚未协商项目隔离能力，不能提交项目字段")
        run_data["project_id"] = run_data.get("project_id") or ""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._worker(db, worker_id)["revoked_at"]:
                raise WorkerUnauthorized("采集电脑已解除配对")
            # 每家门店的项目身份来自 Hub 保存的 worker/task assignment 快照；未知归属不推测。
            task_ids = list(report.run.task_ids)
            placeholders = ",".join("?" for _ in task_ids)
            assignment_rows = db.execute(
                "SELECT task_id,project_id FROM shared_assignments WHERE worker_id=? "
                f"AND task_id IN ({placeholders}) AND project_id<>'' ORDER BY rowid",
                (worker_id, *task_ids),
            ).fetchall() if task_ids else []
            project_by_task: dict[str, str] = {}
            conflicts: set[str] = set()
            for row in assignment_rows:
                previous = project_by_task.get(row["task_id"])
                if previous and previous != row["project_id"]:
                    conflicts.add(row["task_id"])
                project_by_task[row["task_id"]] = row["project_id"]
            if conflicts:
                raise SharedConflict("轮次门店对应多个项目快照，项目归属待核验")
            reported_by_task = {row["task_id"]: row.get("project_id") or "" for row in result_data}
            for task_id, reported in reported_by_task.items():
                expected = project_by_task.get(task_id, "")
                if reported and reported != expected:
                    raise SharedConflict("逐店历史项目与 Hub 保存的分配快照不一致")
            expected_run_projects = {project_by_task[task_id] for task_id in task_ids if task_id in project_by_task}
            all_known = len(project_by_task) == len(task_ids)
            expected_run_project = next(iter(expected_run_projects)) if all_known and len(expected_run_projects) == 1 else ""
            reported_run_project = run_data.get("project_id") or ""
            if reported_run_project and reported_run_project != expected_run_project:
                raise SharedConflict("轮次项目与 Hub 保存的逐店分配快照不一致")
            run_data["project_id"] = expected_run_project
            for row in result_data:
                row["project_id"] = project_by_task.get(row["task_id"], "")
            record = capture_history_row(run_data, result_data, {})
            tasks = record.pop("tasks")
            for task in tasks:
                task["project_id"] = project_by_task.get(task["task_id"], "")
                source = next((row for row in result_data if row["task_id"] == task["task_id"]), {})
                task["batch_id"] = source.get("batch_id", "")
            record["project_id"] = expected_run_project
            record["history_status"] = "partial" if record["counts"]["unknown"] else "synced"
            db.execute("""
                INSERT INTO shared_run_history(worker_id,run_id,generation,status,summary,tasks,synced_at,project_id)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(worker_id,run_id) DO UPDATE SET generation=excluded.generation,
                    status=excluded.status,summary=excluded.summary,tasks=excluded.tasks,synced_at=excluded.synced_at,
                    project_id=excluded.project_id
                WHERE excluded.generation > shared_run_history.generation
            """, (worker_id, report.run.id, report.generation, report.run.status,
                  json.dumps(record, ensure_ascii=False), json.dumps(tasks, ensure_ascii=False), utc_stamp(),
                  expected_run_project))
        return {"acknowledged": report.generation}

    @staticmethod
    def _synced_history_row(row, *, project_id: str | None = None) -> dict:
        summary = json.loads(row["summary"])
        if project_id is not None:
            tasks = json.loads(row["tasks"] or "[]") if "tasks" in row.keys() else []
            tasks = [task for task in tasks if task.get("project_id") == project_id]
            outcomes = [task.get("status", "") for task in tasks]
            recorded = sum(outcome in {"captured", "review", "rejected"} for outcome in outcomes)
            batches = {}
            for task in tasks:
                batch_id = task.get("batch_id") or ""
                if batch_id:
                    batch = batches.setdefault(batch_id, {"id": batch_id,
                        "name": task.get("batch_name") or batch_id, "versions": [], "stores": 0})
                    batch["stores"] += 1
                    version = task.get("version") or ""
                    if version and version not in batch["versions"]:
                        batch["versions"].append(version)
            summary["project_id"] = project_id
            summary["batches"] = list(batches.values())
            summary["batch_id"] = next(iter(batches)) if len(batches) == 1 else ""
            summary["batch_name"] = next(iter(batches.values()))["name"] if len(batches) == 1 else ""
            summary["counts"] = {"total": len(tasks), "processed": recorded,
                "done": outcomes.count("captured"), "attention": outcomes.count("review"),
                "rejected": outcomes.count("rejected"), "recorded": recorded,
                "unknown": len(tasks) - recorded, "failed": 0}
        return {
            **summary, "id": f'{row["worker_id"]}:{row["run_id"]}',
            "local_run_id": row["run_id"], "worker_id": row["worker_id"], "worker_name": row["worker_name"],
            "worker_last_seen": row["worker_last_seen"], "synced_at": row["synced_at"], "can_rename": False,
        }

    def history_records(self, worker_id: str = "", *, shortcut_since: str = "",
                        project_id: str | None = None) -> list[dict]:
        query = """
            SELECT h.worker_id,h.run_id,h.synced_at,h.summary,h.tasks,w.name AS worker_name,w.last_seen AS worker_last_seen
            FROM shared_run_history h JOIN shared_workers w ON h.worker_id=w.id
            WHERE (?='' OR h.worker_id=?)
        """
        args = [worker_id, worker_id]
        if project_id is not None:
            query += " AND (h.project_id=? OR EXISTS (SELECT 1 FROM json_each(h.tasks) t WHERE json_extract(t.value,'$.project_id')=?))"
            args.extend((project_id, project_id))
        if shortcut_since:
            placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
            query += f" AND (h.status IN ({placeholders}) OR h.synced_at >= ?)"
            args.extend([*sorted(ACTIVE_RUN_STATUSES), shortcut_since])
        with self.store.connect() as db:
            return [self._synced_history_row(row, project_id=project_id) for row in db.execute(query, args)]

    def history_detail(self, worker_id: str, run_id: str, *, project_id: str | None = None) -> dict | None:
        with self.store.connect() as db:
            row = db.execute("""
                SELECT h.*,w.name AS worker_name,w.last_seen AS worker_last_seen
                FROM shared_run_history h JOIN shared_workers w ON h.worker_id=w.id
                WHERE h.worker_id=? AND h.run_id=?
            """, (worker_id, run_id)).fetchone()
        if row is None:
            return None
        tasks = json.loads(row["tasks"])
        if project_id is not None:
            tasks = [task for task in tasks if task.get("project_id") == project_id]
            if not tasks:
                return None
        return self._synced_history_row(row, project_id=project_id) | {"tasks": tasks}

    def assignment(self, worker_id: str, assignment_id: str) -> dict:
        with self.store.connect() as db:
            if self._worker(db, worker_id)["revoked_at"]:
                raise WorkerUnauthorized("采集电脑已解除配对")
            row = db.execute("SELECT * FROM shared_assignments WHERE worker_id=? AND id=?",
                             (worker_id, assignment_id)).fetchone()
        if not row:
            raise WorkerUnauthorized("分配不属于此采集端")
        return dict(row)

    def source(self, worker_id: str, assignment_id: str) -> Path:
        assigned = self.assignment(worker_id, assignment_id)
        if assigned["status"] not in ACTIVE or self._matching_current(assigned) is None:
            raise SharedConflict("门店分配已结束或素材已变更")
        task = self.store.get(assigned["task_id"])
        if task["source_hash"] != assigned["source_hash"]:
            raise SharedConflict("二维码版本已变更")
        return self._source(task)

    def _unpack(self, archive: Path, staging: Path) -> dict:
        with ZipFile(archive) as zipped:
            items = zipped.infolist()
            if len(items) > 1000 or sum(info.file_size for info in items) > MAX_RESULT_SIZE:
                raise ValueError("采集结果超过上传大小限制")
            seen = set()
            manifest = None
            images = 0
            for info in items:
                # ZipInfo.filename 在 Windows 会先转换分隔符，用原始值做同一套校验。
                path = safe_artifact(info.orig_filename)
                key = unicodedata.normalize("NFC", str(path)).casefold()
                if (key in seen or info.is_dir() or info.file_size > MAX_ARTIFACT_SIZE
                        or (info.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError("采集结果包含重复、链接或过大的文件")
                seen.add(key)
                if path == PurePosixPath("result.json"):
                    if info.file_size > 64 * 1024:
                        raise ValueError("结果说明过大")
                    manifest = json.loads(zipped.read(info))
                    continue
                if ((path.parts[0] != "artifacts" or len(path.parts) < 2
                     or path.suffix.lower() not in {".png", ".json"})
                        and path != PurePosixPath("evidence.png")):
                    raise ValueError("采集结果含不支持的附件")
                destination = staging.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(info) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
                if path.suffix.lower() == ".png":
                    with Image.open(destination, formats=["PNG"]) as image:
                        image.verify()
                    images += int(path.parts[0] == "artifacts")
            if not isinstance(manifest, dict) or manifest.get("status") not in {"captured", "needs_review"}:
                raise ValueError("采集结果缺少有效状态")
            reason = manifest.get("reject_reason", "")
            if not isinstance(reason, str) or (reason and reason not in REJECTION_REASONS):
                raise ValueError("不合格原因无效")
            if manifest["status"] == "captured" and (not images or reason):
                raise ValueError("采集成功必须附带截图，且不能带有不合格原因")
            return manifest

    def receive(self, worker_id: str, assignment_id: str, archive: Path) -> dict:
        assigned = self.assignment(worker_id, assignment_id)
        digest = sha256_file(archive)
        with self.store.connect() as db:
            previous = db.execute("SELECT * FROM shared_results WHERE assignment_id=?", (assignment_id,)).fetchone()
        if previous:
            if previous["digest"] != digest:
                raise SharedConflict("同一次采集已提交不同内容，请检查采集端")
            return {"disposition": previous["disposition"], "status": previous["status"]}
        destination = self.root / assignment_id
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=self.root) as folder:
            staging = Path(folder) / "result"
            staging.mkdir()
            try:
                manifest = self._unpack(archive, staging)
            except (BadZipFile, RuntimeError, OSError, RecursionError) as exc:
                raise ValueError("采集结果压缩包无法读取或图片校验失败") from exc
            if any(manifest.get(key) != assigned[key] for key in ("task_id", "snapshot_id", "source_hash")):
                raise SharedConflict("结果二维码、任务或发布版本与分配不一致")
            (staging / "result.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            (staging / ".digest").write_text(digest, encoding="ascii")
            current = self._matching_current(assigned)
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if destination.exists():
                    # 目录更名后服务中断可直接补做事务；同一分配不会覆盖旧文件。
                    if (destination / ".digest").read_text(encoding="ascii") != digest:
                        raise SharedConflict("上一次提交尚待确认，结果内容不能更换")
                else:
                    staging.rename(destination)
                live = db.execute("SELECT status FROM shared_assignments WHERE id=?", (assignment_id,)).fetchone()
                state = manifest["status"]
                message = str(manifest.get("message", ""))[:400]
                accepted = current is not None and live["status"] in ACTIVE
                if accepted:
                    self._apply(db, assigned, current, manifest, destination, message)
                    db.execute("UPDATE shared_assignments SET status='completed' WHERE id=?", (assignment_id,))
                elif live["status"] in ACTIVE:
                    db.execute("UPDATE shared_assignments SET status='reclaimed' WHERE id=?", (assignment_id,))
                disposition = "accepted" if accepted else "history"
                observed_name = _observed_store_name(manifest)
                if self._legacy_observed_store_column:
                    # v0.3.2 已发布的旧实现会读取这一列，因此在已迁移数据库上继续双写。
                    db.execute("INSERT INTO shared_results(assignment_id,digest,disposition,status,message,"
                               "received_at,observed_store_name) VALUES(?,?,?,?,?,?,?)",
                               (assignment_id, digest, disposition, state, message, utc_stamp(), observed_name))
                else:
                    db.execute("INSERT INTO shared_results(assignment_id,digest,disposition,status,message,"
                               "received_at) VALUES(?,?,?,?,?,?)",
                               (assignment_id, digest, disposition, state, message, utc_stamp()))
                db.execute("INSERT INTO shared_result_names(assignment_id,store_name) VALUES(?,?) "
                           "ON CONFLICT(assignment_id) DO UPDATE SET store_name=excluded.store_name",
                           (assignment_id, observed_name))
        return {"disposition": disposition, "status": state}

    def _matching_current(self, assigned: dict) -> dict | None:
        return next((task for task in self.materials.effective_capture_tasks()
                     if task["batch_id"] == assigned["batch_id"] and task["source_name"] == assigned["store"]
                     and self._valid_current(assigned, task)), None)

    @staticmethod
    def _apply(db, assigned: dict, current: dict, manifest: dict, destination: Path, message: str) -> None:
        stamp = utc_stamp()
        state = manifest["status"]
        reason = manifest.get("reject_reason", "")
        evidence = str(destination / "evidence.png") if (destination / "evidence.png").is_file() else ""
        artifacts = destination / "artifacts"
        output = str(artifacts) if artifacts.is_dir() else ""
        details = str(manifest.get("reject_detail", ""))[:200] if reason else ""
        stage = "captured" if state == "captured" else str(manifest.get("stage", "shared_review"))[:80]
        code = "" if state == "captured" else str(manifest.get("error_code", ""))[:80]
        # 无关门店换版后，同哈希的新快照复用本次结果，并记录真正完成采集的原任务。
        for task_id in dict.fromkeys((assigned["task_id"], current["id"])):
            inherited = assigned["task_id"] if task_id != assigned["task_id"] else ""
            db.execute("""
                UPDATE tasks SET status=?,stage=?,message=?,output_dir=?,reject_reason=?,reject_detail=?,
                    rejected_at=?,reject_evidence=?,reject_reviewed_at='',error_code=?,updated_at=?,inherited_from=?
                WHERE id=?
            """, (state, stage, message, output, reason, details, stamp if reason else "", evidence,
                  code, stamp, inherited, task_id))
        db.execute("INSERT INTO events(time,level,message,task_id) VALUES(?,?,?,?)",
                   (stamp, "info", "已同步采集端结果", current["id"]))
