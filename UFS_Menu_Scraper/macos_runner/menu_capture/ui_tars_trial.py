"""GUI Agent SDK 和 UiPath 的本机 IPC 与结果契约。"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import queue
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from .config import ConfigStore
from .model import Region


_ENTRY = Path(__file__).resolve().parent.parent / "ui_tars" / "adapter.mjs"
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "ui_tars" / "action-schema.json"
GUI_PLUS_MODEL = "gui-plus-2026-02-26"
_GUI_PLUS_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_MAX_MODEL_IMAGE_PIXELS = 1_003_520
_REFERENCE_MAX_PIXELS = 640_000
_TERMINAL_OUTCOMES = ("success", "needs_review", "unqualified")
# UiPath 的滚动量是滚轮格数，不是屏幕像素；按 1000 归一化空间换算，并限制单次上限。
_SCROLL_UNITS_PER_SCREEN = 6
_SCROLL_UNITS_MAX = 6
# 动作后的局部变化：以点击落点为中心、窗口短边的 ±12% 作为关注区域。
# 全局缩略图差异会被整页内容稀释，识别不了“选人数”这类小控件是否响应，因此单独算一份局部差异。
_ROI_THUMB = 192
_ROI_HALF_RATIO = 0.12
_ROI_MIN_PIXELS = 16
# 点击后界面渲染有延迟；立刻截图会把“已生效”读成无变化，先留固定缓冲。
_CLICK_SETTLE_SECONDS = .35
# 窗口尺寸变化超过该比例时，上一帧的局部坐标不再可比，本次不报局部差异。
_ROI_WINDOW_TOLERANCE = 0.02
# 无变化时在送模截图上标注上次点击点：细环 + 环外十字，中心留空，避免遮住目标本身。
_MARK_MIN_RADIUS = 8
_MARK_RADIUS_RATIO = .02
_MARK_GAP = 3
_MARK_ARM = 6
_MARK_WIDTH = 1
_RED = (255, 64, 64)
_WHITE = (255, 255, 255)
# 第一次无变化重试的落点放大参考：窗口像素裁剪 240×240，放大 2 倍。
# 只给模型看落点和附近文字框，不引入第二套坐标；所有点击仍用整屏 0..1000。
_ZOOM_SIZE = 240
_ZOOM_SCALE = 2
_GREEN = (0, 170, 0)
# 与 progress-tracker.mjs 的 NO_CHANGE_DELTA / NO_CHANGE_ROI 保持一致：
# 只有这次点击确实没让整页或落点邻域变化，才标记落点并生成放大参考图。
_NO_CHANGE_DELTA = 1
_NO_CHANGE_ROI = .4
# OCR 参考只取点击点附近；条数、置信度和文本长度都要收敛，避免把整页文字塞给模型。
_OCR_RADIUS = 120
_OCR_LIMIT = 6
_OCR_MIN_CONFIDENCE = .6
# Adapter 上报的执行计数；格式修正单独计数，便于评估预算占用。
_METRIC_FIELDS = ("model_calls", "screenshots", "navigation_actions",
                  "format_retries", "tool_rejections", "format_failures", "no_progress_notices")


def _output_contract() -> str:
    """动作表由 action-schema.json 生成，保证模型看到的字段与解析器校验的是同一份定义。"""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    size = int(schema.get("coordinate_space") or 1000)
    actions = "\n".join(f"- {spec['doc']}" for spec in schema["actions"].values())
    return (
        "输出契约（固定协议，必须完全遵守；不合规会被拒绝并要求重写）\n"
        '1. 每轮只输出一个 tool_call：<tool_call>{"name":"computer_use","arguments":{...}}</tool_call>。\n'
        "   不要输出多个 tool_call，不要用自然语言描述动作，不要把 action 写成函数调用形式。\n"
        "2. arguments 只能包含动作表为该动作列出的字段。禁止添加未列出的字段，禁止编造页面信息或坐标。\n"
        f"3. 坐标统一使用当前截图的 {size}x{size} 归一化空间，左上角为 (0,0)，取值 0..{size - 1}。\n"
        "动作表：\n"
        f"{actions}\n"
        "任何字段缺失、类型错误、越界或多余字段都视为格式违规。"
    )


# 输出契约是固定协议：字段、类型、取值范围都写死，不允许模型自创字段。
_OUTPUT_CONTRACT = _output_contract()


class TrialProtocolError(RuntimeError):
    pass


class TrialActionRejected(RuntimeError):
    """模型动作本身可修正：回传错误并保留会话，由 Agent 重新决策。"""


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
        raise ValueError("GUI Agent 需要北京地域 GUI-Plus 配置，请在网页设置中保存对应模型和密钥，或设置环境变量")
    node = shutil.which("node")
    if not node or not (_ENTRY.parent / "node_modules/@gui-agent/agent-sdk/package.json").is_file():
        raise ValueError("GUI Agent 需要 Node.js 和 ui_tars 目录下已安装的 GUI Agent SDK（npm ci）")
    return TrialModel(node, base_url, key)


def run_navigation(model: TrialModel, store_name: str, people: int, max_steps: int,
                   *, observe, execute, check_stop, log_path: Path | None = None,
                   trace_path: Path | None = None, trace_meta: dict | None = None,
                   source_image=None, feedback: str = "") -> tuple[dict, dict | None]:
    instruction = (
        f"目标门店：{store_name}，用餐人数 {people} 人。你正在操作已经打开的微信点餐小程序。"
        "\n目标：进入目标门店可采集的菜品菜单。根据当前截图自主决定动作与终态，交接后由程序滚动截图。"
        "\n业务经验已全部写在系统提示词中，按其中的终态标准判断。"
        "\n如果请求里附带二维码原图，它只用于理解门店背景；所有点击坐标仍以当前屏幕截图为准。"
        "\n输出必须严格符合系统提示中的固定输出契约。"
    )
    if feedback:
        # 宿主退回时只说明已验证的事实（页面滚不动），不替 Agent 指定具体点击位置。
        instruction += f"\n上一轮交接未通过宿主校验：{feedback}\n请继续在当前页面操作，直到真正进入可滚动的菜品菜单再输出 success。"
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


class _WindowsPipeReader:
    """Windows 子进程管道不能交给 select；后台线程负责有界分块读取。"""

    def __init__(self, stream):
        self._stream = stream
        self._events: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=4)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._read, name="gui-agent-stdout", daemon=True)
        self._thread.start()

    def _publish(self, event: tuple[str, bytes | None]) -> bool:
        while not self._stopped.is_set():
            try:
                self._events.put(event, timeout=.05)
                return True
            except queue.Full:
                continue
        return False

    def _read(self):
        try:
            # read1 最多读取指定字节数，且不等待凑满整个缓冲区。
            read = getattr(self._stream, "read1", self._stream.read)
            while not self._stopped.is_set():
                data = read(8192)
                if not data:
                    self._publish(("eof", None))
                    return
                if not self._publish(("data", data)):
                    return
        except (OSError, ValueError):
            if not self._stopped.is_set():
                self._publish(("error", None))

    def read(self, timeout: float) -> bytes | None:
        kind, payload = self._events.get(timeout=timeout)
        if kind == "eof":
            return None
        if kind == "error":
            raise TrialProtocolError("GUI Agent 子进程输出读取失败")
        return payload if isinstance(payload, bytes) else b""

    def close(self):
        # 子进程由 _run_agent 统一终止；这里仅停止有界队列生产者。
        self._stopped.set()


def _exchange(child, config, observe, execute, check_stop, *, _windows_pipe=None):
    use_windows_pipe = os.name == "nt" if _windows_pipe is None else _windows_pipe
    pipe_reader = _WindowsPipeReader(child.stdout) if use_windows_pipe else None
    try:
        return _exchange_protocol(child, config, observe, execute, check_stop, pipe_reader)
    finally:
        if pipe_reader is not None:
            pipe_reader.close()


def _exchange_protocol(child, config, observe, execute, check_stop, pipe_reader=None):
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
    previous_thumb = None
    # 上一帧灰度图：点击发生在两帧之间，只能事后按落点回裁 ROI。
    previous_gray = None
    previous_image_size = None
    # 上一次点击的窗口坐标；用于在下一帧计算落点附近的局部变化。
    pending_focus = None
    previous_titles: set[str] = set()
    # OCR 结果按帧缓存：放大参考和随后的 ocr_context 用同一帧的行，避免重复识别。
    ocr_cache: dict = {}
    while True:
        check_stop()
        if b"\n" not in pending:
            if pipe_reader is not None:
                try:
                    data = pipe_reader.read(.2)
                except queue.Empty:
                    continue
            else:
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
            facts, previous_thumb, previous_gray = _frame_facts(
                frame, image_data, image_size, previous_thumb, previous_gray,
                previous_image_size, previous_titles, pending_focus)
            previous_image_size = image_size
            pending_focus = None
            previous_titles = {item["title"] for item in facts["windows"]}
            # delta/roi 已在未标记的原图上算完；只有这次点击确实无变化时，
            # 才在回传副本上标记落点，并生成一张落点放大参考图（坐标空间不变）。
            click = params.get("mark")
            missed = click is not None and _click_missed(facts)
            if missed:
                rows = _frame_ocr_rows(ocr_cache, frame_id, frame)
                zoom, zoom_meta = _zoom_reference(frame, click, rows)
                model_data = _mark_click(image_data, image_size, frame, click)
            else:
                zoom, zoom_meta, model_data = None, None, image_data
            result = {"frame_id": frame_id, "base64": base64.b64encode(model_data).decode("ascii"),
                      "facts": facts, "zoom": zoom, "zoom_meta": zoom_meta}
        elif method == "ocr_context":
            if frame is None or params.get("frame_id") != frame_id:
                raise TrialProtocolError("OCR 参考需要同轮截图")
            result = _ocr_context(frame, image_size, params,
                                  _frame_ocr_rows(ocr_cache, frame_id, frame))
        elif method == "crop_region":
            # 只读观察：按模型给的 0..1000 区域裁当前帧，供局部框选使用，不改变窗口状态。
            if frame is None or params.get("frame_id") != frame_id:
                raise TrialProtocolError("局部图需要同轮截图")
            result = _crop_region(frame, image_size, params)
        else:
            if frame is None or params.get("frame_id") != frame_id or action_sent:
                raise TrialProtocolError("GUI Agent 动作需要同轮新鲜截图")
            try:
                command = _action_command(method, params, frame, image_size)
                check_stop()
                reply = execute(command, frame_id) or {}
                if isinstance(command, dict) and command.get("action") == "click":
                    # 等界面渲染完成再让 Agent 截图，否则会把已生效的点击读成“没变化”。
                    time.sleep(_CLICK_SETTLE_SECONDS)
                    # 记下点击落点，下一帧按该区域算局部变化；若点击后切到了新窗口，坐标已不可比。
                    switched = isinstance(reply, dict) and reply.get("window_switch")
                    pending_focus = None if switched else {
                        "x": command["x"], "y": command["y"],
                        "width": frame["window_width"], "height": frame["window_height"]}
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


def _frame_facts(frame: dict, image_data: bytes, image_size: tuple[int, int],
                 previous_thumb, previous_gray, previous_image_size,
                 previous_titles: set[str], focus):
    """把宿主已经知道的事实压成一份观测包，随截图回传。

    只整理既有数据：窗口身份来自截图回执，窗口清单来自本机枚举，
    “画面变化”由相邻两帧缩略图差异算出。不新增采集动作。

    delta 是整页变化，会被大面积静态内容稀释；roi_delta 只看上一次点击落点附近，
    用于识别“选人数”这类小控件是否响应。两者都只是客观读数，不在此处判定业务成败。
    """
    target = frame.get("target_window") or {}
    with Image.open(io.BytesIO(image_data)) as image:
        gray = image.convert("L")
        thumb = gray.resize((48, 48))
        # ROI 先在原图上裁、再缩放：整图先缩到 192 会把 24px 控件压成几个像素。
        roi = _roi_crop(gray, focus, frame, image_size)
    delta = _thumb_delta(previous_thumb, thumb)
    # 跨帧识别“回到相似画面”用 64 位 dHash；精确 sha 对光标/动画太敏感，不能用于回访判断。
    signature = _frame_signature(gray)
    # ROI 是点击之后才能确定的区域，因此从上一帧灰度图里回裁，而不是复用上一帧的 ROI。
    roi_delta = None
    if focus is not None and previous_gray is not None and previous_image_size == image_size:
        roi_delta = _thumb_delta(_roi_crop(previous_gray, focus, frame, image_size), roi)
    windows = [{"title": str(item.get("title") or ""), "app": str(item.get("app") or ""),
                "width": int(item.get("width") or 0), "height": int(item.get("height") or 0),
                "foreground": bool(item.get("foreground"))}
               for item in frame.get("windows") or [] if item.get("title")]
    titles = [item["title"] for item in windows]
    facts = {
        "window": {"title": str(target.get("title") or ""), "app": str(target.get("app") or ""),
                   "foreground": bool(target.get("foreground")),
                   "width": int(frame.get("window_width") or image_size[0]),
                   "height": int(frame.get("window_height") or image_size[1])},
        "image": {"width": image_size[0], "height": image_size[1]},
        "delta": delta,
        "roi_delta": roi_delta,
        "signature": signature,
        "windows": windows,
        "new_windows": [title for title in titles if title not in previous_titles],
        "closed_windows": sorted(previous_titles - set(titles)),
    }
    return facts, thumb, gray


def _frame_signature(image) -> str:
    """64 位 dHash：只用于跨多帧判断“是否回到相似画面”，不做像素级相等判断。"""
    small = image.resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[row * 9 + col] > pixels[row * 9 + col + 1] else 0)
    return f"{bits:016x}"


def _thumb_delta(before, after):
    """两张灰度图的差异百分比；8 级以内的差异视为采样/抗锯齿噪声。"""
    from PIL import ImageChops

    if before is None or after is None:
        return None
    histogram = ImageChops.difference(before, after).histogram()
    total = sum(histogram) or 1
    return round(100 * sum(histogram[8:]) / total, 1)


def _roi_crop(gray, focus, frame, image_size):
    """按点击落点在原分辨率上裁出关注区域，再统一缩放到 _ROI_THUMB 便于逐帧比较。"""
    if focus is None:
        return None
    box = _roi_box(focus, frame, image_size)
    if box is None:
        return None
    return gray.crop(box).resize((_ROI_THUMB, _ROI_THUMB))


def _roi_box(focus: dict, frame: dict, image_size: tuple[int, int]):
    """把点击落点换算成截图上的像素矩形；窗口尺寸变化时局部坐标不可比，返回 None。"""
    width, height = int(focus.get("width") or 0), int(focus.get("height") or 0)
    if width <= 0 or height <= 0:
        return None
    current_width = int(frame.get("window_width") or width)
    current_height = int(frame.get("window_height") or height)
    if (abs(current_width - width) > width * _ROI_WINDOW_TOLERANCE
            or abs(current_height - height) > height * _ROI_WINDOW_TOLERANCE):
        return None
    half = max(min(width, height) * _ROI_HALF_RATIO, _ROI_MIN_PIXELS)
    scale_x, scale_y = image_size[0] / width, image_size[1] / height
    left = max(0, min(round((focus["x"] - half) * scale_x), image_size[0] - 1))
    top = max(0, min(round((focus["y"] - half) * scale_y), image_size[1] - 1))
    right = min(image_size[0], max(round((focus["x"] + half) * scale_x), left + 1))
    bottom = min(image_size[1], max(round((focus["y"] + half) * scale_y), top + 1))
    if right - left < 2 or bottom - top < 2:
        return None
    return left, top, right, bottom


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _mark_click(image_data: bytes, image_size: tuple[int, int], frame: dict, mark) -> bytes:
    """在送模副本上标出上次点击点：细环 + 环外十字，中心留空，避免遮住目标本身。

    只用于给模型观察，坐标空间不变；宿主计算 delta/roi 仍然基于未标记的原图。
    """
    if not isinstance(mark, dict):
        return image_data
    x, y = mark.get("x"), mark.get("y")
    if not _finite(x) or not _finite(y):
        return image_data
    width = int(frame.get("window_width") or image_size[0])
    height = int(frame.get("window_height") or image_size[1])
    if width <= 0 or height <= 0 or not (0 <= x < width and 0 <= y < height):
        return image_data
    try:
        from PIL import ImageDraw
        with Image.open(io.BytesIO(image_data)).convert("RGB") as image:
            draw = ImageDraw.Draw(image)
            cx = x * image.width / width
            cy = y * image.height / height
            radius = max(_MARK_MIN_RADIUS, round(min(image.size) * _MARK_RADIUS_RATIO))
            ring = (cx - radius, cy - radius, cx + radius, cy + radius)
            # 白描边垫底，保证浅色/深色背景上都看得见。
            draw.ellipse(ring, outline=_WHITE, width=_MARK_WIDTH + 2)
            draw.ellipse(ring, outline=_RED, width=_MARK_WIDTH)
            outer = radius + _MARK_GAP
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                start = (cx + dx * outer, cy + dy * outer)
                end = (cx + dx * (outer + _MARK_ARM), cy + dy * (outer + _MARK_ARM))
                draw.line((start, end), fill=_WHITE, width=_MARK_WIDTH + 2)
                draw.line((start, end), fill=_RED, width=_MARK_WIDTH)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
    except (OSError, ValueError):
        return image_data


def _click_missed(facts: dict) -> bool:
    """这次点击是否真的没让页面或落点邻域发生变化。

    与 progress-tracker.mjs 的判定保持一致：局部读数缺失（切窗口、窗口尺寸变化）时不下结论，
    避免把“无法比较”误当成“没点到”。
    """
    roi = facts.get("roi_delta")
    delta = facts.get("delta")
    if not _finite(roi) or roi >= _NO_CHANGE_ROI:
        return False
    return delta is None or not _finite(delta) or delta < _NO_CHANGE_DELTA


def _frame_ocr_rows(cache: dict, frame_id: str, frame: dict) -> list:
    """按帧缓存 OCR 行；识别失败返回空列表，不影响标记和放大参考。"""
    if cache.get("frame_id") == frame_id:
        return cache.get("rows") or []
    try:
        from .image_tools import image_ocr
        rows = image_ocr(Path(frame["path"]))
    except Exception:
        rows = []
    cache.clear()
    cache.update({"frame_id": frame_id, "rows": rows})
    return rows


def _crop_region(frame: dict, image_size: tuple[int, int], params: dict) -> dict:
    """按 0..1000 全窗区域裁剪当前帧并放大，供模型看清小控件。

    坐标以送模图（image_size）为基准，裁剪时再等比映射回原图，保证“框到的位置”
    和模型在截图里看到的位置一致。返回的 crop 是原图像素，仅用于留痕与回执。
    """
    box = params.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise TrialActionRejected("局部图区域必须是 [x1,y1,x2,y2]")
    try:
        x1, y1, x2, y2 = (float(value) for value in box)
    except (TypeError, ValueError):
        raise TrialActionRejected("局部图区域必须是 4 个数字") from None
    if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
        raise TrialActionRejected("局部图区域越界或宽高为 0")
    model_width, model_height = image_size
    frame_width, frame_height = frame["width"], frame["height"]
    left = max(0, round(x1 / 1000 * model_width))
    right = min(model_width, round(x2 / 1000 * model_width))
    top = max(0, round(y1 / 1000 * model_height))
    bottom = min(model_height, round(y2 / 1000 * model_height))
    if right - left < 4 or bottom - top < 4:
        raise TrialActionRejected("局部图区域太小，请扩大 box")
    scale_x, scale_y = frame_width / model_width, frame_height / model_height
    source_box = (round(left * scale_x), round(top * scale_y),
                  round(right * scale_x), round(bottom * scale_y))
    with Image.open(Path(frame["path"])) as source:
        image = source.convert("RGB")
        if image.size != (frame_width, frame_height):
            image = image.resize((frame_width, frame_height), Image.Resampling.LANCZOS)
        crop = image.crop(source_box)
        # 小区域多放大，保证 1–2 像素的控件也看得清；上限 6 倍，避免局部图过大。
        longest = max(1, max(crop.size))
        scale = max(1, min(6, round(480 / longest)))
        if scale > 1:
            crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
    return {"base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            "crop": list(source_box), "scale": scale, "model_size": list(image_size)}


def _zoom_reference(frame: dict, click, rows) -> tuple[str | None, dict | None]:
    """生成落点周围的 2 倍放大参考图：红叉=实际落点，绿框=附近 OCR 文字。

    只用于让模型核对落点是否点准，不是当前可点击屏幕；输出图的像素坐标不参与任何点击换算。
    """
    if not isinstance(click, dict):
        return None, None
    x, y = click.get("x"), click.get("y")
    width = int(frame.get("window_width") or frame.get("width") or 0)
    height = int(frame.get("window_height") or frame.get("height") or 0)
    if not _finite(x) or not _finite(y) or width <= 0 or height <= 0:
        return None, None
    if not (0 <= x < width and 0 <= y < height):
        return None, None
    crop_width = min(_ZOOM_SIZE, width)
    crop_height = min(_ZOOM_SIZE, height)
    left = int(min(max(round(x - crop_width / 2), 0), width - crop_width))
    top = int(min(max(round(y - crop_height / 2), 0), height - crop_height))
    try:
        from PIL import ImageDraw
        with Image.open(Path(frame["path"])) as source:
            image = source.convert("RGB")
            if image.size != (width, height):
                image = image.resize((width, height), Image.Resampling.LANCZOS)
            zoom = image.crop((left, top, left + crop_width, top + crop_height)).resize(
                (crop_width * _ZOOM_SCALE, crop_height * _ZOOM_SCALE), Image.Resampling.LANCZOS)
            draw = ImageDraw.Draw(zoom)
            for row in rows or []:
                try:
                    box_left, box_top = float(row["x"]), float(row["y"])
                    box_right = box_left + float(row["width"])
                    box_bottom = box_top + float(row["height"])
                    confidence = float(row["confidence"])
                except (KeyError, TypeError, ValueError):
                    continue
                if confidence < _OCR_MIN_CONFIDENCE or not str(row.get("text") or "").strip():
                    continue
                if (box_right < left or box_left > left + crop_width
                        or box_bottom < top or box_top > top + crop_height):
                    continue
                scaled = tuple(round((value - origin) * _ZOOM_SCALE) for value, origin in (
                    (box_left, left), (box_top, top), (box_right, left), (box_bottom, top)))
                draw.rectangle(scaled, outline=_GREEN, width=2)
            # 红叉标出实际落点；先画白描边，保证在深色/浅色文字上都可见。
            cx, cy = round((x - left) * _ZOOM_SCALE), round((y - top) * _ZOOM_SCALE)
            arm = 10
            for start, end in (((cx - arm, cy), (cx + arm, cy)), ((cx, cy - arm), (cx, cy + arm))):
                draw.line((start, end), fill=_WHITE, width=3)
                draw.line((start, end), fill=_RED, width=1)
            buffer = io.BytesIO()
            zoom.save(buffer, format="PNG")
            meta = {"crop": [left, top, crop_width, crop_height], "scale": _ZOOM_SCALE,
                    "click": [round(x), round(y)]}
            return base64.b64encode(buffer.getvalue()).decode("ascii"), meta
    except (OSError, ValueError):
        return None, None


def _ocr_context(frame: dict, image_size: tuple[int, int], params: dict, rows=None) -> dict:
    """返回点击点附近的 OCR 文字框，坐标换算到模型的 0..1000 空间。

    OCR 只是语义参考：识别失败、没有文字或依赖缺失都返回空列表，不影响导航。
    """
    empty = {"items": [], "note": "OCR 仅作参考，不是可点击区域；附近未识别到可用文字。"}
    x, y = params.get("x"), params.get("y")
    if not _finite(x) or not _finite(y):
        return empty
    width = int(frame.get("window_width") or image_size[0])
    height = int(frame.get("window_height") or image_size[1])
    if width <= 0 or height <= 0 or not (0 <= x < width and 0 <= y < height):
        return empty
    radius = params.get("radius")
    radius = radius if _finite(radius) and 0 < radius <= 400 else _OCR_RADIUS
    # 全部按窗口像素计算：OCR 跑在原始截图上，行坐标就是窗口像素，避免和送模缩放混用。
    extent = {
        "left": max(0.0, x - radius),
        "top": max(0.0, y - radius),
        "right": min(float(width), x + radius),
        "bottom": min(float(height), y + radius),
    }
    if rows is None:
        try:
            from .image_tools import image_ocr
            rows = image_ocr(Path(frame["path"]))
        except Exception:
            return empty
    items = _ocr_near(rows, (x, y), (width, height), extent)
    return {
        "roi": [_normalize(extent["left"], width), _normalize(extent["top"], height),
                _normalize(extent["right"], width), _normalize(extent["bottom"], height)],
        "click_point": [_normalize(x, width), _normalize(y, height)],
        "items": items,
        "note": "OCR 仅作参考，不是可点击区域；点击坐标仍由你决定。",
    }


def _normalize(value: float, total: int) -> int:
    return int(round(value * 1000 / total)) if total else 0


def _ocr_near(rows, click: tuple[float, float], image_size: tuple[int, int],
              extent: dict) -> list[dict]:
    """筛出与 ROI 相交的文字框，按“是否覆盖点击点 + 距离”排序，去重后限条数。"""
    width, height = image_size
    cx, cy = click
    candidates = []
    for row in rows:
        try:
            left, top = float(row["x"]), float(row["y"])
            right, bottom = left + float(row["width"]), top + float(row["height"])
            confidence = float(row["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(row.get("text") or "").strip()[:20]
        if not text or confidence < _OCR_MIN_CONFIDENCE:
            continue
        if (right < extent["left"] or left > extent["right"]
                or bottom < extent["top"] or top > extent["bottom"]):
            continue
        # 距离统一在 0..1000 空间里算，和模型输出的坐标同一套单位。
        box = [_normalize(left, width), _normalize(top, height),
               _normalize(right, width), _normalize(bottom, height)]
        point = [_normalize(cx, width), _normalize(cy, height)]
        dx = max(box[0] - point[0], 0, point[0] - box[2])
        dy = max(box[1] - point[1], 0, point[1] - box[3])
        inside = dx == 0 and dy == 0
        candidates.append({
            "text": text,
            "bbox": box,
            # 中心点直接给出，模型不必自己算中值；它仍只是文字框中心，不保证是按钮热区。
            "center": [int(round((box[0] + box[2]) / 2)), int(round((box[1] + box[3]) / 2))],
            "confidence": round(confidence, 2),
            "distance": math.hypot(dx, dy),
            "inside": inside,
            "relation": _ocr_relation(point, box, dx, dy, inside),
        })
    candidates.sort(key=lambda item: (0 if item["inside"] else 1, item["distance"]))
    output = []
    for item in candidates:
        if any(_same_ocr_item(item, kept) for kept in output):
            continue
        output.append({key: item[key] for key in ("text", "bbox", "center", "confidence", "relation")})
        if len(output) >= _OCR_LIMIT:
            break
    return output


def _ocr_relation(point, box, dx: float, dy: float, inside: bool) -> str:
    if inside:
        return "点击点位于该文字框内"
    vertical = "上方" if box[3] < point[1] else ("下方" if box[1] > point[1] else "")
    horizontal = "左方" if box[2] < point[0] else ("右方" if box[0] > point[0] else "")
    return f"在点击点{vertical}{horizontal}，距离约 {int(round(math.hypot(dx, dy)))}"


def _same_ocr_item(item: dict, kept: dict) -> bool:
    """同一段文字且框高度重叠时只保留一个，避免重复项占满名额。"""
    if item["text"] != kept["text"]:
        return False
    left, top = max(item["bbox"][0], kept["bbox"][0]), max(item["bbox"][1], kept["bbox"][1])
    right, bottom = min(item["bbox"][2], kept["bbox"][2]), min(item["bbox"][3], kept["bbox"][3])
    if right <= left or bottom <= top:
        return False
    area = (right - left) * (bottom - top)
    smaller = min((item["bbox"][2] - item["bbox"][0]) * (item["bbox"][3] - item["bbox"][1]),
                  (kept["bbox"][2] - kept["bbox"][0]) * (kept["bbox"][3] - kept["bbox"][1]))
    return bool(smaller) and area / smaller >= .6


def _cmd_wait(params, frame, image_size):
    seconds = params.get("seconds")
    if not _finite(seconds) or not 0 <= seconds <= 5:
        raise TrialActionRejected("等待时间必须在 0 到 5 秒之间")
    return {"action": "noop", "seconds": seconds}


def _cmd_windows(params, frame, image_size):
    return {"action": "windows"}


def _cmd_switch_window(params, frame, image_size):
    title = params.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TrialProtocolError("GUI Agent 切换窗口缺少标题")
    return {"action": "switch_window", "title": title.strip()}


def _cmd_type_text(params, frame, image_size):
    content = params.get("text")
    if not isinstance(content, str) or not content.strip():
        raise TrialActionRejected("输入文本不能为空")
    return {"action": "type_text", "text": content[:200]}


def _cmd_key(params, frame, image_size):
    name = params.get("key")
    if not isinstance(name, str) or not name.strip():
        raise TrialActionRejected("按键不能为空，例如 enter、esc、ctrl+v")
    return {"action": "key", "key": name.strip()[:40]}


def _checked_point(params, image_size):
    """坐标是宿主的信任边界：即使 Node 侧已经校验过，这里仍按当前截图尺寸复核。"""
    x, y = params.get("x"), params.get("y")
    width, height = image_size
    if not _finite(x) or not _finite(y) or not (0 <= x < width and 0 <= y < height):
        raise TrialActionRejected("坐标超出当前截图范围，请在 1000x1000 归一化坐标内选择")
    return x, y, width, height


def _cmd_click(params, frame, image_size):
    x, y, width, height = _checked_point(params, image_size)
    if x > width * .55 and y < height * .045:
        # 右上角是小程序窗口胶囊（… / ◎），点中会退出小程序；本次不执行并明确告知 Agent。
        raise TrialActionRejected("点击落在窗口右上角胶囊保护区，会退出小程序；本次操作未执行，请换一个位置")
    return {"action": "click", "x": x * frame["window_width"] / width,
            "y": y * frame["window_height"] / height}


def _cmd_scroll(params, frame, image_size):
    x, y, width, height = _checked_point(params, image_size)
    pixels = params.get("pixels")
    if not _finite(pixels) or pixels == 0:
        raise TrialActionRejected("滚动需要非零的 pixels：正数向上、负数向下")
    # 模型给的是有符号屏幕幅度；UiPath 只接受滚轮格数，因此换算后限制在 1..6 格。
    units = round(abs(pixels) / 1000 * _SCROLL_UNITS_PER_SCREEN)
    return {"action": "scroll", "x": x * frame["window_width"] / width,
            "y": y * frame["window_height"] / height,
            "direction": "Up" if pixels > 0 else "Down",
            "units": max(1, min(_SCROLL_UNITS_MAX, units))}


# 宿主方法名 -> 处理函数。schema 里的 host.method 必须能在这里找到，契约测试会检查覆盖。
_HOST_COMMANDS = {
    "wait": _cmd_wait,
    "windows": _cmd_windows,
    "switch_window": _cmd_switch_window,
    "type_text": _cmd_type_text,
    "key": _cmd_key,
    "click": _cmd_click,
    "scroll": _cmd_scroll,
}


def _action_command(method, params, frame, image_size):
    handler = _HOST_COMMANDS.get(method)
    if handler is None:
        raise TrialProtocolError("GUI Agent 请求了试验范围以外的动作")
    return handler(params, frame, image_size)


def _result(event, frame, frame_id, image_size, action_sent):
    outcome, reason = event.get("outcome"), event.get("reason")
    if outcome not in _TERMINAL_OUTCOMES or not isinstance(reason, str) or not reason.strip():
        raise TrialProtocolError("GUI Agent 终态缺少有效状态或理由")
    data = {"outcome": outcome, "reason": reason.strip()[:500]}
    metrics = event.get("metrics")
    if isinstance(metrics, dict):
        data["metrics"] = {key: int(metrics[key]) for key in _METRIC_FIELDS
                           if isinstance(metrics.get(key), int) and not isinstance(metrics.get(key), bool)
                           and metrics[key] >= 0}
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
