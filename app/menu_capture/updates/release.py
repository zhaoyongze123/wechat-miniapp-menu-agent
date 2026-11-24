"""正式 Release 的版本顺序、资产约束与清单发现。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_REPOSITORY = "zhaoyongze123/wechat-miniapp-menu-agent"


CHANNELS = {"stable", "preview"}


ASSET_KINDS = {"windows_installer", "hub_image"}


ALLOWED_ASSET_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


MAX_RELEASE_NOTES = 8000


MANIFEST_ASSET = "update-manifest.json"


_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _normalize_arch(value: Any) -> str:
    """把 x64/amd64/x86_64、arm64/aarch64 等写法归一化为统一架构名。"""
    return _ARCH_ALIASES.get(str(value or "").strip().lower(), "")


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


def version_text(version: Version, tag: str) -> str:
    if version.is_prerelease:
        return tag.lstrip("v")
    return f"{version.major}.{version.minor}.{version.patch}"


def select_release(releases: list[dict], channel: str, *, fetch) -> dict | None:
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
        status, _, body = fetch(url, {"Accept": "application/json", "User-Agent": "menu-capture-updater"}, 10.0)
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
