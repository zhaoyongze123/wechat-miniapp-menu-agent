"""Locate an image sent in the right-hand chat transcript below a store-name anchor."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import cv2
import numpy as np


class ChatImageNotFound(ValueError):
    """Raised when the anchored chat area has no unique confident source-image match."""

    def __init__(self, message: str, diagnostics: dict):
        super().__init__(message)
        self.diagnostics = diagnostics


# 实测：真实二维码缩略图（带圆角/留白）最高分只有 0.4047，黑框测试图 0.8998。
# 原 0.70 硬阈值必然把已发出的图片判成“未匹配”，故按实测下探到 0.35。
_MIN_SCORE = 0.35
_AMBIGUITY_GAP = 0.025
# 判“两个候选不相上下”时用的噪声地板：0.35 的接受线会把大量弱候选算进来，
# 若沿用 _MIN_SCORE 做地板，正确图片（实测 0.4047）会被噪声候选连累成“多个难以区分”。
_AMBIGUITY_FLOOR = 0.45
_MAX_TEMPLATE_EDGE = 440
_MIN_TEMPLATE_EDGE = 72
# 微信主窗口被规范到 999×749 后，聊天输入框上沿实测在 597px（79.7% 屏高）。
# 搜索区一旦越过这条线，就会把输入框里的文字/图标当成聊天消息参与匹配。
_TRANSCRIPT_BOTTOM = 0.80
_COARSE_EDGES = (80, 112, 144, 176, 224, 288, 352, 416)


def _read_image(path: Path, role: str) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        # cv2.imread 在 Windows 上打不开含中文的路径，退回 PIL 解码。
        try:
            from PIL import Image

            with Image.open(path) as raw:
                image = cv2.cvtColor(np.array(raw.convert("RGB")), cv2.COLOR_RGB2BGR)
        except Exception:
            image = None
    if image is None:
        raise ChatImageNotFound(
            f"无法读取{'聊天截图' if role == 'screenshot' else '来源图片'}：{path}",
            {"reason": "unreadable_image", "role": role, "path": str(path)},
        )
    return image


def _source_templates(source: np.ndarray) -> list[np.ndarray]:
    """Make near-square source crops to cover the thumbnail crop used by chat clients."""
    height, width = source.shape[:2]
    # Avoid needlessly processing huge camera originals while retaining detail well above
    # the eventual ~160 px chat thumbnail.
    if max(height, width) > 1400:
        scale = 1400 / max(height, width)
        source = cv2.resize(source, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        height, width = source.shape[:2]

    crops: list[np.ndarray] = []
    # 保留整图原始比例，覆盖聊天消息按原比例展示的长图和宽图。
    crops.append(cv2.cvtColor(source, cv2.COLOR_BGR2GRAY))
    for aspect in (0.92, 1.00, 1.08):
        crop_w, crop_h = width, height
        if width / height > aspect:
            crop_w = max(1, round(height * aspect))
        else:
            crop_h = max(1, round(width / aspect))
        max_x, max_y = width - crop_w, height - crop_h
        offsets = ((0.5, 0.5), (0.35, 0.65)) if aspect == 1.00 else ((0.5, 0.5),)
        for fx, fy in offsets:
            x, y = round(max_x * fx), round(max_y * fy)
            crop = source[y : y + crop_h, x : x + crop_w]
            if crop.shape[0] >= 48 and crop.shape[1] >= 48:
                crops.append(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))

    # Near-identical aspect crops are useful, but remove exact duplicates for panoramic
    # sources where some aspect ratios collapse to the same crop.
    unique: list[np.ndarray] = []
    keys: set[tuple[int, int, int]] = set()
    for crop in crops:
        key = (crop.shape[1], crop.shape[0], hash(crop.tobytes()))
        if key not in keys:
            keys.add(key)
            unique.append(crop)
    return unique


def _candidate_peaks(response: np.ndarray, template: np.ndarray, limit: int = 5):
    """Extract separated local peaks so different scales at one location do not look ambiguous."""
    work = response.copy()
    h, w = template.shape[:2]
    peaks = []
    for _ in range(limit):
        _, score, _, point = cv2.minMaxLoc(work)
        if score < 0:
            break
        peaks.append((float(score), point[0], point[1], w, h))
        # 屏蔽当前缩略图范围，避免把同一张图的相邻位置当成另一个候选。
        x1, y1 = max(0, point[0] - w // 2), max(0, point[1] - h // 2)
        x2, y2 = min(work.shape[1], point[0] + w), min(work.shape[0], point[1] + h)
        work[y1:y2, x1:x2] = -1
    return peaks


def _add_candidate(candidates: list[dict], candidate: dict) -> None:
    """Cluster nearby scale/crop hits from one thumbnail, retaining its strongest hit."""
    left, top, right, bottom = candidate["box"]
    cx, cy = (left + right) / 2, (top + bottom) / 2
    w, h = right - left, bottom - top
    match = next((item for item in candidates
                  if abs(item["cx"] - cx) < max(w, item["w"]) * 0.50
                  and abs(item["cy"] - cy) < max(h, item["h"]) * 0.50), None)
    if match is None:
        candidates.append({**candidate, "cx": cx, "cy": cy, "w": w, "h": h})
    elif candidate["score"] > match["score"]:
        match.update({**candidate, "cx": cx, "cy": cy, "w": w, "h": h})


def _candidate_from_match(score: float, x: int, y: int, w: int, h: int,
                          region_x: int, region_y: int, crop: np.ndarray) -> dict:
    return {
        "score": float(score),
        "box": (region_x + x, region_y + y, region_x + x + w, region_y + y + h),
        "crop": crop,
        "method": "multiscale_thumbnail_match",
        "source": "original_image_crop",
    }


def locate_sent_image(screenshot_path: Path, source_path: Path, anchor_bottom: float,
                      *, before_send: Path | None = None) -> dict:
    """Find ``source_path``'s sent thumbnail below ``anchor_bottom`` in the chat transcript.

    Coordinates in the return value are screenshot pixels. ``anchor_bottom`` is the
    screenshot y-coordinate of the store-name label's bottom edge.
    """
    screenshot = _read_image(Path(screenshot_path), "screenshot")
    source = _read_image(Path(source_path), "source")
    screen_h, screen_w = screenshot.shape[:2]

    if not isinstance(anchor_bottom, (int, float)) or not np.isfinite(anchor_bottom):
        raise ChatImageNotFound(
            "门店名称锚点坐标无效",
            {"reason": "invalid_anchor", "anchor_bottom": repr(anchor_bottom)},
        )

    # 右边界避开发送者头像栏，底边避开固定输入框；按截图尺寸缩放以兼容不同分辨率。
    x1, x2 = round(screen_w * 0.58), round(screen_w * 0.92)
    y1 = max(0, round(anchor_bottom))
    y2 = min(round(screen_h * _TRANSCRIPT_BOTTOM), round(anchor_bottom + screen_h * 0.42))
    diagnostics = {
        "reason": "no_confident_match",
        "source_path": str(source_path),
        "anchor_bottom": float(anchor_bottom),
        "search_region_px": [x1, y1, x2, y2],
        "threshold": _MIN_SCORE,
    }
    if y2 <= y1 or x2 <= x1:
        diagnostics["reason"] = "empty_search_region"
        raise ChatImageNotFound("门店名称下方没有可搜索的聊天图片区域", diagnostics)

    region = cv2.cvtColor(screenshot[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    # Light blur suppresses screenshot/JPEG resampling noise without erasing QR and
    # printed-card structure, which makes the source image distinctive.
    region = cv2.GaussianBlur(region, (3, 3), 0)
    templates = _source_templates(source)
    coarse_candidates: list[dict] = []
    for crop in templates:
        crop_h, crop_w = crop.shape[:2]
        aspect = crop_w / crop_h
        max_w = min(_MAX_TEMPLATE_EDGE, region.shape[1], crop_w, round(_MAX_TEMPLATE_EDGE * aspect))
        min_w = max(_MIN_TEMPLATE_EDGE, round(_MIN_TEMPLATE_EDGE * aspect))
        for target_w in _COARSE_EDGES:
            if not min_w <= target_w <= max_w:
                continue
            target_h = round(target_w * crop_h / crop_w)
            if target_h < _MIN_TEMPLATE_EDGE or target_h > region.shape[0]:
                continue
            template = cv2.resize(crop, (target_w, target_h), interpolation=cv2.INTER_AREA if target_w < crop_w else cv2.INTER_CUBIC)
            template = cv2.GaussianBlur(template, (3, 3), 0)
            response = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
            for score, px, py, tw, th in _candidate_peaks(response, template, limit=3):
                _add_candidate(coarse_candidates,
                               _candidate_from_match(score, px, py, tw, th, x1, y1, crop))

    # 粗搜索只负责圈出可能缩略图；对最强的几个空间候选在局部位置和相邻尺度精搜。
    # 保留多个分离候选进入精搜，重复发送同图仍会形成并列结果而被拒绝。
    coarse_candidates.sort(key=lambda item: item["score"], reverse=True)
    refined_candidates: list[dict] = []
    for candidate in [item for item in coarse_candidates if item["score"] >= 0.45][:6]:
        crop = candidate["crop"]
        crop_h, crop_w = crop.shape[:2]
        coarse_w, coarse_h = candidate["w"], candidate["h"]
        cx, cy = candidate["cx"] - x1, candidate["cy"] - y1
        for target_w in range(max(_MIN_TEMPLATE_EDGE, int(coarse_w) - 16),
                               min(_MAX_TEMPLATE_EDGE, region.shape[1], int(coarse_w) + 16) + 1, 8):
            target_h = round(target_w * crop_h / crop_w)
            if target_h < _MIN_TEMPLATE_EDGE or target_h > min(region.shape[0], _MAX_TEMPLATE_EDGE):
                continue
            template = cv2.resize(crop, (target_w, target_h), interpolation=cv2.INTER_AREA if target_w < crop_w else cv2.INTER_CUBIC)
            template = cv2.GaussianBlur(template, (3, 3), 0)
            # 以粗候选中心建立局部 ROI，限制精搜成本并允许轻微位置偏差。
            margin = 20
            rx1 = max(0, round(cx - target_w / 2 - margin))
            ry1 = max(0, round(cy - target_h / 2 - margin))
            rx2 = min(region.shape[1], round(cx + target_w / 2 + margin))
            ry2 = min(region.shape[0], round(cy + target_h / 2 + margin))
            local = region[ry1:ry2, rx1:rx2]
            if local.shape[0] < target_h or local.shape[1] < target_w:
                continue
            response = cv2.matchTemplate(local, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, point = cv2.minMaxLoc(response)
            refined_candidates.append(_candidate_from_match(
                score, rx1 + point[0], ry1 + point[1], target_w, target_h, x1, y1, crop))

    best_by_location: list[dict] = []
    for candidate in coarse_candidates + refined_candidates:
        _add_candidate(best_by_location, candidate)

    best_by_location.sort(key=lambda item: item["score"], reverse=True)
    best = best_by_location[0] if best_by_location else None
    runner_up = best_by_location[1]["score"] if len(best_by_location) > 1 else 0.0
    diagnostics.update({
        "best_score": best["score"] if best else 0.0,
        "runner_up_score": runner_up,
        "candidate_count": len(best_by_location),
    })
    # 缩略图带圆角/留白时，正确图片的分数会略低于硬阈值；只要明显领先第二名，
    # 仍可确认是本轮图片，避免把“已发送但缩略图有留白”误判成没发出去。
    relaxed = best is not None and best["score"] >= 0.30 and best["score"] - runner_up >= 0.10
    if before_send is not None and best is not None:
        previous = _read_image(before_send, "screenshot")
        left, top, right, bottom = best["box"]
        changed = False
        if previous.shape == screenshot.shape:
            before = previous[top:bottom, left:right]
            after = screenshot[top:bottom, left:right]
            changed = float(np.mean(np.max(cv2.absdiff(before, after), axis=2) >= 20)) >= 0.12
        diagnostics["candidate_changed_after_send"] = changed
        if not changed:
            raise ChatImageNotFound("发送后未确认本轮新增图片气泡", diagnostics)
        # 只有发送前后确有新内容、且候选唯一领先时才接纳临界分数。扫码前还要核对完整预览图。
        relaxed = best["score"] >= 0.28 and best["score"] - runner_up >= 0.10
    if best is None or (best["score"] < _MIN_SCORE and not relaxed):
        raise ChatImageNotFound("本轮门店名称下方未匹配到来源图片，请查看图片匹配记录", diagnostics)
    if relaxed and best["score"] < _MIN_SCORE:
        diagnostics["relaxed_match"] = True
    if runner_up >= _AMBIGUITY_FLOOR and best["score"] - runner_up < _AMBIGUITY_GAP:
        diagnostics["reason"] = "ambiguous_matches"
        raise ChatImageNotFound("本轮门店名称下方存在多个难以区分的图片匹配，请查看匹配记录", diagnostics)

    left, top, right, bottom = best["box"]
    # 微信聊天区把本轮原图显示为近方形缩略图；模板可能只匹配到其内部裁剪区域，
    # 因此将近方形命中框按中心扩回方形缩略图边界。明显长宽图仍保留原比例命中框。
    box_w, box_h = right - left, bottom - top
    if 0.85 <= box_w / box_h <= 1.15:
        side = max(box_w, box_h)
        center_x, center_y = (left + right) // 2, (top + bottom) // 2
        left, top = center_x - side // 2, center_y - side // 2
        right, bottom = left + side, top + side
    return {
        "box_image_px": [left, top, right, bottom],
        "center_image_px": {"x": (left + right) // 2, "y": (top + bottom) // 2},
        "method": best["method"],
        "source": best["source"],
        "score": best["score"],
        "runner_up_score": runner_up,
        "search_region_px": [x1, y1, x2, y2],
    }


def locate_chat_image_bubble(screenshot_path: Path, anchor_bottom: float) -> dict:
    """找出门店名称气泡下方最新的一块图片气泡，不比对原图内容。

    图片发到聊天区后立刻就能双击打开，此时缩略图可能仍在解码（灰块/模糊帧），
    原图模板匹配要等渲染完成才有分数，会让整批卡在“未匹配到来源图片”。
    这里只按聊天背景上的连续非背景块定位：最新消息永远在最下方，取底边最靠下的
    候选即可；图片身份由打开后的预览核对负责，不在这一步做内容比对。
    """
    screenshot = _read_image(Path(screenshot_path), "screenshot")
    height, width = screenshot.shape[:2]
    if not isinstance(anchor_bottom, (int, float)) or not np.isfinite(anchor_bottom):
        raise ChatImageNotFound(
            "门店名称锚点坐标无效",
            {"reason": "invalid_anchor", "anchor_bottom": repr(anchor_bottom)},
        )
    # 右边界避开发送者头像栏；下边界取聊天区底部（输入框上沿之上），不把输入框算进来。
    x1, x2 = round(width * 0.58), round(width * 0.92)
    y1 = max(0, round(anchor_bottom))
    y2 = min(round(height * _TRANSCRIPT_BOTTOM), round(anchor_bottom + height * 0.42))
    diagnostics = {
        "reason": "no_bubble_appeared",
        "anchor_bottom": float(anchor_bottom),
        "search_region_px": [x1, y1, x2, y2],
    }
    if y2 <= y1 or x2 <= x1:
        diagnostics["reason"] = "empty_search_region"
        raise ChatImageNotFound("门店名称下方没有可搜索的聊天图片区域", diagnostics)

    region = screenshot[y1:y2, x1:x2]
    # 背景色取整个聊天窗格的众数，而不是搜索区域自身的众数：图片气泡可能占满搜索区域，
    # 用区域众数会把气泡颜色当背景，反而把四周背景判成“新气泡”。聊天窗格中背景始终占多数。
    pane_x1, pane_x2 = round(width * 0.26), round(width * 0.95)
    pane_y1, pane_y2 = round(height * 0.10), round(height * 0.78)
    pane = screenshot[pane_y1:pane_y2, pane_x1:pane_x2]
    sample = pane.reshape(-1, 3)[::11].tolist()
    background = np.array(Counter(map(tuple, sample)).most_common(1)[0][0], dtype=np.int16)
    delta = np.abs(region.astype(np.int16) - background).max(axis=2)
    mask = (delta >= 12).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    region_area = region.shape[0] * region.shape[1]
    min_height = max(40, round(height * 0.05))
    min_width = max(60, round(width * 0.06))
    candidates: list[dict] = []
    for index in range(1, count):
        x, y, box_width, box_height, area = (int(value) for value in stats[index])
        if area < region_area * 0.02 or box_height < min_height or box_width < min_width:
            continue
        candidates.append({
            "box": [x1 + x, y1 + y, x1 + x + box_width, y1 + y + box_height],
            "bottom": y1 + y + box_height,
            "area": area,
        })
    diagnostics.update(bubble_candidates=len(candidates), background_rgb=background.tolist())
    if not candidates:
        raise ChatImageNotFound("发送后门店名称下方未出现图片气泡", diagnostics)

    # 新消息总在最下方：底边最靠下的一块就是本轮图片，面积只用于同底边时打破平局。
    best = max(candidates, key=lambda item: (item["bottom"], item["area"]))
    left, top, right, bottom = best["box"]
    return {
        "box_image_px": [left, top, right, bottom],
        "center_image_px": {"x": (left + right) // 2, "y": (top + bottom) // 2},
        "method": "chat_bubble_background_diff",
        "source": "bubble_appearance",
        "bubble_candidates": len(candidates),
    }


def match_preview_source(preview_path: Path, source_path: Path) -> dict:
    """按原图比例核对完整预览，允许留白和缩放，拒绝白帧及上一张图。"""
    preview = _read_image(preview_path, "screenshot")
    source = _read_image(source_path, "source")
    height, width = preview.shape[:2]
    shrink = min(1.0, 480 / max(width, height))
    canvas = cv2.resize(preview, None, fx=shrink, fy=shrink, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
    h, w = source.shape[:2]
    # 预览按比例缩放。略过原图外边缘，兼容窗口边框和预览底部的暗边。
    inset_x, inset_y = round(w * 0.06), round(h * 0.06)
    source = source[inset_y:h - inset_y, inset_x:w - inset_x]
    available_h, available_w = gray.shape
    fit = min(available_w / w, max(1, available_h - round(32 * shrink)) / h)
    scales = {fit * ratio for ratio in (0.6, 0.7, 0.8, 0.88, 0.93, 0.97, 1.0, 1.04)}
    scales.add(shrink)
    best = {"score": 0.0, "color_error": None, "matched": False}
    best_scale = fit

    def compare(scale: float) -> None:
        nonlocal best, best_scale
        tw, th = round(source.shape[1] * scale), round(source.shape[0] * scale)
        if not (32 <= tw <= available_w and 32 <= th <= available_h):
            return
        template = cv2.resize(source, (tw, th), interpolation=cv2.INTER_AREA)
        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
        if float(template_gray.std()) < 8:
            return
        response = cv2.matchTemplate(gray, template_gray, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(response)
        if score <= best["score"]:
            return
        patch = canvas[y:y + th, x:x + tw]
        color_error = float(np.mean(cv2.absdiff(patch, template)))
        best_scale = scale
        best = {
            "score": round(score, 4), "color_error": round(color_error, 2),
            "matched": score >= 0.89 and color_error <= 21,
            "center_image_px": {"x": (x + tw / 2) / shrink, "y": (y + th / 2) / shrink},
        }

    for scale in sorted(scales):
        compare(scale)
    if not best["matched"] and best["score"] >= 0.60:
        # 窗口边框与工具栏高度会改变实际缩放，尤其是贴满窗口的长图。只在接近时精搜尺度。
        coarse_scale = best_scale
        for step in (-1, 1, -2, 2, -3, 3, -4, 4, -5, 5):
            compare(coarse_scale * (1 + step * .005))
    return best
