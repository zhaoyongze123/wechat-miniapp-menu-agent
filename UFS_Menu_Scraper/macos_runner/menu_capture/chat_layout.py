"""从微信聊天截图中定位发出的绿色文本气泡。"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


class ChatImageUnreadable(ValueError):
    """聊天截图无法打开或解码，属于环境问题，应转人工而不是判业务失败。"""


def find_sent_text_bubbles(path: Path) -> list[dict[str, int | str]]:
    """返回聊天区内发出文本气泡的像素 bbox，坐标为左闭右开。

    只依赖气泡的绿色填充区域，不用门店名称、图片模板或固定门店位置。
    未识别到可靠气泡时返回空列表；调用方应按待复核处理，不退回到可能把
    照片 OCR 拼入消息的宽松分组。
    """
    # cv2.imread 在 Windows 上按 ANSI 打开路径，输出目录含中文时会读失败；先读字节再解码。
    try:
        buffer = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    except OSError as exc:
        raise ChatImageUnreadable(f"无法读取聊天截图：{path}") from exc
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise ChatImageUnreadable(f"无法读取聊天截图：{path}")

    height, width = image.shape[:2]
    if width <= 0 or height <= 0:
        return []

    # 左侧会话列表与底部输入框不属于聊天气泡识别区域；比例随窗口尺寸缩放。
    chat_left = round(width * 0.255)
    chat_bottom = round(height * 0.86)
    chat = image[:chat_bottom, chat_left:]
    if chat.size == 0:
        return []

    hsv = cv2.cvtColor(chat, cv2.COLOR_BGR2HSV)
    # 微信浅色主题的绿色偏低饱和，深色主题则偏暗；放宽色相/亮度，仍要求
    # 有足够色度以排除白色背景和灰色照片内容。
    mask = cv2.inRange(
        hsv,
        np.array((22, 25, 32), dtype=np.uint8),
        np.array((100, 255, 255), dtype=np.uint8),
    )
    # 轻微闭运算跨过字形造成的细小断点，不扩张到邻近聊天内容。
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    min_width = max(70, round(width * 0.18))
    min_height = max(16, round(height * 0.016))
    boxes: list[dict[str, int | str]] = []
    for index in range(1, count):
        x, y, box_width, box_height, area = (int(value) for value in stats[index])
        right = x + box_width + chat_left
        bottom = y + box_height
        if box_width < min_width or box_height < min_height:
            continue
        # 聊天文字气泡只占窗口中的一条窄带；照片内的大块绿色区域不能被当成
        # 已发送文本消息，否则会把图片里的印刷门店名混进消息边界。
        if box_height > height * 0.18 or box_width > width * 0.80 or area > width * height * 0.08:
            continue
        if right < width * 0.70 or box_width / max(1, box_height) < 1.3:
            continue
        # 照片中偶然出现的绿色贴纸/卡片通常更窄；发出的文本气泡横向宽且
        # 填充连续。比例阈值与画面尺寸相关，不依赖某家门店的坐标。
        density = area / float(box_width * box_height)
        if density < 0.28:
            continue
        boxes.append({
            "left": x + chat_left,
            "top": y,
            "right": right,
            "bottom": bottom,
            "method": "wechat_green_text_bubble_hsv",
        })

    boxes.sort(key=lambda box: (int(box["top"]), int(box["left"])))
    return boxes
