"""Velopack 下载子进程。

为什么单独进程：Python SDK 的 `download_updates` 没有取消令牌，无法安全打断在途下载。
把下载放进独立进程后，取消只需终止该进程；已完成的包与断点由 Velopack 自行管理，
不会在业务进程里留下半成品或第二套下载状态机。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _write_progress(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _percent(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _trusted_base(info: Any, backend: Any, recovery: dict, current_version: str) -> bool:
    """SDK 差分基线必须在调用 download_updates 前核验来源与实际摘要，不能只比版本号。"""
    base = getattr(info, "BaseRelease", None)
    deltas = list(getattr(info, "DeltasToTarget", None) or [])
    if base is None or not deltas or str(getattr(base, "Version", "")) != str(current_version):
        return False
    if str(getattr(base, "FileName", "")) != str(recovery.get("name") or ""):
        return False
    package = backend.packages_dir() / str(recovery["name"])
    return backend.package_matches(package, {
        "commit": str(recovery.get("commit") or ""),
        "name": str(recovery["name"]),
        "size": recovery.get("size"),
        "sha256": str(recovery.get("sha256") or ""),
    })


def _record_downloaded_package(backend: Any, target: dict, *, delta_used: bool) -> None:
    """只记录可证明来源的产物：签名 full，或已核验基线+签名 delta 的重建结果。"""
    from menu_capture.updates.velopack_identity import require_inner_manifest

    path = backend.packages_dir() / target["full"]["name"]
    if path.is_symlink() or not path.is_file():
        raise ValueError("下载完成后未找到目标包")
    # 已验证的目标缓存（例如上一次差分重建成功、安装尚未执行）直接沿用原记录，
    # 不按本次下载计划重新定性来源，避免 SDK 跳过下载后陷入重试死循环。
    existing = backend.verified_package(target["version"], target["git_commit"])
    if existing is not None and backend.package_matches(path, existing):
        return
    identity = backend.package_identity(path)
    signed = str((target.get("trust") or {}).get("mode") or "") == "ed25519"
    if identity["size"] == target["full"]["size"] and identity["sha256"] == target["full"]["sha256"]:
        source = "signed-full" if signed else "unsigned-full"
    elif delta_used:
        require_inner_manifest(path, version=target["version"], commit=target["git_commit"])
        source = "signed-delta-rebuild" if signed else "unsigned-delta-rebuild"
    else:
        raise ValueError("下载结果与正式发布身份不一致")
    backend.record_verified_package(
        version=target["version"], commit=target["git_commit"], name=identity["name"],
        size=identity["size"], sha256=identity["sha256"], source=source,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Velopack 受控下载子进程")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--channel", default="stable")
    parser.add_argument("--version", required=True)
    parser.add_argument("--progress", required=True)
    parser.add_argument("--target-json", required=True)
    args = parser.parse_args(argv)
    progress_path = Path(args.progress)
    try:
        from menu_capture.updates.velopack_backend import VelopackBackend
        from menu_capture.updates.velopack_identity import (
            require_publisher_trust, require_target, sdk_asset_identity, validate_extension,
        )

        raw = json.loads(args.target_json)
        target = validate_extension(
            raw, version=args.version, commit=str(raw.get("git_commit") or ""),
            channel=args.channel, repository=args.repo,
            tag=str(raw.get("tag") or f"v{args.version}"),
        )
        require_publisher_trust(target)

        backend = VelopackBackend(args.repo, progress_path.parent, channel=args.channel)
        manager = backend.manager(args.channel, args.version)
        info = manager.check_for_updates()
        if info is None or info.IsDowngrade:
            raise ValueError("发布源没有合法的升级目标")
        require_target(sdk_asset_identity(info.TargetFullRelease), target)

        # 下载前准备恢复包：此时 SDK 尚未清理上一版本地包，避免安装阶段重新下载旧 Full。
        current_version = str(manager.get_current_version() or "")
        current_release = backend.current_release()
        current_commit = str(current_release.get("git_commit") or "")
        if (not current_version or not current_commit
                or str(current_release.get("version") or "") != current_version):
            raise ValueError("无法确定当前安装版本与 commit，不能准备可信回滚包")
        recovery = backend.prepare_recovery_package(current_version, current_commit, target)

        # 差分基线必须与当前运行版本、恢复包身份及实际摘要完全一致，否则走官方全量。
        allowed = {(item["name"], item["sha256"], item["size"]) for item in target["deltas"]}
        deltas = list(info.DeltasToTarget or [])
        delta_trusted = (
            _trusted_base(info, backend, recovery, current_version)
            and all((sdk_asset_identity(item)["file_name"], sdk_asset_identity(item)["sha256"],
                     sdk_asset_identity(item)["size"]) in allowed for item in deltas)
        )
        if not delta_trusted:
            # 未经本次 Release 核验的历史差分链或不可信基线不直接使用；改用官方全量包路径。
            import velopack

            info = velopack.UpdateInfo(info.TargetFullRelease, [], False)

        # 同名目标包没有可信记录或摘要不符时删除，避免 SDK 跳过下载后无限重试。
        backend.discard_untrusted_cached_package(target)

        def on_progress(progress: Any = 0, *_args: Any, **_kwargs: Any) -> None:
            _write_progress(progress_path, {"stage": "downloading", "percent": _percent(progress)})

        _write_progress(progress_path, {"stage": "downloading", "percent": 0})
        manager.download_updates(info, on_progress)
        _record_downloaded_package(backend, target, delta_used=delta_trusted)
        _write_progress(progress_path, {"stage": "completed", "percent": 100})
        return 0
    except Exception as exc:  # noqa: BLE001 - 子进程必须把失败原因交给父进程展示
        _write_progress(progress_path, {
            "stage": "failed",
            "percent": 0,
            "error": str(exc) or "下载失败",
        })
        return 1


if __name__ == "__main__":
    sys.exit(main())
