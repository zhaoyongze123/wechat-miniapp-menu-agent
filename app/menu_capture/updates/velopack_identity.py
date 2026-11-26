"""Release and SDK identity checks, distinct from publisher signature verification."""
from __future__ import annotations

import hashlib
import os
import re
import zipfile
from pathlib import Path
from urllib.parse import quote

PACK_ID = "Yipu.MenuCapture"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TRUST_MODES = {"unsigned", "ed25519"}


def validate_extension(extension, *, version, commit, channel, repository, tag, published=None,
                       release_policy=None):
    if not isinstance(extension, dict) or extension.get("schema_version") != 1:
        raise ValueError("Velopack 发布扩展格式无效")
    if (extension.get("pack_id"), extension.get("platform"), extension.get("arch"),
            extension.get("channel"), extension.get("version"), extension.get("git_commit")) != (
            PACK_ID, "windows", "x64", f"win-x64-{channel}", version, commit):
        raise ValueError("Velopack 发布身份不一致")
    if not _COMMIT.fullmatch(commit):
        raise ValueError("Velopack 发布 commit 无效")
    if extension.get("tag") != tag:
        raise ValueError("Velopack 发布 tag 不一致")
    seen = set()

    def asset(item, suffix):
        if not isinstance(item, dict):
            raise ValueError("Velopack 资产格式无效")
        name = item.get("name")
        if (not isinstance(name, str) or not name.endswith(suffix)
                or any(c in name for c in "/\\:") or name.casefold() in seen):
            raise ValueError("Velopack 资产名称无效或重复")
        seen.add(name.casefold())
        size, digest = item.get("size"), item.get("sha256")
        if type(size) is not int or size <= 0 or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("Velopack 资产大小或 SHA-256 无效")
        url = f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/{quote(name, safe='')}"
        if item.get("url") != url:
            raise ValueError("Velopack 资产地址与正式 Release 不一致")
        if published is not None:
            remote = published.get(name)
            if not remote or remote.get("size") != size or remote.get("browser_download_url") != url:
                raise ValueError("Velopack 资产与 Release 不一致")
            remote_digest = remote.get("digest")
            if remote_digest and remote_digest != f"sha256:{digest}":
                raise ValueError("Velopack 资产摘要与 Release 不一致")
        return {"name": name, "size": size, "sha256": digest, "url": url}

    setup = asset(extension.get("setup"), ".exe")
    full = asset(extension.get("full"), ".nupkg")
    raw_deltas = extension.get("deltas")
    if not isinstance(raw_deltas, list):
        raise ValueError("Velopack 差分资产列表无效")
    deltas = [asset(item, ".nupkg") for item in raw_deltas]
    index = asset(extension.get("index"), ".json")
    if index["name"] != f"releases.win-x64-{channel}.json":
        raise ValueError("Velopack 索引渠道不一致")
    trust = extension.get("trust")
    if not isinstance(trust, dict) or trust.get("mode") not in _TRUST_MODES:
        raise ValueError("发布者信任模式无效")
    if trust.get("mode") == "ed25519":
        from menu_capture.updates.velopack_trust import verify_release

        verify_release({**extension, "trust": trust}, release_policy=release_policy)
    elif trust != {"mode": "unsigned"}:
        raise ValueError("未签名发布必须使用精确的 unsigned 信任声明")
    target = {"schema_version": 1, "pack_id": PACK_ID, "platform": "windows", "arch": "x64",
            "channel": extension["channel"], "version": version, "git_commit": commit,
            "tag": tag, "setup": setup, "full": full, "deltas": deltas, "index": index,
            "trust": {**trust}}
    if trust.get("mode") == "ed25519":
        target["release_policy"] = dict(extension["release_policy"])
    return target


def require_publisher_trust(target):
    """正式发布必须通过 Ed25519；unsigned 只允许显式开发豁免。"""
    trust = target.get("trust") if isinstance(target, dict) else None
    if not isinstance(trust, dict) or trust.get("mode") not in _TRUST_MODES:
        raise ValueError("发布者信任模式无效")
    if trust.get("mode") == "unsigned":
        if os.environ.get("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE") != "1":
            raise ValueError("更新资产未签名，且未显式允许未签名更新")
        return
    from menu_capture.updates.velopack_trust import verify_release

    verify_release(target)


# 旧调用名保留，语义已升级为完整发布者信任校验。
require_unsigned_opt_in = require_publisher_trust


def sdk_asset_identity(asset):
    return {"version": str(asset.Version), "pack_id": str(asset.PackageId),
            "type": str(getattr(asset, "Type", "") or ""),
            "file_name": str(asset.FileName), "sha256": str(asset.SHA256 or "").lower(),
            "size": int(asset.Size or 0)}


def require_target(actual, target):
    full = target["full"]
    expected = {"version": target["version"], "pack_id": target["pack_id"],
                "type": "Full", "file_name": full["name"],
                "sha256": full["sha256"], "size": full["size"]}
    if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
        raise ValueError("SDK 目标包与正式发布身份不一致")


def validate_local_asset(asset, *, version, commit):
    """事务中保存的本地恢复包身份：不接受缺字段或与发布 commit 不一致的记录。"""
    if not isinstance(asset, dict):
        raise ValueError("恢复包身份无效")
    name = asset.get("name")
    if not isinstance(name, str) or not name.endswith(".nupkg") or any(c in name for c in "/\\:"):
        raise ValueError("恢复包名称无效")
    size, digest = asset.get("size"), asset.get("sha256")
    if type(size) is not int or size <= 0 or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("恢复包大小或 SHA-256 无效")
    if asset.get("version") != version or asset.get("commit") != commit:
        raise ValueError("恢复包版本或 commit 与可信来源不一致")
    if not isinstance(asset.get("path"), str) or not asset["path"]:
        raise ValueError("恢复包路径缺失")
    if not isinstance(asset.get("source"), dict) or not asset["source"]:
        raise ValueError("恢复包来源缺失")
    if not isinstance(asset.get("inner_manifest"), dict):
        raise ValueError("恢复包内部发布清单缺失")
    return asset


def inner_release_manifest(nupkg: Path) -> dict:
    """读取 nupkg 内的 release-manifest.json，验证包内容确实对应目标构建。"""
    try:
        with zipfile.ZipFile(nupkg) as archive:
            hits = [item for item in archive.namelist()
                    if Path(item).name == "release-manifest.json" and not item.endswith("/")]
            if len(hits) != 1:
                raise ValueError("恢复包内部发布清单不唯一")
            payload = archive.read(hits[0])
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("恢复包格式无效或缺少内部发布清单") from exc
    import json

    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("恢复包内部发布清单无法解析") from exc
    if not isinstance(manifest, dict):
        raise ValueError("恢复包内部发布清单格式无效")
    return manifest


def require_full_file(root: Path, target):
    """Hash actual bytes, not merely SDK metadata; reject reparse points."""
    full = target["full"]
    name = full["name"]
    if not name or any(c in name for c in "/\\:") or name in {".", ".."}:
        raise ValueError("完整包名称无效")
    path = root / name
    for item in (root, path):
        if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("完整包路径包含重解析点")
    if not path.is_file() or path.stat().st_size != full["size"]:
        raise ValueError("完整包大小不一致")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != full["sha256"]:
            raise ValueError("完整包 SHA-256 不一致")
    return path
