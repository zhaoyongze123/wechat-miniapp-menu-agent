from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from menu_capture.scene_agent import SceneAgent, SceneDecision, SceneError
from menu_capture.uipath_runner import NeedsReview, UiPathRunner

from test_uipath_runner import _setup, scene_response


class DecisionAgent:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.histories = []

    def decide(self, image_path, phase, history, candidates=None):
        self.histories.append([dict(entry) for entry in history])
        assert self.decisions, "runner requested a decision after the scripted sequence"
        return self.decisions.pop(0)


def decision(action="click", *, label="", x=80, y=70, **fields):
    defaults = dict(
        x=x if action in {"click", "right_click", "double_click", "scroll"} else None,
        y=y if action in {"click", "right_click", "double_click", "scroll"} else None,
        target_label=label,
        reason=f"当前页面建议执行{label or action}",
        overlay_visible=False,
        login_required=False,
        phone_required=False,
        consent_required=False,
        transaction_visible=False,
        observed_store_text=[],
        item_names_visible=[],
        prices_visible=[],
        selected_people_count=None,
        expects_transition=False,
        menu_region=None,
    )
    defaults.update(fields)
    return SceneDecision.model_validate_json(scene_response(action=action, **defaults))


def ready():
    return SceneDecision.model_validate_json(scene_response(
        action="ready", overlay_visible=False, item_names_visible=["示例菜"],
        prices_visible=["¥18"], menu_region={"x": 20, "y": 20, "width": 180, "height": 120},
    ))


def prepare_runner(tmp_path, monkeypatch):
    runner, _, task, _, _, output_root = _setup(tmp_path)
    output_dir = output_root / "prepare-progress"
    output_dir.mkdir(parents=True)
    frame_path = output_dir / "frame.png"
    Image.new("RGB", (240, 160), "white").save(frame_path)
    frame = {"path": str(frame_path), "width": 240, "height": 160}
    applied = []
    screenshots = []
    differences = []

    monkeypatch.setattr(runner, "_local_ready_preflight", lambda *args: None)
    monkeypatch.setattr(runner, "_local_prepare_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_should_capture_desktop", lambda *args: (False, {}))
    runner.ocr_reader = lambda _path: []
    monkeypatch.setattr(runner, "_read_scaled_ocr", lambda *args: ((240, 160), [
        {"text": "页面文字", "x": 10, "y": 10},
        {"text": "菜单选项", "x": 20, "y": 20},
        {"text": "继续", "x": 30, "y": 30},
    ]))
    monkeypatch.setattr(runner, "_debug_frame_ref", lambda *args: None)
    monkeypatch.setattr(runner, "_apply_scene_action", lambda d, *args, **kwargs: applied.append(d) or None)
    monkeypatch.setattr(runner, "_send", lambda *args, **kwargs: None)

    def compare(left, right):
        difference = UiPathRunner._image_difference_images(left, right)
        differences.append(difference)
        return difference

    monkeypatch.setattr(runner, "_image_difference_images", compare)

    def screenshot(*args, **kwargs):
        screenshots.append(kwargs.get("desktop", False))
        return frame

    monkeypatch.setattr(runner, "_screenshot", screenshot)
    return runner, task, frame, output_dir, applied, screenshots, differences


def run(runner, task, frame, output_dir, agent):
    return runner._prepare_until_ready(
        task, agent, frame, "recorded-miniapp-selector", "准备菜单",
        runner.configs.load(), output_dir, {"scroll_units": 2},
    )


def test_small_people_selection_change_can_continue_to_confirmation_and_ready(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, differences = prepare_runner(tmp_path, monkeypatch)
    agent = DecisionAgent([
        decision(label="1", x=80, y=70, reason="选择配置的1人"),
        decision(label="确认", x=150, y=140, reason="确认人数配置"),
        ready(),
    ])

    region, _, _, _ = run(runner, task, frame, output_dir, agent)

    assert [d.target_label for d in applied] == ["1", "确认"]
    assert differences[0] < 0.7
    assert agent.histories[1][0]["page_result"] == "unchanged"
    assert agent.histories[1][0]["visual_feedback"] == "no_visible_change"
    assert region.width == 180
    assert len(agent.histories) == 3


def test_two_distinct_same_page_options_can_continue_to_next_and_ready(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    agent = DecisionAgent([
        decision(label="已阅读须知", x=60, y=60),
        decision(label="接受协议", x=110, y=60),
        decision(label="下一步", x=190, y=145),
        ready(),
    ])

    run(runner, task, frame, output_dir, agent)

    assert [d.target_label for d in applied] == ["已阅读须知", "接受协议", "下一步"]
    assert len(agent.histories) == 4


def test_same_label_at_materially_different_coordinates_is_not_blocked(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    agent = DecisionAgent([
        decision(label="选项", x=50, y=50),
        decision(label="选项", x=170, y=110),
        ready(),
    ])

    run(runner, task, frame, output_dir, agent)

    assert [(d.x, d.y) for d in applied] == [(50, 50), (170, 110)]


def test_same_target_and_near_coordinate_retries_twice_then_stops(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    repeated = decision(label="选项", x=80, y=70)
    agent = DecisionAgent([repeated, repeated, repeated])

    with pytest.raises(NeedsReview):
        run(runner, task, frame, output_dir, agent)

    # 同一文字目标最多再点 1 次：第二次无变化即停止，不再多调一次模型。
    assert len(applied) == 2
    assert len(agent.histories) == 2


def test_next_model_history_contains_action_coordinates_and_page_observation(tmp_path, monkeypatch):
    runner, task, frame, output_dir, _, _, _ = prepare_runner(tmp_path, monkeypatch)
    agent = DecisionAgent([decision(label="选择", x=80, y=70), ready()])

    run(runner, task, frame, output_dir, agent)

    observed = agent.histories[1][0]
    assert observed["action"] == "click"
    assert observed["target_label"] == "选择"
    assert observed["x"] == 80
    assert observed["y"] == 70
    assert observed["page_result"] == "unchanged"


def test_small_pixel_change_is_reported_and_allows_next_confirmation_target(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    # 0.1 代表小幅页面变化：高于有效反馈阈值，仍允许后续不同目标继续。
    monkeypatch.setattr(runner, "_image_difference_images", lambda _left, _right: 0.1)
    agent = DecisionAgent([
        decision(label="选择", x=80, y=70),
        decision(label="确认", x=150, y=140),
        ready(),
    ])

    run(runner, task, frame, output_dir, agent)

    assert [d.target_label for d in applied] == ["选择", "确认"]
    observed = agent.histories[1][0]
    assert observed["image_difference"] == 0.1
    assert observed["visual_feedback"] == "major_change"
    assert observed["page_result"] == "changed"


def test_changed_same_target_retries_and_history_uses_executed_coordinates(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    changed_frames = []
    for name, color in (("after-1.png", "black"), ("after-2.png", "gray")):
        path = output_dir / name
        Image.new("RGB", (240, 160), color).save(path)
        changed_frames.append({"path": str(path), "width": 240, "height": 160})

    def apply_with_mapped_coordinates(d, *args, **kwargs):
        applied.append(d)
        # 模拟执行层已将模型建议点校正为截图像素坐标。
        runner._last_action_coordinate_mapping = {"executed_image_px": {"x": 111, "y": 91}}

    monkeypatch.setattr(runner, "_apply_scene_action", apply_with_mapped_coordinates)

    def screenshot(*args, **kwargs):
        assert changed_frames, "scripted frame sequence exhausted"
        return changed_frames.pop(0)

    monkeypatch.setattr(runner, "_screenshot", screenshot)
    repeated = decision(label="同一选项", x=80, y=70)
    agent = DecisionAgent([repeated, repeated, ready()])

    run(runner, task, frame, output_dir, agent)

    assert len(applied) == 2
    assert [history[-1]["page_result"] for history in agent.histories[1:3]] == ["changed", "changed"]
    assert agent.histories[1][-1]["x"] == 111
    assert agent.histories[1][-1]["y"] == 91


@pytest.mark.parametrize(
    ("difference", "page_result", "visual_feedback"),
    [(0.04, "unchanged", "small_change"), (0.06, "changed", "major_change")],
)
def test_change_threshold_boundary_allows_a_different_followup_target(
    tmp_path, monkeypatch, difference, page_result, visual_feedback,
):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_image_difference_images", lambda _left, _right: difference)
    agent = DecisionAgent([
        decision(label="选择选项", x=80, y=70),
        decision(label="确认选项", x=150, y=140),
        ready(),
    ])

    run(runner, task, frame, output_dir, agent)

    assert [d.target_label for d in applied] == ["选择选项", "确认选项"]
    assert agent.histories[1][-1]["page_result"] == page_result
    assert agent.histories[1][-1]["visual_feedback"] == visual_feedback


def test_expected_transition_without_change_gets_another_model_observation(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    finish = decision(label="继续", x=190, y=145, expects_transition=True)
    blocked = decision(
        action="blocked", label="", x=None, y=None,
        reason="确认按钮后页面仍未变化，需要检查页面状态",
    )
    agent = DecisionAgent([finish, blocked])

    with pytest.raises(NeedsReview):
        run(runner, task, frame, output_dir, agent)

    assert len(applied) == 1
    assert len(agent.histories) == 2
    assert agent.histories[1][-1]["expects_transition"] is True
    assert agent.histories[1][-1]["page_result"] == "unchanged"


def test_model_coordinate_failure_keeps_geometry_and_frame_for_diagnosis(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    runner.run_id = "isolated-geometry-test"
    runner.current_task = task
    runner._trace_attempt = "attempt-1"
    monkeypatch.setattr(runner, "_debug_frame_ref", lambda path: {"id": "isolated-frame"})

    class FailingAgent:
        last_geometry_diagnostics = {
            "raw_xy": {"x": 1234, "y": 500}, "requested_space": "normalized_999",
            "image_size": {"width": 240, "height": 160}, "converted_xy": None,
            "geometry_valid": False, "error_stage": "coordinate_conversion",
        }
        def decide(self, *args):
            raise SceneError("归一化坐标超出协议范围")

    with pytest.raises(SceneError):
        run(runner, task, frame, output_dir, FailingAgent())
    actions = runner.store.actions(task["id"])
    failed = next(row for row in actions if row["action"] == "model")
    assert failed["details"]["geometry"]["raw_xy"]["x"] == 1234
    assert failed["details"]["frame_evidence"] == {"id": "isolated-frame"}
    assert failed["result"] == "error"
    assert applied == []


def test_loading_waits_before_desktop_probe_and_times_out_without_menu_parse(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, screenshots, _ = prepare_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_should_capture_desktop", lambda *args: (True, {"reason": "loading"}))

    def screenshot(*args, **kwargs):
        desktop = bool(kwargs.get("desktop", False))
        screenshots.append(desktop)
        return {**frame, "capture_scope": "desktop" if desktop else "window"}

    monkeypatch.setattr(runner, "_screenshot", screenshot)
    clock = {"now": 1000.0}

    def monotonic():
        clock["now"] += 1.0
        return clock["now"]

    monkeypatch.setattr("menu_capture.uipath_runner.time.monotonic", monotonic)
    agent = DecisionAgent([
        SceneDecision.model_validate_json(scene_response(
            action="return_to_window", x=None, y=None, menu_region=None,
            reason="整屏没有系统授权弹窗",
        )),
    ])

    with pytest.raises(NeedsReview, match="一直加载不出内容"):
        run(runner, task, frame, output_dir, agent)

    assert applied == []
    assert screenshots[0] is False
    assert True in screenshots
    assert screenshots.count(True) == 1


class _CandidateVision:
    coordinate_space = "image_pixels"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def _request(self, _image, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.responses.pop(0), ensure_ascii=False)


def test_runner_passes_ocr_candidates_and_executes_candidate_center(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_read_scaled_ocr", lambda *args: ((240, 160), [
        {"text": "确定", "x": 300, "y": 210, "width": 60, "height": 30, "confidence": 0.95},
    ]))
    model = _CandidateVision([
        dict(
            action="click", reason="点击本地确认按钮", target_id="T1", x=None, y=None, pixels=0,
            menu_region=None, categories=[], overlay_visible=True, login_required=False,
            phone_required=False, consent_required=False, transaction_visible=False,
            target_label="", item_names_visible=[], prices_visible=[], observed_store_text=[],
            selected_people_count=None, expects_transition=True,
        ),
        dict(
            action="ready", reason="菜名价格可见", target_id=None, x=None, y=None, pixels=0,
            menu_region={"x": 20, "y": 20, "width": 180, "height": 120}, categories=["热菜"],
            overlay_visible=False, login_required=False, phone_required=False,
            consent_required=False, transaction_visible=False, target_label="",
            item_names_visible=["示例菜"], prices_visible=["¥18"], observed_store_text=[],
            selected_people_count=None, expects_transition=False,
        ),
    ])

    run(runner, task, frame, output_dir, SceneAgent(model))

    assert len(applied) == 1
    assert (applied[0].x, applied[0].y) == (110.0, 75.0)
    assert applied[0].target_id == "T1"
    assert "T1｜button｜确定" in model.prompts[0]


def test_transient_model_response_error_is_retried_once_before_action(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)

    class FlakyAgent:
        def __init__(self):
            self.calls = 0

        def decide(self, image_path, phase, history, candidates=None):
            self.calls += 1
            if self.calls == 1:
                raise SceneError("模型场景响应无效；本轮没有执行动作")
            return ready()

    agent = FlakyAgent()
    region, _, _, _ = run(runner, task, frame, output_dir, agent)

    assert agent.calls == 2
    assert region.width == 180
    assert applied == []


def test_consent_checkbox_no_change_falls_back_to_label_then_stops(tmp_path, monkeypatch):
    from menu_capture.model import Region

    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    consent = decision(label="我已了解并同意隐私协议", x=100, y=70, overlay_visible=True)
    consent = consent.model_copy(update={"target_box": Region(x=20, y=20, width=150, height=20)})
    agent = DecisionAgent([consent])

    with pytest.raises(NeedsReview, match="复选框和文字标签点击后均无变化"):
        run(runner, task, frame, output_dir, agent)

    assert len(applied) == 2
    assert applied[0].target_label == consent.target_label
    assert applied[1].reason.startswith("复选框点击无变化")


def test_fused_ocr_rescue_avoids_the_model_call(tmp_path, monkeypatch):
    runner, task, frame, output_dir, applied, _, _ = prepare_runner(tmp_path, monkeypatch)
    base_rows = [
        {"text": "页面文字", "x": 30, "y": 30, "width": 120, "height": 24, "confidence": 0.9},
    ]
    fused_rows = base_rows + [
        {"text": "同意", "x": 90, "y": 150, "width": 60, "height": 30, "confidence": 0.4},
    ]
    monkeypatch.setattr(runner, "_read_scaled_ocr", lambda *args: ((240, 160), base_rows))
    monkeypatch.setattr(runner, "_read_fused_scaled_ocr",
                        lambda pick, base=None: ((240, 160), fused_rows))
    monkeypatch.setattr(
        runner, "_local_ready_preflight",
        lambda path, name, ocr=None: ready() if ocr and any(
            row["text"] == "同意" for row in ocr[1]) else None,
    )
    agent = DecisionAgent([])

    region, _, _, _ = run(runner, task, frame, output_dir, agent)

    assert region.width == 180
    assert agent.histories == []
    assert applied == []
