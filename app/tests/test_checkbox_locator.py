from __future__ import annotations

from pathlib import Path

import pytest

from menu_capture.checkbox_locator import refine_consent_checkbox
from menu_capture.config import ConfigStore
from menu_capture.model import Region
from menu_capture.scene_agent import SceneDecision
from menu_capture.store import TaskStore
from menu_capture.uipath_runner import UiPathRunner


ROOT = Path(__file__).resolve().parents[1]
REAL_CONSENT_FRAME = ROOT / "data/action-evidence/4f0a4c06f03c484dbd289eea7045c84c.png"


def _runner(tmp_path: Path) -> UiPathRunner:
    return UiPathRunner(ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"), tmp_path / "profile.json")


def _decision() -> SceneDecision:
    return SceneDecision(
        action="click", reason="勾选隐私协议", target_id="T3",
        x=109.15, y=481.0,
        target_label="我已了解以下规則并同意所有政策",
        target_box=Region(x=34.88, y=475.16, width=148.54, height=11.68),
        pixels=0, menu_region=None, categories=[], overlay_visible=True,
        login_required=False, phone_required=False, consent_required=True,
        transaction_visible=False, item_names_visible=[], prices_visible=[],
        observed_store_text=[], selected_people_count=None, expects_transition=False,
    )


def test_locates_square_checkbox_left_of_real_consent_label() -> None:
    if not REAL_CONSENT_FRAME.is_file():
        pytest.skip("real consent screenshot unavailable")
    match = refine_consent_checkbox(REAL_CONSENT_FRAME, 34.88, 475.16, 11.68, 148.54)
    assert match is not None
    assert match["center_image_px"] == {"x": 25, "y": 480}
    assert match["method"] == "local_consent_checkbox_square"


def test_locates_checkbox_merged_into_ocr_label_box() -> None:
    frame = ROOT / "data/action-evidence/1a1feca044df46adb81c48ab313ea380.png"
    if not frame.is_file():
        pytest.skip("real consent screenshot unavailable")
    match = refine_consent_checkbox(frame, 18.08, 473.51, 13.05, 164.04)
    assert match is not None
    assert match["center_image_px"] == {"x": 42, "y": 479}


def test_locates_checkbox_on_real_wenzhouren_consent_frame() -> None:
    frame = ROOT / "data/action-evidence/80bbdf62abec4d92ad530ff56a8ea42b.png"
    if not frame.is_file():
        pytest.skip("real consent screenshot unavailable")
    match = refine_consent_checkbox(frame, 12.92, 880.04, 14.35, 87.83)
    assert match is not None
    assert match["center_image_px"] == {"x": 27, "y": 887}


def test_runner_clicks_checkbox_instead_of_label_center(tmp_path: Path) -> None:
    if not REAL_CONSENT_FRAME.is_file():
        pytest.skip("real consent screenshot unavailable")
    runner = _runner(tmp_path)
    result = runner._resolve_click_coordinates(REAL_CONSENT_FRAME, _decision(), 1)
    assert (result.x, result.y) == (25, 480)
    assert runner._last_target_resolution["method"] == "local_consent_checkbox_square"


def _consent_rows(*extra: dict) -> list[dict]:
    """3x OCR 坐标：隐私弹窗 + 协议复选框 + 拒绝按钮。"""
    return [
        {"text": "隐私权政策提示", "x": 291, "y": 1131, "width": 348, "height": 54, "confidence": 1.0},
        {"text": "利。如您不同意相关协议和条款我们为您提供了仅浏览", "x": 46, "y": 2450,
         "width": 813, "height": 46, "confidence": 0.5},
        {"text": "我已了解以下规則并同意所有政策", "x": 54, "y": 1420,
         "width": 492, "height": 39, "confidence": 0.5},
        {"text": "不同意", "x": 190, "y": 1641, "width": 108, "height": 42, "confidence": 1.0},
        *extra,
    ]


def test_local_authorization_checks_consent_box_before_agreeing(tmp_path: Path) -> None:
    runner = _runner(tmp_path)

    decision = runner._local_prepare_action(((310, 1011), _consent_rows()), 1, True)

    assert decision is not None
    assert decision.action == "click"
    assert decision.target_label == "我已了解以下规則并同意所有政策"
    assert runner._last_target_resolution["method"] == "local_consent_checkbox_first"
    # 复选框在标签左侧，不能点标签文字中心。
    assert decision.x < 40


def test_local_authorization_skips_consent_box_after_it_was_checked(tmp_path: Path) -> None:
    runner = _runner(tmp_path)

    decision = runner._local_prepare_action(
        ((310, 1011), _consent_rows()), 1, True, consent_checked=True)

    assert decision is not None
    assert runner._last_target_resolution["method"] == "local_consent_pair_geometry"
    assert decision.x > 150


def test_local_authorization_accepts_low_confidence_positive_button(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    rows = _consent_rows({"text": "同意", "x": 600, "y": 1650,
                          "width": 90, "height": 42, "confidence": 0.3})

    decision = runner._local_prepare_action(((310, 1011), rows), 1, True, consent_checked=True)

    assert decision is not None
    assert decision.target_label == "同意"
    assert runner._last_target_resolution["method"] == "local_exact_button"
    assert decision.x == pytest.approx(215.0)
    assert decision.y == pytest.approx(557.0)


def test_local_consent_checkbox_ignores_negative_body_text() -> None:
    rows = _consent_rows()
    box = UiPathRunner._local_consent_checkbox_box(rows, (310, 1011))

    assert box is not None
    assert "不同意" not in box["label"]
    assert box["y"] == pytest.approx(1420 / 3)
