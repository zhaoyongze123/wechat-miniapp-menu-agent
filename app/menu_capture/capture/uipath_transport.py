"""UiPath 机器人命令传输：Windows 本机动作直连与 macOS 文件 IPC。

职责边界只有命令往返与回执校验：写入 command.json、等待 reply.json、校验窗口与
截图像素、登记实测几何。超时后绝不重发动作；业务判定与采集流程不在此模块。
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from menu_capture.desktop.window_contract import validate_target_window
from menu_capture.capture.runner_paths import _atomic_write_json, _inside
from menu_capture.capture.uipath_errors import (
    RobotCancelled,
    RobotCommandError,
    RobotProtocolError,
    RobotTimeout,
)
_ACTIONS = {"screenshot", "screenshot_desktop", "capture_rect", "click", "click_desktop_overlay", "right_click", "double_click", "scroll", "scroll_capture", "type_text", "key", "resize", "discover", "noop", "stop", "restore_window", "close_window"}
_COMMAND_FIELDS = {
    "id", "action", "selector", "x", "y", "text", "path", "direction", "units", "key", "seconds",
    "settle_seconds",
    "expected_window_width", "expected_window_height", "expected_window_id", "width", "height",
}


class FileRobotTransport:
    """统一校验 Windows 本机动作和 macOS 文件 IPC。超时后绝不重发动作。"""

    def __init__(self, run_directory: Path, output_directory: Path | None = None, poll_interval: float = 0.05,
                 local: Any | None = None):
        # Windows 的发现、截图和动作都在本进程执行，与 macOS IPC 使用同一回执契约。
        self.local = local
        self.run_directory = Path(run_directory).expanduser().resolve()
        self.output_directory = Path(output_directory or run_directory).expanduser().resolve()
        # 一轮采集有两处合法截图落点：暂存目录（菜单页，待发布）与本次运行目录下的
        # gui-agent 过程证据。自 f3d3689 起运行期目录移出产物目录，两者都不在
        # output_directory 内，校验必须同时放行 gui-agent 子树。
        self.output_directories = (self.output_directory, (self.run_directory / "gui-agent").resolve())
        self.run_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.run_directory.chmod(0o700)
        self.output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.command_path = self.run_directory / "command.json"
        self.reply_path = self.run_directory / "reply.json"
        self.poll_interval = max(0.01, float(poll_interval))
        self._lock = threading.Lock()
        self._window_bounds: tuple[float, float] | None = None
        self._window_selector: str | None = None
        self._window_origin: tuple[float, float] | None = None
        self._window_pixels: tuple[int, int] | None = None
        # 整屏截图确认的桌面坐标系；右键菜单等系统弹窗会超出预览窗口，只能按桌面坐标点。
        self._desktop_bounds: tuple[int, int] | None = None
        self._desktop_origin: tuple[float, float] | None = None
        # 按 selector 保留最近一次截图确认的几何。批量采集时窗口位置与尺寸固定，
        # 输入框、消息区等点位可以直接复用，不必每店再截一张全量图。
        self._geometry: dict[str, dict[str, Any]] = {}
        self.retain_command_history = True

    def run(
        self,
        command: dict,
        timeout: float = 90,
        *,
        cancel_event: threading.Event | None = None,
        process: Any | None = None,
    ) -> dict:
        if not self._lock.acquire(blocking=False):
            raise RobotProtocolError("UiPath IPC 正在处理上一条命令")
        command_id = str(uuid.uuid4())
        payload: dict[str, Any] = dict(command)
        if "id" in payload:
            try:
                command_id = str(uuid.UUID(str(payload["id"])))
            except (ValueError, TypeError, AttributeError):
                self._lock.release()
                raise RobotProtocolError("命令 id 必须是 UUID") from None
        payload["id"] = command_id
        try:
            if payload.get("action") == "click_desktop_overlay" and payload.get("x") is not None:
                if not self._desktop_bounds:
                    raise RobotProtocolError("桌面坐标动作必须先做一次整屏截图")
                payload["expected_window_width"], payload["expected_window_height"] = self._desktop_bounds
            elif payload.get("action") in {"click", "click_desktop_overlay", "right_click", "double_click", "scroll", "scroll_capture"} and payload.get("x") is not None:
                selector = payload.get("selector")
                geometry = self._target_geometry(selector)
                if not selector or not geometry or not geometry.get("bounds"):
                    raise RobotProtocolError("坐标动作必须使用最近一次同 selector 截图确认的窗口")
                payload["expected_window_width"], payload["expected_window_height"] = geometry["bounds"]
                if self.local is not None and geometry.get("window_id"):
                    payload["expected_window_id"] = geometry["window_id"]
            self._validate_command(payload)
            if timeout <= 0 or not math.isfinite(float(timeout)):
                raise ValueError("timeout 必须是正数")
            if cancel_event and cancel_event.is_set():
                raise RobotCancelled("命令发送前已收到暂停")
            if self.local is not None:
                if payload["action"] not in self.local.actions:
                    raise RobotProtocolError(f"Windows 桌面驱动不支持 {payload['action']}")
                if self.retain_command_history:
                    safe_payload = {key: value for key, value in payload.items() if key not in {"text", "key"}}
                    _atomic_write_json(self.run_directory / "last-command.json", safe_payload)
                    audit_dir = self.run_directory / "commands"
                    audit_dir.mkdir(mode=0o700, exist_ok=True)
                    _atomic_write_json(audit_dir / (command_id + ".json"), safe_payload)
                reply = self.local.execute(payload)
                reply["id"] = command_id
                if not reply.get("ok"):
                    raise RobotCommandError(f"桌面命令失败：{str(reply.get('error') or '未提供原因')[:300]}")
                self._validate_reply(payload, reply)
                self._apply_reply(payload, reply)
                return reply
            if self.reply_path.exists():
                self.reply_path.unlink()
            # 实时 IPC 仍必须使用 command.json/reply.json；冗余历史副本由调试日志替代。
            if self.retain_command_history:
                safe_payload = {key: value for key, value in payload.items() if key not in {"text", "key"}}
                _atomic_write_json(self.run_directory / "last-command.json", safe_payload)
                audit_dir = self.run_directory / "commands"
                audit_dir.mkdir(mode=0o700, exist_ok=True)
                _atomic_write_json(audit_dir / (command_id + ".json"), safe_payload)
            _atomic_write_json(self.command_path, payload)
            deadline = time.monotonic() + float(timeout)
            while True:
                if cancel_event and cancel_event.is_set():
                    self._remove_pending(command_id)
                    raise RobotCancelled("等待 UiPath 回复时收到暂停")
                if process is not None:
                    exit_code = process.poll()
                    if exit_code is not None:
                        self._remove_pending(command_id)
                        raise RobotCommandError(f"UiPath CLI 在命令回复前退出（exit code {exit_code}）")
                if self.reply_path.exists():
                    reply = self._read_reply(command_id)
                    if self.retain_command_history:
                        _atomic_write_json(self.run_directory / "last-reply.json", reply)
                    if not reply["ok"]:
                        raise RobotCommandError(f"UiPath 命令失败：{reply['error'][:300] or '未提供原因'}")
                    self._validate_reply(payload, reply)
                    self._apply_reply(payload, reply)
                    return reply
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._remove_pending(command_id)
                    raise RobotTimeout(f"等待 UiPath {payload['action']} 回复超时；动作结果未知，没有重发")
                time.sleep(min(self.poll_interval, remaining))
        finally:
            self._lock.release()

    def _target_geometry(self, selector: str | None) -> dict | None:
        if not selector:
            return None
        geometry = self._geometry.get(selector)
        if geometry is None and selector == self._window_selector:
            # 兼容仅记录单窗口的旧调用路径。
            return {"bounds": self._window_bounds}
        return geometry

    def _apply_reply(self, payload: dict, reply: dict) -> None:
        """登记回复里实测到的窗口几何；后续坐标动作只认最近一次实测。"""
        if payload["action"] in {"screenshot", "scroll_capture"}:
            self._window_bounds = (reply["window_width"], reply["window_height"])
            self._window_selector = payload["selector"]
            self._window_pixels = (reply["width"], reply["height"])
            self._window_origin = ((reply["window_x"], reply["window_y"])
                                   if "window_x" in reply and "window_y" in reply else None)
            self._geometry[payload["selector"]] = {
                "bounds": self._window_bounds,
                "pixels": self._window_pixels,
                "origin": self._window_origin,
                "window_id": reply.get("window_id"),
                "dpi_scale": reply.get("dpi_scale"),
                "client_rect": reply.get("client_rect"),
                "work_area": reply.get("work_area"),
            }
        elif payload["action"] == "resize":
            # 窗口被移动/改尺寸后，旧矩形立即失效；下一次截图必须重新确认。
            self._geometry.pop(payload.get("selector"), None)
            self._window_origin = None
            self._window_bounds = None
            self._window_pixels = None
            self._window_selector = None
        elif payload["action"] == "screenshot_desktop":
            # 整屏观察不覆盖最近一次小程序窗口的点击几何信息。
            self._window_origin = ((reply["window_x"], reply["window_y"])
                                   if "window_x" in reply and "window_y" in reply else None)
            # 整屏坐标不能当作窗口矩形复用，下一次窗口截图必须重新确认。
            self._window_selector = None
            self._desktop_bounds = (reply["width"], reply["height"])
            self._desktop_origin = ((reply["window_x"], reply["window_y"])
                                    if "window_x" in reply and "window_y" in reply else None)
        elif payload["action"] in {"click", "click_desktop_overlay", "right_click", "double_click"} \
                and "window_x" in reply and "window_y" in reply:
            # 点击时 UiPath 已校验过窗口尺寸，这里拿到的是刚确认过的窗口矩形，
            # 可用于点击后立即按矩形取图，不必再等一次窗口截图。
            origin = (reply["window_x"], reply["window_y"])
            bounds = ((reply["window_width"], reply["window_height"])
                      if reply.get("window_width") and reply.get("window_height") else None)
            if payload.get("selector") == self._window_selector:
                self._window_origin = origin
                if bounds:
                    self._window_bounds = bounds
            cached = self._geometry.get(payload.get("selector"))
            if cached is not None:
                cached["origin"] = origin
                if bounds:
                    cached["bounds"] = bounds

    def request_stop(self) -> None:
        """替换仍排队的动作，通知手动运行的 Main 尽快退出；不等待、不重发原动作。"""
        if self.local is None:
            _atomic_write_json(self.command_path, {"id": str(uuid.uuid4()), "action": "stop"})

    def _remove_pending(self, command_id: str) -> None:
        try:
            pending = json.loads(self.command_path.read_text(encoding="utf-8"))
            if pending.get("id") == command_id:
                self.command_path.unlink(missing_ok=True)
        except (OSError, ValueError, AttributeError):
            return

    def _validate_command(self, command: dict) -> None:
        extra = set(command) - _COMMAND_FIELDS
        if extra:
            raise RobotProtocolError(f"命令字段不受支持：{', '.join(sorted(extra))}")
        action = command.get("action")
        if action not in _ACTIONS:
            raise RobotProtocolError("不支持的 UiPath 命令动作")
        selector = command.get("selector")
        if selector is not None and (not isinstance(selector, str) or not selector.strip()):
            raise RobotProtocolError("selector 必须是非空字符串")
        if action == "resize" and (not selector or not all(isinstance(command.get(key), int) and not isinstance(command[key], bool) for key in ("width", "height"))):
            raise RobotProtocolError("resize 需要窗口 selector 和整数尺寸")
        if action in {"screenshot", "screenshot_desktop", "scroll_capture", "capture_rect"}:
            path = command.get("path")
            if not path or not any(_inside(Path(path), root) for root in self.output_directories):
                raise RobotProtocolError("截图路径必须位于当前任务输出目录内")
            if not selector:
                raise RobotProtocolError("截图命令必须指定窗口 selector")
        if action == "capture_rect":
            for key in ("x", "y", "width", "height"):
                value = command.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise RobotProtocolError("capture_rect 需要非负整数矩形")
            if command["width"] <= 0 or command["height"] <= 0:
                raise RobotProtocolError("capture_rect 矩形尺寸必须为正")
        if action in {"click", "click_desktop_overlay", "right_click", "double_click", "scroll", "scroll_capture"}:
            x, y = command.get("x"), command.get("y")
            # 系统弹窗（如识别二维码右键菜单）会超出预览窗口，按整屏坐标校验；其余动作仍以窗口矩形为边界。
            geometry = self._target_geometry(selector) or {}
            bounds = self._desktop_bounds if action == "click_desktop_overlay" else geometry.get("bounds")
            if action in {"click", "right_click", "double_click"} and x is None and y is None and selector:
                pass  # 仅供用户录入的控件 selector 定位，不猜坐标。
            elif not self._valid_pair(x, y):
                raise RobotProtocolError("坐标动作必须提供有限数值 x/y")
            elif bounds is None:
                raise RobotProtocolError("坐标动作前必须先取得窗口尺寸")
            elif command.get("expected_window_width") != bounds[0] or command.get("expected_window_height") != bounds[1]:
                raise RobotProtocolError("坐标动作的窗口尺寸复核值与最近截图不匹配")
            elif not (0 <= float(x) < bounds[0] and 0 <= float(y) < bounds[1]):
                raise RobotProtocolError("UiPath 坐标超出窗口 points 边界")
        for key in ("expected_window_width", "expected_window_height"):
            if key in command:
                value = command[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise RobotProtocolError(f"{key} 必须是有限正数 points")
        if action in {"scroll", "scroll_capture"}:
            if command.get("direction") not in {"Down", "Up"}:
                raise RobotProtocolError("scroll direction 仅允许 Down 或 Up")
            units = command.get("units")
            if isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= 100:
                raise RobotProtocolError("scroll units 必须在 1 到 100 之间")
            if "settle_seconds" in command:
                settle = command["settle_seconds"]
                if isinstance(settle, bool) or not isinstance(settle, (int, float)) \
                        or not math.isfinite(settle) or not 0 <= settle <= 5:
                    raise RobotProtocolError("scroll settle_seconds 必须在 0 到 5 之间")
        if action == "type_text" and not isinstance(command.get("text"), str):
            raise RobotProtocolError("type_text 必须包含 text")
        if action == "key" and (not isinstance(command.get("key"), str) or not command["key"].strip()):
            raise RobotProtocolError("key 必须是非空字符串")
        if action == "noop" and "seconds" in command:
            seconds = command["seconds"]
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 30:
                raise RobotProtocolError("noop seconds 必须在 0 到 30 之间")

    @staticmethod
    def _valid_pair(x: Any, y: Any) -> bool:
        if isinstance(x, bool) or isinstance(y, bool):
            return False
        try:
            return math.isfinite(float(x)) and math.isfinite(float(y))
        except (ValueError, TypeError):
            return False

    def _read_reply(self, command_id: str) -> dict:
        try:
            reply = json.loads(self.reply_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RobotProtocolError("UiPath reply.json 不是完整的 JSON 回复") from None
        if not isinstance(reply, dict) or reply.get("id") != command_id:
            raise RobotProtocolError("UiPath 回复 id 与当前命令不匹配；拒绝使用旧结果")
        if not isinstance(reply.get("ok"), bool):
            raise RobotProtocolError("UiPath 回复缺少布尔型 ok")
        if not reply["ok"] and not isinstance(reply.get("error", ""), str):
            raise RobotProtocolError("UiPath 错误回复格式无效")
        return reply

    def _validate_reply(self, command: dict, reply: dict) -> None:
        try:
            validate_target_window(command, reply)
        except ValueError as exc:
            raise RobotProtocolError(str(exc)) from exc
        if command["action"] not in {"screenshot", "screenshot_desktop", "scroll_capture"}:
            return
        path_value = reply.get("path")
        requested = Path(command["path"]).resolve()
        if not path_value:
            raise RobotProtocolError("截图回复缺少 path")
        path = Path(path_value).expanduser().resolve()
        if not any(_inside(path, root) for root in self.output_directories) or path != requested \
                or not path.is_file():
            raise RobotProtocolError("截图回复路径不属于本任务，拒绝归档")
        for key in ("width", "height"):
            value = reply.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RobotProtocolError(f"截图回复 {key} 必须是正整数像素")
        if command["action"] != "screenshot_desktop":
            for key in ("window_width", "window_height"):
                value = reply.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise RobotProtocolError(f"截图回复 {key} 必须是正数 points")
        for key in ("window_x", "window_y"):
            if key not in reply:
                continue  # 旧版 Studio 工作流可完成窗口截图，但不满足整屏坐标映射条件。
            value = reply[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise RobotProtocolError(f"截图回复 {key} 必须是有限数值")
        try:
            with Image.open(path) as image:
                if image.size != (reply["width"], reply["height"]):
                    raise RobotProtocolError("截图像素尺寸与 reply.json 不匹配")
                image.verify()
        except Exception:
            raise RobotProtocolError("UiPath 截图文件无效") from None
