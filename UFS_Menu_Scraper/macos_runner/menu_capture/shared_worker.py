"""采集端缓存已分配的二维码，持久化未上传的结果，并自动与共享后台对账。"""

from __future__ import annotations

import hashlib
from ipaddress import ip_address, ip_network
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_STORED, ZipFile

import httpx

from .config import atomic_json
from .shared import IMAGE_SUFFIXES, MAX_RESULT_SIZE, MAX_SOURCE_SIZE, safe_artifact
from .store import TaskStore, now


TAILSCALE_IPV4 = ip_network("100.64.0.0/10")


def local_http_host(host: str) -> bool:
    # Tailscale 内网由 WireGuard 加密，可直接用固定的 IPv4 地址配对。
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    try:
        return ip_address(host) in TAILSCALE_IPV4
    except ValueError:
        return False


def hub_url(value: str) -> str:
    """校验共享后台地址，允许 HTTPS、本机回环和 Tailscale 内网 HTTP。"""
    value = str(value or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
        or (parsed.scheme == "http" and not local_http_host(parsed.hostname))
    ):
        raise ValueError("请输入共享工作台地址，可使用 HTTPS 或 Tailscale 内网 IPv4 地址")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("工作台端口不合法")
    return value


class SharedWorker:
    """本机 SQLite 记录缓存和上传进度，离线期间直接沿用已有 Windows 采集执行器。"""

    def __init__(self, root: Path, store: TaskStore, output_lock, control_lock, *, client=None):
        self.store = store
        self.output_lock = output_lock
        self.control_lock = control_lock
        self.credentials = root / "shared-worker.local.json"
        self.inputs = root / "data/shared-inputs"
        self.outbox = root / "data/shared-outbox"
        self.inputs.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.connection = json.loads(self.credentials.read_text(encoding="utf-8")) if self.credentials.exists() else {}
        self._http = client or httpx.Client(timeout=httpx.Timeout(20, connect=5), trust_env=False)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.online = False
        self.error = ""
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS worker_assignments (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                    batch_name TEXT NOT NULL, store TEXT NOT NULL, revision TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, source_hash TEXT NOT NULL, source_suffix TEXT NOT NULL,
                    status TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS worker_assignment_task ON worker_assignments(task_id,status);
            """)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="shared-worker", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._thread is None or not self._thread.is_alive():
            self._http.close()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self.connection:
                    self.poll()
                self._wake.wait(3)
                self._wake.clear()
        finally:
            self._http.close()

    def connect(self, url: str, code: str, name: str) -> dict:
        url = hub_url(url)
        if self.connection:
            raise ValueError("此采集端已配对")
        try:
            response = self._http.post(url + "/api/shared/worker/pair", json={"code": code, "name": name})
        except httpx.RequestError:
            raise ValueError("无法连接工作台，请检查地址和内网连接") from None
        if response.status_code != 200:
            raise ValueError("工作台拒绝配对，请检查地址和配对码")
        data = response.json()
        if (not isinstance(data, dict) or not isinstance(data.get("token"), str)
                or not isinstance(data.get("id"), str) or not data.get("token")):
            raise ValueError("工作台返回了无效的配对结果")
        connection = {"url": url, "token": data["token"], "name": data.get("name", name), "id": data["id"]}
        atomic_json(self.credentials, connection)
        self.connection = connection
        self.online = True
        self.error = ""
        self._wake.set()
        return self.status()

    def _url(self, suffix: str) -> str:
        return self.connection["url"] + "/api/shared/worker" + suffix

    @property
    def _headers(self) -> dict:
        return {"Authorization": "Bearer " + self.connection["token"]}

    def status(self) -> dict:
        with self.store.connect() as db:
            assignments = [dict(row) for row in db.execute("""
                SELECT id,task_id,batch_id,batch_name,store,revision,status,disposition FROM worker_assignments
                WHERE status NOT IN ('completed','reclaimed') ORDER BY rowid
            """)]
            assignments.extend(dict(row) for row in db.execute("""
                SELECT id,task_id,batch_id,batch_name,store,revision,status,disposition FROM worker_assignments
                WHERE status IN ('completed','reclaimed') ORDER BY created_at DESC,id LIMIT 100
            """))
        return {"role": "worker", "paired": bool(self.connection), "online": self.online,
                "name": self.connection.get("name", ""), "error": self.error,
                "pending_uploads": sum(row["status"] == "pending_upload" for row in assignments),
                "assignments": assignments}

    def tasks(self) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute("""
                SELECT t.*, a.batch_id, a.batch_name, a.revision AS version, a.snapshot_id,
                       a.status AS assignment_status
                FROM tasks t JOIN worker_assignments a ON a.task_id=t.id
                WHERE a.status IN ('ready','pending_upload','completed')
                  AND a.rowid=(SELECT MAX(b.rowid) FROM worker_assignments b WHERE b.task_id=t.id)
                ORDER BY a.rowid
            """).fetchall()
        return [dict(row) | {"runnable": row["assignment_status"] == "ready"} for row in rows]

    def _source_path(self, assignment: dict) -> Path:
        return self.inputs / assignment["id"] / "source" / ("二维码" + assignment["source_suffix"])

    def source_root(self, task_id: str) -> str:
        with self.store.connect() as db:
            row = db.execute("""
                SELECT * FROM worker_assignments WHERE task_id=? AND status='ready' ORDER BY rowid DESC LIMIT 1
            """, (task_id,)).fetchone()
        if not row:
            return ""
        path = self._source_path(row)
        return str(path.parent.parent) if path.is_file() else ""

    def check_start(self, task_ids: list[str]) -> None:
        self.prepare_results()
        if any(not self.source_root(task_id) for task_id in task_ids):
            raise ValueError("请只选择本机已下载、可离线采集的门店")

    def bind_run(self, db, run_id: str, task_ids: list[str]) -> None:
        # 与运行记录同一事务，退出或断电后仍知道哪次运行产出了结果。
        db.executemany("UPDATE worker_assignments SET run_id=? WHERE task_id=? AND status='ready'",
                       ((run_id, task_id) for task_id in task_ids))

    def _queue_result(self, assignment: dict, task: dict) -> None:
        path = self.outbox / (assignment["id"] + ".zip")
        if not path.exists():
            manifest = {key: assignment[key] for key in ("task_id", "snapshot_id", "source_hash")}
            manifest.update({key: task.get(key, "") for key in
                             ("stage", "message", "reject_reason", "reject_detail", "error_code")})
            manifest["status"] = "captured" if task["status"] == "captured" else "needs_review"
            temporary = path.with_suffix(".tmp")
            try:
                with ZipFile(temporary, "w", compression=ZIP_STORED) as archive:
                    archive.writestr("result.json", json.dumps(manifest, ensure_ascii=False))
                    if task.get("output_dir"):
                        folder = Path(task["output_dir"]).resolve()
                        for file in sorted(folder.rglob("*")):
                            if (file.is_file() and not file.is_symlink() and folder in file.resolve().parents
                                    and file.suffix.lower() in {".png", ".json"}):
                                relative = safe_artifact(file.relative_to(folder).as_posix())
                                archive.write(file, "artifacts/" + str(relative))
                    evidence = str(task.get("reject_evidence") or "")
                    if evidence and Path(evidence).name == evidence:
                        file = self.store.path.parent / "rejection-evidence" / evidence
                        if file.is_file():
                            archive.write(file, "evidence.png")
                if temporary.stat().st_size > MAX_RESULT_SIZE:
                    raise ValueError("门店采集结果超过可同步的大小")
                with temporary.open("r+b") as stream:
                    os.fsync(stream.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        with self.store.connect() as db:
            db.execute("UPDATE worker_assignments SET status='pending_upload' WHERE id=?", (assignment["id"],))

    def prepare_results(self) -> None:
        """只转存已写完的门店。运行指针还未前进的中断保留任务，等人工复核或恢复。"""
        with self.store.connect() as db:
            candidates = [dict(row) for row in db.execute("""
                SELECT * FROM worker_assignments WHERE status IN ('ready','reclaimed') AND run_id<>''
            """)]
        runs = {}
        for assigned in candidates:
            run_id = assigned["run_id"]
            if run_id not in runs:
                runs[run_id] = self.store.get_run(run_id)
            run = runs[run_id]
            if not run:
                continue
            task = self.store.get(assigned["task_id"])
            completed = assigned["task_id"] in run["task_ids"][:run["current_index"]]
            if completed and task["status"] not in {"pending", "running"}:
                with self.output_lock:
                    self._queue_result(assigned, task)

    def record_annotation(self, task_id: str) -> None:
        """独立人工标注与批量采集共用补传队列，普通浏览和暂停不会生成结果。"""
        with self.store.connect() as db:
            row = db.execute("""
                SELECT * FROM worker_assignments WHERE task_id=? AND status IN ('ready','reclaimed')
                ORDER BY rowid DESC LIMIT 1
            """, (task_id,)).fetchone()
        if row:
            task = self.store.get(task_id)
            if task.get("stage") == "annotation_complete" or task.get("reject_reason"):
                with self.output_lock:
                    self._queue_result(dict(row), task)
                self._wake.set()

    def _download(self, assigned: dict) -> None:
        for key in ("id", "source_hash"):
            if not re.fullmatch(r"[0-9a-f]{32}" if key == "id" else r"[0-9a-f]{64}", str(assigned.get(key, ""))):
                raise ValueError("后台返回了无效的分配标识")
        if assigned.get("source_suffix") not in IMAGE_SUFFIXES:
            raise ValueError("后台返回了不支持的二维码文件")
        path = self._source_path(assigned)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, filename = tempfile.mkstemp(dir=path.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(fd, "wb") as stream:
                with self._http.stream("GET", self._url(f"/assignments/{assigned['id']}/source"),
                                       headers=self._headers) as response:
                    response.raise_for_status()
                    for block in response.iter_bytes():
                        size += len(block)
                        if size > MAX_SOURCE_SIZE:
                            raise ValueError("二维码文件超过缓存大小限制")
                        digest.update(block)
                        stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if digest.hexdigest() != assigned["source_hash"]:
                raise ValueError("二维码下载不完整，稍后重试")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        with self.control_lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            busy = db.execute("""
                SELECT run_id FROM worker_assignments WHERE task_id=? AND id<>?
                AND status IN ('ready','reclaimed','pending_upload') AND run_id<>''
            """, (assigned["task_id"], assigned["id"])).fetchall()
            for row in busy:
                run = db.execute("SELECT status FROM runs WHERE id=?", (row["run_id"],)).fetchone()
                if run and run["status"] in {"running", "pause_requested", "resuming", "terminating"}:
                    return  # 新分配等旧运行到店级边界后再接管本机任务行。
            db.execute("""
                INSERT INTO tasks(id,source_name,source_path,source_hash,source_key,updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET source_name=excluded.source_name,source_path=excluded.source_path,
                source_hash=excluded.source_hash,source_key=excluded.source_key,updated_at=excluded.updated_at,
                status='pending',stage='queued',message='',output_dir='',reject_reason='',reject_detail='',
                reject_evidence='',rejected_at='',reject_reviewed_at='',error_code=''
            """, (assigned["task_id"], assigned["store"], str(path), assigned["source_hash"],
                  "shared:" + assigned["id"], now()))
            db.execute("""
                INSERT INTO worker_assignments (id,task_id,batch_id,batch_name,store,revision,snapshot_id,
                    source_hash,source_suffix,status,created_at)
                VALUES (:id,:task_id,:batch_id,:batch_name,:store,:revision,:snapshot_id,
                    :source_hash,:source_suffix,'ready',:created_at)
            """, assigned)

    def _heartbeat(self) -> set[str]:
        with self.store.connect() as db:
            ready = [row["id"] for row in db.execute("SELECT id FROM worker_assignments WHERE status='ready'")]
            pending = db.execute("SELECT COUNT(*) FROM worker_assignments WHERE status='pending_upload'").fetchone()[0]
        active = None
        for start in range(0, max(len(ready), 1), 1000):
            response = self._http.post(self._url("/sync"), headers=self._headers,
                                       json={"ready_ids": ready[start:start + 1000], "pending_uploads": pending})
            response.raise_for_status()
            active = response.json()["assignments"]
        active_ids = {row["id"] for row in active}
        with self.control_lock:
            self.prepare_results()
            with self.store.connect() as db:
                local = {row["id"] for row in db.execute("SELECT id FROM worker_assignments")}
                db.executemany("UPDATE worker_assignments SET status='reclaimed' WHERE id=? AND status='ready'",
                               ((row_id,) for row_id in local - active_ids))
        self.online = True
        for assigned in active:
            if assigned["id"] not in local:
                self._download(assigned)
                self._wake.set()
        return active_ids

    def _upload_pending(self) -> None:
        with self.store.connect() as db:
            pending = [row["id"] for row in db.execute("SELECT id FROM worker_assignments WHERE status='pending_upload'")]
        for assignment_id in pending:
            if self._stop.is_set():
                return
            path = self.outbox / (assignment_id + ".zip")
            try:
                with path.open("rb") as stream:
                    response = self._http.put(self._url(f"/assignments/{assignment_id}/result"),
                                              headers=self._headers | {"Content-Type": "application/zip",
                                                                      "Content-Length": str(path.stat().st_size)},
                                              content=iter(lambda: stream.read(1024 * 1024), b""))
                response.raise_for_status()
                disposition = response.json()["disposition"]
                if disposition not in {"accepted", "history"}:
                    raise ValueError("后台未确认此结果")
            except (httpx.HTTPStatusError, OSError, ValueError, KeyError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
                    raise
                self.error = "有采集结果尚未同步，请检查后台或本机磁盘空间"
                continue
            with self.store.connect() as db:
                db.execute("UPDATE worker_assignments SET status='completed',disposition=? WHERE id=?",
                           (disposition, assignment_id))
            path.unlink(missing_ok=True)

    def poll(self) -> None:
        """先保存本机结果，网络不可用时仍按本机队列执行。"""
        try:
            self.error = ""
            with self.control_lock:
                self.prepare_results()
            self._upload_pending()
            self._heartbeat()
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            self.online = False
            if isinstance(exc, (httpx.HTTPError, KeyError)):
                self.error = "共享工作台暂不可达，已下载门店可继续采集"
            else:
                self.error = str(exc)[:200]
