"""Windows 全局热键：半自动人工采集时，微信在前台也能触发 F8 / F9。

只在半自动批次运行期间注册；F8=开始截图，F9=按面板当前原因判不合格。
非 Windows 平台直接跳过，不影响其他流程。
"""
from __future__ import annotations

import sys
import threading

VK_F8 = 0x77
VK_F9 = 0x78
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class ManualHotkeys:
    def __init__(self, on_capture, on_reject):
        self._callbacks = {1: on_capture, 2: on_reject}
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._error = ""

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._run, name="manual-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return not self._error

    def stop(self) -> None:
        if sys.platform != "win32" or self._thread_id is None:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.UnregisterHotKey(None, 1)
            user32.UnregisterHotKey(None, 2)
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass

    def _run(self) -> None:
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            user32 = ctypes.windll.user32
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            registered = (user32.RegisterHotKey(None, 1, MOD_NOREPEAT, VK_F8)
                          and user32.RegisterHotKey(None, 2, MOD_NOREPEAT, VK_F9))
            if not registered:
                self._error = "F8/F9 热键已被其他程序占用"
                return
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message != WM_HOTKEY:
                    continue
                callback = self._callbacks.get(int(message.wParam))
                if callback is None:
                    continue
                try:
                    callback()
                except Exception:
                    # 快捷键只在等待人工时生效；重复按键或非等待时段直接忽略。
                    pass
        finally:
            self._ready.set()
            try:
                import ctypes

                user32 = ctypes.windll.user32
                user32.UnregisterHotKey(None, 1)
                user32.UnregisterHotKey(None, 2)
            except Exception:
                pass
