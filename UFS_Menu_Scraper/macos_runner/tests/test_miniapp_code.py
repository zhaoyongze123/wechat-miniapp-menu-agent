from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from menu_capture import miniapp_code
from menu_capture.miniapp_code import (
    CANVAS,
    CircleDetectorUnavailable,
    CircleImageTooLarge,
    MiniProgramCodeDetector,
    NodeCircleWorker,
    _fit_square,
    _normalize_canvas,
    _preview_rois,
    _right_angle_locators,
    _rotated_points,
    _validate_structure,
    error_code,
)


def _structure(points, badge=None, kind: str = "wechat-mini-program-code") -> dict:
    return {
        "kind": kind,
        "confidence": 0.99,
        "locatorPoints": [{"x": x, "y": y} for x, y in points],
        "badgeBounds": badge or {"x": 260, "y": 260, "width": 40, "height": 40},
    }


def test_valid_structure_requires_three_right_angle_locators_and_badge() -> None:
    validated = _validate_structure(_structure([(60, 60), (300, 60), (60, 300)]))
    assert validated is not None
    points, badge = validated
    assert len(points) == 3
    assert badge["width"] == 40


@pytest.mark.parametrize(
    "result",
    [
        _structure([(60, 60), (300, 60), (60, 300)], kind="qr-code"),
        _structure([(60, 60), (300, 60)]),
        _structure([(60, 60), (300, 60), (60, 300)], badge={"x": 260, "y": 260, "width": 0, "height": 40}),
        _structure([(60, 60), (300, 60), (60, 300)], badge=None) | {"badgeBounds": None},
        # 三点接近共线，不构成定位点直角关系。
        _structure([(60, 60), (180, 62), (300, 64)]),
        # 坐标越界。
        _structure([(60, 60), (300, 60), (60, 420)]),
    ],
)
def test_invalid_structures_are_rejected(result: dict) -> None:
    assert _validate_structure(result) is None


def test_non_finite_locator_coordinates_are_rejected() -> None:
    result = _structure([(60, 60), (300, 60), (60, 300)])
    result["locatorPoints"][1]["x"] = math.inf
    assert _validate_structure(result) is None


def test_right_angle_helper_picks_corner_and_reports_ratio() -> None:
    corner, ratio, angle = _right_angle_locators([(300, 60), (60, 60), (60, 300)])
    assert corner == (60, 60)
    assert ratio == pytest.approx(1.0)
    assert angle == pytest.approx(90.0)


def test_rotated_points_follow_canvas_rotation_formulas() -> None:
    last = CANVAS - 1
    assert _rotated_points([(0.0, 0.0)], 90) == [(last, 0.0)]
    assert _rotated_points([(0.0, 0.0)], 180) == [(last, last)]
    assert _rotated_points([(0.0, 0.0)], 270) == [(0.0, last)]
    assert _rotated_points([(12.0, 34.0)], 0) == [(12.0, 34.0)]


def test_fit_square_keeps_aspect_ratio_and_centers_on_canvas() -> None:
    region = Image.new("RGB", (400, 200), "white")
    canvas, scale, pad_x, pad_y = _fit_square(region)
    assert canvas.size == (CANVAS, CANVAS)
    assert scale == pytest.approx(CANVAS / 400)
    assert pad_x == 0
    assert pad_y > 0


def test_normalize_canvas_whitens_light_background_but_keeps_dark_marks() -> None:
    image = Image.new("RGB", (64, 64), (238, 230, 210))
    draw = ImageDraw.Draw(image)
    for x in range(0, 64, 8):
        for y in range(0, 64, 8):
            if ((x // 8) + (y // 8)) % 2:
                draw.rectangle((x, y, x + 7, y + 7), fill=(20, 20, 20))
    normalized = np.array(_normalize_canvas(image))
    assert normalized[2, 2].tolist() == [255, 255, 255]
    assert int(normalized[10, 2].mean()) < 80


def test_preview_rois_are_bounded_and_stay_inside_preview() -> None:
    gray = np.full((240, 240), 255, dtype=np.uint8)
    marker = Image.new("L", (240, 240), 255)
    ImageDraw.Draw(marker).ellipse((70, 70, 170, 170), outline=0, width=8)
    gray = np.array(marker)
    rois = _preview_rois(gray)
    assert 1 <= len(rois) <= miniapp_code.MAX_ROI_PER_IMAGE
    for roi in rois:
        assert roi["left"] >= 0 and roi["top"] >= 0
        assert roi["left"] + roi["size"] <= 240
        assert roi["top"] + roi["size"] <= 240
        assert roi["size"] > 0


def test_missing_node_worker_reports_controlled_unavailable_code(tmp_path: Path) -> None:
    with pytest.raises(CircleDetectorUnavailable) as caught:
        NodeCircleWorker(tmp_path)
    assert error_code(caught.value) == "circle_detector_unavailable"


def test_detector_rejects_oversized_image_before_touching_worker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(miniapp_code, "MAX_DECODE_PIXELS", 100)
    monkeypatch.setattr(miniapp_code, "NodeCircleWorker", lambda *args, **kwargs: _FakeWorker())
    detector = MiniProgramCodeDetector(work_dir=tmp_path / "roi")
    image = tmp_path / "big.png"
    Image.new("RGB", (40, 40), "white").save(image)
    with pytest.raises(CircleImageTooLarge) as caught:
        detector.detect(image)
    assert error_code(caught.value) == "image_too_large"


def test_detector_maps_valid_structure_back_to_original_pixels(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(miniapp_code, "NodeCircleWorker", lambda *args, **kwargs: _FakeWorker(_structure([(60, 60), (300, 60), (60, 300)])))
    # 整图只有一个接近方形的码图案，确保 ROI 覆盖它。
    image = tmp_path / "code.png"
    picture = Image.new("RGB", (400, 400), "white")
    ImageDraw.Draw(picture).rounded_rectangle((100, 100, 300, 300), radius=80, outline="black", width=12)
    picture.save(image)
    detector = MiniProgramCodeDetector(work_dir=tmp_path / "roi")
    found = detector.detect(image)
    assert found is not None
    box = found["box"]
    assert box["width"] >= 40 and box["height"] >= 40
    assert 0 <= box["x"] and 0 <= box["y"]
    assert box["x"] + box["width"] <= 400
    assert box["y"] + box["height"] <= 400
    assert len(found["locator_points"]) == 3
    assert found["detector_confidence"] == 0.99


class _FakeWorker:
    def __init__(self, result: dict | None = None):
        self.result = result

    def verify(self, _path: Path) -> dict | None:
        return self.result

    def close(self) -> None:
        pass
