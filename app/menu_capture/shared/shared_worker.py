"""采集端缓存已分配的二维码，持久化未上传的结果，并自动与共享后台对账。"""

from __future__ import annotations

import hashlib
from ipaddress import ip_address, ip_network
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_STORED, ZipFile

import httpx

from menu_capture.core.config import atomic_json
from menu_capture.shared.shared import IMAGE_SUFFIXES, MAX_RESULT_SIZE, MAX_SOURCE_SIZE, REPORTED_RUN_STATUSES, safe_artifact
from menu_capture.shared.shared_history import WorkerHistoryDelivery
from menu_capture.storage.store import TaskStore, now


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
        self._hub_reports_runs = False
        self._hub_reports_software = False
        self._hub_supports_history = False
        # Hub 分发的版本元数据由更新服务消费；Hub 未启用时保持 None。
        self.hub_update_target: dict | None = None
        self.software_provider = None
        self.history = WorkerHistoryDelivery(store)
        self.history_error = ""
        self.error = ""
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS worker_assignments (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                    batch_name TEXT NOT NULL, store TEXT NOT NULL, revision TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, source_hash TEXT NOT NULL, source_suffix TEXT NOT NULL,
                    status TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT '', shared_state TEXT NOT NULL DEFAULT '',
                    shared_reject TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS worker_assignment_task ON worker_assignments(task_id,status);
                CREATE INDEX IF NOT EXISTS worker_assignment_source ON worker_assignments(source_hash);
                CREATE TABLE IF NOT EXISTS worker_batches (
                    batch_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            # 旧安装包的表没有共享端状态列，按需补齐，保留已有本机任务。
            columns = {row["name"] for row in db.execute("PRAGMA table_info(worker_assignments)")}
            for name in ("shared_state", "shared_reject"):
                if name not in columns:
                    db.execute(f"ALTER TABLE worker_assignments ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

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
                "history_error": self.history_error,
                "credential_invalid": bool(self.connection.get("invalid")),
                "pending_uploads": sum(row["status"] == "pending_upload" for row in assignments),
                "assignments": assignments}

    def tasks(self) -> list[dict]:
        """采集端只展示未结束的分配：结果一旦上传成功（completed）即代表共享端已收到。"""
        with self.store.connect() as db:
            rows = db.execute("""
                SELECT t.*, a.batch_id, a.batch_name, a.revision AS version, a.snapshot_id,
                       a.status AS assignment_status, a.shared_state, a.shared_reject
                FROM tasks t JOIN worker_assignments a ON a.task_id=t.id
                WHERE a.status IN ('ready','pending_upload')
                  AND a.rowid=(SELECT MAX(b.rowid) FROM worker_assignments b WHERE b.task_id=t.id)
                  -- 只展示所属批次当前发布快照的任务；首次同步前没有记录时保留旧行为。
                  AND a.snapshot_id=COALESCE(
                      (SELECT b.snapshot_id FROM worker_batches b WHERE b.batch_id=a.batch_id), a.snapshot_id)
                ORDER BY a.rowid
            """).fetchall()
        # 共享端已判定不合格/已采集/已退回的门店不再允许本机重跑。
        blocked = {"rejected", "captured", "returned"}
        return [dict(row) | {"runnable": row["assignment_status"] == "ready"
                             and row["shared_state"] not in blocked} for row in rows]

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
        if self.connection.get("invalid"):
            raise ValueError("采集电脑的配对已失效，请联系管理员")
        self.prepare_results()
        with self.store.connect() as db:
            states = {row["task_id"]: row["shared_state"] for row in db.execute(
                "SELECT task_id,shared_state FROM worker_assignments WHERE status='ready'")}
        if any(states.get(task_id) in {"rejected", "captured", "returned"} for task_id in task_ids):
            raise ValueError("有门店已在共享端完成或标记不合格，请刷新后重新选择")
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
            # 可选副产物：随成功/待检查结果一起上行；没有观察结果时省略，旧 Hub 忽略未知字段。
            observed = str(self.store.observed_store_name(assignment.get("run_id", ""), task["id"]) or "")
            if observed:
                manifest["observed_store_name"] = observed
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

    def _cached_sources(self, assigned: dict) -> list[Path]:
        # 采集器仍按各自分配目录处理；可直接从旧分配的本地文件复用相同内容。
        with self.store.connect() as db:
            rows = db.execute("SELECT id,source_suffix FROM worker_assignments WHERE source_hash=? ORDER BY rowid DESC",
                              (assigned["source_hash"],)).fetchall()
        return [self._source_path(assigned), *(self._source_path(row) for row in rows if row["id"] != assigned["id"])]

    @staticmethod
    def _copy_source(blocks: Iterable[bytes], target: Path, expected_hash: str) -> bool:
        digest = hashlib.sha256()
        size = 0
        with target.open("wb") as stream:
            for block in blocks:
                size += len(block)
                if size > MAX_SOURCE_SIZE:
                    return False
                digest.update(block)
                stream.write(block)
            if digest.hexdigest() != expected_hash:
                return False
            stream.flush()
            os.fsync(stream.fileno())
        return True

    def _cache_source(self, assigned: dict, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(dir=path.parent)
        os.close(fd)
        temporary = Path(filename)
        try:
            for cached in self._cached_sources(assigned):
                try:
                    source = cached.open("rb")
                except (FileNotFoundError, PermissionError):
                    continue
                with source:
                    valid = self._copy_source(iter(lambda: source.read(256 * 1024), b""),
                                              temporary, assigned["source_hash"])
                if valid:
                    temporary.replace(path)
                    return
            with self._http.stream("GET", self._url(f"/assignments/{assigned['id']}/source"),
                                   headers=self._headers) as response:
                response.raise_for_status()
                if not self._copy_source(response.iter_bytes(), temporary, assigned["source_hash"]):
                    raise ValueError("二维码内容不完整或超过缓存大小限制，稍后重试")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _download(self, assigned: dict) -> None:
        for key in ("id", "source_hash"):
            if not re.fullmatch(r"[0-9a-f]{32}" if key == "id" else r"[0-9a-f]{64}", str(assigned.get(key, ""))):
                raise ValueError("后台返回了无效的分配标识")
        if assigned.get("source_suffix") not in IMAGE_SUFFIXES:
            raise ValueError("后台返回了不支持的二维码文件")
        path = self._source_path(assigned)
        self._cache_source(assigned, path)
        with self.control_lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT status FROM worker_assignments WHERE id=?",
                                  (assigned["id"],)).fetchone()
            if existing:
                # 同一分配补下载：只恢复可采集状态，不重置已有任务结果。
                db.execute("UPDATE worker_assignments SET status='ready' WHERE id=? AND status='reclaimed'",
                           (assigned["id"],))
                return
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

    def _run_summary(self) -> dict | None:
        run = self.store.latest_run()
        if not run or run["status"] not in REPORTED_RUN_STATUSES:
            return None
        total = len(run["task_ids"])
        return {
            "status": run["status"], "mode": run.get("capture_mode") or "",
            "processed": max(0, min(total, int(run["current_index"]))), "total": total,
        }

    def _heartbeat(self) -> set[str]:
        with self.store.connect() as db:
            ready = [row["id"] for row in db.execute("SELECT id FROM worker_assignments WHERE status='ready'")]
            pending = db.execute("SELECT COUNT(*) FROM worker_assignments WHERE status='pending_upload'").fetchone()[0]
        active = None
        summary = self._run_summary() if self._hub_reports_runs else None
        software = None
        if self._hub_reports_software and self.software_provider is not None:
            try:
                candidate = self.software_provider()
                software = candidate if isinstance(candidate, dict) else None
            except Exception:  # noqa: BLE001 - 状态上报失败不能影响任务同步
                software = None
        for start in range(0, max(len(ready), 1), 1000):
            payload = {"ready_ids": ready[start:start + 1000], "pending_uploads": pending}
            if start == 0 and summary is not None:
                payload["run_summary"] = summary
            if start == 0 and software is not None:
                payload["software"] = software
            response = self._http.post(self._url("/sync"), headers=self._headers, json=payload)
            # Hub 回滚后可能拒绝新字段。逐步退回旧格式，确保任务下载与补传继续。
            if response.status_code == 400 and "software" in payload:
                del payload["software"]
                self._hub_reports_software = False
                response = self._http.post(self._url("/sync"), headers=self._headers, json=payload)
            if response.status_code == 400 and "run_summary" in payload:
                del payload["run_summary"]
                response = self._http.post(self._url("/sync"), headers=self._headers, json=payload)
            response.raise_for_status()
            sync = response.json()
            self._hub_reports_runs = sync.get("supports_run_summary") is True
            self._hub_reports_software = sync.get("supports_software_status") is True
            self._hub_supports_history = sync.get("supports_run_history") is True
            target = sync.get("update_target")
            self.hub_update_target = target if isinstance(target, dict) else None
            active = sync["assignments"]
            name = sync.get("worker_name")
            if isinstance(name, str) and name:
                self.connection["name"] = name
        active_ids = {row["id"] for row in active}
        with self.control_lock:
            self.prepare_results()
            self._store_shared_state(sync)
            with self.store.connect() as db:
                local = {row["id"] for row in db.execute("SELECT id FROM worker_assignments")}
                db.executemany("UPDATE worker_assignments SET status='reclaimed' WHERE id=? AND status='ready'",
                               ((row_id,) for row_id in local - active_ids))
        self.online = True
        for assigned in active:
            try:
                self._ensure_source(assigned)
            except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
                self.error = str(exc)[:200]
        return active_ids

    def _sync_run_history(self) -> None:
        if not self._hub_supports_history:
            return
        # 轮次同步失败不影响分配下载和截图补传，待下一次轮询继续。
        try:
            report = self.history.pending_report()
            if report is None:
                return
            response = self._http.put(self._url("/run-history"), headers=self._headers, json=report, timeout=5)
            response.raise_for_status()
            if response.json().get("acknowledged") != report["generation"]:
                raise ValueError("接收端未确认轮次")
            self.history.acknowledge(report["run"]["id"], report["generation"])
            self.history_error = ""
        except (httpx.HTTPError, OSError, ValueError, KeyError):
            self.history_error = "采集轮次历史尚未同步，稍后自动重试"

    def _ensure_source(self, assigned: dict) -> None:
        """保证活动分配在本机有二维码文件，并把任务来源路径纠正到本分配缓存目录。"""
        path = self._source_path(assigned)
        with self.store.connect() as db:
            local = db.execute(
                "SELECT a.status,t.source_path FROM worker_assignments a "
                "LEFT JOIN tasks t ON t.id=a.task_id WHERE a.id=? AND a.task_id=?",
                (assigned["id"], assigned["task_id"]),
            ).fetchone()
        needs_registration = local is None or local["status"] == "reclaimed" or not path.is_file()
        needs_path_repair = (
            local is None
            or local["status"] == "reclaimed"
            or (local["status"] == "ready" and local["source_path"] != str(path))
        )
        if needs_registration:
            # 缓存文件可能在上次同步时先落盘，后续因同店旧运行占用而推迟登记。
            # 是否存在文件不能代表分配已登记，必须重新经过 _download 的登记流程。
            self._download(assigned)
        if needs_path_repair:
            # _download 遇到同店仍在执行的旧轮次会暂缓登记。此时保持旧任务来源不变。
            # 同时要求这仍是最新的 ready assignment，避免迟到心跳覆盖新分配的路径。
            with self.store.connect() as db:
                db.execute(
                    "UPDATE tasks SET source_path=? WHERE id=? AND EXISTS ("
                    "SELECT 1 FROM worker_assignments a WHERE a.id=? AND a.task_id=tasks.id "
                    "AND a.status='ready' AND a.rowid=(SELECT MAX(b.rowid) "
                    "FROM worker_assignments b WHERE b.task_id=a.task_id))",
                    (str(path), assigned["task_id"], assigned["id"]),
                )

    def _store_shared_state(self, sync: dict) -> None:
        """落库共享端下发的当前快照与门店权威状态，供本机列表过滤和展示。"""
        stamp = now()
        batches = [row for row in sync.get("batches") or []
                   if isinstance(row, dict) and row.get("batch_id") and row.get("snapshot_id")]
        states = {str(key): value for key, value in (sync.get("states") or {}).items()
                  if isinstance(value, dict)}
        with self.store.connect() as db:
            if batches:
                db.executemany(
                    "INSERT INTO worker_batches(batch_id,snapshot_id,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(batch_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,"
                    "updated_at=excluded.updated_at",
                    ((row["batch_id"], row["snapshot_id"], stamp) for row in batches))
            if states:
                db.executemany(
                    "UPDATE worker_assignments SET shared_state=?,shared_reject=? WHERE task_id=?",
                    ((value.get("state", ""), value.get("reject_reason", ""), task_id)
                     for task_id, value in states.items()))

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
                if isinstance(exc, httpx.HTTPStatusError):
                    response = exc.response
                    if response.status_code >= 500:
                        raise
                    if response.status_code == 401:
                        # 凭证失效时保留整个补传队列，避免逐包重复发送。
                        try:
                            detail = response.json().get("detail")
                        except (ValueError, AttributeError):
                            detail = None
                        if detail in ("采集端凭证无效", "采集电脑已解除配对"):
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
            if not self.connection.get("invalid"):
                self._upload_pending()
            self._heartbeat()
            self._sync_run_history()
            if self.connection.pop("invalid", None):
                atomic_json(self.credentials, self.connection)
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            self.online = False
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                if not self.connection.get("invalid"):
                    self.connection["invalid"] = True
                    try:
                        atomic_json(self.credentials, self.connection)
                    except OSError:
                        self.error = "配对已失效，本机无法保存连接状态。请联系管理员。"
                        return
                self.error = "采集电脑的配对已失效，本机结果仍保留。请联系管理员。"
            elif self.connection.get("invalid"):
                self.error = "采集电脑的配对已失效，本机结果仍保留。请联系管理员。"
            elif isinstance(exc, (httpx.HTTPError, KeyError)):
                self.error = "共享工作台暂不可达，已下载门店可继续采集"
            else:
                self.error = str(exc)[:200]
