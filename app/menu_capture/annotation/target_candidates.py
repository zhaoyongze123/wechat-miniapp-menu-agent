"""Build local click candidates so the model chooses an ID, not a coordinate.

OCR rows are captured at 3x for small button text (see ``_read_scaled_ocr``).
This module converts them back to screenshot pixels, merges split fragments on
the same visual line and assigns stable per-frame IDs such as ``T3``.  The
model only returns ``target_id``; the execution layer takes the candidate box
center.  No candidate is fabricated when the OCR evidence is weak.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Iterable

OCR_SCALE = 3
MAX_CANDIDATES = 60

_PRICE_ONLY = re.compile(
    r"^[¥￥]?\s*\d+(?:[.,]\d+)?\s*(?:元|起)?\s*(?:/\s*(?:份|个|杯|例|包|串|碗|盒|只))?\s*(?:起)?$"
)
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}$")
_PHONE_ONLY = re.compile(r"^\+?\d[\d\s()-]{6,}$")
_URL_TEXT = re.compile(r"https?://|www\.", re.IGNORECASE)
_PEOPLE_NUMBER = re.compile(r"^\d{1,2}(?:人|位)?$")
_BUTTON_HINTS = (
    "确定", "确认", "开始点餐", "开始点单", "开始选餐", "开始点菜", "去点餐", "查看菜单", "进入菜单",
    "同意", "允许", "关闭", "跳过", "知道了", "我知道了", "继续", "下一步",
    "好的", "重试", "重新加载", "刷新",
)
_NEGATIVE_HINTS = ("不同意", "不允许", "拒绝", "暂不", "稍后", "取消")


def _normalize(text: Any) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    return re.sub(r"\s+", " ", value)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _kind(text: str) -> str:
    compact = _compact(text)
    if _PEOPLE_NUMBER.fullmatch(compact):
        return "number"
    if any(hint in compact for hint in _BUTTON_HINTS):
        return "button"
    return "text"


def _position_hint(center_x: float, center_y: float, width: int, height: int) -> str:
    vertical = "上方" if center_y < height / 3 else "中部" if center_y < height * 2 / 3 else "下方"
    horizontal = "左侧" if center_x < width / 3 else "中间" if center_x < width * 2 / 3 else "右侧"
    return vertical + horizontal


def _is_noise(text: str) -> bool:
    compact = _compact(text)
    if not compact or len(compact) > 20:
        return True
    if "¥" in text or "￥" in text:
        return True
    if _TIME_ONLY.fullmatch(compact) or _PHONE_ONLY.fullmatch(compact) or _URL_TEXT.search(text):
        return True
    # 价格不是可点按钮；人数纯数字仍保留给人数网格。
    if _PRICE_ONLY.fullmatch(compact) and not _PEOPLE_NUMBER.fullmatch(compact):
        return True
    return False


def _rows_in_image_pixels(rows: Iterable[dict], image_size: tuple[int, int]) -> list[dict]:
    width, height = image_size
    output: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        text = _normalize(item.get("text"))
        x = _number(item.get("x"))
        y = _number(item.get("y"))
        box_w = _number(item.get("width"))
        box_h = _number(item.get("height"))
        if not text or None in (x, y, box_w, box_h):
            continue
        x, y = x / OCR_SCALE, y / OCR_SCALE
        box_w, box_h = box_w / OCR_SCALE, box_h / OCR_SCALE
        if box_w <= 0 or box_h <= 0:
            continue
        if x < 0 or y < 0 or x + box_w > width + 1 or y + box_h > height + 1:
            continue
        confidence = _number(item.get("confidence"))
        # 人数网格的小号数字常被判为 0.3 低置信度；先保留纯数字行，
        # 等到确认这是人数网格后再决定是否作为候选。
        if confidence is not None and confidence < 0.35 and not re.fullmatch(r"\d{1,2}", text):
            continue
        output.append({
            "text": text,
            "x": x,
            "y": y,
            "width": box_w,
            "height": box_h,
            "bottom": y + box_h,
            "confidence": confidence if confidence is not None else 1.0,
        })
    output.sort(key=lambda row: (row["y"], row["x"]))
    return output


def _merge_same_line(rows: list[dict], image_width: int) -> list[dict]:
    """Join OCR fragments that sit on one visual line without inventing rows."""
    merged: list[dict] = []
    for row in rows:
        target = None
        for candidate in reversed(merged[-4:]):
            previous = candidate
            tolerance = max(3.0, min(previous["height"], row["height"]) * 0.45)
            if abs((previous["y"] + previous["height"] / 2) - (row["y"] + row["height"] / 2)) > tolerance:
                continue
            gap = row["x"] - (previous["x"] + previous["width"])
            if -2.0 <= gap <= max(6.0, image_width * 0.015):
                target = candidate
                break
        if target is None:
            merged.append(dict(row))
            continue
        right = max(target["x"] + target["width"], row["x"] + row["width"])
        bottom = max(target["bottom"], row["bottom"])
        target.update({
            "text": (target["text"] + row["text"]).strip(),
            "width": right - target["x"],
            "height": bottom - target["y"],
            "bottom": bottom,
            "confidence": min(target["confidence"], row["confidence"]),
        })
    return merged


def build_text_candidates(
    rows: Iterable[dict],
    image_size: tuple[int, int],
    *,
    max_candidates: int = MAX_CANDIDATES,
) -> list[dict]:
    """Return deterministic ``T*`` candidates in screenshot pixel coordinates."""
    width, height = image_size
    if width <= 0 or height <= 0:
        return []
    merged = _merge_same_line(_rows_in_image_pixels(rows, image_size), width)
    number_labels = {
        int(re.match(r"^(\d{1,2})", _compact(row["text"])).group(1))
        for row in merged
        if re.match(r"^(\d{1,2})(?:人|位)?$", _compact(row["text"]))
    }
    # 三个以上不同数字才视为人数网格；单个“28”更可能是价格，不提供为点击候选。
    people_grid = len({value for value in number_labels if 1 <= value <= 12}) >= 3
    selected: list[dict] = []
    for row in merged:
        text = row["text"]
        compact = _compact(text)
        number_match = re.match(r"^(\d{1,2})(?:人|位)?$", compact)
        if number_match:
            number = int(number_match.group(1))
            if not 1 <= number <= 12 or (not people_grid and "人" not in compact and "位" not in compact):
                continue
        if any(hint in compact for hint in _NEGATIVE_HINTS):
            continue
        if _is_noise(text):
            continue
        # 人数网格里的单个数字字号明显小于正文；按正文阈值会把 1-8 整排过滤掉，
        # 只留下 9/10/12，模型因此无法选择配置人数。人数候选单独放宽下限。
        min_height = 6.0 if (number_match and people_grid) else max(8.0, height * 0.008)
        if row["height"] < min_height or row["height"] > height * 0.22:
            continue
        if row["width"] > width * 0.75:
            continue
        center_x = row["x"] + row["width"] / 2
        center_y = row["y"] + row["height"] / 2
        if not (0 <= center_x < width and 0 <= center_y < height):
            continue
        selected.append({
            "id": "",
            "kind": _kind(text),
            "text": text[:80],
            "position": _position_hint(center_x, center_y, width, height),
            "box_image_px": {
                "x": round(row["x"], 2), "y": round(row["y"], 2),
                "width": round(row["width"], 2), "height": round(row["height"], 2),
            },
            "center_image_px": {"x": round(center_x, 2), "y": round(center_y, 2)},
            "confidence": round(float(row["confidence"]), 3),
        })
        if len(selected) >= max(1, int(max_candidates)):
            break
    return [{**item, "id": f"T{index}"} for index, item in enumerate(selected, start=1)]


def candidate_by_id(candidates: Iterable[dict] | None, target_id: str) -> dict | None:
    if not target_id:
        return None
    for item in candidates or []:
        if isinstance(item, dict) and item.get("id") == target_id:
            return item
    return None


def candidate_by_label(candidates: Iterable[dict] | None, label: str) -> dict | None:
    """Unique exact-label fallback for models that omit target_id."""
    key = _compact(_normalize(label))
    if not key:
        return None
    matches = [item for item in candidates or []
               if isinstance(item, dict) and _compact(_normalize(item.get("text"))) == key]
    return matches[0] if len(matches) == 1 else None


def candidate_table(candidates: Iterable[dict] | None, *, limit: int = 40) -> str:
    items = [item for item in (candidates or []) if isinstance(item, dict)]
    lines = []
    for item in items[:limit]:
        suffix = "｜禁用" if item.get("available") is False else ""
        lines.append(f"{item.get('id')}｜{item.get('kind')}｜{str(item.get('text', ''))[:40]}｜{item.get('position')}{suffix}")
    if len(items) > limit:
        lines.append(f"（另有 {len(items) - limit} 个候选未列出）")
    return "\n".join(lines)
