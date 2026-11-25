"""真实发送截图 + 历史任务库的图片定位回归。

这类用例必须使用 Windows 采集归档（`app/data` 下的任务库与 action-evidence 截图），
无法在干净环境合成；默认跳过，显式设置 `MENU_CAPTURE_REAL_DATA=1` 后运行。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from menu_capture.desktop.chat_image_locator import locate_sent_image


APP_ROOT = Path(__file__).resolve().parents[2]
REAL_ROOT = Path(os.environ.get("MENU_CAPTURE_REAL_DATA_DIR", APP_ROOT / "data"))
DB_PATH = REAL_ROOT / "tasks.sqlite3"
EVIDENCE = REAL_ROOT / "action-evidence"
SAMPLES = (
    ("eb660f76c1ee4c9fabd7267ae96d5b61.png", "b8befb08d9d5de834e71", 604),
    ("d5dc5fceccb34a5499ad9339352bbbcb.png", "fb509e40668b6c659188", 602),
)

pytestmark = pytest.mark.skipif(
    os.environ.get("MENU_CAPTURE_REAL_DATA") != "1",
    reason="需要真实采集数据（app/data/tasks.sqlite3 与 action-evidence 发送截图）；"
           "设置 MENU_CAPTURE_REAL_DATA=1 后运行",
)


def _task_source(task_id: str) -> Path:
    if not DB_PATH.is_file():
        pytest.skip(f"真实任务库未提供：{DB_PATH}（需先在 Windows 采集端归档 data/tasks.sqlite3）")
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        row = conn.execute("select source_path from tasks where id=?", (task_id,)).fetchone()
    if not row:
        pytest.skip(f"真实任务库缺少任务夹具：{task_id}")
    source = Path(row[0])
    if not source.is_file():
        pytest.skip(f"真实任务引用的来源图片不存在：{source}")
    return source


@pytest.mark.parametrize("screenshot_name,task_id,anchor", SAMPLES)
def test_locates_real_sent_thumbnail(screenshot_name, task_id, anchor):
    screenshot = EVIDENCE / screenshot_name
    if not screenshot.is_file():
        pytest.skip(f"真实发送截图未提供：{screenshot}（需 data/action-evidence/{screenshot_name}）")
    source = _task_source(task_id)

    found = locate_sent_image(screenshot, source, anchor)

    left, top, right, bottom = found["box_image_px"]
    assert 620 <= left <= 640
    assert 620 <= top <= 640
    assert 770 <= right <= 800
    assert 770 <= bottom <= 800
    assert found["score"] >= 0.70
    assert found["method"] == "multiscale_thumbnail_match"
    assert set(found["center_image_px"]) == {"x", "y"}
