"""UiPath 运行配置。真实选择器由使用者在 Studio 中标注，不从截图猜造。"""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .config import StrictModel, atomic_json
from .window_contract import is_windows_wechat_app


class RobotProfile(StrictModel):
    mode: Literal["attached", "automatic"] = "attached"
    launch_mode: Literal["cli", "manual"] = "manual"
    wechat_selector: str = ""
    miniapp_selector: str = ""
    preview_selector: str = ""
    selected_task_id: str = ""
    selected_task_ids: list[str] = Field(default_factory=list)
    coordinates_verified: bool = False
    wechat_input_x_ratio: float = Field(default=.68, ge=.05, le=.95)
    wechat_input_y_ratio: float = Field(default=.90, ge=.05, le=.95)
    # 最新图片消息缩略图中心距窗口右/下边缘的固定点距（points）。微信会话区布局不从窗口左上角等比缩放，
    # 必须按右下角固定偏移；默认值是 Mac 实机 432×479 窗口的测量结果，Windows 需按实机重新校准。
    latest_image_offset_x: float = Field(default=85.0, ge=20, le=400)
    latest_image_offset_y: float = Field(default=115.0, ge=20, le=400)
    # 微信消息输入框中心距窗口右/下边缘的固定点距（points）。输入框贴底、高度固定，同样不能按比例算。
    wechat_input_offset_x: float = Field(default=138.0, ge=20, le=600)
    wechat_input_offset_y: float = Field(default=48.0, ge=10, le=200)
    paste_shortcut: str = Field(default_factory=lambda: (
        "[d(hk)][d(ctrl)]v[u(ctrl)][u(hk)]" if sys.platform == "win32" else
        "[d(hk)][d(command)]v[u(command)][u(hk)]"
    ))
    send_key: str = "[d(hk)][k(enter)][u(hk)]"
    close_key: str = Field(default_factory=lambda: (
        "[d(hk)][d(alt)][k(f4)][u(alt)][u(hk)]" if sys.platform == "win32" else
        "[d(hk)][d(command)]w[u(command)][u(hk)]"
    ))
    scroll_units: int = Field(default=6, ge=1, le=10)
    max_tasks: int = Field(default=1, ge=1, le=100)

    @field_validator("wechat_selector", "miniapp_selector", "preview_selector")
    @classmethod
    def selector_xml(cls, value):
        value = value.strip()
        if not value:
            return value
        if len(value) > 8000 or "<!" in value:
            raise ValueError("选择器格式无效")
        try:
            root = ET.fromstring("<root>" + value + "</root>")
        except ET.ParseError:
            raise ValueError("请粘贴 Studio 中的选择器 XML，而非界面截图或代码") from None
        if not list(root):
            raise ValueError("请粘贴完整 XML 选择器，不能只填写店名或窗口标题")
        # 这里只保存微信工作流的目标，拒绝将任意应用接入鼠标执行器。
        apps = [v for node in root.iter() for k, v in node.attrib.items() if k.lower() in {"app", "bundleid", "bundle-id", "processname"}]
        if sys.platform == "win32":
            valid_apps = apps and all(is_windows_wechat_app(app) for app in apps)
        else:
            valid_apps = apps and all("wechat" in a.lower() or "微信" in a or "xinwechat" in a.lower() for a in apps)
        if not valid_apps:
            raise ValueError("需要包含微信进程的完整窗口选择器，请在 Studio 中复制完整选择器")
        return value


class ProfileStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return RobotProfile().model_dump()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        legacy_fields = {"preview_recognize_x", "preview_recognize_y", "recognize_selector"}
        needs_migration = any(field in payload for field in legacy_fields)
        if needs_migration:
            payload = {key: value for key, value in payload.items() if key not in legacy_fields}
        result = RobotProfile.model_validate(payload).model_dump()
        if needs_migration:
            atomic_json(self.path, result)
        return result

    def save(self, payload: dict) -> dict:
        result = RobotProfile.model_validate(payload).model_dump()
        atomic_json(self.path, result)
        return result
