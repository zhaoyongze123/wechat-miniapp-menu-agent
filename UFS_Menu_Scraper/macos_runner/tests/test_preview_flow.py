"""预览扫码链路验证：只用已保存截图和假 UiPath 传输，不控制真实桌面。"""
from pathlib import Path
import shutil

import pytest

from menu_capture.config import ConfigStore
from menu_capture.store import TaskStore
from menu_capture.uipath_runner import UiPathRunner, NeedsReview


ROOT = Path(__file__).resolve().parents[1]
FRAME = ROOT / "data/verification/wechat-9c322e80/preview.png"
MAIN = {"app": "WeChat", "title": "微信", "role": "AXWindow", "position": {"x": 10, "y": 10, "width": 900, "height": 850}}
MINI = {"app": "WeChat", "title": "扫码测试门店", "role": "AXWindow", "position": {"x": 100, "y": 35, "width": 310, "height": 800}}


def setup_flow(tmp_path, *, opens_at=1, blank=False):
    runner = UiPathRunner(ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"), tmp_path / "profile.json")
    commands = []
    observations = 0
    runner._debug_frame_ref = lambda _: None

    def send(command, **_):
        nonlocal observations
        commands.append({**command, "trace": dict(getattr(runner, "_trace_click", {}))})
        action = command["action"]
        if action == "discover":
            observations += 1
            return {"windows": [MAIN] + ([MINI] if opens_at is not None and observations >= opens_at else [])}
        if action == "screenshot":
            path = Path(command["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            if blank:
                from PIL import Image
                Image.new("RGB", (825, 760), "white").save(path)
            else:
                shutil.copyfile(FRAME, path)
            return {"path": str(path), "width": 825, "height": 760,
                    "window_width": 412.5, "window_height": 380}
        return {"ok": True}

    runner._send = send
    return runner, commands


def test_visual_preview_click_converts_current_pixels_and_waits_for_open(tmp_path):
    runner, commands = setup_flow(tmp_path, opens_at=3)
    profile = {"preview_selector": "preview"}
    result = runner._recognize_preview(profile, tmp_path / "out", [MAIN], .3)
    clicks = [c for c in commands if c["action"] == "click"]
    assert len(clicks) == 1
    assert clicks[0]["selector"] == "preview"
    assert clicks[0]["x"] == pytest.approx(269.5, abs=1)
    assert clicks[0]["y"] == pytest.approx(10.25, abs=1)
    assert clicks[0]["trace"]["source"] == "local_preview_icon_match"
    assert len([c for c in commands if c["action"] == "discover"]) == 3
    assert "扫码测试门店" in profile["miniapp_selector"]
    # 拿到窗口即返回，页面内容交给后续 resize + 就绪判断，不在这里多截一张图。
    assert result["miniapp_selector"] == profile["miniapp_selector"]
    assert not [c for c in commands
                if c["action"] == "screenshot" and c["path"].endswith("miniapp-after-qr.png")]


def test_missing_icon_never_clicks(tmp_path):
    runner, commands = setup_flow(tmp_path, blank=True)
    with pytest.raises(NeedsReview):
        runner._recognize_preview({"preview_selector": "preview"}, tmp_path / "out", [MAIN], .3)
    assert not any(c["action"] == "click" for c in commands)


def test_multiple_new_miniapp_windows_take_first_and_leave_choice_to_agent(tmp_path):
    runner, _ = setup_flow(tmp_path, opens_at=1)
    other = {**MINI, "title": "另一家门店"}
    send = runner._send

    def multiple(command, **kwargs):
        reply = send(command, **kwargs)
        return {"windows": [MAIN, MINI, other]} if command["action"] == "discover" else reply

    runner._send = multiple
    profile = {"preview_selector": "preview"}
    result = runner._recognize_preview(profile, tmp_path / "out", [MAIN], .3)
    expected = UiPathRunner._miniapp_candidates([MINI, other])[0]["selector"]
    assert result["miniapp_selector"] == expected
    assert profile["miniapp_selector"] == expected


def test_missing_icon_records_external_prompt_without_business_verdict(tmp_path):
    runner, commands = setup_flow(tmp_path, blank=True)
    runner._preview_external_link = lambda *args, **kwargs: True
    with pytest.raises(NeedsReview) as raised:
        runner._recognize_preview({"preview_selector": "preview"}, tmp_path / "out", [MAIN], .3)
    assert raised.value.code == "startup_preview_unavailable"
    assert "非微信小程序" not in str(raised.value)
    assert not any(c["action"] == "click" for c in commands)


def test_recognition_does_not_repeat_click_when_no_miniapp_appears(tmp_path):
    runner, commands = setup_flow(tmp_path, opens_at=None)
    reads = []
    runner.ocr_reader = lambda path: reads.append(path) or [{"text": "无法识别二维码"}]
    with pytest.raises(NeedsReview, match="未重复点击"):
        runner._recognize_preview({"preview_selector": "preview"}, tmp_path / "out", [MAIN], .3)
    assert len([c for c in commands if c["action"] == "click"]) == 1
    assert len([c for c in commands if c["action"] == "discover"]) == 3
    assert len(reads) == 1
    assert (tmp_path / "out/evidence/preview-recognition-result.png").exists()
