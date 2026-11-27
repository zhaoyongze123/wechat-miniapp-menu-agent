"""采集端缓存已分配的二维码，持久化未上传的结果，并自动与共享后台对账。"""

from __future__ import annotations

import hashlib
from ipaddress import ip_address, ip_network
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_STORED, ZipFile

import httpx

from menu_capture.core.config import atomic_json
from menu_capture.shared.shared import (
    IMAGE_SUFFIXES, MAX_RESULT_SIZE, MAX_SOURCE_SIZE, REPORTED_RUN_STATUSES, safe_artifact, utc_stamp,
)
from menu_capture.shared.shared_history import WorkerHistoryDelivery
from menu_capture.storage.store import TaskStore, now
from menu_capture.projects.service import LEGACY_ID


TAILSCALE_IPV4 = ip_network("100.64.0.0/10")
WORKER_CAPABILITIES = "project_scope,run_history,run_summary,software_status"
POLL_INTERVAL_SECONDS = 5
POLL_BACKOFF_MAX_SECONDS = 15
HEARTBEAT_KEEPALIVE_SECONDS = 15.0
OFFLINE_FAILURE_THRESHOLD = 3


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
        # 串行化配对请求，避免并发配对把两次成功的凭证互相覆盖。
        self._pair_lock = threading.Lock()
        # 保护 self.connection 的替换与最终状态写入：身份判定和落库之间不允许被配对打断。
        self._state_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self.online = False
        self._consecutive_failures = 0
        self._last_heartbeat_mono = 0.0
        self._last_connected_at = ""
        self._hub_reports_runs = False
        self._hub_reports_software = False
        self._hub_supports_history = False
        # 该能力由本次配对 Hub 的同步响应明确协商，不能根据本机版本号推断。
        self._hub_supports_project_scope = self.connection.get("supports_project_scope") is True
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
                    batch_name TEXT NOT NULL, store TEXT NOT NULL, store_id TEXT NOT NULL DEFAULT '',
                    revision TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, source_hash TEXT NOT NULL, source_suffix TEXT NOT NULL,
                    status TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT '', shared_state TEXT NOT NULL DEFAULT '',
                    shared_reject TEXT NOT NULL DEFAULT '', worker_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS worker_assignment_task ON worker_assignments(task_id,status);
                CREATE INDEX IF NOT EXISTS worker_assignment_source ON worker_assignments(source_hash);
                CREATE TABLE IF NOT EXISTS worker_batches (
                    batch_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_project_batches (
                    worker_id TEXT NOT NULL, batch_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(worker_id,batch_id)
                );
                CREATE TABLE IF NOT EXISTS worker_project_catalogue (
                    worker_id TEXT NOT NULL, project_id TEXT NOT NULL, code TEXT NOT NULL,
                    name TEXT NOT NULL, archived_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    PRIMARY KEY(worker_id,project_id)
                );
            """)
            # 旧安装包的表没有共享端状态列，按需补齐，保留已有本机任务。
            columns = {row["name"] for row in db.execute("PRAGMA table_info(worker_assignments)")}
            for name in ("shared_state", "shared_reject", "worker_id", "project_id", "project_code", "store_id",
                         "project_name"):
                if name not in columns:
                    db.execute(f"ALTER TABLE worker_assignments ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            # 旧待同步轮次仅在逐店分配能证明唯一设备身份时回填配对范围；混合或空身份保持未知。
            db.execute("""
                UPDATE worker_run_delivery SET worker_id=(
                    SELECT MIN(a.worker_id) FROM worker_assignments a
                    WHERE a.run_id=worker_run_delivery.run_id AND a.worker_id<>'')
                WHERE worker_id='' AND EXISTS (
                    SELECT 1 FROM worker_assignments a WHERE a.run_id=worker_run_delivery.run_id)
                  AND NOT EXISTS (
                    SELECT 1 FROM worker_assignments a WHERE a.run_id=worker_run_delivery.run_id
                      AND a.worker_id='')
                  AND (SELECT COUNT(DISTINCT a.worker_id) FROM worker_assignments a
                       WHERE a.run_id=worker_run_delivery.run_id AND a.worker_id<>'')=1
            """)

        # 上次重新配对若在“凭证已替换、旧分配尚未归档”之间退出，这里补做归档，
        # 避免旧身份的任务被当成当前设备可执行/可上传分配。
        pending_detach = str(self.connection.get("detach_from") or "")
        if pending_detach:
            self._detach_previous(pending_detach)
            self.connection.pop("detach_from", None)
            self._persist_connection()

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
                wait_seconds = min(
                    POLL_BACKOFF_MAX_SECONDS,
                    POLL_INTERVAL_SECONDS + self._consecutive_failures * 3,
                ) if self.connection else POLL_INTERVAL_SECONDS
                self._wake.wait(wait_seconds)
                self._wake.clear()
        finally:
            self._http.close()

    def connect(self, url: str, code: str, name: str) -> dict:
        url = hub_url(url)
        with self._pair_lock:
            with self._state_lock:
                if self.connection and not self.connection.get("invalid"):
                    raise ValueError("此采集端已配对")
                previous = str(self.connection.get("id") or "")
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
            connection = {"url": url, "token": data["token"], "name": data.get("name", name), "id": data["id"],
                          "supports_project_scope": False}
            if previous and previous != connection["id"]:
                # 归档标记与凭证一起原子落盘：归档前异常退出时，下次启动会补做。
                connection["detach_from"] = previous
                abandoned = self._pending_count(previous)
                if abandoned:
                    connection["abandoned_pending"] = abandoned
            with self._state_lock:
                atomic_json(self.credentials, connection)
                self.connection = connection
                self.online = True
                self._consecutive_failures = 0
                self._last_heartbeat_mono = time.monotonic()
                self._last_connected_at = utc_stamp()
                self.error = ""
                self._hub_supports_project_scope = False
            if connection.get("detach_from"):
                self._detach_previous(connection["detach_from"])
                with self._state_lock:
                    self.connection.pop("detach_from", None)
                    if not self._persist_connection():
                        self.error = "配对成功，但本机无法更新凭证文件，请检查磁盘权限"
            self._wake.set()
        return self.status()

    def _persist_connection(self) -> bool:
        """尽力把当前连接状态写回本机；失败只影响下次启动的恢复提示。"""
        try:
            atomic_json(self.credentials, self.connection)
        except OSError:
            return False
        return True

    def _pending_count(self, worker_id: str) -> int:
        with self.store.connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM worker_assignments WHERE status='pending_upload'"
                " AND (worker_id='' OR worker_id=?)", (worker_id,)).fetchone()[0]

    def _detach_previous(self, worker_id: str) -> None:
        """把旧身份下的本地分配归属到旧 worker_id 并停止重试。

        只记录归属、结束活动状态：任务行、截图与 outbox 压缩包全部保留，
        不迁移到新身份，也不删除；需要补传时由管理员在旧设备身份下处理。
        """
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE worker_assignments SET worker_id=?"
                       " WHERE worker_id='' AND status IN ('ready','pending_upload')", (worker_id,))
            db.execute("UPDATE worker_assignments SET status='reclaimed'"
                       " WHERE worker_id=? AND status IN ('ready','pending_upload')", (worker_id,))

    def _current_worker_id(self) -> str:
        return str(self.connection.get("id") or "")

    def _url(self, suffix: str, connection: dict | None = None) -> str:
        return (connection or self.connection)["url"] + "/api/shared/worker" + suffix

    @staticmethod
    def _auth(connection: dict) -> dict:
        return {"Authorization": "Bearer " + connection["token"]}

    @classmethod
    def _capability_headers(cls, connection: dict) -> dict:
        # 旧 Hub 忽略该提示头；新 Hub 仍须以响应 capability 决定是否允许扩展 body。
        return cls._auth(connection) | {"X-Worker-Capabilities": WORKER_CAPABILITIES}

    @property
    def _headers(self) -> dict:
        return self._auth(self.connection)

    def _connection_state(self) -> str:
        """区分未配对、凭证失效、在线、瞬时波动（degraded）和确认离线（offline）。"""
        if not self.connection:
            return "unpaired"
        if self.connection.get("invalid"):
            return "credential_invalid"
        if self.online:
            return "online"
        if 0 < self._consecutive_failures < OFFLINE_FAILURE_THRESHOLD:
            return "degraded"
        return "offline"

    def status(self, project_id: str | None = None) -> dict:
        abandoned = int(self.connection.get("abandoned_pending") or 0)
        if project_id is not None and project_id not in {row["id"] for row in self.projects()}:
            raise ValueError("项目不存在或该采集端没有分配记录")
        scoped = project_id is not None
        with self.store.connect() as db:
            if scoped:
                worker_filter = "(worker_id='' OR worker_id=?)"
                params: tuple[object, ...] = (self._current_worker_id(),)
                if self._hub_supports_project_scope:
                    worker_filter += " AND project_id=?"
                    params += (project_id,)
            else:
                worker_filter = "0"
                params = ()
            assignments = [dict(row) for row in db.execute("""
                SELECT id,task_id,batch_id,batch_name,store,revision,status,disposition,
                       project_id,project_code,project_name FROM worker_assignments
                WHERE status NOT IN ('completed','reclaimed') AND """ + worker_filter + " ORDER BY rowid",
                params)]
            assignments.extend(dict(row) for row in db.execute("""
                SELECT id,task_id,batch_id,batch_name,store,revision,status,disposition,
                       project_id,project_code,project_name FROM worker_assignments
                WHERE status IN ('completed','reclaimed') AND """ + worker_filter +
                " ORDER BY created_at DESC,id LIMIT 100", params))
        return {"role": "worker", "paired": bool(self.connection), "online": self.online,
                "connection_state": self._connection_state(),
                "consecutive_failures": self._consecutive_failures,
                "last_connected_at": self._last_connected_at,
                "supports_project_scope": self._hub_supports_project_scope,
                "name": self.connection.get("name", ""), "error": self.error,
                "history_error": self.history_error,
                "credential_invalid": bool(self.connection.get("invalid")),
                "pending_uploads": sum(row["status"] == "pending_upload" for row in assignments),
                # 重新配对后旧身份下无法补传的结果数量与提示；结果文件仍保留在本机。
                "orphaned_pending": abandoned,
                "notice": (f"重新配对后，旧设备身份下的 {abandoned} 条未上传结果已保留在本机，"
                           "无法再用新凭证补传；请在旧设备身份下处理或联系管理员导出。") if abandoned else "",
                "assignments": assignments}

    def projects(self) -> list[dict]:
        """返回当前配对 Hub 下本机可证明的项目，不从当前 UI 选择推断历史归属。"""
        worker_id = self._current_worker_id()
        if not worker_id:
            return []
        if not self._hub_supports_project_scope:
            # 旧 Hub 不支持项目协议时，继续以固定历史兼容项目承载原单工作区流程。
            with self.store.connect() as db:
                row = db.execute("SELECT id,code,name,archived_at FROM projects WHERE id=?", (LEGACY_ID,)).fetchone()
            return [dict(row)] if row else []
        with self.store.connect() as db:
            projects = {row["id"]: dict(row) for row in db.execute("""
                SELECT project_id AS id,code,name,archived_at FROM worker_project_catalogue
                WHERE worker_id=?
            """, (worker_id,))}
            assignments = db.execute("""
                SELECT project_id,project_code,project_name FROM worker_assignments
                WHERE worker_id=? AND project_id<>''
            """, (worker_id,)).fetchall()
        for row in assignments:
            project_id = row["project_id"]
            if not project_id:
                continue
            current = projects.get(project_id)
            if current is None:
                projects[project_id] = {"id": project_id, "code": row["project_code"],
                                        "name": row["project_name"], "archived_at": ""}
        return sorted(projects.values(), key=lambda row: (row["code"].casefold(), row["id"]))

    def tasks(self, project_id: str | None = None) -> list[dict]:
        """采集端只展示未结束的分配：结果一旦上传成功（completed）即代表共享端已收到。"""
        if self._hub_supports_project_scope and not project_id:
            return []
        if not self._hub_supports_project_scope and project_id not in (None, LEGACY_ID):
            return []
        with self.store.connect() as db:
            project_filter = " AND a.project_id=?" if self._hub_supports_project_scope else ""
            rows = db.execute("""
                SELECT t.*, a.batch_id, a.batch_name, a.revision AS version, a.snapshot_id,
                       a.status AS assignment_status, a.shared_state, a.shared_reject,
                       a.project_id, a.project_code, a.project_name
                FROM tasks t JOIN worker_assignments a ON a.task_id=t.id
                WHERE a.status IN ('ready','pending_upload')
                  -- 重新配对后旧设备身份下的分配不能再被新凭证执行。
                  AND (a.worker_id='' OR a.worker_id=?)
                  AND a.rowid=(SELECT MAX(b.rowid) FROM worker_assignments b WHERE b.task_id=t.id)
                  -- 只展示所属批次当前发布快照的任务；首次同步前没有记录时保留旧行为。
                  AND a.snapshot_id=CASE WHEN ?=1 THEN COALESCE(
                      (SELECT b.snapshot_id FROM worker_project_batches b
                       WHERE b.worker_id=? AND b.batch_id=a.batch_id AND b.project_id=a.project_id), a.snapshot_id)
                    ELSE COALESCE(
                      (SELECT b.snapshot_id FROM worker_batches b WHERE b.batch_id=a.batch_id), a.snapshot_id)
                    END
                  AND (?=0 OR a.project_id<>'')
            """ + project_filter + " ORDER BY a.rowid",
                (self._current_worker_id(), int(self._hub_supports_project_scope),
                 self._current_worker_id(), int(self._hub_supports_project_scope),
                 *((project_id,) if self._hub_supports_project_scope else ()),)).fetchall()
        # 共享端已判定不合格/已采集/已退回的门店不再允许本机重跑。
        blocked = {"rejected", "captured", "returned"}
        return [dict(row) | {"runnable": row["assignment_status"] == "ready"
                             and row["shared_state"] not in blocked} for row in rows]

    def _source_path(self, assignment: dict) -> Path:
        return self.inputs / assignment["id"] / "source" / ("二维码" + assignment["source_suffix"])

    def source_root(self, task_id: str) -> str:
        with self.store.connect() as db:
            if self._hub_supports_project_scope:
                row = db.execute("""
                    SELECT * FROM worker_assignments WHERE task_id=? AND status='ready' AND worker_id=?
                    ORDER BY rowid DESC LIMIT 1
                """, (task_id, self._current_worker_id())).fetchone()
            else:
                row = db.execute("""
                    SELECT * FROM worker_assignments WHERE task_id=? AND status='ready'
                      AND (worker_id='' OR worker_id=?) ORDER BY rowid DESC LIMIT 1
                """, (task_id, self._current_worker_id())).fetchone()
        if not row:
            return ""
        path = self._source_path(row)
        return str(path.parent.parent) if path.is_file() else ""

    def output_binding(self, task_id: str) -> dict | None:
        """把 Worker 成品路径绑定到已缓存 assignment；不能从界面当前项目推断归属。"""
        if not self._hub_supports_project_scope:
            return None
        with self.store.connect() as db:
            row = db.execute("""
                SELECT project_id,project_code,batch_id,store_id,task_id FROM worker_assignments
                WHERE task_id=? AND worker_id=? AND project_id<>''
                ORDER BY rowid DESC LIMIT 1
            """, (task_id, self._current_worker_id())).fetchone()
        if (not row or not row["project_code"] or not row["batch_id"]
                or row["project_id"] not in {project["id"] for project in self.projects()}):
            raise ValueError("无法从当前设备分配记录核实任务项目，拒绝使用共享输出路径")
        return {"project_id": row["project_id"], "project_code": row["project_code"],
                "batch_id": row["batch_id"], "store_id": row["store_id"] or row["task_id"]}

    def check_start(self, task_ids: list[str], project_id: str | None = None) -> None:
        if self.connection.get("invalid"):
            raise ValueError("采集电脑的配对已失效，请联系管理员")
        if self._hub_supports_project_scope and (not isinstance(project_id, str) or not project_id.strip()):
            raise ValueError("请选择本机已分配的项目")
        if not self._hub_supports_project_scope and project_id not in (None, LEGACY_ID):
            raise ValueError("当前连接的工作台不支持项目范围")
        self.prepare_results()
        with self.store.connect() as db:
            if self._hub_supports_project_scope:
                rows = db.execute("""
                    SELECT task_id,shared_state,project_id FROM worker_assignments
                    WHERE status='ready' AND worker_id=? ORDER BY rowid
                """, (self._current_worker_id(),)).fetchall()
            else:
                rows = db.execute("""
                    SELECT task_id,shared_state,project_id FROM worker_assignments
                    WHERE status='ready' AND (worker_id='' OR worker_id=?) ORDER BY rowid
                """, (self._current_worker_id(),)).fetchall()
        states = {row["task_id"]: row["shared_state"] for row in rows}
        projects = {row["task_id"]: row["project_id"] for row in rows}
        if any(states.get(task_id) in {"rejected", "captured", "returned"} for task_id in task_ids):
            raise ValueError("有门店已在共享端完成或标记不合格，请刷新后重新选择")
        if self._hub_supports_project_scope and any(projects.get(task_id) != project_id for task_id in task_ids):
            raise ValueError("所选门店未全部属于当前项目，请刷新任务列表")
        if any(not self.source_root(task_id) for task_id in task_ids):
            raise ValueError("请只选择本机已下载、可离线采集的门店")

    def bind_run(self, db, run_id: str, task_ids: list[str]) -> None:
        # 与运行记录同一事务，退出或断电后仍知道哪次运行产出了结果。
        if self._hub_supports_project_scope:
            run = db.execute("SELECT project_id FROM runs WHERE id=?", (run_id,)).fetchone()
            project_id = str(run["project_id"] or "") if run else ""
            worker_id = self._current_worker_id()
            if not project_id:
                raise ValueError("项目模式下运行记录缺少固定项目身份")
            for task_id in task_ids:
                assignment = db.execute("""
                    SELECT project_id FROM worker_assignments WHERE task_id=? AND status='ready' AND worker_id=?
                    ORDER BY rowid DESC LIMIT 1
                """, (task_id, worker_id)).fetchone()
                if not assignment or assignment["project_id"] != project_id:
                    raise ValueError("开跑时门店分配项目已变化，请刷新后重试")
            db.executemany("UPDATE worker_assignments SET run_id=? WHERE task_id=? AND status='ready' AND worker_id=?",
                           ((run_id, task_id, worker_id) for task_id in task_ids))
        else:
            db.executemany("UPDATE worker_assignments SET run_id=? WHERE task_id=? AND status='ready'"
                           " AND (worker_id='' OR worker_id=?)",
                           ((run_id, task_id, self._current_worker_id()) for task_id in task_ids))
        # 历史投递绑定创建轮次时的配对身份；重配到另一 Hub 后不会把旧轮次误传过去。
        db.execute("UPDATE worker_run_delivery SET worker_id=? WHERE run_id=?",
                   (self._current_worker_id(), run_id))

    def _queue_result(self, assignment: dict, task: dict) -> None:
        path = self.outbox / (assignment["id"] + ".zip")
        if not path.exists():
            manifest = {key: assignment[key] for key in ("task_id", "snapshot_id", "source_hash")}
            if self._hub_supports_project_scope and assignment.get("project_id"):
                manifest["project_id"] = assignment["project_id"]
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
                  AND (worker_id='' OR worker_id=?)
            """, (self._current_worker_id(),))]
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

    def _cache_source(self, assigned: dict, path: Path, connection: dict) -> None:
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
            # 使用本次轮询的原始凭证下载，重新配对后的新凭证不会替旧身份取素材。
            with self._http.stream("GET", self._url(f"/assignments/{assigned['id']}/source", connection),
                                   headers=self._auth(connection)) as response:
                response.raise_for_status()
                if not self._copy_source(response.iter_bytes(), temporary, assigned["source_hash"]):
                    raise ValueError("二维码内容不完整或超过缓存大小限制，稍后重试")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _download(self, assigned: dict, connection: dict) -> None:
        for key in ("id", "source_hash"):
            if not re.fullmatch(r"[0-9a-f]{32}" if key == "id" else r"[0-9a-f]{64}", str(assigned.get(key, ""))):
                raise ValueError("后台返回了无效的分配标识")
        if assigned.get("source_suffix") not in IMAGE_SUFFIXES:
            raise ValueError("后台返回了不支持的二维码文件")
        worker_id = str(connection.get("id") or "")
        project_fields = ("project_id", "project_code", "project_name")
        project_scope = connection.get("supports_project_scope") is True
        if project_scope:
            if (not all(isinstance(assigned.get(key), str) and assigned[key].strip()
                        for key in project_fields)
                    or len(assigned["project_id"]) > 128 or len(assigned["project_code"]) > 48
                    or len(assigned["project_name"]) > 80):
                raise ValueError("项目能力已协商，但分配缺少有效项目身份")
            project_id = assigned["project_id"].strip()
            project_code = assigned["project_code"].strip()
            project_name = assigned["project_name"].strip()
            store_id = str(assigned.get("store_id") or assigned["task_id"]).strip()
            if not store_id or len(store_id) > 256:
                raise ValueError("项目分配缺少有效门店身份")
        else:
            if any(assigned.get(key) not in (None, "") for key in project_fields):
                raise ValueError("后台返回了未协商的项目字段")
            project_id = project_code = project_name = ""
            store_id = ""
        path = self._source_path(assigned)
        self._cache_source(assigned, path, connection)
        with self.control_lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # 同一 Hub 配对身份下，批次项目归属不可改变；未知旧记录不能用当前项目反向补猜。
            if project_scope:
                known_projects = {row["project_id"] for row in db.execute(
                    "SELECT DISTINCT project_id FROM worker_assignments WHERE worker_id=? AND batch_id=?"
                    " AND project_id<>''", (worker_id, assigned["batch_id"]))}
                batch_project = db.execute(
                    "SELECT project_id FROM worker_project_batches WHERE worker_id=? AND batch_id=?",
                    (worker_id, assigned["batch_id"])).fetchone()
                if (known_projects - {project_id}
                        or batch_project and batch_project["project_id"] != project_id):
                    raise ValueError("同一批次的项目归属与本机已保存事实不一致")
            existing = db.execute("SELECT * FROM worker_assignments WHERE id=?",
                                  (assigned["id"],)).fetchone()
            if existing:
                if project_scope and existing["project_id"] not in ("", project_id):
                    raise ValueError("已保存分配的项目身份与后台返回不一致")
                if project_scope and existing["store_id"] not in ("", store_id):
                    raise ValueError("已保存分配的门店身份与后台返回不一致")
                # 同一分配补下载：只恢复可采集状态，不重置已有任务结果；
                # 旧设备身份下的分配不会因为迟到的同步响应被新设备接管。
                if project_scope and (not existing["project_id"] or not existing["store_id"]):
                    db.execute("UPDATE worker_assignments SET project_id=CASE WHEN project_id='' THEN ? ELSE project_id END,"
                               "project_code=?,project_name=?,store_id=CASE WHEN store_id='' THEN ? ELSE store_id END,worker_id=?"
                               " WHERE id=? AND worker_id IN ('',?)",
                               (project_id, project_code, project_name, store_id, worker_id, assigned["id"], worker_id))
                db.execute("UPDATE worker_assignments SET status='ready' WHERE id=? AND status='reclaimed'"
                           " AND (worker_id='' OR worker_id=?)", (assigned["id"], worker_id))
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
                INSERT INTO worker_assignments (id,task_id,batch_id,batch_name,store,store_id,revision,snapshot_id,
                    source_hash,source_suffix,status,created_at,worker_id,project_id,project_code,project_name)
                VALUES (:id,:task_id,:batch_id,:batch_name,:store,:store_id,:revision,:snapshot_id,
                    :source_hash,:source_suffix,'ready',:created_at,:worker_id,:project_id,:project_code,:project_name)
            """, {**assigned, "store_id": store_id, "worker_id": worker_id, "project_id": project_id,
                  "project_code": project_code, "project_name": project_name})
            if project_scope:
                db.execute("""
                    INSERT INTO worker_project_batches(worker_id,batch_id,project_id,snapshot_id,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(worker_id,batch_id) DO UPDATE SET
                        snapshot_id=excluded.snapshot_id,updated_at=excluded.updated_at
                """, (worker_id, assigned["batch_id"], project_id, assigned["snapshot_id"], now()))
                db.execute("""
                    INSERT INTO worker_project_catalogue(worker_id,project_id,code,name,archived_at,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(worker_id,project_id) DO NOTHING
                """, (worker_id, project_id, project_code, project_name, "", now()))

    def _run_summary(self, *, include_project_scope: bool = False, worker_id: str = "") -> dict | None:
        run = self.store.latest_run()
        if not run or run["status"] not in REPORTED_RUN_STATUSES:
            return None
        total = len(run["task_ids"])
        summary = {
            "status": run["status"], "mode": run.get("capture_mode") or "",
            "processed": max(0, min(total, int(run["current_index"]))), "total": total,
        }
        if include_project_scope:
            summary["project_id"] = str(run.get("project_id") or "") or None
        return summary

    def _heartbeat(self, connection: dict | None = None) -> set[str]:
        connection = connection or self.connection
        worker_id = str(connection.get("id") or "")
        headers = self._capability_headers(connection)
        with self.store.connect() as db:
            ready = [row["id"] for row in db.execute(
                "SELECT id FROM worker_assignments WHERE status='ready' AND (worker_id='' OR worker_id=?)",
                (worker_id,))]
            pending = db.execute(
                "SELECT COUNT(*) FROM worker_assignments WHERE status='pending_upload'"
                " AND (worker_id='' OR worker_id=?)", (worker_id,)).fetchone()[0]
        active = None
        scope_offered = connection.get("supports_project_scope") is True
        summary = self._run_summary(include_project_scope=scope_offered, worker_id=worker_id) \
            if self._hub_reports_runs else None
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
            response = self._http.post(self._url("/sync", connection), headers=headers, json=payload)
            # Hub 回滚后可能拒绝新字段。逐步退回旧格式，确保任务下载与补传继续。
            if response.status_code == 400 and "software" in payload:
                del payload["software"]
                self._hub_reports_software = False
                response = self._http.post(self._url("/sync", connection), headers=headers, json=payload)
            if response.status_code == 400 and isinstance(payload.get("run_summary"), dict) \
                    and "project_id" in payload["run_summary"]:
                payload["run_summary"].pop("project_id", None)
                connection["supports_project_scope"] = False
                self._hub_supports_project_scope = False
                self._persist_connection()
                response = self._http.post(self._url("/sync", connection), headers=headers, json=payload)
            if response.status_code == 400 and "run_summary" in payload:
                del payload["run_summary"]
                self._hub_reports_runs = False
                response = self._http.post(self._url("/sync", connection), headers=headers, json=payload)
            response.raise_for_status()
            # 重新配对会替换 self.connection；旧凭证的迟到成功响应不得写入新连接的任务与状态。
            if self.connection is not connection:
                return set()
            sync = response.json()
            self._hub_reports_runs = sync.get("supports_run_summary") is True
            self._hub_reports_software = sync.get("supports_software_status") is True
            self._hub_supports_history = sync.get("supports_run_history") is True
            self._hub_supports_project_scope = sync.get("supports_project_scope") is True
            connection["supports_project_scope"] = self._hub_supports_project_scope
            with self._state_lock:
                if self.connection is connection:
                    self._persist_connection()
            target = sync.get("update_target")
            self.hub_update_target = target if isinstance(target, dict) else None
            active = sync["assignments"]
            name = sync.get("worker_name")
            if isinstance(name, str) and name:
                with self._state_lock:
                    if self.connection is connection:
                        self.connection["name"] = name
        active_ids = {row["id"] for row in active}
        # 身份判定与最终落库在同一把锁内完成：重新配对不能插在“确认仍是本次凭证”
        # 与“写入任务/状态”之间，否则旧响应会把任务与状态写进新连接。
        with self._state_lock:
            if self.connection is not connection:
                return set()
            with self.control_lock:
                self.prepare_results()
                self._store_shared_state(sync, worker_id)
                with self.store.connect() as db:
                    local = {row["id"] for row in db.execute(
                        "SELECT id FROM worker_assignments WHERE worker_id='' OR worker_id=?", (worker_id,))}
                    db.executemany("UPDATE worker_assignments SET status='reclaimed' WHERE id=? AND status='ready'",
                                   ((row_id,) for row_id in local - active_ids))
            self.online = True
            self._consecutive_failures = 0
            self._last_heartbeat_mono = time.monotonic()
            self._last_connected_at = utc_stamp()
        for assigned in active:
            if self._stop.is_set() or self.connection is not connection:
                break
            self._send_keepalive_if_due(connection)
            try:
                self._ensure_source(assigned, connection)
            except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
                self.error = str(exc)[:200]
        return active_ids

    def _send_keepalive_if_due(self, connection: dict) -> None:
        """大批量上传结果或下载二维码期间穿插轻量心跳，避免 Hub 侧超过离线窗口误判掉线。"""
        if self._stop.is_set() or self.connection is not connection or connection.get("invalid"):
            return
        if time.monotonic() - self._last_heartbeat_mono < HEARTBEAT_KEEPALIVE_SECONDS:
            return
        worker_id = str(connection.get("id") or "")
        try:
            with self.store.connect() as db:
                pending = db.execute(
                    "SELECT COUNT(*) FROM worker_assignments WHERE status='pending_upload'"
                    " AND (worker_id='' OR worker_id=?)", (worker_id,)).fetchone()[0]
            response = self._http.post(
                self._url("/sync", connection),
                headers=self._capability_headers(connection),
                json={"ready_ids": [], "pending_uploads": pending},
                timeout=10,
            )
            response.raise_for_status()
            if self.connection is connection:
                self._last_heartbeat_mono = time.monotonic()
                self._last_connected_at = utc_stamp()
        except (httpx.HTTPError, OSError, ValueError):
            # 保活穿插失败不中断当前传输，交由下一轮主 poll 统一判定连接状态。
            pass

    def _sync_run_history(self, connection: dict | None = None) -> None:
        connection = connection or self.connection
        if not self._hub_supports_history:
            return
        # 轮次同步失败不影响分配下载和截图补传，待下一次轮询继续。
        try:
            report = self.history.pending_report(
                include_project_scope=(connection.get("supports_project_scope") is True),
                worker_id=str(connection.get("id") or ""),
            )
            if report is None:
                return
            response = self._http.put(self._url("/run-history", connection),
                                      headers=self._capability_headers(connection), json=report, timeout=15)
            response.raise_for_status()
            if self.connection is not connection:
                return
            if response.json().get("acknowledged") != report["generation"]:
                raise ValueError("接收端未确认轮次")
            self.history.acknowledge(report["run"]["id"], report["generation"])
            self.history_error = ""
        except (httpx.HTTPError, OSError, ValueError, KeyError):
            self.history_error = "采集轮次历史尚未同步，稍后自动重试"

    def _ensure_source(self, assigned: dict, connection: dict) -> None:
        """保证活动分配在本机有二维码文件，并把任务来源路径纠正到本分配缓存目录。"""
        worker_id = str(connection.get("id") or "")
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
            self._download(assigned, connection)
        if needs_path_repair:
            # _download 遇到同店仍在执行的旧轮次会暂缓登记。此时保持旧任务来源不变。
            # 同时要求这仍是最新的 ready assignment，且归属本次凭证的设备，
            # 避免旧身份的迟到心跳改写新设备任务的来源路径。
            with self.control_lock, self.store.connect() as db:
                db.execute(
                    "UPDATE tasks SET source_path=? WHERE id=? AND EXISTS ("
                    "SELECT 1 FROM worker_assignments a WHERE a.id=? AND a.task_id=tasks.id "
                    "AND a.status='ready' AND (a.worker_id='' OR a.worker_id=?) "
                    "AND a.rowid=(SELECT MAX(b.rowid) "
                    "FROM worker_assignments b WHERE b.task_id=a.task_id))",
                    (str(path), assigned["task_id"], assigned["id"], worker_id),
                )

    @staticmethod
    def _validated_project(row: dict) -> dict:
        if (not isinstance(row.get("id"), str) or not row["id"].strip() or len(row["id"]) > 128
                or not isinstance(row.get("code"), str) or not row["code"].strip() or len(row["code"]) > 48
                or not isinstance(row.get("name"), str) or not row["name"].strip() or len(row["name"]) > 80
                or not isinstance(row.get("archived_at", ""), str)):
            raise ValueError("后台返回了无效的项目目录")
        return {"id": row["id"].strip(), "code": row["code"].strip(), "name": row["name"].strip(),
                "archived_at": row.get("archived_at", "")}

    def _store_shared_state(self, sync: dict, worker_id: str) -> None:
        """落库共享端下发的状态与已协商的项目元数据，所有项目缓存绑定配对设备身份。"""
        stamp = now()
        batches = [row for row in sync.get("batches") or []
                   if isinstance(row, dict) and row.get("batch_id") and row.get("snapshot_id")]
        assignments = [row for row in sync.get("assignments") or [] if isinstance(row, dict)]
        projects: dict[str, dict] = {}
        batch_projects: dict[str, str] = {}
        project_scope = sync.get("supports_project_scope") is True
        if project_scope:
            for raw in sync.get("projects") or []:
                if not isinstance(raw, dict):
                    raise ValueError("后台返回了无效的项目目录")
                project = self._validated_project(raw)
                projects[project["id"]] = project
            for assignment in assignments:
                project = self._validated_project({
                    "id": assignment.get("project_id"), "code": assignment.get("project_code"),
                    "name": assignment.get("project_name"),
                })
                previous = projects.get(project["id"])
                if previous and previous["code"] != project["code"]:
                    raise ValueError("项目目录与分配快照中的 code 不一致")
                projects.setdefault(project["id"], project)
                batch_id = str(assignment.get("batch_id") or "")
                if not batch_id:
                    raise ValueError("项目分配缺少批次身份")
                if batch_id in batch_projects and batch_projects[batch_id] != project["id"]:
                    raise ValueError("后台把同一批次分配到了不同项目")
                batch_projects[batch_id] = project["id"]
            for batch in batches:
                project_id = str(batch.get("project_id") or batch_projects.get(str(batch["batch_id"])) or "")
                if not project_id:
                    raise ValueError("项目范围同步的批次缺少归属项目")
                if batch["batch_id"] in batch_projects and batch_projects[batch["batch_id"]] != project_id:
                    raise ValueError("后台批次与分配的项目身份不一致")
                batch_projects[batch["batch_id"]] = project_id
                if project_id not in projects:
                    raise ValueError("项目范围同步缺少批次所属项目元数据")
        elif any(any(row.get(key) not in (None, "") for key in ("project_id", "project_code", "project_name"))
                 for row in assignments):
            raise ValueError("旧 Hub 在未协商项目能力时返回了项目字段")
        states = {str(key): value for key, value in (sync.get("states") or {}).items()
                  if isinstance(value, dict)}
        with self.store.connect() as db:
            if project_scope:
                for batch_id, project_id in batch_projects.items():
                    existing = db.execute(
                        "SELECT project_id FROM worker_project_batches WHERE worker_id=? AND batch_id=?",
                        (worker_id, batch_id)).fetchone()
                    if existing and existing["project_id"] != project_id:
                        raise ValueError("Hub 批次归属与本机已缓存项目不一致")
                    known = {row["project_id"] for row in db.execute(
                        "SELECT DISTINCT project_id FROM worker_assignments WHERE worker_id=? AND batch_id=?"
                        " AND project_id<>''", (worker_id, batch_id))}
                    if known - {project_id}:
                        raise ValueError("Hub 批次归属与本机分配历史不一致")
                for assignment in assignments:
                    project_id = str(assignment["project_id"])
                    store_id = str(assignment.get("store_id") or assignment.get("task_id") or "")
                    existing = db.execute(
                        "SELECT task_id,worker_id,project_id,store_id FROM worker_assignments WHERE id=?",
                        (assignment["id"],)).fetchone()
                    if not existing:
                        continue
                    if (existing["task_id"] != assignment["task_id"]
                            or existing["worker_id"] not in ("", worker_id)
                            or existing["project_id"] not in ("", project_id)
                            or existing["store_id"] not in ("", store_id)):
                        raise ValueError("后台分配与本机已保存身份不一致")
                    # 仅以本次认证同步确认的同一 assignment 补齐旧 Worker 缺少的项目快照。
                    db.execute("UPDATE worker_assignments SET worker_id=?,project_id=?,project_code=?,project_name=?,store_id=? "
                               "WHERE id=? AND worker_id IN ('',?) AND project_id IN ('',?)",
                               (worker_id, project_id, assignment["project_code"], assignment["project_name"], store_id,
                                assignment["id"], worker_id, project_id))
                db.executemany(
                    "INSERT INTO worker_project_batches(worker_id,batch_id,project_id,snapshot_id,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(worker_id,batch_id) DO UPDATE SET "
                    "snapshot_id=excluded.snapshot_id,updated_at=excluded.updated_at",
                    ((worker_id, row["batch_id"], batch_projects[row["batch_id"]], row["snapshot_id"], stamp)
                     for row in batches))
                db.executemany(
                    "INSERT INTO worker_project_catalogue(worker_id,project_id,code,name,archived_at,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(worker_id,project_id) DO UPDATE SET "
                    "code=excluded.code,name=excluded.name,archived_at=excluded.archived_at,updated_at=excluded.updated_at",
                    ((worker_id, project["id"], project["code"], project["name"],
                      project["archived_at"], stamp) for project in projects.values()))
            elif batches:
                db.executemany(
                    "INSERT INTO worker_batches(batch_id,snapshot_id,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(batch_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,"
                    "updated_at=excluded.updated_at",
                    ((row["batch_id"], row["snapshot_id"], stamp) for row in batches))
            if states:
                if project_scope:
                    db.executemany(
                        "UPDATE worker_assignments SET shared_state=?,shared_reject=? "
                        "WHERE task_id=? AND worker_id=?",
                        ((value.get("state", ""), value.get("reject_reason", ""), task_id, worker_id)
                         for task_id, value in states.items()))
                else:
                    db.executemany(
                        "UPDATE worker_assignments SET shared_state=?,shared_reject=? WHERE task_id=?",
                        ((value.get("state", ""), value.get("reject_reason", ""), task_id)
                         for task_id, value in states.items()))

    def _upload_pending(self, connection: dict | None = None) -> None:
        connection = connection or self.connection
        worker_id = str(connection.get("id") or "")
        with self.store.connect() as db:
            pending = [row["id"] for row in db.execute(
                "SELECT id FROM worker_assignments WHERE status='pending_upload'"
                " AND (worker_id='' OR worker_id=?)", (worker_id,))]
        for assignment_id in pending:
            if self._stop.is_set() or self.connection is not connection:
                return
            self._send_keepalive_if_due(connection)
            path = self.outbox / (assignment_id + ".zip")
            try:
                with path.open("rb") as stream:
                    response = self._http.put(self._url(f"/assignments/{assignment_id}/result", connection),
                                              headers=self._capability_headers(connection) |
                                              {"Content-Type": "application/zip",
                                               "Content-Length": str(path.stat().st_size)},
                                              content=iter(lambda: stream.read(1024 * 1024), b""))
                response.raise_for_status()
                if self.connection is not connection:
                    return
                disposition = response.json()["disposition"]
                if disposition not in {"accepted", "history"}:
                    raise ValueError("后台未确认此结果")
            except (httpx.HTTPStatusError, OSError, ValueError, KeyError) as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    response = exc.response
                    if response.status_code >= 500:
                        raise
                    if response.status_code == 401:
                        try:
                            detail = response.json().get("detail")
                        except (ValueError, AttributeError):
                            detail = None
                        # 凭证失效时保留整个补传队列，避免逐包重复发送。
                        if detail in ("采集端凭证无效", "采集电脑已解除配对"):
                            raise
                        if detail == "分配不属于此采集端":
                            # 旧设备身份下的结果永远无法用新凭证补传：停止重试并保留压缩包。
                            with self.store.connect() as db:
                                db.execute("UPDATE worker_assignments SET status='reclaimed' WHERE id=?",
                                           (assignment_id,))
                            self.error = ("有采集结果属于旧设备身份，无法用当前凭证补传；"
                                          "压缩包已保留在本机，请联系管理员处理")
                            continue
                self.error = "有采集结果尚未同步，请检查后台或本机磁盘空间"
                continue
            with self.store.connect() as db:
                db.execute("UPDATE worker_assignments SET status='completed',disposition=? WHERE id=?",
                           (disposition, assignment_id))
            path.unlink(missing_ok=True)

    def poll(self) -> None:
        """先保存本机结果，网络不可用时仍按本机队列执行。"""
        # 记录本次轮询使用的凭证对象：重新配对会替换 self.connection，
        # 旧凭证请求的成功或失败响应都不能改动新连接的凭证、任务与状态。
        connection = self.connection
        try:
            self.error = ""
            with self.control_lock:
                self.prepare_results()
            # 先发核心心跳保活，再上传大体积截图包，避免连续上传阻塞心跳导致 Hub 侧误判离线。
            self._heartbeat(connection)
            if self.connection is not connection:
                return
            if not connection.get("invalid"):
                self._upload_pending(connection)
            if self.connection is not connection:
                return
            self._sync_run_history(connection)
            if self.connection is not connection:
                return
            with self._state_lock:
                if self.connection is connection and self.connection.pop("invalid", None):
                    try:
                        atomic_json(self.credentials, self.connection)
                    except OSError:
                        pass
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            # 失败路径同样要确认凭证未被替换，旧凭证的 401 不能把新连接标记为失效。
            with self._state_lock:
                if self.connection is not connection:
                    return
                self.online = False
                self._consecutive_failures += 1
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
