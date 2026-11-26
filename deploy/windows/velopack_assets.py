"""从 vpk 生成的索引识别本次交付资产，不推测 nupkg 文件名。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

PACK_ID = "Yipu.MenuCapture"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?\Z")


def release_channel(version: str) -> str:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("无效的构建版本")
    return "win-x64-preview" if "-" in version else "win-x64-stable"


def _asset_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or name in {".", ".."} or any(c in name for c in "/\\:"):
        raise ValueError("索引资产名称无效")
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"缺少本次发布资产: {name}")
    return path


def _verify_index_asset(path: Path, asset: dict) -> None:
    size = asset.get("Size")
    if type(size) is not int or size <= 0 or path.stat().st_size != size:
        raise ValueError(f"索引资产大小不一致: {path.name}")
    expected = asset.get("SHA256") or asset.get("SHA1")
    algorithm = "sha256" if asset.get("SHA256") else "sha1"
    length = 64 if algorithm == "sha256" else 40
    if not isinstance(expected, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", expected):
        raise ValueError(f"索引资产摘要无效: {path.name}")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, algorithm).hexdigest()
    if actual != expected.lower():
        raise ValueError(f"索引资产摘要不一致: {path.name}")


def release_assets(root: Path, version: str, *, full_only: bool = False) -> list[str]:
    root = Path(root)
    index_name = f"releases.{release_channel(version)}.json"
    index = _asset_path(root, index_name)
    payload = json.loads(index.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("Assets"), list):
        raise ValueError("Velopack 索引格式无效")
    full: list[str] = []
    deltas: list[str] = []
    seen: set[str] = set()
    for asset in payload["Assets"]:
        if not isinstance(asset, dict):
            raise ValueError("Velopack 索引条目无效")
        if asset.get("Version") != version:
            continue
        if asset.get("PackageId") != PACK_ID or asset.get("Type") not in {"Full", "Delta"}:
            raise ValueError("本次版本的索引包身份无效")
        name = asset.get("FileName")
        path = _asset_path(root, name)
        if path.suffix.lower() != ".nupkg" or name.casefold() in seen:
            raise ValueError("本次版本的索引包重复或类型无效")
        _verify_index_asset(path, asset)
        seen.add(name.casefold())
        (full if asset["Type"] == "Full" else deltas).append(name)
    if len(full) != 1:
        raise ValueError("索引必须包含唯一的本次版本 Full 包")
    if full_only and deltas:
        # 一次性全量过渡必须由索引层面确认没有本次版本 Delta，而不是只看文件名。
        raise ValueError("FullOnly 构建不得包含本次版本 Delta")
    setups = sorted(root.glob("*-Setup.exe"))
    if len(setups) != 1 or _asset_path(root, setups[0].name).stat().st_size <= 0:
        raise ValueError("必须存在唯一且非空的 Setup.exe")
    return [setups[0].name, full[0], *sorted(deltas), index_name]


if __name__ == "__main__":
    args = sys.argv[1:]
    full_only = '--full-only' in args
    args = [arg for arg in args if arg != '--full-only']
    if len(args) != 2:
        raise SystemExit("usage: velopack_assets.py RELEASE_DIR VERSION [--full-only]")
    print(json.dumps(release_assets(Path(args[0]), args[1], full_only=full_only)))
