from __future__ import annotations

import json

from PIL import Image

from menu_capture.material_fallback import detect_missing_qr


class StubModel:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.size = None

    def _request(self, image, _prompt):
        self.size = image.size
        if self.error:
            raise self.error
        return json.dumps(self.response, ensure_ascii=False)


def test_detects_visible_code_and_maps_box_to_original_pixels(tmp_path):
    path = tmp_path / "large.png"
    Image.new("RGB", (2400, 1200), "white").save(path)
    model = StubModel({"candidates": [{"box": {"x": 100, "y": 80, "width": 200, "height": 160},
                                      "purpose": "ordering", "reason": "可见方形码图，旁有扫码点餐提示"}]})

    candidates = detect_missing_qr(model, path)

    assert model.size == (1280, 640)
    assert candidates == [{"box": {"x": 187.5, "y": 150.0, "width": 375.0, "height": 300.0},
                           "purpose": "ordering", "purpose_reason": "可见方形码图，旁有扫码点餐提示",
                           "source": "vision_fallback"}]


def test_no_visual_evidence_and_invalid_response_leave_manual_review(tmp_path):
    path = tmp_path / "menu.png"
    Image.new("RGB", (320, 200), "white").save(path)
    assert detect_missing_qr(StubModel({"candidates": []}), path) == []
    assert detect_missing_qr(StubModel({"unexpected": []}), path) == []
    assert detect_missing_qr(StubModel(error=RuntimeError("private response")), path) == []


def test_invalid_boxes_are_rejected_unknown_purpose_is_preserved(tmp_path):
    path = tmp_path / "small.png"
    Image.new("RGB", (300, 180), "white").save(path)
    model = StubModel({"candidates": [
        {"box": {"x": 250, "y": 20, "width": 80, "height": 80}, "purpose": "ordering", "reason": "越界"},
        {"box": {"x": 20, "y": 30, "width": 100, "height": 90}, "purpose": "guess", "reason": "边界可见，但用途不清"},
    ]})

    result = detect_missing_qr(model, path)

    assert len(result) == 1
    assert result[0]["box"] == {"x": 20.0, "y": 30.0, "width": 100.0, "height": 90.0}
    assert result[0]["purpose"] == "unknown"
    assert result[0]["purpose_reason"] == "边界可见，但用途不清"


def test_malformed_json_returns_empty_without_exposing_error(tmp_path):
    path = tmp_path / "input.png"
    Image.new("RGB", (100, 100), "white").save(path)
    model = StubModel()
    model._request = lambda *_args: "not-json"
    assert detect_missing_qr(model, path) == []
