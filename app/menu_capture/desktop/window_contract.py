"""UiPath 动作回执中的目标窗口身份契约，与像素内容验证分开。"""

from __future__ import annotations

import fnmatch
import sys
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import PureWindowsPath


def _name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    return "wechat" if value == "微信" else value


def windows_app_name(value: str) -> str:
    return PureWindowsPath(value.strip()).name.casefold().removesuffix(".exe")


def is_windows_wechat_app(value: str) -> bool:
    return windows_app_name(value) in {"wechat", "weixin"}


def is_windows_miniapp_app(value: str) -> bool:
    """Windows 微信小程序由独立进程 WeChatAppEx.exe 承载，窗口是内嵌浏览器。"""
    return windows_app_name(value) == "wechatappex"


def window_selector(window: dict, title: str) -> str:
    attrs = {"app": str(window.get("app") or "WeChat"), "title": title}
    attr = "cls" if sys.platform == "win32" else "role"
    if value := str(window.get(attr) or "").strip():
        attrs[attr] = value
    return ET.tostring(ET.Element("wnd", attrs), encoding="unicode", short_empty_elements=True)


def select_wechat_selector(windows: list[dict]) -> str:
    """从进程、窗口形状和标题识别微信主窗口，排除图片预览和小程序。"""
    candidates = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        title = str(window.get("title") or "").strip()
        app = str(window.get("app") or "")
        app_name = app.casefold()
        allowed_app = (is_windows_wechat_app(app) if sys.platform == "win32"
                       else "wechat" in app_name or "xinwechat" in app_name or "微信" in app)
        if not title or not allowed_app or title in {"图片和视频", "聊天记录"}:
            continue
        position = window.get("position") or {}
        width, height = position.get("width", 0), position.get("height", 0)
        has_dimensions = isinstance(width, (int, float)) and isinstance(height, (int, float))
        if has_dimensions and width > 100 and height > 100 and width < height * 0.8:
            continue
        common_titles = ({"微信", "wechat", "weixin", "文件传输助手"}
                         if sys.platform == "win32" else {"微信", "WeChat", "文件传输助手"})
        preferred = (title.casefold() if sys.platform == "win32" else title) in common_titles
        candidates.append((preferred, width * height if has_dimensions else 0, window, title))
    if not candidates:
        return ""
    _, _, window, title = max(candidates, key=lambda item: item[:2])
    return window_selector(window, title)


def windows_selectors_overlap(target: str, configured: str) -> bool:
    """判断关闭目标是否可能是用户配置的聊天或图片预览窗口。"""
    if not configured:
        return False
    actual = ET.fromstring(target).attrib
    expected = ET.fromstring(f"<root>{configured}</root>").find("wnd")
    if expected is None:
        return True  # 选择器不能拆解时，不把它当成可以排除的窗口。
    expected_app = expected.get("app", "")
    if expected_app and windows_app_name(actual.get("app", "")) != windows_app_name(expected_app):
        return False
    return all(
        not expected.get(key) or not actual.get(key)
        or actual[key].casefold() == expected.get(key, "").casefold()
        or fnmatch.fnmatchcase(actual[key].casefold(), expected.get(key, "").casefold())
        for key in ("title", "cls")
    )


def validate_target_window(command: dict, reply: dict) -> None:
    """校验实测身份，不能从 selector 或 PNG 尺寸推断置前成功。"""
    action = command.get("action")
    if action not in {"key", "type_text", "screenshot", "screenshot_desktop"}:
        return  # 快速滚动截图复用已经建立的窗口会话，不逐页增加属性读取。
    observed = reply.get("target_window")
    if not isinstance(observed, dict):
        raise ValueError("UiPath 未返回实际窗口身份；执行器版本需要更新")
    scope = "desktop" if action == "screenshot_desktop" else "window"
    if observed.get("capture_scope") != scope:
        raise ValueError("UiPath 观察范围与请求不一致")
    if scope == "desktop":
        return  # 桌面图不冒充某一个窗口的像素内容，也不要求该窗口抢占系统弹窗。
    if observed.get("foreground") is not True:
        raise ValueError("UiPath 实测目标窗口未置前；未确认输入或截图目标")
    for key in ("app", "title"):
        if not isinstance(observed.get(key), str) or not observed[key].strip():
            raise ValueError(f"UiPath 未测得目标窗口 {key}")
    try:
        expected = ET.fromstring(command["selector"]).attrib
    except (KeyError, TypeError, ET.ParseError) as exc:
        raise ValueError("目标窗口 selector 无法解析") from exc
    for key in ("app", "title"):
        pattern = expected.get(key)
        normalize = windows_app_name if key == "app" and sys.platform == "win32" else _name
        if pattern and not fnmatch.fnmatchcase(normalize(observed[key]), normalize(pattern)):
            raise ValueError(f"UiPath 实际窗口 {key} 与请求不一致：{observed[key][:120]}")


def production_window_geometry(screen) -> dict:
    """批量采集统一使用的小程序窗口几何（贴屏幕左侧、全高）。

    人工标注也套用同一尺寸，保证样本截图与正式执行时的版面一致。
    """
    return {"width": 240, "height": max(300, int(screen.height) - 100),
            "x": int(screen.x), "y": int(screen.y) + 35}
