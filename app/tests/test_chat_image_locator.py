from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from menu_capture.vision.chat_image_locator import ChatImageNotFound, locate_sent_image

pytestmark = pytest.mark.unit


DB_PATH = Path(os.environ.get("MENU_CAPTURE_TEST_TASK_DB", ""))
EVIDENCE = Path(os.environ.get("MENU_CAPTURE_TEST_EVIDENCE_DIR", ""))
SAMPLES = (
    ("eb660f76c1ee4c9fabd7267ae96d5b61.png", "b8befb08d9d5de834e71", 604),
    ("d5dc5fceccb34a5499ad9339352bbbcb.png", "fb509e40668b6c659188", 602),
)


def _synthetic_source(path: Path, seed: int = 1) -> Path:
    """Create a deterministic, non-customer image for matcher unit tests."""
    image = Image.new("RGB", (480, 480), (248, 248, 248))
    draw = ImageDraw.Draw(image)
    rng = np.random.default_rng(seed)
    for _ in range(45):
        x, y = (int(n) for n in rng.integers(12, 440, size=2))
        radius = int(rng.integers(8, 38))
        shade = int(rng.integers(0, 180))
        draw.ellipse((x, y, x + radius, y + radius), fill=(shade, shade, shade))
    draw.rectangle((22, 22, 458, 458), outline=(0, 0, 0), width=5)
    image.save(path)
    return path


def _configured_source(task_id: str) -> Path | None:
    if not DB_PATH.is_file():
        return None
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        row = conn.execute("select source_path from tasks where id=?", (task_id,)).fetchone()
    if not row or not Path(row[0]).is_file():
        return None
    return Path(row[0])


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


@pytest.mark.real_data
@pytest.mark.parametrize("screenshot_name,task_id,anchor", SAMPLES)
def test_locates_real_sent_thumbnail(screenshot_name, task_id, anchor):
    screenshot = EVIDENCE / screenshot_name
    source = _configured_source(task_id)
    if not screenshot.is_file() or source is None:
        pytest.skip("set MENU_CAPTURE_TEST_TASK_DB and MENU_CAPTURE_TEST_EVIDENCE_DIR to run local real-data checks")

    found = locate_sent_image(screenshot, source, anchor)

    left, top, right, bottom = found["box_image_px"]
    assert 620 <= left <= 640
    assert 620 <= top <= 640
    assert 770 <= right <= 800
    assert 770 <= bottom <= 800
    assert found["score"] >= 0.70
    assert found["method"] == "multiscale_thumbnail_match"
    assert set(found["center_image_px"]) == {"x", "y"}


def test_matches_thumbnail_at_changed_scale_and_position(tmp_path):
    source = _synthetic_source(tmp_path / "synthetic-source.png")
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
    source = _synthetic_source(tmp_path / "synthetic-source.png")
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
    source = _synthetic_source(tmp_path / "synthetic-source.png")
    screenshot = _blank_chat()
    _insert_source_thumbnail(screenshot, source, 650, 170, 160)
    shot_path = tmp_path / "history-only.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound) as exc:
        locate_sent_image(shot_path, source, anchor_bottom=400)
    assert exc.value.diagnostics["search_region_px"][1] == 400


def test_rejects_when_candidate_thumbnail_disappears(tmp_path):
    source = _synthetic_source(tmp_path / "synthetic-source.png")
    screenshot = _blank_chat()
    _insert_source_thumbnail(screenshot, source, 650, 250, 160)
    screenshot[250:410, 650:810] = (248, 248, 248)
    shot_path = tmp_path / "removed.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound):
        locate_sent_image(shot_path, source, anchor_bottom=190)


@pytest.mark.real_data
def test_rejects_another_stores_source_image(tmp_path):
    screenshot = EVIDENCE / SAMPLES[0][0]
    other_store_source = _configured_source(SAMPLES[1][1])
    if not screenshot.is_file() or other_store_source is None:
        pytest.skip("set MENU_CAPTURE_TEST_TASK_DB and MENU_CAPTURE_TEST_EVIDENCE_DIR to run local real-data checks")

    with pytest.raises(ChatImageNotFound):
        locate_sent_image(screenshot, other_store_source, anchor_bottom=604)


def test_rejects_two_equally_likely_thumbnail_candidates(tmp_path):
    source = _synthetic_source(tmp_path / "synthetic-source.png")
    screenshot = _blank_chat()
    _insert_source_thumbnail(screenshot, source, 650, 145, 140)
    _insert_source_thumbnail(screenshot, source, 650, 305, 140)
    shot_path = tmp_path / "duplicates.png"
    assert cv2.imwrite(str(shot_path), screenshot)

    with pytest.raises(ChatImageNotFound) as exc:
        locate_sent_image(shot_path, source, anchor_bottom=100)
    assert exc.value.diagnostics["reason"] == "ambiguous_matches"
