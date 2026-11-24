from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from menu_capture.scene_agent import SceneAgent, SceneError
from menu_capture.target_candidates import build_text_candidates, candidate_by_id, candidate_table


class FakeVisionModel:
    def __init__(self, response: dict, coordinate_space: str = "normalized_999"):
        self.response = response
        self.coordinate_space = coordinate_space
        self.prompt = ""

    def _request(self, _image, prompt):
        self.prompt = prompt
        return json.dumps(self.response, ensure_ascii=False)


def _response(**values) -> dict:
    base = {
        "action": "click", "reason": "test", "target_id": None, "x": None, "y": None,
        "pixels": 0, "menu_region": None, "categories": [],
        "overlay_visible": False, "login_required": False,
        "phone_required": False, "consent_required": False,
        "transaction_visible": False, "target_label": "",
        "item_names_visible": [], "prices_visible": [],
        "observed_store_text": [], "selected_people_count": None,
        "expects_transition": False,
    }
    base.update(values)
    return base


def _image(tmp_path: Path, size=(310, 1312)) -> Path:
    path = tmp_path / "frame.png"
    Image.new("RGB", size, "white").save(path)
    return path


def _candidate(cid="T5", text="确定", x=155.0, y=410.0) -> dict:
    return {
        "id": cid, "kind": "button", "text": text, "position": "下方中间",
        "box_image_px": {"x": x - 15, "y": y - 10, "width": 30, "height": 20},
        "center_image_px": {"x": x, "y": y},
    }


def test_candidate_builder_converts_three_x_ocr_and_merges_same_line_fragments():
    rows = [
        {"text": "开始", "x": 420, "y": 2400, "width": 60, "height": 60, "confidence": 0.98},
        {"text": "点餐", "x": 486, "y": 2400, "width": 60, "height": 60, "confidence": 0.97},
    ]

    candidates = build_text_candidates(rows, (310, 1011))

    assert len(candidates) == 1
    item = candidates[0]
    assert item["id"] == "T1"
    assert item["text"] == "开始点餐"
    assert item["kind"] == "button"
    assert item["center_image_px"]["x"] == pytest.approx(161.0)
    assert item["center_image_px"]["y"] == pytest.approx(810.0)


def test_candidate_builder_keeps_people_grid_and_rejects_price_rows():
    rows = [
        *[
            {"text": str(number), "x": 300 + index * 180, "y": 1500,
             "width": 60, "height": 60, "confidence": 0.95}
            for index, number in enumerate((1, 2, 3))
        ],
        {"text": "确定", "x": 420, "y": 2400, "width": 120, "height": 60, "confidence": 0.95},
        {"text": "¥18", "x": 600, "y": 900, "width": 90, "height": 36, "confidence": 0.95},
        {"text": "¥79/份起", "x": 600, "y": 960, "width": 120, "height": 36, "confidence": 0.95},
        {"text": "¥3/包", "x": 600, "y": 1020, "width": 90, "height": 36, "confidence": 0.95},
        {"text": "招牌菜名很长很长很长很长很长很长很长很长很长", "x": 560, "y": 1200, "width": 360, "height": 42, "confidence": 0.95},
    ]

    candidates = build_text_candidates(rows, (310, 1011))
    texts = [item["text"] for item in candidates]

    assert texts.count("1") == 1
    assert texts.count("2") == 1
    assert texts.count("3") == 1
    assert "确定" in texts
    assert "¥18" not in texts
    assert "¥79/份起" not in texts
    assert "¥3/包" not in texts
    assert "招牌菜名很长很长很长很长很长很长很长很长很长" not in texts
    confirm = next(item for item in candidates if item["text"] == "确定")
    assert confirm["id"] == "T4"
    assert "T4｜button｜确定" in candidate_table(candidates)


def test_single_standalone_price_number_is_not_offered_as_person_candidate():
    candidates = build_text_candidates(
        [{"text": "28", "x": 600, "y": 900, "width": 60, "height": 40, "confidence": 0.95}],
        (310, 1011),
    )
    assert candidates == []


def test_scene_agent_uses_candidate_center_instead_of_model_coordinates(tmp_path):
    model = FakeVisionModel(_response(target_id="T5", x=498, y=862))
    calls = []
    agent = SceneAgent(model, coordinate_resolver=lambda *args: calls.append(args) or None)

    result = agent.decide(_image(tmp_path), "prepare", [], [_candidate()])

    assert (result.x, result.y) == (155.0, 410.0)
    assert result.target_label == "确定"
    assert agent.last_coordinate_source == "local_candidate_id_T5"
    assert agent.last_geometry_diagnostics["candidate_resolution"]["target_id"] == "T5"
    assert len(calls) == 1
    assert calls[0][1].target_box is not None
    assert "T5｜button｜确定" in model.prompt
    assert "target_id" in model.prompt


def test_unique_target_label_falls_back_to_candidate_center(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(x=498, y=862, target_label="确定")))

    result = agent.decide(_image(tmp_path), "prepare", [], [_candidate()])

    assert (result.x, result.y) == (155.0, 410.0)
    assert agent.last_coordinate_source == "local_candidate_label_fallback_T5"
    assert agent.last_geometry_diagnostics["candidate_resolution"]["source"] == "local_candidate_label_fallback"


def test_duplicate_labels_do_not_trigger_label_fallback(tmp_path):
    first = _candidate("T1", "确定", 100.0, 300.0)
    second = _candidate("T2", "确定", 200.0, 300.0)
    agent = SceneAgent(FakeVisionModel(_response(x=498, y=862, target_label="确定")))

    result = agent.decide(_image(tmp_path), "prepare", [], [first, second])

    assert agent.last_coordinate_source == "model"
    assert result.x != 100.0 and result.x != 200.0


def test_candidate_text_overrides_model_relabel(tmp_path):
    model = FakeVisionModel(_response(target_id="T5", x=None, y=None, target_label="1"))
    agent = SceneAgent(model)

    result = agent.decide(_image(tmp_path), "prepare", [], [_candidate("T5", "2")])

    assert result.target_label == "2"
    assert (result.x, result.y) == (155.0, 410.0)


def test_scene_agent_rejects_unknown_candidate_id(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(target_id="T99")))

    with pytest.raises(SceneError, match="不存在的本地候选目标"):
        agent.decide(_image(tmp_path), "prepare", [], [_candidate()])


def test_scene_agent_still_accepts_coarse_point_when_candidate_list_is_empty(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(x=287, y=689, target_label="关闭广告")))

    result = agent.decide(_image(tmp_path), "prepare", [], [])

    assert (result.x, result.y) == pytest.approx((287 * 309 / 999, 689 * 1311 / 999))
    assert agent.last_coordinate_source == "model"


@pytest.mark.parametrize("action", ["ready", "click"])
def test_desktop_observation_never_returns_menu_region_or_ready(tmp_path, action):
    response = _response(
        action=action, x=500, y=300,
        menu_region={"x": 145, "y": 160, "width": 290, "height": 1180},
        item_names_visible=["菜"], prices_visible=["¥10"],
    )
    agent = SceneAgent(FakeVisionModel(response))

    result = agent.decide(_image(tmp_path, (2560, 1440)), "desktop_overlay_recovery", [], [])

    assert result.menu_region is None
    if action == "ready":
        assert result.action == "return_to_window"
    assert agent.last_geometry_diagnostics["geometry_valid"] is True


def test_candidate_id_is_rejected_for_non_click_actions(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(
        action="ready", target_id="T5",
        menu_region={"x": 20, "y": 20, "width": 200, "height": 400},
        item_names_visible=["菜"], prices_visible=["¥10"],
    )))

    with pytest.raises(SceneError, match="候选编号"):
        agent.decide(_image(tmp_path), "prepare", [], [_candidate()])


def test_small_menu_region_overflow_is_clamped_to_screenshot(tmp_path):
    response = _response(
        action="ready", x=None, y=None, target_label="",
        menu_region={"x": 170, "y": 460, "width": 829, "height": 554},
        item_names_visible=["菜"], prices_visible=["¥10"],
    )
    response["coordinate_space"] = "normalized_999"
    agent = SceneAgent(FakeVisionModel(response))

    result = agent.decide(_image(tmp_path, (310, 1014)), "prepare", [], [])

    assert result.menu_region is not None
    assert result.menu_region.y + result.menu_region.height <= 1014
    assert result.menu_region.x + result.menu_region.width <= 310
    assert agent.last_geometry_diagnostics["geometry_valid"] is True


def test_people_grid_geometry_recovers_missing_configured_one(tmp_path):
    from menu_capture.config import ConfigStore
    from menu_capture.store import TaskStore
    from menu_capture.uipath_runner import UiPathRunner

    runner = UiPathRunner(ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"), tmp_path / "profile.json")
    rows = [{"text": "选择人数", "x": 300, "y": 300, "width": 240, "height": 60, "confidence": 1.0}]
    for number in range(2, 10):
        row = (number - 1) // 3
        col = (number - 1) % 3
        x = 90 + col * 60
        y = 780 + row * 40
        rows.append({"text": str(number), "x": x * 3, "y": y * 3,
                     "width": 20 * 3, "height": 20 * 3, "confidence": 0.95})

    decision = runner._local_prepare_action(((310, 1011), rows), 1, True)

    assert decision is not None
    assert decision.action == "click"
    assert decision.target_label == "1"
    assert decision.x == pytest.approx(100)
    assert decision.y == pytest.approx(790)
    assert runner._last_target_resolution["method"] == "local_people_grid_geometry"
