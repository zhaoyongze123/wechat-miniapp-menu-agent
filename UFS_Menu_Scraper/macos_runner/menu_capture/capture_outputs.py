"""采集目录按清洗批次名称归档，批次改名和切换版本不移动已有结果。"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime
from pathlib import Path


def _batch_label(batch_name: str) -> str:
    """批次名称直接作为目录名；非法字符转义，空名回退到批次 ID。"""
    name = batch_name if isinstance(batch_name, str) else ""
    return re.sub(r"[\x00-\x1f/\\:]", "_", name).strip(" .")[:120]


def _store_segment(source_name: str) -> str:
    """门店目录名与素材目录保持一致，只转义路径非法字符。"""
    return re.sub(r"[\x00-\x1f/\\:]", "_", source_name).strip(" .") or "未命名来源"


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
                     batch_name: str = "", flat: bool = False) -> Path:
    # flat 用于“保存目录即成品目录”的场景：门店直接作为一级子目录。
    parent = Path(output_root).expanduser().resolve() if flat else batch_output_dir(output_root, batch_id, batch_name)
    name = _store_segment(source_name)
    if flat or not batch_id:
        return parent / name[:120]
    if name != source_name or len(name.encode("utf-8")) > 240:
        # 常规门店目录保持原名。需要转义或截断时保留名称哈希，避免合并不同门店。
        prefix = name.encode("utf-8")[:220].decode("utf-8", errors="ignore")
        name = prefix + "-" + hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:16]
    return parent / name


def archive_unqualified(output_root: str | Path, source_folder: str | Path, source_name: str, *,
                        status: str, stage: str, message: str, code: str = "") -> Path:
    """把不合格门店的原图整目录复制到不合格目录，并写一份人可读的门店信息。

    原图只复制不移动，原始素材目录保持不变，便于复查与重新采集。
    """
    root = Path(output_root).expanduser().resolve()
    destination = root / _store_segment(source_name)[:120]
    destination.mkdir(parents=True, exist_ok=True)
    source = Path(source_folder)
    copied = 0
    if source.is_dir():
        for item in source.iterdir():
            target = destination / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif item.is_file():
                shutil.copy2(item, target)
            else:
                continue
            copied += 1
    info = "\n".join([
        f"原始店名：{source_name}",
        f"原始目录：{source}",
        f"处理结果：{status}",
        f"处理阶段：{stage}",
        f"原因码：{code}",
        f"失败原因：{message}",
        f"原图条目数：{copied}",
        f"归档时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
    ])
    (destination / "归档说明.txt").write_text(info, encoding="utf-8")
    return destination
