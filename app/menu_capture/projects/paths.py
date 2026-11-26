"""Stable project/batch/store storage paths. Never derive identity from display names."""
import hashlib
from pathlib import Path
import re
import stat

from menu_capture.projects.service import _RESERVED


def _safe_child(parent: Path, segment: str) -> Path:
    child = parent / segment
    try:
        info = child.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and (stat.S_ISLNK(info.st_mode) or
                             getattr(info, "st_file_attributes", 0) & 0x400 or
                             not stat.S_ISDIR(info.st_mode)):
        raise ValueError("项目输出路径不是普通目录")
    if child.resolve().parent != parent.resolve():
        raise ValueError("项目输出路径超出允许目录")
    return child


def project_batch_dir(root: str | Path, project_code: str, batch_id: str) -> Path:
    if (not isinstance(project_code, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,48}", project_code)
            or (project_code.lower() in _RESERVED and project_code != "legacy")):
        raise ValueError("项目目录标识无效")
    if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        raise ValueError("清洗批次 ID 无效")
    parent = _safe_child(Path(root).expanduser().resolve(), project_code)
    return _safe_child(parent, batch_id)


def project_store_dir(root: str | Path, project_code: str, batch_id: str, store_id: str,
                      *, flat: bool = False) -> Path:
    if not isinstance(store_id, str) or not store_id.strip():
        raise ValueError("门店身份缺失")
    batch = project_batch_dir(root, project_code, batch_id)
    # Full hash preserves case-sensitive IDs even on case-insensitive Windows filesystems.
    digest = hashlib.sha256(store_id.encode("utf-8")).hexdigest()
    if flat:
        return _safe_child(batch.parent, f"{batch_id}-{digest}")
    return _safe_child(batch, digest)
