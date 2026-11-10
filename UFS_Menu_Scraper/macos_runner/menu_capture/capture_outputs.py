"""采集目录按清洗批次名称归档，批次改名和切换版本不移动已有结果。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def _batch_label(batch_name: str) -> str:
    """批次名称直接作为目录名；非法字符转义，空名回退到批次 ID。"""
    name = batch_name if isinstance(batch_name, str) else ""
    return re.sub(r"[\x00-\x1f/\\:]", "_", name).strip(" .")[:120]


def batch_output_dir(output_root: str | Path, batch_id: str = "", batch_name: str = "") -> Path:
    root = Path(output_root).expanduser().resolve()
    if not batch_id:
        return root
    if not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        raise ValueError("清洗批次 ID 无效")
    grouped = root / "清洗批次"
    directory = grouped / (_batch_label(batch_name) or batch_id)
    if grouped.is_symlink() or directory.is_symlink():
        raise ValueError("清洗批次输出目录不能是符号链接")
    return directory


def store_output_dir(output_root: str | Path, source_name: str, batch_id: str = "",
                     batch_name: str = "") -> Path:
    parent = batch_output_dir(output_root, batch_id, batch_name)
    name = re.sub(r"[\x00-\x1f/\\:]", "_", source_name).strip(" .") or "未命名来源"
    if not batch_id:
        return parent / name[:120]
    if name != source_name or len(name.encode("utf-8")) > 240:
        # 常规门店目录保持原名。需要转义或截断时保留名称哈希，避免合并不同门店。
        prefix = name.encode("utf-8")[:220].decode("utf-8", errors="ignore")
        name = prefix + "-" + hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:16]
    return parent / name
