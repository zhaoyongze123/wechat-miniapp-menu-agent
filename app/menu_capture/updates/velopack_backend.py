"""Windows 更新适配层：薄封装 Velopack 官方 Python SDK。

职责边界：
- 只复用 SDK 的版本发现、差分下载、包重建与安装/重启能力；
- 授权、空闲闸门、业务状态、审计与界面仍由 `UpdateService` / `InstallController` 负责；
- SDK 的 `download_updates` 没有取消令牌，取消通过独立下载子进程终止实现；
- 本模块在非 Windows 或未安装 `velopack` 时可导入但不启用，不能影响 Hub/单机模式。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

# 与 vpk 打包参数保持一致：独立安装目录，避免与旧 Inno Setup 安装目录及默认数据目录混淆。
PACK_ID = "Yipu.MenuCapture"
VELOPACK_VERSION = "1.2.161"
# 通道编码平台、架构与发布渠道，避免不同平台/架构互相看到对方资产。
CHANNEL_PREFIX = "win-x64-"
DEFAULT_CHANNEL = "stable"
# 启动器与工作进程通过环境变量共享安装布局，避免依赖 SDK 从 python.exe 反推安装根。
ENV_ROOT = "MENU_CAPTURE_VELOPACK_ROOT"
ENV_CURRENT = "MENU_CAPTURE_VELOPACK_CURRENT"
ENV_PACKAGES = "MENU_CAPTURE_VELOPACK_PACKAGES"
APPLY_REQUEST = "update-apply-request.json"
APPLY_STATE = "update-apply-state.json"
DOWNLOAD_PROGRESS = "update-download-progress.json"
DOWNLOAD_TIMEOUT_SECONDS = 1800


class VelopackError(RuntimeError):
    """适配层无法完成请求时抛出；调用方应如实展示原因，不回退到自研安装器。"""


def channel_name(channel: str) -> str:
    value = str(channel or DEFAULT_CHANNEL).strip().lower()
    if value not in {"stable", "preview"}:
        raise VelopackError("发布渠道无效")
    return f"{CHANNEL_PREFIX}{value}"


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


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class VelopackBackend:
    """Velopack SDK 的最小适配面；所有方法都不在导入期依赖 Windows 原生模块。"""

    def __init__(
        self,
        repository: str,
        data_dir: Path,
        *,
        channel: str = DEFAULT_CHANNEL,
        python: str | None = None,
        app_dir: Path | None = None,
    ) -> None:
        self.repository = repository
        self.data_dir = Path(data_dir)
        self.channel = channel
        # 下载子进程必须使用打包内的 Python，确保能导入 velopack 原生模块。
        self.python = python or sys.executable
        self.app_dir = Path(app_dir) if app_dir is not None else Path(__file__).resolve().parents[2]
        self.request_path = self.data_dir / APPLY_REQUEST
        self.apply_state_path = self.data_dir / APPLY_STATE
        self.progress_path = self.data_dir / DOWNLOAD_PROGRESS
        self._download_lock = threading.Lock()
        self._download_process: subprocess.Popen | None = None

    # ---------- 环境探测 ----------

    def _current_dir(self) -> Path | None:
        override = os.environ.get(ENV_CURRENT)
        if override:
            path = Path(override)
            return path if path.is_dir() else None
        # 启动器固定位于 current 根；工作进程位于 current/python，逐级向上寻找 sq.version。
        candidate = Path(sys.executable).resolve().parent
        for _ in range(4):
            if (candidate / "sq.version").is_file():
                return candidate
            if candidate.parent == candidate:
                break
            candidate = candidate.parent
        return None

    def _root_dir(self) -> Path | None:
        override = os.environ.get(ENV_ROOT)
        if override:
            path = Path(override)
            return path if path.is_dir() else None
        current = self._current_dir()
        if current is None:
            return None
        root = current.parent
        return root if (root / "Update.exe").is_file() else None

    def _packages_dir(self) -> Path:
        override = os.environ.get(ENV_PACKAGES)
        if override:
            return Path(override)
        root = self._root_dir()
        return (root / "packages") if root is not None else (self.data_dir / "velopack-packages")

    def available(self) -> bool:
        """仅 Windows、已安装 velopack 且能定位安装布局时启用。"""
        if sys.platform != "win32":
            return False
        try:
            import velopack  # noqa: F401
        except Exception:  # noqa: BLE001 - 缺少依赖时按不支持处理
            return False
        return self._root_dir() is not None and self._current_dir() is not None

    def locator(self):
        import velopack

        current = self._current_dir()
        root = self._root_dir()
        if current is None or root is None:
            raise VelopackError("无法定位 Velopack 安装目录")
        return velopack.VelopackLocatorConfig(
            RootAppDir=root,
            UpdateExePath=root / "Update.exe",
            PackagesDir=self._packages_dir(),
            ManifestPath=current / "sq.version",
            CurrentBinaryDir=current,
            IsPortable=False,
        )

    def manager(self, channel: str | None = None):
        import velopack

        selected = channel or self.channel
        source = velopack.GithubSource(
            f"https://github.com/{self.repository}",
            None,
            selected == "preview",
        )
        # AllowVersionDowngrade 保持关闭：降级只用于显式回滚，不参与日常更新检查。
        options = velopack.UpdateOptions(False, 10, channel_name(selected))
        return velopack.UpdateManager(source, options, self.locator())

    # ---------- 版本发现与下载 ----------

    def check(self, channel: str | None = None) -> dict | None:
        """返回 SDK 选中的目标版本；没有任何适用更新时返回 None。"""
        try:
            info = self.manager(channel).check_for_updates()
        except Exception as exc:  # noqa: BLE001 - 网络/解析失败统一呈现为检查失败
            raise VelopackError(f"Velopack 检查更新失败：{exc}") from exc
        if info is None:
            return None
        asset = info.TargetFullRelease
        release = {
            "version": str(asset.Version),
            "notes": str(asset.NotesMarkdown or ""),
            "sha256": str(asset.SHA256 or "").lower(),
            "size": int(asset.Size or 0),
            "is_downgrade": bool(info.IsDowngrade),
            "deltas": len(list(info.DeltasToTarget or [])),
        }
        return release

    def pending_restart(self, channel: str | None = None) -> dict | None:
        """返回已下载并可应用的资产；没有时返回 None。"""
        if not self.available():
            return None
        try:
            asset = self.manager(channel).get_update_pending_restart()
        except Exception as exc:  # noqa: BLE001
            raise VelopackError(f"读取待安装更新失败：{exc}") from exc
        if asset is None:
            return None
        return {
            "version": str(asset.Version),
            "file_name": str(asset.FileName),
            "sha256": str(asset.SHA256 or "").lower(),
            "size": int(asset.Size or 0),
        }

    def download(
        self,
        version: str,
        *,
        channel: str | None = None,
        progress: Callable[[dict], None] | None = None,
        cancel=None,
        timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    ) -> dict:
        """下载互斥；上次子进程未确认退出时禁止开启新的下载。"""
        if not self._download_lock.acquire(blocking=False):
            return {"status": "failed", "message": "已有下载任务正在运行"}
        try:
            previous = self._download_process
            if previous is not None and previous.poll() is None:
                return {"status": "failed", "message": "上次下载进程尚未退出，暂不能重试"}
            self._download_process = None
            progress_path = self.progress_path.with_name(
                f"{self.progress_path.stem}-{uuid.uuid4().hex}.json"
            )
            try:
                return self._download(version, channel, progress, cancel, timeout, progress_path)
            finally:
                process = self._download_process
                if process is None or process.poll() is not None:
                    self._download_process = None
                    try:
                        progress_path.unlink(missing_ok=True)
                        progress_path.with_suffix(progress_path.suffix + ".tmp").unlink(missing_ok=True)
                    except OSError:
                        pass
        finally:
            self._download_lock.release()

    def _download(self, version, channel, progress, cancel, timeout, progress_path: Path) -> dict:
        selected = channel or self.channel
        command = [
            self.python, "-m", "menu_capture.updates.velopack_worker",
            "--repo", self.repository,
            "--channel", selected,
            "--version", str(version),
            "--progress", str(progress_path),
        ]
        _write_json(progress_path, {"stage": "starting", "percent": 0})
        kwargs: dict[str, Any] = {
            "cwd": str(self.app_dir),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            # 独立进程组：终止下载子进程时不会误伤采集端自身。
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
            self._download_process = process
        except OSError as exc:
            _write_json(progress_path, {
                "stage": "failed", "percent": 0, "error": f"无法启动下载进程：{exc}",
            })
            return {"status": "failed", "message": f"无法启动下载进程：{exc}"}
        started = time.monotonic()
        try:
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    if not self._terminate(process):
                        return {"status": "failed", "message": "无法确认下载进程已退出，暂不能重试"}
                    return {"status": "cancelled", "message": "下载已取消，未安装任何内容"}
                if timeout and time.monotonic() - started > timeout:
                    stopped = self._terminate(process)
                    message = "下载超时，已终止下载进程" if stopped else "下载超时，无法确认下载进程已退出"
                    return {"status": "failed", "message": message}
                state = _read_json(progress_path, {})
                if progress is not None and isinstance(state, dict):
                    progress(state)
                time.sleep(0.5)
        finally:
            if process.poll() is None:
                self._terminate(process)
        state = _read_json(progress_path, {})
        if process.returncode == 0 and str(state.get("stage") or "") == "completed":
            return {"status": "completed", "message": f"已下载并校验 {version}"}
        message = str(state.get("error") or "") or f"下载进程退出码 {process.returncode}"
        return {"status": "failed", "message": message}

    @staticmethod
    def _terminate(process: subprocess.Popen) -> bool:
        """只在子进程已退出时返回成功；杀进程后仍需要回收句柄。"""
        if process.poll() is not None:
            return True
        try:
            process.terminate()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return process.poll() is not None
        return process.poll() is not None

    # ---------- 应用更新请求（由启动器执行真正切换） ----------

    def request_apply(self, version: str, *, channel: str | None = None) -> None:
        """只登记应用请求；真正退出、切换与重启由启动器完成，避免自进程被强制结束。"""
        selected = channel or self.channel
        channel_name(selected)
        _write_json(self.request_path, {
            "version": str(version),
            "channel": selected,
            "requested_at": _utc_now(),
            "requested_by_pid": os.getpid(),
        })

    def read_apply_request(self) -> dict | None:
        data = _read_json(self.request_path, None)
        return data if isinstance(data, dict) and data.get("version") else None

    def clear_apply_request(self) -> None:
        try:
            self.request_path.unlink(missing_ok=True)
        except OSError:
            pass

    def read_apply_state(self) -> dict:
        data = _read_json(self.apply_state_path, {})
        return data if isinstance(data, dict) else {}

    def write_apply_state(self, status: str, version: str, message: str = "", error: str = "") -> None:
        _write_json(self.apply_state_path, {
            "status": status,
            "version": str(version),
            "message": message,
            "error": error,
            "updated_at": _utc_now(),
        })

    def apply_pending(self, expected_version: str, *, channel: str | None = None) -> dict:
        """启动器使用同一 Manager 和同一资产对象核对并提交应用。"""
        if not self.available():
            raise VelopackError("当前不是 Velopack 安装环境")
        manager = self.manager(channel)
        asset = manager.get_update_pending_restart()
        if asset is None:
            raise VelopackError("没有已下载完成的更新包")
        if str(asset.Version) != str(expected_version):
            raise VelopackError(
                f"待安装版本 {asset.Version} 与请求版本 {expected_version} 不一致"
            )
        pending = {
            "version": str(asset.Version),
            "file_name": str(asset.FileName),
            "sha256": str(asset.SHA256 or "").lower(),
            "size": int(asset.Size or 0),
        }
        self.write_apply_state("applying", expected_version, "正在应用更新并重启")
        manager.apply_updates_and_restart(asset)
        return pending
