"""在微信图片预览截图的顶部工具栏中定位二维码识别图标。"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

from .template_locator import DEFAULT_SCALES as _SCALES, locate_templates


_TEMPLATE_PATH = Path(__file__).parent / "assets" / "preview-recognize.png"
# Windows 微信 4.x 的入口在工具栏“用 AI 处理”左侧，图标与 mac 版不同，必须单独取样。
_WINDOWS_TEMPLATE_PATH = Path(__file__).parent / "assets" / "preview-recognize-windows.png"
_MIN_SCORE = 0.70
_MIN_MARGIN = 0.045


def _template_path() -> Path:
    """按平台选模板；Windows 模板缺失时退回 mac 模板，由置信度阈值决定是否可用。"""
    if sys.platform == "win32" and _WINDOWS_TEMPLATE_PATH.is_file():
        return _WINDOWS_TEMPLATE_PATH
    return _TEMPLATE_PATH


def _read_bgr(path: Path) -> np.ndarray | None:
    """cv2.imread 打不开含中文的输出路径，退回 PIL 解码。"""
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is not None and image.size:
        return image
    try:
        from PIL import Image

        with Image.open(path) as raw:
            return cv2.cvtColor(np.array(raw.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


class PreviewIconNotFound(ValueError):
    """图标缺失或定位结果不唯一；diagnostics 保留候选与阈值证据。"""

    def __init__(self, message: str, diagnostics: dict):
        super().__init__(message)
        self.diagnostics = diagnostics


def _estimate_toolbar_bottom(image: np.ndarray, template_height: int) -> int | None:
    """用整行亮度跃迁找工具栏与预览内容的水平分界，忽略局部图标边缘。"""
    height = image.shape[0]
    scan_end = min(height, max(160, template_height * 3 + 24))
    if scan_end < 16:
        return None
    gray = cv2.cvtColor(image[:scan_end], cv2.COLOR_BGR2GRAY).astype(np.float32)
    # 横向分界覆盖整行；按钮自身的上下边缘只覆盖局部像素，因此用全行变化率加权。
    row_delta = np.abs(np.diff(gray, axis=0))
    jumps = row_delta.mean(axis=1) * (row_delta > 2).mean(axis=1)
    start = min(8, len(jumps) - 1)
    end = min(len(jumps), scan_end - 4)
    if end <= start:
        return None
    local = jumps[start:end]
    peak_offset = int(np.argmax(local))
    peak = float(local[peak_offset])
    baseline = float(np.median(local))
    # 噪声不明显高于工具栏内部变化时，不把内容中的普通横纹当成边界。
    if peak < 3.5 or peak < max(3.5, baseline * 2.5):
        return None
    return start + peak_offset + 1


def locate_preview_recognize(image_path: Path) -> dict:
    """定位图标，返回截图像素坐标；低置信度或重复候选时抛 PreviewIconNotFound。"""
    image_path = Path(image_path)
    image = _read_bgr(image_path)
    if image is None:
        raise PreviewIconNotFound("无法读取预览截图", {"image_path": str(image_path)})
    height, width = image.shape[:2]
    template_path = _template_path()
    template = _read_bgr(template_path)
    if template is None:
        raise RuntimeError(f"图标模板缺失或不可读: {template_path}")

    # 分隔边缘可随截图像素密度变化；只用它限定工具栏 ROI，不推算固定点击坐标。
    toolbar_bottom = _estimate_toolbar_bottom(image, template.shape[0])
    if toolbar_bottom is None:
        # 缺少可信分隔线时保守搜索顶部，并在结果校验中拒绝越过常规工具栏上界的候选。
        roi_height = min(height, max(112, template.shape[0] * 3 + 24))
        candidate_bottom_limit = min(roi_height, max(42, template.shape[0] * 3 + 12))
    else:
        roi_height = min(height, toolbar_bottom + 4)
        candidate_bottom_limit = roi_height
    matched = locate_templates(image, [(_TEMPLATE_PATH.name, template)],
                               roi=(0, 0, width, roi_height), scales=_SCALES)
    distinct = matched["candidates"]
    diagnostics = {
        "image_size": {"width": width, "height": height},
        "toolbar_region_px": {"x": 0, "y": 0, "width": width, "height": roi_height},
        "toolbar_boundary_px": toolbar_bottom,
        "minimum_score": _MIN_SCORE,
        "minimum_runner_up_margin": _MIN_MARGIN,
        "best_score": None,
        "runner_up_score": None,
        "candidates": [dict(item) for item in distinct[:5]],
    }
    if not distinct:
        raise PreviewIconNotFound("顶部工具栏中未找到二维码识别图标", diagnostics)

    best = distinct[0]
    runner_up_score = distinct[1]["score"] if len(distinct) > 1 else None
    diagnostics["best_score"] = best["score"]
    diagnostics["runner_up_score"] = runner_up_score
    if best["score"] < _MIN_SCORE:
        raise PreviewIconNotFound("二维码识别图标匹配置信度不足", diagnostics)
    if runner_up_score is not None and best["score"] - runner_up_score < _MIN_MARGIN:
        raise PreviewIconNotFound("存在相近的重复图标候选，定位结果不唯一", diagnostics)
    if best["y"] + best["height"] > candidate_bottom_limit:
        raise PreviewIconNotFound("候选图标超出工具栏边界", diagnostics)

    x, y = best["x"], best["y"]
    return {
        "box_image_px": {"x": x, "y": y, "width": best["width"], "height": best["height"]},
        "center_image_px": {"x": x + best["width"] / 2, "y": y + best["height"] / 2},
        "score": best["score"],
        "runner_up_score": runner_up_score,
        "scale": best["scale"],
        "image_size": diagnostics["image_size"],
        "toolbar_region_px": diagnostics["toolbar_region_px"],
    }
