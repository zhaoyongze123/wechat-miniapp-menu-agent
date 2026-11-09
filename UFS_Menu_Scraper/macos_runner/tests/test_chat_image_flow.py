"""发送图片到打开预览的契约测试，不操作真实微信。"""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from menu_capture.config import ConfigStore
from menu_capture.store import TaskStore
from menu_capture.uipath_runner import UiPathRunner, NeedsReview
from menu_capture.chat_image_locator import ChatImageNotFound


NAME = "ZA0070214＿再回首＿宁波市＿再回首单店"


def setup_flow(tmp_path):
    image = Image.new("RGB", (864, 958), "white")
    draw = ImageDraw.Draw(image)
    for y in (170, 570):
        draw.rectangle((270, y, 779, y + 39), fill=(153, 238, 153))
    image.save(tmp_path / "chat.png")
    commands, clipboard = [], []
    rows = [
        {"text": "文件传输助手", "x": 240, "y": 20},
        {"text": NAME, "x": 280, "y": 180, "width": 490, "height": 24},
        {"text": NAME, "x": 280, "y": 580, "width": 490, "height": 24},
    ]

    def no_qr_detection(_):
        raise AssertionError("打开聊天图片不应要求解码二维码")

    runner = UiPathRunner(
        ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"), tmp_path / "profile.json",
        clipboard_preparer=lambda p: clipboard.append(p),
        clipboard_text_preparer=lambda text: clipboard.append(text),
        ocr_reader=lambda _: rows,
        qr_detector=no_qr_detection,
    )
    runner._debug_frame_ref = lambda _: None
    runner._screenshot = lambda *_: {
        "path": str(tmp_path / "chat.png"), "width": 864, "height": 958,
        "window_width": 432, "window_height": 479,
    }
    runner._send = lambda command: commands.append({**command, "trace": dict(getattr(runner, "_trace_click", {}))}) or {"windows": []}
    runner._recognize_preview = lambda *_: {"preview_opened": True}
    task = {"id": "test", "source_name": NAME, "source_path": str(tmp_path / "source.png")}
    profile = {"wechat_selector": "assistant", "wechat_input_x_ratio": .7, "wechat_input_y_ratio": .9,
               "paste_shortcut": "Cmd+V", "send_key": "Enter"}
    return runner, task, profile, commands, clipboard


def test_repeated_store_names_use_last_anchor_and_convert_image_coordinates(tmp_path, monkeypatch):
    runner, task, profile, commands, clipboard = setup_flow(tmp_path)
    calls = []

    def locate(shot, source, anchor):
        calls.append((shot, source, anchor))
        return {"box_image_px": [628, 629, 788, 789], "center_image_px": {"x": 708, "y": 709},
                "method": "multiscale_thumbnail_match", "score": .96}

    monkeypatch.setattr("menu_capture.uipath_runner.locate_sent_image", locate)
    assert runner._open_source_automatic(task, profile, tmp_path, .3) == {"preview_opened": True}
    assert calls == [(tmp_path / "chat.png", Path(task["source_path"]), 610)]
    double_clicks = [c for c in commands if c["action"] == "double_click"]
    assert len(double_clicks) == 1
    assert double_clicks[0]["x"] == 354
    assert double_clicks[0]["y"] == 354.5
    assert double_clicks[0]["trace"]["source"] == "source_thumbnail_match"
    assert clipboard == [NAME, Path(task["source_path"])]


def test_missing_current_image_never_opens_history(tmp_path, monkeypatch):
    runner, task, profile, commands, _ = setup_flow(tmp_path)

    def missing(*_):
        raise ChatImageNotFound("未找到本轮图片", {"reason": "no_confident_match"})

    monkeypatch.setattr("menu_capture.uipath_runner.locate_sent_image", missing)
    with pytest.raises(NeedsReview, match="未找到本轮图片"):
        runner._open_source_automatic(task, profile, tmp_path, .3)
    assert not any(c["action"] == "double_click" for c in commands)


def test_latest_multiline_store_name_can_anchor_after_old_single_line(tmp_path):
    rows = [
        {"text": NAME, "x": 280, "y": 100, "width": 490, "height": 24},
        {"text": "ZA0070214＿再回首＿宁波市", "x": 280, "y": 500, "width": 490, "height": 24},
        {"text": "＿再回首单店", "x": 500, "y": 527, "width": 270, "height": 24},
    ]
    matches = UiPathRunner._store_name_matches(rows, NAME, 864)
    assert len(matches) == 2
    assert max(matches, key=lambda m: m["bottom"])["bottom"] == 551


def test_composer_draft_is_not_a_sent_store_name():
    rows = [
        {"text": NAME, "x": 280, "y": 500, "width": 490, "height": 24},
        {"text": NAME, "x": 240, "y": 836, "width": 490, "height": 20},
    ]
    matches = UiPathRunner._store_name_matches(rows, NAME, 864, 958)
    assert len(matches) == 1
    assert matches[0]["bottom"] == 524


def test_visible_thumbnail_opens_preview_without_resending(tmp_path, monkeypatch):
    runner, task, profile, commands, clipboard = setup_flow(tmp_path)
    monkeypatch.setattr("menu_capture.uipath_runner.locate_sent_image", lambda *_: {
        "box_image_px": [628, 629, 788, 789], "center_image_px": {"x": 708, "y": 709}})
    assert runner._open_source_automatic(task, profile, tmp_path, .3) == {"preview_opened": True}
    assert clipboard == [NAME, Path(task["source_path"])]
    # 文本粘贴+回车合并为一条；图片粘贴后要等缩略图附着，粘贴与回车分开两条。
    assert len([c for c in commands if c["action"] == "key"]) == 3
    assert len([c for c in commands if c["action"] == "double_click"]) == 1
