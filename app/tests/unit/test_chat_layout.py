from __future__ import annotations

import cv2
import numpy as np
import pytest

from menu_capture.desktop.chat_anchor import find_store_name_matches
from menu_capture.desktop.chat_layout import ChatImageUnreadable, find_sent_text_bubbles


def _near_box(found: dict, expected: tuple[int, int, int, int], tolerance: int = 2) -> bool:
    actual = tuple(int(found[key]) for key in ("left", "top", "right", "bottom"))
    return all(abs(value - target) <= tolerance for value, target in zip(actual, expected))


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

