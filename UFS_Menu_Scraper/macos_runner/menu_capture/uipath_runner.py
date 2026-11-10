from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Any, Callable

from PIL import Image, ImageChops, ImageStat
from pydantic import ValidationError

from .config import ConfigStore, atomic_json
from .model import Region
from .model_usage import usage_context
from .chat_anchor import find_store_name_matches
from .chat_layout import find_sent_text_bubbles
from .failures import classify as classify_failure
from .window_contract import is_windows_wechat_app, validate_target_window, window_selector, windows_selectors_overlap
from .close_icon_locator import refine_close_icon, locate_close_icon, refine_back_arrow
from .checkbox_locator import refine_consent_checkbox
from .scene_agent import SceneDecision
from .target_candidates import build_text_candidates
from .store import TaskStore
from .preview_locator import PreviewIconNotFound, locate_preview_recognize
from .chat_image_locator import ChatImageNotFound, locate_sent_image
from .ui_tars_trial import GUI_PLUS_MODEL, TrialModel, TrialProtocolError, run_navigation, trial_model_from_env
from .capture_outputs import store_output_dir
from .uipath_cli import cli_entry


class RobotProtocolError(RuntimeError):
    pass


class RobotTimeout(TimeoutError):
    pass


class RobotCancelled(RuntimeError):
    pass


class RobotCommandError(RuntimeError):
    pass


class NeedsReview(RuntimeError):
    """需要人工或重试的失败；code 决定分流，未给出时由失败文字归类。"""

    code = ""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code or type(self).code


class AgentUnqualified(NeedsReview):
    """Agent 给出了带理由的明确不合格终态。"""


class RunnerPaused(RuntimeError):
    pass


class RunnerTerminated(RuntimeError):
    pass


_ACTIONS = {"screenshot", "screenshot_desktop", "capture_rect", "click", "click_desktop_overlay", "right_click", "double_click", "scroll", "scroll_capture", "type_text", "key", "resize", "discover", "noop", "stop"}
# 微信窗口位置与尺寸固定时，本轮最新图片消息所在的缩略图中心可直接双击打开。
# 取自实机成功样本（窗口 865×958 时约 695,729；历史缩略图范围 x 684–709、y 725–729）。
_LATEST_IMAGE_POINT_RATIO = (0.8035, 0.7610)
_COMMAND_FIELDS = {
    "id", "action", "selector", "x", "y", "text", "path", "direction", "units", "key", "seconds",
    "expected_window_width", "expected_window_height", "width", "height",
}
_PROFILE_FIELDS = {
    "mode", "launch_mode", "wechat_selector", "miniapp_selector", "preview_selector",
    "selected_task_id", "selected_task_ids", "wechat_input_x_ratio",
    "wechat_input_y_ratio", "paste_shortcut", "send_key", "close_key",
    "coordinates_verified", "scroll_units", "max_tasks",
}
# “价格”本身不是交易动作；菜单与隐私说明页都会出现，不能据此判定为交易界面。
# “手机下单/扫码下单”是门店首页进入菜单的入口文案，不是提交订单；只拦截真正的提交与支付动作。
_TRANSACTION_TEXT = re.compile(r"支付|结算|加购|领券|领取|加入购物车|提交订单|提交下单|确认下单|确认购买|购物车|加号|\+|＋|¥|￥", re.I)
_AUTHORIZATION_TEXT = re.compile(r"隐私|协议|同意|允许|授权|手机号|登录|验证码|扫码", re.I)
# 系统通知/权限弹窗的目的只是把它关掉：优先拒绝，拒绝按钮置灰时点可用的允许。
_NOTIFICATION_TEXT = re.compile(
    r"发送通知|通知权限|通知提醒|接收通知|消息通知|开启通知|通知设置|订阅消息|推送通知|"
    r"发送一次以下消息|订阅提醒|申请发送|"
    r"允许.{0,6}通知|通知.{0,4}(消息|提醒)")
_NOTIFICATION_DECLINE = ("不允许", "拒绝", "以后再说", "暂不允许", "暂不提醒", "暂不开启")
_NOTIFICATION_ALLOW = ("允许", "开启", "去开启", "同意")
# 微信自动识别到二维码不是小程序时会提示改用浏览器打开。
_EXTERNAL_LINK = re.compile(r"在浏览器打开|打开系统浏览器|打开此链接|想要打开.{0,12}(链接|网页)")
# 识别后没有小程序窗口时只记录画面原文，是否还能进入菜单由 Agent 判断；
# 不在宿主侧按渠道图标组合给出门店结论。
# 隐私/授权弹窗还没处理时，页面上的“网络有点异常”往往只是初始化提示，不能据此判死。
_PENDING_CONSENT = re.compile(r"同意并继续|同意并授权|同意继续|隐私保护指引.{0,20}同意|请点击.{0,6}同意")
# 模型给出粗坐标后，本地在周围重识别时要按当前场景挑按钮，而不是随便点最近的文字。
_EXPECTED_BUTTON_TEXT = {
    "close": ("关闭", "跳过", "知道了", "我知道了", "不再提示", "×", "x"),
    "decline": ("不允许", "拒绝", "以后再说", "暂不允许", "暂不开启", "暂不提醒"),
    "allow": ("允许", "同意", "授权", "继续", "允许并继续", "我知道了，允许"),
    "confirm": ("确定", "确认", "完成", "开始点餐", "去点餐", "查看菜单", "进入", "立即点餐"),
    "people": ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"),
}
# 页面状态判死时给用户的结论；code 本身决定可重试 / 不可采 / 需人工。
_PAGE_STATE_MESSAGES = {
    "network_error": "页面反复提示网络异常，重试后仍无法进入菜单；该门店当前不可采",
    "store_closed": "页面显示门店已打烊或休息，且没有可见菜单",
    "menu_unavailable": "页面显示暂无菜品或暂无菜单；该门店当前不可采",
    "menu_not_configured": "页面显示门店未设置点餐/装修数据；该门店没有可采菜单",
    "pickup_unavailable": "该城市未开通自提/点餐业务，无法进入菜单；该门店当前不可采",
    "qr_expired": "页面显示二维码已失效或无效，无法进入小程序菜单",
    "miniapp_unavailable": "页面显示小程序已到期、停止服务或门店未绑定小程序",
    "scan_order_unsupported": "页面提示门店暂不支持扫码点餐，需联系服务员",
    "phone_only": "页面要求在手机上打开或长按识别；桌面小程序无法继续采集",
    "customer_service": "页面跳转到了客服入口；桌面小程序无法继续采集",
}
def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        temporary.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        import os

        os.fsync(stream.fileno())
    temporary.replace(path)
    path.chmod(0o600)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def production_window_geometry(screen) -> dict:
    """批量采集统一使用的小程序窗口几何（贴屏幕左侧、全高）。

    人工标注也套用同一尺寸，保证样本截图与正式执行时的版面一致。
    """
    return {"width": 240, "height": max(300, int(screen.height) - 100),
            "x": int(screen.x), "y": int(screen.y) + 35}


class FileRobotTransport:
    """UiPath 与 Python 之间的一命令一回复文件 IPC。超时后绝不重发动作。"""

    def __init__(self, run_directory: Path, output_directory: Path | None = None, poll_interval: float = 0.05):
        self.run_directory = Path(run_directory).expanduser().resolve()
        self.output_directory = Path(output_directory or run_directory).expanduser().resolve()
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
            if payload.get("action") in {"click", "click_desktop_overlay", "right_click", "double_click", "scroll", "scroll_capture"} and payload.get("x") is not None:
                selector = payload.get("selector")
                geometry = self._geometry.get(selector) if selector else None
                if geometry is None and selector and selector == self._window_selector:
                    # 兼容只设置单槽几何的调用方（测试与旧调用路径）。
                    geometry = {"bounds": self._window_bounds}
                if not selector or not geometry or not geometry.get("bounds"):
                    raise RobotProtocolError("坐标动作必须使用最近一次同 selector 截图确认的窗口")
                payload["expected_window_width"], payload["expected_window_height"] = geometry["bounds"]
            self._validate_command(payload)
            if timeout <= 0 or not math.isfinite(float(timeout)):
                raise ValueError("timeout 必须是正数")
            if cancel_event and cancel_event.is_set():
                raise RobotCancelled("命令发送前已收到暂停")
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
                    elif payload["action"] in {"click", "click_desktop_overlay", "right_click", "double_click"} \
                            and "window_x" in reply and "window_y" in reply:
                        # 点击时 UiPath 已校验过窗口尺寸，这里拿到的是刚确认过的窗口矩形，
                        # 可用于点击后立即按矩形取图，不必再等一次窗口截图。
                        self._window_origin = (reply["window_x"], reply["window_y"])
                        if reply.get("window_width") and reply.get("window_height"):
                            self._window_bounds = (reply["window_width"], reply["window_height"])
                        cached = self._geometry.get(payload.get("selector"))
                        if cached is not None:
                            cached["origin"] = self._window_origin
                            if reply.get("window_width") and reply.get("window_height"):
                                cached["bounds"] = self._window_bounds
                    return reply
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._remove_pending(command_id)
                    raise RobotTimeout(f"等待 UiPath {payload['action']} 回复超时；动作结果未知，没有重发")
                time.sleep(min(self.poll_interval, remaining))
        finally:
            self._lock.release()

    def request_stop(self) -> None:
        """替换仍排队的动作，通知手动运行的 Main 尽快退出；不等待、不重发原动作。"""
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
            if not path or not _inside(Path(path), self.output_directory):
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
            if action in {"click", "right_click", "double_click"} and x is None and y is None and selector:
                pass  # 仅供用户录入的控件 selector 定位，不猜坐标。
            elif not self._valid_pair(x, y):
                raise RobotProtocolError("坐标动作必须提供有限数值 x/y")
            elif self._window_bounds is None:
                raise RobotProtocolError("坐标动作前必须先取得窗口尺寸")
            elif command.get("expected_window_width") != self._window_bounds[0] or command.get("expected_window_height") != self._window_bounds[1]:
                raise RobotProtocolError("坐标动作的窗口尺寸复核值与最近截图不匹配")
            elif not (0 <= float(x) < self._window_bounds[0] and 0 <= float(y) < self._window_bounds[1]):
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
        if not _inside(path, self.output_directory) or path != requested or not path.is_file():
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


class UiPathRunner:
    """手动启动单个 UiPath Main；attached 单任务，automatic 可运行有限实验批次。"""

    def __init__(
        self,
        configs: ConfigStore,
        store: TaskStore,
        profile_path: Path,
        *,
        project_dir: Path | None = None,
        transport_factory=FileRobotTransport,
        clipboard_preparer: Callable[[Path], None] | None = None,
        clipboard_text_preparer: Callable[[str], None] | None = None,
        ocr_reader: Callable[[Path], list[dict]] | None = None,
        qr_detector: Callable[[Path], list[dict]] | None = None,
        process_factory: Callable[..., Any] = subprocess.Popen,
        debug_log=None,
        usage_ledger=None,
    ):
        self.configs, self.store = configs, store
        self.profile_path = Path(profile_path).expanduser().resolve()
        self.project_dir = Path(project_dir).expanduser().resolve() if project_dir else self.profile_path.parent
        self.transport_factory = transport_factory
        self.clipboard_preparer = clipboard_preparer
        self.clipboard_text_preparer = clipboard_text_preparer
        self.ocr_reader, self.qr_detector = ocr_reader, qr_detector
        # 本地候选点击无效后记录已尝试的位置，下一轮本地规则改试同一文案的下一个候选。
        self._consumed_local_points: list[tuple[float, float]] = []
        # 点击后按已知窗口矩形直接取图；权限或尺寸异常时置为 False，整轮退回 UiPath 截图。
        self._fast_capture_enabled = True
        self.process_factory = process_factory
        self.debug_log = debug_log
        self.usage_ledger = usage_ledger
        self.stop_event = threading.Event()
        # 暂停只在门店边界生效；终止则在当前 UiPath 命令回复后停止发送下一条命令。
        self.pause_event = threading.Event()
        self.terminate_event = self.stop_event
        self.stop = self.stop_event
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.current_task: dict | None = None
        self.run_directory: Path | None = None  # in_RunDirectory 对应的当前私有 IPC 路径。
        self.output_directory: Path | None = None
        self.transport: FileRobotTransport | Any | None = None
        self._deadline = 0.0
        self._cli_process: Any | None = None
        self._cli_metadata_path: Path | None = None
        self._cli_log_path: Path | None = None
        self._cli_args_path: Path | None = None
        self._cli_info: dict | None = None
        self._cli_reader_thread: threading.Thread | None = None
        # 项目源文件未变时复用上次构建产物，跳过 UiPath 的再次校验与编译。
        self._skip_build_requested = False
        self._build_fingerprint: str | None = None
        self._build_cache_path: Path | None = None
        self._launch_params: tuple | None = None
        self._debug_frame_refs: dict[str, str] = {}
        self._last_ocr_meta: dict = {}
        self._last_target_resolution: dict | None = None
        self._active_ocr_path: str | None = None
        self._active_ocr_result: tuple[tuple[int, int] | None, list[dict]] | None = None
        self.run_id: str | None = None
        # 批次起点保护：每个 run 的首个任务保留一次截图+OCR，后续任务复用已确认的窗口几何。
        self._startup_guard_run_id: str | None = None
        self._ipc_commands = 0
        self._last_readiness: dict | None = None
        # 人工标注复用截图/点击能力，但不得改动采集任务状态。
        self._annotation_mode = False
        self._ui_tars_trial_active = False

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def last_readiness(self) -> dict | None:
        return dict(self._last_readiness) if self._last_readiness else None

    def invalidate_readiness(self) -> None:
        self._last_readiness = None

    def readiness(self, task_ids_override: list[str] | None = None, *, trial: bool = False,
                  trial_task_id: str | None = None, trial_source: str = "",
                  trial_batch_sources: dict[str, str] | None = None) -> dict:
        issues: list[str] = []
        profile: dict = {}
        try:
            parsed = json.loads(self.profile_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("profile 必须是 JSON 对象")
            unknown = set(parsed) - _PROFILE_FIELDS
            if unknown:
                issues.append("profile 含有不支持的字段")
            profile = {key: value for key, value in parsed.items() if key in _PROFILE_FIELDS}
        except FileNotFoundError:
            issues.append("尚未找到 uipath.profile.json")
        except (OSError, ValueError):
            issues.append("uipath.profile.json 无法读取或不是有效 JSON")
        if trial_task_id:
            # 评测复用真实任务与执行器，接管参数仅作用于本次，不改批量队列和配置。
            profile.update(mode="attached", selected_task_id=trial_task_id, max_tasks=1)
        elif trial_batch_sources:
            profile.update(mode="automatic")

        mode = profile.get("mode", "attached")
        if mode not in {"attached", "automatic"}:
            issues.append("mode 只能是 attached 或 automatic")
        # automatic 会在扫码后通过 UiPath discover 取得本轮窗口，不能依赖店铺固定标题。
        required_selectors = () if mode == "attached" else (
            "wechat_selector", "preview_selector"
        )
        for key in required_selectors:
            if not isinstance(profile.get(key), str) or not profile[key].strip():
                issues.append(f"profile 缺少 {key}；请在 UiPath 中录制并填写实际 selector")
        if mode == "automatic" and profile.get("coordinates_verified") is not True:
            issues.append("坐标尚未由用户校准确认；自动化不会假设默认点击位置")
        launch_mode = profile.get("launch_mode", "manual")
        if trial_batch_sources and launch_mode != "cli":
            issues.append("GUI Agent 全自动试验需要 UiPath CLI 启动方式")
        if launch_mode not in {"cli", "manual"}:
            issues.append("launch_mode 只能是 cli 或 manual")
        if launch_mode == "cli":
            if not cli_entry():
                issues.append("未找到 UiPath CLI（uip）；请确认已安装并位于 PATH")
            if not self.project_dir.is_dir():
                issues.append("UiPath 项目目录不存在")
            elif not (self.project_dir / "Main.xaml").is_file():
                issues.append("UiPath 项目缺少 Main.xaml")
        task_id = profile.get("selected_task_id")
        raw_task_ids = task_ids_override if task_ids_override is not None else profile.get("selected_task_ids", [])
        task_ids: list[str] = []
        if mode == "automatic":
            if not isinstance(raw_task_ids, list) or not raw_task_ids or any(not isinstance(value, str) or not value.strip() for value in raw_task_ids):
                issues.append("automatic 模式请按顺序选择至少一个来源任务 ID")
            else:
                task_ids = [value.strip() for value in raw_task_ids]
                if len(set(task_ids)) != len(task_ids):
                    issues.append("selected_task_ids 不能包含重复任务")
        else:
            if not isinstance(task_id, str) or not task_id.strip():
                issues.append("请先在任务列表中手动指定一个来源任务 ID")
            else:
                task_ids = [task_id.strip()]
        max_tasks = profile.get("max_tasks", 1)
        if mode == "attached" and (isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks != 1):
            issues.append("attached 模式只允许一个明确选定的任务；max_tasks 必须为 1")

        try:
            settings = self.configs.load()
            if not settings.input_dir or not settings.output_dir:
                issues.append("输入目录或输出目录尚未配置")
        except Exception:
            settings = None
            issues.append("配置服务设置无法读取")

        if settings is not None:
            try:
                trial_model_from_env(self.configs)
            except ValueError as exc:
                issues.append(str(exc))

        if mode == "automatic":
            for key in ("paste_shortcut", "send_key", "close_key"):
                if not isinstance(profile.get(key), str) or not profile[key].strip():
                    issues.append(f"automatic 模式缺少已录制的 {key}")
            for key in ("wechat_input_x_ratio", "wechat_input_y_ratio"):
                value = profile.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
                    issues.append(f"automatic 模式需要校准 {key}（0 到 1 之间）")
            if self.clipboard_preparer is None:
                issues.append("图片剪贴板准备接口尚未接入")
            if self.clipboard_text_preparer is None:
                issues.append("门店名称剪贴板准备接口尚未接入")

        task = None
        selected_tasks = []
        for selected_id in task_ids:
            try:
                selected = self.store.get(selected_id)
                if selected["status"] not in {"pending", "needs_review", "needs_retry", "error", "blocked", "captured"}:
                    issues.append(f"任务状态 {selected['status']} 不允许重新接管")
                if settings is not None:
                    source_root = trial_batch_sources[selected_id] if trial_batch_sources else trial_source or settings.input_dir
                    self._validate_source(selected, source_root)
                selected_tasks.append(selected)
            except Exception as exc:
                if isinstance(exc, NeedsReview):
                    issues.append(str(exc))
                else:
                    issues.append("指定任务不存在或来源文件已变化")
        if selected_tasks:
            task = selected_tasks[0]
        if mode == "automatic" and settings is not None and not trial_batch_sources:
            binding = self.store.material_binding(settings.input_dir) or {}
            destinations = [str(self._store_output_dir(settings.output_dir, selected,
                                                       binding.get("batch_id", ""),
                                                       binding.get("batch_name", ""))).casefold()
                            for selected in selected_tasks]
            if len(set(destinations)) != len(destinations):
                issues.append("所选素材映射到同一个门店输出目录；请先在素材清洗中每家店只发布一张二维码")

        result = {
            "ready": not issues,
            "issues": issues,
            "mode": mode,
            "launch_mode": launch_mode,
            "profile": profile,
            "project_dir": str(self.project_dir),
            "selected_task_id": task_id if isinstance(task_id, str) else None,
            "selected_task_ids": task_ids,
            "tasks": [{"id": value["id"], "source_name": value["source_name"]} for value in selected_tasks],
            "task": {"id": task["id"], "source_name": task["source_name"]} if task else None,
            "experimental_batch": mode == "automatic" and len(task_ids) > 1,
        }
        if not trial:
            self._last_readiness = result
        return result

    def _registered_trial_source(self, task_id: str) -> str:
        source = Path(self.store.get(task_id)["source_path"]).resolve()
        snapshot = source.parent.parent
        published = (self.store.path.parent / "materials/published").resolve()
        return str(snapshot) if snapshot.parent == published and self.store.material_binding(str(snapshot)) else ""

    def start(self, resume_run: dict | None = None, *, ui_tars_trial: bool = False,
              trial_task_id: str | None = None, trial_task_ids: list[str] | None = None) -> dict:
        with self.lock:
            if self.running:
                raise ValueError("UiPath 单任务控制器已在运行")
            task_ids_override = resume_run.get("task_ids", []) if resume_run else None
            start_index = int(resume_run.get("current_index", 0)) if resume_run else 0
            if resume_run:
                snapshot = resume_run.get("material_snapshot")
                if snapshot and Path(self.configs.load().input_dir).name != snapshot:
                    raise ValueError("当前素材与暂停任务的版本不同，请切回原版本后继续")
                task_ids_override = task_ids_override[start_index:]
                if not task_ids_override:
                    raise ValueError("批次中没有尚未完成的门店")
            trial_source = ""
            trial_batch_sources = None
            if trial_task_id:
                if not ui_tars_trial or resume_run:
                    raise ValueError("指定试验门店仅用于 GUI Agent 单店接管")
                # 未登记的普通素材仍须属于当前配置的输入目录，由 readiness 校验。
                trial_source = self._registered_trial_source(trial_task_id)
            if trial_task_ids is not None:
                if not ui_tars_trial or resume_run or trial_task_id or not 1 <= len(trial_task_ids) <= 20:
                    raise ValueError("全自动试验须指定 1 到 20 条已发布的门店任务")
                trial_batch_sources = {task_id: self._registered_trial_source(task_id) for task_id in trial_task_ids}
                if not all(trial_batch_sources.values()):
                    raise ValueError("全自动试验门店必须使用已登记且有效的发布素材")
                task_ids_override = trial_task_ids
            status = (self.readiness(task_ids_override=task_ids_override, trial=True,
                                     trial_task_id=trial_task_id, trial_source=trial_source,
                                     trial_batch_sources=trial_batch_sources) if ui_tars_trial
                      else self.readiness(task_ids_override=task_ids_override))
            if not status["ready"]:
                raise ValueError("UiPath 尚未就绪：" + "；".join(status["issues"]))
            profile = status["profile"]
            if ui_tars_trial and not trial_batch_sources and (
                resume_run or profile["mode"] != "attached" or len(status["selected_task_ids"]) != 1
            ):
                raise ValueError("GUI Agent 试验仅接管已打开小程序的单店任务")
            model = trial_model_from_env(self.configs)
            self.stop_event.clear()
            self.pause_event.clear()
            task_ids = status["selected_task_ids"]
            max_tasks = profile.get("max_tasks", 1)
            if not trial_batch_sources and profile.get("mode") == "automatic" and isinstance(max_tasks, int) and not isinstance(max_tasks, bool):
                # 界面约定 automatic 每批最多 100 家；这里真正截断，避免一次启动跑完全部勾选门店。
                task_ids = task_ids[:max(1, min(100, max_tasks))]
            task = self.store.get(task_ids[0])
            settings = self.configs.load()
            if trial_source:
                settings = settings.model_copy(update={"input_dir": trial_source})
            attempt = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8]
            run_directory = Path(settings.output_dir).expanduser().resolve() / ".uipath-runs" / (attempt + "-" + uuid.uuid4().hex[:8])
            output_dir = (run_directory / "ui-tars-trial" if ui_tars_trial
                          else self._staging_dir(settings.output_dir, task, attempt))
            if not ui_tars_trial:
                output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
                output_dir.chmod(0o700)
            run_directory.mkdir(parents=True, mode=0o700, exist_ok=False)
            run_directory.chmod(0o700)
            if ui_tars_trial:
                output_dir.mkdir(mode=0o700)
            entry = {
                "in_RunDirectory": str(run_directory),
                "task_id": task["id"],
                "selected_task_ids": task_ids,
                "mode": profile["mode"],
                "project_dir": str(self.project_dir),
                "main_file": "Main.xaml",
                "idle_timeout_seconds": 600,
                "startup_timeout_seconds": 300,
                "launch_mode": profile.get("launch_mode", "manual"),
            }
            _atomic_write_json(run_directory / "entry.json", entry)
            self.run_directory, self.output_directory = run_directory, output_dir
            self.transport = self.transport_factory(run_directory, Path(settings.output_dir).expanduser().resolve())
            if isinstance(self.transport, FileRobotTransport):
                self.transport.retain_command_history = self.debug_log is None
            self._cli_process = None
            self._cli_metadata_path = self._cli_log_path = self._cli_args_path = None
            self._cli_info = None
            self._cli_reader_thread = None
            self.run_id = resume_run["id"] if resume_run else uuid.uuid4().hex
            if profile.get("launch_mode", "manual") == "cli":
                self._launch_cli(run_directory, settings, profile, task_count=len(task_ids))
            if ui_tars_trial:
                self._deadline = 0
                self._ui_tars_trial_active = True
                self.current_task = {"id": task["id"], "source_name": task["source_name"],
                                     "stage": "ui_tars_connecting", "mode": profile["mode"],
                                     "navigation": "gui_agent", "trial": True}
                self.thread = threading.Thread(
                    target=self._ui_tars_trial_worker,
                    args=(task, settings, profile, output_dir, model, trial_batch_sources),
                    name="ui-tars-trial", daemon=True,
                )
                self.thread.start()
                return {"ok": True, "running": True, "navigation": "gui_agent", "run_id": self.run_id,
                        "task_id": task["id"], "in_RunDirectory": str(run_directory),
                        "task_count": len(task_ids),
                        "result_file": str(output_dir / ("batch-result.json" if trial_batch_sources else "navigation-result.json")),
                        "launch_mode": profile.get("launch_mode", "manual")}
            if resume_run:
                self.store.update_run(self.run_id, status="resuming", current_task_id=task_ids[0],
                                      message="正在恢复批次；开始下一家前会先确认微信安全起点")
            else:
                self.store.create_run(self.run_id, profile["mode"], task_ids,
                                      material=self.store.material_binding(settings.input_dir))
            self._deadline = 0
            self.current_task = {"id": task["id"], "source_name": task["source_name"], "stage": "waiting_uipath", "mode": profile["mode"]}
            launch_mode = profile.get("launch_mode", "manual")
            start_message = "UiPath CLI 已启动 Main.xaml 并传入本轮 IPC 目录" if launch_mode == "cli" else "等待用户在 UiPath Studio 手动运行 Main.xaml"
            self.store.update(task["id"], status="running", stage="waiting_uipath", message=start_message)
            self.store.event(start_message, task_id=task["id"])
            self.thread = threading.Thread(target=self._worker,
                                           args=(task, settings, profile, output_dir, model, task_ids, start_index, bool(resume_run)),
                                           name="uipath-menu-capture", daemon=True)
            self.thread.start()
            return {
                "ok": True,
                "running": True,
                "mode": profile["mode"],
                "launch_mode": launch_mode,
                "cli_pid": self._cli_info.get("pid") if self._cli_info else None,
                "task_id": task["id"],
                "in_RunDirectory": str(run_directory),
                "entry_file": str(run_directory / "entry.json"),
                "project_dir": str(self.project_dir),
                "main_file": "Main.xaml",
                "idle_timeout_seconds": 600,
                "startup_timeout_seconds": 300,
                "max_tasks": profile.get("max_tasks", 1),
                "experimental_batch": bool(status.get("experimental_batch")),
                "task_count": len(task_ids),
                "run_id": self.run_id,
                "resumed": bool(resume_run),
                "message": ("UiPath CLI 正在启动 Main.xaml；启动握手上限 300 秒。" if launch_mode == "cli" else "请在 UiPath Studio 手动运行 Main.xaml（需在 300 秒内启动），并将 in_RunDirectory 参数设为返回路径；Python 不会自动打开 Studio。"),
            }

    def pause(self) -> None:
        if not self.running:
            raise ValueError("当前没有运行中的批次")
        if self._ui_tars_trial_active:
            raise ValueError("GUI Agent 试验可以终止后重试")
        self.pause_event.set()
        if self.run_id:
            self.store.request_run_state(self.run_id, "pause_requested", "已请求暂停；当前门店完成并安全返回后暂停")
        if self.current_task:
            self.store.event("已请求暂停；当前门店会继续到安全边界", task_id=self.current_task["id"])

    def terminate(self) -> None:
        if not self.running:
            run = self.store.latest_run()
            if run and run["status"] in {"paused", "blocked", "interrupted"}:
                self.store.update_run(run["id"], status="terminated", message="本轮已终止，已保存的截图仍可查看")
                self.store.event("已终止批次，剩余门店未处理")
                return
            raise ValueError("当前没有可终止的批次")
        self.stop_event.set()
        if self._ui_tars_trial_active:
            return
        if self.run_id:
            self.store.request_run_state(self.run_id, "terminating", "终止请求已收到；当前 UiPath 命令结束后停止本批次")
        if self.current_task:
            self.store.event("已请求终止；当前 UiPath 命令结束后停止发送新命令", task_id=self.current_task["id"])

    def _launch_cli(self, run_directory: Path, settings, profile: dict, *, task_count: int | None = None,
                    timeout_seconds: float | None = None, allow_skip_build: bool = True) -> None:
        """只启动当前这轮自己的 Main.xaml 进程，并把 CLI 工件留在私有 run 目录。"""
        entry = cli_entry()
        if not entry:
            raise ValueError("未找到 UiPath CLI（uip）；请确认已安装并位于 PATH")
        args_path = run_directory / "args.json"
        log_path = self.debug_log.cli_log_path(self.run_id or run_directory.name) if self.debug_log else run_directory / "cli.log"
        metadata_path = run_directory / "cli-process.json"
        _atomic_write_json(args_path, {"in_RunDirectory": str(run_directory)})

        environment = os.environ.copy()
        dotnet_root = Path("/Users/mac/.dotnet")
        if sys.platform != "win32" and dotnet_root.is_dir():
            environment["DOTNET_ROOT"] = str(dotnet_root)
            environment["PATH"] = str(dotnet_root) + os.pathsep + environment.get("PATH", "")
        else:
            dotnet_path = shutil.which("dotnet", path=environment.get("PATH"))
            if dotnet_path:
                environment.setdefault("DOTNET_ROOT", str(Path(dotnet_path).resolve().parent))
        environment["UIPATH_CLI_DISABLE_VERSION_SYNC"] = "true"

        startup_timeout = 300
        max_tasks = task_count if task_count is not None else len(profile.get("selected_task_ids", []))
        # 人工标注按人工节奏进行，不能用自动采集的单店上限计算 CLI 生命周期。
        cli_timeout = (int(timeout_seconds) if timeout_seconds else
                       settings.capture.max_task_seconds * max_tasks + startup_timeout + 60)
        command = [
            *entry, "rpa", "run",
            "--project-dir", str(self.project_dir),
            "--file-path", "Main.xaml",
            "--input-arguments-file", str(args_path),
            "--timeout", str(cli_timeout),
            "--output", "json",
        ]
        self._build_fingerprint = self._project_build_fingerprint()
        self._build_cache_path = Path(settings.output_dir).expanduser().resolve() / ".uipath-build" / "state.json"
        self._skip_build_requested = allow_skip_build and self._build_cache_matches()
        if self._skip_build_requested:
            # 源文件与上次成功构建一致，直接复用 .local/install 产物。
            command.append("--skip-build")
        self._launch_params = (run_directory, settings, profile, task_count, timeout_seconds)
        log_stream = None
        try:
            if self.debug_log:
                process = self.process_factory(
                    command, cwd=str(self.project_dir), env=environment,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    shell=False, close_fds=True,
                )
            else:
                fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                log_stream = os.fdopen(fd, "ab")
                process = self.process_factory(
                    command, cwd=str(self.project_dir), env=environment,
                    stdin=subprocess.DEVNULL, stdout=log_stream, stderr=subprocess.STDOUT,
                    shell=False, close_fds=True,
                )
        except Exception as exc:
            if log_stream:
                log_stream.close()
            _atomic_write_json(metadata_path, {
                "pid": None, "exit_code": None, "status": "start_failed",
                "error_type": type(exc).__name__, "log": str(log_path),
            })
            raise ValueError(f"UiPath CLI 启动失败（{type(exc).__name__}）") from None
        finally:
            if log_stream:
                log_stream.close()

        self._cli_process = process
        self._cli_args_path, self._cli_log_path, self._cli_metadata_path = args_path, log_path, metadata_path
        self._cli_info = {"pid": int(process.pid), "exit_code": None, "status": "running"}
        if self.debug_log and getattr(process, "stdout", None) is not None:
            self._cli_reader_thread = threading.Thread(
                target=self._drain_cli_output, args=(process.stdout, str(self.run_id or run_directory.name)),
                name="uipath-cli-log-reader", daemon=True,
            )
            self._cli_reader_thread.start()
        self._write_cli_metadata()

    def _drain_cli_output(self, stream, run_key: str) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                self.debug_log.add_cli_output(run_key, chunk)
        except (OSError, ValueError):
            self.debug_log.emit("uipath_cli_output_read_error", run_id=run_key)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _write_cli_metadata(self) -> None:
        if self._cli_metadata_path is None or self._cli_info is None:
            return
        value = dict(self._cli_info)
        value.update({
            "args_file": str(self._cli_args_path),
            "log_file": str(self._cli_log_path),
            "project_dir": str(self.project_dir),
        })
        _atomic_write_json(self._cli_metadata_path, value)

    def _project_build_fingerprint(self) -> str:
        """源文件内容指纹：Main.xaml 等任一改动都必须重新构建。"""
        digest = hashlib.sha256()
        sources = sorted(self.project_dir.glob("*.xaml")) + [self.project_dir / "project.json"]
        for path in sources:
            if not path.is_file():
                continue
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                return ""
            digest.update(b"\0")
        return digest.hexdigest()

    def _build_cache_matches(self) -> bool:
        if not self._build_fingerprint or self._build_cache_path is None:
            return False
        if not (self.project_dir / ".local" / "install").is_dir():
            return False
        try:
            cached = json.loads(self._build_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(cached, dict) and cached.get("fingerprint") == self._build_fingerprint

    def _record_build_cache(self) -> None:
        """握手成功后确认产物可用，供下一轮复用。"""
        if not self._build_fingerprint or self._build_cache_path is None:
            return
        try:
            _atomic_write_json(self._build_cache_path, {
                "fingerprint": self._build_fingerprint,
                "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
        except OSError:
            return

    def _invalidate_build_cache(self) -> None:
        if self._build_cache_path is None:
            return
        try:
            self._build_cache_path.unlink(missing_ok=True)
        except OSError:
            return

    def _await_cli_startup(self) -> None:
        try:
            self._handshake_cli()
        except (RobotCommandError, RobotTimeout) as exc:
            # 复用构建产物失败说明产物已过期或被清理；本轮立刻完整构建重来一次。
            if not self._skip_build_requested or not self._launch_params:
                raise
            self._invalidate_build_cache()
            self._terminate_cli()
            run_directory, settings, profile, task_count, timeout_seconds = self._launch_params
            self._launch_cli(run_directory, settings, profile, task_count=task_count,
                             timeout_seconds=timeout_seconds, allow_skip_build=False)
            self._handshake_cli()
        self._record_build_cache()

    def _terminate_cli(self) -> None:
        process = self._cli_process
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except (OSError, subprocess.SubprocessError):
                return
        finally:
            reader = self._cli_reader_thread
            if reader is not None:
                reader.join(timeout=1)
            self._cli_reader_thread = None
            self._cli_process = None

    def _handshake_cli(self) -> None:
        process = self._cli_process
        if process is None:
            self._send({"action": "noop"}, timeout=300)
            return
        self._guard()
        code = process.poll()
        if code is not None:
            self._cli_info.update(exit_code=int(code), status="exited_before_handshake")
            self._write_cli_metadata()
            raise RobotCommandError(f"UiPath CLI 在 Main IPC 握手前退出（exit code {code}）")
        # 握手只发送一个ID，等待期间由transport监测进程和取消。
        # 反复用新ID重发noop会与上一条迟到的回复竞争，造成偶发的协议ID不匹配。
        kwargs = {"timeout": 300}
        if isinstance(self.transport, FileRobotTransport):
            kwargs["process"] = process
        try:
            self.transport.run({"action": "noop"}, **kwargs)
        except RobotCancelled:
            self._guard()
            raise RunnerPaused("等待 UiPath CLI 握手时收到暂停") from None

    def _guard(self) -> None:
        if self.stop_event.is_set():
            if self._ui_tars_trial_active:
                raise RunnerTerminated("用户已终止 GUI Agent 试验")
            raise RunnerTerminated("用户已终止批次；当前门店结果不发布")
        if self._deadline and time.monotonic() > self._deadline:
            raise NeedsReview("超过单任务时间上限；本次结果待人工检查")

    @contextmanager
    def _trace(self, action, **details):
        task = self.current_task or {}
        attempt = getattr(self, "_trace_attempt", None)
        if not self.run_id or not task.get("id") or not attempt:
            with usage_context(source="capture", run_id=self.run_id or "", task_id=task.get("id", ""),
                               source_name=task.get("source_name", ""), stage=task.get("stage", "")):
                yield details
            return
        started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        compact = {"debug_enabled": bool(self.debug_log), "debug_state": "pending" if self.debug_log else "expired"}
        target = details.get("target") or details.get("target_label")
        if target:
            compact["target"] = self._sanitize_target_label(str(target))
        action_id = self.store.start_action(
            self.run_id, task["id"], attempt, task.get("stage", ""),
            getattr(self, "_trace_step", None), action, compact if self.debug_log else details,
        )
        started = time.monotonic()
        result = "ok"
        try:
            material = getattr(self, "_trace_material", {})
            # 通过动作上下文传递计量归属，不把模型响应或业务状态复制进另一套状态机。
            with usage_context(source="capture", run_id=self.run_id, task_id=task["id"],
                               source_name=task.get("source_name", ""), attempt_id=attempt,
                               action_id=action_id, stage=task.get("stage", ""),
                               step=getattr(self, "_trace_step", None),
                               material_batch_id=material.get("material_batch_id", ""),
                               material_version=material.get("material_version", "")):
                yield details
        except BaseException as exc:
            result = "unknown" if isinstance(exc, (RobotTimeout, RobotCancelled)) else "error"
            details["error_type"] = type(exc).__name__
            details["error_message"] = str(exc)[:2000]
            details["error_stack"] = traceback.format_exc(limit=30)
            raise
        finally:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if self.debug_log:
                record = {
                    "action_id": action_id,
                    "run_id": self.run_id,
                    "task_id": task["id"],
                    "source_name": task.get("source_name", ""),
                    "material": getattr(self, "_trace_material", {}),
                    "attempt_id": attempt,
                    "stage": task.get("stage", ""),
                    "step": getattr(self, "_trace_step", None),
                    "action": action,
                    "started_at": started_at,
                    "elapsed_ms": elapsed_ms,
                    "result": result,
                    "details": details,
                }
                queued = self.debug_log.emit_action(record)
                compact = {"debug_enabled": True, "debug_state": "pending" if queued else "dropped"}
                if target:
                    compact["target"] = self._sanitize_target_label(str(target))
                if details.get("error_type"):
                    compact["error_type"] = details["error_type"]
                self.store.finish_action(action_id, elapsed_ms, result, compact)
            else:
                self.store.finish_action(action_id, elapsed_ms, result, details)

    def _send(self, command: dict, timeout: float | None = None) -> dict:
        self._guard()
        try:
            command_timeout = timeout or 90
            if self._deadline:
                command_timeout = max(.01, min(command_timeout, self._deadline - time.monotonic()))
            kwargs = {"timeout": command_timeout}
            if self._cli_process is not None and isinstance(self.transport, FileRobotTransport):
                kwargs["process"] = self._cli_process
            command = dict(command)
            command_id = command.setdefault("id", str(uuid.uuid4()))
            safe_parameters = {}
            for key, value in command.items():
                if key in {"id", "text", "key"}:
                    continue
                safe_parameters["path_basename" if key == "path" else key] = (
                    Path(value).name if key == "path" and value else value
                )
            details = {
                "command_id": command_id,
                "command_parameters": safe_parameters,
                "duration_scope": "command_round_trip",
            }
            if command.get("action") == "key" and re.fullmatch(r"(?i)(?:cmd\+v|ctrl\+v|return|enter|escape|esc|cmd\+w|alt\+f4)", str(command.get("key", ""))):
                details["command_parameters"]["key"] = command["key"]
            details.update(getattr(self, "_trace_click", {}))
            command_started = time.monotonic()
            with self._trace(command["action"], **details) as trace:
                reply = self.transport.run(command, **kwargs)
                self._ipc_commands += 1
                summary = {key: reply.get(key) for key in (
                    "ok", "action", "error", "width", "height", "window_width", "window_height", "exit_code", "target_window"
                ) if key in reply}
                if isinstance(reply.get("windows"), list):
                    summary["windows"] = [
                        {key: item.get(key) for key in ("app", "title", "role", "position") if key in item}
                        for item in reply["windows"] if isinstance(item, dict)
                    ][:100]
                elif isinstance(reply.get("windows"), str):
                    summary["windows_raw"] = reply["windows"][:12000]
                trace["reply_summary"] = summary
                trace["command_elapsed_ms"] = round((time.monotonic() - command_started) * 1000)
                return reply
        except RobotCancelled as exc:
            raise RunnerPaused(str(exc)) from None
        except RobotCommandError as exc:
            # 其他窗口抢焦点时 UiPath 会拒绝截图/键盘输入；把微信唤回前台后重试一次。
            # 试验期间 Agent 的动作同样需要这次恢复，否则一次失焦就会整店失败。
            if (sys.platform == "darwin" and "foreground=false" in str(exc)
                    and not getattr(self, "_focus_retry_active", False)):
                self._focus_retry_active = True
                try:
                    # open -a 只保证应用被激活，多个微信窗口时目标窗口仍可能不在前台；
                    # 先用 AppleScript 显式激活，再点一次标题栏把目标窗口变成 key window。
                    subprocess.run(["osascript", "-e", 'tell application "WeChat" to activate'],
                                   check=False, timeout=15)
                    subprocess.run(["open", "-a", "WeChat"], check=False, timeout=15)
                    time.sleep(0.6)
                    self._raise_target_window(command.get("selector"))
                    time.sleep(0.4)
                    return self._send(command, timeout)
                finally:
                    self._focus_retry_active = False
            raise

    def _raise_target_window(self, selector: str | None) -> None:
        """点目标窗口标题栏，把它变成 key window。

        macOS 上激活应用只保证 App 在前台；微信同时存在“微信”“微信 (窗口)”
        和“图片和视频”多个窗口时，截图目标仍可能不是前台窗口而被拒绝。
        """
        selector = str(selector or "")
        if not selector or self.transport._window_selector != selector:
            return
        bounds = self.transport._window_bounds
        if not bounds:
            return
        try:
            self.transport.run({"action": "click", "selector": selector,
                                "x": bounds[0] / 2, "y": 18}, timeout=25)
        except (RobotCommandError, RobotProtocolError, RobotTimeout):
            # 置前失败不改变原错误语义，仍由外层重试截图决定成败。
            pass

    def _ui_tars_trial_worker(self, task: dict, settings, profile: dict, output_dir: Path,
                             model: TrialModel, batch_sources: dict[str, str] | None = None) -> None:
        """一轮试验复用同一个 UiPath 实例，且只把结果写到试验目录。"""
        summary = None
        summary_path = output_dir / "batch-result.json"
        try:
            if not batch_sources:
                self._run_ui_tars_trial_case(task, settings, profile, output_dir, model)
                return
            summary = {"run_id": self.run_id, "status": "running", "results": [],
                       "unstarted_task_ids": list(batch_sources)}
            atomic_json(summary_path, summary)
            self._trace_attempt = self.run_directory.name
            with self._trace("uipath_startup"):
                self._await_cli_startup()
            for task_id, source in batch_sources.items():
                self._guard()
                task = self.store.get(task_id)
                case_dir = output_dir / task_id
                case_dir.mkdir(mode=0o700)
                case_settings = settings.model_copy(update={"input_dir": source})
                report = self._run_ui_tars_trial_case(task, case_settings, dict(profile), case_dir, model, automatic=True)
                summary["unstarted_task_ids"].pop(0)
                summary["results"].append(report | {"result_file": f"{task_id}/navigation-result.json"})
                self._guard()
                if report["wechat_return"]["status"] != "ready":
                    summary.update(status="blocked", reason=report["wechat_return"]["reason"])
                    break
                atomic_json(summary_path, summary)
            if summary["status"] == "running":
                summary["status"] = "completed"
        except RunnerTerminated:
            if summary is not None:
                summary.update(status="terminated", reason="已终止试验")
        except Exception as exc:
            if summary is not None:
                summary.update(status="blocked", reason=f"试验执行中断（{type(exc).__name__}）")
        finally:
            try:
                if summary is not None:
                    atomic_json(summary_path, summary)
            finally:
                try:
                    self.transport.request_stop()
                finally:
                    try:
                        self._reap_cli()
                    finally:
                        self._ui_tars_trial_active = False
                        self.current_task = None
                        self._deadline = 0

    def _run_ui_tars_trial_case(self, task: dict, settings, profile: dict, output_dir: Path,
                                model: TrialModel, *, automatic: bool = False) -> dict:
        self._trace_attempt = self.run_directory.name
        binding = self.store.material_binding(settings.input_dir) or {}
        self._trace_material = {"material_version_id": binding.get("id", ""),
                                "material_batch_id": binding.get("batch_id", ""),
                                "material_version": binding.get("version", ""),
                                "material_snapshot": binding.get("snapshot_id", "")}
        self._set_stage(task, "ui_tars_connecting", "准备 GUI Agent 试验")
        return self._run_gui_agent_case(task, settings, profile, output_dir, model,
                                        automatic=automatic, capture_dir=output_dir, trial=True)

    def _run_gui_agent_case(self, task: dict, settings, profile: dict, output_dir: Path,
                            model: TrialModel, *, automatic: bool, capture_dir: Path, trial: bool = False) -> dict:
        """正式任务与试验共用窗口准备、Agent 工具和菜单截图交接。"""
        self._trace_step = None
        self._trace_click = {}
        self._debug_frame_refs.clear()
        active = None
        safe_to_reset = True
        report = {
            "task_id": task["id"], "run_id": self.run_id, "source_name": task["source_name"],
            "model": GUI_PLUS_MODEL,
            "navigation": {"outcome": "needs_review", "reason": "导航未完成", "source": "runtime"},
            "capture": {"status": "not_started", "pages": []},
            # Agent 的业务终态与技术执行结果分开记录，避免把协议/窗口/截图故障当成门店判断。
            "agent_outcome": "", "agent_reason": "",
            "runtime_status": "pending", "runtime_code": "", "runtime_error": "",
            "capture_status": "not_started",
            "metrics": {},
        }
        started_at = time.monotonic()
        self._ipc_commands = 0
        navigation_started = capture_started = None
        timing = report["timing_ms"] = {}
        last_frame = None
        raw_decision = None
        agent_terminal = None
        trace_path = output_dir / "agent-calls.jsonl"
        host_log = output_dir / "agent-host.jsonl"

        def host_trace(kind: str, **fields) -> None:
            """记录宿主侧看到的截图、动作与窗口变化；写失败不影响导航。"""
            try:
                with host_log.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({
                        "kind": kind,
                        "at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        **fields,
                    }, ensure_ascii=False) + "\n")
            except OSError:
                pass

        try:
            report["material"] = self._trace_material.copy()
            self._guard()
            self._validate_source(task, settings.input_dir)
            if trial and not automatic:
                with self._trace("uipath_startup"):
                    self._await_cli_startup()
            self._deadline = time.monotonic() + settings.capture.max_task_seconds
            if automatic:
                self._startup_timing = timing
                self._open_source_automatic(task, profile, output_dir, settings.capture.settle_seconds)
                selector = profile["miniapp_selector"]
            else:
                # 宿主只负责发现窗口；多个候选时先接管一个，由 Agent 用
                # list_windows / switch_window 自行判断该操作哪个页面。
                candidates = self._miniapp_candidates(self._send({"action": "discover"}).get("windows"))
                if not candidates:
                    raise NeedsReview("未发现可操作的小程序窗口")
                selector = candidates[0]["selector"]
                host_trace("window_discovery", count=len(candidates),
                           titles=[item["title"] for item in candidates], selected=candidates[0]["title"])
            # 窗口准备与导航分离：先归位并截图校准尺寸，再启动 Agent 决策。
            active = {"selector": selector, "title": ""}
            self._set_stage(task, "ui_tars_navigation", "GUI Agent 正在进入菜单")
            evidence_dir = output_dir / "evidence"
            evidence_dir.mkdir(mode=0o700, exist_ok=True)
            self._prepare_trial_window(selector, evidence_dir, host_trace)

            def observe(sequence):
                nonlocal last_frame
                self._trace_step = sequence
                path = evidence_dir / f"ui-tars-{sequence:03}.png"
                # 每次观察使用当前窗口实测截图，后续动作与这一帧绑定。
                last_frame = self._send({"action": "screenshot", "selector": active["selector"], "path": str(path)})
                host_trace("frame", sequence=sequence, frame_id=str(sequence),
                           path=str(Path(last_frame["path"]).relative_to(output_dir)),
                           width=last_frame.get("width"), height=last_frame.get("height"),
                           window_width=last_frame.get("window_width"),
                           window_height=last_frame.get("window_height"),
                           selector=active["selector"])
                if sequence == 1:
                    report["initial_frame"] = str(Path(last_frame["path"]).relative_to(output_dir))
                return last_frame

            def execute(command, frame_id):
                return self._execute_trial_action(active, command, frame_id, host_trace)

            navigation_started = time.monotonic()
            timing["startup"] = round((navigation_started - started_at) * 1000)
            with self._trace("ui_tars_navigation", model=GUI_PLUS_MODEL) as trace:
                decision, frame = run_navigation(
                    model, task["source_name"], settings.people_count, settings.capture.max_agent_steps,
                    observe=observe, execute=execute, check_stop=self._guard,
                    log_path=output_dir / "adapter.log",
                    trace_path=trace_path,
                    trace_meta={"run_id": self.run_id, "task_id": task["id"],
                                "store_name": task["source_name"],
                                "batch": self._trace_material.get("material_batch_id", "")},
                    # 原始二维码照片提供品牌、桌号等背景，只作参考，不能当成可点击屏幕。
                    source_image=task.get("source_path"),
                )
                timing["navigation"] = round((time.monotonic() - navigation_started) * 1000)
                for field in ("reason",):
                    if field in decision:
                        decision[field] = decision[field].replace(model.api_key, "[已隐藏]")
                if isinstance(decision.get("diagnostic"), dict):
                    for field in ("code", "raw", "detail"):
                        value = decision["diagnostic"].get(field)
                        if isinstance(value, str):
                            decision["diagnostic"][field] = value.replace(model.api_key, "[已隐藏]")
                # 先留存 Agent 自己的判断：交接校验失败时仍能复盘原始决策，业务终态始终由 Agent 决定。
                raw_decision = {"outcome": decision["outcome"], "reason": decision["reason"]}
                # source=runtime 表示 Agent 没有作出有效业务终态；只有 Agent 自己给出的终态才写入顶层字段。
                agent_terminal = raw_decision if decision.get("source") != "runtime" else None
                if agent_terminal:
                    report["agent_outcome"] = agent_terminal["outcome"]
                    report["agent_reason"] = agent_terminal["reason"]
                if isinstance(decision.get("metrics"), dict):
                    report["metrics"] = decision["metrics"]
                if decision["outcome"] == "success":
                    # 校验必须发生在写入 navigation 之前：窗口/截图/区域不一致时整条结果降级为待检查。
                    self._validate_trial_handoff(decision, frame, active["selector"])
                report["navigation"] = decision
                if decision.get("source") == "runtime":
                    report["runtime_status"] = "failed"
                    report["runtime_code"] = str((decision.get("diagnostic") or {}).get("code") or "runtime")
                    report["runtime_error"] = decision["reason"]
                else:
                    report["runtime_status"] = "ok"
                if frame:
                    report["navigation"]["frame"] = str(Path(frame["path"]).relative_to(output_dir))
                    trace["frame_evidence"] = self._debug_frame_ref(Path(frame["path"]))
                if active["title"]:
                    report["navigation"]["window"] = active["title"]
                trace["outcome"] = decision["outcome"]
                if decision.get("diagnostic"):
                    trace["diagnostic"] = decision["diagnostic"]

            if decision["outcome"] == "success":
                self._set_stage(task, "capturing", "正在滚动截图；完整覆盖率留待复核")
                capture_started = time.monotonic()
                (capture_dir / "evidence").mkdir(mode=0o700, exist_ok=True)
                pages = self._capture_menu(active["selector"], frame, Region(**decision["menu_region"]),
                                           settings, profile, capture_dir)
                # 正常菜单滚动一次就会产生第二屏；只有 1–2 张多半是停在前置页（欢迎页/人数选择）的假成功。
                if len(pages) <= 2:
                    raise NeedsReview(
                        f"滚动截图只得到 {len(pages)} 张，疑似尚未进入可滚动的菜品菜单；已转人工复核",
                        "menu_too_few_pages")
                report["capture"] = {"status": "staged", "pages": pages}
        except (TrialProtocolError, NeedsReview, RunnerTerminated, RunnerPaused, RobotProtocolError,
                RobotCommandError, RobotTimeout) as exc:
            safe_to_reset = not isinstance(exc, (RobotTimeout, RunnerTerminated, RunnerPaused))
            reason = str(exc).replace(model.api_key, "[已隐藏]")[:500]
            report["runtime_status"] = "failed"
            report["runtime_error"] = reason
            if report["navigation"]["outcome"] == "success":
                report["capture"] = {"status": "needs_review", "reason": reason,
                                     "pages": sorted(path.name for path in capture_dir.glob("page-*.png"))}
                report["runtime_code"] = "capture"
            else:
                report["navigation"] = {"outcome": "needs_review", "reason": reason, "source": "runtime"}
                if agent_terminal:
                    # 交接校验/运行异常造成的降级仍保留 Agent 自己的判断，便于复盘。
                    report["navigation"].update(agent_outcome=agent_terminal["outcome"],
                                                agent_reason=agent_terminal["reason"])
                if last_frame:
                    report["navigation"]["frame"] = str(Path(last_frame["path"]).relative_to(output_dir))
                report["runtime_code"] = report["runtime_code"] or "runtime"
            if not trial:
                raise
        except Exception as exc:
            result = report["capture"] if report["navigation"]["outcome"] == "success" else report["navigation"]
            result.update(reason=f"运行失败（{type(exc).__name__}）")
            if result is report["capture"]:
                result.update(status="needs_review", pages=sorted(path.name for path in capture_dir.glob("page-*.png")))
                report["runtime_code"] = "capture"
            else:
                report["runtime_code"] = report["runtime_code"] or "runtime"
            report["runtime_status"] = "failed"
            report["runtime_error"] = str(result.get("reason") or "")
            if not trial:
                raise
        finally:
            end = time.monotonic()
            if navigation_started is not None:
                timing.setdefault("navigation", round((end - navigation_started) * 1000))
            if capture_started is not None:
                timing["capture"] = round((end - capture_started) * 1000)
            if automatic and active:
                # Agent 切换过窗口时，批次收尾从最后接管的窗口退出。
                profile["miniapp_selector"] = active["selector"]
            if automatic and trial:
                self._deadline = time.monotonic() + 60
                try:
                    if not safe_to_reset:
                        raise NeedsReview("UiPath 操作未确认结束，未执行桌面清理")
                    self._guard()
                    self._return_to_wechat(profile, output_dir, settings.capture.settle_seconds)
                    report["wechat_return"] = {"status": "ready"}
                except Exception as exc:
                    report["wechat_return"] = {
                        "status": "blocked",
                        "reason": f"未确认安全返回文件传输助手（{type(exc).__name__}）",
                    }
            if trial:
                self._deadline = 0
            report["capture_status"] = str(report["capture"].get("status") or "not_started")
            timing["total"] = round((time.monotonic() - started_at) * 1000)
            trace_files = {name: path.name for name, path in
                           (("model_calls", trace_path), ("host_log", host_log)) if path.is_file()}
            if trace_files:
                report["trace"] = trace_files
            try:
                recorded = self._record_agent_usage(trace_path, task, model)
                if recorded:
                    report["usage"] = recorded
            except Exception as exc:
                # 用量入库失败不能改写导航与采集结论，但必须在报告里可见。
                report["usage_error"] = f"{type(exc).__name__}: {str(exc)[:300]}".replace(model.api_key, "[已隐藏]")
            try:
                atomic_json(output_dir / "navigation-result.json", report)
            except OSError:
                if trial:
                    raise
                self.store.event("无法保存 GUI Agent 导航报告", "error", task["id"])
        return report

    def _worker(self, initial_task: dict, settings, profile: dict, initial_output: Path,
                model: TrialModel, selected_task_ids: list[str] | None = None, start_index: int = 0,
                resumed: bool = False) -> None:
        task, output_dir = initial_task, initial_output
        self._trace_attempt = initial_output.name
        self._trace_step = None
        self._debug_frame_refs.clear()
        run_binding = self.store.get_run(self.run_id) or {}
        self._trace_material = {key: run_binding.get(key, "") for key in (
            "material_batch_id", "material_version_id", "material_version", "material_snapshot"
        )}
        run_status = "blocked"
        run_message = "批次启动失败；请检查错误详情"
        try:
            self._set_stage(task, "connecting", "等待 UiPath Main 响应 IPC noop 命令")
            with self._trace("uipath_startup"):
                self._await_cli_startup()
            task_ids = selected_task_ids if selected_task_ids is not None else [initial_task["id"]]
            if resumed and profile["mode"] == "automatic":
                profile["miniapp_selector"] = ""
                try:
                    self._ensure_file_assistant(profile, output_dir, settings.capture.settle_seconds)
                except Exception as exc:
                    run_message = f"恢复前未能确认微信已回到文件传输助手（{type(exc).__name__}）；批次保持阻塞"
                    self._guard()
                    self._persist_task_result(task, output_dir, "needs_review", "resume_check", run_message)
                    self.store.update_run(self.run_id, status="blocked", message=run_message)
                    return
            if resumed:
                self._guard()
                if not self.pause_event.is_set():
                    self.store.update_run(self.run_id, status="running", message="恢复检查通过，继续采集")
            run_status = "completed"
            run_message = "批次处理结束，请在门店列表查看采集结果"
            for batch_index, selected_id in enumerate(task_ids):
                self._guard()
                absolute_index = start_index + batch_index
                if batch_index:
                    # 严格按用户勾选顺序处理；上一轮只有安全关闭小程序后才会走到这里。
                    task = self.store.get(selected_id)
                    attempt = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8]
                    output_dir = self._staging_dir(settings.output_dir, task, attempt)
                    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
                    output_dir.chmod(0o700)
                    self.store.update(task["id"], status="running", stage="waiting_uipath", message="等待当前 UiPath Main 处理批次任务")
                    self.current_task = {"id": task["id"], "source_name": task["source_name"], "stage": "waiting_uipath", "mode": profile["mode"]}
                self._trace_attempt = output_dir.name
                self._trace_step = None
                self._debug_frame_refs.clear()
                self.store.update_run(self.run_id, current_index=absolute_index,
                                      current_task_id=task["id"], message=f"正在处理第 {absolute_index + 1} 家：{task['source_name']}")
                if profile["mode"] == "automatic":
                    profile["miniapp_selector"] = ""
                self._deadline = time.monotonic() + settings.capture.max_task_seconds
                status, stage, message = "needs_review", "uipath_error", "执行未完成，需人工检查"
                failure_code = ""
                safe_to_continue = False
                terminated = False
                try:
                    with self._trace("task_execution"):
                        self._run_task(task, settings, profile, output_dir, model)
                    if self.stop_event.is_set():
                        raise RunnerTerminated("用户已终止批次；当前门店结果不发布")
                    status, stage, message, failure_code = "captured", "captured", "已保存菜单截图；覆盖完整性待复核", ""
                except AgentUnqualified as exc:
                    stage, message, failure_code = "agent_unqualified", str(exc)[:500], "agent_unqualified"
                except RunnerTerminated as exc:
                    stage, message, terminated, failure_code = "terminated", str(exc), True, "terminated"
                except RunnerPaused as exc:
                    stage, message, failure_code = "paused", str(exc), "paused"
                except RobotTimeout as exc:
                    stage, message, failure_code = "uipath_timeout", str(exc), "uipath_timeout"
                except (NeedsReview, TrialProtocolError, RobotProtocolError, RobotCommandError, ValidationError) as exc:
                    stage, message = "needs_review", str(exc).replace(model.api_key, "[已隐藏]")[:500]
                    failure_code = getattr(exc, "code", "") or ("agent_runtime" if isinstance(exc, TrialProtocolError) else "")
                except Exception as exc:
                    stage, message = "needs_review", f"{type(exc).__name__}；结果待人工检查"
                    failure_code = ""
                if status == "captured":
                    try:
                        with self._trace("publish_output"):
                            self._publish_store_output(settings.output_dir, task, output_dir,
                                                       batch_id=run_binding.get("material_batch_id", ""), message=message)
                    except Exception as exc:
                        status, stage = "needs_review", "output_publish_error"
                        message = f"截图完成但尚未发布成功（{type(exc).__name__}）；旧结果已保留"
                        self._persist_task_result(task, output_dir, status, stage, message, "output_publish_error")
                        # 截图阶段已按 automatic 规则确认回到文件传输助手；文件发布故障不影响微信起点。
                        safe_to_continue = True
                    else:
                        self.store.event(message, task_id=task["id"])
                else:
                    self._persist_task_result(task, output_dir, status, stage, message, failure_code)
                    if profile["mode"] == "automatic":
                        self._deadline = 0
                        resume_safe = not terminated and stage not in {"paused", "uipath_timeout"}
                        try:
                            if resume_safe:
                                self._return_to_wechat(profile, output_dir, settings.capture.settle_seconds)
                                safe_to_continue = True
                        except RunnerTerminated as exc:
                            stage, message, terminated = "terminated", str(exc), True
                            self._persist_task_result(task, output_dir, status, stage, message, "terminated")
                        except Exception as exc:
                            if resume_safe:
                                stage = "blocked_not_safe"
                                message = f"{message}；微信安全起点确认失败（{type(exc).__name__}），批次已阻塞"
                                self._persist_task_result(task, output_dir, status, stage, message, failure_code)
                                run_status, run_message = "blocked", message
                with self._trace("task_result", status=status if stage not in {"paused", "terminated"} else stage,
                                 stage=stage, message=message):
                    pass
                self._deadline = 0
                if status == "captured" or safe_to_continue:
                    next_index = absolute_index + 1
                    self.store.update_run(self.run_id, current_index=next_index,
                                          current_task_id=task_ids[batch_index + 1] if batch_index + 1 < len(task_ids) else "")
                else:
                    self.store.update_run(self.run_id, current_index=absolute_index, current_task_id=task["id"])
                if output_dir.exists():
                    shutil.rmtree(output_dir, ignore_errors=True)
                if terminated:
                    run_status, run_message = "terminated", "批次已终止；当前门店需检查，未发布部分截图，后续门店仍待处理"
                    self.store.update_run(self.run_id, status=run_status, current_index=absolute_index,
                                          current_task_id=task["id"], message=run_message)
                    break
                if status != "captured" and not safe_to_continue:
                    if run_status != "blocked":
                        run_status, run_message = "blocked", "当前门店未能安全完成；批次已阻塞"
                        self.store.update_run(self.run_id, status=run_status, current_index=absolute_index,
                                              current_task_id=task["id"], message=run_message)
                    break
                if self.pause_event.is_set() and absolute_index + 1 < start_index + len(task_ids):
                    run_status, run_message = "paused", "当前门店已处理，下一家尚未开始"
                    self.store.update_run(self.run_id, status=run_status, current_index=absolute_index + 1,
                                          current_task_id=task_ids[batch_index + 1], message=run_message)
                    self.store.event(run_message, task_id=task["id"])
                    break
        except RunnerTerminated:
            if self.store.get(task["id"])["status"] == "running":
                self._persist_task_result(task, output_dir, "needs_review", "terminated", "本轮采集已终止")
            run_status, run_message = "terminated", "批次已终止，后续门店未执行"
        except RunnerPaused as exc:
            self._persist_task_result(task, output_dir, "needs_review", "paused", str(exc))
            run_status, run_message = "blocked", "自动化在门店边界外停止；当前门店需检查后再继续"
        except RobotTimeout as exc:
            self._persist_task_result(task, output_dir, "needs_review", "uipath_timeout", str(exc))
            run_status, run_message = "blocked", "UiPath 命令结果未知；批次已阻塞"
        except RobotCommandError as exc:
            self._persist_task_result(task, output_dir, "needs_review", "uipath_error", str(exc)[:500])
            run_status, run_message = "blocked", "UiPath 执行失败；批次已阻塞"
        except Exception as exc:
            self._persist_task_result(task, output_dir, "needs_review", "uipath_error", f"{type(exc).__name__}；结果待人工检查")
            run_status, run_message = "blocked", "批次启动或执行失败；请查看当前门店详情"
        finally:
            if self.run_id:
                current = self.store.get_run(self.run_id)
                if current and current["status"] not in {"paused", "blocked", "terminated"}:
                    self.store.update_run(self.run_id, status=run_status, message=run_message,
                                          current_task_id="" if run_status == "completed" else current.get("current_task_id", ""))
            if output_dir.exists():
                shutil.rmtree(output_dir, ignore_errors=True)
            if self.transport is not None:
                try:
                    # Main 等待下一条 IPC 时用 stop 正常退出；该消息不触发桌面动作或重发。
                    self.transport.request_stop()
                except Exception:
                    pass
            self._reap_cli()
            self.current_task = None
            # 运行证据与调试日志共用容量上限；先删最早的成功门店，失败门店最后才动。
            try:
                self._prune_run_evidence(settings.output_dir, settings.debug_logging.max_storage_mb)
            except Exception:
                pass
            # 动作明细按容量滚动，SQLite 也只保留有限索引，避免长期双写和无限增长。
            try:
                self.store.trim_action_history()
            except Exception:
                pass
            self._deadline = 0

    def _ensure_wechat_window(self) -> bool:
        """微信主窗口被关掉后，所有 selector 都会报“找不到目标应用程序”；
        先用系统命令把主窗口唤回，避免整批被环境问题卡死。"""
        def has_main(windows: Any) -> bool:
            titles = {"微信", "wechat", "weixin"} if sys.platform == "win32" else {"微信", "wechat"}
            return any(str(item.get("title") or "").strip().casefold() in titles
                       for item in self._window_list(windows))

        try:
            if has_main(self._send({"action": "discover"}).get("windows")):
                return True
            if sys.platform == "win32":
                return False
            subprocess.run(["open", "-a", "WeChat"], check=False, timeout=15)
            time.sleep(2.5)
            return has_main(self._send({"action": "discover"}).get("windows"))
        except (RobotTimeout, RunnerPaused, RunnerTerminated):
            raise
        except Exception:
            return False

    def _ensure_file_assistant(self, profile: dict, output_dir: Path, settle_seconds: float) -> None:
        """恢复或失败续跑前用微信当前画面确认安全起点，不依赖进程存在状态。"""
        self._guard()
        self._ensure_wechat_window()
        if profile.get("miniapp_selector"):
            self._close_to_file_assistant(profile, output_dir, settle_seconds)
            return
        frame = self._screenshot(output_dir, profile["wechat_selector"], "wechat-safe-start.png")
        confirmed = self._read_assistant_context(frame, "resume_safe_start")
        if confirmed:
            return
        if sys.platform == "win32":
            raise NeedsReview("未确认微信已回到文件传输助手，请手动切回后继续")
        windows = self._window_list(self._send({"action": "discover"}).get("windows"))
        if confirmed is None:
            # OCR 不可用时不能核对聊天标题：先收起残留小程序窗口，再按微信主窗口继续。
            self._close_miniapp_windows(profile, output_dir)
            return
        profile["miniapp_selector"] = self._select_miniapp_selector(windows)
        self._close_to_file_assistant(profile, output_dir, settle_seconds)

    def _reap_cli(self) -> None:
        """有界回收本控制器创建的 CLI PID；不查找或终止 Studio/其他进程。"""
        process = self._cli_process
        if process is None:
            return
        status = "exited"
        try:
            try:
                code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                status = "terminated"
                process.terminate()
                try:
                    code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    status = "killed"
                    process.kill()
                    code = process.wait(timeout=5)
            self._cli_info.update(exit_code=int(code) if code is not None else process.poll(), status=status)
        except Exception as exc:
            self._cli_info.update(exit_code=process.poll(), status=f"reap_error:{type(exc).__name__}")
        finally:
            reader = self._cli_reader_thread
            if reader is not None:
                reader.join(timeout=2)
            if self.debug_log and self.run_id:
                self.debug_log.finish_cli_output(self.run_id)
            self._write_cli_metadata()

    def _run_task(self, task: dict, settings, profile: dict, output_dir: Path, model: TrialModel) -> None:
        # 证据留在本次私有运行目录；发布事务只接收连续编号的菜单页。
        evidence_dir = self.run_directory / "gui-agent" / output_dir.name
        evidence_dir.mkdir(parents=True, mode=0o700)
        automatic = profile["mode"] == "automatic"
        report = self._run_gui_agent_case(task, settings, profile, evidence_dir, model,
                                          automatic=automatic, capture_dir=output_dir)
        self._guard()
        if report["runtime_status"] != "ok":
            raise NeedsReview(report["runtime_error"], "agent_runtime")
        if report["agent_outcome"] == "unqualified":
            raise AgentUnqualified(report["agent_reason"])
        if report["agent_outcome"] == "needs_review":
            raise NeedsReview(report["agent_reason"], "agent_needs_review")
        if report["capture_status"] != "staged":
            raise NeedsReview("GUI Agent 未交接可发布的菜单截图", "agent_runtime")
        if automatic:
            self._return_to_wechat(profile, output_dir, settings.capture.settle_seconds)
        # 成功门店的过程截图不立即删除，保留一天供复核；由 _prune_run_evidence 统一按时间与容量回收。

    @staticmethod
    def _trim_success_evidence(evidence_dir: Path, keep: str = "") -> None:
        """成功门店一天后只留导航结果、Agent 日志与交接帧，删掉其余过程截图。"""
        try:
            for path in evidence_dir.glob("*.png"):
                if keep and path.name == keep:
                    continue
                path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _case_succeeded(case_dir: Path) -> bool:
        try:
            report = json.loads((case_dir / "navigation-result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (report.get("agent_outcome") == "success"
                and (report.get("capture") or {}).get("status") == "staged")

    @staticmethod
    def _case_frame_name(case_dir: Path) -> str:
        try:
            report = json.loads((case_dir / "navigation-result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return Path((report.get("navigation") or {}).get("frame") or "").name

    @staticmethod
    def _case_anchor(case_dir: Path) -> float | None:
        """以导航结果落盘时间为准判断门店龄期；目录 mtime 会随清理变动。"""
        try:
            return (case_dir / "navigation-result.json").stat().st_mtime
        except OSError:
            try:
                return case_dir.stat().st_mtime
            except OSError:
                return None

    @staticmethod
    def _directory_size(directory: Path) -> int:
        return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())

    def _prune_run_evidence(self, output_root: str | Path, budget_mb: int) -> None:
        """按时间和容量回收历史运行证据。

        成功门店保留一天后只留简要记录；失败门店最多留两天，超期整体删除；
        存储超上限时从最早的门店开始删，成功门店优先，仍不够再删失败门店。
        """
        root = Path(output_root).expanduser().resolve() / ".uipath-runs"
        if not root.is_dir():
            return
        now = time.time()
        budget = max(64, int(budget_mb)) * 1024 * 1024
        cases: list[tuple[float, Path, bool, int]] = []
        for run in root.iterdir():
            if not run.is_dir():
                continue
            folders = list((run / "gui-agent").glob("*")) + list((run / "ui-tars-trial").glob("*"))
            for case in folders:
                if not case.is_dir():
                    continue
                anchor = self._case_anchor(case)
                if anchor is None:
                    continue
                succeeded = self._case_succeeded(case)
                if succeeded:
                    if now - anchor >= 24 * 3600:
                        self._trim_success_evidence(case, keep=self._case_frame_name(case))
                elif now - anchor >= 2 * 24 * 3600:
                    shutil.rmtree(case, ignore_errors=True)
                    continue
                cases.append((anchor, case, succeeded, self._directory_size(case)))
        total = sum(size for _anchor, _case, _succeeded, size in cases)
        if total <= budget:
            return
        for succeeded_group in (True, False):
            for _anchor, case, succeeded, size in sorted(cases):
                if total <= budget:
                    return
                if succeeded != succeeded_group:
                    continue
                shutil.rmtree(case, ignore_errors=True)
                total -= size

    def _return_to_wechat(self, profile: dict, output_dir: Path, settle_seconds: float) -> None:
        self._ensure_file_assistant(profile, output_dir, settle_seconds)
        self._close_miniapp_windows(profile, output_dir)
        if self._miniapp_candidates(self._send({"action": "discover"}).get("windows")):
            raise NeedsReview("仍有小程序窗口未关闭，未确认安全返回微信", "wechat_context_lost")

    def _persist_task_result(self, task: dict, output_dir: Path, status: str, stage: str, message: str,
                             code: str = "") -> None:
        code = code or classify_failure(stage, message)
        # Agent 的原因是给人阅读的证据；只在收到明确业务终态时创建不合格记录。
        reject = {}
        reject_reason = "other" if code == "agent_unqualified" else ""
        if reject_reason:
            reject = {
                "reject_reason": reject_reason,
                "reject_detail": message[:200],
                "rejected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        try:
            self.store.update(task["id"], status=status, stage=stage, message=message,
                              error_code=code, **reject)
            atomic_json(output_dir / "result.json", {"task_id": task["id"], "status": status, "stage": stage,
                                                    "message": message, "error_code": code,
                                                    "reject_reason": reject_reason})
            self.store.event(message, "warning" if status != "captured" else "info", task["id"])
        except Exception:
            self.store.event("无法持久化 UiPath 运行结果；请检查当前任务输出目录", "error", task["id"])

    def _set_stage(self, task: dict, stage: str, message: str) -> None:
        if self._ui_tars_trial_active:
            mode = (self.current_task or {}).get("mode", "attached")
            self.current_task = {"id": task["id"], "source_name": task["source_name"], "stage": stage,
                                 "mode": mode, "navigation": "gui_agent", "trial": True}
            return
        if getattr(self, "_annotation_mode", False):
            # 人工标注只借用打开流程，不能把门店写成“自动采集中”。
            self.current_task = {"id": task["id"], "source_name": task["source_name"], "stage": stage, "mode": "annotation"}
            return
        self.current_task = {"id": task["id"], "source_name": task["source_name"], "stage": stage,
                             "mode": (self.current_task or {}).get("mode", "attached"), "navigation": "gui_agent"}
        self.store.update(task["id"], status="running", stage=stage, message=message)
        self.store.event(message, task_id=task["id"])

    def _validate_source(self, task: dict, input_dir: str) -> Path:
        if not input_dir:
            raise NeedsReview("请先选择用于采集的发布版本")
        root = Path(input_dir).expanduser().resolve()
        if (root / ".materials-snapshot.json").is_file() and self.store.material_binding(input_dir) is None:
            raise NeedsReview("素材版本已移入回收站或尚未发布，请重新选择采集版本")
        source = Path(task["source_path"]).expanduser().resolve()
        if not source.is_file() or not _inside(source, root):
            raise NeedsReview("来源文件不在当前输入目录内")
        if hashlib.sha256(source.read_bytes()).hexdigest() != task["source_hash"]:
            raise NeedsReview("来源文件在扫描后有变化，请重新扫描")
        return source

    def _store_output_dir(self, output_root: str | Path, task: dict, batch_id: str = "",
                          batch_name: str = "") -> Path:
        # 目录使用用户起的批次名称；只有拿不到名称（历史数据）时才回退到批次 ID。
        name = batch_name or self.store.material_batch_name(batch_id)
        return store_output_dir(output_root, task["source_name"], batch_id, name)

    @staticmethod
    def _staging_dir(output_root: str | Path, task: dict, attempt: str) -> Path:
        root = Path(output_root).expanduser().resolve()
        staging = root / ".uipath-staging" / task["id"] / attempt
        if not _inside(staging, root):
            raise ValueError("自动采集暂存路径无效")
        return staging

    def _publish_store_output(self, output_root: str | Path, task: dict, staging: Path, *, batch_id: str = "",
                              message: str = "已保存菜单截图；覆盖完整性待复核") -> Path:
        """以 SQLite 发布记录保护目录替换；提交状态前旧目录始终可恢复。"""
        root = Path(output_root).expanduser().resolve()
        staging = Path(staging).expanduser().resolve()
        staging_root = root / ".uipath-staging"
        destination = self._store_output_dir(root, task, batch_id)
        if not _inside(staging, staging_root) or not staging.is_dir():
            raise ValueError("自动采集暂存目录不存在或越界")
        if destination.is_symlink():
            raise ValueError("门店输出目录不能是符号链接")
        destination.parent.mkdir(parents=True, exist_ok=True)

        pages = [item for item in staging.iterdir() if item.is_file() and re.fullmatch(r"page-\d{4}\.png", item.name)]
        if not pages:
            raise ValueError("暂存目录中没有菜单页截图")
        page_numbers = sorted(int(item.stem.split("-")[1]) for item in pages)
        if page_numbers != list(range(1, len(pages) + 1)):
            raise ValueError("菜单页截图编号不连续，不能发布")
        for page in pages:
            try:
                with Image.open(page) as image:
                    image.verify()
            except Exception:
                raise ValueError(f"菜单页截图文件无效：{page.name}") from None
        for item in list(staging.iterdir()):
            if item.is_file() and re.fullmatch(r"page-\d{4}\.png", item.name):
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()

        backup = staging_root / ("previous-" + uuid.uuid4().hex)
        # 查询接口同锁读取，避免目录刚替换、旧任务路径尚未解绑时看到其他二维码版本。
        with self.output_lock:
            had_previous = destination.exists()
            if had_previous and not destination.is_dir():
                raise ValueError("门店输出路径已存在且不是文件夹")
            self.store.begin_publish(task["id"], root, staging, destination, backup, had_previous)
            try:
                if had_previous:
                    os.replace(destination, backup)
                self.store.update_publish(task["id"], "old_moved")
                os.replace(staging, destination)
                self.store.update_publish(task["id"], "new_moved")
                self.store.complete_capture(task["id"], destination, message)
            except Exception:
                try:
                    if destination.exists() and not staging.exists():
                        os.replace(destination, staging)
                    if had_previous and backup.exists() and not destination.exists():
                        os.replace(backup, destination)
                    self.store.finish_publish(task["id"])
                except Exception:
                    # 事务记录保留，服务启动时按同一条记录恢复。
                    pass
                raise
        if had_previous and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        return destination

    def recover_publishes(self) -> None:
        """服务重启后完成已提交发布的清理，或把未提交的新目录回滚到旧结果。"""
        for transaction in self.store.publish_transactions():
            task_id = transaction["task_id"]
            root = Path(transaction["output_root"]).expanduser().resolve()
            staging = Path(transaction["staging"]).expanduser().resolve()
            destination = Path(transaction["destination"]).expanduser().resolve()
            backup = Path(transaction["backup"]).expanduser().resolve()
            staging_root = root / ".uipath-staging"
            if (not _inside(destination, root) or destination == root or
                    not _inside(staging, staging_root) or not _inside(backup, staging_root)):
                self.store.event("发现越界的截图发布恢复记录，未触碰本地目录", "error", task_id)
                continue
            try:
                committed = transaction["phase"] == "new_moved" and destination.is_dir()
                task = self.store.get(task_id)
                if committed:
                    self.store.complete_capture(task_id, destination, "已完成截图发布（服务恢复后确认）")
                    if backup.exists():
                        shutil.rmtree(backup) if backup.is_dir() and not backup.is_symlink() else backup.unlink()
                else:
                    if destination.exists() and not staging.exists() and transaction["phase"] in {"old_moved", "new_moved"}:
                        os.replace(destination, staging)
                    if transaction["had_previous"] and backup.exists() and not destination.exists():
                        os.replace(backup, destination)
                    if task["status"] == "captured" and not committed:
                        self.store.update(task_id, status="needs_review", stage="output_recovery",
                                          message="发布未能确认提交；旧截图已恢复，请检查后重试")
                    self.store.finish_publish(task_id)
                self.store.event("截图发布事务已恢复", task_id=task_id)
            except Exception as exc:
                self.store.event(f"截图发布恢复失败（{type(exc).__name__}）；保留旧结果和恢复记录", "error", task_id)

    def _screenshot(self, output_dir: Path, selector: str, name: str, *, desktop: bool = False,
                    allow_fast: bool = True) -> dict:
        path = (output_dir / "evidence" / name).resolve()
        if not _inside(path, output_dir):
            raise RobotProtocolError("截图路径超出本任务目录")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not desktop and allow_fast:
            # 同一个窗口刚截过、尺寸已确认时，按矩形直取比再走一次 UiPath 窗口截图快数秒。
            fast = self._fast_window_capture(output_dir, selector, name)
            if fast is not None:
                return fast
        command = {
            "action": "screenshot_desktop" if desktop else "screenshot",
            "selector": selector,
            "path": str(path),
        }
        try:
            reply = self._send(command)
        except RobotCommandError as exc:
            # macOS 置前是异步的；首个窗口动作后的连续截图可能瞬间读到 foreground=false。
            # 截图是只读动作，允许延迟后重发一次，避免把焦点竞态误判为门店打开失败。
            if "foreground=false" not in str(exc):
                raise
            time.sleep(0.4)
            reply = self._send(dict(command))
        reply["path"] = str(Path(reply["path"]).resolve())
        reply["capture_scope"] = "desktop" if desktop else "window"
        return reply

    def _fast_window_capture(self, output_dir: Path, selector: str, name: str) -> dict | None:
        """按点击时刚校验过的窗口矩形直接截屏，省掉 UiPath 的窗口定位与重拍。

        取图由 UiPath 调用 screencapture 完成（它已有屏幕录制权限）；矩形只用在
        刚通过窗口尺寸校验的那次动作之后，尺寸/缩放比例异常时一律放弃并退回 UiPath 截图。
        """
        if sys.platform == "win32" or not getattr(self, "_fast_capture_enabled", True):
            return None
        transport = getattr(self, "transport", None)
        # 矩形只对最近一次校验过的那个窗口有效；换了窗口就必须重新走完整截图。
        if getattr(transport, "_window_selector", None) != selector:
            return None
        origin = getattr(transport, "_window_origin", None)
        bounds = getattr(transport, "_window_bounds", None)
        pixels = getattr(transport, "_window_pixels", None)
        if not origin or not bounds or not pixels or min(bounds) <= 0 or min(pixels) <= 0:
            return None
        width, height = bounds
        ratio_x, ratio_y = pixels[0] / width, pixels[1] / height
        if not (0.5 <= ratio_x <= 4 and 0.5 <= ratio_y <= 4):
            return None
        target = (Path(output_dir) / "evidence" / name).resolve()
        if not _inside(target, output_dir):
            return None
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            reply = self._send({
                "action": "capture_rect",
                "selector": selector,
                "x": int(round(origin[0])),
                "y": int(round(origin[1])),
                "width": int(round(width)),
                "height": int(round(height)),
                "path": str(target),
            })
        except (RobotCommandError, RobotTimeout, RobotProtocolError):
            self._fast_capture_enabled = False
            return None
        shot_path = Path(reply.get("path") or target)
        if not shot_path.exists():
            return None
        try:
            with Image.open(shot_path) as shot:
                shot_width, shot_height = shot.size
                stats = ImageStat.Stat(shot.convert("L"))
                unusable = stats.mean[0] < 3 and stats.stddev[0] < 1.5
        except (OSError, ValueError):
            return None
        if unusable or abs(shot_width - round(width * ratio_x)) > 3 \
                or abs(shot_height - round(height * ratio_y)) > 3:
            return None
        return {"path": str(shot_path.resolve()), "width": shot_width, "height": shot_height,
                "window_x": origin[0], "window_y": origin[1],
                "window_width": width, "window_height": height, "capture_scope": "window"}

    @classmethod
    def _miniapp_candidates(cls, windows: Any) -> list[dict]:
        """筛出微信里独立的小程序窗口；主窗口、图片预览和聊天记录都不算。"""
        if isinstance(windows, str):
            try:
                windows = json.loads(windows)
            except ValueError:
                windows = []
        if not isinstance(windows, list):
            raise NeedsReview("UiPath 未返回窗口列表")
        candidates = []
        for item in windows:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if sys.platform == "win32" and (
                not is_windows_wechat_app(str(item.get("app") or ""))
                or title.casefold().startswith("weixin")
            ):
                continue
            role = str(item.get("role") or "").strip()
            rect = item.get("position") or {}
            if not isinstance(rect, dict) or not title or title.casefold() in {"微信", "wechat"}:
                continue
            # 主窗口标题偶尔带后缀（如“微信 (3)”），绝不能把它当小程序关掉；
            # 但微信把独立小程序窗口标题成“微信 (窗口)”，它属于小程序窗口。
            if title.casefold().startswith("wechat"):
                continue
            if "微信" in title and "窗口" not in title:
                continue
            if "聊天记录" in title or title in {"图片和视频", "文件传输助手"}:
                continue
            width, height = rect.get("width", 0), rect.get("height", 0)
            if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 100 and height > 200:
                candidates.append((item, title, role, width, height))
        narrow = [candidate for candidate in candidates if candidate[3] < candidate[4] * 0.8]
        if narrow:
            candidates = narrow
        result = []
        for item, title, role, _, _ in candidates:
            result.append({
                "selector": window_selector(item, title),
                "title": title,
                "position": item.get("position"),
            })
        return result

    @classmethod
    def _select_miniapp_selector(cls, windows: Any) -> str:
        candidates = cls._miniapp_candidates(windows)
        if len(candidates) != 1:
            raise NeedsReview("未能唯一确定当前小程序窗口，请仅保留要采集的小程序窗口")
        return candidates[0]["selector"]

    def _switch_trial_window(self, active: dict, title: str) -> dict:
        """Agent 指定窗口标题后切换；找不到或同名不唯一时只回告，不替它猜。"""
        candidates = self._miniapp_candidates(self._send({"action": "discover"}).get("windows"))
        titles = [item["title"] for item in candidates]
        matches = [item for item in candidates if item["title"] == title]
        if len(matches) != 1:
            error = "目标窗口不存在" if not matches else "同名窗口不唯一"
            return {"switched": False, "error": error, "titles": titles}
        active["selector"] = matches[0]["selector"]
        active["title"] = title
        # 新窗口可能停留在屏幕外；切过去先按采集布局归位，随后由 Agent 触发的新截图确认。
        self._move_window_on_screen(active["selector"])
        return {"switched": True, "title": title, "titles": titles}

    def _execute_trial_action(self, active: dict, command: dict, frame_id: str, host_trace) -> dict:
        """执行 Agent 的一个动作；无论 UiPath 成功还是抛异常，宿主侧都要留一条记录。"""
        action = command.get("action")
        started = time.monotonic()
        params = {key: value for key, value in command.items() if key != "selector"}
        try:
            if action == "windows":
                candidates = self._miniapp_candidates(
                    self._send({"action": "discover"}).get("windows"))
                result = {"windows": [{"title": item["title"]} for item in candidates]}
            elif action == "switch_window":
                result = self._switch_trial_window(active, str(command.get("title") or ""))
            else:
                self._trace_click = {"frame_id": frame_id, "source": "gui_agent"}
                try:
                    self._send({**command, "selector": active["selector"]})
                finally:
                    self._trace_click = {}
                result = {}
        except Exception as exc:
            host_trace("tool", frame_id=frame_id, action=action, params=params, ok=False,
                       error=f"{type(exc).__name__}: {str(exc)[:300]}",
                       duration_ms=round((time.monotonic() - started) * 1000))
            raise
        host_trace("tool", frame_id=frame_id, action=action, params=params, ok=True,
                   result=result, duration_ms=round((time.monotonic() - started) * 1000))
        return result

    def _prepare_trial_window(self, selector: str, evidence_dir: Path, host_trace=None) -> None:
        """Agent 启动前先归位窗口，再用一次实测截图校准截图像素与窗口坐标的比例。"""
        if sys.platform == "win32":
            windows = self._send({"action": "discover"}).get("windows")
            if sum(item["selector"] == selector for item in self._miniapp_candidates(windows)) != 1:
                raise NeedsReview("Windows 无法唯一确认目标小程序窗口；未发送操作")
        self._move_window_on_screen(selector)
        frame = self._send({"action": "screenshot", "selector": selector,
                            "path": str(evidence_dir / "window-prepare.png")})
        width, height = frame.get("width"), frame.get("height")
        window_width, window_height = frame.get("window_width"), frame.get("window_height")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
                   for value in (width, height, window_width, window_height)):
            raise NeedsReview("窗口截图缺少有效尺寸，无法校准 GUI Agent 坐标")
        scale_x = float(width) / float(window_width)
        scale_y = float(height) / float(window_height)
        for value in (scale_x, scale_y):
            if not math.isfinite(value) or value <= 0:
                raise NeedsReview("窗口截图与窗口坐标比例异常，无法校准 GUI Agent 坐标")
        # 截图是窗口的等比缩放；两个方向比例不一致说明截图与窗口尺寸不同源。
        if abs(scale_x - scale_y) / max(scale_x, scale_y) > 0.02:
            raise NeedsReview("窗口截图宽高比例与窗口坐标不一致，无法校准 GUI Agent 坐标")
        if host_trace:
            host_trace("window_prepare", selector=selector, frame=Path(frame["path"]).name,
                       width=int(width), height=int(height),
                       window_width=float(window_width), window_height=float(window_height),
                       scale_x=round(scale_x, 4), scale_y=round(scale_y, 4))

    def _record_agent_usage(self, trace_path: Path, task: dict, model: TrialModel) -> dict:
        """按 call_id 归并留痕：成功与失败的模型调用都入账，Token 缺失保持 missing，不估算。"""
        if self.usage_ledger is None or not trace_path.is_file():
            return {}
        context = {"source": "agent", "run_id": self.run_id, "task_id": task.get("id", ""),
                   "source_name": task.get("source_name", ""), "stage": "ui_tars_navigation"}
        requests: dict[str, dict] = {}
        finals: dict[str, dict] = {}
        for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            key = record.get("call_id")
            if isinstance(key, bool) or not isinstance(key, int):
                continue
            kind = record.get("type")
            if kind == "request":
                requests.setdefault(str(key), record)
            elif kind in {"response", "error"}:
                finals.setdefault(str(key), record)
        summary = {"model_calls": 0, "with_usage": 0, "failed": 0, "skipped": 0}
        for key in sorted(set(requests) | set(finals), key=int):
            request = requests.get(key, {})
            final = finals.get(key, {})
            # 开始时间取请求发出时刻；响应时间是完成时刻，直接用会把耗时算晚。
            started_at = request.get("at") if isinstance(request.get("at"), str) else final.get("at")
            elapsed = final.get("duration_ms")
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
                elapsed = self._trace_elapsed_ms(started_at, final.get("at"))
            stable = uuid.uuid5(uuid.NAMESPACE_URL, f"{trace_path.resolve()}|{self.run_id}|{key}").hex
            call_id = self.usage_ledger.start_call(
                provider="openai_compatible", model=GUI_PLUS_MODEL, endpoint=model.base_url,
                context=context, started_at=started_at, call_id=stable)
            if not call_id:
                summary["skipped"] += 1
                continue
            usage = final.get("usage") if final.get("type") == "response" else None
            if final.get("type") == "response":
                self.usage_ledger.finish_call(
                    call_id, status="success", elapsed_ms=elapsed, http_status=200,
                    response={"id": final.get("request_id") or "",
                              "model": final.get("returned_model") or "", "usage": usage},
                    request_id=final.get("request_id") or "")
                summary["with_usage"] += 1 if isinstance(usage, dict) else 0
            else:
                # 只留下 request（网络中断、SDK 中止）也按失败入账，原因记为 interrupted。
                code = final.get("code") if isinstance(final.get("code"), str) else ""
                if not code:
                    code = "interrupted"
                self.usage_ledger.finish_call(
                    call_id, status="error", elapsed_ms=elapsed, http_status=None,
                    response={"id": "", "model": "", "usage": None},
                    error_type=code if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", code) else "agent_error")
                summary["failed"] += 1
            summary["model_calls"] += 1
        return summary if summary["model_calls"] or summary["skipped"] else {}

    @staticmethod
    def _trace_elapsed_ms(started_at, finished_at) -> int | None:
        try:
            start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(0, round((end - start).total_seconds() * 1000))

    def _validate_trial_handoff(self, decision: dict, frame: dict | None, selector: str) -> None:
        """交接前核对窗口、截图和菜品区域必须来自同一扇当前窗口。"""
        if not frame or not Path(frame["path"]).is_file():
            raise TrialProtocolError("交接缺少当前窗口截图")
        if getattr(self.transport, "_window_selector", None) != selector:
            raise TrialProtocolError("交接截图不是来自当前小程序窗口")
        if getattr(self.transport, "_window_pixels", None) != (frame["width"], frame["height"]):
            raise TrialProtocolError("交接截图与当前窗口尺寸不一致")
        region = decision.get("menu_region") or {}
        keys = ("x", "y", "width", "height")
        if not all(isinstance(region.get(key), (int, float)) and not isinstance(region.get(key), bool)
                   for key in keys):
            raise TrialProtocolError("交接缺少有效菜品区域")
        if not (0 <= region["x"] and 0 <= region["y"]
                and region["x"] + region["width"] <= frame["width"]
                and region["y"] + region["height"] <= frame["height"]):
            raise TrialProtocolError("交接菜品区域超出当前截图")

    def _window_geometry(self, selector: str) -> dict | None:
        """复用同 selector 上一次截图确认的窗口几何；窗口固定时直接按比例定位，不再截图。"""
        transport = getattr(self, "transport", None)
        geometry = getattr(transport, "_geometry", {}).get(selector) if transport is not None else None
        if not geometry:
            return None
        bounds, pixels = geometry.get("bounds"), geometry.get("pixels")
        if not bounds or not pixels or min(bounds) <= 0 or min(pixels) <= 0:
            return None
        return {"width": pixels[0], "height": pixels[1],
                "window_width": bounds[0], "window_height": bounds[1]}

    def _startup_wechat_frame(self, profile: dict, output_dir: Path, *, guarded: bool) -> dict:
        """首店必须截图；后续店优先复用几何，几何缺失或动作失败时才补截图。"""
        selector = profile["wechat_selector"]
        if not guarded:
            cached = self._window_geometry(selector)
            if cached is not None:
                return cached
        try:
            # 批次起点必须是一张全新截图，不能复用上一批可能已经变化的窗口矩形。
            return self._screenshot(output_dir, selector, "wechat-before-send.png", allow_fast=not guarded)
        except RobotCommandError:
            # 只有微信主窗口真的不可用时才唤回；正常路径不做窗口发现。
            if not self._ensure_wechat_window():
                raise
            return self._screenshot(output_dir, selector, "wechat-before-send.png")

    def _click_wechat_input(self, profile: dict, frame: dict) -> None:
        x = float(profile["wechat_input_x_ratio"]) * float(frame["window_width"])
        y = float(profile["wechat_input_y_ratio"]) * float(frame["window_height"])
        self._trace_click = {"source": "profile_input_ratio", "target": "微信消息输入区",
                             "coordinates": {"window_points": {"x": x, "y": y},
                                             "window_size_points": {"width": frame["window_width"], "height": frame["window_height"]},
                                             "screenshot_size_px": {"width": frame["width"], "height": frame["height"]},
                                             "ratio": {"x": profile["wechat_input_x_ratio"], "y": profile["wechat_input_y_ratio"]}}}
        try:
            self._send({"action": "click", "selector": profile["wechat_selector"], "x": x, "y": y})
        finally:
            self._trace_click = {}

    def _double_click_latest_image(self, profile: dict, frame: dict) -> None:
        """窗口位置固定时直接双击最新图片消息中心，不再截图、OCR、原图匹配。"""
        ratio_x, ratio_y = _LATEST_IMAGE_POINT_RATIO
        x = ratio_x * float(frame["window_width"])
        y = ratio_y * float(frame["window_height"])
        self._trace_click = {"source": "fixed_latest_image_point", "target": "本轮最新图片消息",
                             "coordinates": {"window_points": {"x": x, "y": y},
                                             "window_size_points": {"width": frame["window_width"], "height": frame["window_height"]},
                                             "ratio": {"x": ratio_x, "y": ratio_y}}}
        try:
            self._send({"action": "double_click", "selector": profile["wechat_selector"], "x": x, "y": y})
        finally:
            self._trace_click = {}

    def _open_source_automatic(self, task: dict, profile: dict, output_dir: Path, settle_seconds: float,
                               timing: dict | None = None) -> dict:
        """正常路径只保留发送和固定点位打开；OCR、原图匹配、窗口发现只在失败时恢复一次。"""
        if not all((self.clipboard_preparer, self.clipboard_text_preparer)):
            raise NeedsReview("automatic 模式依赖尚未接入")
        # 不能写成 `getattr(...) or {}`：空字典是调用方刚建好的同一个计时容器，
        # 被 or 换掉后各分段会写进临时字典，报告里只剩 startup/total。
        timing = timing if timing is not None else getattr(self, "_startup_timing", None)
        if timing is None:
            timing = {}
        started = time.monotonic()
        ipc_before = self._ipc_commands

        def mark(name: str) -> None:
            timing[name] = round((time.monotonic() - started) * 1000)

        # 上一轮失败或人工留下的旧小程序窗口，会让本轮“识别后是否出现新窗口”误判成没打开；
        # 每家开始前先全部收起，保证识别结果只对应本轮二维码。
        with self._trace("close_stale_miniapp") as trace:
            trace["closed"] = self._close_miniapp_windows(profile, output_dir)
        mark("close_stale")
        profile["miniapp_selector"] = ""
        self._set_stage(task, "verifying_wechat", "发送门店名与二维码；批次首店核对聊天上下文")
        # 批次起点保护：每个 run 的首个任务保留一次截图 + OCR；其余任务复用已确认的窗口几何。
        guard_needed = self._startup_guard_run_id != (self.run_id or "")
        wechat = self._startup_wechat_frame(profile, output_dir, guarded=guard_needed)
        if guard_needed:
            with self._trace("wechat_ocr", purpose="batch_start",
                             frame_evidence=self._debug_frame_ref(Path(wechat["path"])),
                             image_size={"width": wechat["width"], "height": wechat["height"]}) as trace:
                if self.ocr_reader is None:
                    trace.update(skipped=True, reason="ocr_unavailable")
                else:
                    ocr_started = time.monotonic()
                    texts = self.ocr_reader(Path(wechat["path"]))
                    trace.update(ocr_rows=texts, ocr_engine=self._ocr_engine_name(),
                                 ocr_elapsed_ms=round((time.monotonic() - ocr_started) * 1000))
                    if not self._assistant_active(texts, wechat["width"], wechat["height"]):
                        raise NeedsReview("微信右上方未确认文件传输助手；未输入或发送素材")
            self._startup_guard_run_id = self.run_id or ""
        mark("wechat_frame")
        self._guard()
        try:
            self._click_wechat_input(profile, wechat)
        except RobotCommandError:
            if not self._ensure_wechat_window():
                raise
            wechat = self._screenshot(output_dir, profile["wechat_selector"], "wechat-recover.png")
            self._click_wechat_input(profile, wechat)
        mark("input_click")
        source_name = str(task["source_name"])
        # 多条快捷键可拼在同一次 key 命令里（UiPath 会按 DelayBetweenShortcuts 依次发送），
        # 把“粘贴 + 发送”合并成一次 IPC，单店少两次往返。
        paste_send = profile["paste_shortcut"] + profile["send_key"]
        # 粘贴完整来源文件夹名，避免英文、数字经过中文输入法组合转换。
        with self._trace("prepare_store_name_clipboard", char_count=len(source_name)):
            self.clipboard_text_preparer(source_name)
        self._send({"action": "key", "selector": profile["wechat_selector"], "key": paste_send})
        mark("store_name_sent")
        self._guard()
        with self._trace("prepare_image_clipboard", source_file=Path(task["source_path"]).name):
            self.clipboard_preparer(Path(task["source_path"]))
        # 图片粘贴与发送仍分开两条命令：拼在一起回车会被吞掉。
        # 下一条 key 命令自身往返数秒，已覆盖缩略图附着，不再额外固定等待。
        self._send({"action": "key", "selector": profile["wechat_selector"],
                    "key": profile["paste_shortcut"]})
        self._send({"action": "key", "selector": profile["wechat_selector"],
                    "key": profile["send_key"]})
        mark("image_sent")
        try:
            self._double_click_latest_image(profile, wechat)
        except RobotCommandError:
            # 微信窗口尺寸发生变化时旧几何会被 UiPath 拒绝；重拍一次再双击。
            wechat = self._screenshot(output_dir, profile["wechat_selector"], "wechat-recover.png")
            self._double_click_latest_image(profile, wechat)
        mark("open_image")
        try:
            result = self._recognize_preview(profile, output_dir, None, settle_seconds)
        except RobotCommandError:
            # 只有预览没打开（截图找不到窗口）才退回原图匹配；业务失败不重试。
            sent, match = self._wait_for_sent_image(task, profile, output_dir)
            self._guard()
            before = self._window_list(self._send({"action": "discover"}).get("windows"))
            center = match["center_image_px"]
            window_x = center["x"] * sent["window_width"] / sent["width"]
            window_y = center["y"] * sent["window_height"] / sent["height"]
            self._trace_click = {"source": "source_thumbnail_match", "target": "本轮门店图片消息",
                                 "target_resolution": match,
                                 "coordinates": {"image_px": center,
                                                 "window_points": {"x": window_x, "y": window_y},
                                                 "window_size_points": {"width": sent["window_width"], "height": sent["window_height"]},
                                                 "screenshot_size_px": {"width": sent["width"], "height": sent["height"]}}}
            try:
                self._send({"action": "double_click", "selector": profile["wechat_selector"],
                            "x": window_x, "y": window_y})
            finally:
                self._trace_click = {}
            result = self._recognize_preview(profile, output_dir, before, settle_seconds)
        mark("preview_recognize")
        timing["startup_ipc"] = self._ipc_commands - ipc_before
        return result

    def _wait_for_sent_image(self, task: dict, profile: dict, output_dir: Path) -> tuple[dict, dict]:
        """快速路径只用现有发送后截图；只有加载中或图片未出现时才有限观察，不重复发送。"""
        last_reason = "发送后尚未看到本轮图片消息"
        deadline = time.monotonic() + 12
        for observation in range(1, 4):
            self._guard()
            if observation > 1:
                if time.monotonic() >= deadline:
                    break
                self._send({"action": "noop", "seconds": .3})
            basename = "wechat-after-send.png" if observation == 1 else f"wechat-after-send-{observation}.png"
            sent = self._screenshot(output_dir, profile["wechat_selector"], basename)
            frame_ref = self._debug_frame_ref(Path(sent["path"]))
            # OCR 只用于确认仍停留在文件传输助手；不可用时不再阻断启动。
            if self.ocr_reader is not None:
                with self._trace("wechat_ocr", frame_evidence=frame_ref, observation=observation,
                                 image_size={"width": sent["width"], "height": sent["height"]}) as trace:
                    started = time.monotonic()
                    ocr = self.ocr_reader(Path(sent["path"]))
                    trace.update(ocr_rows=ocr, ocr_engine=self._ocr_engine_name(),
                                 ocr_elapsed_ms=round((time.monotonic() - started) * 1000))
                if not self._assistant_active(ocr, sent["width"], sent["height"]):
                    raise NeedsReview("发送后文件传输助手上下文丢失，停止处理")
            # 用最后一条右侧文字气泡限定图片范围，再用本轮原图匹配确认身份；
            # 不识别气泡文字，也不要求 OCR 认出店名/编码。
            bubbles = find_sent_text_bubbles(Path(sent["path"]))
            latest = max(bubbles, key=lambda item: item["bottom"], default=None)
            anchor_bottom = latest["bottom"] if latest else 0
            with self._trace("wechat_image_match", source_name=task["source_name"],
                             observation=observation, anchor_bottom=anchor_bottom,
                             bubble_count=len(bubbles), frame_evidence=frame_ref) as trace:
                try:
                    match = locate_sent_image(Path(sent["path"]), Path(task["source_path"]), anchor_bottom)
                except ChatImageNotFound as exc:
                    last_reason = str(exc)
                    trace.update(diagnostics=exc.diagnostics, observation_result="thumbnail_not_visible", retry=observation < 3)
                    if observation < 3:
                        # 新发的大图可能有一部分落在输入框下方；往下滚一点让整张缩略图露出再匹配。
                        try:
                            self._send({"action": "scroll", "selector": profile["wechat_selector"],
                                        "x": sent["width"] * 0.7, "y": sent["height"] * 0.6,
                                        "direction": "Down", "units": 1})
                            self._send({"action": "noop", "seconds": .4})
                        except Exception:
                            pass
                    continue
                trace.update(target_resolution=match)
            # 只发到文件传输助手，缩略图出现即可继续；不校验上传徽标，避免把图片内容误判为发送失败。
            return sent, match
        raise NeedsReview(last_reason + "；未打开历史二维码")

    def _preview_external_link(self, profile: dict, output_dir: Path, seconds: float = 6.0) -> bool:
        """预览里是否出现“在浏览器打开”一类提示；仅作为启动阶段的诊断证据。"""
        if self.ocr_reader is None:
            return False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                preview = self._screenshot(output_dir, profile["preview_selector"], "image-preview-link.png")
            except Exception:
                return False
            _, rows = self._read_scaled_ocr(Path(preview["path"]))
            if any(self._EXTERNAL_LINK.search(str(row.get("text", ""))) for row in rows):
                return True
            time.sleep(1.0)
        return False

    def _recognize_preview(self, profile: dict, output_dir: Path, before_windows: list[dict] | None,
                           settle_seconds: float) -> dict:
        """每轮依据当前预览图标定位，只点击一次；后续仅等待和观察识别结果。"""
        selector = profile["preview_selector"]
        # 预览窗口宽高每轮都可能不同，必须重新取窗口矩形，不能复用上一轮。
        preview = self._screenshot(output_dir, selector, "image-preview.png", allow_fast=False)
        with self._trace("preview_icon_match", frame_evidence=self._debug_frame_ref(Path(preview["path"]))) as trace:
            try:
                match = locate_preview_recognize(Path(preview["path"]))
            except PreviewIconNotFound as exc:
                trace.update(diagnostics=exc.diagnostics)
                # 只记录预览里看到的现象，不在启动阶段替门店下“二维码不是小程序码”的业务结论。
                external_prompt = self._preview_external_link(profile, output_dir)
                trace["external_link_prompt"] = external_prompt
                raise NeedsReview(
                    ("预览中出现“在浏览器打开”提示；" if external_prompt else "")
                    + "未找到识别二维码入口，未进入小程序",
                    "startup_preview_unavailable") from exc
            trace.update(target_resolution=match)
        center = match["center_image_px"]
        x = center["x"] * preview["window_width"] / preview["width"]
        y = center["y"] * preview["window_height"] / preview["height"]
        self._trace_click = {
            "source": "local_preview_icon_match", "target": "识别二维码按钮",
            "target_resolution": match, "frame_evidence": self._debug_frame_ref(Path(preview["path"])),
            "coordinates": {
                "image_px": center, "window_points": {"x": x, "y": y},
                "screenshot_size_px": {"width": preview["width"], "height": preview["height"]},
                "window_size_points": {"width": preview["window_width"], "height": preview["window_height"]},
                "scale": {"x": preview["window_width"] / preview["width"], "y": preview["window_height"] / preview["height"]},
            },
        }
        try:
            self._send({"action": "click", "selector": selector, "x": x, "y": y})
        finally:
            self._trace_click = {}
        with self._trace("preview_recognition_result") as trace:
            # 只需要小程序窗口被创建；页面内容由后续 resize、截图和就绪判断承担。
            # 首次 discover 本身要数秒，已足够覆盖窗口创建，不再额外固定等待；
            # 偶发慢的才补两次短等待，仍失败转人工，不重发点击。
            for attempt, wait_seconds in enumerate((0, .5, 1.0), start=1):
                if wait_seconds:
                    self._send({"action": "noop", "seconds": wait_seconds})
                after_windows = self._window_list(self._send({"action": "discover"}).get("windows"))
                # 正常路径开始前已收起旧小程序，任何小程序窗口都属于本轮；
                # 恢复路径仍用点击前窗口列表排除旧窗口，避免误接管。
                scoped = after_windows if before_windows is None else self._new_windows(before_windows, after_windows)
                candidates = self._miniapp_candidates(scoped)
                trace.update(observation=attempt, new_windows=[item["title"] for item in candidates])
                if not candidates:
                    continue
                # 多个新窗口不在启动阶段做业务取舍：先接管第一个，
                # Agent 可用 list_windows / switch_window 自行判断该操作哪个页面。
                if len(candidates) > 1:
                    trace["candidate_count"] = len(candidates)
                profile["miniapp_selector"] = candidates[0]["selector"]
                trace.update(result="miniapp_opened", selector=candidates[0]["selector"])
                return {"miniapp_selector": candidates[0]["selector"]}
            trace["result"] = "no_miniapp_window"
            # 无新窗口时保留结果画面作为证据；OCR 文字只留痕，不据此判定门店业务状态。
            try:
                result_frame = self._screenshot(output_dir, selector, "preview-recognition-result.png")
            except RobotCommandError as exc:
                trace["preview_error"] = str(exc)
            else:
                trace["frame_evidence"] = self._debug_frame_ref(Path(result_frame["path"]))
                if self.ocr_reader:
                    rows = self.ocr_reader(Path(result_frame["path"]))
                    trace["ocr_rows"] = rows
                    trace["visible_failure"] = any(re.search(r"无法识别|未识别|识别失败|未发现.*二维码", str(row.get("text", ""))) for row in rows)
            raise NeedsReview("已点击识别二维码，但尚未观察到新的小程序窗口；未重复点击，未进入小程序",
                              "startup_no_miniapp")

    @staticmethod
    def _window_list(windows: Any) -> list[dict]:
        if isinstance(windows, str):
            try:
                windows = json.loads(windows)
            except ValueError:
                windows = []
        return [item for item in windows if isinstance(item, dict)] if isinstance(windows, list) else []

    @staticmethod
    def _window_identity(item: dict) -> tuple:
        position = item.get("position") or {}
        return (
            str(item.get("app") or "").casefold(), str(item.get("title") or "").strip().casefold(),
            str(item.get("role") or "").casefold(),
            *(position.get(key) for key in ("x", "y", "width", "height")),
        )

    @classmethod
    def _new_windows(cls, before: list[dict], after: list[dict]) -> list[dict]:
        previous = {cls._window_identity(item) for item in before}
        return [item for item in after if cls._window_identity(item) not in previous]

    def _move_window_on_screen(self, selector: str) -> None:
        """把滞留在屏幕外的小程序窗口挪回主屏；否则点击右上角会直接报“超出屏幕边界”。"""
        if sys.platform == "win32":
            # 先使用实际的窗口截图和几何，避免将 Mac 布局坐标套到 Windows DPI。
            return
        from screeninfo import get_monitors
        screens = get_monitors()
        screen = next((item for item in screens if item.is_primary), screens[0])
        self._send({"action": "resize", "selector": selector, **production_window_geometry(screen)})
        # 调用方紧接着会截图确认新几何（本身要数秒），不再额外固定等待。

    def _close_miniapp_windows(self, profile: dict, output_dir: Path,
                               settle_seconds: float = .4, rounds: int = 3) -> int:
        """收起所有残留的小程序窗口；清不干净也不阻断当前结论，交给下一轮安全起点校验。"""
        if sys.platform == "win32":
            # Windows 批次仅通过 _close_to_file_assistant 关闭本轮已接管窗口。
            # 其他微信窗口保留，由批次收尾的重新发现检查能否安全继续。
            return 0
        close_key = profile.get("close_key")
        if not close_key:
            return 0
        closed = 0
        for _ in range(max(1, rounds)):
            try:
                windows = self._window_list(self._send({"action": "discover"}).get("windows"))
                candidates = self._miniapp_candidates(windows)
            except (RobotTimeout, RunnerPaused, RunnerTerminated):
                raise
            except Exception:
                break
            if not candidates:
                break
            for index, candidate in enumerate(candidates):
                try:
                    shot = self._screenshot(
                        output_dir, candidate["selector"], f"close-miniapp-{index}.png")
                    width = float(shot.get("window_width") or 0)
                    height = float(shot.get("window_height") or 0)
                    if width and height:
                        # 小程序页面会吞掉 Cmd+W；右上角胶囊里的“关闭”才是可靠出口。
                        # 窗口可能被上一家拖到屏幕外，先挪回主屏再点，否则会报超出边界。
                        try:
                            self._send({"action": "click", "selector": candidate["selector"],
                                        "x": width - 21, "y": 30})
                        except RobotCommandError:
                            self._move_window_on_screen(candidate["selector"])
                            moved = self._screenshot(output_dir, candidate["selector"],
                                                     f"close-miniapp-{index}-moved.png")
                            width = float(moved.get("window_width") or width)
                            self._send({"action": "click", "selector": candidate["selector"],
                                        "x": width - 21, "y": 30})
                        self._send({"action": "noop", "seconds": .5})
                        remaining = self._miniapp_candidates(
                            self._window_list(self._send({"action": "discover"}).get("windows")))
                        if any(item["title"] == candidate["title"] for item in remaining):
                            # 胶囊未命中时退回“点标题栏聚焦 + Cmd+W”的老路径。
                            self._send({"action": "click", "selector": candidate["selector"],
                                        "x": width / 2, "y": 18})
                            self._send({"action": "key", "selector": candidate["selector"],
                                        "key": close_key})
                            self._send({"action": "noop", "seconds": .3})
                            remaining = self._miniapp_candidates(
                                self._window_list(self._send({"action": "discover"}).get("windows")))
                        if not any(item["title"] == candidate["title"] for item in remaining):
                            closed += 1
                except (RobotTimeout, RunnerPaused, RunnerTerminated):
                    raise
                except Exception:
                    continue
            self._send({"action": "noop", "seconds": settle_seconds})
        return closed

    def _close_to_file_assistant(self, profile: dict, output_dir: Path, settle_seconds: float) -> None:
        if not all((profile.get("miniapp_selector"), profile.get("wechat_selector"), profile.get("close_key"))):
            raise NeedsReview("缺少关闭小程序所需的已录制 selector/key")
        self._guard()
        if sys.platform == "win32":
            selector = profile["miniapp_selector"]
            windows = self._send({"action": "discover"}).get("windows")
            matches = [item for item in self._miniapp_candidates(windows) if item["selector"] == selector]
            if len(matches) > 1 or any(
                windows_selectors_overlap(selector, profile.get(key, ""))
                for key in ("wechat_selector", "preview_selector")
            ):
                raise NeedsReview("无法安全区分待关闭的小程序与微信聊天或图片预览窗口")
            if matches:
                self._screenshot(output_dir, selector, "miniapp-before-close.png")
                self._send({"action": "key", "selector": selector, "key": profile["close_key"]})
            self._send({"action": "noop", "seconds": settle_seconds})
            closed = self._screenshot(output_dir, profile["wechat_selector"], "wechat-after-close.png")
            if self._read_assistant_context(closed, "after_close") is not True:
                raise NeedsReview("关闭当前小程序后未确认回到文件传输助手；批次停止")
            return
        # 小程序页面会吞掉 Cmd+W；优先点右上角胶囊的“关闭”，失败再退回聚焦 + Cmd+W。
        shot = self._screenshot(output_dir, profile["miniapp_selector"], "miniapp-before-close.png")
        width = float(shot.get("window_width") or 0)
        height = float(shot.get("window_height") or 0)
        if width and height:
            try:
                self._send({"action": "click", "selector": profile["miniapp_selector"],
                            "x": width - 21, "y": 30})
            except RobotCommandError:
                self._move_window_on_screen(profile["miniapp_selector"])
                moved = self._screenshot(output_dir, profile["miniapp_selector"], "miniapp-moved.png")
                width = float(moved.get("window_width") or width)
                self._send({"action": "click", "selector": profile["miniapp_selector"],
                            "x": width - 21, "y": 30})
            self._send({"action": "noop", "seconds": .5})
            remaining = self._miniapp_candidates(
                self._window_list(self._send({"action": "discover"}).get("windows")))
            if any(item["selector"] == profile["miniapp_selector"] for item in remaining):
                self._send({"action": "click", "selector": profile["miniapp_selector"],
                            "x": width / 2, "y": 18})
                self._send({"action": "key", "selector": profile["miniapp_selector"],
                            "key": profile["close_key"]})
        self._send({"action": "noop", "seconds": settle_seconds})
        closed = self._screenshot(output_dir, profile["wechat_selector"], "wechat-after-close.png")
        if self._read_assistant_context(closed, "after_close") is False:
            raise NeedsReview("关闭当前小程序后未确认回到文件传输助手；批次停止")

    def _read_assistant_context(self, frame: dict, purpose: str) -> bool | None:
        """True/False 为 OCR 结论；None 表示 OCR 不可用、未确认（不据此拦截）。"""
        with self._trace("wechat_ocr", purpose=purpose,
                         frame_evidence=self._debug_frame_ref(Path(frame["path"]))) as trace:
            if self.ocr_reader is None:
                trace.update(skipped=True, reason="ocr_unavailable",
                             image_size={"width": frame["width"], "height": frame["height"]})
                return None
            rows = self.ocr_reader(Path(frame["path"]))
            active = self._assistant_active(rows, frame["width"], frame["height"])
            trace.update(ocr_rows=rows, assistant_active=active, ocr_engine=self._ocr_engine_name(),
                         image_size={"width": frame["width"], "height": frame["height"]})
            return active

    @staticmethod
    def _assistant_active(items: list[dict], width: int, height: int) -> bool:
        # 微信侧栏约占窗口宽度的 25.5%；当前会话标题应位于聊天区，不能把侧栏条目当作收件会话。
        chat_content_left = width * 0.255
        return any(
            "文件传输助手" in str(item.get("text", "")).replace(" ", "")
            and float(item.get("x", -1)) > chat_content_left
            and float(item.get("y", height)) < height * 0.14
            for item in items
        )

    @classmethod
    def _store_name_matches(cls, items: list[dict], source_name: str, width: int,
                            image_height: int | None = None, *, message_boxes=None) -> list[dict]:
        return find_store_name_matches(items, source_name, width, image_height, message_boxes=message_boxes)

    def _augment_chat_ocr_with_bubbles(self, image_path: Path, rows: list[dict], boxes) -> list[dict]:
        """整图 OCR 偶尔会整条漏读消息气泡；对没有文字的右对齐气泡单独放大补 OCR。"""
        if not boxes or self.ocr_reader is None or not image_path.is_file():
            return rows
        covered: list[tuple[float, float, float, float]] = []
        for item in rows:
            try:
                top = float(item.get("y", 0))
                left = float(item.get("x", 0))
                bottom = top + float(item.get("height", 0))
                right = left + float(item.get("width", 0))
            except (TypeError, ValueError):
                continue
            covered.append((top, bottom, left, right))
        extra: list[dict] = []
        with Image.open(image_path) as original:
            image = original.convert("RGB")
            for box in boxes:
                try:
                    left = float(box.get("left", 0))
                    top = float(box.get("top", 0))
                    right = float(box.get("right", 0))
                    bottom = float(box.get("bottom", 0))
                except (TypeError, ValueError):
                    continue
                if bottom - top < 8 or right - left < 20:
                    continue
                # 只有横向也落在该气泡范围内的 OCR 行才算已覆盖；左侧会话栏同一行的文字不能顶替。
                if any(top - 6 <= y0 and y1 <= bottom + 6 and
                       min(right, x1) - max(left, x0) >= (right - left) * 0.5
                       for y0, y1, x0, x1 in covered):
                    continue
                pad = 4
                crop_box = (max(0, int(left) - pad), max(0, int(top) - pad),
                            min(image.width, int(right) + pad), min(image.height, int(bottom) + pad))
                if crop_box[2] - crop_box[0] < 8 or crop_box[3] - crop_box[1] < 8:
                    continue
                crop = image.crop(crop_box)
                crop = crop.resize((crop.width * 3, crop.height * 3), Image.Resampling.LANCZOS)
                fd, name = tempfile.mkstemp(prefix="bubble-ocr-", suffix=".png", dir=str(image_path.parent))
                os.close(fd)
                temporary = Path(name)
                temporary.chmod(0o600)
                try:
                    crop.save(temporary, format="PNG")
                    found = self.ocr_reader(temporary)
                except OSError:
                    found = []
                finally:
                    temporary.unlink(missing_ok=True)
                for item in found or []:
                    try:
                        extra.append({
                            "text": item.get("text", ""),
                            "x": crop_box[0] + float(item["x"]) / 3,
                            "y": crop_box[1] + float(item["y"]) / 3,
                            "width": float(item["width"]) / 3,
                            "height": float(item["height"]) / 3,
                            "confidence": item.get("confidence", 1.0),
                        })
                    except (KeyError, TypeError, ValueError):
                        continue
        return rows + extra

    @staticmethod
    def _normalize_chat_label(value: str) -> str:
        # 统一全半角、大小写和空格/标点，保留中英文与数字，按完整目录名匹配。
        normalized = unicodedata.normalize("NFKC", str(value)).casefold()
        return "".join(character for character in normalized if character.isalnum())

    @staticmethod
    def _anchor_diagnostic_rows(items: list[dict], width: int) -> dict:
        rows = []
        for item in items:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            try:
                if float(item.get("x", -1)) <= width * 0.35:
                    continue
            except (TypeError, ValueError):
                continue
            row = {"text": text[:500]}
            for key in ("x", "y", "width", "height"):
                try:
                    row[key] = round(float(item[key]), 1)
                except (KeyError, TypeError, ValueError):
                    pass
            rows.append(row)
        return {"rows": rows[-60:], "omitted_rows": max(0, len(rows) - 60)}

    def _preserve_action_screenshot(self, source: Path) -> str:
        if not source.is_file():
            raise OSError("本轮微信截图文件不存在")
        if self.debug_log:
            name = self.debug_log.save_image(source)
            if not name:
                raise OSError("调试截图达到空间上限或无法排队")
            return name
        evidence_dir = self.store.path.parent / "action-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(evidence_dir, 0o700)
        name = uuid.uuid4().hex + ".png"
        destination = evidence_dir / name
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        return name

    def _debug_frame_ref(self, source: Path) -> str | None:
        """同一张菜单前截图只入证据区一次，OCR 与页面检查共同引用。"""
        if not self.debug_log:
            return self._preserve_action_screenshot(source)
        key = str(Path(source).resolve())
        if key in self._debug_frame_refs:
            return self._debug_frame_refs[key]
        name = self.debug_log.save_image(Path(source))
        if name:
            self._debug_frame_refs[key] = name
            if len(self._debug_frame_refs) > 512:
                self._debug_frame_refs.pop(next(iter(self._debug_frame_refs)))
        return name

    def _ocr_engine_name(self) -> str:
        reader = self.ocr_reader
        return f"{getattr(reader, '__module__', type(reader).__module__)}.{getattr(reader, '__qualname__', type(reader).__qualname__)}"

    # 以下定位与 OCR 辅助仍供人工标注建议使用，不参与正式采集导航。
    def _local_people_grid_target(self, ocr_result, people_count: int) -> SceneDecision | None:
        """当配置人数被 OCR 漏掉时，用可见数字网格的几何位置补出该格。"""
        image_size, rows = ocr_result
        if image_size is None or people_count != 1:
            return None
        factor = 3
        cells: list[dict] = []
        for item in rows:
            normalized = self._normalize_ocr_text(str(item.get("text", "")))
            match = re.fullmatch(r"(\d{1,2})(?:人|位)?", normalized)
            if not match:
                continue
            number = int(match.group(1))
            if not 1 <= number <= 12:
                continue
            try:
                confidence = float(item.get("confidence", 0))
                x, y, width, height = (float(item[key]) / factor for key in ("x", "y", "width", "height"))
            except (KeyError, TypeError, ValueError):
                continue
            if confidence < 0.5 or width <= 0 or height <= 0:
                continue
            if x < 0 or y < 0 or x + width > image_size[0] or y + height > image_size[1]:
                continue
            cells.append({"number": number, "cx": x + width / 2, "cy": y + height / 2, "h": height})
        if len({cell["number"] for cell in cells}) < 3:
            return None

        def cluster(values: list[float], tolerance: float) -> list[float]:
            ordered = sorted(values)
            groups: list[list[float]] = []
            for value in ordered:
                if not groups or value - groups[-1][-1] > tolerance:
                    groups.append([value])
                else:
                    groups[-1].append(value)
            return [sum(group) / len(group) for group in groups]

        tolerance = max(6.0, median(cell["h"] for cell in cells) * 0.8)
        x_centers = cluster([cell["cx"] for cell in cells], tolerance)
        y_centers = cluster([cell["cy"] for cell in cells], tolerance)
        if len(x_centers) < 2 or not y_centers:
            return None
        x_gaps = [right - left for left, right in zip(x_centers, x_centers[1:]) if right - left > 1]
        if not x_gaps:
            return None
        spacing = median(x_gaps)
        first_row_y = min(y_centers)
        top_row = [cell for cell in cells if abs(cell["cy"] - first_row_y) <= tolerance]
        if len(top_row) < 2:
            return None
        anchor = min(top_row, key=lambda cell: cell["number"])
        inferred_x = anchor["cx"] - (anchor["number"] - people_count) * spacing
        inferred_y = anchor["cy"]
        if not (5 <= inferred_x < image_size[0] - 5 and 5 <= inferred_y < image_size[1] - 5):
            return None
        if not (min(x_centers) - spacing * 0.6 <= inferred_x <= max(x_centers) + spacing * 0.6):
            return None
        self._last_target_resolution = {
            "matched": True,
            "method": "local_people_grid_geometry",
            "configured_people": people_count,
            "anchor_number": anchor["number"],
            "anchor_center_image_px": {"x": round(anchor["cx"], 1), "y": round(anchor["cy"], 1)},
            "column_spacing": round(spacing, 1),
            "x_centers": [round(value, 1) for value in x_centers],
            "y_centers": [round(value, 1) for value in y_centers],
            "center_image_px": {"x": round(inferred_x, 1), "y": round(inferred_y, 1)},
        }
        return SceneDecision(
            action="click", x=inferred_x, y=inferred_y,
            reason="配置人数被 OCR 漏识别；按可见人数网格几何推断第一格",
            target_label=str(people_count), overlay_visible=True, transaction_visible=False,
            login_required=False, phone_required=False, consent_required=False,
        )

    def _local_people_confirm_target(self, ocr_result, people_count: int) -> SceneDecision | None:
        """人数已选后点底部确认；该按钮小字常被 OCR 认成近似字，按底栏位置兜底。"""
        image_size, rows = ocr_result
        if image_size is None:
            return None
        width, height = image_size
        best: dict | None = None
        for item in rows:
            text = self._normalize_ocr_text(str(item.get("text", "")))
            if not 2 <= len(text) <= 4:
                continue
            score = max(SequenceMatcher(None, text, label).ratio() for label in ("确定", "确认"))
            if score < 0.5:
                continue
            try:
                x, y, box_w, box_h = (float(item[key]) / 3 for key in ("x", "y", "width", "height"))
            except (KeyError, TypeError, ValueError):
                continue
            center_y = y + box_h / 2
            # 只认底部主操作区，避免把正文里的“确认”字样当按钮。
            if center_y < height * 0.85 or box_w > width * 0.6:
                continue
            if best is None or score > best["score"]:
                best = {"score": score, "x": x + box_w / 2, "y": center_y, "text": text}
        if best is None:
            return None
        self._last_target_resolution = {
            "matched": True, "method": "local_people_confirm_fuzzy", "selected": best,
            "configured_people": people_count,
        }
        return SceneDecision(
            action="click", x=best["x"], y=best["y"],
            reason="人数已选，底部确认按钮文字被 OCR 误识别；按底栏近似文字兜底",
            target_label="确定", overlay_visible=True, transaction_visible=False,
            login_required=False, phone_required=False, consent_required=False,
            selected_people_count=people_count,
        )

    def _local_prepare_action(
        self,
        ocr_result,
        people_count: int,
        auto_authorize: bool,
        people_selected: bool = False,
        image_path: Path | None = None,
        consent_checked: bool = False,
    ) -> SceneDecision | None:
        """先识别场景，再对唯一、明确的前景按钮作一次本地决策。"""
        image_size, rows = ocr_result
        self._last_target_resolution = {
            "strategy": "scene_first_exact_button_once",
            "matched": False,
            "scene": "unknown",
            "attempts": [],
        }
        if image_size is None:
            self._last_target_resolution["reason"] = "ocr_unavailable"
            return None

        normalized_rows = [self._normalize_ocr_text(str(row.get("text", ""))) for row in rows]
        full_text = " ".join(normalized_rows)

        # 微信“添加到我的小程序”引导浮层会吞掉小程序页面里的点击：
        # 页面看起来没变、tab 点了也不跳。先点浮层右上角的关闭叉。
        if "添加到我的" in full_text:
            for row in rows or []:
                if self._normalize_ocr_text(str(row.get("text", ""))) not in {"x", "×"}:
                    continue
                try:
                    x, y = float(row["x"]), float(row["y"])
                    box_w, box_h = float(row["width"]), float(row["height"])
                except (KeyError, TypeError, ValueError):
                    continue
                width, height = image_size
                if y + box_h / 2 > height * 3 * 0.2 or x + box_w / 2 < width * 3 * 0.7:
                    continue
                center = {"x": (x + box_w / 2) / 3, "y": (y + box_h / 2) / 3}
                if self._local_point_consumed(center["x"], center["y"]):
                    continue
                self._last_target_resolution.update(
                    matched=True, method="local_wechat_tip_close",
                    selected={"center_image_px": center}, reason="wechat_add_to_miniapp_tip")
                return SceneDecision(
                    action="click", x=center["x"], y=center["y"],
                    reason="关闭微信“添加到我的小程序”引导浮层，避免它吞掉后续点击",
                    target_label="X", overlay_visible=True, transaction_visible=False,
                    login_required=False, phone_required=False, consent_required=False,
                )

        # 品牌小程序常先让选城市/门店；必须按任务里的城市和门店选，
        # 选错会采到其他城市同名门店（例如“一坐一忘（杭州西湖店）”）。
        city, store_name = self._task_store_parts()
        if city and "暂未开通自提" in full_text:
            switch = self._find_ocr_target_detail(rows, image_size, "选择其他城市", min_confidence=0.3)
            if switch.get("matched"):
                center = switch["center_image_px"]
                if not self._local_point_consumed(center["x"], center["y"]):
                    self._last_target_resolution.update(
                        matched=True, method="local_city_switch", selected=switch,
                        reason="city_not_open_switch")
                    return SceneDecision(
                        action="click", x=center["x"], y=center["y"],
                        reason="当前城市未开通自提，点“选择其他城市”按任务地址重新选",
                        target_label="选择其他城市", overlay_visible=False, transaction_visible=False,
                        login_required=False, phone_required=False, consent_required=False,
                    )
            raise NeedsReview(f"小程序自提渠道在{city}未开通；未自动切换到其他城市", "pickup_unavailable")
        if city and re.search(r"城市暂未开通此业务|您检索的城市暂未开通|所选城市暂未开通此业务", full_text):
            # 搜到任务城市后仍提示“暂未开通”，换城市或重试都无法进入菜单。
            raise NeedsReview(f"{city}未开通该门店的自提/点餐业务，无法进入菜单", "pickup_unavailable")
        if city and re.search(r"选择城市|城市列表|切换城市", full_text):
            for label in (city, city.removesuffix("市")):
                resolved = self._find_ocr_target_detail(rows, image_size, label, min_confidence=0.3)
                if not resolved.get("matched"):
                    continue
                center = resolved["center_image_px"]
                if self._local_point_consumed(center["x"], center["y"]):
                    continue
                self._last_target_resolution.update(
                    matched=True, method="local_task_city", selected=resolved,
                    reason=f"select_city_{label}")
                return SceneDecision(
                    action="click", x=center["x"], y=center["y"],
                    reason=f"城市选择页按任务地址选择“{label}”",
                    target_label=label, overlay_visible=False, transaction_visible=False,
                    login_required=False, phone_required=False, consent_required=False,
                )
            # 目标城市不在首屏：点搜索框并把城市名输入进去（输入由准备循环执行）。
            box = self._find_ocr_target_detail(rows, image_size, "请输入城市名或拼音", min_confidence=0.3)
            if not box.get("matched"):
                box = self._find_ocr_target_detail(rows, image_size, "搜索城市", min_confidence=0.3)
            if not box.get("matched"):
                # 搜索框文字常被 OCR 带上放大镜“Q”等噪声（“Q 请输入城市名或拼音”），
                # 精确匹配会漏；按包含关系兜底，命中多个时仍按歧义放弃。
                box = self._find_ocr_target_detail(
                    rows, image_size, "请输入城市", min_confidence=0.3, contains=True)
            if box.get("matched"):
                center = box["center_image_px"]
                if not self._local_point_consumed(center["x"], center["y"]):
                    # 搜索词去掉“市”，兼容列表里存“北京”与“北京市”两种写法。
                    keyword = city.removesuffix("市") or city
                    self._last_target_resolution.update(
                        matched=True, method="local_city_search", selected=box,
                        reason=f"city_search_{city}")
                    return SceneDecision(
                        action="click", x=center["x"], y=center["y"],
                        reason=f"城市列表首屏没有“{city}”，点搜索框并输入城市名",
                        target_label=keyword, overlay_visible=False, transaction_visible=False,
                        login_required=False, phone_required=False, consent_required=False,
                        input_text=keyword,
                    )
        if store_name and "去下单" in full_text:
            target, mismatch = self._store_row_target(rows, image_size, store_name)
            if mismatch:
                raise NeedsReview(
                    f"门店列表里没有任务门店“{store_name}”；未点击其他门店", "store_mismatch")
            if target is not None:
                center = target["center_image_px"]
                if not self._local_point_consumed(center["x"], center["y"]):
                    self._last_target_resolution.update(
                        matched=True, method="local_task_store", selected=target,
                        reason="select_task_store")
                    return SceneDecision(
                        action="click", x=center["x"], y=center["y"],
                        reason=f"门店列表按任务门店名选择“{store_name}”",
                        target_label=store_name, overlay_visible=False, transaction_visible=False,
                        login_required=False, phone_required=False, consent_required=False,
                    )

        # 识别二维码后可能弹出“即将打开 XX 小程序”这里只采当前门店，
        # 点“取消”留在原小程序，避免被带到会员社团等旁支小程序。
        if re.search(r"即将打开.{0,24}小程序", full_text) and "取消" in full_text:
            cancel = self._find_ocr_target_detail(rows, image_size, "取消", min_confidence=0.3)
            if cancel.get("matched"):
                center = cancel["center_image_px"]
                if self._local_point_consumed(center["x"], center["y"]):
                    cancel = None
                else:
                    self._last_target_resolution.update(
                        matched=True, method="local_decline_other_miniapp",
                        selected=cancel, reason="decline_other_miniapp")
                    return SceneDecision(
                        action="click", x=center["x"], y=center["y"],
                        reason="弹窗要打开其他小程序，点“取消”留在当前门店小程序",
                        target_label="取消", overlay_visible=True, transaction_visible=False,
                        login_required=False, phone_required=False, consent_required=False,
                    )

        # 这些场景中背景入口即使可 OCR，也不能视为当前可操作目标。
        # 覆盖式弹窗会同时读到被遮挡的菜单价格，先确认是否弹窗，再决定要不要按交易文本短路。
        # 会员中心落地页常把“外卖自提/积分兑换”和“优惠券包免费领取”放在同一屏。
        # “外卖自提”是点餐入口；“预点单”会打开会员社团小程序，不能当菜单入口。
        for label in ("外卖自提", "在线点餐", "自助点餐", "手机点餐"):
            if label not in full_text:
                continue
            resolved = self._find_ocr_target_detail(rows, image_size, label, min_confidence=0.3)
            if not resolved.get("matched"):
                continue
            center = resolved["center_image_px"]
            if self._local_point_consumed(center["x"], center["y"]):
                continue
            self._last_target_resolution.update(
                matched=True, method="local_preorder_entry",
                selected=resolved, reason="member_center_preorder_entry")
            return SceneDecision(
                action="click", x=center["x"], y=center["y"],
                reason=f"会员中心页存在明确的“{label}”入口，点击进入菜单",
                target_label=label, overlay_visible=False, transaction_visible=False,
                login_required=False, phone_required=False, consent_required=False,
            )

        # 会员中心/“我的”页没有菜单入口，底部有“首页”导航时先回首页，
        # 再由首页的“立即点餐/开始点餐”进入（这类页面常被“领取优惠券”文案误判成交易页）。
        home_tab = None
        for label in ("自提", "点餐", "菜单", "首页"):
            home_tab = self._bottom_tab_target(rows, image_size, label)
            if home_tab and not self._local_point_consumed(
                    home_tab["center_image_px"]["x"], home_tab["center_image_px"]["y"]):
                break
            home_tab = None
        if (home_tab and re.search(r"会员|积分", full_text) and not re.search(
                r"立即点餐|开始点餐|开始点单|开始选餐|开始点菜|去点餐|查看菜单|进入菜单", full_text)):
            self._last_target_resolution.update(
                matched=True, method="local_home_tab", selected=home_tab,
                reason="member_or_profile_page_back_home")
            return SceneDecision(
                action="click", x=home_tab["center_image_px"]["x"], y=home_tab["center_image_px"]["y"],
                reason="当前是会员中心/我的页且没有点餐入口，点底部“首页”回到门店首页",
                target_label=str(home_tab.get("matched_ocr", {}).get("text", "首页")),
                overlay_visible=False, transaction_visible=False,
                login_required=False, phone_required=False, consent_required=False,
            )

        overlay_prompt = any(
            _NOTIFICATION_TEXT.search(text)
            or re.search(r"隐私|协议|用户须知|授权|位置信息|地理位置|手机号|快捷登录|一键登录", text)
            for text in normalized_rows)
        if not overlay_prompt and any(_TRANSACTION_TEXT.search(str(row.get("text", ""))) for row in rows):
            self._last_target_resolution.update(scene="transaction_or_marketing", reason="transaction_text_present")
            return None

        error_terms = ("无效的uri", "方案无效", "已暂停服务", "暂停服务", "服务好像出了点问题",
                       "重新加载", "刷新试试看", "无法打开")
        if any(term in full_text for term in error_terms):
            self._last_target_resolution.update(scene="error_or_unavailable", reason="error_page_requires_scene_review")
            return None
        if any(term in full_text for term in ("选择门店", "选这家", "地图找店", "当前城市没有更多门店")):
            self._last_target_resolution.update(scene="store_selection", reason="store_identity_must_be_verified")
            return None

        def choose_exact(labels: tuple[str, ...], scene: str, reason: str, **flags) -> SceneDecision | None:
            candidates: list[tuple[str, dict]] = []
            attempts: list[dict] = []
            for label in labels:
                # 短按钮（同意/允许）在 Apple Vision 里常只有 0.3 置信度，
                # 用 0.8 会把真实按钮全部过滤掉，只能退回模型猜坐标。
                resolution = self._find_ocr_target_detail(rows, image_size, label, min_confidence=0.3)
                attempts.append(resolution)
                if resolution.get("matched"):
                    candidates.append((label, resolution))
                elif resolution.get("reason") == "ambiguous":
                    # 同名文案出现多次（正文一行 + 按钮一行）：全部保留为候选，
                    # 按按钮特征排序后逐个尝试，不必为此调用模型。
                    for row in resolution.get("candidates") or []:
                        box = row.get("box_scaled_ocr_px") or {}
                        try:
                            box_image = {key: float(box[key]) / 3 for key in ("x", "y", "width", "height")}
                            center_image = {"x": box_image["x"] + box_image["width"] / 2,
                                            "y": box_image["y"] + box_image["height"] / 2}
                        except (KeyError, TypeError, ValueError):
                            continue
                        candidates.append((label, {
                            "matched": True, "mode": "exact_text_ambiguous",
                            "reason": "ambiguous_exact_text", "candidate_count": 1,
                            "box_image_px": box_image, "center_image_px": center_image,
                            "confidence": float(row.get("confidence", 0) or 0),
                            "matched_ocr": row,
                        }))
            if not candidates:
                # OCR 常把前后缀并进同一行（如“1人开始点餐”）；唯一包含入口词时才兜底。
                loose: dict[str, dict] = {}
                for label in labels:
                    for item in rows:
                        text = self._normalize_ocr_text(str(item.get("text", "")))
                        # “不同意授权”这类否定文案绝不能因为包含“同意”而被点。
                        if not text or re.search(r"不同意|不允许|拒绝|暂不|取消|不要|关闭", text):
                            continue
                        if label in text and len(text) <= len(label) + 8:
                            loose[text] = item
                if len(loose) == 1:
                    text = next(iter(loose))
                    resolution = self._find_ocr_target_detail(rows, image_size, text, min_confidence=0.3)
                    attempts.append({"loose_match": text, **resolution})
                    if resolution.get("matched"):
                        candidates = [(str(loose[text].get("text", ""))[:120], resolution)]
            self._last_target_resolution.update(scene=scene, attempts=attempts)
            if not candidates:
                self._last_target_resolution["reason"] = "no_exact_candidate"
                return None
            distinct_labels = {self._normalize_ocr_text(label) for label, _ in candidates}
            if len(distinct_labels) != 1:
                # 同时出现“同意/允许”等不同正向文案时语义需要模型判断，本地不做排序猜测。
                self._last_target_resolution["reason"] = "ambiguous_exact_candidates"
                return None

            def button_rank(item: tuple[str, dict]) -> tuple[float, float]:
                resolution = item[1]
                box = resolution.get("box_image_px") or {}
                try:
                    area = float(box.get("width", 0)) * float(box.get("height", 0))
                except (TypeError, ValueError):
                    area = 0.0
                return float(resolution.get("confidence", 0) or 0), area

            ranked = sorted(candidates, key=button_rank, reverse=True)
            baseline = self._button_visual_baseline(image_path, rows, image_size)
            for _, resolution in ranked:
                resolution["visual"] = self._button_visual_state(
                    image_path, resolution.get("box_image_px"), image_size, baseline)
            enabled = [(label, resolution) for label, resolution in ranked
                       if resolution.get("visual", {}).get("available", True)]
            if not enabled:
                # 候选按钮全部是灰底低对比的禁用态：宁可不点，交给模型/人工复核。
                self._last_target_resolution["reason"] = "all_candidates_disabled"
                return None
            ranked = enabled
            self._last_target_resolution["alternatives"] = [
                {"label": label, "x": resolution["center_image_px"]["x"],
                 "y": resolution["center_image_px"]["y"],
                 "method": resolution.get("mode", "exact_text"),
                 "visual": resolution.get("visual")}
                for label, resolution in ranked
            ]
            fresh = [
                (label, resolution) for label, resolution in ranked
                if not any(
                    math.hypot(resolution["center_image_px"]["x"] - point[0],
                               resolution["center_image_px"]["y"] - point[1]) <= 24
                    for point in getattr(self, "_consumed_local_points", [])
                )
            ]
            if not fresh:
                self._last_target_resolution["reason"] = "all_local_candidates_consumed"
                return None
            label, resolution = fresh[0]
            center = resolution["center_image_px"]
            method = "local_exact_button" if len(candidates) == 1 else "local_exact_button_ranked"
            self._last_target_resolution.update(
                matched=True, method=method, selected=resolution, reason="unique_scene_button"
            )
            return SceneDecision(
                action="click", x=center["x"], y=center["y"], reason=reason,
                target_label=label, overlay_visible=True, transaction_visible=False,
                login_required=flags.get("login_required", False),
                phone_required=flags.get("phone_required", False),
                consent_required=flags.get("consent_required", False),
            )

        # 系统通知/权限弹窗只要能关掉就行：优先点拒绝，拒绝按钮置灰时点可用的允许。
        # 是否可点由同屏基线判断，不按“通知就必须拒绝”的固定规则处理。
        if any(_NOTIFICATION_TEXT.search(text) for text in normalized_rows):
            self._last_target_resolution.update(scene="notification_permission")
            baseline = self._button_visual_baseline(image_path, rows, image_size)
            for labels in (_NOTIFICATION_DECLINE, _NOTIFICATION_ALLOW):
                for label in labels:
                    detail = self._find_ocr_target_detail(rows, image_size, label, min_confidence=0.5)
                    if not detail.get("matched"):
                        continue
                    visual = self._button_visual_state(
                        image_path, detail.get("box_image_px"), image_size, baseline)
                    if not visual.get("available", True):
                        continue  # 这个按钮是灰色禁用态，换另一个可用按钮关闭弹窗。
                    center = detail["center_image_px"]
                    self._last_target_resolution.update(
                        matched=True, method="local_notification_button",
                        selected={**detail, "visual": visual},
                        reason=f"notification_click_{label}",
                    )
                    return SceneDecision(
                        action="click", x=center["x"], y=center["y"],
                        reason=f"系统通知弹窗：点击当前可用的“{label}”关闭弹窗",
                        target_label=label, overlay_visible=True, transaction_visible=False,
                        login_required=False, phone_required=False, consent_required=False,
                    )
            self._last_target_resolution["reason"] = "notification_buttons_unavailable"
            return None

        # 授权场景的明确按钮优先于被遮罩页面里的导航文字。
        # 有些弹窗只有“同意并继续/允许”，正文里没有“隐私/协议”字样，也要识别为授权场景。
        authorization_context = any(
            re.search(r"地理位置|位置信息|隐私|协议|用户须知|授权|手机号|登录|申请|"
                      r"同意并继续|同意并授权|同意并开始使用|允许本|始终允许", text)
            for text in normalized_rows)
        if authorization_context:
            self._last_target_resolution.update(scene="authorization_overlay")
            if not auto_authorize:
                self._last_target_resolution["reason"] = "authorization_requires_user"
                return None
            # 顺序要先勾选隐私协议，再点“同意”；直接点同意会弹“请先阅读并同意”。
            consent_box = self._local_consent_checkbox_box(rows, image_size)
            if consent_box is not None and not consent_checked:
                center = self._consent_checkbox_center(image_path, consent_box)
                if not self._consent_box_checked(image_path, center):
                    self._last_target_resolution.update(
                        matched=True, method="local_consent_checkbox_first",
                        reason="授权弹窗要求先勾选隐私协议", selected={**consent_box, "center_image_px": center},
                    )
                    return SceneDecision(
                        action="click", x=center["x"], y=center["y"],
                        reason="授权弹窗要求先勾选隐私协议复选框，再点同意",
                        target_label=consent_box["label"], overlay_visible=True,
                        transaction_visible=False, login_required=False,
                        phone_required=False, consent_required=True,
                    )
            auth = choose_exact(
                ("同意并继续", "同意并授权", "同意并开始使用", "同意", "允许",
                 "手机号快捷登录", "微信手机号快捷登录", "一键登录"),
                "authorization_overlay", "本地 OCR 在授权场景中唯一确认正向授权按钮",
                consent_required=any(term in full_text for term in (
                    "地理位置", "位置信息", "隐私", "协议", "授权",
                    "同意并继续", "同意并授权", "同意并开始使用")),
                phone_required="手机号" in full_text,
                login_required="登录" in full_text,
            )
            if auth is not None:
                return auth
            # 整个弹窗只有一行正向文案（如“授权后进入”）时，本地唯一确认后直接点。
            if len(rows) == 1:
                text = self._normalize_ocr_text(str(rows[0].get("text", "")))
                if (0 < len(text) <= 12
                        and re.search(r"同意|允许|授权|一键登录|进入", text)
                        and not re.search(r"不同意|不允许|拒绝|暂不|取消", text)):
                    resolved = self._find_ocr_target_detail(rows, image_size, text, min_confidence=0.3)
                    if resolved.get("matched"):
                        self._last_target_resolution.update(
                            matched=True, method="local_authorization_single_label",
                            selected=resolved, reason="授权弹窗只有一行正向按钮文案")
                        center = resolved["center_image_px"]
                        return SceneDecision(
                            action="click", x=center["x"], y=center["y"],
                            reason="授权弹窗只有一行正向按钮文案；本地唯一确认",
                            target_label=str(rows[0].get("text", ""))[:120], overlay_visible=True,
                            transaction_visible=False, login_required=False,
                            phone_required=False, consent_required=True,
                        )
            # 正向按钮被 OCR 漏检时，用同一行的“拒绝/不同意”推算按钮位置。
            paired = self._paired_consent_target(rows, image_size)
            if paired is not None:
                self._last_target_resolution.update(
                    matched=True, method="local_consent_pair_geometry",
                    reason="正向按钮未被 OCR 检出；按拒绝按钮同行右侧推算",
                    selected=paired,
                )
                return SceneDecision(
                    action="click", x=paired["x"], y=paired["y"],
                    reason="正向授权按钮未被 OCR 检出；按“拒绝”按钮同一行右侧推算位置",
                    target_label="同意", overlay_visible=True, transaction_visible=False,
                    login_required=False, phone_required=False, consent_required=True,
                )
            # 不能因协议正文中出现“授权”就猜一个文字框当按钮。
            return None

        # 人数页可能没有“用餐人数”提示词，只剩 1–12 的网格；纯价格数字（>12）不参与判定。
        number_labels = {
            int(match.group(1))
            for text in normalized_rows
            if (match := re.fullmatch(r"(\d{1,2})(?:人|位)?", text))
        }
        people_grid = len(number_labels) >= 3 and all(1 <= value <= 12 for value in number_labels)
        people_context = people_grid or any(
            re.search(r"几[人位]|用餐人数|就餐人数|选择人数|请选择人数", text)
            for text in normalized_rows)
        if people_context:
            self._last_target_resolution.update(scene="people_selection")
            if not people_selected:
                people_resolution = self._find_ocr_target_detail(
                    rows, image_size, str(people_count), people_count, require_people_grid=True
                )
                self._last_target_resolution["attempts"] = [people_resolution]
                point = people_resolution.get("center_image_px") if people_resolution.get("matched") else None
                if point is None:
                    grid_target = self._local_people_grid_target(ocr_result, people_count)
                    if grid_target is not None:
                        return grid_target
                    self._last_target_resolution["reason"] = "configured_person_not_unique"
                    return None
                self._last_target_resolution.update(
                    matched=True, method="local_people_grid_ocr", selected=people_resolution,
                    reason="unique_people_grid_target")
                return SceneDecision(
                    action="click", x=point["x"], y=point["y"], reason="本地 OCR 唯一确认配置的用餐人数",
                    target_label=str(people_count), overlay_visible=True, transaction_visible=False,
                    login_required=False, phone_required=False, consent_required=False,
                )
            # 选择状态由后续画面确认；这里只允许精确的菜单入口文案。
            start = choose_exact(("开始点餐", "开始点单", "开始选餐", "开始点菜", "去点餐", "查看菜单", "进入菜单"),
                                 "people_selection", "人数已由当前画面验证，定位菜单入口")
            if start is not None:
                return start
            return self._local_people_confirm_target(ocr_result, people_count)

        # 促销、遮挡和未识别页面不允许点到底栏或背景里的“点餐”。
        if any(term in full_text for term in ("优惠券", "优惠活动", "立即领取", "抽免单", "广告", "活动规则")):
            self._last_target_resolution.update(scene="promotional_overlay", reason="foreground_overlay_requires_model")
            return None

        entry = choose_exact(("开始点餐", "开始点单", "开始选餐", "开始点菜", "去点餐", "查看菜单", "进入菜单"),
                             "welcome_or_menu_entry", "本地 OCR 唯一确认只读菜单入口")
        return entry

    @classmethod
    def _local_consent_checkbox_box(cls, rows: list[dict], image_size: tuple[int, int]) -> dict | None:
        """找“我已了解/已阅读并同意…”这类协议复选框标签行。"""
        factor = 3
        width, height = image_size
        best: dict | None = None
        for item in rows:
            text = str(item.get("text", ""))
            normalized = cls._normalize_ocr_text(text)
            if not normalized:
                continue
            # 只认明确的复选框标签。正文里的“不同意相关协议”“如您同意”不能当标签，
            # 否则会把点击落到正文段落上。协议页常见换行断句，勾选框与“允许我们…已阅读”同一行。
            if not re.search(r"我已了解|我已阅读|已阅读并同意|我已同意|我已勾选|已勾选并同意|"
                             r"允许我们.{0,40}(已阅读|个人信息|必要场景)", normalized):
                continue
            if re.search(r"不同意|不允许|拒绝|暂不", normalized):
                continue
            try:
                confidence = float(item.get("confidence", 0))
                x, y, box_w, box_h = (float(item[key]) for key in ("x", "y", "width", "height"))
            except (KeyError, TypeError, ValueError):
                continue
            if confidence < 0.3 or box_w <= 0 or box_h <= 0:
                continue
            x, y, box_w, box_h = x / factor, y / factor, box_w / factor, box_h / factor
            if x < 0 or y < 0 or x + box_w > width + 1 or y + box_h > height + 1:
                continue
            candidate = {"label": text[:120], "x": x, "y": y, "width": box_w, "height": box_h,
                         "confidence": confidence}
            if best is None or candidate["y"] < best["y"]:
                best = candidate
        return best

    @staticmethod
    def _consent_checkbox_center(image_path: Path | None, box: dict) -> dict:
        """复选框可点区域是标签左侧的小方框；找不到方框时退回标签行左缘。"""
        # OCR 把行首的空心圆/方框读成 C、○、口 时，勾选框就在文字框最左端。
        if re.match(r"^[cCoO○◯☐□口]", str(box.get("label", "")).strip()):
            return {"x": box["x"] + box["height"] * 0.55, "y": box["y"] + box["height"] / 2}
        if image_path is not None and Path(image_path).is_file():
            match = refine_consent_checkbox(image_path, box["x"], box["y"], box["height"], box["width"])
            if match:
                return match["center_image_px"]
        return {"x": max(0.0, box["x"] - 8.0), "y": box["y"] + box["height"] / 2}

    @staticmethod
    def _consent_box_checked(image_path: Path | None, center: dict) -> bool:
        """已勾选的协议框是实心色块；空心圆环中心为白色，不能重复勾选。"""
        if image_path is None or not Path(image_path).is_file():
            return False
        try:
            with Image.open(image_path) as image:
                gray = image.convert("L")
                x, y = int(round(center["x"])), int(round(center["y"]))
                patch = gray.crop((max(0, x - 6), max(0, y - 6), x + 7, y + 7))
                return float(ImageStat.Stat(patch).mean[0]) < 210.0
        except (OSError, ValueError):
            return False

    @classmethod
    def _paired_consent_target(cls, rows: list[dict], image_size: tuple[int, int]) -> dict | None:
        """正向按钮被 OCR 漏检时，用同一行的“拒绝/不同意”推算按钮中心。"""
        factor = 3
        width, height = image_size
        boxes: list[dict] = []
        negatives: list[dict] = []
        for item in rows:
            text = cls._normalize_ocr_text(str(item.get("text", "")))
            try:
                confidence = float(item.get("confidence", 0))
                x, y, box_w, box_h = (float(item[key]) for key in ("x", "y", "width", "height"))
            except (KeyError, TypeError, ValueError):
                continue
            box = {"x": x / factor, "y": y / factor, "width": box_w / factor, "height": box_h / factor}
            boxes.append(box)
            if confidence >= 0.5 and re.fullmatch(r"拒绝|不同意|不允许|暂不", text):
                negatives.append({**box, "text": text})
        if len(negatives) != 1:
            return None
        negative = negatives[0]
        center_y = negative["y"] + negative["height"] / 2
        right = negative["x"] + negative["width"]
        # 常见授权弹窗是左右并排的两个等宽按钮，中心间距约 110–140px。
        for gap in (125.0, 115.0, 135.0, 105.0, 145.0):
            x = right + gap
            if not (10 < x < width - 10):
                continue
            # 推断点周围不能已有识别文字，否则可能落在正文上。
            if any(box["x"] - 10 < x < box["x"] + box["width"] + 10 and
                   box["y"] - 10 < center_y < box["y"] + box["height"] + 10 for box in boxes):
                continue
            return {"x": x, "y": center_y, "gap": gap, "negative": negative,
                    "screenshot_size_px": {"width": width, "height": height}}
        return None

    @staticmethod
    def _sheet_close_point(path: Path) -> tuple[float, float] | None:
        """优惠券/活动浮层没有文字关闭键，按“浮层顶边右上角”的固定版式定位关闭 X。"""
        try:
            with Image.open(path) as image:
                gray = image.convert("L")
                width, height = gray.size
                pixels = gray.load()
                step = max(1, width // 80)
                columns = list(range(1, width - 1, step))
                if not columns:
                    return None
                for y in range(int(height * 0.45), height - 3):
                    white = sum(1 for x in columns if pixels[x, y] >= 250)
                    if white >= len(columns) * 0.85:
                        close_y = min(height - 2.0, y + max(10.0, width * 0.07))
                        return (width - max(8.0, width * 0.06), close_y)
        except Exception:
            return None
        return None

    def _ocr_target_point(self, ocr_result, label: str) -> dict | None:
        """按当前帧 OCR 精确找一个按钮文字，返回截图坐标中心。"""
        if not ocr_result or ocr_result[0] is None or not ocr_result[1]:
            return None
        detail = self._find_ocr_target_detail(ocr_result[1], ocr_result[0], label, min_confidence=0.3)
        return detail.get("center_image_px") if detail.get("matched") else None

    def _local_ready_preflight(self, path: Path, source_name: str, ocr_result=None) -> SceneDecision | None:
        if self.ocr_reader is None:
            return None
        candidate = SceneDecision(action="wait", reason="本地菜单锚点检查", overlay_visible=False,
                                  login_required=False, phone_required=False, consent_required=False,
                                  transaction_visible=False)
        resolved = self._resolve_ready_region(path, candidate, source_name, ocr_result)
        identity_unconfirmed = "门店标识未确认" in resolved.reason
        if ocr_result and ocr_result[1]:
            # 底部优惠券/登录浮层不会让整页变暗，但会吞掉滚动手势，导致只截到首屏。
            # 出现这类文案时把页面交给模型确认并点掉关闭，不直接判就绪。
            ready_text = " ".join(str(row.get("text", "")) for row in ocr_result[1])
            if "小伙伴已在美团在线点菜" in ready_text or "留在小程序" in ready_text:
                # 平台提示浮层会吞掉滚动手势，只截到首屏；点“留在小程序”回到菜单。
                point = self._ocr_target_point(ocr_result, "留在小程序")
                if point is not None:
                    return SceneDecision(
                        action="click", x=point["x"], y=point["y"],
                        reason="美团在线点菜提示遮挡菜单，先点“留在小程序”关闭提示",
                        target_label="留在小程序", overlay_visible=True,
                        transaction_visible=False, login_required=False,
                        phone_required=False, consent_required=False,
                    )
                return None
            if "待支付的订单" in ready_text and "继续加购" in ready_text:
                # 桌码已有待支付订单时的提示；点“继续加购”回到菜单，不触发支付。
                point = self._ocr_target_point(ocr_result, "继续加购")
                if point is not None:
                    return SceneDecision(
                        action="click", x=point["x"], y=point["y"],
                        reason="待支付订单提示遮挡菜单，先点“继续加购”回到菜单",
                        target_label="继续加购", overlay_visible=True,
                        transaction_visible=False, login_required=False,
                        phone_required=False, consent_required=False,
                    )
                return None
            if re.search(r"同意此应用使用你的地理位置|申请使用你的地理位置|"
                         r"获取你的地理位置|需开启.{0,6}位置", ready_text):
                # 微信定位授权弹窗是应用级模态，会挡住滚动；没有定位也能看菜单，优先拒绝。
                for label in ("拒绝", "不允许", "允许", "同意"):
                    point = self._ocr_target_point(ocr_result, label)
                    if point is not None:
                        return SceneDecision(
                            action="click", x=point["x"], y=point["y"],
                            reason=f"地理位置授权弹窗遮挡菜单，先点“{label}”关闭弹窗",
                            target_label=label, overlay_visible=True,
                            transaction_visible=False, login_required=False,
                            phone_required=False, consent_required=False,
                        )
                return None
            if re.search(r"会员充值|充值活动", ready_text):
                # 会员充值活动弹窗会整块盖住菜单且吞掉滚动，必须先点圆形关闭键。
                located = locate_close_icon(Path(path))
                center = (located or {}).get("center_image_px") or {}
                if center.get("x") is not None:
                    return SceneDecision(
                        action="click", x=center["x"], y=center["y"],
                        reason="会员充值活动弹窗遮挡菜单，先点弹窗关闭键",
                        target_label="浮层收起按钮", overlay_visible=True,
                        transaction_visible=False, login_required=False,
                        phone_required=False, consent_required=False,
                    )
                return None
            if re.search(r"立即领取|免费领取|领取优惠|开卡赠送", ready_text):
                point = self._sheet_close_point(path)
                if point is not None:
                    return SceneDecision(
                        action="click", x=point[0], y=point[1],
                        reason="优惠券浮层挡住菜单滚动，先点浮层右上角关闭",
                        target_label="浮层收起按钮", overlay_visible=True,
                        transaction_visible=False, login_required=False,
                        phone_required=False, consent_required=False,
                    )
                return None
            if re.search(r"注册/?登录|开通会员|同意并继续|"
                         r"服务协议|隐私政策|用户协议|未成年人隐私", ready_text):
                # 协议/授权弹窗未关闭时不能判就绪，否则弹窗会被截进菜单且滚动被吞。
                return None
        if not resolved.ready_evidence_ok:
            return None
        if not identity_unconfirmed and not self._store_matches(source_name, resolved.observed_store_text):
            return None

        # 菜单栏与底部导航均未变暗才认为菜单没有被半透明弹窗遮挡；暗色主题交给模型。
        # 已经读到 ≥3 个菜名和 ≥3 个价格时，证据强于亮度启发式，直接放行。
        strong_evidence = (len(resolved.item_names_visible) >= 3
                           and len(resolved.prices_visible) >= 3)
        if not strong_evidence:
            with Image.open(path) as image:
                gray = image.convert("L")
                w, h = gray.size
                samples = [gray.getpixel((max(0, round(w * .01)), min(h - 1, round(h * y)))) for y in (.44, .52, .60, .68, .97)]
            if sorted(samples)[len(samples) // 2] < 200:
                return None
        return resolved

    @staticmethod
    def _expected_button_kind(label: str, reason: str) -> str:
        """按模型给的场景与目标描述推断这一步大概率要点的按钮类型。"""
        text = f"{label} {reason}"
        if re.search(r"关闭|close|dismiss|叉号|×", text, re.I):
            return "close"
        if re.search(r"不允许|拒绝|暂不", text):
            return "decline"
        if re.search(r"确定|确认|完成|开始点餐|去点餐|查看菜单|立即点餐", text):
            return "confirm"
        if re.search(r"允许|同意|授权|登录|继续", text):
            return "allow"
        if re.search(r"人数|就餐人数|几位", text):
            return "people"
        return "any"

    def _local_button_candidates_near(self, image_path: Path, decision: SceneDecision) -> dict | None:
        """在模型粗坐标周围放大重识别，按场景期望打分，只返回唯一最高分候选。

        模型给的候选编号不合法时，它的 x/y 仍可能指向真实按钮；这里不编造编号，
        而是重读该区域，拿文字、距离和按钮可用性共同打分。两个候选分数接近时
        视为不确定，宁可返回 None 交给下一轮，也不猜一个点击。
        """
        if self.ocr_reader is None or not all(
                isinstance(value, (int, float)) and math.isfinite(float(value))
                for value in (decision.x, decision.y)):
            return None
        cached_size = (self._active_ocr_result or (None, []))[0]
        temp_path: Path | None = None
        try:
            with Image.open(image_path) as opened:
                image = opened.convert("RGB")
                width, height = image.size
                size = cached_size or (width, height)
                half_w = max(90, min(220, int(width * .55)))
                half_h = max(60, min(180, int(height * .28)))
                left = max(0, int(round(decision.x)) - half_w)
                top = max(0, int(round(decision.y)) - half_h)
                right = min(width, int(round(decision.x)) + half_w)
                bottom = min(height, int(round(decision.y)) + half_h)
                if right - left < 24 or bottom - top < 16:
                    return None
                scale = 4
                crop = image.crop((left, top, right, bottom))
                crop = crop.resize((crop.width * scale, crop.height * scale),
                                   Image.Resampling.LANCZOS)
            fd, name = tempfile.mkstemp(prefix="roi-ocr-", suffix=".png",
                                        dir=str(Path(image_path).parent))
            os.close(fd)
            temp_path = Path(name)
            temp_path.chmod(0o600)
            crop.save(temp_path, format="PNG")
            rows = self.ocr_reader(temp_path)
        except Exception:
            return None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        if not isinstance(rows, list) or not rows:
            return None
        kind = self._expected_button_kind(decision.target_label, decision.reason)
        expected = _EXPECTED_BUTTON_TEXT.get(kind, ())
        norm_label = self._normalize_ocr_text(decision.target_label or "")
        baseline = self._button_visual_baseline(
            image_path, (self._active_ocr_result or (None, []))[1], size)
        ranked: list[dict] = []
        for item in rows:
            text = self._normalize_ocr_text(str(item.get("text", "")))
            if not text or len(text) > 14:
                continue
            try:
                confidence = float(item.get("confidence") or 0)
                raw_x = float(item["x"])
                raw_y = float(item["y"])
                raw_w = float(item["width"])
                raw_h = float(item["height"])
            except (KeyError, TypeError, ValueError):
                continue
            # OCR 结果必须是这张 ROI 图的坐标；越界说明拿到的是别的帧，直接丢弃。
            if not (0 <= raw_x <= crop.width and 0 <= raw_y <= crop.height
                    and 0 < raw_w <= crop.width and 0 < raw_h <= crop.height):
                continue
            x = left + raw_x / scale
            y = top + raw_y / scale
            box_w = raw_w / scale
            box_h = raw_h / scale
            if confidence < .3 or box_w < 4 or box_h < 4:
                continue
            box = {"x": x, "y": y, "width": box_w, "height": box_h}
            center = {"x": x + box_w / 2, "y": y + box_h / 2}
            score = 0.0
            if expected and any(word in text for word in expected):
                score += .60
            elif norm_label:
                score = max(score, .45 * SequenceMatcher(None, norm_label, text).ratio())
            distance = math.hypot(center["x"] - decision.x, center["y"] - decision.y)
            score += .25 * max(0.0, 1 - distance / max(half_w, half_h))
            visual = self._button_visual_state(image_path, box, size, baseline)
            if not visual.get("available", True):
                score -= .30
            ranked.append({"text": text, "box_image_px": box, "center_image_px": center,
                           "confidence": round(confidence, 3), "score": round(score, 3),
                           "visual": visual})
        ranked.sort(key=lambda item: item["score"], reverse=True)
        alternatives = [
            {"text": item["text"], "score": item["score"],
             "x": round(item["center_image_px"]["x"], 1),
             "y": round(item["center_image_px"]["y"], 1), "visual": item["visual"]}
            for item in ranked[:6]
        ]
        if not ranked or ranked[0]["score"] < .55:
            self._last_target_resolution = {
                "matched": False, "method": "local_roi_button", "target": decision.target_label,
                "reason": "no_confident_button", "alternatives": alternatives,
            }
            return None
        if len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < .15:
            self._last_target_resolution = {
                "matched": False, "method": "local_roi_button", "target": decision.target_label,
                "reason": "ambiguous_button", "alternatives": alternatives,
            }
            return None
        best = dict(ranked[0])
        best["alternatives"] = alternatives
        return best

    def _resolve_click_coordinates(self, image_path: Path, decision: SceneDecision, people_count: int) -> SceneDecision:
        """文字用 OCR 框，关闭图标用局部形状精定位；均复用模型所看的同一张图。"""
        if decision.action != "click":
            return decision
        if re.search(r"关闭|close|dismiss|^×$|^x$", decision.target_label, re.IGNORECASE):
            match = refine_close_icon(image_path, decision.x, decision.y)
            method = "local_visual_close_icon"
            if match is None and decision.overlay_visible:
                # 广告弹窗的关闭键常在弹窗下方较远处，超出局部范围；有弹窗时允许整页找一次。
                match = locate_close_icon(image_path, decision.x, decision.y)
                method = "page_visual_close_icon"
            self._last_target_resolution = {
                "matched": match is not None, "method": method,
                "target": decision.target_label, "coarse_image_px": {"x": decision.x, "y": decision.y},
                **(match or {}),
            }
            if match:
                return decision.model_copy(update=match["center_image_px"])
        # 微信地理位置权限条里“同意此应用使用你的地理位置”只是标题，真正可点的是同屏的
        # “允许”。模型偶尔会把标题当按钮，勾选框启发式还会把点击推到标题最左侧（点空），
        # 因此这里直接按 OCR 改点当前帧的允许按钮。
        if re.search(r"地理位置|位置信息|申请使用", decision.target_label or "") and self.ocr_reader is not None:
            cached = self._ocr_result_for(image_path)
            image_size, rows = cached if cached is not None else self._read_scaled_ocr(image_path)
            if image_size is not None:
                for label in ("允许", "始终允许", "仅在使用中允许", "同意"):
                    detail = self._find_ocr_target_detail(rows, image_size, label, min_confidence=0.3)
                    if detail.get("matched"):
                        self._last_target_resolution = {
                            "matched": True, "method": "local_geolocation_allow",
                            "target": decision.target_label, "selected": detail,
                        }
                        return decision.model_copy(update=detail["center_image_px"])
                self._last_target_resolution = {
                    "matched": False, "method": "local_geolocation_allow",
                    "target": decision.target_label, "reason": "allow_button_not_found",
                }
        if re.search(r"我已了解|已阅读|同意|隐私|协议|政策|用户须知|知悉|接受", decision.target_label) \
                and not re.search(r"地理位置|位置信息", decision.target_label):
            # 独立按钮（同意/允许）本身就是要点的区域，不能改点左侧勾选框。
            button_word = re.sub(r"\s+", "", decision.target_label or "") in {
                "同意", "允许", "继续", "确定", "确认", "同意并继续", "立即进入", "开始使用",
            }
            if not button_word:
                # 隐私协议勾选框的可点区域通常是文字左侧的小方框，不是文字本身。
                # 模型不返回 target_box 时，用同帧 OCR 的标签行补上，避免精定位失效。
                label_box = decision.target_box
                box_source = "model_target_box"
                if label_box is None or label_box.width < 8 or label_box.height < 4:
                    located = self._locate_consent_label_box(decision.target_label)
                    if located is not None:
                        label_box = Region(**located)
                        box_source = "local_ocr_label_box"
                if label_box is not None:
                    match = refine_consent_checkbox(
                        image_path, label_box.x, label_box.y, label_box.height, label_box.width,
                    )
                    self._last_target_resolution = {
                        "matched": match is not None, "method": "local_consent_checkbox",
                        "target": decision.target_label, "label_box_source": box_source,
                        "label_box": label_box.model_dump(),
                        **(match or {}),
                    }
                    if match:
                        return decision.model_copy(update=match["center_image_px"])
        if decision.x is not None and decision.y is not None and "返回" in decision.target_label:
            # 返回箭头没有文字，OCR 帮不上；模型纵向常点偏，用同帧左上角笔画精定位。
            match = refine_back_arrow(image_path, decision.x, decision.y)
            self._last_target_resolution = {
                "matched": match is not None, "method": "local_back_arrow",
                "target": decision.target_label,
                "coarse_image_px": {"x": decision.x, "y": decision.y},
                **(match or {}),
            }
            if match:
                return decision.model_copy(update=match["center_image_px"])
        if self.ocr_reader is None:
            return decision
        cached = self._ocr_result_for(image_path)
        image_size, rows = cached if cached is not None else self._read_scaled_ocr(image_path)
        self._last_ocr_meta["cache_reused"] = cached is not None
        if image_size is None:
            return decision
        if self._is_people_count_choice(decision.target_label, decision.reason, people_count):
            target = str(people_count)
            require_people_grid = True
        else:
            target = self._normalize_ocr_text(decision.target_label)
            require_people_grid = False
        resolution = self._find_ocr_target_detail(
            rows, image_size, target, people_count if require_people_grid else None, require_people_grid,
            min_confidence=0.3 if not require_people_grid else 0.8,
        )
        self._last_target_resolution = resolution
        match = resolution.get("center_image_px") if resolution.get("matched") else None
        if match is None:
            # 整帧 OCR 没覆盖目标：在模型粗坐标周围放大重识别，按当前场景期望选唯一候选。
            nearby = self._local_button_candidates_near(image_path, decision)
            if nearby is None:
                return decision
            self._last_target_resolution = {
                "matched": True, "method": "local_roi_button", "target": decision.target_label,
                "score": nearby["score"], "text": nearby["text"],
                "box_image_px": nearby["box_image_px"],
                "center_image_px": nearby["center_image_px"],
                "alternatives": nearby.get("alternatives"),
            }
            return decision.model_copy(update={"x": nearby["center_image_px"]["x"],
                                               "y": nearby["center_image_px"]["y"]})
        return decision.model_copy(update={"x": match["x"], "y": match["y"]})

    def _locate_consent_label_box(self, label: str) -> dict | None:
        """从同帧 OCR 找协议标签行（image px），模型没给 target_box 时用。"""
        image_size, rows = self._active_ocr_result if self._active_ocr_result else (None, [])
        if image_size is None or not rows:
            return None
        target = self._normalize_ocr_text(label)
        if not target:
            return None
        best: dict | None = None
        best_score = 0.0
        for item in rows:
            text = self._normalize_ocr_text(str(item.get("text", "")))
            if not text:
                continue
            score = SequenceMatcher(None, target, text).ratio()
            if target in text or text in target:
                score = max(score, 0.95)
            if score > best_score:
                best_score, best = score, item
        if best is None or best_score < 0.6:
            return None
        try:
            x, y, width, height = (float(best[key]) / 3 for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return {"x": x, "y": y, "width": width, "height": height}

    @staticmethod
    def _target_candidates(ocr_result) -> list[dict]:
        """从当前帧 OCR 生成候选框；模型只选编号，执行端取框中心。"""
        image_size, rows = ocr_result if ocr_result else (None, [])
        if image_size is None or not rows:
            return []
        return build_text_candidates(rows, image_size)

    def _ocr_result_for(self, image_path: Path):
        if self._active_ocr_path == str(Path(image_path).resolve()):
            return self._active_ocr_result
        return None

    @staticmethod
    def _clamp_bottom_overflow(decision, size: tuple[int, int]):
        """模型区域只在竖直方向超出画面底部时，按可见底边裁剪。

        列表式菜单常把整块内容底部算到画面外；横向越界通常是坐标空间用错，
        不能顺手截断，仍交给几何校验拒绝。
        """
        region = getattr(decision, "menu_region", None)
        if region is None or decision.action != "ready":
            return decision
        width, height = size
        if region.x + region.width > width + 0.5 or region.y >= height:
            return decision
        if region.y + region.height - height <= 0.5:
            return decision
        if height - region.y < height * 0.2 or region.width < width * 0.2:
            return decision
        return decision.model_copy(update={"menu_region": Region(
            x=region.x, y=region.y, width=region.width, height=height - region.y)})

    def _task_store_parts(self) -> tuple[str, str]:
        """从任务名解析（城市, 门店名）；格式如 ZA...＿品牌＿北京市＿品牌（总店）。"""
        name = str((getattr(self, "current_task", None) or {}).get("source_name") or "")
        parts = [part for part in name.split("＿") if part]
        if len(parts) < 3:
            return "", ""
        return parts[-2], parts[-1]

    def _store_row_target(self, rows: list[dict], image_size: tuple[int, int],
                          store_name: str) -> tuple[dict | None, bool]:
        """在门店列表里找任务门店；找不到返回 (None, True) 表示存在同名品牌但门店不符。"""
        brand = re.split(r"[（(]", store_name)[0].strip()
        branch = store_name[len(brand):].strip("（）() ")
        if not brand:
            return None, False
        norm = self._normalize_ocr_text
        ordered = sorted(rows or [], key=lambda row: float(row.get("y", 0) or 0))
        brand_rows = []
        for index, row in enumerate(ordered):
            text = norm(str(row.get("text", "")))
            if brand not in text:
                continue
            # 门店名常被 OCR 拆成两行（“一坐一忘（杭州西湖” + “店）”），
            # 把同列紧邻的下一行拼起来再比对分店名。
            combined = text
            for following in ordered[index + 1:]:
                try:
                    gap = float(following["y"]) - float(row["y"])
                    same_column = abs(float(following["x"]) - float(row["x"])) < 60
                except (KeyError, TypeError, ValueError):
                    continue
                if gap >= 160:
                    break
                if same_column:
                    combined += norm(str(following.get("text", "")))
            brand_rows.append((row, combined))
        if not brand_rows:
            return None, False
        matched = None
        for row, combined in brand_rows:
            if branch and branch not in combined:
                continue
            matched = row
            break
        if matched is None:
            return None, True

        def center(row: dict) -> tuple[dict, float]:
            x, y = float(row["x"]), float(row["y"])
            box_w, box_h = float(row["width"]), float(row["height"])
            return {"x": (x + box_w / 2) / 3, "y": (y + box_h / 2) / 3}, y + box_h / 2

        try:
            best, base_y = center(matched)
        except (KeyError, TypeError, ValueError):
            return None, False
        orders = []
        for row in rows or []:
            if norm(str(row.get("text", ""))) != "去下单":
                continue
            try:
                order_center, order_y = center(row)
            except (KeyError, TypeError, ValueError):
                continue
            orders.append((abs(order_y - base_y), order_center))
        if orders:
            orders.sort(key=lambda item: item[0])
            if orders[0][0] <= 300:
                best = orders[0][1]
        return {"center_image_px": best, "matched": True}, False

    def _local_point_consumed(self, x: float, y: float) -> bool:
        """同一位置刚点过且无变化时，不再重复本地点击。"""
        return any(math.hypot(float(x) - point[0], float(y) - point[1]) <= 24
                   for point in getattr(self, "_consumed_local_points", []))

    @staticmethod
    def _bottom_tab_target(rows: list[dict], image_size: tuple[int, int], label: str) -> dict | None:
        """在底部导航区找目标 tab；小字 OCR 常把“首页”读成“首頭”，按相似度容忍。"""
        width, height = image_size
        best: tuple[float, dict] | None = None
        for row in rows or []:
            text = UiPathRunner._normalize_ocr_text(str(row.get("text", "")))
            if not text or len(text) > 4:
                continue
            if label not in text and SequenceMatcher(None, text, label).ratio() < 0.5:
                continue
            try:
                x, y = float(row["x"]), float(row["y"])
                box_w, box_h = float(row["width"]), float(row["height"])
            except (KeyError, TypeError, ValueError):
                continue
            center_y = y + box_h / 2
            # OCR 行坐标是窗口尺寸的 3 倍；只认画面底部 12% 的导航区。
            if center_y < height * 3 * 0.88:
                continue
            center_x = (x + box_w / 2) / 3
            # 底栏贴着窗口下沿，点最底部容易落到圆角/安全区外，统一上移一点。
            tab_y = min(center_y / 3, height - 40)
            if best is None or center_y > best[0]:
                best = (center_y, {
                    "matched": True, "mode": "bottom_tab_fuzzy",
                    "box_image_px": {"x": x / 3, "y": y / 3, "width": box_w / 3, "height": box_h / 3},
                    "center_image_px": {"x": center_x, "y": tab_y},
                    "confidence": float(row.get("confidence", 0) or 0),
                    "matched_ocr": row,
                })
        return best[1] if best else None

    def _resolve_ready_region(self, image_path: Path, decision: SceneDecision, expected_source_name: str = "",
                              ocr_result=None, *, _resting_inference: bool = False) -> SceneDecision:
        """先落安全诊断，再用强OCR锚点确认菜单并推导右侧滚动区域。"""
        self._record_scene_inspection(image_path, decision)
        if decision.action == "blocked":
            resting_evidence = self._resting_menu_ocr_evidence(image_path, decision, ocr_result)
            if resting_evidence is None:
                return decision
            # 只有“明确休息”理由、同帧休息提示且本地 OCR 找到菜单列/菜名/价格时，
            # 才把 blocked 送进同一套区域 resolver；这不会授权点击或绕过遮挡检查。
            inferred = decision.model_copy(update={
                "action": "wait",
                "categories": resting_evidence["categories"],
                "item_names_visible": resting_evidence["item_names_visible"],
                "prices_visible": resting_evidence["prices_visible"],
            })
            resolved = self._resolve_ready_region(
                image_path, inferred, expected_source_name, ocr_result, _resting_inference=True
            )
            # 保留原始 blocked，直到标准 resolver 实际给出滚动区域并完成 ready 证据校验。
            return resolved if resolved.action == "ready" and resolved.ready_evidence_ok else decision
        image_size = None
        rows: list[dict] = []
        if decision.overlay_visible and decision.action == "ready":
            # 休息提示常被模型当成遮挡；只有同帧 OCR 明确出现休息提示且模型已给出
            # 菜名/价格证据时，才允许本地菜单锚点继续推导区域，不能放行真实广告遮挡。
            if ocr_result is not None:
                image_size, rows = ocr_result
            elif self.ocr_reader is not None:
                image_size, rows = self._read_scaled_ocr(image_path)
            resting_notice = any(
                term in self._normalize_menu_name(str(item.get("text", "")))
                for item in rows
                for term in ("已休息", "休息中", "打烊", "非营业", "暂停接单")
            )
            if not (resting_notice and decision.item_names_visible and decision.prices_visible):
                return decision
            decision = decision.model_copy(update={"overlay_visible": False})
        elif (
            decision.overlay_visible
            or decision.login_required
            or decision.phone_required
            or decision.consent_required
            or decision.transaction_visible
        ):
            return decision
        can_promote = decision.action in {"click", "scroll", "wait"}
        if can_promote and (_TRANSACTION_TEXT.search(decision.target_label) or _AUTHORIZATION_TEXT.search(decision.target_label)):
            return decision
        if decision.action != "ready" and not can_promote:
            return decision
        if self.ocr_reader is None:
            return decision
        if image_size is None:
            image_size, rows = ocr_result if ocr_result is not None else self._read_scaled_ocr(image_path)
        if image_size is None:
            return decision
        width, height = image_size
        categories = {self._normalize_menu_name(value) for value in decision.categories if self._normalize_menu_name(value)}
        names = {self._normalize_menu_name(value) for value in decision.item_names_visible if self._normalize_menu_name(value)}
        prices = {price for value in decision.prices_visible for price in self._price_keys(value)}

        anchors: list[dict] = []
        bottom_navigation: list[float] = []
        nav_labels = {"首页", "扫码点餐", "我的"}
        for item in rows:
            text = unicodedata.normalize("NFKC", str(item.get("text", ""))).strip()
            normalized_text = self._normalize_menu_name(text)
            try:
                confidence = float(item.get("confidence", 0))
                x, y, box_width, box_height = (float(item[key]) for key in ("x", "y", "width", "height"))
            except (KeyError, TypeError, ValueError):
                continue
            scaled_width, scaled_height = width * 3, height * 3
            if (
                not math.isfinite(confidence) or confidence <= 0
                or not all(math.isfinite(value) for value in (x, y, box_width, box_height))
                or box_width <= 0 or box_height <= 0 or x < 0 or y < 0
                or x + box_width > scaled_width or y + box_height > scaled_height
            ):
                continue
            left, top = x / 3, y / 3
            box_right, box_bottom = (x + box_width) / 3, (y + box_height) / 3
            center_x, center_y = (left + box_right) / 2, (top + box_bottom) / 2
            if normalized_text in nav_labels and center_y >= height * 0.72 and confidence >= 0.8:
                bottom_navigation.append(top)
            anchor = {
                "text": text, "normalized": normalized_text,
                "x": left, "y": top, "right": box_right, "bottom": box_bottom,
                "center_x": center_x, "center_y": center_y, "confidence": confidence,
            }
            anchors.append(anchor)

        local_fallback = None
        structure_evidence = False
        if not categories and not names and not prices:
            local_fallback = self._local_menu_template_anchors(anchors, width, height, expected_source_name)
            if local_fallback is not None:
                left_categories = local_fallback["categories"]
                categories = {anchor["normalized"] for anchor in left_categories}
                dish_anchors = [local_fallback["dish"]]
                price_anchors = [local_fallback["price"]]
                prices = self._price_keys(local_fallback["price"]["text"])
            else:
                # 门店名、底栏模板都不成立时，再退一步看双列结构本身是否足够像菜单页。
                structure = self._local_dual_column_menu_evidence(anchors, width, height)
                if structure is None:
                    return self._clamp_bottom_overflow(decision, (width, height))
                structure_evidence = True
                left_categories = structure["categories"]
                categories = {anchor["normalized"] for anchor in left_categories}
                dish_anchors = structure["names"]
                price_anchors = structure["prices"]
                prices = set()
                for anchor in price_anchors:
                    prices.update(self._price_keys(anchor["text"]))
        else:
            # 模型已明确判定“菜单就绪”时，它是从画面语义上确认了菜名与价格；
            # 海报式菜单的价格常画在图里、OCR 置信度偏低（0.3～0.5），此时只要求
            # OCR 文本真实出现在对应列，不因单字置信度低而否掉整帧。
            ready_claim = decision.action == "ready"
            left_categories = [
                anchor for anchor in anchors
                if anchor["normalized"] in categories and anchor["center_x"] < width * 0.30
                and anchor["center_y"] < height * 0.82
                and anchor["confidence"] >= (0.5 if (_resting_inference or ready_claim) else 0.8)
            ]
            if left_categories:
                # 窄窗口中右侧内容标题也可能落入左侧 30% 范围。按最左分类列的
                # 左边界聚合，防止将右栏同名标题并入侧栏、把菜价排除在内容区外。
                category_left = min(anchor["x"] for anchor in left_categories)
                column_tolerance = max(12, width * 0.06)
                left_categories = [anchor for anchor in left_categories
                                   if anchor["x"] <= category_left + column_tolerance]
            distinct_categories = {anchor["normalized"] for anchor in left_categories}
            if len(distinct_categories) < 2:
                return self._clamp_bottom_overflow(decision, (width, height))
            content_left = max(anchor["right"] for anchor in left_categories) + 8
            content_bottom_limit = min(bottom_navigation) if bottom_navigation else height * 0.88
            dish_anchors = [
                anchor for anchor in anchors
                if anchor["normalized"] in names and anchor["center_x"] >= content_left
                and anchor["center_y"] < content_bottom_limit
                and anchor["confidence"] >= (0.3 if ready_claim else 0.8)
            ]
            price_anchors = [
                anchor for anchor in anchors
                if anchor["center_x"] >= content_left and anchor["center_y"] < content_bottom_limit
                and prices.intersection(self._price_keys(anchor["text"]))
            ]
            if not dish_anchors or not price_anchors:
                return decision
        content_left = max(anchor["right"] for anchor in left_categories) + 8
        content_bottom_limit = min(bottom_navigation) if bottom_navigation else height * 0.88
        right_category_headers = [
            anchor for anchor in anchors
            if anchor["normalized"] in categories
            and anchor["center_x"] >= content_left
            and anchor["center_y"] < content_bottom_limit
            and anchor["confidence"] >= 0.8
        ]
        top = min(
            min(anchor["y"] for anchor in right_category_headers)
            if right_category_headers else min(anchor["y"] for anchor in left_categories),
            min(anchor["y"] for anchor in dish_anchors),
            min(anchor["y"] for anchor in price_anchors),
        ) - 8
        nav_top = min(bottom_navigation) if bottom_navigation else None
        bottom = nav_top - 6 if nav_top is not None else height * 0.88
        right = width - 8
        if content_left < 0 or top < 0 or right <= content_left or bottom <= top:
            return decision
        region_width, region_height = right - content_left, bottom - top
        # 小框无法安全用于滚动，继续走模型原区域验证，由SceneAgent决定是否拒绝。
        if region_width < width * 0.2 or region_height < height * 0.2:
            return decision
        matched_categories = list(dict.fromkeys(anchor["text"] for anchor in left_categories))
        matched_names = list(dict.fromkeys(anchor["text"] for anchor in dish_anchors))
        matched_prices = list(dict.fromkeys(
            anchor["text"] for anchor in price_anchors
            if prices.intersection(self._price_keys(anchor["text"]))
        ))
        updates = {
            "menu_region": Region(x=content_left, y=top, width=region_width, height=region_height),
            "categories": matched_categories,
            "item_names_visible": matched_names,
            "prices_visible": matched_prices,
        }
        if local_fallback is not None and local_fallback["brand"] is not None:
            existing_store_text = list(decision.observed_store_text)
            observed_brand = local_fallback["brand"]["text"]
            if observed_brand not in existing_store_text:
                existing_store_text.append(observed_brand)
            updates["observed_store_text"] = existing_store_text
        if decision.action != "ready":
            # 页面已被本地锚点证明是只读菜单，跳过模型猜测的动作，交给受限滚动采集。
            updates.update({
                "action": "ready", "x": None, "y": None, "pixels": 0,
                "reason": "本地 OCR 已核验可见菜单菜名、价格和分类；进入只读滚动采集",
            })
        if structure_evidence:
            updates["reason"] = "本地 OCR 已核验双列菜单结构；门店标识未确认"
        elif local_fallback is not None and local_fallback["brand"] is None:
            # 品牌字没识别到但菜单结构足够强；显式标注，便于后续复核与统计本地命中。
            updates["reason"] = "本地 OCR 已核验菜单结构和菜价配对；门店标识未确认"
        return decision.model_copy(update=updates)

    def _resting_menu_ocr_evidence(self, image_path: Path, decision: SceneDecision, ocr_result=None) -> dict | None:
        """从休息页同帧 OCR 中提取通用双列菜单证据，不依赖商家或底栏模板。"""
        if (decision.action != "blocked" or decision.overlay_visible or decision.login_required
                or decision.phone_required or decision.consent_required or decision.transaction_visible):
            return None
        reason = unicodedata.normalize("NFKC", decision.reason)
        error_pattern = re.compile(r"无效.{0,8}URI|URI.{0,8}无效|服务暂停|暂停服务|无法服务|服务不可用|系统错误", re.I)
        resting_pattern = re.compile(r"(?:当前)?门店.{0,8}(?:已)?(?:休息|打烊|停业)|门店休息中|非营业|暂停营业|暂停接单|停止接单")
        if error_pattern.search(reason):
            return None
        if not resting_pattern.search(reason):
            return None
        if self.ocr_reader is None:
            return None
        image_size, rows = ocr_result if ocr_result is not None else self._read_scaled_ocr(image_path)
        if image_size is None:
            return None
        full_text = " ".join(unicodedata.normalize("NFKC", str(row.get("text", ""))) for row in rows)
        if not resting_pattern.search(full_text):
            return None
        if error_pattern.search(full_text):
            return None

        width, height = image_size
        anchors = []
        for row in rows:
            text = unicodedata.normalize("NFKC", str(row.get("text", ""))).strip()
            normalized = self._normalize_menu_name(text)
            try:
                confidence = float(row.get("confidence", 0))
                x, y, box_width, box_height = (float(row[key]) for key in ("x", "y", "width", "height"))
            except (KeyError, TypeError, ValueError):
                continue
            if (not all(math.isfinite(value) for value in (confidence, x, y, box_width, box_height))
                    or confidence <= 0 or box_width <= 0 or box_height <= 0
                    or x < 0 or y < 0 or x + box_width > width * 3 or y + box_height > height * 3):
                continue
            left, top = x / 3, y / 3
            right, bottom = (x + box_width) / 3, (y + box_height) / 3
            anchors.append({"text": text, "normalized": normalized, "x": left, "y": top,
                            "right": right, "bottom": bottom, "center_x": (left + right) / 2,
                            "center_y": (top + bottom) / 2, "confidence": confidence})
        if not anchors:
            return None

        nav_top = min((anchor["y"] for anchor in anchors
                       if anchor["normalized"] in {"首页", "点餐", "扫码点餐", "订单", "我的", "个人中心"}
                       and anchor["center_y"] >= height * 0.72), default=height * 0.94)
        categories = [anchor for anchor in anchors
                      if anchor["center_x"] < width * 0.30 and anchor["center_y"] < nav_top
                      and anchor["right"] - anchor["x"] <= width * 0.22
                      and anchor["confidence"] >= 0.5 and 2 <= len(anchor["normalized"]) <= 10
                      and re.search(r"[\u3400-\u9fff]", anchor["text"])
                      and not re.search(r"新品|充值|优惠|活动|会员|点餐|打卡", anchor["text"])]
        # 同一侧栏列内去重，避免重复 OCR 文本被当成多个分类。
        if categories:
            col_left = min(anchor["x"] for anchor in categories)
            categories = [anchor for anchor in categories if anchor["x"] <= col_left + max(12, width * 0.06)]
        if len({anchor["normalized"] for anchor in categories}) < 2:
            return None
        content_left = max(anchor["right"] for anchor in categories) + 8
        prices = [anchor for anchor in anchors if anchor["center_x"] >= content_left
                  and anchor["center_y"] < nav_top
                  and re.search(r"[¥￥]\s*\d|\d+(?:\.\d+)?\s*元", anchor["text"])
                  and self._price_keys(anchor["text"])]
        dishes = [anchor for anchor in anchors if anchor["center_x"] >= content_left
                  and anchor["center_y"] < nav_top and anchor["confidence"] >= 0.8
                  and 2 <= len(anchor["normalized"]) <= 18
                  and re.search(r"[\u3400-\u9fff]", anchor["text"])
                  and not re.search(r"免费|会员|推荐|新品|点餐|充值|优惠|活动|休息", anchor["text"])]
        matched_dishes = []
        matched_prices = []
        for dish in dishes:
            for price in prices:
                if 0 <= price["y"] - dish["bottom"] <= 90:
                    matched_dishes.append(dish)
                    matched_prices.append(price)
        if not matched_dishes or not matched_prices:
            return None
        # 门店信息也可能位于左上方。分类列须与实际菜品卡片处于同一纵向区段，
        # 避免把距离、取餐方式等抬头文字当作分类，进而改变滚动范围。
        first_dish_top = min(anchor["y"] for anchor in matched_dishes)
        categories = [anchor for anchor in categories
                      if anchor["y"] >= first_dish_top - max(90, height * 0.12)]
        if len({anchor["normalized"] for anchor in categories}) < 2:
            return None
        return {
            "categories": list(dict.fromkeys(anchor["text"] for anchor in categories)),
            "item_names_visible": list(dict.fromkeys(anchor["text"] for anchor in matched_dishes)),
            "prices_visible": list(dict.fromkeys(anchor["text"] for anchor in matched_prices)),
        }

    def _local_menu_template_anchors(
        self, anchors: list[dict], width: int, height: int, expected_source_name: str,
    ) -> dict | None:
        """仅在模型三类证据全空时，用门店名、菜单列和底栏构成严格本地模板。

        门店名识别不到时不直接放弃：只要左侧分类列、底栏导航和菜价配对足够强，
        就按“门店标识未确认”放行 ready，避免为了一个品牌字再付一次模型调用。
        """
        if not expected_source_name:
            return None
        nav_labels = {"首页", "扫码点餐", "点餐", "订单", "我的", "个人中心"}
        bottom_nav = [
            anchor for anchor in anchors
            if anchor["normalized"] in nav_labels
            and anchor["center_y"] >= height * 0.72
            and anchor["confidence"] >= 0.8
        ]
        nav_terms = {anchor["normalized"] for anchor in bottom_nav}
        if len(nav_terms) < 2 or not nav_terms & {"首页", "扫码点餐", "点餐"}:
            return None
        brand_candidates = [
            anchor for anchor in anchors
            if anchor["confidence"] >= 0.8
            and anchor["center_x"] < width * 0.55
            and height * 0.12 <= anchor["center_y"] <= height * 0.6
            and self._store_matches(expected_source_name, [anchor["text"]])
        ]
        if len(brand_candidates) > 1:
            return None
        brand = brand_candidates[0] if brand_candidates else None
        food_category_pattern = re.compile(
            r"串|烤|菜|饮|酒|汤|饭|面|啤|主食|小吃|套餐|肉|鱼|虾|锅|海鲜|烧烤"
        )
        excluded_category_pattern = re.compile(r"新品|充值|优惠|活动|会员|打卡|订单|购物车")
        categories = [
            anchor for anchor in anchors
            if anchor["confidence"] >= 0.8
            and anchor["center_x"] < width * 0.30
            and (brand is None or anchor["y"] >= brand["bottom"])
            and anchor["center_y"] < min(item["y"] for item in bottom_nav)
            and 2 <= len(anchor["normalized"]) <= 10
            and re.search(r"[\u3400-\u9fff]", anchor["text"])
            and not excluded_category_pattern.search(anchor["text"])
            and (food_category_pattern.search(anchor["text"]) or brand is None)
        ]
        unique_categories = {anchor["normalized"]: anchor for anchor in categories}
        if len(unique_categories) < 3:
            return None
        content_left = max(anchor["right"] for anchor in unique_categories.values()) + 8
        nav_top = min(anchor["y"] for anchor in bottom_nav)
        explicit_prices = [
            anchor for anchor in anchors
            if anchor["center_x"] >= content_left
            and anchor["center_y"] < nav_top
            and re.search(r"[¥￥]\s*\d", unicodedata.normalize("NFKC", anchor["text"]))
            and self._price_keys(anchor["text"])
        ]
        if not explicit_prices:
            return None
        dish_candidates = [
            anchor for anchor in anchors
            if anchor["confidence"] >= 0.8
            and anchor["center_x"] >= content_left
            and anchor["center_y"] < nav_top
            and 2 <= len(anchor["normalized"]) <= 18
            and re.search(r"[\u3400-\u9fff]", anchor["text"])
            and not re.search(r"[，,、]|免费|本店|请|欢迎|VIP|会员|扫码|点餐", anchor["text"], re.I)
        ]
        pairs = []
        for price in explicit_prices:
            for dish in dish_candidates:
                # 窄窗口（310–440px）里菜名和价格常在同一行，宽窗口才是上下堆叠；
                # 只认纵向堆叠会把所有窄窗口的菜单页误判成“没有菜价配对”。
                same_row = (
                    abs(price["center_y"] - dish["center_y"]) <= max(12.0, dish["bottom"] - dish["y"])
                    and 0 <= price["x"] - dish["right"] <= 160
                )
                vertical = 0 <= price["y"] - dish["bottom"] <= 110
                if same_row or vertical:
                    pairs.append((abs(price["center_y"] - dish["center_y"])
                                  if same_row else price["y"] - dish["bottom"], dish, price))
        if not pairs:
            return None
        # 没有门店名时，用“至少两组成对菜名-价格”替代品牌约束，降低误判成其他页面的概率。
        if brand is None and len(pairs) < 2:
            return None
        _, dish, price = min(pairs, key=lambda item: item[0])
        return {
            "categories": list(unique_categories.values()),
            "dish": dish,
            "price": price,
            "brand": brand,
        }


    def _local_dual_column_menu_evidence(
        self, anchors: list[dict], width: int, height: int,
    ) -> dict | None:
        """无品牌时的双列菜单软校验：左侧分类 ≥3、菜名 ≥3、带 ¥ 价格 ≥3、成对 ≥2。

        比门店模板宽松，但要求三类证据同时出现，避免把订单页或活动页当成菜单页。
        """
        nav_labels = {"首页", "扫码点餐", "点餐", "订单", "我的", "个人中心"}
        entry_words = {"开始点餐", "开始点单", "开始选餐", "开始点菜", "去点餐", "查看菜单", "进入菜单"}
        excluded = re.compile(r"新品|充值|优惠|活动|会员|打卡|购物车|月售|销量|好评|排名|推荐|公告|营业|配送|休息|打烊|停业")
        bottom_nav = [
            anchor for anchor in anchors
            if anchor["normalized"] in nav_labels and anchor["center_y"] >= height * 0.72
        ]
        nav_top = min((anchor["y"] for anchor in bottom_nav), default=height * 0.9)
        categories = {}
        for anchor in anchors:
            if anchor["center_x"] >= width * 0.30 or anchor["confidence"] < 0.7:
                continue
            if not 2 <= len(anchor["normalized"]) <= 10:
                continue
            if not re.search(r"[\u3400-\u9fff]", anchor["text"]):
                continue
            if anchor["normalized"] in nav_labels or anchor["text"] in entry_words:
                continue
            if excluded.search(anchor["text"]) or self._price_keys(anchor["text"]):
                continue
            if anchor["center_y"] >= nav_top:
                continue
            categories.setdefault(anchor["normalized"], anchor)
        if len(categories) < 3:
            return None
        # 右侧内容标题也可能落在左侧 30% 内；按最左分类列的左边界聚合，
        # 避免把右栏标题算进分类栏、把菜价挤到内容区外。
        category_left = min(anchor["x"] for anchor in categories.values())
        column_tolerance = max(12, width * 0.06)
        categories = {key: anchor for key, anchor in categories.items()
                      if anchor["x"] <= category_left + column_tolerance}
        if len(categories) < 3:
            return None
        content_left = max(anchor["right"] for anchor in categories.values()) + 8
        names = {}
        for anchor in anchors:
            if anchor["center_x"] < content_left or anchor["center_y"] >= nav_top:
                continue
            if anchor["confidence"] < 0.3:
                continue
            text = anchor["text"]
            normalized = anchor["normalized"]
            if self._price_keys(text):
                # 窄窗口常把“菜名 ¥价”识别成一个框；去掉尾部价格后仍可作为菜名证据。
                text = re.split(r"[¥￥]\s*\d", text, maxsplit=1)[0].strip()
                normalized = self._normalize_menu_name(text)
            if not 2 <= len(normalized) <= 18:
                continue
            if not re.search(r"[\u3400-\u9fff]", text):
                continue
            if normalized in nav_labels or text in entry_words or excluded.search(text):
                continue
            names.setdefault(normalized, {**anchor, "text": text, "normalized": normalized})
        if len(names) < 3:
            return None
        prices = {
            anchor["normalized"]: anchor for anchor in anchors
            if anchor["center_x"] >= content_left and anchor["center_y"] < nav_top
            and re.search(r"[¥￥]\s*\d", anchor["text"]) and self._price_keys(anchor["text"])
        }
        if len(prices) < 3:
            return None
        pairs = 0
        # 窄窗口常把“菜名 ¥价”识别成一个文字框，这种合并行本身就构成一组配对。
        for price in prices.values():
            if re.search(r"[\u3400-\u9fff]", price["text"]) and len(price["normalized"]) >= 4:
                pairs += 1
        for price in prices.values():
            for name in names.values():
                if price["normalized"] == name["normalized"]:
                    continue
                same_row = (
                    abs(price["center_y"] - name["center_y"]) <= max(14.0, name["bottom"] - name["y"])
                    and -20 <= price["x"] - name["right"] <= 200
                )
                vertical = 0 <= price["y"] - name["bottom"] <= 120
                if same_row or vertical:
                    pairs += 1
                    break
        if pairs < 2:
            return None
        return {
            "categories": list(categories.values()),
            "names": list(names.values()),
            "prices": list(prices.values()),
        }

    def _record_scene_inspection(self, image_path: Path, decision: SceneDecision) -> None:
        if self.debug_log:
            return
        region = decision.menu_region
        try:
            atomic_json(image_path.parent / f"scene-inspection-{image_path.stem}.json", {
                "action": decision.action,
                "x": decision.x,
                "y": decision.y,
                "menu_region": region.model_dump() if region is not None else None,
                "selected_people_count": decision.selected_people_count,
                "flags": {
                    "overlay_visible": decision.overlay_visible,
                    "login_required": decision.login_required,
                    "phone_required": decision.phone_required,
                    "consent_required": decision.consent_required,
                    "transaction_visible": decision.transaction_visible,
                },
                "target_label": self._sanitize_target_label(decision.target_label),
            })
        except Exception:
            # 检查摘要失败不能改变当前决策，也不记录任何模型原文。
            return

    @staticmethod
    def _sanitize_target_label(value: str) -> str:
        safe = re.sub(r"(?<!\d)(?:\+?\d[\d ()-]{5,}\d)(?!\d)", "[数字已隐藏]", value)
        return re.sub(r"\d{7,}", "[数字已隐藏]", safe)[:120]

    def _read_scaled_ocr(self, image_path: Path) -> tuple[tuple[int, int] | None, list[dict]]:
        """3x 单档 OCR；坐标按 ``OCR_SCALE = 3`` 还原，是本地规则的默认证据源。"""

        started = time.monotonic()
        original_size = None
        scaled_size = None
        error = None
        if self.ocr_reader is None:
            self._last_ocr_meta = {"engine": None, "image_size": None, "scaled_size": None,
                                   "elapsed_ms": 0, "error": "OCR reader 未配置"}
            return None, []
        temp_path: Path | None = None
        try:
            with Image.open(image_path) as original:
                image = original.convert("RGB")
                original_size = image.size
                scaled = image.resize((image.width * 3, image.height * 3), Image.Resampling.LANCZOS)
                scaled_size = scaled.size
            fd, name = tempfile.mkstemp(prefix="button-ocr-", suffix=".png", dir=str(image_path.parent))
            os.close(fd)
            temp_path = Path(name)
            temp_path.chmod(0o600)
            scaled.save(temp_path, format="PNG")
            rows = self.ocr_reader(temp_path)
            if not isinstance(rows, list):
                error = "OCR 返回格式不是列表"
                self._last_ocr_meta = {"engine": self._ocr_engine_name(), "image_size": original_size,
                                       "scaled_size": scaled_size,
                                       "elapsed_ms": round((time.monotonic() - started) * 1000), "error": error}
                return None, []
            self._last_ocr_meta = {"engine": self._ocr_engine_name(), "image_size": original_size,
                                   "scaled_size": scaled_size,
                                   "elapsed_ms": round((time.monotonic() - started) * 1000),
                                   "row_count": len(rows), "error": None}
            return original_size, rows
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
            self._last_ocr_meta = {"engine": self._ocr_engine_name(), "image_size": original_size,
                                   "scaled_size": scaled_size,
                                   "elapsed_ms": round((time.monotonic() - started) * 1000), "error": error}
            return None, []
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    _OCR_FUSION_SCALES = (2, 4)

    def _read_fused_scaled_ocr(
        self, image_path: Path, base: tuple[tuple[int, int] | None, list[dict]] | None = None,
    ) -> tuple[tuple[int, int] | None, list[dict]]:
        """本地未命中时的二次召回：2x/4x 结果换算回 3x 坐标后与基础结果并集。

        只在本地规则已经失败、即将调用模型时执行，因此额外 OCR 成本不会影响
        本地直接命中的帧；两档合计约 0.6 秒，仍低于一次模型调用。
        """
        base_size, base_rows = base if base is not None else self._read_scaled_ocr(image_path)
        if base_size is None or self.ocr_reader is None:
            return base_size, base_rows
        started = time.monotonic()
        merged = [dict(row) for row in base_rows]
        per_scale: dict[str, int] = {}
        for scale in self._OCR_FUSION_SCALES:
            rows = self._read_ocr_at_scale(image_path, scale)
            per_scale[str(scale)] = len(rows)
            merged = self._merge_scaled_ocr_rows(merged, rows)
        meta = dict(self._last_ocr_meta or {})
        meta.update({"fused": True, "fusion_rows_by_scale": per_scale,
                     "fused_rows": len(merged),
                     "fusion_elapsed_ms": round((time.monotonic() - started) * 1000),
                     "error": None})
        self._last_ocr_meta = meta
        return base_size, merged

    def _read_ocr_at_scale(self, image_path: Path, scale: int) -> list[dict]:
        """按指定倍率识别并换算回 3x 坐标系；失败时返回空列表，不阻断基础结果。"""
        temp_path: Path | None = None
        try:
            with Image.open(image_path) as original:
                image = original.convert("RGB")
                frame = image.resize((image.width * scale, image.height * scale),
                                     Image.Resampling.LANCZOS)
            fd, name = tempfile.mkstemp(prefix=f"button-ocr-{scale}x-", suffix=".png",
                                        dir=str(image_path.parent))
            os.close(fd)
            temp_path = Path(name)
            temp_path.chmod(0o600)
            frame.save(temp_path, format="PNG")
            rows = self.ocr_reader(temp_path)
            if not isinstance(rows, list):
                return []
            return self._rescale_ocr_rows(rows, scale)
        except Exception:
            return []
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _rescale_ocr_rows(self, rows: list[dict], scale: int) -> list[dict]:
        """把某一 OCR 档的行换算到 3x 坐标系；数值非法的行直接丢弃。"""
        if scale == 3:
            return [dict(row) for row in rows if isinstance(row, dict)]
        factor = 3 / scale
        converted: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            output = dict(row)
            for key in ("x", "y", "width", "height"):
                try:
                    output[key] = float(row[key]) * factor
                except (KeyError, TypeError, ValueError):
                    output = None
                    break
            if output is not None:
                converted.append(output)
        return converted

    def _merge_scaled_ocr_rows(self, base: list[dict], extra: list[dict]) -> list[dict]:
        """按文字内容和重叠率合并多尺度结果，保留置信度更高的一份几何。"""
        merged = list(base)
        for row in extra:
            text = self._normalize_ocr_text(str(row.get("text", "")))
            if not text:
                continue
            for existing in merged:
                if self._normalize_ocr_text(str(existing.get("text", ""))) != text:
                    continue
                if self._ocr_boxes_overlap(existing, row, threshold=0.45):
                    try:
                        if float(row.get("confidence", 0)) > float(existing.get("confidence", 0)):
                            existing.update(row)
                    except (TypeError, ValueError):
                        pass
                    break
            else:
                merged.append(row)
        return merged

    @staticmethod
    def _ocr_boxes_overlap(left: dict, right: dict, threshold: float) -> bool:
        try:
            lx, ly = float(left["x"]), float(left["y"])
            lw, lh = float(left["width"]), float(left["height"])
            rx, ry = float(right["x"]), float(right["y"])
            rw, rh = float(right["width"]), float(right["height"])
        except (KeyError, TypeError, ValueError):
            return True
        x1, y1 = max(lx, rx), max(ly, ry)
        x2, y2 = min(lx + lw, rx + rw), min(ly + lh, ry + rh)
        if x2 <= x1 or y2 <= y1:
            return False
        intersection = (x2 - x1) * (y2 - y1)
        return intersection / max(1.0, min(lw * lh, rw * rh)) >= threshold


    @staticmethod
    def _normalize_ocr_text(value: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()

    @staticmethod
    def _normalize_menu_name(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"[^\w\u3400-\u9fff]", "", normalized)

    @staticmethod
    def _price_keys(value: str) -> set[str]:
        normalized = unicodedata.normalize("NFKC", value).replace(",", "")
        values: set[str] = set()
        for token in re.findall(r"\d+(?:\.\d+)?", normalized):
            try:
                number = Decimal(token).normalize()
            except InvalidOperation:
                continue
            if number.is_finite():
                values.add(format(number, "f"))
        return values

    @classmethod
    def _find_ocr_target(
        cls,
        rows: list[dict],
        image_size: tuple[int, int],
        target: str,
        people_count: int | None = None,
        require_people_grid: bool = False,
    ) -> tuple[float, float] | None:
        detail = cls._find_ocr_target_detail(rows, image_size, target, people_count, require_people_grid)
        center = detail.get("center_image_px") if detail.get("matched") else None
        return (center["x"], center["y"]) if center is not None else None

    @staticmethod
    def _button_features(
        image_path: Path | None, box: dict | None, image_size: tuple[int, int]
    ) -> dict | None:
        """裁剪按钮区域，返回对比度、主色色差和彩色占比；裁剪失败返回 None。"""
        if image_path is None or not isinstance(box, dict):
            return None
        try:
            left = max(0, int(round(float(box["x"]))))
            top = max(0, int(round(float(box["y"]))))
            right = min(int(image_size[0]), int(round(float(box["x"]) + float(box["width"]))))
            bottom = min(int(image_size[1]), int(round(float(box["y"]) + float(box["height"]))))
            if right - left < 3 or bottom - top < 3:
                return None
            with Image.open(image_path) as opened:
                crop = opened.convert("RGB").crop((left, top, right, bottom))
        except Exception:
            return None
        gray = crop.convert("L")
        histogram = gray.histogram()
        total = sum(histogram) or 1
        # 10%/90% 分位亮度差近似文字与底色的对比度，比均值更能反映按钮笔画。
        cumulative = 0
        low = high = 0
        for value, count in enumerate(histogram):
            cumulative += count
            if not low and cumulative >= total * .1:
                low = value
            if cumulative >= total * .9:
                high = value
                break
        colors = crop.getcolors(maxcolors=1 << 16) or []
        dominant = max(colors, key=lambda item: item[0])[1] if colors else (0, 0, 0)
        saturation = crop.convert("HSV").getchannel("S")
        return {
            "contrast": int(high - low),
            "color_spread": int(max(dominant) - min(dominant)),
            "colored_ratio": round(float(sum(saturation.histogram()[48:]) / total), 3),
            "flat": bool(ImageStat.Stat(gray).stddev[0] < 3),
        }

    @classmethod
    def _button_visual_baseline(
        cls, image_path: Path | None, rows: list[dict], image_size: tuple[int, int],
        max_samples: int = 16,
    ) -> dict | None:
        """用同屏其他文字框的中位特征做基线，适配不同小程序主题色的深浅。"""
        if image_path is None or not rows:
            return None
        samples: list[dict] = []
        for item in rows[:max_samples]:
            try:
                box = {
                    "x": float(item["x"]) / 3, "y": float(item["y"]) / 3,
                    "width": float(item["width"]) / 3, "height": float(item["height"]) / 3,
                }
            except (KeyError, TypeError, ValueError):
                continue
            features = cls._button_features(image_path, box, image_size)
            if features is None or features["flat"]:
                continue  # 纯色空白框不参与基线，否则会把整体对比度拉低。
            samples.append(features)
        if len(samples) < 3:
            return None
        return {
            "contrast": median(sorted(item["contrast"] for item in samples)),
            "color_spread": median(sorted(item["color_spread"] for item in samples)),
            "colored_ratio": median(sorted(item["colored_ratio"] for item in samples)),
        }

    @classmethod
    def _button_visual_state(
        cls, image_path: Path | None, box: dict | None, image_size: tuple[int, int],
        baseline: dict | None = None,
    ) -> dict:
        """判断按钮是否处于可用态；有基线时用同屏按钮的中位特征做相对比较。

        禁用按钮的文字与底色亮度接近、主色没有彩度。固定阈值只在不同小程序
        主题色深浅差异很大时失效，所以优先拿当前界面的中位对比度/彩度当参照，
        三个信号同时显著低于基线才判不可用，避免误杀浅色正常按钮。
        """
        features = cls._button_features(image_path, box, image_size)
        if features is None:
            return {"available": True, "reason": "no_crop"}
        if baseline:
            contrast_floor = min(60.0, max(12.0, float(baseline["contrast"]) * .40))
            spread_floor = min(40.0, max(8.0, float(baseline["color_spread"]) * .35))
            colored_floor = max(.06, float(baseline["colored_ratio"]) * .5)
        else:
            contrast_floor, spread_floor, colored_floor = 35.0, 20.0, .10
        disabled = ((features["flat"] or features["contrast"] < contrast_floor)
                    and features["color_spread"] < spread_floor
                    and features["colored_ratio"] < colored_floor)
        return {**features, "available": not disabled, "baseline": baseline is not None}

    @classmethod
    def _find_ocr_target_detail(
        cls,
        rows: list[dict],
        image_size: tuple[int, int],
        target: str,
        people_count: int | None = None,
        require_people_grid: bool = False,
        *,
        min_confidence: float = 0.8,
        contains: bool = False,
    ) -> dict:
        factor = 3
        scaled_width, scaled_height = image_size[0] * factor, image_size[1] * factor
        valid: list[dict] = []
        number_labels: set[int] = set()
        effective_min_confidence = min_confidence if math.isfinite(min_confidence) and min_confidence >= 0 else 0.8
        for item in rows:
            normalized = cls._normalize_ocr_text(str(item.get("text", "")))
            numeric = re.fullmatch(r"(\d{1,2})(?:人|位)?", normalized)
            try:
                confidence = float(item.get("confidence", 0))
                x, y, width, height = (float(item[key]) for key in ("x", "y", "width", "height"))
            except (KeyError, TypeError, ValueError):
                continue
            if (
                not math.isfinite(confidence) or confidence < (0.5 if require_people_grid and numeric else effective_min_confidence)
                or not all(math.isfinite(value) for value in (x, y, width, height))
                or width <= 0 or height <= 0 or x < 0 or y < 0
                or x + width > scaled_width or y + height > scaled_height
            ):
                continue
            number = int(numeric.group(1)) if numeric else None
            if number is not None and 1 <= number <= 12:
                number_labels.add(number)
            valid.append({
                "text": str(item.get("text", ""))[:300], "normalized": normalized,
                "confidence": confidence, "number": number,
                "box_scaled_ocr_px": {"x": x, "y": y, "width": width, "height": height},
                "center_scaled_ocr_px": {"x": x + width / 2, "y": y + height / 2},
            })
        if require_people_grid and len(number_labels) < 3:
            return {"target": target, "mode": "people_grid", "matched": False,
                    "reason": "fewer_than_three_distinct_number_labels", "number_labels": sorted(number_labels),
                    "candidate_count": 0, "zoom_factor": factor, "min_confidence": 0.5,
                    "screenshot_size_px": {"width": image_size[0], "height": image_size[1]}}
        if require_people_grid:
            matches = [row for row in valid if row["number"] == people_count]
        else:
            exact_target = cls._normalize_ocr_text(target)
            if contains:
                matches = [row for row in valid if exact_target in row["normalized"]]
            else:
                matches = [row for row in valid if row["normalized"] == exact_target]
        if len(matches) != 1:
            return {"target": target, "mode": "people_grid" if require_people_grid else "exact_text",
                    "matched": False, "reason": "not_found" if not matches else "ambiguous",
                    "candidate_count": len(matches), "candidates": matches[:20],
                    "number_labels": sorted(number_labels), "zoom_factor": factor,
                    "min_confidence": 0.5 if require_people_grid else effective_min_confidence,
                    "screenshot_size_px": {"width": image_size[0], "height": image_size[1]}}
        match = matches[0]
        box = match["box_scaled_ocr_px"]
        center_scaled = match["center_scaled_ocr_px"]
        box_image = {key: value / factor for key, value in box.items()}
        center_image = {key: value / factor for key, value in center_scaled.items()}
        return {"target": target, "mode": "people_grid" if require_people_grid else "exact_text",
                "matched": True, "reason": "unique_match", "candidate_count": 1,
                "matched_ocr": match, "box_image_px": box_image, "center_image_px": center_image,
                "number_labels": sorted(number_labels), "zoom_factor": factor,
                "min_confidence": 0.5 if require_people_grid else effective_min_confidence,
                "screenshot_size_px": {"width": image_size[0], "height": image_size[1]}}

    @staticmethod
    def _is_people_count_choice(target_label: str, reason: str, people_count: int) -> bool:
        target_label = unicodedata.normalize("NFKC", target_label).strip()
        reason = unicodedata.normalize("NFKC", reason)
        # 模型的场景说明常写成“已选 2 人”而不是“用餐人数”，不能因此拒绝合法点击。
        if not re.search(r"人数|就餐|用餐|几[人位]|已选.{0,6}[人位]|选择.{0,6}[人位]", reason):
            return False
        # 数字与人数上下文都存在仍不足以点击；排除“1元优惠”等带数字的广告。
        if not re.fullmatch(r"(?:(?:选择)?(?:就餐|用餐)?人数\s*[:：]?\s*)?\d{1,2}\s*(?:人|位)?", target_label.strip()):
            return False
        numbers = re.findall(r"(?<!\d)(\d{1,2})(?!\d)", target_label)
        return len(numbers) == 1 and int(numbers[0]) == people_count

    def _page_ocr_text(self, page_path: Path) -> str:
        try:
            rows = self.ocr_reader(page_path) or []
        except Exception:
            return ""
        return re.sub(r"\s+", "", "".join(str(row.get("text", "")) for row in rows))

    @staticmethod
    def _same_page_text(left: str, right: str) -> bool:
        if not left or not right:
            return False
        return left == right or SequenceMatcher(None, left, right).ratio() >= 0.98

    def _capture_menu(self, selector, first_frame, region, settings, profile, output_dir) -> list[str]:
        box = (round(region.x), round(region.y), round(region.x + region.width), round(region.y + region.height))
        frame = first_frame
        previous = None
        pages: list[str] = []
        sample_text = ""
        # 滚动落点放在菜品列表上半部，让滚轮更容易作用在真实菜品区域。
        scroll_y_ratio = 0.30

        def scroll_once(target: dict) -> dict:
            return self._send({
                "action": "scroll_capture", "path": str((output_dir / "evidence" / f"page-{index + 1:04d}.png").resolve()),
                "selector": selector,
                "x": (region.x + region.width * 0.65) * target["window_width"] / target["width"],
                "y": (region.y + region.height * scroll_y_ratio) * target["window_height"] / target["height"],
                "direction": "Down",
                "units": int(profile.get("scroll_units", 4)),
            })

        # 截图、滚动、再截图；相邻两帧近似一致即视为到底。
        for index in range(settings.capture.max_pages + 1):
            self._guard()
            with Image.open(frame["path"]) as image_file:
                image_file.load()
                if box[2] > image_file.width or box[3] > image_file.height:
                    raise NeedsReview("菜单区域超出当前截图边界")
                content = image_file.crop(box).copy()
            with self._trace("menu_page_check", page_index=index + 1,
                             threshold=0.7, comparison_region=region.model_dump()) as trace:
                difference = self._image_difference_images(previous, content) if previous is not None else None
                trace.update(image_difference=difference,
                             stop_reason="similar_to_previous" if difference is not None and difference < 0.7 else "continue")
            if difference is not None and difference < 0.7:
                return pages
            if len(pages) >= settings.capture.max_pages:
                raise NeedsReview(f"达到 max_pages={settings.capture.max_pages}，已保留部分截图但未确认菜单到底")
            page_path = output_dir / f"page-{len(pages) + 1:04d}.png"
            with Image.open(frame["path"]) as image_file:
                image_file.save(page_path)
            pages.append(str(page_path.relative_to(output_dir)))
            previous = content
            # 长菜单/信息流里页首轮播动画会让图像差异一直很大，只看图像会一路滚到页数上限。
            # 20 页之后每 5 页 OCR 一次，文字完全重复就认定已经到底。
            if len(pages) >= 20 and len(pages) % 5 == 0:
                current_text = self._page_ocr_text(page_path)
                if sample_text and self._same_page_text(sample_text, current_text):
                    return pages
                sample_text = current_text
            self._guard()
            frame = scroll_once(frame)
        raise NeedsReview(f"达到 max_pages={settings.capture.max_pages}，已保留部分截图但未确认菜单到底")

    @staticmethod
    def _image_difference_images(left: Image.Image, right: Image.Image) -> float:
        left = left.resize((256, 256))
        right = right.resize((256, 256))
        return sum(ImageStat.Stat(ImageChops.difference(left, right)).mean) / 3

    @staticmethod
    def _normalize_store(value: str) -> str:
        folded = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"[^\w\u3400-\u9fff]", "", folded)

    @classmethod
    def _store_matches(cls, expected: str, observed: list[str]) -> bool:
        parts = [part for part in re.split(r"[_＿]+", expected) if part.strip()]
        names = [parts[1]] if len(parts) >= 2 else []
        if len(parts) >= 4:
            # 品牌名和门店名可能不同（加盟店），两者任一命中都算本店。
            names.append(parts[-1])
        targets = []
        for name in names:
            normalized = cls._normalize_store(name)
            core = re.sub(r"(单店|总店|分店|店)$", "", normalized)
            for value in (core, normalized):
                if len(value) >= 2 and value not in targets:
                    targets.append(value)
        if not targets:
            return False
        for value in observed:
            candidate = cls._normalize_store(value)
            # 城市名、单字和编号都不能证明门店身份。
            if len(candidate) < 2:
                continue
            for target in targets:
                if target in candidate or candidate in target:
                    return True
                if SequenceMatcher(None, target, candidate).ratio() >= 0.86:
                    return True
                # 品牌核心前缀一致（允许 OCR 多字/少字），如“落汤鱼”对“落汤鱼酸菜鱼”。
                if len(target) >= 3 and target[:3] in candidate:
                    return True
        return False
