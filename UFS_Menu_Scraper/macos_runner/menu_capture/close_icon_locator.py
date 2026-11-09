"""Refine a model-provided point to a nearby circular X close icon."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


_METHOD = "hough_circle_cross_v1"
# 深色圆形底上是白 X，白底广告上是深灰/黑 X；两种极性都接受，但笔画本身必须够极端，
# 否则照片纹理的高对比边缘会被误判成交叉笔画。
_LIGHT_STROKE_LEVEL = 150.0
_DARK_STROKE_LEVEL = 120.0
_MIN_STROKE_JUMP = 12.0


def _cross_support(gray: np.ndarray, cx: int, cy: int, radius: int) -> float:
    """Measure whether both diagonal strokes cross near the circle center."""
    line_hits: list[float] = []
    contrast_hits: list[float] = []
    # 沿两条对角线采样，并与另一条对角线上的像素比较；这能排除仅有圆环的常见按钮。
    # 采样点落在哪条对角线上，背景就必须取另一条：否则会把同一笔画的像素当成背景。
    for t in np.linspace(max(2.0, radius * 0.16), radius * 0.48, 8):
        for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            x, y = round(cx + dx * t), round(cy + dy * t)
            if not (2 <= x < gray.shape[1] - 2 and 2 <= y < gray.shape[0] - 2):
                continue
            # 每条笔划附近至少有一个极端像素，容忍抗锯齿和 1px 的中心误差。
            patch = gray[y - 1 : y + 2, x - 1 : x + 2]
            bright = float(np.percentile(patch, 85))
            dark = float(np.percentile(patch, 15))
            anti = (int(gray[y - 2, x + 2]) + int(gray[y + 2, x - 2])) / 2
            main = (int(gray[y - 2, x - 2]) + int(gray[y + 2, x + 2])) / 2
            background = anti if dx == dy else main
            light_jump = bright - background
            dark_jump = background - dark
            light = bright >= _LIGHT_STROKE_LEVEL and light_jump >= _MIN_STROKE_JUMP
            deep = dark <= _DARK_STROKE_LEVEL and dark_jump >= _MIN_STROKE_JUMP
            line_hits.append(float(light or deep))
            jump = light_jump if light else dark_jump if deep else 0.0
            contrast_hits.append(float(np.clip((jump - _MIN_STROKE_JUMP) / 65, 0, 1)))
    if len(line_hits) < 24:
        return 0.0
    return 0.55 * float(np.mean(line_hits)) + 0.45 * float(np.mean(contrast_hits))


def refine_close_icon(path: str | Path, x: float, y: float) -> dict | None:
    """Return the nearest high-confidence circular-X center, or ``None``.

    ``x`` and ``y`` are a model-provided coarse point in screenshot pixels. Search
    stays inside a width-scaled local ROI (capped at 82 px per side); callers should
    retain their original point when this function returns ``None``.
    """
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    try:
        coarse_x, coarse_y = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(coarse_x) or not np.isfinite(coarse_y):
        return None

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    if not (0 <= coarse_x < width and 0 <= coarse_y < height):
        return None

    # 窗口像素宽度决定控件尺度；限制 ROI 边长，避免退化为整页模板扫描。
    half = min(82, max(42, round(width * 0.20)))
    left, right = max(0, round(coarse_x) - half), min(width, round(coarse_x) + half + 1)
    top, bottom = max(0, round(coarse_y) - half), min(height, round(coarse_y) + half + 1)
    roi = image[top:bottom, left:right]
    if min(roi.shape[:2]) < 18:
        return None

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    min_radius = max(6, round(width * 0.022))
    max_radius = min(32, max(min_radius, round(width * 0.075)))

    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=max(8, round(width * 0.035)),
        param1=80,
        param2=max(16, round(width * 0.035)),
        minRadius=min_radius,
        maxRadius=max_radius,
    )
    candidates = [] if circles is None else [tuple(int(v) for v in np.rint(circle)) for circle in circles[0]]

    accepted: list[tuple[float, float, int, int, int]] = []
    for cx, cy, radius in candidates:
        image_x, image_y = cx + left, cy + top
        distance = float(np.hypot(image_x - coarse_x, image_y - coarse_y))
        if distance > half or radius < min_radius or radius > max_radius:
            continue
        cross = _cross_support(gray, cx, cy, radius)
        if cross < 0.62:
            continue
        # 交叉纹理是主置信度，距离仅用于多个真实候选间的轻微排序。
        score = min(1.0, 0.78 + 0.22 * cross - 0.04 * distance / half)
        accepted.append((score, distance, image_x, image_y, radius))

    if not accepted:
        return None
    score, _, center_x, center_y, radius = max(accepted, key=lambda item: (item[0], -item[1]))
    return {
        "center_image_px": {"x": center_x, "y": center_y},
        "box": [center_x - radius, center_y - radius, center_x + radius + 1, center_y + radius + 1],
        "score": round(score, 4),
        "method": _METHOD,
    }


def locate_close_icon(path: str | Path, coarse_x: float | None = None,
                      coarse_y: float | None = None) -> dict | None:
    """找整页唯一可信的圆形 X 关闭图标。

    广告弹窗的关闭按钮常在弹窗下方较远处，模型粗坐标偏差超过局部 ROI 上限；
    整页搜索只在“明确是关闭动作且页面有弹窗”时由调用方使用。广告图里的品牌
    logo、菜品插画同样是圆形且纹理交叉，因此不按分最高取，而是取距模型粗坐标
    最近的可信候选——模型坐标纵使有偏差，通常仍指向弹窗本身而非内部插图。
    """
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    if width < 40 or height < 40:
        return None
    try:
        anchor_x, anchor_y = float(coarse_x), float(coarse_y)
    except (TypeError, ValueError):
        anchor_x = anchor_y = None
    if anchor_x is None or anchor_y is None or not (np.isfinite(anchor_x) and np.isfinite(anchor_y)):
        anchor_x = anchor_y = None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    min_radius = max(6, round(width * 0.022))
    max_radius = min(32, max(min_radius, round(width * 0.075)))
    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=max(10, round(width * 0.06)),
        param1=80,
        param2=max(18, round(width * 0.045)),
        minRadius=min_radius,
        maxRadius=max_radius,
    )
    if circles is None:
        return None
    accepted: list[tuple[float, int, int, int]] = []
    for cx, cy, radius in (tuple(int(v) for v in np.rint(circle)) for circle in circles[0]):
        if not (0 <= cx < width and 0 <= cy < height):
            continue
        if cy - radius < 0 or cy + radius >= height or cx - radius < 0 or cx + radius >= width:
            continue
        cross = _cross_support(gray, cx, cy, radius)
        if cross < 0.68:
            continue
        # 广告弹窗的关闭键是白色/灰色圆底上的灰 X；彩色装饰圆（星星、金币）饱和度明显更高。
        yy, xx = np.ogrid[-radius:radius + 1, -radius:radius + 1]
        disc = hsv[cy - radius:cy + radius + 1, cx - radius:cx + radius + 1][xx * xx + yy * yy <= radius * radius]
        if float(disc[:, 1].mean()) > 40.0:
            continue
        accepted.append((cross, cx, cy, radius))
    if not accepted:
        return None
    if anchor_x is None:
        cross, center_x, center_y, radius = max(accepted, key=lambda item: item[0])
    else:
        cross, center_x, center_y, radius = min(
            accepted, key=lambda item: (float(np.hypot(item[1] - anchor_x, item[2] - anchor_y)), -item[0]))
    return {
        "center_image_px": {"x": center_x, "y": center_y},
        "box": [center_x - radius, center_y - radius, center_x + radius + 1, center_y + radius + 1],
        "score": round(min(1.0, 0.78 + 0.22 * cross), 4),
        "method": "hough_circle_cross_page_v1",
    }


def refine_back_arrow(path: str | Path, x: float, y: float) -> dict | None:
    """在左上角窄 ROI 内定位返回箭头（< / ‹）的笔画中心。

    模型对这类无文字图标的纵向坐标常偏低 10~30px。这里只在模型点原本就落在
    左上角时生效，找竖直细长的深色连通块并取离模型点最近者的质心，避免误覆盖
    页面中间的文字型“返回”按钮。
    """
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    try:
        coarse_x, coarse_y = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(coarse_x) and np.isfinite(coarse_y)):
        return None
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    if width < 40 or height < 40 or coarse_x > width * 0.3 or coarse_y > height * 0.12:
        return None
    roi_w = max(24, min(90, round(width * 0.26)))
    roi_h = max(28, min(120, round(height * 0.1)))
    gray = cv2.cvtColor(image[:roi_h, :roi_w], cv2.COLOR_BGR2GRAY)
    mask = (gray < 120).astype(np.uint8)
    if int(mask.sum()) < 8:
        return None
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    candidates: list[tuple[float, float, float, int, int]] = []
    for index in range(1, count):
        x0, y0, w, h, area = (int(v) for v in stats[index])
        if area < 6 or area > 400:
            continue
        if w < 3 or w > 24 or h < 6 or h > 32 or h <= w:
            continue
        # 紧贴边界的连通块是窗口描边或状态栏，不是箭头。
        if y0 <= 1 or x0 <= 1:
            continue
        cx, cy = (float(v) for v in centroids[index])
        if cx > roi_w * 0.5:
            continue
        candidates.append((float(np.hypot(cx - coarse_x, cy - coarse_y)), cx, cy, w, h))
    if not candidates:
        return None
    distance, cx, cy, w, h = min(candidates, key=lambda item: item[0])
    return {
        "center_image_px": {"x": round(cx, 2), "y": round(cy, 2)},
        "box": [round(cx - w / 2, 1), round(cy - h / 2, 1), round(cx + w / 2, 1), round(cy + h / 2, 1)],
        "score": 0.9,
        "method": "dark_glyph_back_arrow_v1",
    }
