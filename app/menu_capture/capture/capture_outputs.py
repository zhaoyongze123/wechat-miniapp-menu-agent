"""采集目录按清洗批次名称归档，批次改名和切换版本不移动已有结果。

同时负责把不合格门店的原图与归档说明复制到独立目录，供人工复核；
不参与任务状态判断，只处理文件落点与命名。
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime
from pathlib import Path

from menu_capture.projects.paths import project_batch_dir, project_store_dir


def _batch_label(batch_name: str) -> str:
    """批次名称直接作为目录名；非法字符转义，空名回退到批次 ID。"""
    name = batch_name if isinstance(batch_name, str) else ""
    return re.sub(r"[\x00-\x1f/\\:]", "_", name).strip(" .")[:120]


def _store_segment(source_name: str) -> str:
    """门店目录名与素材目录保持一致，只转义路径非法字符。"""
    return re.sub(r"[\x00-\x1f/\\:]", "_", source_name).strip(" .") or "未命名来源"


def batch_output_dir(output_root: str | Path, batch_id: str = "", batch_name: str = "",
                     *, project_code: str | None = None) -> Path:
    if project_code is not None:
        return project_batch_dir(output_root, project_code, batch_id)
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
                     batch_name: str = "", flat: bool = False, *,
                     project_code: str | None = None, store_id: str = "") -> Path:
    if project_code is not None:
        return project_store_dir(output_root, project_code, batch_id, store_id, flat=flat)
    if store_id:
        raise ValueError("门店项目身份缺失")
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


def unqualified_store_dir(output_root: str | Path, source_name: str, *,
                          project_code: str | None = None, batch_id: str = "", store_id: str = "") -> Path:
    """不合格归档中该门店的目录；与 archive_unqualified 使用同一命名规则。"""
    if project_code is not None:
        return project_store_dir(output_root, project_code, batch_id, store_id)
    if batch_id or store_id:
        raise ValueError("门店项目身份缺失")
    return Path(output_root).expanduser().resolve() / _store_segment(source_name)[:120]


def retire_store_dir(directory: str | Path) -> Path | None:
    """把已被当前结果取代的门店目录移到同级 `_历史`，不删除文件。

    同一门店只应出现在「合格」或「不合格」之一；重采成功或后来判不合格时，
    旧结论目录必须让位，否则两个目录的数量会同时虚高。
    """
    current = Path(directory).expanduser()
    if not current.is_dir() or current.is_symlink():
        return None
    history = current.parent / "_历史"
    history.mkdir(parents=True, exist_ok=True)
    target = history / current.name
    if target.exists():
        target = history / f"{current.name}-{datetime.now():%Y%m%d-%H%M%S-%f}"
    shutil.move(str(current), str(target))
    return target


def archive_unqualified(output_root: str | Path, source_folder: str | Path, source_name: str, *,
                        status: str, stage: str, message: str, code: str = "",
                        project_code: str | None = None, batch_id: str = "", store_id: str = "") -> Path:
    """把不合格门店的原图整目录复制到不合格目录，并写一份人可读的门店信息。

    原图只复制不移动，原始素材目录保持不变，便于复查与重新采集。
    """
    root = Path(output_root).expanduser().resolve()
    destination = unqualified_store_dir(root, source_name, project_code=project_code,
                                        batch_id=batch_id, store_id=store_id)
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
