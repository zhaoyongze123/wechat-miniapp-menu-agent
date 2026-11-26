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
            require_full_file, require_publisher_trust, require_target, sdk_asset_identity, validate_extension,
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
        allowed = {(item["name"], item["sha256"], item["size"]) for item in target["deltas"]}
        deltas = list(info.DeltasToTarget or [])
        if any((sdk_asset_identity(item)["file_name"], sdk_asset_identity(item)["sha256"],
                sdk_asset_identity(item)["size"]) not in allowed for item in deltas):
            # 未经本次 Release 核验的历史差分链不直接信任；改用官方全量包路径。
            import velopack

            info = velopack.UpdateInfo(info.TargetFullRelease, [], False)

        def on_progress(progress: Any = 0, *_args: Any, **_kwargs: Any) -> None:
            _write_progress(progress_path, {"stage": "downloading", "percent": _percent(progress)})

        _write_progress(progress_path, {"stage": "downloading", "percent": 0})
        manager.download_updates(info, on_progress)
        require_full_file(backend._packages_dir(), target)
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
