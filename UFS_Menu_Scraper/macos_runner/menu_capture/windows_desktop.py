"""Windows 本地桌面驱动：用 Win32 直接完成窗口级动作。

微信 4.x 把小程序/网页版放在独立进程 WeChatAppEx.exe 里，窗口是 Chromium 内嵌浏览器。
UiPath 的 NApplicationCard 会把它识别成 Chromium 浏览器，卡片内的窗口级目标
（置前/截图/点击/输入）全部解析失败，因此这些动作改由本进程用 user32/gdi32 完成，
回包字段与 FileRobotTransport 的约定保持一致。
"""

from __future__ import annotations

import ctypes
import fnmatch
import io
import time
import unicodedata
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_CLOSE = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
SW_RESTORE = 9
SW_SHOW = 5
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
GE_VK = 0x12
KEYEVENTF_KEYUP = 0x0002
CF_UNICODETEXT = 13


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _declare() -> None:
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                                   wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p]
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.CloseClipboard.argtypes = []
    user32.EmptyClipboard.argtypes = []
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]


_declare()

_PROCESS_NAMES: dict[int, str] = {}
_MODIFIERS = {"ctrl": 0x11, "control": 0x11, "cmd": 0x11, "command": 0x11, "alt": 0x12, "shift": 0x10}
_KEYS = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
}
for _index in range(1, 25):
    _KEYS[f"f{_index}"] = 0x6F + _index


def _enable_dpi_awareness() -> None:
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass


def _process_name(pid: int) -> str:
    if pid in _PROCESS_NAMES:
        return _PROCESS_NAMES[pid]
    name = ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                name = Path(buffer.value).name
        finally:
            kernel32.CloseHandle(handle)
    _PROCESS_NAMES[pid] = name
    return name


def _rect(hwnd) -> tuple[int, int, int, int]:
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _windows() -> list[dict]:
    items: list[dict] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app = _process_name(pid.value)
        if not app:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        name = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, name, 512)
        left, top, width, height = _rect(hwnd)
        items.append({"hwnd": hwnd, "app": app, "title": title.value, "cls": name.value,
                      "visible": bool(user32.IsWindowVisible(hwnd)),
                      "rect": (left, top, width, height)})
        return True

    user32.EnumWindows(proto(collect), 0)
    return items


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _matches(window: dict, expected: dict) -> bool:
    app = expected.get("app")
    if app and _normalize(Path(app).name).removesuffix(".exe") != _normalize(window["app"]).removesuffix(".exe"):
        return False
    for key in ("title", "cls"):
        pattern = expected.get(key)
        if pattern and not fnmatch.fnmatchcase(window[key], pattern):
            return False
    return True


class WindowsDesktop:
    """实现 FileRobotTransport 会下发的窗口级动作；失败时回 ok=False 由上层转成命令错误。"""

    actions = frozenset({"screenshot", "scroll_capture", "click", "right_click", "double_click",
                         "scroll", "type_text", "key", "noop"})

    def __init__(self) -> None:
        _enable_dpi_awareness()

    def execute(self, command: dict) -> dict:
        action = command["action"]
        if action == "noop":
            time.sleep(float(command.get("seconds") or 0))
            return {"ok": True, "action": "noop"}
        try:
            window = self._resolve(str(command.get("selector") or ""))
            if action in {"screenshot", "scroll_capture"}:
                return self._capture_command(window, command)
            if action in {"click", "right_click", "double_click"}:
                return self._click(window, command)
            if action == "scroll":
                return self._scroll(window, command)
            if action == "type_text":
                return self._type_text(window, str(command.get("text") or ""))
            if action == "key":
                return self._key(window, str(command.get("key") or ""))
        except RuntimeError as exc:
            return {"ok": False, "action": action, "error": str(exc)[:300]}
        return {"ok": False, "action": action, "error": f"Windows 桌面驱动不支持 {action}"}

    def _resolve(self, selector: str) -> dict:
        try:
            expected = ET.fromstring(selector).attrib
        except ET.ParseError:
            raise RuntimeError("目标窗口 selector 无法解析") from None
        candidates = [item for item in _windows() if _matches(item, expected)]
        sized = [item for item in candidates
                 if item["visible"] and item["rect"][2] > 200 and item["rect"][3] > 200]
        if not sized:
            raise RuntimeError("未找到可操作的目标窗口")
        if len(sized) > 1:
            raise RuntimeError(f"目标窗口不唯一（{len(sized)} 个可见候选）")
        return sized[0]

    def _activate(self, hwnd) -> bool:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, SW_SHOW)
        if user32.GetForegroundWindow() == hwnd:
            return True
        current = kernel32.GetCurrentThreadId()
        foreground = user32.GetForegroundWindow()
        foreign = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = bool(foreign and foreign != current and user32.AttachThreadInput(current, foreign, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current, foreign, False)
        if user32.GetForegroundWindow() != hwnd:
            # 允许前景切换需要一个“用户输入”信号。
            user32.keybd_event(GE_VK, 0, 0, None)
            user32.keybd_event(GE_VK, 0, KEYEVENTF_KEYUP, None)
            user32.SetForegroundWindow(hwnd)
        for _ in range(10):
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.05)
        return False

    def _identity(self, window: dict, foreground: bool) -> dict:
        return {"app": window["app"], "title": window["title"], "class": window["cls"],
                "foreground": foreground, "capture_scope": "window"}

    def _geometry(self, window: dict) -> dict:
        left, top, width, height = window["rect"]
        return {"window_x": left, "window_y": top, "window_width": width, "window_height": height}

    def _capture_command(self, window: dict, command: dict) -> dict:
        hwnd = window["hwnd"]
        foreground = self._activate(hwnd)
        if command["action"] == "scroll_capture":
            self._wheel(window, float(command["x"]), float(command["y"]), command["direction"],
                        int(command["units"]))
            time.sleep(0.25)
        left, top, width, height = _rect(hwnd)
        image = self._print_window(hwnd, width, height)
        path = Path(command["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return {"ok": True, "action": command["action"], "path": str(path.resolve()),
                "width": image.width, "height": image.height,
                **self._geometry({"rect": (left, top, width, height)}),
                "target_window": self._identity(window, foreground)}

    def _print_window(self, hwnd, width: int, height: int):
        if width <= 0 or height <= 0:
            raise RuntimeError(f"目标窗口尺寸无效：{width}x{height}")
        screen = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        previous = gdi32.SelectObject(memory, bitmap)
        try:
            printed = bool(user32.PrintWindow(hwnd, memory, PW_RENDERFULLCONTENT))
            buffer = ctypes.create_string_buffer(width * height * 4)
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            # 负高度表示自上而下扫描，省掉翻转。
            info.bmiHeader.biHeight = -height
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            rows = gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(info), 0)
            if not printed or rows != height or _is_blank(buffer.raw):
                # 被遮挡或 GPU 合成窗口拿不到内容时退回屏幕取图。
                gdi32.BitBlt(memory, 0, 0, width, height, screen,
                             _rect(hwnd)[0], _rect(hwnd)[1], SRCCOPY)
                rows = gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(info), 0)
                if rows != height:
                    raise RuntimeError("窗口截图失败：无法读取位图")
        finally:
            gdi32.SelectObject(memory, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory)
            user32.ReleaseDC(None, screen)
        from PIL import Image

        return Image.frombuffer("RGBA", (width, height), buffer.raw, "raw", "BGRA", 0, 1).convert("RGB")

    def _point(self, window: dict, x: float, y: float) -> tuple[int, int, int, int]:
        left, top, width, height = _rect(window["hwnd"])
        if not (0 <= x < width and 0 <= y < height):
            raise RuntimeError(f"坐标超出窗口范围：{x:.0f},{y:.0f} 不在 {width}x{height} 内")
        return left, top, width, height

    def _click(self, window: dict, command: dict) -> dict:
        hwnd = window["hwnd"]
        x, y = float(command["x"]), float(command["y"])
        left, top, width, height = self._point(window, x, y)
        if not self._activate(hwnd):
            raise RuntimeError("目标窗口未置前；未执行点击")
        user32.SetCursorPos(left + int(round(x)), top + int(round(y)))
        time.sleep(0.05)
        action = command["action"]
        down = MOUSEEVENTF_RIGHTDOWN if action == "right_click" else MOUSEEVENTF_LEFTDOWN
        up = MOUSEEVENTF_RIGHTUP if action == "right_click" else MOUSEEVENTF_LEFTUP
        for _ in range(2 if action == "double_click" else 1):
            user32.mouse_event(down, 0, 0, 0, None)
            time.sleep(0.02)
            user32.mouse_event(up, 0, 0, 0, None)
            if action == "double_click":
                time.sleep(0.06)
        return {"ok": True, "action": action, **self._geometry({"rect": (left, top, width, height)})}

    def _scroll(self, window: dict, command: dict) -> dict:
        x, y = float(command["x"]), float(command["y"])
        left, top, width, height = self._point(window, x, y)
        if not self._activate(window["hwnd"]):
            raise RuntimeError("目标窗口未置前；未执行滚动")
        self._wheel(window, x, y, command["direction"], int(command["units"]))
        return {"ok": True, "action": "scroll", **self._geometry({"rect": (left, top, width, height)})}

    def _wheel(self, window: dict, x: float, y: float, direction: str, units: int) -> None:
        left, top, _, _ = _rect(window["hwnd"])
        user32.SetCursorPos(left + int(round(x)), top + int(round(y)))
        time.sleep(0.05)
        delta = -120 * max(1, min(100, units)) if direction == "Down" else 120 * max(1, min(100, units))
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_ulong(delta & 0xFFFFFFFF).value, None)
        time.sleep(0.05)

    def _type_text(self, window: dict, text: str) -> dict:
        if not self._activate(window["hwnd"]):
            raise RuntimeError("目标窗口未置前；未执行输入")
        previous = _clipboard_text()
        _set_clipboard_text(text)
        _tap(_MODIFIERS["ctrl"], 0x56)
        time.sleep(0.2)
        if previous:
            _set_clipboard_text(previous)
        return {"ok": True, "action": "type_text", "target_window": self._identity(window, True)}

    def _key(self, window: dict, key: str) -> dict:
        if not self._activate(window["hwnd"]):
            raise RuntimeError("目标窗口未置前；未执行按键")
        parts = [part.strip().casefold() for part in key.replace("-", "+").split("+") if part.strip()]
        if not parts:
            raise RuntimeError("按键为空")
        codes = []
        for part in parts:
            code = _MODIFIERS.get(part) or _KEYS.get(part)
            if code is None and len(part) == 1:
                code = ord(part.upper())
            if code is None:
                raise RuntimeError(f"不支持的按键：{part}")
            codes.append(code)
        for code in codes[:-1]:
            user32.keybd_event(code, 0, 0, None)
        user32.keybd_event(codes[-1], 0, 0, None)
        time.sleep(0.03)
        user32.keybd_event(codes[-1], 0, KEYEVENTF_KEYUP, None)
        for code in reversed(codes[:-1]):
            user32.keybd_event(code, 0, KEYEVENTF_KEYUP, None)
        return {"ok": True, "action": "key", "target_window": self._identity(window, True)}


def _is_blank(raw: bytes) -> bool:
    """GPU 合成窗口 PrintWindow 偶发返回全同色位图；抽样判断，避免把黑图当证据。"""
    if not raw:
        return True
    sample = raw[::97 * 4]
    return len(set(sample)) <= 1


def _tap(modifier: int, key: int) -> None:
    user32.keybd_event(modifier, 0, 0, None)
    user32.keybd_event(key, 0, 0, None)
    time.sleep(0.02)
    user32.keybd_event(key, 0, KEYEVENTF_KEYUP, None)
    user32.keybd_event(modifier, 0, KEYEVENTF_KEYUP, None)


def _clipboard_text() -> str:
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> None:
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("无法打开剪贴板")
    try:
        user32.EmptyClipboard()
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        handle = kernel32.GlobalAlloc(0x0002, size)
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise RuntimeError("写入剪贴板失败")
    finally:
        user32.CloseClipboard()
