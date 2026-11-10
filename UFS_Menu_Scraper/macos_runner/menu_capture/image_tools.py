"""只处理已保存的图片与剪贴板，不读取桌面、不注入鼠标或键盘事件。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image


def image_ocr(path: Path) -> list[dict]:
    if sys.platform == "win32":
        from .windows_image import image_ocr as windows_ocr
        return windows_ocr(path)
    if sys.platform == "darwin":
        from .macos import MacDesktop, Frame, Window
        with Image.open(path) as image:
            width, height = image.size
        return MacDesktop().ocr(Frame(Path(path), Window(0, 0, "image-only", 0, 0, width, height), width, height))
    raise RuntimeError("本地 OCR 仅支持 macOS Apple Vision 或 Windows Tesseract")


def qr_boxes(path: Path) -> list[dict]:
    if sys.platform == "win32":
        from .windows_image import qr_detections
        return [detection["box"] for detection in qr_detections(path)]
    if sys.platform == "darwin":
        from .macos import MacDesktop, Frame, Window
        with Image.open(path) as image:
            width, height = image.size
        return MacDesktop().detect_qr(Frame(Path(path), Window(0, 0, "image-only", 0, 0, width, height), width, height))
    raise RuntimeError("本地二维码定位仅支持 macOS Apple Vision 或 Windows OpenCV")


def prepare_clipboard_image(path: Path) -> None:
    """只准备图片。粘贴及发送必须由 UiPath 发出，并在确认收件窗口后执行。"""
    if sys.platform == "win32":
        from .windows_image import prepare_clipboard_image as windows_clipboard_image
        windows_clipboard_image(path)
        return
    if sys.platform != "darwin":
        raise RuntimeError("系统图片剪贴板仅支持 macOS 或 Windows")
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
    if sys.platform == "win32":
        from .windows_image import prepare_clipboard_text as windows_clipboard_text
        windows_clipboard_text(text)
        return
    if sys.platform != "darwin":
        raise RuntimeError("系统文本剪贴板仅支持 macOS 或 Windows")
    import Cocoa

    board = Cocoa.NSPasteboard.generalPasteboard()
    board.clearContents()
    if not board.setString_forType_(str(text), Cocoa.NSPasteboardTypeString):
        raise RuntimeError("无法准备门店名称剪贴板")
