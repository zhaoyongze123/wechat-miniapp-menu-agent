"""Windows-only adapters for local OCR, QR scanning, and clipboard preparation.

This module deliberately imports Win32 APIs only inside clipboard calls so the
Mac runtime can import shared image tooling without loading Windows libraries.
"""
from __future__ import annotations

import csv
import ctypes
import io
import os
import shutil
import subprocess
from collections import OrderedDict
from pathlib import Path

from PIL import Image


def _tesseract_command() -> str:
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    command = configured or shutil.which("tesseract")
    if not command:
        raise RuntimeError("未找到 Tesseract OCR；请安装 Tesseract 并配置 chi_sim、eng 语言数据，或设置 TESSERACT_CMD")
    return command


def image_ocr(path: Path) -> list[dict]:
    """Run Tesseract in TSV mode and return word-grouped lines in source pixels."""
    image_path = Path(path).expanduser().resolve(strict=True)
    command = _tesseract_command()
    try:
        result = subprocess.run(
            [command, "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "11", "tsv"],
            check=False,
            capture_output=True,
            input=image_path.read_bytes(),
            timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tesseract OCR 超时（45 秒）") from exc
    except OSError as exc:
        raise RuntimeError("无法启动 Tesseract OCR") from exc
    if result.returncode != 0:
        # Avoid surfacing command stderr: it can contain local paths or image text.
        raise RuntimeError(f"Tesseract OCR 执行失败（退出码 {result.returncode}）")

    lines: OrderedDict[tuple[str, ...], list[tuple[int, int, int, int, float, str]]] = OrderedDict()
    stdout = result.stdout.decode("utf-8-sig", errors="replace") if isinstance(result.stdout, bytes) else result.stdout
    reader = csv.DictReader(io.StringIO(stdout), delimiter="\t")
    required = {"level", "page_num", "block_num", "par_num", "line_num", "left", "top", "width", "height", "conf", "text"}
    if not required.issubset(reader.fieldnames or []):
        raise RuntimeError("Tesseract OCR 返回的 TSV 格式无效")
    for row in reader:
        text = (row.get("text") or "").strip()
        if row.get("level") != "5" or not text:
            continue
        try:
            key = tuple(row[name] or "0" for name in ("page_num", "block_num", "par_num", "line_num"))
            left, top = int(row["left"]), int(row["top"])
            width, height = int(row["width"]), int(row["height"])
            confidence = max(0.0, min(1.0, float(row["conf"]) / 100.0))
        except (TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        lines.setdefault(key, []).append((left, top, width, height, confidence, text))

    output = []
    for words in lines.values():
        left = min(word[0] for word in words)
        top = min(word[1] for word in words)
        right = max(word[0] + word[2] for word in words)
        bottom = max(word[1] + word[3] for word in words)
        output.append({
            "text": " ".join(word[5] for word in words),
            "x": float(left),
            "y": float(top),
            "width": float(right - left),
            "height": float(bottom - top),
            "confidence": sum(word[4] for word in words) / len(words),
        })
    return output


def qr_detections(path: Path) -> list[dict]:
    """Find QR boxes with OpenCV; decoded payload remains transient to caller."""
    import cv2
    import numpy as np

    image_path = Path(path).expanduser().resolve(strict=True)
    # cv2.imread 在 Windows 上按 ANSI 打开路径，中文目录会直接读失败；改为先读字节再解码。
    try:
        buffer = np.frombuffer(image_path.read_bytes(), dtype=np.uint8)
    except OSError as exc:
        raise RuntimeError("OpenCV 无法读取图片") from exc
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("OpenCV 无法读取图片")
    detector = cv2.QRCodeDetector()
    detections: list[dict] = []
    try:
        found, decoded, points, _straight = detector.detectAndDecodeMulti(image)
    except (AttributeError, cv2.error):
        found, decoded, points = False, (), None
    if found and points is not None:
        for payload, polygon in zip(decoded or (), points):
            corners = polygon.reshape(-1, 2)
            left = float(min(point[0] for point in corners))
            top = float(min(point[1] for point in corners))
            right = float(max(point[0] for point in corners))
            bottom = float(max(point[1] for point in corners))
            if right > left and bottom > top:
                detections.append({
                    "box": {"x": left, "y": top, "width": right - left, "height": bottom - top},
                    "payload": str(payload) if payload else None,
                })
    if detections:
        return detections
    try:
        payload, points, _straight = detector.detectAndDecode(image)
    except cv2.error:
        return []
    if points is None:
        return []
    corners = points.reshape(-1, 2)
    left = float(min(point[0] for point in corners))
    top = float(min(point[1] for point in corners))
    right = float(max(point[0] for point in corners))
    bottom = float(max(point[1] for point in corners))
    if right <= left or bottom <= top:
        return []
    return [{
        "box": {"x": left, "y": top, "width": right - left, "height": bottom - top},
        "payload": str(payload) if payload else None,
    }]


def _set_clipboard_data(format_id: int, data: bytes) -> None:
    if os.name != "nt":
        raise RuntimeError("Windows 剪贴板只能在 Windows 上使用")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.DestroyWindow.restype = ctypes.c_bool
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    # OpenClipboard(NULL) + EmptyClipboard leaves a NULL owner, which makes
    # SetClipboardData fail. A temporary message-only STATIC HWND supplies a
    # valid owner without creating a visible desktop window.
    message_only_parent = ctypes.c_void_p(-3)  # HWND_MESSAGE
    owner = user32.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 0, 0, message_only_parent, None, None, None)
    if not owner:
        raise RuntimeError("无法创建 Windows 剪贴板所有者窗口")
    if not user32.OpenClipboard(owner):
        user32.DestroyWindow(owner)
        raise RuntimeError(f"无法打开 Windows 剪贴板（错误码 {ctypes.get_last_error()}）")
    handle = None
    transferred = False
    try:
        if not user32.EmptyClipboard():
            raise RuntimeError("无法清空 Windows 剪贴板")
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not handle:
            raise MemoryError("无法分配剪贴板内存")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise MemoryError("无法锁定剪贴板内存")
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(format_id, handle):
            raise RuntimeError("无法设置 Windows 剪贴板数据")
        # Successful SetClipboardData transfers ownership to Windows.
        transferred = True
    finally:
        user32.CloseClipboard()
        user32.DestroyWindow(owner)
        if handle and not transferred:
            kernel32.GlobalFree(handle)


def prepare_clipboard_image(path: Path) -> None:
    """Publish a DIB image to the Windows clipboard; UiPath performs paste/send."""
    with Image.open(path) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="BMP")
    bmp = buffer.getvalue()
    if len(bmp) <= 14 or bmp[:2] != b"BM":
        raise RuntimeError("无法编码 Windows 剪贴板图片")
    _set_clipboard_data(8, bmp[14:])  # CF_DIB excludes the BMP file header.


def prepare_clipboard_text(text: str) -> None:
    """Publish Unicode text to the Windows clipboard; UiPath performs paste/send."""
    _set_clipboard_data(13, (str(text) + "\0").encode("utf-16-le"))  # CF_UNICODETEXT
