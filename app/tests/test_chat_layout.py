from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from menu_capture.chat_anchor import find_store_name_matches
from menu_capture.chat_layout import ChatImageUnreadable, find_sent_text_bubbles


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "action-evidence"
LATEST_RUN_LOG = ROOT / "data" / "debug-logs" / "segments" / "20261105-225758-008986-0001.jsonl"

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


def _near_box(found: dict, expected: tuple[int, int, int, int], tolerance: int = 2) -> bool:
    actual = tuple(int(found[key]) for key in ("left", "top", "right", "bottom"))
    return all(abs(value - target) <= tolerance for value, target in zip(actual, expected))


def _latest_failure_ocr() -> list[dict]:
    with LATEST_RUN_LOG.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("action_id") == 1314 and record.get("action") == "wechat_ocr":
                return record["details"]["ocr_rows"]
    raise AssertionError("full OCR replay for action 1314 is unavailable")


def test_finds_the_sent_store_text_bubble_in_14_prior_images_and_latest_failure():
    assert len(REAL_BUBBLES) == 15
    for name, expected in REAL_BUBBLES:
        path = EVIDENCE / name
        assert path.is_file(), f"missing real screenshot fixture: {path}"
        boxes = find_sent_text_bubbles(path)
        assert any(_near_box(box, expected) for box in boxes), (name, boxes)


def test_latest_failure_uses_bubble_bottom_and_does_not_claim_photo_ocr():
    path = EVIDENCE / "dbd992125c4241c69bfae86b7b8a3adf.png"
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


def test_message_box_keeps_wrapped_rows_and_duplicate_message_ownership():
    rows = [
        {"text": "ZA0070551_冒二三烤鸭_大连市_红烧尚（大连）餐饮管理有", "x": 376, "y": 556, "width": 398, "height": 20},
        {"text": "限公司单店", "x": 378, "y": 576, "width": 70, "height": 18},
        {"text": "ZA0070551_冒二三烤鸭_大连市_红烧尚（大连）餐饮管理有", "x": 376, "y": 700, "width": 398, "height": 20},
    ]
    boxes = [
        {"left": 367, "top": 545, "right": 793, "bottom": 602, "method": "test"},
        {"left": 367, "top": 689, "right": 793, "bottom": 726, "method": "test"},
    ]

    matches = find_store_name_matches(
        rows,
        "ZA0070551＿冒二三烤鸭＿大连市＿红烧尚（大连）餐饮管理有限公司单店",
        864,
        958,
        message_boxes=boxes,
    )

    assert len(matches) == 2
    assert matches[0]["text"].endswith("限公司单店")
    assert [match["bottom"] for match in matches] == [602, 726]
    assert [match["message_box"]["method"] for match in matches] == ["test", "test"]


def test_explicitly_unknown_segmentation_fails_closed_but_omitted_boxes_keep_legacy():
    rows = [
        {"text": "ZA0070005_老胖串店_济南市_单店", "x": 400, "y": 200, "width": 260, "height": 20},
        {"text": "照片内的老胖串店", "x": 405, "y": 240, "width": 140, "height": 20},
    ]
    source = "ZA0070005＿老胖串店＿济南市＿单店"

    assert len(find_store_name_matches(rows, source, 864, 958)) == 1
    assert find_store_name_matches(rows, source, 864, 958, message_boxes=[]) == []


def test_ignores_non_light_theme_green_fill(tmp_path):
    """只支持浅色主题：暗绿色填充属于其它皮肤或照片内容，不当作文本气泡。"""
    image = np.full((400, 600, 3), (45, 45, 45), dtype=np.uint8)
    cv2.rectangle(image, (300, 170), (570, 215), (38, 92, 58), thickness=-1)
    path = tmp_path / "dark-chat.png"
    assert cv2.imwrite(str(path), image)

    assert find_sent_text_bubbles(path) == []


def test_large_green_photo_region_is_not_a_sent_text_bubble(tmp_path):
    image = np.full((800, 600, 3), (245, 245, 245), dtype=np.uint8)
    # 照片内的绿色卡片远高于文本气泡；不能拥有其下方图片里的 OCR 行。
    cv2.rectangle(image, (330, 260), (590, 620), (153, 238, 153), thickness=-1)
    path = tmp_path / "green-photo.png"
    assert cv2.imwrite(str(path), image)

    assert find_sent_text_bubbles(path) == []


def test_reads_screenshot_under_chinese_output_path(tmp_path):
    """采集输出目录可含中文，读图不能依赖 ANSI 路径。"""
    image = np.full((400, 600, 3), (245, 245, 245), dtype=np.uint8)
    cv2.rectangle(image, (300, 170), (570, 215), (95, 191, 95), thickness=-1)
    chinese_dir = tmp_path / "输出" / "截图输出" / "清洗批次"
    chinese_dir.mkdir(parents=True)
    path = chinese_dir / "wechat-after-send.png"
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(str(path))

    boxes = find_sent_text_bubbles(path)

    assert len(boxes) == 1
    assert _near_box(boxes[0], (300, 170, 571, 216))


def test_unreadable_screenshot_raises_dedicated_error(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")

    with pytest.raises(ChatImageUnreadable):
        find_sent_text_bubbles(path)

