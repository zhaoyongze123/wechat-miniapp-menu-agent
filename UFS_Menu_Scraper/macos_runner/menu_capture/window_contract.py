"""UiPath 动作回执中的目标窗口身份契约，与像素内容验证分开。"""

from __future__ import annotations

import fnmatch
import unicodedata
import xml.etree.ElementTree as ET


def _name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    return "wechat" if value == "微信" else value


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
        if pattern and not fnmatch.fnmatchcase(_name(observed[key]), _name(pattern)):
            raise ValueError(f"UiPath 实际窗口 {key} 与请求不一致：{observed[key][:120]}")
