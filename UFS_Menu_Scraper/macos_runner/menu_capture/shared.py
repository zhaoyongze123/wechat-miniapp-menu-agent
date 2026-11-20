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

from .materials import REJECTION_REASONS, MaterialService
from .store import TaskStore

MAX_RESULT_SIZE = 1024**3
MAX_ARTIFACT_SIZE = 32 * 1024**2
MAX_SOURCE_SIZE = 32 * 1024**2
ACTIVE = ("assigned", "ready")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class SharedConflict(ValueError):
    """并发、版本或分配状态已变化，操作可以在刷新后重试。"""


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
                CREATE TABLE IF NOT EXISTS shared_pairing (
                    code_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_assignments (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL, batch_name TEXT NOT NULL, store TEXT NOT NULL,
                    revision TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL, source_suffix TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS shared_current_store
                    ON shared_assignments(batch_id, store) WHERE status IN ('assigned','ready');
                CREATE INDEX IF NOT EXISTS shared_worker_assignments ON shared_assignments(worker_id,status);
                CREATE TABLE IF NOT EXISTS shared_results (
                    assignment_id TEXT PRIMARY KEY, digest TEXT NOT NULL, disposition TEXT NOT NULL,
                    status TEXT NOT NULL, message TEXT NOT NULL, received_at TEXT NOT NULL
                );
            """)

    def status(self, *, include_history: bool = True) -> dict:
        with self.store.connect() as db:
            workers = [dict(row) for row in db.execute(
                "SELECT id,name,last_seen,pending_uploads FROM shared_workers ORDER BY name,id")]
            assignments = [dict(row) for row in db.execute("""
                SELECT a.*, COALESCE(r.disposition, '') AS disposition, COALESCE(r.message, '') AS result_message
                FROM shared_assignments a LEFT JOIN shared_results r ON a.id=r.assignment_id
                WHERE a.status IN ('assigned','ready') ORDER BY a.created_at DESC,a.id
            """)]
            if include_history:
                assignments.extend(dict(row) for row in db.execute("""
                    SELECT a.*, COALESCE(r.disposition, '') AS disposition, COALESCE(r.message, '') AS result_message
                    FROM shared_assignments a LEFT JOIN shared_results r ON a.id=r.assignment_id
                    WHERE a.status IN ('completed','reclaimed') ORDER BY a.created_at DESC,a.id LIMIT 200
                """))
        if any(row["status"] in ACTIVE for row in assignments):
            current = {(task["batch_id"], task["source_name"]): task["id"]
                       for task in self.materials.effective_capture_tasks()}
            for row in assignments:
                if row["status"] in ACTIVE:
                    row["current_task_id"] = current.get((row["batch_id"], row["store"]), row["task_id"])
        return {"role": "hub", "workers": workers, "assignments": assignments}

    def pairing_code(self) -> dict:
        code = secrets.token_urlsafe(9)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds")
        with self.store.connect() as db:
            db.execute("DELETE FROM shared_pairing WHERE expires_at<=?", (utc_stamp(),))
            db.execute("INSERT INTO shared_pairing VALUES(?,?)", (_token_hash(code), expires))
        return {"code": code, "expires_at": expires}

    def pair(self, code: str, name: str) -> dict:
        name = str(name or "").strip()
        if not name or len(name) > 64 or any(ord(char) < 32 for char in name):
            raise ValueError("电脑名称应为 1 至 64 个字符")
        if not isinstance(code, str) or not re.fullmatch(r"[\w-]{12}", code, flags=re.ASCII):
            raise ValueError("配对码无效或已过期")
        worker_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("DELETE FROM shared_pairing WHERE code_hash=? AND expires_at>?",
                              (_token_hash(code), utc_stamp()))
            if not used.rowcount:
                raise ValueError("配对码无效或已过期")
            db.execute("INSERT INTO shared_workers VALUES(?,?,?,?,0)",
                       (worker_id, name, _token_hash(token), utc_stamp()))
        return {"id": worker_id, "name": name, "token": token}

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

    def assign(self, worker_id: str, task_ids: list[str]) -> dict:
        if (not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 300
                or any(not isinstance(value, str) or not value for value in task_ids)):
            raise ValueError("请选择 1 至 300 家已发布门店")
        tasks = {task["id"]: task for task in self.materials.effective_capture_tasks()}
        selected = []
        for task_id in dict.fromkeys(task_ids):
            task = tasks.get(task_id)
            if not task or not task["runnable"]:
                raise SharedConflict("所选门店已换版或当前状态不可采集，请刷新队列")
            selected.append((task, self._source(task).suffix.lower()))
        rows = []
        stamp = utc_stamp()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM shared_workers WHERE id=?", (worker_id,)).fetchone():
                raise ValueError("采集电脑不存在，请先配对")
            for task, suffix in selected:
                if db.execute("""
                    SELECT 1 FROM shared_assignments WHERE batch_id=? AND store=?
                    AND status IN ('assigned','ready')
                """, (task["batch_id"], task["source_name"])).fetchone():
                    raise SharedConflict(f"{task['source_name']} 已分配，需等待同步或先收回")
                record = {
                    "id": secrets.token_hex(16), "task_id": task["id"], "worker_id": worker_id,
                    "batch_id": task["batch_id"], "batch_name": task["batch_name"], "store": task["source_name"],
                    "revision": task["version"], "snapshot_id": task["snapshot_id"],
                    "source_hash": task["source_hash"], "source_suffix": suffix, "status": "assigned",
                    "created_at": stamp,
                }
                db.execute("INSERT INTO shared_assignments VALUES(:id,:task_id,:worker_id,:batch_id,:batch_name,"
                           ":store,:revision,:snapshot_id,:source_hash,:source_suffix,:status,:created_at)", record)
                rows.append(record)
            db.executemany(
                "UPDATE material_batches SET capture_rounds=capture_rounds+1,manual_stage='',"
                "approved_at='',updated_at=? WHERE id=? AND deleted_at=''",
                ((stamp, batch_id) for batch_id in dict.fromkeys(row["batch_id"] for row in rows)),
            )
        return {"assignments": rows}

    def reclaim(self, assignment_id: str) -> dict:
        with self.store.connect() as db:
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

    def protect_publish(self, batch_id: str, store_ids: list[str] | None) -> None:
        with self.store.connect() as db:
            names = {row["source_name"] for row in db.execute(
                "SELECT store_id,source_name FROM material_decisions WHERE batch_id=? AND decision='confirmed'",
                (batch_id,),
            ) if store_ids is None or row["store_id"] in store_ids}
        self.require_unassigned(batch_id, names)

    def protect_task(self, task_id: str) -> None:
        task = self.store.get(task_id)
        snapshot = Path(task["source_path"]).parent.parent
        binding = self.store.material_binding(str(snapshot))
        if binding:
            self.require_unassigned(binding["batch_id"], {task["source_name"]})

    def sync(self, worker_id: str, ready_ids: list[str], pending_uploads: int) -> dict:
        if (not isinstance(ready_ids, list) or len(ready_ids) > 1000
                or any(not isinstance(value, str) or len(value) != 32 for value in ready_ids)
                or type(pending_uploads) is not int or not 0 <= pending_uploads <= 10000):
            raise ValueError("采集端同步参数无效")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE shared_workers SET last_seen=?,pending_uploads=? WHERE id=?",
                       (utc_stamp(), pending_uploads, worker_id))
            db.executemany("UPDATE shared_assignments SET status='ready' WHERE id=? AND worker_id=? AND status='assigned'",
                           ((assignment_id, worker_id) for assignment_id in ready_ids))
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM shared_assignments WHERE worker_id=? AND status IN ('assigned','ready') "
                "ORDER BY rowid", (worker_id,))]
        return {"assignments": rows}

    def assignment(self, worker_id: str, assignment_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM shared_assignments WHERE worker_id=? AND id=?",
                             (worker_id, assignment_id)).fetchone()
        if not row:
            raise WorkerUnauthorized("分配不属于此采集端")
        return dict(row)

    def source(self, worker_id: str, assignment_id: str) -> Path:
        assigned = self.assignment(worker_id, assignment_id)
        if assigned["status"] not in ACTIVE:
            raise SharedConflict("门店分配已结束")
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
                db.execute("INSERT INTO shared_results VALUES(?,?,?,?,?,?)",
                           (assignment_id, digest, disposition, state, message, utc_stamp()))
        return {"disposition": disposition, "status": state}

    def _matching_current(self, assigned: dict) -> dict | None:
        return next((task for task in self.materials.effective_capture_tasks()
                     if task["batch_id"] == assigned["batch_id"] and task["source_name"] == assigned["store"]
                     and task["source_hash"] == assigned["source_hash"]), None)

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
