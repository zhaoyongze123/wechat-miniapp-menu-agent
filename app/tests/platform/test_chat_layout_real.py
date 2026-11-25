"""真实微信聊天截图与调试日志的气泡回归。

需要 Windows 采集归档的 15 张历史截图和完整 Apple Vision OCR 段日志，
无法在干净环境合成；默认跳过，显式设置 `MENU_CAPTURE_REAL_DATA=1` 后运行。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from menu_capture.desktop.chat_anchor import find_store_name_matches
from menu_capture.desktop.chat_layout import find_sent_text_bubbles


APP_ROOT = Path(__file__).resolve().parents[2]
REAL_ROOT = Path(os.environ.get("MENU_CAPTURE_REAL_DATA_DIR", APP_ROOT / "data"))
EVIDENCE = REAL_ROOT / "action-evidence"
LATEST_RUN_LOG = REAL_ROOT / "debug-logs" / "segments" / "20261105-225758-008986-0001.jsonl"

# 2026-11-05 18:54 批次的 14 张已归档截图，加上本次 22:58 老胖失败截图。
# 坐标是气泡检测器的期望区域，仅作历史图像回归，不供生产定位使用。
REAL_BUBBLES = (
    ("6db13db7032a41dba3f79bbb2d1877d0.png", (434, 592, 793, 629)),
    ("9a6fffc567764ef6a950130b5197ab43.png", (490, 593, 793, 630)),
    ("8e9af3a6381c49c4b857ab194db9e29d.png", (407, 559, 793, 596)),
    ("9f85a79be64c4c9a9e39438f3de8779b.png", (435, 592, 793, 629)),
    ("1f465154b1fc439bab98b7b18e55abb4.png", (518, 592, 793, 629)),
    ("8fcc2c8e833e4d659fdee11b41ae1919.png", (435, 592, 793, 629)),
    ("7b00f85710434c2e8296fecb37909fa9.png", (462, 592, 793, 629)),
    ("e5d0577847884d8a820d7d7b26c743f5.png", (367, 545, 793, 602)),
    ("5817a2d983504b8284fdb611cb40f9e3.png", (464, 592, 793, 629)),
    ("ceb74c2704994bfb9f31a8113fcf5e9f.png", (439, 592, 793, 629)),
    ("dbf68735b637458cb50930f7c7e32979.png", (466, 592, 793, 629)),
    ("4700e661ba1a43c4ab84c6eff6c77b83.png", (462, 567, 793, 604)),
    ("8cc1860c78fe4386a4973ca7bcddbc33.png", (478, 592, 793, 629)),
    ("b4feed6b679a430aab0d22b08751bde3.png", (493, 589, 793, 626)),
    ("dbd992125c4241c69bfae86b7b8a3adf.png", (434, 592, 793, 629)),
)

pytestmark = pytest.mark.skipif(
    os.environ.get("MENU_CAPTURE_REAL_DATA") != "1",
    reason="需要真实采集数据（app/data/action-evidence 历史截图与 data/debug-logs 段日志）；"
           "设置 MENU_CAPTURE_REAL_DATA=1 后运行",
)


def _near_box(found: dict, expected: tuple[int, int, int, int], tolerance: int = 2) -> bool:
    actual = tuple(int(found[key]) for key in ("left", "top", "right", "bottom"))
    return all(abs(value - target) <= tolerance for value, target in zip(actual, expected))


def _latest_failure_ocr() -> list[dict]:
    if not LATEST_RUN_LOG.is_file():
        pytest.skip(f"真实调试日志未提供：{LATEST_RUN_LOG}")
    with LATEST_RUN_LOG.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("action_id") == 1314 and record.get("action") == "wechat_ocr":
                return record["details"]["ocr_rows"]
    pytest.skip("真实调试日志缺少 action 1314 的完整 OCR 记录")


def test_finds_the_sent_store_text_bubble_in_14_prior_images_and_latest_failure():
    assert len(REAL_BUBBLES) == 15
    for name, expected in REAL_BUBBLES:
        path = EVIDENCE / name
        if not path.is_file():
            pytest.skip(f"真实聊天截图未提供：{path}")
        boxes = find_sent_text_bubbles(path)
        assert any(_near_box(box, expected) for box in boxes), (name, boxes)


def test_latest_failure_uses_bubble_bottom_and_does_not_claim_photo_ocr():
    path = EVIDENCE / "dbd992125c4241c69bfae86b7b8a3adf.png"
    if not path.is_file():
        pytest.skip(f"真实失败截图未提供：{path}")
    # 使用真实日志中的完整 Apple Vision OCR，不把问题简化成手工挑出的几行。
    rows = _latest_failure_ocr()
    boxes = find_sent_text_bubbles(path)

    legacy = find_store_name_matches(
        rows, "ZA0070005＿老胖串店＿济南市＿老胖串店中央厨房", 864, 958
    )
    owned = find_store_name_matches(
        rows, "ZA0070005＿老胖串店＿济南市＿老胖串店中央厨房", 864, 958,
        message_boxes=boxes,
    )

    assert round(legacy[0]["bottom"]) == 686
    assert len(owned) == 1
    assert owned[0]["bottom"] == 629
    assert "ZA0070005" in owned[0]["text"]
    assert "St 老胖串店" not in owned[0]["text"]
    assert "溶都" not in owned[0]["text"]
    assert owned[0]["message_box"]["method"] == "wechat_green_text_bubble_hsv"
    assert owned[0]["message_box"]["top"] == 592
