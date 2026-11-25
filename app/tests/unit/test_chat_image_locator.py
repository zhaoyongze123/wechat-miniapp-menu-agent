from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from menu_capture.desktop.chat_image_locator import ChatImageNotFound, locate_sent_image


def _source_image(tmp_path: Path, name: str, *, other_store: bool = False) -> Path:
    """用几何图形合成“菜单原图”夹具，避免依赖本机历史素材。

    构图刻意不对称（L 形 + 圆 + 斜线 + 小块），缩略图缩小后仍只有一个明确峰值；
    另一家门店的构图完全不同，用于覆盖“别家原图不应命中”的负例。
    """
    image = np.full((480, 480, 3), 235, dtype=np.uint8)
    if other_store:
        for offset in range(0, 480, 60):
            cv2.line(image, (0, offset), (offset, 0), (0, 0, 0), 12)
        cv2.circle(image, (140, 360), 80, (30, 150, 220), -1)
        cv2.rectangle(image, (300, 60), (430, 380), (150, 30, 150), 14)
    else:
        cv2.rectangle(image, (40, 40), (150, 90), (10, 10, 10), -1)
        cv2.rectangle(image, (40, 40), (70, 300), (10, 10, 10), -1)
        cv2.circle(image, (360, 330), 95, (20, 20, 200), -1)
        cv2.line(image, (120, 430), (420, 180), (200, 60, 20), 18)
        cv2.rectangle(image, (200, 120), (260, 150), (60, 160, 40), -1)
    path = tmp_path / name
    assert cv2.imwrite(str(path), image)
    return path


def _insert_source_thumbnail(canvas: np.ndarray, source_path: Path, x: int, y: int, size: int = 160):
    source = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
    assert source is not None
    h, w = source.shape[:2]
    side = min(h, w)
    crop = source[(h - side) // 2 : (h + side) // 2, (w - side) // 2 : (w + side) // 2]
    thumb = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
    canvas[y : y + size, x : x + size] = thumb
    return (x, y, x + size, y + size)


def _blank_chat(width=900, height=900):
    return np.full((height, width, 3), 248, dtype=np.uint8)


def test_matches_thumbnail_at_changed_scale_and_position(tmp_path):
    source = _source_image(tmp_path, "source-a.png")
    screenshot = _blank_chat()
    _insert_source_thumbnail(screenshot, source, 650, 250, 146)
    shot_path = tmp_path / "scaled.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    found = locate_sent_image(shot_path, source, anchor_bottom=190)
    left, top, right, bottom = found["box_image_px"]
    assert abs(left - 650) <= 5
    assert abs(top - 250) <= 5
    assert abs((right - left) - 146) <= 8


def test_matches_two_x_thumbnail_scale(tmp_path):
    source = _source_image(tmp_path, "source-a.png")
    screenshot = _blank_chat(width=1200, height=1200)
    _insert_source_thumbnail(screenshot, source, 760, 220, 300)
    shot_path = tmp_path / "two-x.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    found = locate_sent_image(shot_path, source, anchor_bottom=150)
    left, top, right, bottom = found["box_image_px"]
    assert abs(left - 760) <= 8
    assert abs(top - 220) <= 8
    assert 285 <= right - left <= 315


def test_does_not_match_same_image_above_anchor(tmp_path):
    source = _source_image(tmp_path, "source-a.png")
    screenshot = _blank_chat()
    _insert_source_thumbnail(screenshot, source, 650, 170, 160)
    shot_path = tmp_path / "history-only.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound) as exc:
        locate_sent_image(shot_path, source, anchor_bottom=400)
    assert exc.value.diagnostics["search_region_px"][1] == 400


def test_rejects_when_candidate_thumbnail_disappears(tmp_path):
    source = _source_image(tmp_path, "source-a.png")
    screenshot = _blank_chat()
    box = _insert_source_thumbnail(screenshot, source, 650, 250, 146)
    # 把已出现的缩略图整块擦成聊天背景，模拟“图片其实没发出去”。
    left, top, right, bottom = box
    screenshot[top:bottom, left:right] = (248, 248, 248)
    shot_path = tmp_path / "removed.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound):
        locate_sent_image(shot_path, source, anchor_bottom=190)


def test_rejects_another_stores_source_image(tmp_path):
    source = _source_image(tmp_path, "source-a.png")
    other_store_source = _source_image(tmp_path, "source-b.png", other_store=True)
    screenshot = _blank_chat()
    _insert_source_thumbnail(screenshot, source, 650, 250, 146)
    shot_path = tmp_path / "other-store.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound):
        locate_sent_image(shot_path, other_store_source, anchor_bottom=190)


def test_rejects_two_equally_likely_thumbnail_candidates(tmp_path):
    source = _source_image(tmp_path, "source-a.png")
    screenshot = _blank_chat()
    _insert_source_thumbnail(screenshot, source, 650, 145, 140)
    _insert_source_thumbnail(screenshot, source, 650, 305, 140)
    shot_path = tmp_path / "duplicates.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound) as exc:
        locate_sent_image(shot_path, source, anchor_bottom=100)
    assert exc.value.diagnostics["reason"] == "ambiguous_matches"
