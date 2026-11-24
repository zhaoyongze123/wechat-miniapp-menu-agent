"""把已有 Windows 备份转换为共享后台目录。源文件不动，验证通过才生成目标目录。"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import shutil
import sqlite3
import sys
import tempfile


CONTAINER_HOME = PurePosixPath("/var/lib/menu-capture")
DATABASE = "tasks.sqlite3"
PATH_COLUMNS = {
    "tasks": ("source_path", "output_dir"),
    "material_batches": ("source_root",),
    "publish_transactions": ("output_root", "staging", "destination", "backup"),
}
JSON_COLUMNS = {
    "material_batches": ("summary_json", "manifest_json"),
    "material_decisions": ("selected_json", "store_json"),
    "run_actions": ("details",),
}


def absolute_path(value: str) -> PurePath | None:
    if PureWindowsPath(value).is_absolute():
        return PureWindowsPath(value)
    if PurePosixPath(value).is_absolute():
        return PurePosixPath(value)
    return None


@dataclass(frozen=True)
class Mapping:
    original: PurePath
    backup: Path
    target: PurePosixPath

    def convert(self, source: PurePath) -> PurePosixPath | None:
        if type(source) is not type(self.original) or not source.is_relative_to(self.original):
            return None
        relative = source.relative_to(self.original)
        if ".." in relative.parts:
            raise ValueError(f"备份引用包含上级目录跳转：{source}")
        return self.target.joinpath(*relative.parts)


def make_mapping(original: str, backup: Path, target: str) -> Mapping:
    old_path = absolute_path(original)
    if old_path is None or not backup.is_dir():
        raise ValueError(f"映射需要原机器的绝对路径和存在的备份目录：{original} -> {backup}")
    return Mapping(old_path, backup.resolve(), CONTAINER_HOME / target)


def relocate(value, mappings: list[Mapping], *, required: bool = False):
    if isinstance(value, list):
        return [relocate(item, mappings) for item in value]
    if isinstance(value, dict):
        return {key: relocate(item, mappings) for key, item in value.items()}
    if not isinstance(value, str) or not value:
        return value
    source = absolute_path(value)
    for mapping in mappings:
        if source is not None and (target := mapping.convert(source)) is not None:
            return str(target)
    if required:
        raise ValueError(f"未找到原目录映射：{value}。请提供对应备份及 --extra 映射。")
    return value


def table_counts(db: sqlite3.Connection) -> dict[str, int]:
    names = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return {name: db.execute(f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
            for (name,) in names.fetchall()}


def rewrite_table(db: sqlite3.Connection, table: str, columns: tuple[str, ...],
                  mappings: list[Mapping], *, structured: bool) -> None:
    present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for column in (name for name in columns if name in present):
        rows = db.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} <> ''")
        updates = []
        for rowid, old in rows:
            value = json.loads(old) if structured else old
            new = relocate(value, mappings, required=not structured)
            if new != value:
                updates.append((json.dumps(new, ensure_ascii=False) if structured else new, rowid))
        if updates:
            db.executemany(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", updates)


def native_tree_path(path: Path) -> Path:
    # 素材目录携带批次、门店和文件名，Windows 备份复制后可能超过默认路径长度。
    # 转换仅用于文件操作，数据库仍保存容器内的标准绝对路径。
    if os.name != "nt":
        return path
    value = str(path.absolute())
    prefix = "\\\\?\\"
    if value.startswith(prefix):
        return path
    if value.startswith("\\\\"):
        return Path(prefix + "UNC\\" + value[2:])
    return Path(prefix + value)


def copy_verified_tree(source: Path, destination: Path, *, exclude: set[str] = frozenset()) -> None:
    # Windows 联接点和符号链接不能当作备份数据。避免跨根目录带入额外文件。
    if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
        raise ValueError(f"备份目录包含链接：{source}")
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in exclude:
            continue
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError(f"备份目录包含链接：{item}")
        target = destination / item.name
        if item.is_dir():
            copy_verified_tree(item, target)
        elif item.is_file():
            shutil.copy2(item, target)
        else:
            raise ValueError(f"备份目录中存在非常规文件：{item}")


def sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def staged_path(path: str | PurePosixPath, root: Path) -> Path:
    target = PurePosixPath(path)
    if not target.is_relative_to(CONTAINER_HOME) or ".." in target.parts:
        raise ValueError(f"路径不在共享后台数据目录：{path}")
    return root.joinpath(*target.relative_to(CONTAINER_HOME).parts)


def verify_files(db: sqlite3.Connection, root: Path, tables: dict[str, int]) -> dict[str, int]:
    screenshots: set[Path] = set()
    verified_qr: dict[Path, str] = {}
    if "tasks" in tables:
        for path, digest, output, status in db.execute("SELECT source_path, source_hash, output_dir, status FROM tasks"):
            qr = staged_path(path, root)
            if qr.is_file():
                if qr not in verified_qr:
                    verified_qr[qr] = sha256(qr)
                if verified_qr[qr] != digest and "published" in qr.parts:
                    raise ValueError(f"发布快照二维码校验失败：{qr.name}")
            if status == "captured":
                files = set(staged_path(output, root).glob("page-*.png"))
                if not files:
                    raise ValueError(f"已采集门店的备份中没有截图：{output}")
                screenshots.update(files)

    if "material_versions" in tables:
        snapshots = db.execute(
            """SELECT DISTINCT v.snapshot_id FROM material_versions v
               JOIN material_batches b ON b.id = v.batch_id WHERE b.deleted_at = ''"""
        ).fetchall()
        for (snapshot_id,) in snapshots:
            if len(snapshot_id) != 64 or any(char not in "0123456789abcdef" for char in snapshot_id):
                raise ValueError("发布快照 ID 不合法")
            directory = root / "data" / "materials" / "published" / snapshot_id
            marker = json.loads((directory / ".materials-snapshot.json").read_text(encoding="utf-8"))
            if marker.get("snapshot_id") != snapshot_id:
                raise ValueError(f"发布快照标记不匹配：{snapshot_id}")
            for image in marker.get("artifacts", []):
                relative = PurePosixPath(image["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("发布快照标记引用了目录以外的文件")
                path = directory.joinpath(*relative.parts)
                if path not in verified_qr:
                    verified_qr[path] = sha256(path)
                if verified_qr[path] != image["sha256"]:
                    raise ValueError(f"发布快照文件校验失败：{snapshot_id}/{relative}")
    return {"checked_qr_files": len(verified_qr), "captured_screenshot_files": len(screenshots)}


def migrate(args: argparse.Namespace) -> dict:
    backup_home = args.backup_home.resolve(strict=True)
    backup_output = args.backup_output.resolve(strict=True)
    destination = args.destination.resolve()
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("目标目录必须尚不存在，且其父目录必须已存在")
    settings_path = backup_home / "config.local.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig")) if settings_path.is_file() else {}
    old_output = args.legacy_output or settings.get("output_dir", "")
    mappings = [
        make_mapping(args.legacy_home or str(backup_home), backup_home, ""),
        make_mapping(old_output, backup_output, "output"),
    ]
    for index, (original, backup) in enumerate(args.extra, 1):
        mappings.append(make_mapping(original, Path(backup), f"legacy-sources/{index}"))
    if len({mapping.original for mapping in mappings}) != len(mappings):
        raise ValueError("同一个原目录不能有两个备份映射")
    if any(destination.is_relative_to(mapping.backup) for mapping in mappings):
        raise ValueError("迁移目标不能位于源备份内")
    mappings.sort(key=lambda mapping: len(mapping.original.parts), reverse=True)

    source_db = (backup_home / "data" / DATABASE).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="menu-capture-migration-",
                                     dir=native_tree_path(destination.parent)) as temporary:
        staged = Path(temporary) / "ready"
        staged.mkdir(mode=0o700)
        copy_verified_tree(native_tree_path(backup_home / "data"), staged / "data",
                           exclude={DATABASE, f"{DATABASE}-wal", f"{DATABASE}-shm", "runner.lock"})
        copy_verified_tree(native_tree_path(backup_output), staged / "output")
        for mapping in mappings:
            if mapping.target.parent == CONTAINER_HOME / "legacy-sources":
                copy_verified_tree(native_tree_path(mapping.backup), staged_path(mapping.target, staged))
        with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as old_db, \
                closing(sqlite3.connect(staged / "data" / DATABASE)) as db:
            old_db.backup(db)
            before = table_counts(db)
            if before.get("publish_transactions", 0):
                raise ValueError("备份含未完成的截图发布事务。请让旧工作台恢复事务后重新备份。")
            with db:
                for table, columns in PATH_COLUMNS.items():
                    rewrite_table(db, table, columns, mappings, structured=False)
                for table, columns in JSON_COLUMNS.items():
                    rewrite_table(db, table, columns, mappings, structured=True)
                if db.execute("PRAGMA integrity_check").fetchone() != ("ok",) or table_counts(db) != before:
                    raise ValueError("迁移后的 SQLite 完整性或记录数校验失败")
                report = {"rows": before, **verify_files(db, staged, before)}

        settings = relocate(settings, mappings)
        settings["output_dir"] = str(CONTAINER_HOME / "output")
        for key in ("input_dir", "unqualified_dir"):
            value = settings.get(key, "")
            if value:
                staged_path(value, staged)
            else:
                settings[key] = ""
        (staged / "config.local.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        secrets = backup_home / "secrets.local.json"
        if secrets.is_file():
            shutil.copyfile(secrets, staged / secrets.name)
            os.chmod(staged / secrets.name, 0o600)
        (staged / "migration-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        staged.rename(destination)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-home", type=Path, required=True, help="包含 data/ 的旧采集端备份")
    parser.add_argument("--backup-output", type=Path, required=True, help="旧截图目录的备份")
    parser.add_argument("--legacy-home", help="原机器上采集端的绝对路径")
    parser.add_argument("--legacy-output", help="原机器上截图目录的绝对路径，默认读取配置")
    parser.add_argument("--extra", action="append", nargs=2, default=[], metavar=("原路径", "备份目录"),
                        help="额外的来源素材或不合格归档备份，可重复")
    parser.add_argument("--destination", type=Path, required=True, help="写入尚不存在的迁移目标目录")
    try:
        report = migrate(parser.parse_args())
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"迁移未完成：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
