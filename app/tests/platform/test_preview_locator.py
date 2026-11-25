"""预览工具栏二维码识别图标的定位测试。

可合成部分只用仓库内已提交的图标模板在 tmp_path 造图；两处真实 Windows 采集
截图（近重复样本、工作区外验收截图、二维码正文反例）与用户提供的圆底图标
必须显式 opt-in，默认跳过并在 reason 里注明所需数据。
"""
from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from menu_capture.desktop.preview_locator import PreviewIconNotFound, locate_preview_recognize


APP_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = cv2.imread(str(APP_ROOT / "menu_capture/assets/preview-recognize.png"))

REAL_ROOT = Path(os.environ.get("MENU_CAPTURE_REAL_DATA_DIR", APP_ROOT / "data"))
REAL_A = REAL_ROOT / "verification/wechat-9c322e80/preview.png"
REAL_B = REAL_ROOT / "verification/wechat-65f3c68c/preview.png"
REAL_MOVED = REAL_ROOT / "outputs/预览扫码视觉验收-20261105/evidence/image-preview.png"
USER_ICON_ENV = "MENU_CAPTURE_USER_ICON"

needs_real_capture = pytest.mark.skipif(
    os.environ.get("MENU_CAPTURE_REAL_DATA") != "1",
    reason="需要真实 Windows 采集截图（app/data/verification/*/preview.png）；"
           "设置 MENU_CAPTURE_REAL_DATA=1 后运行",
)
needs_user_icon = pytest.mark.skipif(
    not os.environ.get(USER_ICON_ENV),
    reason=f"需要用户提供的圆底二维码图标截图；设置 {USER_ICON_ENV}=<png 路径> 后运行",
)


def _require_real(path: Path) -> None:
    if not path.is_file():
        pytest.skip(f"真实采集截图未提供：{path}（设置 MENU_CAPTURE_REAL_DATA_DIR 指向归档目录）")


def _write(tmp_path: Path, name: str, image: np.ndarray) -> Path:
    path = tmp_path / name
    assert cv2.imwrite(str(path), image)
    return path


def _synthetic_preview(*, size: tuple[int, int] = (825, 760), toolbar_h: int = 41,
                       icon_xy: tuple[int, int] | None = (526, 7)) -> np.ndarray:
    """用仓库内图标模板合成一张最小预览截图，替代真实采集帧。"""
    width, height = size
    image = np.full((height, width, 3), 245, dtype=np.uint8)
    # 工具栏与正文保留足够亮度差，重采样（尤其是 2x CUBIC）后才能被分隔线检测识别。
    image[:toolbar_h] = (210, 210, 210)
    if icon_xy is not None:
        x, y = icon_xy
        image[y:y + TEMPLATE.shape[0], x:x + TEMPLATE.shape[1]] = TEMPLATE
    return image


def _rearranged_toolbar(*, icon_x: int | None, scale: float = 1.0,
                        second_x: int | None = None) -> np.ndarray:
    """保留合成预览主体，重绘工具栏以独立改变图标横向位置和缩放。"""
    result = _synthetic_preview(toolbar_h=64, icon_xy=None)
    icon = cv2.resize(TEMPLATE, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
    ih, iw = icon.shape[:2]
    for x in (icon_x, second_x):
        if x is not None:
            result[8:8 + ih, x:x + iw] = icon
    return result


@needs_real_capture
def test_locates_both_real_preview_captures():
    _require_real(REAL_A)
    _require_real(REAL_B)
    # 两张图仅有少量像素差异，作为近重复采集覆盖，不视为独立样本。
    a, b = cv2.imread(str(REAL_A)), cv2.imread(str(REAL_B))
    assert a.shape == b.shape
    assert np.count_nonzero(np.any(a != b, axis=2)) / (a.shape[0] * a.shape[1]) < 0.01
    for path in (REAL_A, REAL_B):
        found = locate_preview_recognize(path)
        assert found["score"] >= 0.70
        assert abs(found["center_image_px"]["x"] - 539) <= 2
        assert abs(found["center_image_px"]["y"] - 20.5) <= 2
        assert found["image_size"] == {"width": 825, "height": 760}


@needs_real_capture
def test_locates_actual_preview_with_toolbar_shift():
    _require_real(REAL_MOVED)
    found = locate_preview_recognize(REAL_MOVED)
    assert found["image_size"] == {"width": 825, "height": 500}
    assert abs(found["center_image_px"]["x"] - 459) <= 2
    assert abs(found["center_image_px"]["y"] - 20.5) <= 2
    assert found["score"] >= 0.90


def test_locates_repositioned_and_rescaled_icon(tmp_path):
    image = _rearranged_toolbar(icon_x=357, scale=1.2)
    found = locate_preview_recognize(_write(tmp_path, "rearranged.png", image))
    assert found["score"] >= 0.70
    assert abs(found["center_image_px"]["x"] - (357 + 26 * 1.2 / 2)) <= 4
    assert abs(found["center_image_px"]["y"] - (8 + 27 * 1.2 / 2)) <= 4


def test_locates_icon_in_differently_sized_capture(tmp_path):
    image = np.full((900, 1200, 3), 245, dtype=np.uint8)
    image[:54] = (238, 238, 238)
    icon = cv2.resize(TEMPLATE, None, fx=1.75, fy=1.75, interpolation=cv2.INTER_CUBIC)
    image[1:1 + icon.shape[0], 740:740 + icon.shape[1]] = icon
    found = locate_preview_recognize(_write(tmp_path, "large-capture.png", image))
    assert found["image_size"] == {"width": 1200, "height": 900}
    assert found["score"] >= 0.70
    assert abs(found["center_image_px"]["x"] - (740 + icon.shape[1] / 2)) <= 4


def test_locates_two_x_icon_in_short_cropped_hidpi_capture(tmp_path):
    doubled = cv2.resize(_synthetic_preview(toolbar_h=41), None, fx=2, fy=2,
                         interpolation=cv2.INTER_CUBIC)
    # 2x 截图在 300px 高处裁短；工具栏结束在约 82px，图标底边约 68px。
    cropped = doubled[:300]
    found = locate_preview_recognize(_write(tmp_path, "hidpi-short.png", cropped))
    assert 82 <= found["toolbar_region_px"]["height"] <= 90
    assert abs(found["center_image_px"]["x"] - 1078) <= 3
    assert abs(found["center_image_px"]["y"] - 41) <= 3


@pytest.mark.parametrize("width,height,icon_x", [(800, 300, 500), (1200, 300, 800), (800, 900, 500)])
def test_width_and_height_can_vary_independently(tmp_path, width, height, icon_x):
    image = np.full((height, width, 3), 245, dtype=np.uint8)
    toolbar_bottom = 82
    image[:toolbar_bottom] = (238, 238, 238)
    icon = cv2.resize(TEMPLATE, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    image[14:14 + icon.shape[0], icon_x:icon_x + icon.shape[1]] = icon
    found = locate_preview_recognize(_write(tmp_path, f"dimensions-{width}-{height}.png", image))
    assert found["image_size"] == {"width": width, "height": height}
    assert abs(found["center_image_px"]["x"] - (icon_x + 26)) <= 3
    assert abs(found["center_image_px"]["y"] - 41) <= 3


@needs_user_icon
def test_matches_user_icon_with_circular_background(tmp_path):
    supplied = cv2.imread(os.environ[USER_ICON_ENV], cv2.IMREAD_UNCHANGED)
    if supplied is None:
        pytest.skip(f"无法读取用户圆底图标截图：{os.environ[USER_ICON_ENV]}")
    crop = supplied[34:91, 33:129]
    alpha = crop[:, :, 3:4].astype(np.float32) / 255
    circular_icon = (crop[:, :, :3] * alpha + 238 * (1 - alpha)).astype(np.uint8)
    image = np.full((300, 800, 3), 245, dtype=np.uint8)
    image[:82] = (238, 238, 238)
    x, y = 350, 8
    image[y:y + circular_icon.shape[0], x:x + circular_icon.shape[1]] = circular_icon
    found = locate_preview_recognize(_write(tmp_path, "circular-background.png", image))
    glyph_y, glyph_x = np.where(cv2.cvtColor(circular_icon, cv2.COLOR_BGR2GRAY) < 150)
    expected_center = {"x": x + float(glyph_x.mean()), "y": y + float(glyph_y.mean())}
    assert found["score"] >= 0.70
    assert abs(found["center_image_px"]["x"] - expected_center["x"]) <= 8
    assert abs(found["center_image_px"]["y"] - expected_center["y"]) <= 8


def test_no_icon_fails_with_diagnostics(tmp_path):
    image = _rearranged_toolbar(icon_x=None)
    with pytest.raises(PreviewIconNotFound) as exc:
        locate_preview_recognize(_write(tmp_path, "no-icon.png", image))
    assert exc.value.diagnostics["toolbar_region_px"]["height"] > 0
    assert "best_score" in exc.value.diagnostics


def test_duplicate_icons_fail_as_ambiguous(tmp_path):
    image = _rearranged_toolbar(icon_x=230, second_x=520)
    with pytest.raises(PreviewIconNotFound, match="重复") as exc:
        locate_preview_recognize(_write(tmp_path, "duplicates.png", image))
    assert exc.value.diagnostics["runner_up_score"] is not None


@needs_real_capture
def test_qr_content_in_preview_body_is_not_a_candidate(tmp_path):
    _require_real(REAL_A)
    image = cv2.imread(str(REAL_A))
    image[7:34, 526:552] = (238, 238, 238)
    with pytest.raises(PreviewIconNotFound):
        locate_preview_recognize(_write(tmp_path, "qr-body-only.png", image))
