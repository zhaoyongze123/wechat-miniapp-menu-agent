from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from menu_capture.close_icon_locator import refine_close_icon


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "action-evidence"
REAL_CASES = (
    ("416243709fb340f78e923473f3dde0d9.png", 283.6, 690.5, (288, 689)),
    ("21d540b0b3394f209c409aee3d26d541.png", 218.84, 732.4, (220, 753)),
    ("b86c9a9398054a559f6e974eba4a394b.png", 152.5, 533.8, (155, 506)),
)


@pytest.mark.parametrize("filename,coarse_x,coarse_y,expected", REAL_CASES)
def test_refines_real_ad_close_icons(filename, coarse_x, coarse_y, expected):
    image_path = EVIDENCE / filename
    if not image_path.is_file():
        pytest.skip(f"evidence screenshot is unavailable: {image_path}")

    found = refine_close_icon(image_path, coarse_x, coarse_y)

    assert found is not None
    center = found["center_image_px"]
    assert abs(center["x"] - expected[0]) <= 2
    assert abs(center["y"] - expected[1]) <= 2
    assert found["score"] >= 0.90
    assert found["method"] == "hough_circle_cross_v1"
    assert len(found["box"]) == 4


@pytest.mark.parametrize("width,scale", [(320, 1.0), (500, 1.6)])
def test_refines_synthetic_icon_at_multiple_window_scales(tmp_path, width, scale):
    height = 420
    image = np.full((height, width, 3), 105, dtype=np.uint8)
    center = (width // 2, 220)
    radius = round(15 * scale)
    stroke = max(2, round(2 * scale))
    span = round(8 * scale)
    cv2.circle(image, center, radius, (245, 245, 245), stroke, cv2.LINE_AA)
    cv2.line(image, (center[0] - span, center[1] - span),
             (center[0] + span, center[1] + span), (250, 250, 250), stroke, cv2.LINE_AA)
    cv2.line(image, (center[0] + span, center[1] - span),
             (center[0] - span, center[1] + span), (250, 250, 250), stroke, cv2.LINE_AA)
    path = tmp_path / f"synthetic-{width}.png"
    assert cv2.imwrite(str(path), image)

    found = refine_close_icon(path, center[0] + round(12 * scale), center[1] - round(8 * scale))

    assert found is not None
    assert abs(found["center_image_px"]["x"] - center[0]) <= 2
    assert abs(found["center_image_px"]["y"] - center[1]) <= 2


def test_refines_dark_x_on_light_background(tmp_path):
    """白底广告常见深灰/黑 X，不能只认深色底上的白笔画。"""
    image = np.full((420, 320, 3), 255, dtype=np.uint8)
    center, radius, span = (160, 220), 15, 8
    cv2.circle(image, center, radius, (90, 90, 90), 2, cv2.LINE_AA)
    for ax, ay in ((-span, -span), (span, -span)):
        cv2.line(image, (center[0] + ax, center[1] + ay),
                 (center[0] - ax, center[1] - ay), (55, 55, 55), 2, cv2.LINE_AA)
    path = tmp_path / "dark-x-on-white.png"
    assert cv2.imwrite(str(path), image)

    found = refine_close_icon(path, center[0] + 12, center[1] - 8)

    assert found is not None
    assert abs(found["center_image_px"]["x"] - center[0]) <= 2
    assert abs(found["center_image_px"]["y"] - center[1]) <= 2


@pytest.mark.parametrize("background,ink", [(105, 245), (255, 55)])
@pytest.mark.parametrize("wrong_mark", ["none", "plus", "check"])
def test_rejects_circle_without_x_or_with_another_mark(tmp_path, wrong_mark, background, ink):
    image = np.full((300, 320, 3), background, dtype=np.uint8)
    center, radius = (170, 150), 17
    ring = 245 if background < 128 else 90
    color = (ink, ink, ink)
    cv2.circle(image, center, radius, (ring, ring, ring), 2, cv2.LINE_AA)
    if wrong_mark == "plus":
        cv2.line(image, (center[0] - 8, center[1]), (center[0] + 8, center[1]),
                 color, 2, cv2.LINE_AA)
        cv2.line(image, (center[0], center[1] - 8), (center[0], center[1] + 8),
                 color, 2, cv2.LINE_AA)
    elif wrong_mark == "check":
        cv2.line(image, (center[0] - 8, center[1]), (center[0] - 2, center[1] + 7),
                 color, 2, cv2.LINE_AA)
        cv2.line(image, (center[0] - 2, center[1] + 7), (center[0] + 10, center[1] - 8),
                 color, 2, cv2.LINE_AA)
    path = tmp_path / f"{wrong_mark}-{background}.png"
    assert cv2.imwrite(str(path), image)

    assert refine_close_icon(path, center[0] + 10, center[1] - 6) is None
