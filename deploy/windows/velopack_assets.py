"""从 vpk 生成的索引识别本次交付资产，不推测 nupkg 文件名。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

PACK_ID = "Yipu.MenuCapture"
REPOSITORY = "zhaoyongze123/wechat-miniapp-menu-agent"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?\Z")
STABLE_VERSION = re.compile(r"\d+\.\d+\.\d+\Z")


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


def release_assets(root: Path, version: str, *, full_only: bool = False, delta_base: str = "") -> list[str]:
    root = Path(root)
    index_name = f"releases.{release_channel(version)}.json"
    index = _asset_path(root, index_name)
    payload = json.loads(index.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("Assets"), list):
        raise ValueError("Velopack 索引格式无效")
    full: list[str] = []
    deltas: list[str] = []
    bases: list[str] = []
    seen: set[str] = set()
    if delta_base and (full_only or release_channel(delta_base) != "win-x64-stable"
                       or release_channel(version) != "win-x64-stable"
                       or tuple(map(int, delta_base.split("."))) >= tuple(map(int, version.split(".")))):
        raise ValueError("Delta 基线必须低于目标正式版本，且不能与 FullOnly 同用")
    for asset in payload["Assets"]:
        if not isinstance(asset, dict):
            raise ValueError("Velopack 索引条目无效")
        if delta_base and asset.get("Version") != version:
            if asset.get("Version") != delta_base or asset.get("Type") != "Full" or asset.get("PackageId") != PACK_ID:
                raise ValueError("显式 Delta 索引只能包含目标版本和指定基线 Full")
            base_path = _asset_path(root, asset.get("FileName"))
            _verify_index_asset(base_path, asset)
            bases.append(base_path.name)
            continue
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
    if delta_base and (len(bases) != 1 or len(deltas) != 1):
        raise ValueError("显式 Delta 构建必须包含唯一基线 Full 和唯一目标 Delta")
    setups = sorted(root.glob("*-Setup.exe"))
    if len(setups) != 1 or _asset_path(root, setups[0].name).stat().st_size <= 0:
        raise ValueError("必须存在唯一且非空的 Setup.exe")
    return [setups[0].name, full[0], *sorted(deltas), index_name]


def signed_baseline(manifest_path: Path, version: str) -> dict:
    """复用客户端的发布者签名检查，拒绝调试豁免或非正式渠道基线。"""
    from menu_capture.updates.velopack_identity import validate_extension
    from menu_capture.updates.velopack_trust import release_policy_from_manifest

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not STABLE_VERSION.fullmatch(version) or manifest.get("version") != version or manifest.get("channel") != "stable":
        raise ValueError("Delta 基线发布清单版本或渠道不一致")
    commit = manifest.get("git_commit")
    if not isinstance(commit, str):
        raise ValueError("Delta 基线缺少发布 commit")
    target = validate_extension(
        manifest.get("velopack"), version=version, commit=commit, channel="stable", repository=REPOSITORY,
        tag=f"v{version}", release_policy=release_policy_from_manifest(manifest, allow_empty_assets=True),
    )
    if target["trust"]["mode"] != "ed25519":
        raise ValueError("Delta 基线必须经过发布者 Ed25519 签名")
    full = target["full"]
    # 文件名会作为 gh --pattern 参数，不能包含通配符。
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.nupkg", full["name"]):
        raise ValueError("Delta 基线 Full 文件名无效")
    return {**full, "commit": commit}


def verify_baseline(package: Path, asset: dict, version: str) -> None:
    from menu_capture.updates.velopack_identity import require_inner_manifest

    if package.name != asset["name"]:
        raise ValueError("Delta 基线 Full 文件名与签名清单不符")
    _verify_index_asset(package, {"Size": asset["size"], "SHA256": asset["sha256"]})
    require_inner_manifest(package, version=version, commit=asset["commit"])


def verify_rebuild(full: Path, rebuilt: Path) -> None:
    """包容器可以重排压缩条目，但重建后的每个安装文件必须与目标 Full 字节一致。"""
    def lib_hashes(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if not item.filename.startswith("lib/") or item.is_dir():
                    continue
                if item.filename in result:
                    raise ValueError(f"存在重复的安装文件: {item.filename}")
                with archive.open(item) as stream:
                    result[item.filename] = hashlib.file_digest(stream, "sha256").hexdigest()
        return result

    expected = lib_hashes(full)
    if not expected or expected != lib_hashes(rebuilt):
        raise ValueError("Delta 重建的安装文件与目标 Full 不一致")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("version")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--full-only", action="store_true")
    modes.add_argument("--delta-base", default="")
    modes.add_argument("--signed-baseline", action="store_true")
    modes.add_argument("--verify-baseline", type=Path, metavar="NUPKG")
    modes.add_argument("--verify-rebuild", type=Path, metavar="NUPKG")
    args = parser.parse_args()
    if args.verify_rebuild:
        verify_rebuild(args.path, args.verify_rebuild)
    elif args.signed_baseline or args.verify_baseline:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
        asset = signed_baseline(args.path, args.version)
        if args.verify_baseline:
            verify_baseline(args.verify_baseline, asset, args.version)
        else:
            print(asset["name"])
    else:
        print(json.dumps(release_assets(args.path, args.version, full_only=args.full_only, delta_base=args.delta_base)))


if __name__ == "__main__":
    main()
