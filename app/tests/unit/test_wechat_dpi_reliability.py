"""微信全链路 DPI 自适应、输入框多策略清空与连续环境故障熔断测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from menu_capture.capture.uipath_errors import NeedsReview
from menu_capture.capture.uipath_runner import (
    UiPathRunner,
    _chat_pane_right,
    _locate_wechat_input_region,
)
from menu_capture.quality.failures import ENVIRONMENT, bucket_of, describe


def _make_wechat_screenshot(
    path: Path,
    *,
    scale: float = 1.0,
    with_right_panel: bool = False,
    send_active: bool = False,
    panel_green: bool = False,
) -> tuple[int, int]:
    """按指定 DPI 缩放绘制接近微信 4.x 结构的合成窗口截图。"""
    width = round(999 * scale)
    height = round(749 * scale)
    img = Image.new("RGB", (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(img)

    pane_right = round(width * 0.62) if with_right_panel else width
    chat_left = round(pane_right * 0.28)
    divider_y = round(height * 0.76)

    # 左侧聊天列表背景与竖向分栏线
    draw.rectangle((0, 0, chat_left - 1, height), fill=(235, 235, 235))
    draw.line((chat_left, 0, chat_left, height), fill=(190, 190, 190), width=max(1, round(scale)))

    # 聊天正文区与底部输入区横向分界线
    draw.rectangle((chat_left + 1, divider_y, pane_right - 1, height), fill=(252, 252, 252))
    draw.line((chat_left, divider_y, pane_right, divider_y), fill=(190, 190, 190), width=max(1, round(scale)))

    # 右侧内嵌小程序面板与竖向分栏线
    if with_right_panel:
        draw.rectangle((pane_right, 0, width, height), fill=(255, 255, 255))
        draw.line((pane_right, 0, pane_right, height), fill=(180, 180, 180), width=max(1, round(scale)))
        if panel_green:
            # 右侧面板里的绿色按钮，不能误判为聊天发送按钮
            gx = width - round(90 * scale)
            gy = height - round(50 * scale)
            draw.rectangle((gx, gy, gx + round(70 * scale), gy + round(32 * scale)), fill=(7, 193, 96))

    # 聊天发送按钮（右下角）
    btn_w = round(76 * scale)
    btn_h = round(30 * scale)
    btn_x2 = pane_right - round(18 * scale)
    btn_y2 = height - round(12 * scale)
    btn_color = (7, 193, 96) if send_active else (225, 225, 225)
    draw.rectangle((btn_x2 - btn_w, btn_y2 - btn_h, btn_x2, btn_y2), fill=btn_color)

    img.save(path)
    return width, height


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
@pytest.mark.parametrize("with_right_panel", [False, True])
def test_dynamic_input_region_and_send_button_across_dpi(tmp_path: Path, scale: float, with_right_panel: bool):
    idle_path = tmp_path / f"idle-{scale}-{with_right_panel}.png"
    active_path = tmp_path / f"active-{scale}-{with_right_panel}.png"
    width, height = _make_wechat_screenshot(
        idle_path, scale=scale, with_right_panel=with_right_panel, send_active=False, panel_green=True
    )
    _make_wechat_screenshot(
        active_path, scale=scale, with_right_panel=with_right_panel, send_active=True, panel_green=True
    )

    with Image.open(idle_path) as img:
        pane = _chat_pane_right(img)
        layout = _locate_wechat_input_region(img, dpi_scale=scale)

    expected_pane = round(width * 0.62) if with_right_panel else width
    assert abs(pane - expected_pane) <= max(3, round(3 * scale))
    assert layout["detected"] is True
    in_left, in_top, in_right, in_bottom = layout["box"]
    cx, cy = layout["center"]
    assert in_left < cx < in_right < pane
    assert in_top < cy < in_bottom < height
    # 中心点必须远离发送按钮（不会误点发送按钮）
    assert cx < pane - round(100 * scale)

    # 右侧面板里有绿色按钮时，聊天区空闲仍须判为未激活；聊天发送按钮变绿时须判为激活
    assert UiPathRunner._send_button_active(idle_path, dpi_scale=scale) is False
    assert UiPathRunner._send_button_active(active_path, dpi_scale=scale) is True


def test_wechat4_top_line_text_detected_below_divider(tmp_path: Path):
    """微信 4.x 工具栏在底部，首行短文本紧贴分界线下方（y ≈ divider_y + 10），差分检测不得裁掉首行。"""
    base_path = tmp_path / "base.png"
    text_path = tmp_path / "text.png"
    width, height = _make_wechat_screenshot(base_path, scale=1.0, send_active=False)
    _make_wechat_screenshot(text_path, scale=1.0, send_active=True)

    with Image.open(text_path) as img:
        draw = ImageDraw.Draw(img)
        divider_y = round(height * 0.78)
        chat_left = round(width * 0.28)
        # 在紧贴分界线下方 10~24px 绘制一段短门店编号（模拟 ZA0070318）
        draw.rectangle((chat_left + 20, divider_y + 10, chat_left + 100, divider_y + 24), fill=(30, 30, 30))
        img.save(text_path)

    state = UiPathRunner._input_content(text_path, base_path, dpi_scale=1.0)
    assert state["kind"] == "text"


def test_clear_wechat_input_multi_strategy_succeeds_and_fails_with_env_code(tmp_path: Path):
    active_img = tmp_path / "active.png"
    idle_img = tmp_path / "idle.png"
    w, h = _make_wechat_screenshot(active_img, scale=1.5, send_active=True)
    _make_wechat_screenshot(idle_img, scale=1.5, send_active=False)

    runner = UiPathRunner.__new__(UiPathRunner)
    runner._guard = lambda: None
    runner._trace_click = {}
    runner.store = SimpleNamespace(event=lambda *a, **kw: None)

    from contextlib import contextmanager

    @contextmanager
    def _noop_trace(*a, **kw):
        yield {}

    runner._trace = _noop_trace

    sent_actions: list[dict] = []
    shot_sequence = [active_img, active_img, idle_img]  # probe=active, strategy1=active, strategy2=idle

    def fake_screenshot(output_dir, selector, filename, **kw):
        src = shot_sequence.pop(0)
        return {"path": str(src), "width": w, "height": h, "window_width": w, "window_height": h, "dpi_scale": 1.5}

    runner._screenshot = fake_screenshot
    runner._send = lambda cmd, timeout=None: sent_actions.append(dict(cmd)) or {"ok": True}

    profile = {"wechat_selector": "<wnd app='Weixin.exe' title='微信' />"}
    wechat = {"path": str(active_img), "width": w, "height": h, "window_width": w, "window_height": h, "dpi_scale": 1.5}

    runner._clear_wechat_input(profile, wechat, tmp_path)
    # 至少经历了策略 1 和策略 2 的点击与清空按键
    clicks = [a for a in sent_actions if a["action"] == "click"]
    keys = [a["key"] for a in sent_actions if a["action"] == "key"]
    assert len(clicks) == 2
    assert clicks[0]["x"] != clicks[1]["x"]  # 策略 1 居中，策略 2 偏左
    assert any("backspace" in k for k in keys)
    assert any("delete" in k for k in keys)

    # 3 种策略全部无法清空时，必须抛出 wechat_input_env_error 环境故障码
    sent_actions.clear()
    shot_sequence[:] = [active_img, active_img, active_img, active_img]
    with pytest.raises(NeedsReview) as exc_info:
        runner._clear_wechat_input(profile, wechat, tmp_path)
    assert exc_info.value.code == "wechat_input_env_error"
    assert bucket_of(exc_info.value.code) == ENVIRONMENT
    assert describe(exc_info.value.code)["bucket"] == ENVIRONMENT


def test_normalize_wechat_frame_scales_by_dpi_and_clamps_to_work_area(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    runner = UiPathRunner.__new__(UiPathRunner)
    calls: list[dict] = []

    def fake_send(cmd, timeout=None):
        calls.append(dict(cmd))
        if cmd["action"] == "discover":
            return {
                "windows": [{
                    "app": "Weixin.exe",
                    "title": "微信",
                    "cls": "QtWindow",
                    "dpi_scale": 1.5,
                    "work_area": {"x": 1920, "y": 0, "width": 1400, "height": 900},
                    "position": {"x": 1950, "y": 40, "width": 800, "height": 600},
                }]
            }
        if cmd["action"] == "resize":
            return {"ok": True, "window_width": cmd["width"], "window_height": cmd["height"]}
        return {"ok": True}

    runner._send = fake_send
    runner._window_list = UiPathRunner._window_list
    profile = {"wechat_selector": "<wnd app='Weixin.exe' title='微信' />"}
    runner._normalize_wechat_frame(profile)

    resize_cmd = next(c for c in calls if c["action"] == "resize")
    # 999*1.5=1499 被工作区 1400-20=1380 钳位；749*1.5=1124 被工作区 900-20=880 钳位
    assert resize_cmd["width"] == 1380
    assert resize_cmd["height"] == 880


def test_resize_miniapp_window_uses_monitor_work_area_and_dpi(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    runner = UiPathRunner.__new__(UiPathRunner)
    calls: list[dict] = []
    runner._send = lambda cmd, timeout=None: calls.append(dict(cmd)) or {"ok": True}

    window = {
        "dpi_scale": 1.5,
        "work_area": {"x": 1920, "y": 40, "width": 1920, "height": 1000},
        "position": {"x": 2200, "y": 100, "width": 800, "height": 600},
    }
    runner._resize_miniapp_window("<wnd app='WeChatAppEx.exe' title='小程序' />", window=window)

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd["x"] == 1920
    assert cmd["y"] == 40 + round(35 * 1.5)
    assert cmd["width"] == round(240 * 1.5)
    assert cmd["height"] == 1000 - round(100 * 1.5)


def test_circuit_breaker_pauses_after_three_consecutive_input_env_failures(tmp_path: Path):
    """连续 3 家触发微信输入环境故障时自动暂停批次，不继续消耗剩余门店。"""
    runner = UiPathRunner.__new__(UiPathRunner)
    runner.run_id = "run-cb-test"
    runner.run_directory = tmp_path / "run"
    runner.run_directory.mkdir()
    runner._pause_at_boundary = False
    runner.assignment_source = None
    runner._guard = lambda: None
    runner._await_cli_startup = lambda: None
    runner._prepare_batch_wechat_frame = lambda profile: None
    runner._capture_run_progress = lambda done, total, next_id: None
    runner._reap_cli = lambda: None
    runner.transport = SimpleNamespace(request_stop=lambda: None)

    from contextlib import contextmanager

    @contextmanager
    def _noop_trace(*a, **kw):
        yield {}

    runner._trace = _noop_trace
    events: list[tuple] = []
    runner.store = SimpleNamespace(
        get=lambda tid: {"id": tid, "source_name": tid, "source_path": str(tmp_path / f"{tid}.png")},
        event=lambda msg, level="info", task_id=None: events.append((level, msg, task_id)),
    )
    finished_summary: dict = {}
    runner._finish_capture_run = lambda summary: finished_summary.update(summary)
    runner._persist_trial_case = lambda task, case_dir, report, settings: "needs_review"

    executed: list[str] = []

    def fake_case(task, settings, profile, output_dir, model, automatic=False):
        executed.append(task["id"])
        return {
            "task_id": task["id"],
            "runtime_status": "error",
            "runtime_code": "wechat_input_env_error",
            "runtime_error": "输入框残留内容无法清空；未发送，避免串店名",
            "wechat_return": {"status": "ready"},
        }

    runner._run_ui_tars_trial_case = fake_case

    batch_sources = {f"store-{i}": str(tmp_path) for i in range(1, 8)}
    settings = SimpleNamespace(model_copy=lambda update: SimpleNamespace(**update))
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    runner._ui_tars_trial_worker(
        {"id": "store-1"}, settings, {"mode": "automatic"}, output_dir, None, batch_sources=batch_sources
    )

    assert executed == ["store-1", "store-2", "store-3"]
    assert finished_summary["status"] == "paused"
    assert finished_summary["pause_reason"] == "wechat_input_env_circuit_breaker"
    assert len(finished_summary["unstarted_task_ids"]) == 4
    assert any("连续 3 家门店触发微信输入环境故障" in msg for _, msg, _ in events)
