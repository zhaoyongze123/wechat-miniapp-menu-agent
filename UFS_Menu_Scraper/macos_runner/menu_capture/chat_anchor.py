"""Find store-name OCR anchors in a WeChat chat screenshot."""

from __future__ import annotations

import re
import math
import unicodedata
from difflib import SequenceMatcher
from typing import Any


_OCR_DIGIT_MAP = str.maketrans({
    "o": "0", "〇": "0", "○": "0",
    "i": "1", "l": "1", "丨": "1",
})
_DIGIT_OR_CONFUSABLE = frozenset("0123456789o〇○il丨")


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(char for char in value if char.isalnum())


def _expected_identity(source_name: str) -> tuple[str, str, str, str] | None:
    normalized = unicodedata.normalize("NFKC", source_name).strip()
    match = re.match(r"([a-zA-Z]{1,4})([0-9]{5,12})(?![0-9])", normalized)
    if not match:
        return None
    prefix, digits = match.group(1).casefold(), match.group(2)
    suffix = normalized[match.end():]
    # 只取文件夹开头的稳定门店编码，品牌文字仅作辅助证据，不模糊匹配其他编码。
    parts = [part for part in re.split(r"[_\s]+", suffix) if part]
    brand = _normalize(parts[0]) if parts else ""
    city = _normalize(parts[1]) if len(parts) > 1 else ""
    return prefix, digits, brand, city


def _find_code(text: str, prefix: str, expected_digits: str) -> dict[str, Any] | None:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    prefix_len = len(prefix)
    for start in range(len(normalized)):
        if normalized[start:start + prefix_len] != prefix:
            continue
        if start and normalized[start - 1].isalnum():
            continue
        cursor = start + prefix_len
        # OCR sometimes inserts punctuation/spacing between the alphabetic prefix
        # and the fixed-width number. Confusion correction is restricted to digits.
        while cursor < len(normalized) and not normalized[cursor].isalnum():
            cursor += 1
        raw_digits = normalized[cursor:cursor + len(expected_digits)]
        if len(raw_digits) != len(expected_digits) or any(
            char not in _DIGIT_OR_CONFUSABLE for char in raw_digits
        ):
            continue
        observed_digits = raw_digits.translate(_OCR_DIGIT_MAP)
        if observed_digits != expected_digits:
            continue
        after = cursor + len(expected_digits)
        if after < len(normalized) and normalized[after] in _DIGIT_OR_CONFUSABLE:
            continue
        return {
            "expected": prefix + expected_digits,
            "observed": normalized[start:after],
            "normalized_observed": prefix + observed_digits,
            "digit_confusions": [
                {"observed": char, "corrected": char.translate(_OCR_DIGIT_MAP)}
                for char, expected in zip(raw_digits, expected_digits)
                if char != expected
            ],
        }
    return None


def _brand_similarity(brand: str, text: str) -> float:
    if not brand:
        return 0.0
    if brand in text:
        return 1.0
    min_size = max(1, len(brand) - 1)
    max_size = len(brand) + 1
    return max(
        (SequenceMatcher(None, brand, text[index:index + size]).ratio()
         for size in range(min_size, max_size + 1)
         for index in range(max(0, len(text) - size + 1))),
        default=0.0,
    )


def _best_token_similarity(token: str, text: str) -> float:
    """比较品牌/城市短字段，容忍 OCR 少识别一个字符。"""
    if not token:
        return 0.0
    if token in text:
        return 1.0
    return max(
        (SequenceMatcher(None, token, text[index:index + size]).ratio()
         for size in range(max(1, len(token) - 1), len(token) + 2)
         for index in range(max(0, len(text) - size + 1))),
        default=0.0,
    )


def _explicit_store_code(text: str) -> str | None:
    """只把完整可读的门店编号当作排他证据，避免同品牌串店。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    match = re.search(r"(?<![a-z0-9])[a-z]{1,4}[0-9]{5,12}(?![0-9])", normalized)
    return match.group(0) if match else None


def _is_partial_expected_code(observed: str, expected: str) -> bool:
    """判定完整可见的编号片段是否只是目标编号漏识了少量字符。"""
    if not 1 <= len(expected) - len(observed) <= 2:
        return False
    cursor = 0
    for char in expected:
        if cursor < len(observed) and observed[cursor] == char:
            cursor += 1
    return cursor == len(observed)


def _compatible(previous: dict[str, Any], current: dict[str, Any], width: int) -> bool:
    vertical_gap = current["y"] - previous["bottom"]
    max_height = max(previous["height"], current["height"], 1.0)
    if vertical_gap > max(8.0, max_height * 0.75):
        return False

    previous_left, previous_right = previous["x"], previous["x"] + previous["width"]
    current_left, current_right = current["x"], current["x"] + current["width"]
    tolerance = max(10.0, width * 0.06)
    if abs(previous_left - current_left) <= tolerance:
        return True  # left aligned wrapped lines
    if abs(previous_right - current_right) <= tolerance:
        return True  # right aligned wrapped lines

    overlap = max(0.0, min(previous_right, current_right) - max(previous_left, current_left))
    shorter = min(previous["width"], current["width"])
    if shorter > 0 and overlap / shorter >= 0.45:
        return True

    # OCR may return one visual line as adjacent text fragments. Only join fragments
    # that touch horizontally at essentially the same vertical position.
    same_line = abs(current["y"] - previous["y"]) <= max_height * 0.6
    horizontal_gap = current_left - previous_right
    return same_line and -tolerance <= horizontal_gap <= max(8.0, width * 0.025)


def find_store_name_matches(
    items: list[dict], source_name: str, width: int, image_height: int | None = None,
    message_boxes: list[dict] | None = None,
) -> list[dict]:
    """Return chat-area OCR rows matching a source store label or stable store ID.

    Stable IDs are preferred because OCR can corrupt separators and optional legal
    suffixes. Ambiguous or historical duplicates are returned to the caller, which
    can select the lowest visible anchor as the most recent occurrence.
    """
    if not isinstance(source_name, str) or width <= 0:
        return []
    expected_full = _normalize(source_name)
    if not expected_full:
        return []
    identity = _expected_identity(source_name)
    rows: list[dict[str, Any]] = []
    for item in items:
        text = str(item.get("text", "")).strip()
        try:
            x = float(item.get("x", -1))
            y = float(item.get("y", 0))
            row_width = float(item.get("width", 0))
            row_height = float(item.get("height", 0))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (x, y, row_width, row_height)):
            continue
        if y < 0 or row_width <= 0 or row_height <= 0:
            continue
        bottom = y + row_height
        if not text or x <= width * 0.255:
            continue
        if image_height is not None and bottom > image_height * 0.85:
            continue
        rows.append({
            "text": text, "normalized": _normalize(text), "x": x, "y": y,
            "width": row_width, "height": row_height, "bottom": bottom,
        })
    rows.sort(key=lambda row: (row["y"], row["x"]))

    # None 表示旧调用方尚未提供气泡分割，保持既有纯函数行为；显式传入空
    # 列表则表示本帧没有可靠气泡，必须 fail closed，不能回退到跨区域拼接。
    if message_boxes is None:
        owned_rows = [(None, rows)]
    else:
        owned_rows = []
        for box in message_boxes:
            try:
                left, top, right, bottom = (
                    float(box[key]) for key in ("left", "top", "right", "bottom")
                )
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(value) for value in (left, top, right, bottom)):
                continue
            if right <= left or bottom <= top:
                continue
            # 用 OCR 框中心判定所属气泡，避免贴边字形或下方图片文字串入消息。
            members = [
                row for row in rows
                if left <= row["x"] + row["width"] / 2 < right
                and top <= row["y"] + row["height"] / 2 < bottom
            ]
            owned_rows.append(({
                "left": left, "top": top, "right": right, "bottom": bottom,
                **({"method": box["method"]} if isinstance(box.get("method"), str) else {}),
            }, members))

    matches: list[dict[str, Any]] = []
    seen: set[tuple[float, float, str, tuple[float, float, float, float] | None]] = set()
    for message_box, message_rows in owned_rows:
        for start, first in enumerate(message_rows):
            group = [first]
            for row in message_rows[start + 1:start + 6]:
                # 气泡所有权先隔离消息；组内仍禁止把下一条编码并入本条门店文本。
                if re.match(r"[a-zA-Z]{1,4}[0-9oO〇○il丨]{5,12}", row["text"]):
                    break
                if not _compatible(group[-1], row, width):
                    break
                group.append(row)

            text = "".join(row["text"] for row in group)
            normalized_text = "".join(row["normalized"] for row in group)
            match = None
            match_method = ""
            helper = {"expected_brand": "", "matched": False, "similarity": 0.0}
            if identity:
                prefix, digits, brand, expected_city = identity
                match = _find_code(text, prefix, digits)
                if match:
                    similarity = _brand_similarity(brand, normalized_text)
                    brand_matched = bool(brand and brand in normalized_text)
                    match_method = "store_id+brand" if brand_matched else "store_id_only"
                    helper = {
                        "expected_brand": brand,
                        "matched": brand_matched,
                        "similarity": round(similarity, 3),
                        "confidence": round(similarity, 3),
                    }
                else:
                    # 完整识别到其他门店编码时禁止文字兜底；只有编码不完整/不可读时
                    # 才使用名称相似度，以容忍 OCR 漏字和局部编码误识。
                    explicit_code = _explicit_store_code(text)
                    expected_code = prefix + digits
                    if (explicit_code and explicit_code != expected_code
                            and not _is_partial_expected_code(explicit_code, expected_code)):
                        continue

                    full_similarity = SequenceMatcher(None, expected_full, normalized_text).ratio()
                    brand_similarity = _brand_similarity(brand, normalized_text)
                    city_similarity = _best_token_similarity(expected_city, normalized_text)
                    # 全串达到 0.82 且至少能从品牌/城市确认上下文，才允许文字匹配。
                    # 完整编号的排他检查在上方，防止同品牌门店被模糊命中。
                    context_matches = brand_similarity >= 0.8 or city_similarity >= 0.8
                    if full_similarity >= 0.82 and context_matches:
                        match = {
                            "expected": expected_code,
                            "observed": "",
                            "normalized_observed": "",
                            "digit_confusions": [],
                        }
                        match_method = "normalized_name_similarity"
                        helper = {
                            "expected_brand": brand,
                            "matched": context_matches,
                            "similarity": round(full_similarity, 3),
                            "confidence": round(full_similarity, 3),
                            "brand_similarity": round(brand_similarity, 3),
                            "city_similarity": round(city_similarity, 3),
                        }
            elif expected_full in normalized_text:
                match = {"expected": "", "observed": "", "normalized_observed": "", "digit_confusions": []}
                match_method = "normalized_full_label"
                helper = {"expected_brand": "", "matched": True, "similarity": 1.0}

            if not match:
                continue
            box_key = None if message_box is None else tuple(
                message_box[key] for key in ("left", "top", "right", "bottom")
            )
            key = (first["x"], first["y"], match_method, box_key)
            if key in seen:
                continue
            seen.add(key)
            result = {
                "text": text[:500], "x": first["x"], "y": first["y"],
                "bottom": message_box["bottom"] if message_box else group[-1]["bottom"],
                "match_method": match_method,
                "id_evidence": match, "helper_evidence": helper,
            }
            if message_box is not None:
                result["message_box"] = message_box
            matches.append(result)
    return matches
