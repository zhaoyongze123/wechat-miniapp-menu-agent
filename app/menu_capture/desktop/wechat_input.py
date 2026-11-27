"""微信桌面聊天窗口 DPI 自适应布局识别、输入框定位与发送按钮状态检测。

从 uipath_runner.py 抽离为纯视觉/几何无状态模块，便于独立单测、跨分辨率回归与后续执行器拆分。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

# 微信输入区裁剪比例（96 DPI 基准兜底，动态分界线不可识别时生效）
INPUT_REGION_RATIO = (0.17, 0.80, 0.905, 0.925)


def frame_dpi_scale(frame: dict | None) -> float:
    """提取截图/窗口回执中的 DPI 缩放倍率，缺省或异常值回退 1.0。"""
    if not isinstance(frame, dict):
        return 1.0
    raw = frame.get("dpi_scale")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0.5 <= float(raw) <= 4.0:
        return float(raw)
    return 1.0


def chat_pane_right(image: Image.Image) -> int:
    """返回微信聊天窗格右边界；右侧内嵌小程序面板会把窗口变成左右分栏。

    兼容 100%~200% DPI 下 1~2px 抗锯齿分栏线；识别不到按整窗宽处理。
    """
    width, height = image.size
    if width < 80 or height < 80:
        return width
    gray = image.convert("L")
    pixels = gray.load()
    top, bottom = int(height * 0.12), int(height * 0.88)
    rows = max(1, bottom - top)
    best_x, best_score = width, 0.0
    start_x = max(2, int(width * 0.52))
    end_x = min(width - 2, int(width * 0.80))
    for x in range(start_x, end_x):
        edge = sum(
            1 for y in range(top, bottom)
            if max(abs(pixels[x, y] - pixels[x - 1, y]), abs(pixels[x + 1, y] - pixels[x - 1, y])) >= 10
        )
        score = edge / rows
        if score > best_score:
            best_x, best_score = x, score
    return best_x if best_score >= 0.75 else width


def chat_pane_left(image: Image.Image, pane_right: int) -> int | None:
    """定位聊天列表与聊天正文之间的竖向分栏线（DPI 自适应）。"""
    width, height = image.size
    pane_right = min(width, max(1, pane_right))
    if pane_right < 120 or height < 120:
        return None
    gray = image.convert("L")
    pixels = gray.load()
    top, bottom = int(height * 0.12), int(height * 0.88)
    rows = max(1, bottom - top)
    start_x = max(2, int(pane_right * 0.14))
    end_x = min(pane_right - 2, int(pane_right * 0.46))
    divider_x: int | None = None
    for x in range(start_x, end_x):
        edge = sum(
            1 for y in range(top, bottom)
            if max(abs(pixels[x, y] - pixels[x - 1, y]), abs(pixels[x + 1, y] - pixels[x - 1, y])) >= 8
        )
        if edge / rows >= 0.72:
            divider_x = x
    return divider_x


def input_divider_y(image: Image.Image, chat_left: int, pane_right: int) -> int | None:
    """定位聊天历史区与底部输入区之间的横向分界线（DPI 自适应）。"""
    width, height = image.size
    left = max(1, min(chat_left + 8, pane_right - 20))
    right = min(width - 1, pane_right - 8)
    cols = right - left
    if cols < 60 or height < 120:
        return None
    gray = image.convert("L")
    pixels = gray.load()
    start_y = max(2, int(height * 0.55))
    end_y = min(height - 20, int(height * 0.88))
    best_y: int | None = None
    best_score = 0.0
    step = max(1, cols // 160)
    sampled = len(range(left, right, step))
    for y in range(start_y, end_y):
        edge = sum(
            1 for x in range(left, right, step)
            if max(abs(pixels[x, y] - pixels[x, y - 1]), abs(pixels[x, y + 1] - pixels[x, y - 1])) >= 8
        )
        score = edge / max(1, sampled)
        if score > best_score:
            best_score, best_y = score, y
    return best_y if best_score >= 0.70 else None


def locate_wechat_input_region(image: Image.Image, dpi_scale: float = 1.0) -> dict[str, Any]:
    """动态计算微信消息输入框区域与安全点击中心，避免高 DPI 下点偏或误触发送按钮。"""
    scale = dpi_scale if 0.5 <= dpi_scale <= 4.0 else 1.0
    width, height = image.size
    pane = chat_pane_right(image)
    detected_left = chat_pane_left(image, pane)
    chat_left = detected_left if detected_left is not None else round(pane * INPUT_REGION_RATIO[0])
    divider_y = input_divider_y(image, chat_left, pane)
    if divider_y is not None:
        span_h = max(32, height - divider_y)
        toolbar_h = min(int(span_h * 0.35), max(22, round(38 * scale)))
        send_h = min(int(span_h * 0.36), max(26, round(44 * scale)))
        in_top = min(height - 20, divider_y + toolbar_h)
        in_bottom = max(in_top + 14, height - send_h)
        in_left = min(pane - 60, chat_left + max(8, round(12 * scale)))
        send_reserve = max(round(105 * scale), round((pane - chat_left) * 0.14))
        in_right = max(in_left + 40, pane - send_reserve)
        detected = True
    else:
        in_left = round(pane * INPUT_REGION_RATIO[0])
        in_top = round(height * INPUT_REGION_RATIO[1])
        in_right = round(pane * INPUT_REGION_RATIO[2])
        in_bottom = round(height * INPUT_REGION_RATIO[3])
        detected = False
    cx = (in_left + in_right) / 2.0
    cy = (in_top + in_bottom) / 2.0
    return {
        "pane_right": pane,
        "chat_left": chat_left,
        "divider_y": divider_y,
        "box": (in_left, in_top, in_right, in_bottom),
        "center": (cx, cy),
        "detected": detected,
    }


def send_button_active(image_path: Path, *, dpi_scale: float = 1.0) -> bool:
    """发送按钮可用（微信绿）说明输入框已有内容；按 DPI 自适应搜索区域与像素面积阈值。"""
    scale = dpi_scale if 0.5 <= dpi_scale <= 4.0 else 1.0
    try:
        with Image.open(image_path) as image:
            rgb = image.convert("RGB")
            pane = chat_pane_right(rgb)
            left = max(0, min(int(pane * 0.84), int(pane - round(150 * scale))))
            top = max(0, min(int(rgb.height * 0.87), int(rgb.height - round(75 * scale))))
            region = rgb.crop((left, top, pane, int(rgb.height * 0.995)))
    except (OSError, ValueError):
        return False
    colors = region.getcolors(maxcolors=region.width * region.height) or []
    hits = sum(
        count for count, (red, green, blue) in colors
        if green >= 150 and red <= 110 and blue <= 150 and green - max(red, blue) >= 60
    )
    min_hits = max(120, round(200 * (scale ** 2)))
    return hits >= min_hits
