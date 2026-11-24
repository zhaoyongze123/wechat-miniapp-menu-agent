"""Locate the small checkbox that belongs to a consent/agreement label."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


_METHOD = "local_consent_checkbox_square"


def refine_consent_checkbox(
    path: str | Path,
    left: float,
    top: float,
    height: float,
    width: float = 0,
) -> dict | None:
    """Return the square checkbox near the left edge of a consent label."""
    try:
        label_left, label_top, label_height = float(left), float(top), float(height)
    except (TypeError, ValueError):
        return None
    if not all(np.isfinite(value) for value in (label_left, label_top, label_height)):
        return None
    if label_height <= 0:
        return None
    try:
        label_width = max(0.0, float(width))
    except (TypeError, ValueError):
        label_width = 0.0

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return None
    image_height, image_width = image.shape[:2]
    y1 = max(0, round(label_top - label_height * 0.8))
    y2 = min(image_height, round(label_top + label_height * 1.8))
    if y2 - y1 < 8:
        return None

    def squares(x1: int, x2: int) -> list[tuple[int, int, int, int, float]]:
        x1, x2 = max(0, x1), min(image_width, x2)
        if x2 - x1 < 8:
            return []
        roi = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        found = []
        for contour in contours:
            x, y, width_px, height_px = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            if not (7 <= width_px <= 28 and 7 <= height_px <= 28):
                continue
            if not (0.65 <= width_px / max(1, height_px) <= 1.55):
                continue
            if area < 18:
                continue
            found.append((x1 + x + width_px // 2, y1 + y + height_px // 2, width_px, height_px, area))
        return found

    # 第一优先：文字框左侧的独立方框；第二优先：被 OCR 并入文字框开头的方框。
    left_squares = squares(max(0, round(label_left) - 32), round(label_left))
    inner_squares = squares(round(label_left), round(label_left + min(30.0, label_width or 30.0)))
    if left_squares:
        center_x, center_y, width_px, height_px, _ = max(left_squares, key=lambda item: (item[4], -item[0]))
    elif inner_squares:
        # 文字框内部可能有字形笔画形成的方形，选最靠左的一个。
        center_x, center_y, width_px, height_px, _ = min(inner_squares, key=lambda item: (item[0], -item[4]))
    else:
        return None

    return {
        "center_image_px": {"x": center_x, "y": center_y},
        "box": [center_x - width_px // 2, center_y - height_px // 2,
                center_x + width_px // 2 + 1, center_y + height_px // 2 + 1],
        "score": 0.9,
        "method": _METHOD,
    }
