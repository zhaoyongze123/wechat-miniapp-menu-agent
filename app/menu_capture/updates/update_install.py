"""Windows 安装入口：授权、空闲闸门与受控安装事务。

设计边界：
- 真正的包选择、差分重建、下载与应用由 Velopack SDK 完成，本模块不再自带安装器/版本指针；
- 本模块只负责业务安全：目标版本一致性、空闲校验、一次授权消费、事务互斥与状态落盘；
- 安装前必须准备可信旧版恢复包；无法核验或无法留存时拒绝自动安装；
- 应用更新会替换整个 `current` 目录，因此不能由运行中的业务进程自行结束自己：
  控制器只登记事务，由外部恢复协调器与启动器在确认业务空闲后完成停止、应用与恢复。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from menu_capture.updates.install_transaction import ACTIVE, InstallTransaction, TransactionError
from menu_capture.updates.velopack_identity import require_publisher_trust

# 超时仅作为诊断提示，不作为解除安装互斥的证据。
MAX_ACTIVE_SECONDS = 900
ACTIVE_STATUSES = ACTIVE
STALLED_MESSAGE = "安装长时间无进展，保留业务闸门，请检查外部恢复协调器"


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


def release_commit(app_root: Path | None = None) -> str:
    """读取当前运行目录的发布 commit；缺失时返回空串，由调用方拒绝安装。"""
    root = Path(app_root) if app_root is not None else Path(__file__).resolve().parents[3]
    payload = _read_json(root / "release-manifest.json", {})
    return str(payload.get("git_commit") or "").strip() if isinstance(payload, dict) else ""


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
    """服务端安装入口：授权、状态机与事务登记；不启动自研安装进程。"""

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
        current_commit: Callable[[], str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.backend = backend
        self.latest_provider = latest_provider
        self.current_version = current_version
        self.current_commit = current_commit or release_commit
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
        # 锁外准备阶段（核验包与生成数据库基线快照）可能持续数秒；
        # 暴露该状态让 /api/updates/status 立即显示“正在准备安装”，避免界面停留在“等待空闲”。
        self._preparing_version = ""
        self._preparing_message = ""

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
            preparing_version = self._preparing_version
            preparing_message = self._preparing_message
        state = journal.get("status", "idle")
        # UI 继续使用既有阶段；原始事务阶段单独提供，绝不把恢复阻断映射成可重试失败。
        status = {"prepared": "restarting", "stopping": "restarting",
                  "applying": "installing", "recovery_required": "verifying"}.get(state, state)
        payload = {
            "supported": self.supported,
            "reason": "" if self.supported else "外部恢复协调器与 Windows 原生验收尚未完成，安装保持关闭",
            "active": state in ACTIVE,
            "stalled": stalled,
            **journal,
            "transaction_status": state,
            "status": status,
        }
        if preparing_version and state not in ACTIVE:
            # 事务尚未消费授权，保持 status="idle" 允许随时取消，但透出准备进度给 flow 展示。
            payload["preparing"] = True
            payload["preparing_version"] = preparing_version
            payload["preparing_message"] = preparing_message or "正在准备数据库基线与安装事务"
            if not payload.get("target_version"):
                payload["target_version"] = preparing_version
        if stalled and state != "recovery_required":
            payload["message"] = STALLED_MESSAGE
        return payload

    def recover(self) -> None:
        # 只有外部恢复协调器能根据进程、包与健康证据完成事务。
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

    def authorize_recovery_shutdown(self, transaction_id: str, target_version: str) -> None:
        """只允许持有协调令牌的本机启动器在候选版本验证阶段请求有序关闭。"""
        with self._lock:
            journal = self.transaction.read()
            if (not transaction_id or journal.get("transaction_id") != transaction_id
                    or journal.get("status") != "verifying"
                    or journal.get("target_version") != target_version):
                raise ValueError("恢复关闭请求不对应当前验证事务")
            idle, reason = self.idle_check()
            if not idle:
                raise ValueError(reason or "采集或标注仍在执行，不能为恢复关闭")

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
        previous_commit = str(self.current_commit() or "").strip()
        if not previous or not previous_commit:
            raise ValueError("无法确定当前运行版本或 commit，拒绝自动安装")
        from menu_capture.updates.updates import Version

        target = Version.parse(version)
        if target <= Version.parse(previous):
            raise ValueError("目标版本必须高于当前版本")
        channel = "preview" if target.is_prerelease else "stable"
        extension = latest.get("velopack")
        if not isinstance(extension, dict) or extension.get("version") != version:
            raise ValueError("缺少已验证的发布目标")
        require_publisher_trust(extension)

        # 锁外完成网络与长哈希：SDK 待安装校验、可信旧包定位/复制/复验、数据库基线快照。
        # 若把大库备份与哈希放在 self._lock 内，会阻塞 /api/updates/status 轮询导致界面假死。
        idle, reason = self.idle_check()
        if not idle:
            raise ValueError(reason or "采集或标注仍在执行，暂不能安装")
        with self._lock:
            if self._preparing_version or self.active_nonblocking():
                raise ValueError("已有安装任务正在执行")
            self._preparing_version = version
            self._preparing_message = "正在核验安装包与旧版恢复包"
        try:
            try:
                pending = self.backend.verify_pending(extension, channel)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"读取待安装更新失败：{exc}") from exc
            if not pending or str(pending.get("version")) != version:
                raise ValueError("更新包尚未下载完成或版本不一致，请先下载并校验")
            try:
                recovery = self.backend.prepare_recovery_package(previous, previous_commit, extension)
            except Exception as exc:  # noqa: BLE001 - 恢复能力不可用必须拒绝，不能先开始再找退路
                raise ValueError(f"无法准备可信旧版恢复包：{exc}") from exc

            schema = schema_fingerprint(self.data_dir)
            if schema is None:
                raise ValueError("无法读取有效数据库结构，拒绝自动安装")
            from menu_capture.updates import recovery as recovery_probe

            with self._lock:
                self._preparing_message = "正在生成数据库一致性基线快照"
            snapshot_path = (self.data_dir / "recovery"
                             / f"baseline-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}.sqlite3")
            snapshot = recovery_probe.snapshot_database(self.data_dir, snapshot_path)

            created = False
            try:
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
                        # 恢复包与数据库快照已在锁外落盘并复验；锁内只确认身份未变并登记事务。
                        self.backend.require_recovery_package(recovery)
                        self.transaction.create(
                            previous_version=previous, previous_commit=previous_commit,
                            target_version=version, target_commit=extension["git_commit"],
                            channel=channel,
                            schema_baseline={"version": previous, "schema": schema, "snapshot": snapshot},
                            target_asset=dict(extension), recovery_asset=dict(recovery),
                            authorization={"claimed_at": _utc_now(), "claimed_by": self.role}
                            if claimed else {},
                        )
                        created = True
                    except BaseException:
                        self._release_claim(claimed, version)
                        raise
                    finally:
                        if acquired_fs:
                            filesystem_lock.release()
            finally:
                if not created:
                    for suffix in ("", "-wal", "-shm", "-journal"):
                        Path(f"{snapshot_path}{suffix}").unlink(missing_ok=True)
        finally:
            with self._lock:
                self._preparing_version = ""
                self._preparing_message = ""
        return self.status()

    def _release_claim(self, claimed: bool, version: str) -> None:
        if claimed and self.authorization_release is not None:
            self.authorization_release(version)
