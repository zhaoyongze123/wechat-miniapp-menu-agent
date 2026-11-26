"""Release and SDK identity checks, distinct from publisher signature verification."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import quote

PACK_ID = "Yipu.MenuCapture"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def validate_extension(extension, *, version, commit, channel, repository, tag, published=None):
    if not isinstance(extension, dict) or extension.get("schema_version") != 1:
        raise ValueError("Velopack 发布扩展格式无效")
    if (extension.get("pack_id"), extension.get("platform"), extension.get("arch"),
            extension.get("channel"), extension.get("version"), extension.get("git_commit")) != (
            PACK_ID, "windows", "x64", f"win-x64-{channel}", version, commit):
        raise ValueError("Velopack 发布身份不一致")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Velopack 发布 commit 无效")
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
    if trust != {"mode": "unsigned"}:
        raise ValueError("当前尚未接入可信签名验证，拒绝未知签名模式")
    return {"schema_version": 1, "pack_id": PACK_ID, "platform": "windows", "arch": "x64",
            "channel": extension["channel"], "version": version, "git_commit": commit,
            "setup": setup, "full": full, "deltas": deltas, "index": index, "trust": trust}


def require_unsigned_opt_in(target):
    if target.get("trust") != {"mode": "unsigned"}:
        raise ValueError("发布者签名验证尚未接入")
    if os.environ.get("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE") != "1":
        raise ValueError("更新资产未签名，且未显式允许未签名更新")


def sdk_asset_identity(asset):
    return {"version": str(asset.Version), "pack_id": str(asset.PackageId),
            "file_name": str(asset.FileName), "sha256": str(asset.SHA256 or "").lower(),
            "size": int(asset.Size or 0)}


def require_target(actual, target):
    full = target["full"]
    expected = {"version": target["version"], "pack_id": target["pack_id"],
                "file_name": full["name"], "sha256": full["sha256"], "size": full["size"]}
    if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
        raise ValueError("SDK 目标包与正式发布身份不一致")


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
