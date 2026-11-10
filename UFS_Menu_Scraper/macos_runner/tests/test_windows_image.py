from __future__ import annotations

import subprocess
import ctypes
from types import SimpleNamespace
from pathlib import Path

import pytest

from menu_capture import windows_image
from menu_capture import image_tools


TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t90\t菜单\n"
    "5\t1\t1\t1\t1\t2\t45\t20\t16\t12\t80\tMenu\n"
    "5\t1\t1\t1\t2\t1\t5\t50\t20\t10\t-1\t\n"
    "5\t1\t1\t1\t2\t2\t5\t50\t20\t10\t70\t继续\n"
)


def test_tesseract_tsv_groups_words_into_line_boxes(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "sample.png"
    source.write_bytes(b"image")
    monkeypatch.setattr(windows_image, "_tesseract_command", lambda: "tesseract.exe")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, TSV.encode("utf-8"), b"")

    monkeypatch.setattr(windows_image.subprocess, "run", fake_run)
    rows = windows_image.image_ocr(source)

    assert len(rows) == 2
    assert rows[0] == {"text": "菜单 Menu", "x": 10.0, "y": 20.0, "width": 51.0, "height": 12.0, "confidence": pytest.approx(0.85)}
    assert rows[1] == {"text": "继续", "x": 5.0, "y": 50.0, "width": 20.0, "height": 10.0, "confidence": pytest.approx(0.7)}
    assert calls[0][0][1:] == ["stdin", "stdout", "-l", "chi_sim+eng", "--psm", "11", "tsv"]
    assert calls[0][1]["input"] == b"image"
    assert calls[0][1]["timeout"] == 45


def test_tesseract_missing_has_install_hint(monkeypatch):
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr(windows_image.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="chi_sim、eng"):
        windows_image._tesseract_command()


def test_windows_image_module_import_does_not_load_win32_dlls():
    # The cross-platform test runner is macOS; Windows DLLs must stay lazy.
    assert callable(windows_image.prepare_clipboard_text)


def test_shared_image_tools_dispatch_to_windows_adapter(monkeypatch, tmp_path: Path):
    source = tmp_path / "sample.png"
    monkeypatch.setattr(image_tools.sys, "platform", "win32")
    monkeypatch.setattr(windows_image, "image_ocr", lambda _path: [{"text": "ok"}])
    monkeypatch.setattr(windows_image, "qr_detections", lambda _path: [
        {"box": {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}, "payload": "transient"}
    ])
    called = []
    monkeypatch.setattr(windows_image, "prepare_clipboard_text", lambda text: called.append(("text", text)))
    monkeypatch.setattr(windows_image, "prepare_clipboard_image", lambda path: called.append(("image", path)))

    assert image_tools.image_ocr(source) == [{"text": "ok"}]
    assert image_tools.qr_boxes(source) == [{"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}]
    image_tools.prepare_clipboard_text("店铺")
    image_tools.prepare_clipboard_image(source)
    assert called == [("text", "店铺"), ("image", source)]


@pytest.mark.parametrize("set_succeeds", [True, False])
def test_win32_clipboard_owner_order_and_memory_transfer(monkeypatch, set_succeeds):
    calls = []
    block = ctypes.create_string_buffer(32)

    class Api:
        def __init__(self, name, callback):
            self.name = name
            self.callback = callback

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.callback(*args)

    class Library:
        pass

    user32 = Library()
    kernel32 = Library()
    for name, callback in {
        "CreateWindowExW": lambda *_args: 101,
        "DestroyWindow": lambda *_args: True,
        "OpenClipboard": lambda *_args: True,
        "EmptyClipboard": lambda: True,
        "SetClipboardData": lambda *_args: 201 if set_succeeds else None,
        "CloseClipboard": lambda: True,
    }.items():
        setattr(user32, name, Api(name, callback))
    for name, callback in {
        "GlobalAlloc": lambda *_args: 201,
        "GlobalLock": lambda *_args: ctypes.addressof(block),
        "GlobalUnlock": lambda *_args: True,
        "GlobalFree": lambda *_args: None,
    }.items():
        setattr(kernel32, name, Api(name, callback))

    monkeypatch.setattr(windows_image, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_kwargs: user32 if name == "user32" else kernel32, raising=False)
    if set_succeeds:
        windows_image._set_clipboard_data(13, b"text\0")
    else:
        with pytest.raises(RuntimeError, match="无法设置 Windows 剪贴板数据"):
            windows_image._set_clipboard_data(13, b"text\0")

    names = [name for name, _args in calls]
    assert names == [
        "CreateWindowExW", "OpenClipboard", "EmptyClipboard", "GlobalAlloc",
        "GlobalLock", "GlobalUnlock", "SetClipboardData", "CloseClipboard",
        "DestroyWindow",
    ] + ([] if set_succeeds else ["GlobalFree"])
    assert calls[1][1] == (101,)  # A valid owner HWND is passed, never NULL.
    assert calls[6][1] == (13, 201)
