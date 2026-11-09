"""GUI Agent SDK 和 UiPath 的本机 IPC 与结果契约。"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import select
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from .config import ConfigStore
from .model import Region


_ENTRY = Path(__file__).resolve().parent.parent / "ui_tars" / "adapter.mjs"
GUI_PLUS_MODEL = "gui-plus-2026-02-26"
_GUI_PLUS_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_MAX_MODEL_IMAGE_PIXELS = 1_003_520
_TERMINAL_OUTCOMES = ("success", "needs_review", "unqualified")


class TrialProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrialModel:
    node: str
    base_url: str
    api_key: str


def trial_model_from_env(configs: ConfigStore | None = None) -> TrialModel:
    explicit_url = os.environ.get("GUI_PLUS_BASE_URL", "").strip().rstrip("/")
    base_url = explicit_url or _GUI_PLUS_URL
    key = os.environ.get("GUI_PLUS_API_KEY", "").strip()
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    saved_model = configs.load().model if configs is not None and not (key or dashscope_key) else None
    use_saved = bool(saved_model and saved_model.provider == "openai_compatible"
                     and saved_model.model == GUI_PLUS_MODEL and saved_model.base_url
                     and (not explicit_url or saved_model.base_url == explicit_url))
    if use_saved:
        # 网页保存的密钥只用于它绑定的 GUI-Plus 地址，不能随试验环境变量改发到其他工作空间。
        base_url = saved_model.base_url
    url = urlsplit(base_url)
    host = url.hostname or ""
    beijing = url.scheme == "https" and (
        host == "dashscope.aliyuncs.com" or host.endswith(".cn-beijing.maas.aliyuncs.com")
    ) and url.path == "/compatible-mode/v1" and url.port in (None, 443)
    local = url.scheme == "http" and host in ("localhost", "127.0.0.1", "::1")
    if (not (beijing or local) or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError("GUI-Plus 需要百炼北京地域兼容接口。本机 HTTP 只用于联调")
    if not key and beijing:
        key = configs.key().strip() if use_saved else dashscope_key
    if not key:
        raise ValueError("单店试验需要北京地域 GUI-Plus 配置，请在网页设置中保存对应模型和密钥，或设置环境变量")
    node = shutil.which("node")
    if not node or not (_ENTRY.parent / "node_modules/@gui-agent/agent-sdk/package.json").is_file():
        raise ValueError("单店试验需要 Node.js 和 ui_tars 目录下已安装的 GUI Agent SDK（npm ci）")
    return TrialModel(node, base_url, key)


def run_navigation(model: TrialModel, store_name: str, people: int, max_steps: int,
                   *, observe, execute, check_stop, log_path: Path | None = None,
                   trace_path: Path | None = None, trace_meta: dict | None = None) -> tuple[dict, dict | None]:
    instruction = (
        f"目标门店：{store_name}，用餐人数 {people} 人。你正在操作已经打开的微信点餐小程序。"
        "\n目标：尽可能进入可采集的菜单页（能看到真实菜品，通常带价格），然后交接；后续程序负责滚动截图。"
        "\n执行原则：根据当前截图自主选择动作。欢迎页、人数选择、广告、加载、授权弹窗都只是中间状态，优先尝试继续。"
        "弹窗以消除阻碍为目标，允许、拒绝、同意、关闭都可以；点击没反应就重新观察再试，允许重试同一位置；"
        "加载先 wait 两到三次；出现新小程序窗口先 list_windows 再 switch_window。"
        "\n终态判断的核心：已经拿到可采集菜单、已经明确知道不能采集、还是仍然无法确定。判断依据是当前截图和操作反馈，不是关键词表。"
        "\n- success：已进入可正常浏览的菜品菜单，有真实菜品和价格，菜单区域可框选。只有一两道菜也可以；"
        "不要求店名出现在截图；门店打烊但菜单仍可浏览也算 success。"
        "只看到“立即点餐”、欢迎页、广告、购物车、订单配置、支付页或只有分类入口时，继续探索，不要 success。"
        "\n- unqualified：有清楚证据表明本轮无法采集。例如扫码点餐服务明确关闭且没有菜单入口；明确要求到店再扫码；"
        "二维码明确失效；商家明确未配置菜单；小程序停止服务且无菜单。"
        "反例：打烊但菜单可见 → success；提示暂时无法下单但有查看菜单入口 → 继续；白屏且没有明确原因 → 不是 unqualified；"
        "点击失败、需要登录、页面归属不明 → 不是 unqualified。"
        "\n- needs_review：合理尝试后仍无法确定或继续。例如强制手机号+验证码无法跳过；持续白屏或加载；"
        "页面与本轮二维码关系不明；工具持续失败；弹窗无法消除。"
        "记住：暂时进不去菜单 ≠ 不合格；不能下单 ≠ 不能采集菜单；菜品少或没显示完整店名 ≠ 不能成功。"
        "\n输出：每轮一个 computer_use tool_call，arguments 必须含 action（left_click/scroll/wait/list_windows/switch_window/terminate），"
        "不要写成自然语言函数调用；坐标和 menu_box 使用当前截图的 1000x1000 归一化；"
        "terminate 时给出 outcome、reason（中文，说明实际观察到的结果），menu_box 仅在 success 时提供，为 4 个 0..1000 的整数。"
    )
    return _run_agent(model, instruction, max_steps, observe=observe, execute=execute,
                      check_stop=check_stop, log_path=log_path,
                      trace_path=trace_path, trace_meta=trace_meta)


def _run_agent(model: TrialModel, instruction: str, max_steps: int,
               *, observe, execute, check_stop, log_path: Path | None = None,
               trace_path: Path | None = None, trace_meta: dict | None = None) -> tuple[dict, dict | None]:
    config = {
        "model": {"baseURL": model.base_url, "apiKey": model.api_key, "model": GUI_PLUS_MODEL},
        "instruction": instruction, "max_steps": max_steps,
        "trace_path": str(trace_path) if trace_path else "",
        "trace": {str(key): str(value) for key, value in (trace_meta or {}).items() if value is not None},
    }
    child_env = os.environ.copy()
    for key in ("GUI_PLUS_API_KEY", "DASHSCOPE_API_KEY", "UI_TARS_API_KEY"):
        child_env.pop(key, None)
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL
    try:
        with subprocess.Popen(
            [model.node, str(_ENTRY)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, env=child_env,
        ) as child:
            try:
                return _exchange(child, config, observe, execute, check_stop)
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
    finally:
        if log_path is not None:
            log.close()


def _exchange(child, config, observe, execute, check_stop):
    def send(payload):
        try:
            child.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            child.stdin.flush()
        except OSError:
            raise TrialProtocolError("GUI Agent 子进程通信中断") from None

    send(config)
    frame, frame_id = None, ""
    image_size = None
    action_sent = False
    sequence = 0
    pending = b""
    while True:
        check_stop()
        if b"\n" not in pending:
            if not select.select([child.stdout], [], [], .2)[0]:
                continue
            data = os.read(child.stdout.fileno(), 8192)
            if not data:
                raise TrialProtocolError("GUI Agent 在返回有效终态前退出")
            pending += data
            if len(pending) > 32_768:
                raise TrialProtocolError("GUI Agent 返回消息超过大小上限")
            continue
        line, pending = pending.split(b"\n", 1)
        try:
            event = json.loads(line)
        except (UnicodeError, ValueError):
            raise TrialProtocolError("GUI Agent 返回无效消息") from None
        if not isinstance(event, dict):
            raise TrialProtocolError("GUI Agent 返回无效消息")
        if event.get("type") == "result":
            return _result(event, frame, frame_id, image_size, action_sent)
        params = event.get("params")
        request_id = event.get("id")
        if (event.get("type") != "request" or not isinstance(params, dict)
                or not isinstance(request_id, (int, str)) or isinstance(request_id, bool)):
            raise TrialProtocolError("GUI Agent 返回未知请求")
        method = event.get("method")
        if method == "screenshot":
            sequence += 1
            frame = observe(sequence)
            frame_id = str(sequence)
            action_sent = False
            image_data, image_size = _model_image(frame)
            result = {"frame_id": frame_id, "base64": base64.b64encode(image_data).decode("ascii")}
        else:
            if frame is None or params.get("frame_id") != frame_id or action_sent:
                raise TrialProtocolError("GUI Agent 动作需要同轮新鲜截图")
            command = _action_command(method, params, frame, image_size)
            check_stop()
            reply = execute(command, frame_id) or {}
            if method == "windows":
                # 列窗口不改变页面，下一张截图仍属于同一帧。
                result = {"windows": reply.get("windows", []) if isinstance(reply, dict) else []}
            else:
                action_sent = True
                result = reply if method == "switch_window" and isinstance(reply, dict) else {}
        send({"id": request_id, "ok": True, "result": result})


def _model_image(frame: dict) -> tuple[bytes, tuple[int, int]]:
    width, height = frame["width"], frame["height"]
    if width * height <= _MAX_MODEL_IMAGE_PIXELS:
        return Path(frame["path"]).read_bytes(), (width, height)
    # 保留原始取证图。只对送模图缩放，让两套像素尺寸之间的映射由程序掌握。
    scale = math.sqrt(_MAX_MODEL_IMAGE_PIXELS / (width * height))
    size = (max(1, int(width * scale)), max(1, int(height * scale)))
    with Image.open(frame["path"]) as image, io.BytesIO() as data:
        image.resize(size, Image.Resampling.LANCZOS).save(data, format="PNG")
        return data.getvalue(), size


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _action_command(method, params, frame, image_size):
    if method == "wait":
        seconds = params.get("seconds")
        if not _finite(seconds) or not 0 <= seconds <= 5:
            raise TrialProtocolError("GUI Agent 等待参数无效")
        return {"action": "noop", "seconds": seconds}
    if method == "windows":
        return {"action": "windows"}
    if method == "switch_window":
        title = params.get("title")
        if not isinstance(title, str) or not title.strip():
            raise TrialProtocolError("GUI Agent 切换窗口缺少标题")
        return {"action": "switch_window", "title": title.strip()}
    if method not in ("click", "scroll"):
        raise TrialProtocolError("GUI Agent 请求了试验范围以外的动作")
    x, y = params.get("x"), params.get("y")
    width, height = image_size
    if not _finite(x) or not _finite(y) or not (0 <= x < width and 0 <= y < height):
        raise TrialProtocolError("GUI Agent 动作坐标超出送模窗口截图")
    if method == "click" and x > width * .55 and y < height * .045:
        # 右上角是小程序窗口胶囊（… / ◎），点中会退出小程序；降级为短暂等待，
        # 让 Agent 从新截图重新选择目标，而不是整店失败。
        return {"action": "noop", "seconds": 0.2}
    result = {"action": method, "x": x * frame["window_width"] / width,
              "y": y * frame["window_height"] / height}
    if method == "scroll":
        if params.get("direction") not in ("Up", "Down"):
            raise TrialProtocolError("GUI Agent 滚动方向无效")
        result.update(direction=params["direction"], units=4)
    return result


def _result(event, frame, frame_id, image_size, action_sent):
    outcome, reason = event.get("outcome"), event.get("reason")
    if outcome not in _TERMINAL_OUTCOMES or not isinstance(reason, str) or not reason.strip():
        raise TrialProtocolError("GUI Agent 终态缺少有效状态或理由")
    data = {"outcome": outcome, "reason": reason.strip()[:500]}
    diagnostic = event.get("diagnostic")
    if isinstance(diagnostic, dict):
        # 只保留可诊断的短字段；原始模型输出由 adapter.log 留存。
        data["diagnostic"] = {
            "code": str(diagnostic.get("code") or "")[:80],
            "raw": str(diagnostic.get("raw") or "")[:800],
            "detail": str(diagnostic.get("detail") or "")[:200],
        }
    if outcome == "needs_review":
        if event.get("source") == "runtime":
            data["source"] = "runtime"
        return data, frame
    # 可交接与不合格都必须带一张操作后的同轮证据图。
    if frame is None or event.get("frame_id") != frame_id or action_sent:
        raise TrialProtocolError("GUI Agent 终态缺少操作后的同轮窗口证据")
    if outcome == "unqualified":
        return data, frame
    region = event.get("menu_region")
    keys = ("x", "y", "width", "height")
    if not isinstance(region, dict) or set(region) != set(keys) or not all(_finite(region[k]) for k in keys):
        raise TrialProtocolError("GUI Agent 成功终态缺少有效菜单区域")
    x, y, width, height = (region[key] for key in keys)
    if not (0 <= x < x + width <= image_size[0] and 0 <= y < y + height <= image_size[1]):
        raise TrialProtocolError("GUI Agent 菜单区域超出送模窗口截图")
    scale_x, scale_y = frame["width"] / image_size[0], frame["height"] / image_size[1]
    left, top = round(x * scale_x), round(y * scale_y)
    right, bottom = round((x + width) * scale_x), round((y + height) * scale_y)
    if left >= right or top >= bottom:
        raise TrialProtocolError("GUI Agent 菜单区域尺寸无效")
    data["menu_region"] = Region(x=left, y=top, width=right - left, height=bottom - top).model_dump()
    return data, frame
