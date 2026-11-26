"""Windows 更新适配层：薄封装 Velopack 官方 Python SDK。

职责边界：
- 只复用 SDK 的版本发现、差分下载、包重建与安装/重启能力；
- 授权、空闲闸门、业务状态、审计与界面仍由 `UpdateService` / `InstallController` 负责；
- SDK 的 `download_updates` 没有取消令牌，取消通过独立下载子进程终止实现；
- 本模块在非 Windows 或未安装 `velopack` 时可导入但不启用，不能影响 Hub/单机模式。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from menu_capture.updates.velopack_identity import (
    inner_release_manifest, require_full_file, require_publisher_trust, require_target,
    sdk_asset_identity, validate_extension, validate_local_asset,
)
from menu_capture.updates.velopack_trust import release_policy_from_manifest

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
DOWNLOAD_PROGRESS = "update-download-progress.json"
DOWNLOAD_TIMEOUT_SECONDS = 1800
_VERSION_TEXT_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


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

    def _recovery_root(self) -> Path:
        # 恢复包必须放在 current/packages 之外，避免版本清理删除唯一退路。
        return self.data_dir / "recovery"

    # ---------- 可信恢复包 ----------

    @staticmethod
    def _http_open(url: str):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(url, timeout=120)

    def fetch_release_manifest(self, version: str, channel: str) -> dict:
        url = (f"https://github.com/{self.repository}/releases/download/"
               f"v{version}/update-manifest.json")
        try:
            with self._http_open(url) as response:
                body = response.read(2 * 1024 * 1024 + 1)
        except Exception as exc:  # noqa: BLE001 - 网络错误统一呈现为不可验证恢复来源
            raise VelopackError(f"无法读取旧版正式发布清单：{exc}") from exc
        if len(body) > 2 * 1024 * 1024:
            raise VelopackError("旧版正式发布清单超出大小上限")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise VelopackError("旧版正式发布清单无法解析") from exc
        if not isinstance(payload, dict):
            raise VelopackError("旧版正式发布清单格式无效")
        if (str(payload.get("version") or "") != version
                or str(payload.get("channel") or "") != channel):
            raise VelopackError("旧版正式发布清单版本或渠道不一致")
        return payload

    def _download_verified(self, asset: dict, destination: Path) -> Path:
        partial = destination.with_suffix(destination.suffix + ".partial")
        partial.unlink(missing_ok=True)
        try:
            with self._http_open(asset["url"]) as response, partial.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
                    if partial.stat().st_size > asset["size"]:
                        raise VelopackError("恢复包下载超出声明大小")
                stream.flush()
                os.fsync(stream.fileno())
            if partial.stat().st_size != asset["size"]:
                raise VelopackError("恢复包下载大小不一致")
            with partial.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != asset["sha256"]:
                    raise VelopackError("恢复包下载 SHA-256 不一致")
            os.replace(partial, destination)
            return destination
        finally:
            partial.unlink(missing_ok=True)

    def prepare_recovery_package(self, previous_version: str, previous_commit: str,
                                 target: dict, *, previous_channel: str | None = None) -> dict:
        """从上一版正式 Release 核验并复制旧 full 包；使用旧版实际发布渠道而非目标渠道。"""
        if previous_channel is not None:
            channel = str(previous_channel).strip().lower()
        else:
            from menu_capture.updates.updates import Version

            channel = "preview" if Version.parse(previous_version).is_prerelease else "stable"
        if channel not in {"stable", "preview"}:
            raise VelopackError("旧版恢复包发布渠道无效")
        manifest = self.fetch_release_manifest(previous_version, channel)
        if str(manifest.get("git_commit") or "") != previous_commit:
            raise VelopackError("旧版正式发布 commit 与当前运行版本不一致")
        raw = manifest.get("velopack")
        if not isinstance(raw, dict):
            raise VelopackError("旧版正式发布缺少 Velopack 扩展")
        extension = validate_extension(
            raw, version=previous_version, commit=previous_commit, channel=channel,
            repository=self.repository, tag=f"v{previous_version}",
            release_policy=release_policy_from_manifest(manifest),
        )
        require_publisher_trust(extension)
        full = extension["full"]
        local = self._packages_dir() / full["name"]
        if local.is_file() and local.stat().st_size == full["size"]:
            with local.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == full["sha256"]:
                    source = {"type": "local-packages", "url": full["url"],
                              "tag": f"v{previous_version}"}
                    package = local
                else:
                    package = None
        else:
            package = None
        if package is None:
            recovery_tmp = self._recovery_root() / ".download"
            recovery_tmp.mkdir(parents=True, exist_ok=True)
            package = self._download_verified(full, recovery_tmp / full["name"])
            source = {"type": "official-release", "url": full["url"],
                      "tag": f"v{previous_version}"}
        inner = inner_release_manifest(package)
        if (str(inner.get("version") or "") != previous_version
                or str(inner.get("git_commit") or "") != previous_commit):
            raise VelopackError("恢复包内部发布清单与正式发布身份不一致")
        destination_dir = self._recovery_root() / f"{previous_version}-{full['sha256'][:12]}"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / full["name"]
        if not destination.is_file() or destination.stat().st_size != full["size"]:
            temp = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(package, temp)
            os.replace(temp, destination)
        with destination.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != full["sha256"]:
                raise VelopackError("恢复包复制后 SHA-256 不一致")
        try:
            os.chmod(destination, 0o444)
        except OSError:
            pass
        return {
            "version": previous_version, "commit": previous_commit,
            "channel": channel,
            "name": full["name"], "size": full["size"], "sha256": full["sha256"],
            "path": str(destination), "source": source,
            "inner_manifest": {
                "version": str(inner.get("version") or ""),
                "commit": str(inner.get("git_commit") or ""),
                "frontend_entry": str(inner.get("frontend_entry") or ""),
                "frontend_sha256": str(inner.get("frontend_sha256") or ""),
            },
        }

    def require_recovery_package(self, asset: dict) -> Path:
        validate_local_asset(asset, version=asset.get("version"), commit=asset.get("commit"))
        root = self._recovery_root().resolve()
        path = Path(asset["path"])
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise VelopackError("恢复包路径不在专属恢复目录") from exc
        if path.is_symlink() or not resolved.is_file() or resolved.stat().st_size != asset["size"]:
            raise VelopackError("恢复包不存在或大小不一致")
        return resolved

    def verify_recovery_package(self, asset: dict) -> Path:
        path = self.require_recovery_package(asset)
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != asset["sha256"]:
                raise VelopackError("恢复包 SHA-256 不一致")
        inner = inner_release_manifest(path)
        if (str(inner.get("version") or "") != asset["version"]
                or str(inner.get("git_commit") or "") != asset["commit"]):
            raise VelopackError("恢复包内部发布清单已变化")
        return path

    def rollback(self, asset: dict, *, channel: str | None = None) -> None:
        """仅供外部恢复协调器在复核数据库与包后调用；使用官方 updater 与旧包实际渠道应用旧 full 包。"""
        if not self.available():
            raise VelopackError("当前不是 Velopack 安装环境")
        package = self.verify_recovery_package(asset)
        recovery_channel = str(asset.get("channel") or "").strip().lower()
        if channel is not None and str(channel).strip().lower() != recovery_channel:
            raise VelopackError("回滚渠道与恢复包实际发布渠道不一致")
        # 官方 updater 从 packages 目录按文件名取包；先复制并再次校验。
        target_path = self._packages_dir() / asset["name"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp = target_path.with_suffix(target_path.suffix + ".tmp")
        shutil.copyfile(package, temp)
        os.replace(temp, target_path)
        with target_path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != asset["sha256"]:
                raise VelopackError("回滚包复制后 SHA-256 不一致")
        import velopack

        old = velopack.VelopackAsset(
            PackageId=PACK_ID, Version=asset["version"], Type="Full",
            FileName=asset["name"], SHA1="", SHA256=asset["sha256"],
            Size=int(asset["size"]), NotesMarkdown="", NotesHtml="",
        )
        self.manager(recovery_channel, str(asset.get("version") or "")).wait_exit_then_apply_updates(
            old, silent=True, restart=True, restart_args=None,
        )

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

    def _static_base_url(self, version: str | None) -> str:
        """正式渠道静态源：固定到已验证版本目录，避免下载期间新发布导致索引与包不一致。"""
        prefix = f"https://github.com/{self.repository}/releases/"
        if not version:
            # 仅本地操作（读取待安装包、应用已下载包）会省略版本；这些操作不访问网络。
            return prefix + "latest/download/"
        if not _VERSION_TEXT_RE.fullmatch(str(version)):
            raise VelopackError("更新目标版本号无效")
        return f"{prefix}download/v{version}/"

    def manager(self, channel: str | None = None, version: str | None = None):
        import velopack

        selected = channel or self.channel
        if selected == "preview":
            # 预览渠道包含预发布版本，只能走 Release 列表发现；正式渠道不依赖 REST API。
            source = velopack.GithubSource(
                f"https://github.com/{self.repository}",
                None,
                True,
            )
        else:
            source = velopack.HttpSource(self._static_base_url(version))
        # AllowVersionDowngrade 保持关闭：降级只用于显式回滚，不参与日常更新检查。
        options = velopack.UpdateOptions(False, 10, channel_name(selected))
        return velopack.UpdateManager(source, options, self.locator())

    # ---------- 版本发现与下载 ----------

    def check(self, channel: str | None = None, version: str | None = None) -> dict | None:
        """返回 SDK 选中的目标版本；没有任何适用更新时返回 None。"""
        try:
            info = self.manager(channel, version).check_for_updates()
        except Exception as exc:  # noqa: BLE001 - 网络/解析失败统一呈现为检查失败
            raise VelopackError(f"Velopack 检查更新失败：{exc}") from exc
        if info is None:
            return None
        asset = info.TargetFullRelease
        release = {
            **sdk_asset_identity(asset),
            "notes": str(asset.NotesMarkdown or ""),
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
        return sdk_asset_identity(asset)

    def verify_pending(self, target: dict, channel: str) -> dict:
        require_publisher_trust(target)
        pending = self.pending_restart(channel)
        require_target(pending, target)
        require_full_file(self._packages_dir(), target)
        return pending

    def download(
        self,
        version: str,
        *,
        channel: str | None = None,
        target: dict | None = None,
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
            if not isinstance(target, dict) or target.get("version") != version:
                return {"status": "failed", "message": "缺少已验证的下载目标"}
            try:
                require_publisher_trust(target)
            except ValueError as exc:
                return {"status": "failed", "message": str(exc)}
            progress_path = self.progress_path.with_name(
                f"{self.progress_path.stem}-{uuid.uuid4().hex}.json"
            )
            try:
                return self._download(version, channel, target, progress, cancel, timeout, progress_path)
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

    def _download(self, version, channel, target, progress, cancel, timeout, progress_path: Path) -> dict:
        selected = channel or self.channel
        command = [
            self.python, "-m", "menu_capture.updates.velopack_worker",
            "--repo", self.repository,
            "--channel", selected,
            "--version", str(version),
            "--progress", str(progress_path),
            "--target-json", json.dumps(target, ensure_ascii=False, separators=(",", ":")),
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
            try:
                self.verify_pending(target, selected)
            except Exception as exc:
                return {"status": "failed", "message": str(exc)}
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

    def apply_pending(self, target: dict, *, channel: str) -> None:
        """仅供独立恢复协调器完成所有事务门禁后调用；此处再次核验实际包。"""
        if not self.available():
            raise VelopackError("当前不是 Velopack 安装环境")
        require_publisher_trust(target)
        manager = self.manager(channel, str(target.get("version") or ""))
        asset = manager.get_update_pending_restart()
        if asset is None:
            raise VelopackError("没有已下载完成的更新包")
        require_target(sdk_asset_identity(asset), target)
        require_full_file(self._packages_dir(), target)
        # 启动器在调用后必须走正常退出路径，由官方 Update.exe 完成应用与重启。
        manager.wait_exit_then_apply_updates(
            asset, silent=True, restart=True, restart_args=None,
        )
