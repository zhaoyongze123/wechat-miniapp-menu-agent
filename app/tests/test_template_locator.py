"""通用模板定位器的容错与唯一性行为。"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from menu_capture import template_locator
from menu_capture.template_locator import (
    ASSETS_DIR,
    TemplateNotFound,
    locate_control,
    locate_templates,
)

_TEMPLATE = ASSETS_DIR / "preview-recognize.png"


def _template() -> np.ndarray:
    image = cv2.imread(str(_TEMPLATE), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def _canvas_with(items: list[tuple[np.ndarray, int, int]], size=(825, 760), shift: int = 0) -> np.ndarray:
    canvas = np.full((size[1], size[0], 3), 245, dtype=np.uint8)
    for template, x, y in items:
        patch = np.clip(template.astype(np.int16) + shift, 0, 255).astype(np.uint8)
        canvas[y:y + patch.shape[0], x:x + patch.shape[1]] = patch
    return canvas


def test_template_is_found_after_brightness_shift():
    template = _template()
    canvas = _canvas_with([(template, 240, 10)], shift=26)

    result = locate_templates(canvas, [template], roi=(0, 0, 825, 70), scales=(1.0,))

    assert result["best"]["score"] >= 0.70
    assert result["best"]["center_image_px"]["x"] == pytest.approx(240 + template.shape[1] / 2, abs=2)
    assert result["best"]["center_image_px"]["y"] == pytest.approx(10 + template.shape[0] / 2, abs=2)


def test_template_is_found_at_a_different_scale():
    template = _template()
    scaled = cv2.resize(template, (round(template.shape[1] * 1.5), round(template.shape[0] * 1.5)),
                        interpolation=cv2.INTER_CUBIC)
    canvas = _canvas_with([(scaled, 300, 10)])

    result = locate_templates(canvas, [template], roi=(0, 0, 825, 80))

    assert result["best"]["scale"] == pytest.approx(1.5, abs=0.2)
    assert result["best"]["center_image_px"]["x"] == pytest.approx(300 + scaled.shape[1] / 2, abs=3)


def test_two_identical_controls_are_reported_as_ambiguous(tmp_path: Path):
    template = _template()
    canvas = _canvas_with([(template, 100, 10), (template, 600, 10)])
    path = tmp_path / "duplicate.png"
    cv2.imwrite(str(path), canvas)

    with pytest.raises(TemplateNotFound) as exc:
        locate_control(path, "preview-recognize")

    assert exc.value.diagnostics["reason"] == "ambiguous_matches"
    assert len(exc.value.diagnostics["candidates"]) >= 2


def test_missing_control_template_fails_fast(tmp_path: Path):
    path = tmp_path / "frame.png"
    cv2.imwrite(str(path), _canvas_with([]))

    with pytest.raises(TemplateNotFound) as exc:
        locate_control(path, "no-such-control", assets_dir=tmp_path)

    assert exc.value.diagnostics["reason"] == "missing_template"


def test_locate_control_returns_image_pixel_center_and_box():
    result = locate_control(_canvas_with([(_template(), 240, 10)]), "preview-recognize",
                            roi=(0, 0, 825, 70))

    assert result["method"] == "multiscale_template_match"
    assert result["box_image_px"]["x"] == pytest.approx(240, abs=3)
    assert result["center_image_px"]["x"] == pytest.approx(result["box_image_px"]["x"] +
                                                         result["box_image_px"]["width"] / 2)
    assert result["image_size"] == {"width": 825, "height": 760}
    assert result["score"] >= 0.70
