from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from menu_capture.config import ModelConfig
from menu_capture.model import VisionModel
from menu_capture.scene_agent import SceneAgent, SceneError


class FakeVisionModel:
    def __init__(self, response: dict, coordinate_space: str | None = None):
        self.response = response
        if coordinate_space is not None:
            self.coordinate_space = coordinate_space
        self.prompt = ""

    def _request(self, image, prompt):
        self.prompt = prompt
        return json.dumps(self.response)


def _response(**values) -> dict:
    base = {
        "action": "click", "reason": "test", "x": 100, "y": 200,
        "pixels": 0, "menu_region": None, "categories": [],
        "overlay_visible": False, "login_required": False,
        "phone_required": False, "consent_required": False,
        "transaction_visible": False, "target_label": "target",
        "item_names_visible": [], "prices_visible": [],
        "observed_store_text": [], "selected_people_count": None,
        "expects_transition": False,
    }
    base.update(values)
    return base


def _image(tmp_path: Path, size: tuple[int, int]) -> Path:
    path = tmp_path / f"frame-{size[0]}x{size[1]}.png"
    Image.new("RGB", size, "white").save(path)
    return path


def test_invalid_candidate_id_falls_back_to_model_coordinates(tmp_path):
    response = _response(target_id="T99", x=150, y=400, target_label="确定")
    agent = SceneAgent(FakeVisionModel(response))

    decision = agent.decide(
        _image(tmp_path, (310, 1011)), "prepare", [],
        [{"id": "T1", "text": "取消",
          "box_image_px": {"x": 20, "y": 100, "width": 60, "height": 24},
          "center_image_px": {"x": 50, "y": 112}}],
    )

    assert decision.target_id is None
    assert (decision.x, decision.y) == (150, 400)
    assert agent.last_candidate["source"] == "rejected_candidate_id"
    assert agent.last_coordinate_source == "model_coordinates_after_rejected_id"


def test_invalid_candidate_id_without_coordinates_still_fails(tmp_path):
    response = _response(target_id="T99", x=None, y=None, target_label="确定")
    agent = SceneAgent(FakeVisionModel(response))

    with pytest.raises(SceneError, match="不存在的本地候选目标"):
        agent.decide(
            _image(tmp_path, (310, 1011)), "prepare", [],
            [{"id": "T1", "text": "取消",
              "box_image_px": {"x": 20, "y": 100, "width": 60, "height": 24},
              "center_image_px": {"x": 50, "y": 112}}],
        )


def test_normalized_target_box_is_converted_like_point_coordinates(tmp_path):
    response = _response(
        x=500, y=500, target_label="我已了解以下规则并同意所有政策",
        target_box={"x": 100, "y": 400, "width": 400, "height": 40},
    )
    agent = SceneAgent(FakeVisionModel(response, "normalized_999"))

    decision = agent.decide(_image(tmp_path, (310, 1011)), "prepare")

    assert decision.target_box is not None
    assert decision.target_box.x == pytest.approx(100 * 309 / 999)
    assert decision.target_box.y == pytest.approx(400 * 1010 / 999)
    assert decision.target_box.width == pytest.approx(400 * 309 / 999)
    assert decision.target_box.height == pytest.approx(40 * 1010 / 999)


@pytest.mark.parametrize(
    ("size", "space", "raw", "expected"),
    [
        ((310, 1011), "normalized_999", (500, 250), (154.65465465465465, 252.75275275275275)),
        ((440, 1013), "normalized_1000", (500, 250), (219.5, 253.0)),
    ],
)
def test_normalized_coordinates_map_against_each_source_image_size(tmp_path, size, space, raw, expected):
    model = FakeVisionModel(_response(x=raw[0], y=raw[1]), space)
    agent = SceneAgent(model)

    result = agent.decide(_image(tmp_path, size), "prepare")

    assert result.x == pytest.approx(expected[0])
    assert result.y == pytest.approx(expected[1])
    assert result.coordinate_space == "image_pixels"
    assert agent.last_model_coordinates == raw
    assert agent.last_model_pixel_coordinates == pytest.approx(expected)
    assert agent.last_geometry_diagnostics["geometry_valid"] is True
    assert f"坐标协议：{space}" in model.prompt


def test_normalized_999_endpoint_maps_to_last_valid_pixel(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(x=999, y=999), "normalized_999"))

    result = agent.decide(_image(tmp_path, (310, 1011)), "prepare")

    assert (result.x, result.y) == pytest.approx((309, 1010))


def test_image_pixel_protocol_is_not_rescaled_even_for_small_images(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(x=220, y=800)))

    result = agent.decide(_image(tmp_path, (440, 1013)), "prepare")

    assert (result.x, result.y) == pytest.approx((220, 800))
    assert result.coordinate_space == "image_pixels"
    assert agent.last_geometry_diagnostics["requested_space"] == "image_pixels"


def test_invalid_normalized_value_requires_successful_local_target_recovery(tmp_path):
    resolver_calls = []
    agent = SceneAgent(
        FakeVisionModel(_response(x=1000, y=500), "normalized_999"),
        coordinate_resolver=lambda *_: resolver_calls.append(True),
    )

    with pytest.raises(SceneError, match="归一化坐标超出协议范围"):
        agent.decide(_image(tmp_path, (310, 1011)), "prepare")

    assert resolver_calls == [True]
    assert agent.last_geometry_diagnostics["raw_xy"] == {"x": 1000, "y": 500}
    assert agent.last_geometry_diagnostics["error_stage"] == "geometry_validation"
    assert agent.last_geometry_diagnostics["geometry_valid"] is False


def test_invalid_normalized_point_without_resolver_still_stops(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(x=498, y=1256), "normalized_999"))
    with pytest.raises(SceneError, match="归一化坐标超出协议范围"):
        agent.decide(_image(tmp_path, (310, 1312)), "prepare")


def test_normalized_model_context_converts_history_without_changing_stored_pixels(tmp_path):
    model = FakeVisionModel(_response(x=498, y=950), "normalized_999")
    history = [{"action": "click", "x": 154.5, "y": 1256.0, "page_result": "unchanged"}]
    SceneAgent(model).decide(_image(tmp_path, (310, 1312)), "prepare", history)
    context = json.loads(model.prompt.split("当前阶段上下文：", 1)[1])
    assert context["history_coordinate_space"] == "normalized_999"
    assert context["recent_actions"][0]["x"] == pytest.approx(499.5)
    assert context["recent_actions"][0]["y"] < 999
    assert history[0]["y"] == 1256


def test_resting_store_prompt_explicitly_allows_readonly_menu(tmp_path):
    model = FakeVisionModel(_response())
    SceneAgent(model).decide(_image(tmp_path, (310, 1312)), "prepare")
    assert "均不构成停止原因" in model.prompt
    assert "只要菜名价格可见且菜单可滚动就返回 ready" in model.prompt


def test_observed_mixed_unit_confirm_point_is_recovered_by_named_ocr_target(tmp_path):
    from menu_capture.config import ConfigStore
    from menu_capture.store import TaskStore
    from menu_capture.uipath_runner import UiPathRunner
    path = _image(tmp_path, (310, 1312))
    rows = [{"text": "确定", "x": 410.75, "y": 3800, "width": 81.375, "height": 47, "confidence": 0.3}]
    runner = UiPathRunner(ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"), tmp_path / "profile.json", ocr_reader=lambda _: rows)
    runner._active_ocr_path = str(path.resolve())
    runner._active_ocr_result = ((310, 1312), rows)
    agent = SceneAgent(FakeVisionModel(_response(x=498, y=1256, target_label="确定", selected_people_count=1), "normalized_999"),
                       coordinate_resolver=lambda p,d: runner._resolve_click_coordinates(p,d,1))
    result = agent.decide(path, "prepare")
    assert result.x == pytest.approx(150.4791667)
    assert result.y == pytest.approx(1274.5)
    assert agent.last_geometry_diagnostics["point_protocol_valid"] is False
    assert agent.last_coordinate_source == "local_target_resolver"
    assert runner._last_target_resolution["min_confidence"] == 0.3
    assert UiPathRunner._find_ocr_target_detail(rows, (310,1312), "确定")["matched"] is False


def test_low_confidence_duplicate_named_targets_do_not_pick_arbitrarily(tmp_path):
    from menu_capture.config import ConfigStore
    from menu_capture.store import TaskStore
    from menu_capture.uipath_runner import UiPathRunner
    path = _image(tmp_path, (310, 1312))
    rows = [{"text": "确定", "x": 410.75, "y": y, "width": 81.375, "height": 47, "confidence": 0.3}
            for y in (3500, 3800)]
    runner = UiPathRunner(ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"), tmp_path / "profile.json", ocr_reader=lambda _: rows)
    agent = SceneAgent(FakeVisionModel(_response(x=498, y=1256, target_label="确定"), "normalized_999"),
                       coordinate_resolver=lambda p,d: runner._resolve_click_coordinates(p,d,1))
    with pytest.raises(SceneError, match="本地未定位到目标"):
        agent.decide(path, "prepare")


def test_invalid_normalized_region_runs_resolver_but_cannot_execute_if_not_corrected(tmp_path):
    response = _response(
        action="ready", x=None, y=None,
        menu_region={"x": 100, "y": 100, "width": 950, "height": 800},
        item_names_visible=["菜"], prices_visible=["¥10"],
    )
    resolver_calls = []
    agent = SceneAgent(
        FakeVisionModel(response, "normalized_999"),
        region_resolver=lambda *_: resolver_calls.append(True),
    )

    with pytest.raises(SceneError, match="模型菜单区域无效"):
        agent.decide(_image(tmp_path, (310, 1011)), "prepare")

    assert resolver_calls == [True]
    assert agent.last_geometry_diagnostics["raw_region"] == {
        "x": 100, "y": 100, "width": 950, "height": 800,
    }
    assert agent.last_geometry_diagnostics["error_stage"] == "geometry_validation"


@pytest.mark.parametrize("raw_region", [
    {"x": 218, "y": 395, "width": 781, "height": 840},
    {"x": 165, "y": 430, "width": 145, "height": 820},
])
def test_observed_menu_region_overflow_can_be_replaced_by_local_ocr(tmp_path, raw_region):
    from menu_capture.model import Region
    response = _response(action="ready", x=None, y=None, menu_region=raw_region,
                         item_names_visible=["菜单菜品"], prices_visible=["¥9.9"])
    local_region = Region(x=70, y=360, width=232, height=870)
    observed = []
    def resolve(_path, decision):
        observed.append(decision.menu_region)
        return decision.model_copy(update={"menu_region": local_region})
    agent = SceneAgent(FakeVisionModel(response, "normalized_999"), region_resolver=resolve)
    result = agent.decide(_image(tmp_path, (310, 1312)), "prepare")
    assert result.menu_region == local_region
    assert observed[0].y + observed[0].height > 1312
    assert agent.last_geometry_diagnostics["raw_region"] == raw_region
    assert agent.last_geometry_diagnostics["region_source"] == "local_ocr_menu_anchors"
    assert agent.last_geometry_diagnostics["geometry_valid"] is True


def test_narrow_menu_content_heading_does_not_shift_sidebar_boundary(tmp_path):
    from menu_capture.config import ConfigStore
    from menu_capture.store import TaskStore
    from menu_capture.uipath_runner import UiPathRunner
    runner = UiPathRunner(ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"), tmp_path / "profile.json", ocr_reader=lambda _: [])
    def row(text, x, y, width):
        return {"text": text, "x": x * 3, "y": y * 3, "width": width * 3, "height": 30, "confidence": 1.0}
    rows = [row("新品上新", 9, 395, 44), row("馄饨", 9, 575, 22),
            row("新品上新", 70, 385, 44), row("土耳其烤肉夹馍", 77, 540, 135),
            row("¥9.9/份", 76, 582, 50), row("我的", 268, 1297, 22)]
    agent = SceneAgent(FakeVisionModel(_response(
        action="ready", x=None, y=None, menu_region={"x": 165, "y": 430, "width": 145, "height": 820},
        categories=["新品上新", "馄饨"], item_names_visible=["土耳其烤肉夹馍"], prices_visible=["¥9.9"],
    ), "normalized_999"), region_resolver=lambda path, decision: runner._resolve_ready_region(path, decision, "", ((310, 1312), rows)))
    result = agent.decide(_image(tmp_path, (310, 1312)), "prepare")
    assert result.menu_region.x == pytest.approx(61)
    assert result.menu_region.y == pytest.approx(377)
    assert agent.last_region_source == "local_ocr_menu_anchors"


def test_exact_ocr_correction_can_still_replace_finite_out_of_bounds_pixel_xy(tmp_path):
    response = _response(x=500, y=500)

    def resolver(_path, decision):
        return decision.model_copy(update={"x": 220.0, "y": 801.0})

    agent = SceneAgent(FakeVisionModel(response), coordinate_resolver=resolver)
    result = agent.decide(_image(tmp_path, (440, 1013)), "prepare")

    assert (result.x, result.y) == pytest.approx((220, 801))
    assert agent.last_model_coordinates == (500, 500)
    assert agent.last_model_pixel_coordinates == (500, 500)
    assert agent.last_geometry_diagnostics["resolver_xy"] == {"x": 220.0, "y": 801.0}
    assert agent.last_geometry_diagnostics["coordinate_source"] == "local_target_resolver"


def test_ready_region_and_action_coordinates_share_normalized_conversion(tmp_path):
    response = _response(
        action="ready", x=None, y=None, target_label="menu",
        menu_region={"x": 100, "y": 100, "width": 800, "height": 800},
        item_names_visible=["菜"], prices_visible=["¥10"],
        coordinate_space="normalized_1000",
    )
    agent = SceneAgent(FakeVisionModel(response, "image_pixels"))

    result = agent.decide(_image(tmp_path, (440, 1013)), "prepare")

    assert result.menu_region is not None
    assert result.menu_region.x == pytest.approx(43.9)
    assert result.menu_region.y == pytest.approx(101.2)
    assert result.menu_region.width == pytest.approx(351.2)
    assert result.menu_region.height == pytest.approx(809.6)
    assert agent.last_geometry_diagnostics["requested_space"] == "image_pixels"
    assert agent.last_geometry_diagnostics["response_space"] == "normalized_1000"
    assert agent.last_geometry_diagnostics["converted_region"] == result.menu_region.model_dump()


def test_unknown_response_space_is_rejected_and_diagnosed(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(coordinate_space="normalized_500"), "image_pixels"))

    with pytest.raises(SceneError, match="未知坐标协议"):
        agent.decide(_image(tmp_path, (310, 1011)), "prepare")

    assert agent.last_geometry_diagnostics["response_space"] == "unsupported"
    assert agent.last_geometry_diagnostics["raw_xy"] == {"x": 100, "y": 200}
    assert agent.last_geometry_diagnostics["error_stage"] == "unsupported_response_space"


def test_click_ignores_null_unused_scroll_distance(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(x=498, y=862, pixels=None), "normalized_999"))
    result = agent.decide(_image(tmp_path, (310, 1011)), "prepare")
    assert result.pixels == 0
    assert result.x == pytest.approx(498 * 309 / 999)


def test_scroll_still_rejects_null_distance(tmp_path):
    agent = SceneAgent(FakeVisionModel(_response(action="scroll", pixels=None)))
    with pytest.raises(SceneError, match="pixels"):
        agent.decide(_image(tmp_path, (310, 1011)), "prepare")


def test_runner_refines_normalized_close_target_on_same_frame(tmp_path):
    from menu_capture.config import ConfigStore
    from menu_capture.store import TaskStore
    from menu_capture.uipath_runner import UiPathRunner
    path = Path(__file__).resolve().parents[1] / "data/action-evidence/b86c9a9398054a559f6e974eba4a394b.png"
    if not path.exists():
        pytest.skip("real evidence unavailable")
    runner = UiPathRunner(ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"), tmp_path / "profile.json")
    agent = SceneAgent(
        FakeVisionModel(_response(x=493, y=528, target_label="关闭弹窗", pixels=None), "normalized_999"),
        coordinate_resolver=lambda image, decision: runner._resolve_click_coordinates(image, decision, 1),
    )
    result = agent.decide(path, "prepare")
    assert abs(result.x - 155) <= 2 and abs(result.y - 506) <= 2
    assert agent.last_geometry_diagnostics["converted_xy"]["y"] > 530
    assert agent.last_geometry_diagnostics["resolver_xy"]["y"] < 509
    assert runner._last_target_resolution["method"] == "hough_circle_cross_v1"


def test_ordinary_text_button_does_not_run_close_icon_detection(tmp_path, monkeypatch):
    from menu_capture.config import ConfigStore
    from menu_capture.store import TaskStore
    from menu_capture.uipath_runner import UiPathRunner
    runner = UiPathRunner(ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"), tmp_path / "profile.json")
    def unexpected(*args):
        raise AssertionError("ordinary text targets must not trigger icon matching")
    monkeypatch.setattr("menu_capture.uipath_runner.refine_close_icon", unexpected)
    response = SceneAgent(FakeVisionModel(_response(target_label="开始点餐"))).decide(_image(tmp_path, (440, 1013)), "prepare")
    assert runner._resolve_click_coordinates(tmp_path / "unused.png", response, 1) is response


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("qwen3-vl-plus", "normalized_999"),
        ("Qwen3.7-Plus", "normalized_999"),
        ("qwen3.8-flash", "normalized_999"),
        ("qwen2.5-vl", "image_pixels"),
        ("other-qwen3.7-plus", "image_pixels"),
        ("gpt-vision", "image_pixels"),
    ],
)
def test_vision_model_uses_limited_model_family_coordinate_mapping(model_name, expected):
    model = VisionModel(ModelConfig(base_url="https://example.test/v1", model=model_name), "test-key")
    try:
        assert model.coordinate_space == expected
    finally:
        model.close()
