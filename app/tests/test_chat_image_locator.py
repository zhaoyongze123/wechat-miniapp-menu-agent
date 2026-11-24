from __future__ import annotations

import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytest

from menu_capture.chat_image_locator import ChatImageNotFound, locate_sent_image


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "tasks.sqlite3"
EVIDENCE = ROOT / "data" / "action-evidence"
SAMPLES = (
    ("eb660f76c1ee4c9fabd7267ae96d5b61.png", "b8befb08d9d5de834e71", 604),
    ("d5dc5fceccb34a5499ad9339352bbbcb.png", "fb509e40668b6c659188", 602),
)


def _task_source(task_id: str) -> Path:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        row = conn.execute("select source_path from tasks where id=?", (task_id,)).fetchone()
    if not row:
        pytest.skip(f"task fixture {task_id} is unavailable")
    source = Path(row[0])
    if not source.is_file():
        pytest.skip(f"source image fixture is unavailable: {source}")
    return source


@pytest.mark.parametrize("screenshot_name,task_id,anchor", SAMPLES)
def test_locates_real_sent_thumbnail(screenshot_name, task_id, anchor):
    screenshot = EVIDENCE / screenshot_name
    if not screenshot.is_file():
        pytest.skip(f"screenshot fixture is unavailable: {screenshot}")

    found = locate_sent_image(screenshot, _task_source(task_id), anchor)

    left, top, right, bottom = found["box_image_px"]
    assert 620 <= left <= 640
    assert 620 <= top <= 640
    assert 770 <= right <= 800
    assert 770 <= bottom <= 800
    assert found["score"] >= 0.70
    assert found["method"] == "multiscale_thumbnail_match"
    assert set(found["center_image_px"]) == {"x", "y"}


def _insert_source_thumbnail(canvas: np.ndarray, source_path: Path, x: int, y: int, size: int = 160):
    source = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
    assert source is not None
    h, w = source.shape[:2]
    side = min(h, w)
    crop = source[(h - side) // 2 : (h + side) // 2, (w - side) // 2 : (w + side) // 2]
    thumb = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
    canvas[y : y + size, x : x + size] = thumb


def _blank_chat(width=900, height=900):
    return np.full((height, width, 3), 248, dtype=np.uint8)


def test_matches_thumbnail_at_changed_scale_and_position(tmp_path):
    source = _task_source(SAMPLES[0][1])
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
    source = _task_source(SAMPLES[0][1])
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
    source = _task_source(SAMPLES[0][1])
    screenshot = _blank_chat()
    _insert_source_thumbnail(screenshot, source, 650, 170, 160)
    shot_path = tmp_path / "history-only.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound) as exc:
        locate_sent_image(shot_path, source, anchor_bottom=400)
    assert exc.value.diagnostics["search_region_px"][1] == 400


def test_rejects_when_candidate_thumbnail_disappears(tmp_path):
    screenshot = cv2.imread(str(EVIDENCE / SAMPLES[0][0]), cv2.IMREAD_COLOR)
    source = _task_source(SAMPLES[0][1])
    if screenshot is None:
        pytest.skip("real screenshot fixture is unavailable")
    # Remove the whole displayed thumbnail, leaving the surrounding white chat area.
    screenshot[620:800, 615:800] = (248, 248, 248)
    shot_path = tmp_path / "removed.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound):
        locate_sent_image(shot_path, source, anchor_bottom=604)


def test_rejects_another_stores_source_image(tmp_path):
    screenshot = EVIDENCE / SAMPLES[0][0]
    if not screenshot.is_file():
        pytest.skip("real screenshot fixture is unavailable")
    other_store_source = _task_source(SAMPLES[1][1])

    with pytest.raises(ChatImageNotFound):
        locate_sent_image(screenshot, other_store_source, anchor_bottom=604)


def test_rejects_two_equally_likely_thumbnail_candidates(tmp_path):
    source = _task_source(SAMPLES[0][1])
    screenshot = _blank_chat()
    _insert_source_thumbnail(screenshot, source, 650, 145, 140)
    _insert_source_thumbnail(screenshot, source, 650, 305, 140)
    shot_path = tmp_path / "duplicates.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound) as exc:
        locate_sent_image(shot_path, source, anchor_bottom=100)
    assert exc.value.diagnostics["reason"] == "ambiguous_matches"
