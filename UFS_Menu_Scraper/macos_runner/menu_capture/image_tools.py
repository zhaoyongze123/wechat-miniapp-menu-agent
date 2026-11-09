"""只处理已保存的图片与剪贴板，不读取桌面、不注入鼠标或键盘事件。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image


def image_ocr(path: Path) -> list[dict]:
    if sys.platform != "darwin":
        raise RuntimeError("本版的本地 OCR 使用 Apple Vision，Windows 适配尚未验收")
    from .macos import MacDesktop, Frame, Window
    with Image.open(path) as image:
        width, height = image.size
    return MacDesktop().ocr(Frame(Path(path), Window(0, 0, "image-only", 0, 0, width, height), width, height))


def qr_boxes(path: Path) -> list[dict]:
    if sys.platform != "darwin":
        raise RuntimeError("本版二维码定位使用 Apple Vision，Windows 适配尚未验收")
    from .macos import MacDesktop, Frame, Window
    with Image.open(path) as image:
        width, height = image.size
    return MacDesktop().detect_qr(Frame(Path(path), Window(0, 0, "image-only", 0, 0, width, height), width, height))


def prepare_clipboard_image(path: Path) -> None:
    """只准备图片。粘贴及发送必须由 UiPath 发出，并在确认收件窗口后执行。"""
    if sys.platform != "darwin":
        raise RuntimeError("图片剪贴板的 Windows 适配尚未验收")
    import Cocoa
    from Foundation import NSData
    with Image.open(path) as image:
        buffer = io.BytesIO()
        image.convert("RGBA").save(buffer, format="PNG")
    data = buffer.getvalue()
    board = Cocoa.NSPasteboard.generalPasteboard()
    board.clearContents()
    if not board.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), Cocoa.NSPasteboardTypePNG):
        raise RuntimeError("无法准备二维码图片剪贴板")


def prepare_clipboard_text(text: str) -> None:
    """把门店名称写入系统剪贴板；实际粘贴和发送仍由 UiPath 控制。"""
    if sys.platform != "darwin":
        raise RuntimeError("文本剪贴板的 Windows 适配尚未验收")
    import Cocoa

    board = Cocoa.NSPasteboard.generalPasteboard()
    board.clearContents()
    if not board.setString_forType_(str(text), Cocoa.NSPasteboardTypeString):
        raise RuntimeError("无法准备门店名称剪贴板")
