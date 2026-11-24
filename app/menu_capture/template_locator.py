"""通用多模板视觉定位：固定 UI 控件不需要逐像素一致也能匹配。

模板匹配用 ``TM_CCOEFF_NORMED``（零均值归一化互相关），对整体亮度和对比度的
线性变化不敏感；多尺度缩放覆盖 DPI 与窗口尺寸差异；Canny 边缘通道覆盖抗锯齿、
JPEG 重采样和主题色微调。它不能处理旋转、透视形变和控件改版，这些需要新增模板。

控件模板放在 ``menu_capture/assets/<control>-*.png``，每个控件可放多张样本
（常态/选中/深色）。新增控件只需放图片，不需要改代码。
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

ASSETS_DIR = Path(__file__).parent / "assets"
DEFAULT_SCALES = tuple(float(value) for value in np.geomspace(0.6, 3.0, 20))
DEFAULT_MIN_SCORE = 0.70
DEFAULT_MIN_MARGIN = 0.045
DEFAULT_GRAY_WEIGHT = 0.58


class TemplateNotFound(ValueError):
    """模板缺失、置信度不足或结果不唯一；``diagnostics`` 保留候选与阈值证据。"""

    def __init__(self, message: str, diagnostics: dict):
        super().__init__(message)
        self.diagnostics = diagnostics


def _read_color(path: str | Path) -> np.ndarray | None:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return image if image is not None and image.size else None


def load_templates(paths: list[str | Path]) -> list[tuple[str, np.ndarray]]:
    """读取模板；任一模板不可读时直接失败，避免用残缺模板库悄悄降级。"""
    loaded: list[tuple[str, np.ndarray]] = []
    for path in paths:
        image = _read_color(path)
        if image is None:
            raise RuntimeError(f"控件模板缺失或不可读: {path}")
        loaded.append((Path(path).name, image))
    return loaded


def _peaks(score_map: np.ndarray, width: int, height: int, scale: float,
           template_index: int, limit: int = 8) -> list[dict]:
    work = score_map.copy()
    peaks: list[dict] = []
    for _ in range(limit):
        _, score, _, location = cv2.minMaxLoc(work)
        if not math.isfinite(score):
            break
        x, y = location
        peaks.append({
            "x": x, "y": y, "width": width, "height": height,
            "score": float(score), "scale": scale, "template_index": template_index,
        })
        # 同一尺度下屏蔽命中框附近，避免相邻像素冒充第二个候选。
        left, top = max(0, x - width // 2), max(0, y - height // 2)
        right = min(work.shape[1], x + width // 2 + 1)
        bottom = min(work.shape[0], y + height // 2 + 1)
        work[top:bottom, left:right] = -1.0
    return peaks


def _overlap(left: dict, right: dict) -> float:
    x1, y1 = max(left["x"], right["x"]), max(left["y"], right["y"])
    x2 = min(left["x"] + left["width"], right["x"] + right["width"])
    y2 = min(left["y"] + left["height"], right["y"] + right["height"])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    area_left = left["width"] * left["height"]
    area_right = right["width"] * right["height"]
    return intersection / max(1, min(area_left, area_right))


def distinct_candidates(candidates: list[dict]) -> list[dict]:
    """按中心距离和重叠率聚合同一目标的多次命中，保留得分最高的一次。"""
    kept: list[dict] = []
    for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
        cx = candidate["x"] + candidate["width"] / 2
        cy = candidate["y"] + candidate["height"] / 2
        for prior in kept:
            px = prior["x"] + prior["width"] / 2
            py = prior["y"] + prior["height"] / 2
            radius = 0.45 * max(candidate["width"], candidate["height"],
                                prior["width"], prior["height"])
            if math.hypot(cx - px, cy - py) <= radius or _overlap(candidate, prior) >= 0.35:
                break
        else:
            kept.append(candidate)
    return kept


def locate_templates(
    image: np.ndarray | str | Path,
    templates: list[tuple[str, np.ndarray]] | list[np.ndarray],
    *,
    roi: tuple[int, int, int, int] | None = None,
    scales: tuple[float, ...] = DEFAULT_SCALES,
    gray_weight: float = DEFAULT_GRAY_WEIGHT,
    max_peaks: int = 8,
) -> dict:
    """在 ``roi`` 内多模板多尺度匹配，返回全部保留候选（不抛置信度异常）。

    ``roi`` 为原图像素 ``(x, y, width, height)``；返回坐标同样是原图像素。
    调用方自行决定阈值与唯一性策略，便于复用同一份候选做重试。
    """
    if isinstance(image, (str, Path)):
        loaded = _read_color(image)
        if loaded is None:
            raise TemplateNotFound("无法读取截图", {"reason": "unreadable_image", "path": str(image)})
        image = loaded
    height, width = image.shape[:2]
    if roi is None:
        roi = (0, 0, width, height)
    roi_x, roi_y, roi_width, roi_height = (int(value) for value in roi)
    roi_x, roi_y = max(0, roi_x), max(0, roi_y)
    roi_width = min(roi_width, width - roi_x)
    roi_height = min(roi_height, height - roi_y)
    if roi_width <= 1 or roi_height <= 1:
        raise TemplateNotFound("搜索区域为空", {"reason": "empty_roi", "roi": [roi_x, roi_y, roi_width, roi_height]})

    region = image[roi_y:roi_y + roi_height, roi_x:roi_x + roi_width]
    gray_region = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    edge_region = cv2.Canny(gray_region, 45, 135)

    normalized_templates: list[tuple[str, np.ndarray]] = []
    for index, template in enumerate(templates):
        if isinstance(template, tuple):
            name, template_image = template
        else:
            name, template_image = f"template-{index}", template
        normalized_templates.append((name, template_image))

    candidates: list[dict] = []
    for template_index, (name, template_image) in enumerate(normalized_templates):
        gray_template = cv2.cvtColor(template_image, cv2.COLOR_BGR2GRAY)
        template_height, template_width = gray_template.shape[:2]
        for scale in scales:
            scaled_width = max(1, round(template_width * scale))
            scaled_height = max(1, round(template_height * scale))
            if scaled_width > roi_width or scaled_height > roi_height:
                continue
            interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
            scaled_gray = cv2.resize(gray_template, (scaled_width, scaled_height), interpolation=interpolation)
            # 边缘图必须从缩放后的灰度图重算；直接缩放边缘图会稀释笔画，降低小图标召回。
            scaled_edge = cv2.Canny(scaled_gray, 45, 135)
            gray_score = cv2.matchTemplate(gray_region, scaled_gray, cv2.TM_CCOEFF_NORMED)
            edge_score = cv2.matchTemplate(edge_region, scaled_edge, cv2.TM_CCOEFF_NORMED)
            combined = gray_weight * gray_score + (1.0 - gray_weight) * edge_score
            for peak in _peaks(combined, scaled_width, scaled_height, scale, template_index, max_peaks):
                peak["template"] = name
                peak["x"] += roi_x
                peak["y"] += roi_y
                candidates.append(peak)

    distinct = distinct_candidates(candidates)
    for candidate in distinct:
        candidate["box_image_px"] = {
            "x": candidate["x"], "y": candidate["y"],
            "width": candidate["width"], "height": candidate["height"],
        }
        candidate["center_image_px"] = {
            "x": candidate["x"] + candidate["width"] / 2,
            "y": candidate["y"] + candidate["height"] / 2,
        }
    return {
        "image_size": {"width": width, "height": height},
        "roi": {"x": roi_x, "y": roi_y, "width": roi_width, "height": roi_height},
        "scales": list(scales),
        "gray_weight": gray_weight,
        "peak_count": len(candidates),
        "candidates": distinct,
        "best": distinct[0] if distinct else None,
        "runner_up_score": distinct[1]["score"] if len(distinct) > 1 else None,
    }


def locate_control(
    image: str | Path | np.ndarray,
    control: str,
    *,
    roi: tuple[int, int, int, int] | None = None,
    assets_dir: str | Path = ASSETS_DIR,
    min_score: float = DEFAULT_MIN_SCORE,
    min_margin: float = DEFAULT_MIN_MARGIN,
    scales: tuple[float, ...] = DEFAULT_SCALES,
    **kwargs,
) -> dict:
    """按控件名定位：读取 ``<control>.png`` 或 ``<control>-*.png`` 全部样本。

    返回 ``box_image_px`` / ``center_image_px`` / ``score`` / ``runner_up_score`` /
    ``scale`` / ``template`` / ``method``；低分或不唯一时抛 ``TemplateNotFound``。
    """
    assets = Path(assets_dir)
    paths = sorted(assets.glob(f"{control}-*.png"))
    if not paths and (assets / f"{control}.png").is_file():
        paths = [assets / f"{control}.png"]
    if not paths:
        raise TemplateNotFound(
            f"控件模板缺失: {control}", {"reason": "missing_template", "control": control, "assets_dir": str(assets)}
        )
    templates = load_templates(paths)
    result = locate_templates(image, templates, roi=roi, scales=scales, **kwargs)
    diagnostics = {
        "reason": "no_confident_match",
        "control": control,
        "image_size": result["image_size"],
        "roi": result["roi"],
        "threshold": min_score,
        "minimum_runner_up_margin": min_margin,
        "best_score": result["best"]["score"] if result["best"] else None,
        "runner_up_score": result["runner_up_score"],
        "candidates": result["candidates"][:5],
    }
    if not result["best"] or result["best"]["score"] < min_score:
        raise TemplateNotFound("控件匹配置信度不足", diagnostics)
    best = result["best"]
    runner_up = result["runner_up_score"]
    if runner_up is not None and best["score"] - runner_up < min_margin:
        diagnostics["reason"] = "ambiguous_matches"
        raise TemplateNotFound("存在相近的重复控件候选，定位结果不唯一", diagnostics)
    return {
        "box_image_px": dict(best["box_image_px"]),
        "center_image_px": dict(best["center_image_px"]),
        "score": best["score"],
        "runner_up_score": runner_up,
        "scale": best["scale"],
        "template": best["template"],
        "method": "multiscale_template_match",
        "image_size": result["image_size"],
        "roi": result["roi"],
    }
