"""Normalize the runtime and remove smoke-test bytecode before release packaging."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
from pathlib import Path, PurePosixPath


def _sha256_record(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _remove_bytecode(root: Path) -> int:
    bytecode: list[Path] = []
    for path in root.rglob("*.pyc"):
        if not path.is_file():
            continue
        try:
            source = Path(importlib.util.source_from_cache(str(path)))
        except ValueError:
            continue
        # 只有可由随包源码重新生成的缓存才能删除；pyc-only 模块保留为运行代码。
        if source.is_file():
            bytecode.append(path)
    for path in bytecode:
        path.unlink()
    cache_dirs = sorted(
        (path for path in root.rglob("__pycache__") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in cache_dirs:
        try:
            directory.rmdir()
        except OSError:
            pass
    return len(bytecode)


def prepare_python_bundle(root: Path, *, app_root: Path | None = None) -> dict[str, int]:
    """清理解释器缓存、未进入运行 PATH 的 pip 启动器和冒烟测试生成的应用缓存。"""
    root = Path(root).resolve()
    scripts = root / "Scripts"
    if not root.is_dir():
        raise ValueError(f"Python bundle directory does not exist: {root}")
    if app_root is not None:
        app_root = Path(app_root).resolve()
        if not app_root.is_dir():
            raise ValueError(f"Application bundle directory does not exist: {app_root}")

    launchers = [path for path in scripts.glob("*.exe") if path.is_file()]
    launcher_names = {path.name.lower(): path for path in launchers}
    records = sorted(
        path for path in root.rglob("RECORD")
        if path.is_file() and path.parent.name.lower().endswith(".dist-info")
    )
    removed_record_refs: set[str] = set()
    record_updates: list[tuple[Path, list[list[str]]]] = []
    for record in records:
        with record.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        kept: list[list[str]] = []
        removed: set[str] = set()
        for row in rows:
            if not row:
                kept.append(row)
                continue
            relative = PurePosixPath(row[0])
            if (
                len(row) == 3
                and relative.parts[:2] == ("..", "..")
                and relative.parts[2:3] == ("Scripts",)
                and relative.suffix.lower() == ".exe"
            ):
                name = relative.name.lower()
                launcher = launcher_names.get(name)
                if launcher is not None and (
                    row[1] != _sha256_record(launcher)
                    or row[2] != str(launcher.stat().st_size)
                ):
                    raise ValueError(f"Python script launcher RECORD mismatch: {record}")
                removed.add(name)
                removed_record_refs.add(name)
                continue
            kept.append(row)
        if removed:
            record_updates.append((record, kept))

    unrecorded = sorted(set(launcher_names) - removed_record_refs)
    if unrecorded:
        raise ValueError(f"Python script launcher is missing from RECORD: {unrecorded}")

    for record, rows in record_updates:
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        temporary = record.with_suffix(record.suffix + ".tmp")
        temporary.write_text(output.getvalue(), encoding="utf-8", newline="")
        temporary.replace(record)

    for launcher in launchers:
        launcher.unlink()

    python_bytecode = _remove_bytecode(root)
    app_bytecode = _remove_bytecode(app_root) if app_root is not None else 0
    return {
        "python_bytecode_files_removed": python_bytecode,
        "app_bytecode_files_removed": app_bytecode,
        "script_launchers_removed": len(launchers),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("python_root", type=Path, help="Python runtime directory inside the bundle")
    parser.add_argument("--app-root", type=Path, help="Application directory inside the bundle")
    args = parser.parse_args()
    result = prepare_python_bundle(args.python_root, app_root=args.app_root)
    print(
        "Prepared Python bundle: "
        f"removed {result['python_bytecode_files_removed']} runtime bytecode files, "
        f"{result['app_bytecode_files_removed']} app bytecode files and "
        f"{result['script_launchers_removed']} unused script launchers."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
