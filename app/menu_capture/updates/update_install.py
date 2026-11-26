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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# 应用请求到新版本启动之间的最长等待；超时后如实标记失败，允许用户重试。
MAX_ACTIVE_SECONDS = 900
ACTIVE_STATUSES = {"starting", "restarting", "applying"}
STALLED_MESSAGE = "更新已请求应用，但启动器长时间未完成切换，请检查本机日志后重试"


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


def schema_fingerprint(data_dir: Path) -> list:
    """记录数据库结构，用于回滚前判断新版本是否已经改动过存储。"""
    db_path = Path(data_dir) / "tasks.sqlite3"
    if not db_path.is_file():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )]
            return [
                [table, [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]]
                for table in tables
            ]
    except sqlite3.Error:
        return []


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
        self.journal_path = self.data_dir / "update-install.json"
        self.baseline_path = self.data_dir / "update-schema-baseline.json"
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

    def active(self) -> bool:
        with self._lock:
            self._refresh()
            return str(_read_json(self.journal_path, {}).get("status") or "") in ACTIVE_STATUSES

    def active_nonblocking(self) -> bool:
        return str(_read_json(self.journal_path, {}).get("status") or "") in ACTIVE_STATUSES

    def status(self) -> dict:
        with self._lock:
            self._refresh()
            journal = _read_json(self.journal_path, {})
            stalled = self._is_stalled(journal)
        status = str(journal.get("status") or "idle")
        payload = {
            "supported": self.supported,
            "reason": "" if self.supported else "安装执行器需在 Windows 采集端并显式启用后才能使用",
            "active": status in ACTIVE_STATUSES,
            "stalled": stalled,
            **journal,
        }
        if stalled:
            payload["message"] = STALLED_MESSAGE
        return payload

    def recover(self) -> None:
        with self._lock:
            self._refresh()

    # ---------- 状态收尾 ----------

    def _refresh(self) -> None:
        journal = _read_json(self.journal_path, {})
        if str(journal.get("status") or "") not in ACTIVE_STATUSES:
            return
        target = str(journal.get("target_version") or "")
        try:
            running = str(self.current_version() or "").strip()
        except Exception:  # noqa: BLE001 - 状态探测失败不能拖垮接口
            running = ""
        apply_state = {}
        if self.backend is not None:
            try:
                apply_state = self.backend.read_apply_state()
            except Exception:  # noqa: BLE001
                apply_state = {}
        state = str(apply_state.get("status") or "")
        # 1) 已经运行目标版本：更新成功，哪怕启动器还来不及回写状态。
        if target and running == target:
            self._finish("completed", f"已切换到 {target}")
            return
        # 2) 启动器明确失败：如实转为失败并清理请求。
        if state == "failed":
            self._finish(
                "failed",
                str(apply_state.get("message") or "应用更新失败"),
                str(apply_state.get("error") or ""),
            )
            return
        # 3) 请求未被启动器接手或切换长时间无进展：超时后解除活动态，允许重试。
        age = _age_seconds(str(journal.get("updated_at") or journal.get("started_at") or ""), self.journal_path)
        if age is not None and age > MAX_ACTIVE_SECONDS:
            self._finish("failed", STALLED_MESSAGE, STALLED_MESSAGE)

    def _finish(self, status: str, message: str, error: str = "") -> None:
        journal = _read_json(self.journal_path, {})
        journal.update({
            "status": status,
            "message": message,
            "error": error,
            "finished_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        _write_json(self.journal_path, journal)
        if status == "completed" and self.backend is not None:
            try:
                self.backend.clear_apply_request()
            except Exception:  # noqa: BLE001
                pass

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

        if Version.parse(version) <= Version.parse(previous):
            raise ValueError("目标版本必须高于当前版本")
        # 锁外确认 SDK 已下载完成目标版本；下载状态不能只信本地记录。
        try:
            pending = self.backend.pending_restart()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"读取待安装更新失败：{exc}") from exc
        if not pending or str(pending.get("version")) != version:
            raise ValueError("更新包尚未下载完成或版本不一致，请先下载并校验")

        with self._lock:
            self._refresh()
            journal = _read_json(self.journal_path, {})
            if str(journal.get("status") or "") in ACTIVE_STATUSES:
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
                _write_json(self.baseline_path, {
                    "version": previous,
                    "schema": schema_fingerprint(self.data_dir),
                    "captured_at": _utc_now(),
                })
                self.backend.request_apply(version)
                _write_json(self.journal_path, {
                    "status": "restarting",
                    "target_version": version,
                    "previous_version": previous,
                    "started_at": _utc_now(),
                    "updated_at": _utc_now(),
                    "finished_at": "",
                    "message": "已请求启动器在空闲时应用更新并重启",
                    "error": "",
                })
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
