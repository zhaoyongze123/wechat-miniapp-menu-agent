"""人工标注的坐标映射、目标框与截图变化判定。"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from .schema import CATEGORIES, CATEGORY_PAGE_STATE


# 只用于给出“建议”，不参与任何自动点击决策。命中越多只代表越像该类别。
CATEGORY_KEYWORDS = {
    "close_icon": {"跳过": 6, "关闭": 5, "广告": 5, "×": 5, "领券": 4, "优惠券": 4, "暂不": 5, "我知道了": 4},
    "system_notification": {"通知": 6, "推送": 5, "定位": 4, "位置": 3, "蓝牙": 3, "无线数据": 3, "允许": 1},
    "people_grid": {"用餐人数": 6, "就餐人数": 6, "选择人数": 6, "请选择": 2, "人数": 4, "几位": 4},
    "consent_checkbox": {"隐私": 5, "协议": 5, "同意": 4, "授权": 4, "允许": 1},
    "resting_menu": {"菜单": 5, "点餐": 4, "菜品": 4, "购物车": 4, "价格": 3, "分类": 3, "¥": 3, "￥": 3},
    # 门店入口与门店选择：后续门店列表、定位授权都会走到这两个场景。
    "miniapp_home": {"开始点餐": 6, "开始点单": 6, "堂食": 4, "自取": 4, "外带": 4, "门店首页": 5},
    "store_select": {"选择门店": 6, "切换门店": 6, "附近门店": 5, "门店列表": 5, "当前门店": 4, "距您": 4},
}


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数字")
    return result


def map_box_to_window(box: Any, image_size: tuple[float, float],
                      window_size: tuple[float, float]) -> dict:
    """把截图像素框换算成 UiPath 窗口 points；点击坐标取框中心。"""
    if not isinstance(box, dict):
        raise ValueError("框选区域无效")
    x = _number(box.get("x"), "x")
    y = _number(box.get("y"), "y")
    width = _number(box.get("width"), "width")
    height = _number(box.get("height"), "height")
    image_width, image_height = float(image_size[0]), float(image_size[1])
    window_width, window_height = float(window_size[0]), float(window_size[1])
    if min(image_width, image_height, window_width, window_height) <= 0:
        raise ValueError("截图或窗口尺寸无效")
    if width < 4 or height < 4:
        raise ValueError("框选区域太小，请重新拖动")
    if x < -1 or y < -1 or x + width > image_width + 1 or y + height > image_height + 1:
        raise ValueError("框选区域超出截图")
    x = max(0.0, x)
    y = max(0.0, y)
    width = min(width, image_width - x)
    height = min(height, image_height - y)
    center_x, center_y = x + width / 2, y + height / 2
    scale_x, scale_y = window_width / image_width, window_height / image_height
    return {
        "box_px": {"x": round(x, 1), "y": round(y, 1), "width": round(width, 1), "height": round(height, 1)},
        "center_px": {"x": round(center_x, 1), "y": round(center_y, 1)},
        "center_points": {"x": round(center_x * scale_x, 2), "y": round(center_y * scale_y, 2)},
        "scale": {"x": round(scale_x, 6), "y": round(scale_y, 6)},
        "image_size": {"width": int(image_width), "height": int(image_height)},
        "window_size": {"width": round(window_width, 2), "height": round(window_height, 2)},
    }


def _row_box(row: dict) -> dict | None:
    try:
        box = {"x": float(row["x"]), "y": float(row["y"]),
               "width": float(row["width"]), "height": float(row["height"])}
    except (KeyError, TypeError, ValueError):
        return None
    if box["width"] <= 0 or box["height"] <= 0:
        return None
    return box


def _pad_box(box: dict, pad: float, image_width: float, image_height: float) -> dict:
    x = max(0.0, box["x"] - pad)
    y = max(0.0, box["y"] - pad)
    right = min(image_width, box["x"] + box["width"] + pad)
    bottom = min(image_height, box["y"] + box["height"] + pad)
    return {"x": round(x, 1), "y": round(y, 1),
            "width": round(max(0.1, right - x), 1), "height": round(max(0.1, bottom - y), 1)}


def suggest_target(rows: Any, image_width: float, image_height: float) -> dict | None:
    """根据本地 OCR 给一个可修改的建议；没有把握时返回 None。"""
    if not isinstance(rows, list):
        return None
    scores = {category: 0 for category in CATEGORIES}
    hits: dict[str, list[dict]] = {category: [] for category in CATEGORIES}
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = _normalize_text(row.get("text", ""))
        if not text:
            continue
        for category, keywords in CATEGORY_KEYWORDS.items():
            for keyword, weight in keywords.items():
                if keyword.casefold() in text:
                    scores[category] += weight
                    hits[category].append({"row": row, "weight": weight, "text": text})
                    break
        # “1人 / 12位”这类人数格子没有类别关键词，单独补分。
        if re.search(r"\d{1,2}\s*[人位]", text):
            scores["people_grid"] += 3
            hits["people_grid"].append({"row": row, "weight": 3, "text": text})
    category = max(CATEGORIES, key=lambda item: scores[item])
    if scores[category] < 3 or not hits[category]:
        return None
    best = max(hits[category], key=lambda item: (item["weight"], len(item["text"])))
    box = _row_box(best["row"])
    if box is None:
        return None
    padded = _pad_box(box, max(6.0, min(image_width, image_height) * 0.008), image_width, image_height)
    return {
        "category": category,
        "page_state": CATEGORY_PAGE_STATE[category],
        "target": best["text"][:80],
        "box_px": padded,
        "matched_text": best["text"][:120],
        "score": scores[category],
        "confidence": round(min(0.95, 0.3 + scores[category] / 16), 2),
    }


_EFFECT_GLOBAL_THRESHOLD = 1.5


_EFFECT_ROI_RATIO = 0.02


_EFFECT_ROI_MIN_PIXELS = 24


_EFFECT_ROI_PIXEL_DIFF = 12


def _effect_roi(size: tuple[int, int], box: dict | None, center: dict | None) -> tuple[int, int, int, int]:
    """以实际点击位置为中心的局部窗口；有目标框时按框高收紧，避免小变化被稀释。"""
    width, height = size
    if not center:
        return (0, 0, width, height)
    try:
        center_x = float(center.get("x") or 0)
        center_y = float(center.get("y") or 0)
    except (TypeError, ValueError):
        return (0, 0, width, height)
    pad = 28.0
    if box:
        try:
            pad = max(16.0, min(36.0, float(box.get("height") or 0) * 1.5))
        except (TypeError, ValueError):
            pad = 28.0
    left = max(0, int(center_x - pad))
    top = max(0, int(center_y - pad))
    right = min(width, int(center_x + pad))
    bottom = min(height, int(center_y + pad))
    if right - left < 8 or bottom - top < 8:
        return (0, 0, width, height)
    return (left, top, right, bottom)


def _roi_change(before: "Image.Image", after: "Image.Image", roi: tuple[int, int, int, int]) -> tuple[float, int]:
    difference = ImageChops.difference(before.convert("L").crop(roi), after.convert("L").crop(roi))
    total = max(1, difference.width * difference.height)
    # 直方图统计，避免逐像素遍历整图。
    histogram = difference.histogram()
    changed = sum(histogram[_EFFECT_ROI_PIXEL_DIFF:])
    return round(changed / total, 4), changed


def image_effect(before_path: Path, after_path: Path, *, box: dict | None = None,
                 center: dict | None = None) -> dict:
    """用点击前后截图的差异判断这次点击是否可能生效；只做记录，不自动判定对错。"""
    try:
        with Image.open(before_path) as before, Image.open(after_path) as after:
            if before.size != after.size:
                return {"changed": True, "diff_score": None, "reason": "screenshot_size_changed"}
            difference = ImageChops.difference(before.convert("RGB"), after.convert("RGB"))
            score = round(float(ImageStat.Stat(difference.convert("L")).mean[0]), 2)
            size = before.size
            roi = _effect_roi(size, box, center)
            roi_ratio, roi_pixels = _roi_change(before, after, roi)
    except Exception as exc:  # 图片读取失败不能阻断标注流程
        return {"changed": None, "diff_score": None, "reason": type(exc).__name__}
    # 只有真正缩到点击目标附近时才按局部判据，否则几十个像素的噪声也会误判成“有变化”。
    localized = roi != (0, 0, size[0], size[1])
    changed = score >= _EFFECT_GLOBAL_THRESHOLD or (
        localized and (roi_ratio >= _EFFECT_ROI_RATIO or roi_pixels >= _EFFECT_ROI_MIN_PIXELS))
    return {"changed": changed, "diff_score": score, "threshold": _EFFECT_GLOBAL_THRESHOLD,
            "roi_ratio": roi_ratio, "roi_pixels": roi_pixels, "roi_threshold": _EFFECT_ROI_RATIO,
            "roi": list(roi), "roi_localized": localized}
