"""Windows 安装入口：授权、空闲闸门与 Velopack 应用请求。

设计边界：
- 真正的包选择、差分重建、下载与应用由 Velopack SDK 完成，本模块不再自带安装器/版本指针；
- 本模块只负责业务安全：目标版本一致性、空闲校验、一次授权消费、事务互斥与状态落盘；
- 应用更新会替换整个 `current` 目录，因此不能由运行中的业务进程自行结束自己：
  控制器只登记应用请求，由启动器在确认业务空闲并正常停止 Worker 后调用 SDK 应用更新。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from menu_capture.updates.install_transaction import ACTIVE, InstallTransaction, TransactionError

# 超时仅作为诊断提示，不作为解除安装互斥的证据。
MAX_ACTIVE_SECONDS = 900
ACTIVE_STATUSES = ACTIVE
STALLED_MESSAGE = "安装长时间无进展，保留业务闸门，请检查更新器及恢复协调器"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def schema_fingerprint(data_dir: Path) -> dict | None:
    """只读记录 SQLite 结构；缺失、损坏或不可读均不得作为可回滚基线。"""
    db_path = Path(data_dir) / "tasks.sqlite3"
    try:
        if not db_path.is_file():
            return None
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            objects = [list(row) for row in db.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            )]
            if not any(row[0] == "table" and not row[1].startswith("sqlite_") for row in objects):
                return None
            user_version = db.execute("PRAGMA user_version").fetchone()[0]
            return {"user_version": user_version, "objects": objects}
    except (OSError, sqlite3.Error, ValueError):
        return None


def rollback_schema_matches(data_dir: Path, baseline: dict, version: str) -> bool:
    """只允许与已记录的旧版本及有效结构快照严格匹配。"""
    if not isinstance(baseline, dict) or baseline.get("version") != version:
        return False
    before = baseline.get("schema")
    after = schema_fingerprint(data_dir)
    return isinstance(before, dict) and after is not None and before == after


def _age_seconds(text: str, fallback: Path | None = None) -> float | None:
    value = str(text or "")
    if value:
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            stamp = None
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    if fallback is not None:
        try:
            return max(0.0, time.time() - fallback.stat().st_mtime)
        except OSError:
            return None
    return None


class InstallController:
    """服务端安装入口：授权、状态机与应用请求；不再启动自研安装进程。"""

    def __init__(
        self,
        *,
        data_dir: Path,
        backend: Any,
        latest_provider: Callable[[], dict],
        current_version: Callable[[], str],
        idle_check: Callable[[], tuple[bool, str]],
        role: str,
        platform: str,
        supported: bool = False,
        authorization_check: Callable[[str], bool] | None = None,
        authorization_release: Callable[[str], None] | None = None,
        filesystem_lock: threading.Lock | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.backend = backend
        self.latest_provider = latest_provider
        self.current_version = current_version
        self.idle_check = idle_check
        self.role = role
        self.platform = platform
        self.transaction = InstallTransaction(self.data_dir)
        self.journal_path = self.transaction.path
        self.authorization_check = authorization_check
        self.authorization_release = authorization_release
        self.filesystem_lock = filesystem_lock
        self._supported = bool(
            supported and role == "worker" and platform == "windows" and backend is not None
        )
        self._lock = threading.RLock()

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def gate(self) -> threading.RLock:
        """与采集/标注启动共享的互斥闸门，闭合空闲检查到写请求之间的竞态窗口。"""
        return self._lock

    def _snapshot(self) -> dict:
        if self.role != "worker":
            return {}
        try:
            return self.transaction.read()
        except TransactionError as exc:
            return {"status": "recovery_required", "message": str(exc), "error": str(exc)}

    def active(self) -> bool:
        with self._lock:
            return self.active_nonblocking()

    def active_nonblocking(self) -> bool:
        return self._snapshot().get("status") in ACTIVE

    def status(self) -> dict:
        with self._lock:
            journal = self._snapshot()
            stalled = self._is_stalled(journal)
        state = journal.get("status", "idle")
        # UI 继续使用既有阶段；原始事务阶段单独提供，绝不把恢复阻断映射成可重试失败。
        status = {"prepared": "restarting", "stopping": "restarting",
                  "applying": "installing", "recovery_required": "verifying"}.get(state, state)
        payload = {
            "supported": self.supported,
            "reason": "" if self.supported else "安全恢复协调器尚未就绪或未显式启用安装",
            "active": state in ACTIVE,
            "stalled": stalled,
            **journal,
            "transaction_status": state,
            "status": status,
        }
        if stalled and state != "recovery_required":
            payload["message"] = STALLED_MESSAGE
        return payload

    def recover(self) -> None:
        # 只有独立恢复协调器能根据进程、包与健康证据完成事务。
        # Worker 启动或 GET 查询不能以版本相等、超时作为成功或失败的依据。
        return None

    def authorize_shutdown(self, transaction_id: str, target_version: str) -> None:
        with self._lock:
            journal = self.transaction.read()
            if (not transaction_id or journal.get("transaction_id") != transaction_id
                    or journal.get("status") != "stopping"
                    or journal.get("target_version") != target_version):
                raise ValueError("关闭请求不对应当前安装事务")
            idle, reason = self.idle_check()
            if not idle:
                raise ValueError(reason or "采集或标注仍在执行，不能关闭")

    def _is_stalled(self, journal: dict) -> bool:
        if str(journal.get("status") or "") not in ACTIVE_STATUSES:
            return False
        age = _age_seconds(str(journal.get("updated_at") or journal.get("started_at") or ""), self.journal_path)
        return age is not None and age > MAX_ACTIVE_SECONDS

    # ---------- 启动安装 ----------

    def start(self, version: str, confirm: str, *, require_authorization: bool = False) -> dict:
        if not self.supported:
            raise ValueError("当前环境未启用自动安装")
        version = str(version or "").strip()
        if not version or version != str(confirm or "").strip():
            raise ValueError("请确认要安装的版本号")
        if self.active():
            raise ValueError("已有安装任务正在执行")
        latest = self.latest_provider()
        if not isinstance(latest, dict) or not latest.get("eligible") or str(latest.get("version") or "") != version:
            raise ValueError("目标版本不是当前可更新的最新版本")
        previous = str(self.current_version() or "").strip()
        if not previous:
            raise ValueError("无法确定当前运行版本，拒绝自动安装")
        from menu_capture.updates.updates import Version

        target = Version.parse(version)
        if target <= Version.parse(previous):
            raise ValueError("目标版本必须高于当前版本")
        channel = "preview" if target.is_prerelease else "stable"
        # 锁外确认 SDK 已下载完成目标版本；下载状态不能只信本地记录。
        try:
            extension = latest.get("velopack")
            if not isinstance(extension, dict) or extension.get("version") != version:
                raise ValueError("缺少已验证的发布目标")
            pending = self.backend.verify_pending(extension, channel)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"读取待安装更新失败：{exc}") from exc
        if not pending or str(pending.get("version")) != version:
            raise ValueError("更新包尚未下载完成或版本不一致，请先下载并校验")

        with self._lock:
            journal = self.transaction.read()
            if journal.get("status") in ACTIVE_STATUSES:
                raise ValueError("已有安装任务正在执行")
            idle, reason = self.idle_check()
            if not idle:
                raise ValueError(reason or "采集或标注仍在执行，暂不能安装")
            claimed = False
            if require_authorization:
                if self.authorization_check is None or not self.authorization_check(version):
                    raise ValueError("更新授权已取消，安装未启动")
                claimed = True
            filesystem_lock = self.filesystem_lock
            acquired_fs = False
            if filesystem_lock is not None:
                if not filesystem_lock.acquire(blocking=False):
                    self._release_claim(claimed, version)
                    raise ValueError("存储清理进行中，请稍后重试")
                acquired_fs = True
            try:
                # 回滚兼容判断基线：记录切换前数据库结构，供显式回滚前比对。
                schema = schema_fingerprint(self.data_dir)
                if schema is None:
                    raise ValueError("无法读取有效数据库结构，拒绝自动安装")
                self.transaction.create(
                    previous_version=previous, target_version=version, channel=channel,
                    schema_baseline=schema, target_asset=dict(extension),
                )
            except BaseException:
                self._release_claim(claimed, version)
                raise
            finally:
                if acquired_fs:
                    filesystem_lock.release()
        return self.status()

    def _release_claim(self, claimed: bool, version: str) -> None:
        if claimed and self.authorization_release is not None:
            self.authorization_release(version)
