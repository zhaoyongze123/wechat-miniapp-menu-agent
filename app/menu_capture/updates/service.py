"""应用内更新：受信任发布发现、清单校验、偏好、审计与本地版本清点。

边界：
- 只访问固定仓库的正式 Release，不接受前端传入 URL、命令或版本路径；
- SHA-256 仅用于完整性校验，发布者可信度依赖固定仓库与正式 Release 语义；
- 下载只写入数据目录并做断点续传与哈希校验；安装/切换执行器仍不启用。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import total_ordering
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_REPOSITORY = "zhaoyongze123/wechat-miniapp-menu-agent"
CHANNELS = {"stable", "preview"}
ASSET_KINDS = {"windows_installer", "hub_image"}
ALLOWED_ASSET_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
CACHE_TTL_SECONDS = 3600
FAILURE_BACKOFF_SECONDS = 300
MAX_RELEASE_NOTES = 8000
DOWNLOAD_CHUNK_BYTES = 256 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30.0
# 预留余量：下载期间系统、日志与其他进程仍要写入，不能把磁盘恰好填满。
DOWNLOAD_FREE_SPACE_MARGIN = 64 * 1024 * 1024
# 进度持久化节流：每 4 MiB 落盘一次，避免每块都写小文件。
DOWNLOAD_PROGRESS_PERSIST_BYTES = 4 * 1024 * 1024
# 存储治理：只清理超过保留期的 CLI 转录；调试分段与截图证据由日志服务按配额管理。
LOG_CLEANUP_MAX_AGE_SECONDS = 7 * 24 * 3600
# 清理是破坏性操作，必须由调用方提交固定确认标记，避免误触。
CLEANUP_CONFIRM = "cleanup"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# GitHub Release 上附带的更新清单；与安装目录内的运行时 release-manifest.json 区分。
MANIFEST_ASSET = "update-manifest.json"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)
# 发布资产与本机架构统一归一化后再比较，避免 x64/amd64/x86_64 三种写法造成误判。
_ARCH_ALIASES = {
    "x64": "x64",
    "amd64": "x64",
    "x86_64": "x64",
    "x86-64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86": "x86",
    "i386": "x86",
    "i686": "x86",
}
# 程序目录默认取包所在目录的上一级（app）；与数据目录严格分离，不参与反推。
DEFAULT_APP_ROOT = Path(__file__).resolve().parents[2]


@total_ordering
@dataclass(frozen=True)
class Version:
    """语义版本：正式版高于同号预发布版，数字段按数值比较。"""

    major: int
    minor: int
    patch: int
    prerelease: tuple[tuple[int, Any], ...] | None

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = _VERSION_RE.fullmatch(str(text or "").strip())
        if not match:
            raise ValueError(f"版本号无效: {text!r}")
        pre = match.group(4)
        parts: tuple[tuple[int, Any], ...] | None = None
        if pre:
            parts = tuple(
                (0, int(part)) if part.isdigit() else (1, part.lower())
                for part in pre.split(".")
                if part
            )
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), parts)

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease is not None

    def __lt__(self, other: "Version") -> bool:
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)
        if self.prerelease is None and other.prerelease is None:
            return False
        if self.prerelease is None:
            return False
        if other.prerelease is None:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left != right:
                return left < right
        return len(self.prerelease) < len(other.prerelease)


def is_newer(candidate: str, current: str) -> bool:
    try:
        return Version.parse(candidate) > Version.parse(current)
    except ValueError:
        return False


def _meets_min_supported(latest: dict, current: str) -> bool:
    minimum = str(latest.get("min_supported_version") or "")
    if not minimum:
        return True
    try:
        return Version.parse(current) >= Version.parse(minimum)
    except ValueError:
        return False


def _update_available(latest: dict | None, current: str, platform: str, arch: str) -> bool:
    if not latest or not latest.get("eligible") or not current:
        return False
    if not is_newer(str(latest.get("version") or ""), current):
        return False
    if not _has_asset_for_platform(latest, platform, arch):
        return False
    return _meets_min_supported(latest, current)


def _asset_kind_for_platform(platform: str) -> str:
    return {"windows": "windows_installer", "linux": "hub_image"}.get(platform, "")


def _downloadable_asset(latest: dict, platform: str, arch: str) -> dict | None:
    """返回与本机平台、架构匹配的资产；下载前必须再走一次同样的匹配。"""
    kind = _asset_kind_for_platform(platform)
    if not kind or not arch:
        return None
    for asset in latest.get("assets") or []:
        if (
            isinstance(asset, dict)
            and asset.get("kind") == kind
            and asset.get("platform") == platform
            and _normalize_arch(asset.get("arch")) == arch
        ):
            return asset
    return None


def _has_asset_for_platform(latest: dict, platform: str, arch: str) -> bool:
    return _downloadable_asset(latest, platform, arch) is not None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _header_value(headers: dict[str, str], name: str) -> str:
    """响应头大小写不固定，统一按小写取值。"""
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return ""


def _int_header(headers: dict[str, str], name: str) -> int | None:
    try:
        return int(_header_value(headers, name))
    except (TypeError, ValueError):
        return None


def _valid_content_range(headers: dict[str, str], offset: int, size: int) -> bool:
    """校验续传范围必须从本地断点开始、到清单末尾结束，且长度与清单一致。"""
    match = _CONTENT_RANGE_RE.fullmatch(_header_value(headers, "content-range").strip())
    if not match:
        return False
    start, end, total = int(match.group(1)), int(match.group(2)), match.group(3)
    if start != offset or end < start:
        return False
    if total != "*" and (int(total) != size or end != size - 1):
        return False
    length = _int_header(headers, "content-length")
    return length is None or length == size - offset


class _ResumeMismatch(Exception):
    """本地断点与服务器返回范围不一致；调用方应丢弃断点并重下一次完整文件。"""


def _normalize_arch(value: Any) -> str:
    """把 x64/amd64/x86_64、arm64/aarch64 等写法归一化为统一架构名。"""
    return _ARCH_ALIASES.get(str(value or "").strip().lower(), "")


def _local_arch() -> str:
    return _normalize_arch(platform_module.machine())


def _is_stale(cache: dict, now: float) -> bool:
    """缓存是否超过有效期；失败后保留的旧信息也会随年龄变为过期。"""
    checked = float(cache.get("checked_at") or 0)
    return not checked or now - checked > CACHE_TTL_SECONDS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_asset_url(url: str, tag: str) -> str:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_ASSET_HOSTS:
        raise ValueError("发布资产地址不在允许的 GitHub 域名内")
    if f"/releases/download/{tag}/" not in parsed.path:
        raise ValueError("发布资产地址与 Release 标签不一致")
    return str(url)


def _validate_manifest(manifest: dict, tag: str, channel: str, release_assets: list[dict]) -> dict:
    if not isinstance(manifest, dict):
        raise ValueError("发布清单格式无效")
    version = str(manifest.get("version") or "")
    tag_version = Version.parse(tag)
    if Version.parse(version) != tag_version:
        raise ValueError("发布清单版本与 Release 标签不一致")
    manifest_channel = str(manifest.get("channel") or "stable")
    actual_channel = "preview" if tag_version.is_prerelease else "stable"
    # preview 渠道同时接收稳定版与预发布版；清单渠道必须与版本本身一致。
    if manifest_channel not in CHANNELS or manifest_channel != actual_channel:
        raise ValueError("发布清单渠道与版本不一致")
    if channel == "stable" and actual_channel != "stable":
        raise ValueError("正式渠道不接受预发布版本")
    min_supported = str(manifest.get("min_supported_version") or "")
    if min_supported:
        Version.parse(min_supported)
    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ValueError("发布清单缺少资产列表")
    known = {str(item.get("name") or ""): item for item in release_assets if isinstance(item, dict)}
    assets: list[dict] = []
    seen: set[str] = set()
    for item in raw_assets:
        if not isinstance(item, dict):
            raise ValueError("发布清单资产格式无效")
        kind = str(item.get("kind") or "")
        name = str(item.get("name") or "")
        if kind not in ASSET_KINDS or kind in seen:
            raise ValueError(f"发布清单资产类型无效或重复: {kind!r}")
        platform = str(item.get("platform") or "")
        arch = str(item.get("arch") or "")
        expected_platform = "windows" if kind == "windows_installer" else "linux"
        if platform != expected_platform:
            raise ValueError(f"发布资产平台与类型不一致: {name}")
        if not _normalize_arch(arch):
            raise ValueError(f"发布资产架构无效: {name}")
        if not name or Path(name).name != name:
            raise ValueError("发布资产名称无效")
        published = known.get(name)
        if published is None:
            raise ValueError(f"Release 中不存在资产: {name}")
        size = item.get("size")
        if not isinstance(size, int) or size <= 0:
            raise ValueError(f"发布资产大小无效: {name}")
        if published.get("size") is not None and int(published["size"]) != size:
            raise ValueError(f"发布资产大小与 Release 不一致: {name}")
        sha = str(item.get("sha256") or "").lower()
        if not _SHA256_RE.fullmatch(sha):
            raise ValueError(f"发布资产 SHA-256 无效: {name}")
        digest = str(published.get("digest") or "")
        if digest.startswith("sha256:") and digest.split(":", 1)[1].lower() != sha:
            raise ValueError(f"发布资产摘要与 Release 不一致: {name}")
        url = _safe_asset_url(str(item.get("url") or ""), tag)
        if published.get("browser_download_url") and str(published["browser_download_url"]) != url:
            raise ValueError(f"发布资产地址与 Release 不一致: {name}")
        seen.add(kind)
        assets.append({
            "kind": kind, "name": name, "size": size, "sha256": sha, "url": url,
            "platform": platform, "arch": arch,
        })
    return {
        "version": version,
        "channel": manifest_channel,
        "git_commit": str(manifest.get("git_commit") or ""),
        "built_at": str(manifest.get("built_at") or ""),
        "min_supported_version": str(manifest.get("min_supported_version") or ""),
        "rollback": str(manifest.get("rollback") or ""),
        "assets": assets,
    }


class UpdateService:
    def __init__(
        self,
        data_root: Path,
        *,
        role: str,
        app_root: Path | None = None,
        install_root: Path | None = None,
        repository: str = DEFAULT_REPOSITORY,
        fetcher: Callable[[str, dict[str, str], float], tuple[int, dict[str, str], bytes]] | None = None,
        opener: Callable[[str, dict[str, str], float], Any] | None = None,
        clock: Callable[[], float] = time.time,
        platform: str | None = None,
        machine: str | None = None,
    ) -> None:
        # 三个目录语义必须分开：
        # - data_root：配置、缓存、审计等可写数据（Windows 上通常由 MENU_CAPTURE_HOME 指定）；
        # - app_root：当前运行的程序目录（只读，含 release-manifest.json）；
        # - install_root：多版本安装根（含 releases/），Windows 才能从 app_root 推导。
        # 绝不能从 data_root 反推安装目录，否则清理与回滚执行器会定位到错误位置。
        self.data_root = Path(data_root)
        self.app_root = Path(app_root) if app_root is not None else DEFAULT_APP_ROOT
        self._install_root_override = Path(install_root) if install_root is not None else None
        self.role = role
        self.platform = platform or _platform_name()
        self.local_arch = _normalize_arch(machine or platform_module.machine())
        self.repository = repository
        self.data_dir = self.data_root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.data_dir / "update-cache.json"
        self.preferences_path = self.data_dir / "update-preferences.json"
        self.history_path = self.data_dir / "update-history.jsonl"
        # 下载产物与断点只落在数据目录，绝不写入程序目录或安装目录。
        self.download_dir = self.data_dir / "updates"
        self.download_state_path = self.data_dir / "update-download.json"
        self._lock = threading.RLock()
        self._fetcher = fetcher or _http_request
        self._opener = opener or _http_open
        self._clock = clock
        self._cache = self._read_json(self.cache_path, {})
        self._preferences = self._read_preferences()
        self._checking = False
        self._check_done: threading.Event | None = None
        # 渠道切换时递增；在途检查写回前必须确认代号未变化，否则丢弃旧结果。
        self._generation = 0
        # 下载同一时刻只允许一个任务；进度频繁变化，落盘按阈值节流。
        self._downloading = False
        self._download_cancel: threading.Event | None = None
        self._download_thread: threading.Thread | None = None
        self._download_persisted = 0
        self._download_state = self._load_download_state()
        # 由服务端按角色注入真实安装能力：Windows 为本地执行器，Hub 为宿主机代理。
        self.install_capability: Callable[[], bool] | None = None
        self.rollback_capability: Callable[[], bool] | None = None
        # 安装/回滚期间必须禁止清理历史版本，否则可能删掉正在使用的回滚目标。
        self.cleanup_guard: Callable[[], tuple[bool, str]] | None = None
        # Worker 可由共享后台分发版本元数据；只有 Hub 校验后且未过期的结果才会被采纳。
        self.hub_target_provider: Callable[[], dict | None] | None = None
        # Worker 向 Hub 上报安装状态时使用；执行器未接入时留空，不推测。
        self.install_status_provider: Callable[[], str] | None = None

    # ---------- 对外状态 ----------

    def status(self) -> dict:
        with self._lock:
            preferences = dict(self._preferences)
            cache = dict(self._cache)
            now = self._clock()
            stale = _is_stale(cache, now)
            should_start = bool(
                preferences.get("auto_check")
                and stale
                and now >= float(cache.get("backoff_until") or 0)
            )
        # 只置位并启动后台线程；网络请求不占用状态锁，也不与已有检查并发。
        start_background = should_start and self._try_begin_check()
        if start_background:
            self._spawn_background_check()
        return self._status_payload(preferences, cache, self._is_checking())

    def check(self, *, force: bool = True) -> dict:
        with self._lock:
            stale = _is_stale(self._cache, self._clock())
        if not force and not stale:
            return self.status_no_check()
        if self._try_begin_check():
            try:
                self._run_check(force=force)
            finally:
                self._end_check()
        else:
            # 已有检查在运行：等待它结束后返回同一份结果，避免重复请求 Release 接口。
            self._wait_for_check()
        return self.status_no_check()

    def status_no_check(self) -> dict:
        with self._lock:
            preferences = dict(self._preferences)
            cache = dict(self._cache)
        return self._status_payload(preferences, cache, self._is_checking())

    def _status_payload(self, preferences: dict, cache: dict, checking: bool) -> dict:
        now = self._clock()
        current = self.current_version()
        latest = cache.get("latest")
        return {
            "role": self.role,
            "repository": self.repository,
            "platform": self.platform,
            "arch": self.local_arch,
            "current_version": current,
            "channel": preferences.get("channel", "stable"),
            "preferences": preferences,
            "latest": latest,
            "update_available": _update_available(latest, current, self.platform, self.local_arch),
            "last_checked": _iso(cache.get("checked_at")),
            # 元数据来源：github 为本机检查，hub 为共享后台分发；默认不伪装。
            "source": str(cache.get("source") or "github"),
            "stale": _is_stale(cache, now),
            "error": str(cache.get("error") or ""),
            "checking": checking,
            "download": self.download_status(),
            "capabilities": {
                "check": True,
                # 本阶段提供受控下载（临时文件 + 断点续传 + SHA-256 校验）；
                # 安装执行器需按角色接入：Windows 本地执行器或 Hub 宿主机代理。
                "download": True,
                "install": self._install_capable(),
                "rollback": self._rollback_capable(),
                "inventory": self._inventory_supported(),
                # 清理始终先预览；安装/回滚期间由 cleanup_guard 阻止实际删除。
                "cleanup": True,
            },
            "inventory": self.inventory(),
        }

    def release_target(self) -> dict | None:
        """Hub 向 Worker 分发的版本元数据；只返回新鲜且已通过清单校验的结果。"""
        with self._lock:
            cache = dict(self._cache)
            channel = str(self._preferences.get("channel") or "stable")
        latest = cache.get("latest")
        checked_at = float(cache.get("checked_at") or 0)
        if not isinstance(latest, dict) or not latest.get("eligible"):
            return None
        if not checked_at or self._clock() - checked_at > CACHE_TTL_SECONDS:
            return None
        return {"channel": channel, "checked_at": checked_at, "latest": latest}

    def worker_software_status(self) -> dict:
        """采集端向 Hub 上报的精简状态；不含路径、凭证或业务数据。"""
        payload = self.status_no_check()
        latest = payload.get("latest") or {}
        download = payload.get("download") or {}
        preferences = payload.get("preferences") or {}
        install_status = ""
        if self.install_status_provider is not None:
            try:
                install_status = str(self.install_status_provider() or "")[:32]
            except Exception:  # noqa: BLE001 - 状态上报失败不影响采集
                install_status = ""
        return {
            "current_version": str(payload.get("current_version") or ""),
            "platform": str(payload.get("platform") or ""),
            "latest_version": str(latest.get("version") or "") if latest.get("eligible") else "",
            "update_available": bool(payload.get("update_available")),
            "channel": str(preferences.get("channel") or "stable"),
            "checked_at": str(payload.get("last_checked") or ""),
            "download_status": str(download.get("status") or ""),
            "install_status": install_status,
        }

    def _adopt_hub_target(self, target: Any, channel: str) -> tuple[dict, float] | None:
        """复用 Hub 校验结果前重新核对版本、渠道、资产与 URL，避免盲信跨端数据。"""
        if not isinstance(target, dict):
            return None
        latest = target.get("latest")
        if not isinstance(latest, dict) or not latest.get("eligible"):
            return None
        try:
            checked_at = float(target.get("checked_at") or 0)
            version = Version.parse(str(latest.get("version") or ""))
            tag = str(latest.get("tag") or f"v{latest.get('version')}")
            if Version.parse(tag) != version:
                return None
        except (TypeError, ValueError):
            return None
        if not checked_at or self._clock() - checked_at > CACHE_TTL_SECONDS:
            return None
        manifest_channel = str(latest.get("channel") or "")
        # Worker 只采纳与自身渠道一致、且新鲜的分发结果。
        if manifest_channel not in CHANNELS or str(target.get("channel") or "") != channel:
            return None
        if manifest_channel != channel:
            return None
        if channel == "stable" and version.is_prerelease:
            return None
        min_supported = str(latest.get("min_supported_version") or "")
        if min_supported:
            try:
                Version.parse(min_supported)
            except ValueError:
                return None
        assets = latest.get("assets")
        if not isinstance(assets, list) or not assets:
            return None
        seen: set[str] = set()
        for asset in assets:
            if not isinstance(asset, dict):
                return None
            kind = str(asset.get("kind") or "")
            name = str(asset.get("name") or "")
            if kind not in ASSET_KINDS or kind in seen or not name or Path(name).name != name:
                return None
            seen.add(kind)
            expected = "windows" if kind == "windows_installer" else "linux"
            if asset.get("platform") != expected or not _normalize_arch(asset.get("arch")):
                return None
            size = asset.get("size")
            if not isinstance(size, int) or size <= 0:
                return None
            if not _SHA256_RE.fullmatch(str(asset.get("sha256") or "").lower()):
                return None
            try:
                _safe_asset_url(str(asset.get("url") or ""), tag)
            except ValueError:
                return None
        return latest, checked_at

    def save_preferences(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"auto_check", "channel"}:
            raise ValueError("更新偏好参数无效")
        preferences = dict(self._preferences)
        previous_channel = preferences.get("channel", "stable")
        if "auto_check" in payload:
            if not isinstance(payload["auto_check"], bool):
                raise ValueError("自动检查开关应为布尔值")
            preferences["auto_check"] = payload["auto_check"]
        if "channel" in payload:
            if payload["channel"] not in CHANNELS:
                raise ValueError("发布渠道无效")
            preferences["channel"] = payload["channel"]
        with self._lock:
            self._preferences = preferences
            self._write_json(self.preferences_path, preferences)
            if preferences.get("channel") != previous_channel:
                # 切换渠道后旧渠道的 latest 不再代表当前策略，下一次状态读取必须重新检查。
                # 代号递增让仍在途的旧渠道请求在写回时被丢弃，不会覆盖新渠道缓存。
                self._generation += 1
                cache = dict(self._cache)
                cache["checked_at"] = 0
                cache["latest"] = None
                cache["etag"] = ""
                self._cache = cache
                self._write_json(self.cache_path, cache)
            self._append_history("preferences", "ok", "更新偏好已保存", "")
        return preferences

    def history(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        result: list[dict] = []
        for line in lines[-limit:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                result.append(row)
        return list(reversed(result))

    def current_version(self) -> str:
        version = os.environ.get("MENU_CAPTURE_VERSION", "")
        try:
            release = json.loads((self.app_root.parent / "release-manifest.json").read_text(encoding="utf-8"))
            version = str(release.get("version") or version)
        except (OSError, ValueError, AttributeError):
            pass
        return version.strip()

    # ---------- 版本清点（只读） ----------

    def _install_root(self) -> Path | None:
        if sys.platform != "win32":
            return None
        # 安装布局：<install>/releases/<version>/app/menu_capture → 安装根为程序目录的上三级。
        # 这里只允许显式 install_root 或程序目录推导；数据目录（data_root）不参与定位。
        root = self._install_root_override
        if root is None:
            root = self.app_root.parent.parent.parent
        return root if (root / "releases").is_dir() else None

    def _inventory_supported(self) -> bool:
        return self._install_root() is not None

    def install_root(self) -> Path | None:
        """安装根只用于安装执行器与清点；未启用安装时也允许只读调用。"""
        return self._install_root()

    def install_supported(self) -> bool:
        """自动安装必须同时满足：Windows 采集端、可定位安装根、运维显式启用。

        未在真实 Windows 升级/回滚验收前不默认开启，避免把未经环境验证的高权限链路暴露给业务页面。
        """
        return (
            sys.platform == "win32"
            and self.role == "worker"
            and self._install_root() is not None
            and os.environ.get("MENU_CAPTURE_ALLOW_SELF_UPDATE") == "1"
        )

    def _install_capable(self) -> bool:
        if self.install_capability is not None:
            try:
                return bool(self.install_capability())
            except Exception:  # noqa: BLE001 - 能力探测失败按不可用处理，不能阻断状态接口
                return False
        return self.install_supported()

    def _rollback_capable(self) -> bool:
        if self.rollback_capability is not None:
            try:
                return bool(self.rollback_capability())
            except Exception:  # noqa: BLE001 - 同上
                return False
        return self.install_supported()

    def inventory(self) -> dict:
        install_root = self._install_root()
        if install_root is None:
            return {
                "supported": False,
                "reason": "仅 Windows 采集端支持程序版本目录清点",
                "releases": [],
                "cleanup_candidates": [],
            }
        current = ""
        try:
            current = (install_root / "current-version").read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        running = self.current_version()
        releases: list[dict] = []
        releases_dir = install_root / "releases"
        for directory in releases_dir.iterdir() if releases_dir.is_dir() else []:
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest_path = directory / "release-manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                version = str(manifest.get("version") or directory.name)
                parsed = Version.parse(version)
            except (OSError, ValueError, AttributeError):
                continue
            releases.append({
                "version": version,
                "built_at": str(manifest.get("built_at") or ""),
                "git_commit": str(manifest.get("git_commit") or ""),
                "size_bytes": _tree_size(directory),
            })
        releases.sort(key=lambda row: Version.parse(row["version"]), reverse=True)
        protected_versions = {current, running} - {""}
        for row in releases:
            if row["version"] not in protected_versions and len(protected_versions) < 3:
                protected_versions.add(row["version"])
        for row in releases:
            row["current"] = row["version"] == current
            row["running"] = row["version"] == running
            row["protected"] = row["version"] in protected_versions
        return {
            "supported": True,
            "install_root": str(install_root),
            "current_version": current,
            "running_version": running,
            "releases": releases,
            "cleanup_candidates": [row for row in releases if not row["protected"]],
            # 清点保持只读；真正删除必须调用 cleanup() 并提交确认标记，且以重算结果为准。
            "cleanup_supported": True,
            "cleanup_reason": "清理需显式确认；保护运行中、当前与最近两个可回滚版本",
        }

    # ---------- 存储治理（只读预览 + 显式确认删除） ----------

    def storage_preview(self) -> dict:
        """预览各存储范围的可清理项；不删除任何文件。"""
        scopes = {
            "releases": self._preview_releases(),
            "downloads": self._preview_downloads(),
            "logs": self._preview_logs(),
        }
        blocked, reason = self._cleanup_block()
        return {
            "generated_at": _utc_now(),
            "blocked": blocked,
            "blocked_reason": reason,
            "confirm": CLEANUP_CONFIRM,
            "scopes": scopes,
            "reclaimable_bytes": sum(int(scope.get("reclaimable_bytes") or 0) for scope in scopes.values()),
        }

    def _cleanup_block(self) -> tuple[bool, str]:
        if self._downloading:
            return True, "下载进行中，请等待完成或取消后再清理"
        if self.cleanup_guard is not None:
            try:
                return self.cleanup_guard()
            except Exception:  # noqa: BLE001 - 闸门异常按阻止处理，不能冒险删除
                return True, "无法确认安装状态，暂不允许清理"
        return False, ""

    def _preview_releases(self) -> dict:
        install_root = self._install_root()
        if install_root is None:
            return {
                "supported": False,
                "reason": "仅 Windows 采集端支持历史版本清理",
                "items": [],
                "reclaimable_bytes": 0,
            }
        inventory = self.inventory()
        items = [
            {
                "version": row["version"],
                "size_bytes": row["size_bytes"],
                "path": str(install_root / "releases" / row["version"]),
            }
            for row in inventory["cleanup_candidates"]
        ]
        return {
            "supported": True,
            "reason": "",
            "items": items,
            "reclaimable_bytes": sum(item["size_bytes"] for item in items),
        }

    def _pending_download_version(self) -> str:
        """当前下载状态指向且产物仍在的版本必须保留，安装执行器还要用它。"""
        state = dict(self._download_state)
        version = str(state.get("version") or "")
        name = str(state.get("asset_name") or "")
        if state.get("status") != "completed" or not version or not name:
            return ""
        if Path(name).name != name:
            return ""
        try:
            target = self.download_dir / version / name
            return version if target.is_file() and not _is_reparse(target) else ""
        except OSError:
            return ""

    def _preview_downloads(self) -> dict:
        if not self.download_dir.is_dir():
            return {"supported": True, "reason": "", "kept": [], "items": [], "reclaimable_bytes": 0}
        keep = self._pending_download_version()
        items: list[dict] = []
        for directory in sorted(self.download_dir.iterdir()):
            # 只处理下载目录的直接子目录；链接与其他文件一律跳过，不按名字猜测内容。
            if not directory.is_dir() or _is_reparse(directory):
                continue
            version = directory.name
            try:
                Version.parse(version)
            except ValueError:
                continue
            if version == keep:
                continue
            items.append({"version": version, "size_bytes": _tree_size(directory), "path": str(directory)})
        return {
            "supported": True,
            "reason": "",
            "kept": [keep] if keep else [],
            "items": items,
            "reclaimable_bytes": sum(item["size_bytes"] for item in items),
        }

    def _preview_logs(self) -> dict:
        cli_dir = self.data_dir / "debug-logs" / "cli"
        if not cli_dir.is_dir():
            return {
                "supported": True,
                "reason": "调试日志目录尚未生成",
                "items": [],
                "reclaimable_bytes": 0,
            }
        cutoff = self._clock() - LOG_CLEANUP_MAX_AGE_SECONDS
        items: list[dict] = []
        for path in sorted(cli_dir.glob("*.log")):
            try:
                if not path.is_file() or _is_reparse(path):
                    continue
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                continue
            items.append({"name": path.name, "size_bytes": stat.st_size, "path": str(path)})
        return {
            "supported": True,
            "reason": "只清理超过 7 天的 CLI 转录；调试分段与截图证据由日志服务按配额管理，避免破坏证据引用",
            "items": items,
            "reclaimable_bytes": sum(item["size_bytes"] for item in items),
        }

    def cleanup(self, payload: dict) -> dict:
        """删除预览中的可清理项。任何一项校验失败只记录失败，不扩大删除范围。"""
        if not isinstance(payload, dict) or set(payload) - {"confirm", "scopes"}:
            raise ValueError("清理参数无效")
        if str(payload.get("confirm") or "") != CLEANUP_CONFIRM:
            raise ValueError("清理必须提交确认标记")
        scopes = payload.get("scopes") or ["releases", "downloads", "logs"]
        if not isinstance(scopes, list) or not scopes or set(scopes) - {"releases", "downloads", "logs"}:
            raise ValueError("清理范围无效")
        with self._lock:
            blocked, reason = self._cleanup_block()
            if blocked:
                raise ValueError(reason)
            deleted: list[dict] = []
            failures: list[dict] = []
            for scope in scopes:
                if scope == "releases":
                    self._cleanup_releases(deleted, failures)
                elif scope == "downloads":
                    self._cleanup_downloads(deleted, failures)
                else:
                    self._cleanup_logs(deleted, failures)
            freed = sum(int(row.get("size_bytes") or 0) for row in deleted)
            summary = f"已清理 {len(deleted)} 项，释放 {freed} 字节" if deleted else "没有可清理项"
            if failures:
                summary += f"，{len(failures)} 项失败"
            self._append_history(
                "cleanup", "failed" if failures else "ok", f"{summary}（范围：{', '.join(scopes)}）", ""
            )
            return {
                "scopes": scopes,
                "deleted": deleted,
                "failures": failures,
                "freed_bytes": freed,
                "preview": self.storage_preview(),
            }

    def _cleanup_releases(self, deleted: list[dict], failures: list[dict]) -> None:
        install_root = self._install_root()
        if install_root is None:
            return
        for item in self._preview_releases()["items"]:
            version = str(item["version"])
            try:
                size = self._remove_release(install_root, version)
            except (OSError, ValueError) as exc:
                failures.append({"scope": "releases", "version": version, "error": str(exc)})
                continue
            deleted.append({"scope": "releases", "version": version, "size_bytes": size})

    @staticmethod
    def _remove_release(install_root: Path, version: str) -> int:
        """删除 releases 下的单个版本目录；逐级拒绝链接并确认仍是 releases 的直接子目录。"""
        install_root = Path(install_root)
        try:
            Version.parse(version)
        except ValueError:
            raise ValueError("版本号无效") from None
        if not version or Path(version).name != version:
            raise ValueError("版本目录非法")
        if not install_root.is_dir() or _is_reparse(install_root):
            raise ValueError("安装根不可信")
        releases_dir = install_root / "releases"
        if not releases_dir.is_dir() or _is_reparse(releases_dir):
            raise ValueError("版本目录不可信")
        target = releases_dir / version
        if not target.exists() or target.is_symlink() or _is_reparse(target) or not target.is_dir():
            raise ValueError("版本目录不存在或不是普通目录")
        resolved_root = releases_dir.resolve(strict=True)
        resolved = target.resolve(strict=True)
        if resolved.parent != resolved_root:
            raise ValueError("版本目录越界")
        size = _tree_size(target)
        shutil.rmtree(target)
        return size

    def _cleanup_downloads(self, deleted: list[dict], failures: list[dict]) -> None:
        for item in self._preview_downloads()["items"]:
            version = str(item["version"])
            directory = Path(item["path"])
            try:
                if not directory.is_dir() or directory.is_symlink() or _is_reparse(directory):
                    raise ValueError("下载目录不可信")
                root = self.download_dir.resolve(strict=True)
                if directory.resolve(strict=True).parent != root:
                    raise ValueError("下载目录越界")
                size = _tree_size(directory)
                shutil.rmtree(directory)
            except (OSError, ValueError) as exc:
                failures.append({"scope": "downloads", "version": version, "error": str(exc)})
                continue
            deleted.append({"scope": "downloads", "version": version, "size_bytes": size})

    def _cleanup_logs(self, deleted: list[dict], failures: list[dict]) -> None:
        for item in self._preview_logs()["items"]:
            name = str(item["name"])
            path = Path(item["path"])
            try:
                if not path.is_file() or _is_reparse(path):
                    raise ValueError("日志文件不可信")
                size = path.stat().st_size
                path.unlink()
            except (OSError, ValueError) as exc:
                failures.append({"scope": "logs", "name": name, "error": str(exc)})
                continue
            deleted.append({"scope": "logs", "name": name, "size_bytes": size})

    # ---------- 受控下载（只下载，不安装） ----------

    def download_status(self) -> dict:
        with self._lock:
            state = dict(self._download_state)
        if state.get("status") in {"failed", "cancelled", "interrupted"}:
            state["resumable"] = self._part_path(
                str(state.get("version") or ""), str(state.get("asset_name") or "")
            ).is_file()
        else:
            state["resumable"] = False
        return state

    def start_download(self) -> dict:
        """启动唯一的下载任务；版本、平台、架构与哈希全部来自已验证清单。"""
        with self._lock:
            if self._downloading:
                raise ValueError("已有下载任务正在运行")
            latest = self._cache.get("latest")
            current = self.current_version()
            if not isinstance(latest, dict) or not _update_available(latest, current, self.platform, self.local_arch):
                raise ValueError("当前没有可下载的更新，请先检查更新并确认平台、架构匹配")
            asset = _downloadable_asset(latest, self.platform, self.local_arch)
            if asset is None:
                raise ValueError("发布清单中没有与本机平台和架构匹配的资产")
            version = str(latest.get("version") or "")
            previous = dict(self._download_state)
            # 同一版本、同一资产续传时保留 ETag/Last-Modified，用于 If-Range 防止拼接已变化的资源。
            same_asset = (
                previous.get("version") == version
                and previous.get("asset_name") == str(asset.get("name") or "")
                and previous.get("sha256") == str(asset.get("sha256") or "")
            )
            self._downloading = True
            self._download_cancel = threading.Event()
            self._download_persisted = 0
            self._download_state = {
                **_empty_download_state(),
                "active": True,
                "status": "downloading",
                "version": version,
                "asset_kind": str(asset.get("kind") or ""),
                "asset_name": str(asset.get("name") or ""),
                "arch": str(asset.get("arch") or ""),
                # 记录资产身份，安装前与后续版本绑定判断都以它为准，不能只看 completed。
                "sha256": str(asset.get("sha256") or ""),
                "etag": str(previous.get("etag") or "") if same_asset else "",
                "last_modified": str(previous.get("last_modified") or "") if same_asset else "",
                "total": int(asset.get("size") or 0),
                "started_at": _utc_now(),
            }
            self._write_download_state()
            thread = threading.Thread(
                target=self._run_download,
                args=(dict(asset), version, self._download_cancel),
                name="menu-capture-update-download",
                daemon=True,
            )
            self._download_thread = thread
        thread.start()
        return self.download_status()

    def cancel_download(self) -> dict:
        with self._lock:
            cancel = self._download_cancel if self._downloading else None
        if cancel is not None:
            cancel.set()
        return self.download_status()

    def _run_download(self, asset: dict, version: str, cancel: threading.Event) -> None:
        name = str(asset.get("name") or "")
        size = int(asset.get("size") or 0)
        sha = str(asset.get("sha256") or "")
        final = self.download_dir / version / name
        part = self._part_path(version, name)
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            # 已存在且校验通过的产物直接复用，避免同一版本反复下载。
            if final.is_file() and _file_sha256(final) == sha:
                self._finish_download("completed", path=str(final), downloaded=size)
                return
            if final.exists():
                final.unlink(missing_ok=True)
            offset = part.stat().st_size if part.is_file() else 0
            if offset > size or (offset == size and _file_sha256(part) != sha):
                part.unlink(missing_ok=True)
                offset = 0
            if offset == size:
                os.replace(part, final)
                self._finish_download("completed", path=str(final), downloaded=size)
                return
            free = shutil.disk_usage(final.parent).free
            if free < size - offset + DOWNLOAD_FREE_SPACE_MARGIN:
                raise ValueError(f"磁盘剩余空间不足：需要 {size - offset} 字节，可用 {free} 字节")
            written = offset
            # 断点与服务器范围不一致时，最多重下一次完整文件，避免把错误范围拼接进产物。
            for attempt in (1, 2):
                if cancel.is_set():
                    self._finish_download("cancelled", error="下载已取消，已保留断点")
                    return
                self._report_progress(offset, persist=True)
                hasher = hashlib.sha256()
                if offset:
                    with part.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
                            hasher.update(chunk)
                headers = {"Accept": "application/octet-stream", "User-Agent": "menu-capture-updater"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                    etag = str(self._download_state.get("etag") or "")
                    # If-Range 让服务器在资源已变化时返回完整文件而不是错误区段。
                    if etag:
                        headers["If-Range"] = etag
                try:
                    with self._opener(str(asset.get("url") or ""), headers, DOWNLOAD_TIMEOUT_SECONDS) as opened:
                        status, response_headers, response = opened
                        if status == 416:
                            raise _ResumeMismatch("服务器拒绝了当前断点范围")
                        if status == 206:
                            if offset <= 0 or not _valid_content_range(response_headers, offset, size):
                                raise _ResumeMismatch("服务器返回的续传范围与本地断点或清单大小不一致")
                            append = True
                        elif status == 200:
                            append = False
                        else:
                            raise ValueError(f"下载失败（HTTP {status}）")
                        if not append:
                            length = _int_header(response_headers, "content-length")
                            if length is not None and length != size:
                                raise ValueError("下载响应大小与发布清单不一致，已中止")
                            if offset:
                                # 服务器忽略 Range 或资源已变化：从零重写，并重新确认完整文件的磁盘余量。
                                offset = 0
                                written = 0
                                hasher = hashlib.sha256()
                                part.unlink(missing_ok=True)
                                self._report_progress(0, persist=True)
                                free = shutil.disk_usage(final.parent).free
                                if free < size + DOWNLOAD_FREE_SPACE_MARGIN:
                                    raise ValueError(f"磁盘剩余空间不足：需要 {size} 字节，可用 {free} 字节")
                        self._set_download_meta(response_headers)
                        with part.open("ab" if append else "wb") as handle:
                            while True:
                                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                                if not chunk:
                                    break
                                if cancel.is_set():
                                    self._finish_download("cancelled", error="下载已取消，已保留断点")
                                    return
                                written += len(chunk)
                                # 不能只信 Content-Length：响应体超过清单大小时立即中止。
                                if written > size:
                                    raise ValueError("下载内容超过发布清单声明的大小，已中止")
                                hasher.update(chunk)
                                handle.write(chunk)
                                self._report_progress(written)
                except _ResumeMismatch as exc:
                    if attempt >= 2:
                        raise ValueError(f"{exc}，请重新开始下载") from exc
                    part.unlink(missing_ok=True)
                    offset = 0
                    written = 0
                    self._report_progress(0, persist=True)
                    continue
                break
            if written != size:
                raise ValueError(f"下载未完成：{written}/{size} 字节，可重试续传")
            self._set_download_status("verifying")
            if hasher.hexdigest() != sha:
                part.unlink(missing_ok=True)
                raise ValueError("SHA-256 校验失败，已删除损坏文件")
            os.replace(part, final)
            self._finish_download("completed", path=str(final), downloaded=size)
        except Exception as exc:
            # 未完成的 .part 原样保留，下一次开始下载会按 Range 续传。
            self._finish_download("failed", error=str(exc) or "下载失败")
        finally:
            with self._lock:
                self._downloading = False
                self._download_cancel = None

    def _set_download_meta(self, headers: dict[str, str]) -> None:
        etag = _header_value(headers, "etag")
        modified = _header_value(headers, "last-modified")
        if not etag and not modified:
            return
        with self._lock:
            state = dict(self._download_state)
            if etag:
                state["etag"] = etag
            if modified:
                state["last_modified"] = modified
            self._download_state = state
            self._write_download_state()

    def _part_path(self, version: str, name: str) -> Path:
        return self.download_dir / str(version) / (str(name) + ".part")

    def _set_download_status(self, status: str) -> None:
        with self._lock:
            state = dict(self._download_state)
            state["status"] = status
            self._download_state = state
            self._write_download_state()

    def _report_progress(self, written: int, *, persist: bool = False) -> None:
        with self._lock:
            state = dict(self._download_state)
            state["downloaded"] = int(written)
            self._download_state = state
            should_persist = persist or written - self._download_persisted >= DOWNLOAD_PROGRESS_PERSIST_BYTES
            if should_persist:
                self._download_persisted = written
        if should_persist:
            self._write_download_state()

    def _finish_download(self, status: str, *, path: str = "", error: str = "",
                         downloaded: int | None = None) -> None:
        with self._lock:
            state = dict(self._download_state)
            if downloaded is not None:
                state["downloaded"] = int(downloaded)
            state.update({
                "active": False,
                "status": status,
                "error": error,
                "finished_at": _utc_now(),
                "path": path if status == "completed" else "",
            })
            self._download_state = state
            self._write_download_state()
            message = error or (f"已下载 {state.get('asset_name')}" if status == "completed" else status)
            self._append_history("download", "ok" if status == "completed" else "failed",
                                 message, str(state.get("version") or ""))

    def _write_download_state(self) -> None:
        try:
            self._write_json(self.download_state_path, dict(self._download_state))
        except OSError:
            # 状态文件写失败不应中断真实下载；内存状态仍然有效。
            pass

    def _load_download_state(self) -> dict:
        data = self._read_json(self.download_state_path, {})
        state = _empty_download_state()
        if isinstance(data, dict):
            for key, default in state.items():
                value = data.get(key)
                if isinstance(value, type(default)):
                    state[key] = value
        if state["status"] in {"downloading", "verifying"}:
            # 进程重启后下载线程已不存在，必须如实标记为中断并允许续传。
            state.update({
                "status": "interrupted",
                "active": False,
                "error": "下载进程已中断，可重新开始以续传",
            })
        state["active"] = False
        return state

    # ---------- 内部实现 ----------

    def _is_checking(self) -> bool:
        with self._lock:
            return self._checking

    def _try_begin_check(self) -> bool:
        """原子地占用唯一的检查名额；已有检查在跑时返回 False，绝不并发请求同一接口。"""
        with self._lock:
            if self._checking:
                return False
            self._checking = True
            self._check_done = threading.Event()
            return True

    def _end_check(self) -> None:
        with self._lock:
            self._checking = False
            event = self._check_done
            self._check_done = None
        if event is not None:
            event.set()

    def _wait_for_check(self, timeout: float = 30.0) -> None:
        with self._lock:
            event = self._check_done
        if event is not None:
            event.wait(timeout)

    def _spawn_background_check(self) -> None:
        def run() -> None:
            try:
                self._run_check(force=False)
            finally:
                self._end_check()

        threading.Thread(target=run, name="menu-capture-update-check", daemon=True).start()

    def _run_check(self, *, force: bool) -> None:
        """网络请求完全不持有状态锁；只在读取快照与写回结果时短暂加锁。"""
        with self._lock:
            cache = dict(self._cache)
            now = self._clock()
            channel = str(self._preferences.get("channel") or "stable")
            # 请求发出前记录代号；期间渠道被切换时，结果与失败都不允许写回。
            generation = self._generation
            if not force and cache.get("checked_at") and now - float(cache["checked_at"]) <= CACHE_TTL_SECONDS:
                return
            if not force and now < float(cache.get("backoff_until") or 0):
                return

        def still_current() -> bool:
            with self._lock:
                return (
                    self._generation == generation
                    and str(self._preferences.get("channel") or "stable") == channel
                )

        # 优先采用共享后台校验并分发的元数据，避免每台 Worker 重复请求 GitHub；
        # 分发结果过期、渠道不符或结构异常时回退本机检查。
        adopted = None
        if self.hub_target_provider is not None:
            try:
                adopted = self._adopt_hub_target(self.hub_target_provider(), channel)
            except Exception:  # noqa: BLE001 - 分发失败不能阻塞本机检查
                adopted = None
        if adopted is not None:
            latest, checked_at = adopted
            if not still_current():
                return
            with self._lock:
                self._cache = {
                    "latest": latest,
                    "checked_at": checked_at,
                    "error": "",
                    "backoff_until": 0,
                    "source": "hub",
                }
                self._write_json(self.cache_path, self._cache)
            self._append_history("check", "ok", "使用共享后台分发的版本信息",
                                 str((latest or {}).get("version") or ""))
            return

        headers = {"Accept": "application/vnd.github+json", "User-Agent": "menu-capture-updater"}
        if cache.get("etag"):
            headers["If-None-Match"] = str(cache["etag"])
        try:
            status, response_headers, body = self._fetcher(
                f"https://api.github.com/repos/{self.repository}/releases?per_page=20", headers, 10.0
            )
            if status == 304:
                if not still_current():
                    return
                with self._lock:
                    latest_cache = dict(self._cache)
                    latest_cache.update({"checked_at": now, "error": "", "backoff_until": 0,
                                         "source": "github"})
                    self._cache = latest_cache
                    self._write_json(self.cache_path, latest_cache)
                return
            if status != 200:
                raise ValueError(f"GitHub 返回 HTTP {status}")
            releases = json.loads(body.decode("utf-8"))
            if not isinstance(releases, list):
                raise ValueError("GitHub Release 响应格式无效")
            # 渠道已切换时不再下载旧渠道的清单，直接丢弃在途结果。
            if not still_current():
                return
            # 清单下载也在锁外完成，避免慢网络阻塞 /api/updates/status。
            latest = self._select_release(releases, channel)
        except Exception as exc:  # 网络、清单或格式错误都保留上次成功信息
            if not still_current():
                return
            with self._lock:
                failed = dict(self._cache)
                failed["error"] = str(exc) or "更新检查失败"
                failed["backoff_until"] = now + FAILURE_BACKOFF_SECONDS
                self._cache = failed
                self._write_json(self.cache_path, failed)
                self._append_history("check", "failed", str(exc) or "更新检查失败", "")
            return
        if not still_current():
            return
        with self._lock:
            updated = dict(self._cache)
            updated.update({
                "checked_at": now,
                "etag": response_headers.get("etag") or response_headers.get("ETag") or updated.get("etag", ""),
                "latest": latest,
                "error": "",
                "backoff_until": 0,
                "channel": channel,
                "source": "github",
            })
            self._cache = updated
            self._write_json(self.cache_path, updated)
            message = f"最新版本 {latest.get('version')}" if latest else "没有可用的正式发布"
            self._append_history("check", "ok", message, str((latest or {}).get("version") or ""))

    def _select_release(self, releases: list[dict], channel: str) -> dict | None:
        candidates: list[tuple[Version, dict]] = []
        for release in releases:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            try:
                version = Version.parse(str(release.get("tag_name") or ""))
            except ValueError:
                continue
            if channel == "stable" and (release.get("prerelease") or version.is_prerelease):
                continue
            candidates.append((version, release))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        version, release = candidates[0]
        tag = str(release.get("tag_name") or "")
        base = {
            "version": version_text(version, tag),
            "tag": tag,
            "channel": channel,
            "name": str(release.get("name") or tag),
            "html_url": str(release.get("html_url") or ""),
            "published_at": str(release.get("published_at") or ""),
            "notes": str(release.get("body") or "")[:MAX_RELEASE_NOTES],
            "eligible": False,
            "reason": "",
        }
        raw_assets = release.get("assets") if isinstance(release.get("assets"), list) else []
        manifest_asset = next(
            (item for item in raw_assets if isinstance(item, dict) and item.get("name") == MANIFEST_ASSET),
            None,
        )
        if manifest_asset is None:
            base["reason"] = f"该 Release 缺少 {MANIFEST_ASSET}，暂不提供更新"
            return base
        try:
            url = _safe_asset_url(str(manifest_asset.get("browser_download_url") or ""), tag)
            status, _, body = self._fetcher(url, {"Accept": "application/json", "User-Agent": "menu-capture-updater"}, 10.0)
            if status != 200:
                raise ValueError(f"发布清单下载失败（HTTP {status}）")
            manifest = json.loads(body.decode("utf-8"))
            checked = _validate_manifest(manifest, tag, channel, raw_assets)
        except Exception as exc:
            base["reason"] = str(exc) or "发布清单校验失败"
            return base
        base.update(checked)
        base["eligible"] = True
        base["reason"] = ""
        return base

    def _read_preferences(self) -> dict:
        data = self._read_json(self.preferences_path, {})
        preferences = {"auto_check": True, "channel": "stable"}
        if isinstance(data, dict):
            if isinstance(data.get("auto_check"), bool):
                preferences["auto_check"] = data["auto_check"]
            if data.get("channel") in CHANNELS:
                preferences["channel"] = data["channel"]
        return preferences

    def _append_history(self, action: str, result: str, message: str, version: str) -> None:
        row = {
            "time": _utc_now(),
            "action": action,
            "result": result,
            "message": message,
            "version": version,
            "platform": _platform_name(),
            "role": self.role,
        }
        try:
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        return value if isinstance(value, type(default)) else default

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def version_text(version: Version, tag: str) -> str:
    if version.is_prerelease:
        return tag.lstrip("v")
    return f"{version.major}.{version.minor}.{version.patch}"


def _empty_download_state() -> dict:
    return {
        "active": False,
        "status": "idle",
        "version": "",
        "asset_kind": "",
        "asset_name": "",
        "arch": "",
        "sha256": "",
        "etag": "",
        "last_modified": "",
        "downloaded": 0,
        "total": 0,
        "started_at": "",
        "finished_at": "",
        "error": "",
        "path": "",
    }


class _TrustedRedirectHandler(HTTPRedirectHandler):
    """只允许重定向到受信任域名，避免清单或资产下载被引导到任意主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(str(newurl or ""))
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in ALLOWED_ASSET_HOSTS | {"api.github.com"}:
            raise HTTPError(newurl, code, "下载重定向到未授权域名", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 进程级复用；所有发布发现与资产请求统一经过受信任重定向策略。
_TRUSTED_OPENER = build_opener(_TrustedRedirectHandler())


@contextmanager
def _http_open(url: str, headers: dict[str, str], timeout: float):
    """流式打开受信任资产地址以支持 Range 续传；416 交由调用方判定是否需要重下。"""
    request = Request(url, headers=headers)
    try:
        response = _TRUSTED_OPENER.open(request, timeout=timeout)  # noqa: S310 - 地址已在清单校验阶段限定
    except HTTPError as exc:
        if exc.code == 416:
            yield 416, dict(exc.headers.items()) if exc.headers else {}, exc
            return
        raise
    with response:
        yield response.status, dict(response.headers.items()), response


def _is_reparse(path: Path) -> bool:
    """判定符号链接/目录联接。Windows 上 is_symlink() 只能识别部分重解析点。"""
    try:
        if path.is_symlink():
            return True
        info = path.lstat()
    except OSError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _tree_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _iso(timestamp: Any) -> str:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _http_request(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
    request = Request(url, headers=headers)
    try:
        with _TRUSTED_OPENER.open(request, timeout=timeout) as response:  # noqa: S310 - 固定受信任仓库
            return response.status, dict(response.headers.items()), response.read(4 * 1024 * 1024)
    except Exception as exc:
        status = getattr(exc, "code", None)
        if status == 304:
            return 304, {}, b""
        raise
