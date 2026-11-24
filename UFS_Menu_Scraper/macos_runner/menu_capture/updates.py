"""应用内更新：受信任发布发现、清单校验、偏好、审计与本地版本清点。

边界：
- 只访问固定仓库的正式 Release，不接受前端传入 URL、命令或版本路径；
- SHA-256 仅用于完整性校验，发布者可信度依赖固定仓库与正式 Release 语义；
- 本阶段不执行下载/安装/切换，避免在采集期间引入未经验证的高权限执行链路。
"""
from __future__ import annotations

import json
import os
import platform as platform_module
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import total_ordering
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

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
# GitHub Release 上附带的更新清单；与安装目录内的运行时 release-manifest.json 区分。
MANIFEST_ASSET = "update-manifest.json"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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
# 程序目录默认取包所在目录的上一级（macos_runner）；与数据目录严格分离，不参与反推。
DEFAULT_APP_ROOT = Path(__file__).resolve().parent.parent


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


def _has_asset_for_platform(latest: dict, platform: str, arch: str) -> bool:
    kind = {"windows": "windows_installer", "linux": "hub_image"}.get(platform)
    if not kind or not arch:
        return False
    return any(
        isinstance(asset, dict) and asset.get("kind") == kind
        and asset.get("platform") == platform
        # 不仅看清单字段，还要与本机真实架构归一化后匹配，ARM64 不会拿到 x64 资产。
        and _normalize_arch(asset.get("arch")) == arch
        for asset in latest.get("assets") or []
    )


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
        self._lock = threading.RLock()
        self._fetcher = fetcher or _http_request
        self._clock = clock
        self._cache = self._read_json(self.cache_path, {})
        self._preferences = self._read_preferences()
        self._checking = False
        self._check_done: threading.Event | None = None
        # 渠道切换时递增；在途检查写回前必须确认代号未变化，否则丢弃旧结果。
        self._generation = 0

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
            "stale": _is_stale(cache, now),
            "error": str(cache.get("error") or ""),
            "checking": checking,
            "capabilities": {
                "check": True,
                # 下载/安装/切换执行器尚未纳入本阶段，UI 必须如实展示为不支持。
                "download": False,
                "install": False,
                "rollback": False,
                "inventory": self._inventory_supported(),
            },
            "inventory": self.inventory(),
        }

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
            "cleanup_supported": False,
            "cleanup_reason": "自动清理执行器尚未启用；当前仅提供安全清点与保护规则预览",
        }

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
                    latest_cache.update({"checked_at": now, "error": "", "backoff_until": 0})
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
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定受信任仓库与 GitHub 资产域名
            return response.status, dict(response.headers.items()), response.read(4 * 1024 * 1024)
    except Exception as exc:
        status = getattr(exc, "code", None)
        if status == 304:
            return 304, {}, b""
        raise
