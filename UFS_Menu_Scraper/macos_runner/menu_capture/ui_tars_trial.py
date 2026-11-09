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
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "ui_tars" / "skills"
GUI_PLUS_MODEL = "gui-plus-2026-02-26"
_GUI_PLUS_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_MAX_MODEL_IMAGE_PIXELS = 1_003_520
_REFERENCE_MAX_PIXELS = 640_000
_TERMINAL_OUTCOMES = ("success", "needs_review", "unqualified")
_SKILL_MAX_CHARS = 6000
# Skill 只提供可参考的业务经验，由 Agent 自行决定是否加载。
_SKILLS = {
    "menu-navigation": "点餐小程序导航经验：三终态判定、弹窗与前置步骤、常见误判反例。",
}

# 输出契约是固定协议：字段、类型、取值范围都写死，不允许模型自创字段。
_OUTPUT_CONTRACT = """输出契约（固定协议，必须完全遵守；不合规会被拒绝并要求重写）
1. 每轮只输出一个 tool_call：<tool_call>{"name":"computer_use","arguments":{...}}</tool_call>。
   不要输出多个 tool_call，不要用自然语言描述动作，不要把 action 写成函数调用形式。
2. arguments 只能包含动作表为该动作列出的字段。禁止添加未列出的字段，禁止编造页面信息或坐标。
3. 坐标统一使用当前截图的 1000x1000 归一化空间，左上角为 (0,0)，取值 0..999。
动作表：
- left_click：必填 coordinate=[x,y]；不得有其他字段。
- scroll：必填 pixels（非零数字，正数向上、负数向下）；选填 coordinate=[x,y]。
- wait：必填 time（秒，0 < time <= 5）；不得有其他字段。
- list_windows：只有 action，不得有其他字段。
- switch_window：必填 title（窗口标题原文，非空字符串）；不得有其他字段。
- read_skill：必填 name（只能是已提供 Skill 索引中的名称）。
- terminate：必填 outcome（success / unqualified / needs_review）和 reason（中文，说明实际观察到的结果，不超过 300 字）；
  menu_box 只在 success 时必填，为 4 个 0..1000 的数字 [x1,y1,x2,y2]，且 x1<x2、y1<y2；非 success 不得提供 menu_box。
仅在两种情况下会容忍并纠正：遗漏 action 但 outcome 与 reason 完整（按 terminate 处理）；非 success 带空数组 menu_box（视同未提供）。
除这两种以外，任何字段缺失、类型错误、越界或多余字段都视为格式违规。"""


class TrialProtocolError(RuntimeError):
    pass


class TrialActionRejected(RuntimeError):
    """模型动作本身可修正：回传错误并保留会话，由 Agent 重新决策。"""


def load_skill(name: str) -> str:
    """按名称读取本地 Skill 文档；名称必须来自固定索引，避免任意路径读取。"""
    if name not in _SKILLS:
        raise TrialActionRejected("没有该 Skill；可用：" + "、".join(sorted(_SKILLS)))
    try:
        content = (_SKILLS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
    except OSError:
        raise TrialActionRejected("该 Skill 文档当前无法读取") from None
    if not content:
        raise TrialActionRejected("该 Skill 文档为空")
    return content[:_SKILL_MAX_CHARS] + ("\n（内容已截断）" if len(content) > _SKILL_MAX_CHARS else "")


def reference_image(path) -> dict | None:
    """把本轮二维码原图压缩成参考图；读取失败只表示没有参考资料，不影响启动。"""
    if not path:
        return None
    try:
        with Image.open(path) as image:
            image.load()
            image = image.convert("RGB")
            width, height = image.size
            if width * height > _REFERENCE_MAX_PIXELS:
                scale = math.sqrt(_REFERENCE_MAX_PIXELS / (width * height))
                image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                                     Image.Resampling.LANCZOS)
            with io.BytesIO() as data:
                image.save(data, format="JPEG", quality=85)
                return {"mime": "image/jpeg", "base64": base64.b64encode(data.getvalue()).decode("ascii"),
                        "width": image.width, "height": image.height}
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


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
                   trace_path: Path | None = None, trace_meta: dict | None = None,
                   source_image=None) -> tuple[dict, dict | None]:
    instruction = (
        f"目标门店：{store_name}，用餐人数 {people} 人。你正在操作已经打开的微信点餐小程序。"
        "\n目标：尽可能进入可采集的菜单页（能看到真实菜品，通常带价格），然后交接；后续程序负责滚动截图。"
        "\n执行原则：页面判断和下一步动作都由你负责，根据当前截图自主选择。欢迎页、人数选择、广告、加载、"
        "授权弹窗都是中间状态，优先尝试继续；弹窗以消除阻碍为目标，允许、拒绝、同意、关闭都可以；"
        "点击没反应就重新观察，可以重试同一位置；加载可 wait 两到三次；出现新小程序窗口先 list_windows 再 switch_window。"
        "\n终态：success=已看到可正常浏览的菜品菜单；unqualified=有明确证据表明本轮无法采集；"
        "needs_review=合理尝试后仍无法确定。暂时进不去菜单、不能下单、需要登录、点击失败、菜品少，都不等于 unqualified。"
        "\n业务经验可按需加载："
        + "；".join(f'read_skill(name="{name}")：{description}' for name, description in _SKILLS.items())
        + "。需要时先加载再操作，不需要就直接按截图判断。"
        "\n如果请求里附带二维码原图，它只用于理解门店背景；所有点击坐标仍以当前屏幕截图为准。"
        "\n输出必须严格符合系统提示中的固定输出契约。"
    )
    return _run_agent(model, instruction, max_steps, observe=observe, execute=execute,
                      check_stop=check_stop, log_path=log_path,
                      trace_path=trace_path, trace_meta=trace_meta, source_image=source_image)


def _run_agent(model: TrialModel, instruction: str, max_steps: int,
               *, observe, execute, check_stop, log_path: Path | None = None,
               trace_path: Path | None = None, trace_meta: dict | None = None,
               source_image=None) -> tuple[dict, dict | None]:
    config = {
        "model": {"baseURL": model.base_url, "apiKey": model.api_key, "model": GUI_PLUS_MODEL},
        "instruction": instruction, "max_steps": max_steps,
        "output_contract": _OUTPUT_CONTRACT,
        "trace_path": str(trace_path) if trace_path else "",
        "trace": {str(key): str(value) for key, value in (trace_meta or {}).items() if value is not None},
    }
    # 原始二维码照片只作为任务背景参考，不能当成可点击屏幕。
    reference = reference_image(source_image)
    if reference:
        config["reference_image"] = reference
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
        if method == "skill":
            # 读取 Skill 不改变页面，也不消耗当前帧；由 Agent 自主决定何时加载。
            name = params.get("name")
            if not isinstance(name, str) or not name.strip():
                send({"id": request_id, "ok": False, "error": "read_skill 缺少 name"})
                continue
            try:
                content = load_skill(name.strip())
            except TrialActionRejected as exc:
                send({"id": request_id, "ok": False, "error": str(exc)[:300]})
                continue
            send({"id": request_id, "ok": True, "result": {"name": name.strip(), "content": content}})
            continue
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
            try:
                command = _action_command(method, params, frame, image_size)
                check_stop()
                reply = execute(command, frame_id) or {}
            except TrialActionRejected as exc:
                # 模型动作本身可修正：回传错误让 Agent 重新决策，不终止本轮。
                send({"id": request_id, "ok": False, "error": str(exc)[:300]})
                continue
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
            raise TrialActionRejected("等待时间必须在 0 到 5 秒之间")
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
        raise TrialActionRejected("坐标超出当前截图范围，请在 1000x1000 归一化坐标内选择")
    if method == "click" and x > width * .55 and y < height * .045:
        # 右上角是小程序窗口胶囊（… / ◎），点中会退出小程序；本次不执行并明确告知 Agent。
        raise TrialActionRejected("点击落在窗口右上角胶囊保护区，会退出小程序；本次操作未执行，请换一个位置")
    result = {"action": method, "x": x * frame["window_width"] / width,
              "y": y * frame["window_height"] / height}
    if method == "scroll":
        if params.get("direction") not in ("Up", "Down"):
            raise TrialActionRejected("滚动方向无效，只允许 Up 或 Down")
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
