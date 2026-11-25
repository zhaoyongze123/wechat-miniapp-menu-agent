"""素材模块的纯辅助函数。

包含哈希、路径校验、原子裁剪、OCR 归属与用途判定；这些函数不依赖
MaterialService 状态，从 materials 拆出以降低单文件职责，materials 仍按原名字再导出。
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from menu_capture.materials.preprocess import CROP_MARGIN_RATIO, _crop_candidate

logger = logging.getLogger(__name__)

# OCR 关键词分三级：明确点餐 > 明确非点餐 > 泛化的“下单”，避免团购文案被当成点餐。
_STRONG_ORDERING_TERMS = (
    "扫码点餐", "扫码点单", "扫码点菜", "点餐", "点单", "点菜",
    "订餐", "餐桌码", "桌码", "点餐码", "在线点餐", "自助点餐", "立即点餐",
    "小程序点餐", "扫码菜单",
)
_WEAK_ORDERING_TERMS = ("扫码下单", "立即下单")
_NON_ORDERING_TERMS = (
    "企业微信", "企业号", "关注公众号", "微信公众号", "添加好友", "加好友",
    "进群", "加群", "收款码", "付款码", "微信支付", "核销",
    "抢购", "团购", "买单", "入群", "吐槽", "打卡", "收藏", "买券",
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (ValueError, OSError):
        return False


def _store_id(source_folder: str) -> str:
    return hashlib.sha256(source_folder.encode("utf-8")).hexdigest()[:24]


def _mkdir_ok(path: Path) -> None:
    """建目录，且不对已存在路径做 stat。

    Windows 上安全软件/过滤驱动会偶发拒绝 stat（WinError 5）；pathlib 的
    exist_ok=True 在 mkdir 抛 FileExistsError 后会再 stat 一次，把“目录已存在”
    放大成裁剪失败。这里直接捕获 FileExistsError 视为成功。
    """
    try:
        path.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        return


def _atomic_crop(source: Path, box: dict, destination: Path,
                 margin_ratio: float = CROP_MARGIN_RATIO) -> None:
    """原子写入裁剪图：预览服务可能短暂占用目标文件，先写临时文件再替换；失败重试一次。"""
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.png")
    try:
        for attempt in (0, 1):
            try:
                _crop_candidate(source, box, temporary, margin_ratio)
                os.replace(temporary, destination)
                return
            except OSError as exc:
                logger.warning(
                    "crop atomic write failed attempt=%s winerror=%s errno=%s filename=%s dest_exists=%s",
                    attempt, getattr(exc, "winerror", None), getattr(exc, "errno", None),
                    Path(getattr(exc, "filename", "") or "").name, destination.exists(),
                )
                if attempt:
                    raise
                time.sleep(0.2)
    finally:
        temporary.unlink(missing_ok=True)


def _selected_margin_ratio(selected: dict) -> float:
    """人工框选严格按框裁剪；旧记录缺字段时按 candidate_index 推断，避免历史版本继续扩边。"""
    if not isinstance(selected, dict):
        return CROP_MARGIN_RATIO
    value = selected.get("crop_margin_ratio")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return 0.0 if selected.get("candidate_index") is None else CROP_MARGIN_RATIO


def _assigned_ocr_texts(candidates: list[dict], items: list[dict],
                        max_ratio: float = 3.0) -> dict[int, str]:
    """把每条 OCR 文字只归给最近的二维码，避免同一段文字被多个码共用。

    固定扩边会把同一张海报上的团购、返利文案算给旁边的点餐码；按中心距离归属后，
    一条文字只有一个主人。距离上限按码尺寸归一化，避免把远处文字拉进来。
    """
    rects: list[tuple[float, float, float, float] | None] = []
    for candidate in candidates:
        try:
            box = candidate["box"]
            rects.append(tuple(float(box[key]) for key in ("x", "y", "width", "height")))
        except (KeyError, TypeError, ValueError):
            rects.append(None)
    grouped: list[list[tuple[float, float, str]]] = [[] for _ in rects]
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            text_x, text_y, text_width, text_height = (
                float(item[key]) for key in ("x", "y", "width", "height")
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (text_x, text_y, text_width, text_height)):
            continue
        if text_width <= 0 or text_height <= 0:
            continue
        center_x, center_y = text_x + text_width / 2, text_y + text_height / 2
        best, best_score = None, float("inf")
        for index, rect in enumerate(rects):
            if rect is None:
                continue
            x, y, width, height = rect
            size = max(width, height, 1.0)
            score = math.hypot(center_x - (x + width / 2), center_y - (y + height / 2)) / size
            if score < best_score:
                best, best_score = index, score
        if best is not None and best_score <= max_ratio:
            grouped[best].append((text_y, text_x, item["text"]))
    assigned = {}
    for index, rows in enumerate(grouped):
        rows.sort(key=lambda row: (row[0], row[1]))
        assigned[index] = " ".join(text for _y, _x, text in rows)
    return assigned


def _classify_local_purpose(text: str) -> tuple[str, str]:
    """用二维码附近的本地 OCR 文本判断用途；没有明确点餐字样时不猜。

    顺序是“强点餐 → 非点餐 → 弱下单”：团购卡上的“扫码下单 享优惠”会先命中
    “抢购/团购”，不会被当成点餐。
    """
    normalized = re.sub(r"[\W_]+", "", text.casefold())
    for term in _STRONG_ORDERING_TERMS:
        if re.sub(r"[\W_]+", "", term.casefold()) in normalized:
            return "ordering", f"周边 OCR 识别到“{term}”"
    for term in _NON_ORDERING_TERMS:
        if re.sub(r"[\W_]+", "", term.casefold()) in normalized:
            return "not_ordering", "周边 OCR 识别到非点餐用途标识"
    for term in _WEAK_ORDERING_TERMS:
        if re.sub(r"[\W_]+", "", term.casefold()) in normalized:
            return "ordering", f"周边 OCR 识别到“{term}”"
    return "unknown", "二维码周边没有识别到明确点餐标识，需人工确认"
