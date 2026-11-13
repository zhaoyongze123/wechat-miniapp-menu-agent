"""从微信聊天截图中定位发出的绿色文本气泡。"""

from __future__ import annotations

from collections import Counter
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
    # 只跑浅色主题：实测微信绿气泡为 H≈57.5、S≈84、V≈221，而图片卡片里的木纹/底栏
    # 是 H≈23、V≈97。窄范围在掩码阶段就排除后者，不再依赖事后按亮度兜底。
    mask = cv2.inRange(
        hsv,
        np.array((44, 50, 150), dtype=np.uint8),
        np.array((95, 255, 255), dtype=np.uint8),
    )
    # 轻微闭运算跨过字形造成的细小断点，不扩张到邻近聊天内容。
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    # 3~7 字的短店名气泡只有 70~130px 宽，18% 窗口宽会把它们全部漏掉
    # （实测 20 家里 9 家因此误判为“未看到发送文本气泡”）。先用 6.5% 收候选，
    # 再用下面“同一条右边界”的约束剔除照片里偶然出现的绿色块。
    min_width = max(60, round(width * 0.065))
    min_height = max(16, round(height * 0.016))
    boxes: list[dict[str, int | float | str]] = []
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

    if not boxes:
        return []
    # 真实发出的气泡共享同一条右边界，照片/贴纸里的绿色块通常对不齐。
    # 只在存在足够宽的“可信气泡”时才启用对齐约束，避免全是短气泡时误杀。
    confident = [item for item in boxes
                 if int(item["right"]) - int(item["left"]) >= max(100, round(width * 0.10))]
    if confident:
        edge = Counter(int(item["right"]) for item in confident).most_common(1)[0][0]
        boxes = [item for item in boxes if abs(int(item["right"]) - edge) <= 4]
    boxes.sort(key=lambda box: (int(box["top"]), int(box["left"])))
    return boxes
