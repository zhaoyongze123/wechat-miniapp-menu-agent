from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from menu_capture.config import ConfigStore
from menu_capture.materials import MaterialService
from menu_capture.store import TaskStore
from menu_capture.ui_tars_trial import (
    GUI_PLUS_MODEL,
    TrialModel,
    TrialProtocolError,
    _run_agent,
    run_navigation,
    trial_model_from_env,
)
from menu_capture.uipath_runner import NeedsReview, RobotCommandError, UiPathRunner


_SDK = Path(__file__).resolve().parents[1] / "ui_tars/node_modules/@gui-agent/agent-sdk/package.json"
_MAX_MODEL_IMAGE_PIXELS = 1_003_520


def _runtime_available() -> bool:
    return bool(shutil.which("node") and _SDK.is_file())


def _tool_call(arguments: dict, *, action_line: str = "Action: follow the requested safe navigation.") -> str:
    payload = {"name": "computer_use", "arguments": arguments}
    return f'{action_line}\n<tool_call>{json.dumps(payload, ensure_ascii=False)}</tool_call>'


def _terminal(outcome: str, *, reason="已核对页面", menu_box=None) -> str:
    args = {"action": "terminate", "outcome": outcome, "reason": reason}
    if menu_box is not None:
        args["menu_box"] = menu_box
    return _tool_call(args, action_line="Action: report the observed terminal state.")


def _image_data_items(value: object) -> list[bytes]:
    if isinstance(value, dict):
        found = []
        for key, nested in value.items():
            if key == "url" and isinstance(nested, str) and nested.startswith("data:image/"):
                try:
                    encoded = nested.split(",", 1)[1]
                    found.append(base64.b64decode(encoded))
                except (IndexError, ValueError, OSError):
                    pass
            else:
                found.extend(_image_data_items(nested))
        return found
    elif isinstance(value, list):
        found = []
        for nested in value:
            found.extend(_image_data_items(nested))
        return found
    return []


def _image_data_bytes(value: object) -> bytes | None:
    images = _image_data_items(value)
    return images[0] if images else None


def _image_data_dimensions(value: object) -> tuple[int, int] | None:
    encoded = _image_data_bytes(value)
    if encoded is None:
        return None
    try:
        with Image.open(io.BytesIO(encoded)) as image:
            return image.size
    except OSError:
        return None


@pytest.fixture
def model_server():
    class Handler(BaseHTTPRequestHandler):
        responses: list[str | object] = []
        requests: list[dict] = []
        delay_seconds = 0.0

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            type(self).requests.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            })
            usage = None
            if not type(self).responses:
                content = ""
            else:
                response = type(self).responses.pop(0)
                if isinstance(response, dict):
                    # 支持显式 usage，用来验证流末尾用量块的解析与入账。
                    content = response.get("content", "")
                    usage = response.get("usage")
                else:
                    content = response(body) if callable(response) else response
            if type(self).delay_seconds:
                time.sleep(type(self).delay_seconds)
            chunks = [content[index:index + 31] for index in range(0, len(content), 31)] or [""]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for index, chunk in enumerate(chunks):
                    payload = {
                        "id": "chatcmpl-local-test", "object": "chat.completion.chunk", "created": 1,
                        "model": body.get("model", GUI_PLUS_MODEL),
                        "choices": [{
                            "index": 0,
                            "delta": {"content": chunk},
                            "finish_reason": "stop" if index == len(chunks) - 1 else None,
                        }],
                    }
                    self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                if usage is not None:
                    # 真实服务把 usage 放在 choices 为空的尾块里。
                    tail = {
                        "id": "chatcmpl-local-test", "object": "chat.completion.chunk", "created": 1,
                        "model": body.get("model", GUI_PLUS_MODEL), "choices": [], "usage": usage,
                    }
                    self.wfile.write(b"data: " + json.dumps(tail, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except OSError:
                # Cancellation tests intentionally close the SDK connection mid-stream.
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Handler.responses = []
    Handler.requests = []
    Handler.delay_seconds = 0.0
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _model(server, *, key="test-only-key") -> TrialModel:
    node = shutil.which("node")
    assert node
    return TrialModel(node, f"http://127.0.0.1:{server.server_port}/v1", key)


def _frame_factory(tmp_path: Path, *, width=320, height=200, window_width=None, window_height=None,
                   square: tuple[int, int, int] | None = None):
    frames = []

    def observe(sequence: int) -> dict:
        path = tmp_path / f"frame-{sequence}.png"
        image = Image.new("RGB", (width, height), "white" if sequence == 1 else "seagreen")
        if square:
            x, y, size = square
            ImageDraw.Draw(image).rectangle((x, y, x + size - 1, y + size - 1), fill="#E600FF")
        image.save(path)
        frame = {
            "path": str(path), "width": width, "height": height,
            "window_width": window_width or width, "window_height": window_height or height,
        }
        frames.append(frame)
        return frame

    return observe, frames


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_real_agent_sdk_sse_clicks_then_observes_fresh_frame_before_success(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [
        _tool_call({"action": "left_click", "coordinate": [375, 450]}),
        _terminal("success", menu_box=[100, 100, 900, 900]),
    ]
    observe, frames = _frame_factory(tmp_path)
    actions = []

    result, terminal_frame = run_navigation(
        _model(server), "测试门店", 2, 4,
        observe=observe,
        execute=lambda command, frame_id: actions.append((command, frame_id)),
        check_stop=lambda: None,
    )

    assert result["outcome"] == "success"
    assert result["reason"] == "已核对页面"
    assert "evidence" not in result
    assert len(frames) == 2
    assert terminal_frame == frames[1]
    assert actions == [({"action": "click", "x": 120, "y": 90}, "1")]
    assert len(handler.requests) == 2
    assert all(request["path"] == "/v1/chat/completions" for request in handler.requests)
    assert all(request["body"].get("stream") is True for request in handler.requests)
    assert all(request["body"].get("enable_thinking") is False for request in handler.requests)
    assert all("thinking" not in request["body"] for request in handler.requests)
    assert all(request["authorization"] == "Bearer test-only-key" for request in handler.requests)
    assert all(request["body"]["model"] == GUI_PLUS_MODEL for request in handler.requests)
    for request in handler.requests:
        assert _image_data_dimensions(request["body"]["messages"]) == (320, 200)
    first_images = _image_data_items(handler.requests[0]["body"]["messages"])
    later_images = _image_data_items(handler.requests[1]["body"]["messages"])
    assert later_images[-1] != first_images[-1], "second turn should include the post-click screenshot"


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_large_model_image_maps_normalized_click_back_to_original_window_points(model_server, tmp_path):
    server, handler = model_server
    model_dimensions = []
    actions = []
    observe, frames = _frame_factory(
        tmp_path, width=1600, height=1200, window_width=800, window_height=600,
    )

    def click_in_lower_right(body):
        dimensions = _image_data_dimensions(body["messages"])
        assert dimensions is not None
        model_dimensions.append(dimensions)
        return _tool_call({"action": "left_click", "coordinate": [650, 500]})

    def finish_full_model_box(body):
        dimensions = _image_data_dimensions(body["messages"])
        assert dimensions is not None
        return _terminal("success", menu_box=[0, 0, 1000, 1000])

    handler.responses = [click_in_lower_right, finish_full_model_box]
    result, terminal_frame = run_navigation(
        _model(server), "测试门店", 2, 4,
        observe=observe,
        execute=lambda command, frame_id: actions.append((command, frame_id)),
        check_stop=lambda: None,
    )

    assert result["outcome"] == "success"
    assert model_dimensions[0][0] * model_dimensions[0][1] <= _MAX_MODEL_IMAGE_PIXELS
    assert model_dimensions[0] < (1600, 1200)
    assert terminal_frame == frames[1]
    assert terminal_frame["width"] == 1600 and terminal_frame["height"] == 1200
    assert actions == [(
        {"action": "click", "x": pytest.approx(650 * 800 / 1000),
         "y": pytest.approx(500 * 600 / 1000)}, "1",
    )]
    # The normalized model point maps through the sent image, then to original window coordinates.
    assert 650 * 1600 / 1000 > 1000
    assert result["menu_region"] == {"x": 0, "y": 0, "width": 1600, "height": 1200}


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_success_menu_box_with_small_pixel_overflow_is_clamped(model_server, tmp_path):
    server, handler = model_server
    # 模型把截图像素高（200 缩放后的 1017）当作归一化坐标；5% 内的越界按边界钳制。
    handler.responses = [_terminal("success", menu_box=[0, 0, 1000, 1017])]
    observe, frames = _frame_factory(tmp_path)

    result, terminal_frame = run_navigation(
        _model(server), "测试门店", 2, 2,
        observe=observe, execute=lambda *_: pytest.fail("终态不应执行动作"), check_stop=lambda: None,
    )

    assert result["outcome"] == "success"
    assert result["menu_region"] == {"x": 0, "y": 0, "width": 320, "height": 200}
    assert terminal_frame == frames[0]


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_success_menu_box_far_outside_tolerance_is_rejected(model_server, tmp_path):
    server, handler = model_server
    # 只容忍真实 ±5%（-50..1050）；-800 已无法确认语义，必须拒绝并让 Agent 重新输出。
    handler.responses = [_terminal("success", menu_box=[-800, 100, 900, 900])] * 3
    observe, _frames = _frame_factory(tmp_path)
    actions = []

    result, _frame = run_navigation(
        _model(server), "测试门店", 2, 4,
        observe=observe, execute=lambda *args: actions.append(args), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert result.get("source") == "runtime"
    assert actions == []
    assert len(handler.requests) == 3
    # 三次都是格式错误，其中两次获得修正机会；最后一次终止也必须计入失败次数。
    assert result["metrics"]["format_failures"] == 3
    assert result["metrics"]["format_retries"] == 2


def test_scroll_pixels_map_to_direction_and_bounded_units():
    from menu_capture.ui_tars_trial import _action_command

    frame = {"window_width": 100, "window_height": 200}
    cases = [(-100, "Down", 1), (-800, "Down", 5), (1000, "Up", 6), (9000, "Up", 6)]
    for pixels, direction, units in cases:
        command = _action_command("scroll", {"x": 50, "y": 50, "pixels": pixels}, frame, (200, 400))
        assert command["action"] == "scroll"
        assert command["direction"] == direction
        assert command["units"] == units

    with pytest.raises(Exception, match="pixels"):
        _action_command("scroll", {"x": 50, "y": 50, "pixels": 0}, frame, (200, 400))


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
@pytest.mark.parametrize("pixels,units", [(-100, 1), (-800, 5)])
def test_scroll_magnitude_reaches_host_operator(model_server, tmp_path, pixels, units):
    server, handler = model_server
    handler.responses = [
        _tool_call({"action": "scroll", "pixels": pixels}),
        _terminal("needs_review", reason="滚动后重新核对页面"),
    ]
    observe, _frames = _frame_factory(tmp_path)
    actions = []

    result, _frame = run_navigation(
        _model(server), "测试门店", 1, 4,
        observe=observe, execute=lambda command, frame_id: actions.append(command), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert len(actions) == 1
    assert actions[0]["action"] == "scroll"
    assert actions[0]["direction"] == "Down"
    assert actions[0]["units"] == units


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_missing_required_field_feedback_allows_correction(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [
        _tool_call({"action": "wait"}),
        _terminal("needs_review", reason="按反馈补齐字段后核对完成"),
    ]
    observe, _frames = _frame_factory(tmp_path)

    result, _frame = run_navigation(
        _model(server), "测试门店", 1, 4,
        observe=observe, execute=lambda *args: pytest.fail("格式错误不应执行动作"), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert len(handler.requests) == 2
    feedback = _all_text(handler.requests[1]["body"]["messages"])
    assert "不符合工具协议" in feedback and "time" in feedback
    assert result["metrics"]["format_failures"] == 1
    assert result["metrics"]["format_retries"] == 1


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_parse_failures_reset_after_valid_action(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [
        _tool_call({"action": "wait"}),
        _tool_call({"action": "left_click", "coordinate": [375, 450]}),
        _tool_call({"action": "wait"}),
        _tool_call({"action": "left_click", "coordinate": [375, 450]}),
        _terminal("needs_review", reason="修正后完成核对"),
    ]
    observe, _frames = _frame_factory(tmp_path)
    actions = []

    result, _frame = run_navigation(
        _model(server), "测试门店", 1, 6,
        observe=observe, execute=lambda command, frame_id: actions.append(command), check_stop=lambda: None,
    )

    # 两次格式错误之间都有合法动作，连续计数已重置，不应被耗尽后终止。
    assert result["outcome"] == "needs_review"
    assert result.get("source") != "runtime"
    assert len(actions) == 2
    assert len(handler.requests) == 5
    assert result["metrics"]["format_failures"] == 2
    assert result["metrics"]["format_retries"] == 2


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_navigation_puts_full_guide_in_system_prompt(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [_terminal("needs_review", reason="页面状态需要人工核实")]
    observe, _frames = _frame_factory(tmp_path)

    result, _frame = run_navigation(
        _model(server), "测试门店", 1, 2,
        observe=observe, execute=lambda *_: pytest.fail("终态不应执行动作"), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert len(handler.requests) == 1
    messages = handler.requests[0]["body"]["messages"]
    system_text = "\n".join(str(m.get("content")) for m in messages if m.get("role") == "system")
    # 业务经验只维护系统提示词一份：业务背景、三个终态与正常前置页面都必须常驻。
    assert "点餐小程序导航经验" in system_text
    assert "你正在完成的业务" in system_text
    assert "三个终态" in system_text
    assert "就餐人数、堂食方式、桌号等选择页面" in system_text
    user_text = "\n".join(str(m.get("content")) for m in messages if m.get("role") == "user")
    assert "当前具体页面和状态尚未确认" in user_text
    assert "实际页面证据优先" in user_text


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_needs_review_terminal_ignores_empty_menu_box(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [_terminal("needs_review", reason="门店身份仍无法确认", menu_box=[])]
    observe, frames = _frame_factory(tmp_path)

    result, terminal_frame = run_navigation(
        _model(server), "身份待确认门店", 1, 2,
        observe=observe, execute=lambda *_: pytest.fail("终态不应执行动作"), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert "身份" in result["reason"]
    assert terminal_frame == frames[0]


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_capsule_click_is_returned_to_agent_instead_of_ending_run(model_server, tmp_path):
    server, handler = model_server
    # 胶囊区点击应被拒绝并把错误反馈给 Agent，下一次输出仍可正常结束本轮。
    handler.responses = [
        _tool_call({"action": "left_click", "coordinate": [800, 10]}),
        _terminal("needs_review", reason="胶囊区被拒绝，重新观察后仍无法确认"),
    ]
    observe, _frames = _frame_factory(tmp_path)
    executed = []

    result, _frame = run_navigation(
        _model(server), "测试门店", 1, 4,
        observe=observe, execute=lambda *args: executed.append(args), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert executed == []
    assert len(handler.requests) == 2


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_needs_review_terminal_does_not_claim_menu_success(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [_terminal("needs_review", reason="门店身份仍无法确认")]
    observe, frames = _frame_factory(tmp_path)

    result, terminal_frame = run_navigation(
        _model(server), "身份待确认门店", 1, 2,
        observe=observe, execute=lambda *_: pytest.fail("终态不应执行动作"), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert "身份" in result["reason"]
    assert "evidence" not in result
    assert terminal_frame == frames[0]


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_explicit_unqualified_terminal_keeps_reason_and_evidence_frame(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [
        _terminal("unqualified", reason="页面明确提示暂停扫码点餐，且没有可浏览菜单"),
    ]
    observe, frames = _frame_factory(tmp_path)

    result, terminal_frame = run_navigation(
        _model(server), "暂停点餐的门店", 1, 2,
        observe=observe, execute=lambda *_: pytest.fail("终态不应执行动作"), check_stop=lambda: None,
    )

    assert result["outcome"] == "unqualified"
    assert result["reason"] == "页面明确提示暂停扫码点餐，且没有可浏览菜单"
    assert "code" not in result
    assert terminal_frame == frames[0]


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_terminal_without_action_field_is_accepted(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [
        '<tool_call>{"name":"computer_use","arguments":{"outcome":"needs_review","reason":"门店身份无法确认"}}</tool_call>',
    ]
    observe, frames = _frame_factory(tmp_path)

    result, terminal_frame = run_navigation(
        _model(server), "身份待确认门店", 1, 2,
        observe=observe, execute=lambda *_: pytest.fail("终态不应执行动作"), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert result["reason"] == "门店身份无法确认"
    assert terminal_frame == frames[0]


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_action_name_with_empty_parentheses_is_normalized(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [
        '<tool_call>{"name":"computer_use","arguments":{"action":"list_windows()"}}</tool_call>',
        _terminal("needs_review", reason="仅有一个小程序窗口，身份待确认"),
    ]
    observe, _frames = _frame_factory(tmp_path)
    actions = []

    result, _frame = run_navigation(
        _model(server), "测试门店", 1, 4,
        observe=observe, execute=lambda command, frame_id: actions.append((command, frame_id)),
        check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert actions[0][0]["action"] == "windows"
    assert len(handler.requests) == 2


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_old_action_syntax_is_not_executed(model_server, tmp_path):
    server, handler = model_server
    # 可修正的格式错误会给两次反馈机会，第三次仍不合法才终止。
    handler.responses = ["Action: click(start_box='[0.1,0.1,0.2,0.2]')"] * 3
    observe, _frames = _frame_factory(tmp_path)
    actions = []

    try:
        result, _terminal_frame = run_navigation(
        _model(server), "测试门店", 1, 4,
            observe=observe, execute=lambda *args: actions.append(args), check_stop=lambda: None,
        )
    except TrialProtocolError:
        result = {"outcome": "protocol_error"}

    assert result["outcome"] == "needs_review"
    assert result.get("source") == "runtime"
    assert actions == []
    assert len(handler.requests) == 3


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_normalized_point_upper_bound_is_rejected_before_execution(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [_tool_call({"action": "left_click", "coordinate": [1000, 500]})] * 3
    observe, _frames = _frame_factory(tmp_path)
    actions = []

    result, _frame = run_navigation(
        _model(server), "测试门店", 1, 4,
        observe=observe, execute=lambda *args: actions.append(args), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert result.get("source") == "runtime"
    assert actions == []
    assert len(handler.requests) == 3


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
@pytest.mark.parametrize("extra,expected", [("", 1), (',"extra":true', 0)])
def test_gui_plus_missing_click_bracket_requires_exact_known_shape(model_server, tmp_path, extra, expected):
    server, handler = model_server
    handler.responses = [
        '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":375,450]'
        + extra + '}}</tool_call>',
        _terminal("needs_review"),
    ]
    observe, _frames = _frame_factory(tmp_path)
    actions = []

    result, _frame = run_navigation(
        _model(server), "测试门店", 1, 4, observe=observe,
        execute=lambda command, _frame_id: actions.append(command), check_stop=lambda: None,
    )

    assert result["outcome"] == "needs_review"
    assert len(actions) == expected
    if expected:
        assert actions[0] == {"action": "click", "x": 120, "y": 90}


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_missing_success_menu_box_fails_closed(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [_terminal("success")] * 3
    observe, _frames = _frame_factory(tmp_path)

    try:
        result, _frame = run_navigation(
            _model(server), "测试门店", 2, 4,
            observe=observe, execute=lambda *args: pytest.fail(f"invalid terminal unexpectedly executed {args}"),
            check_stop=lambda: None,
        )
    except TrialProtocolError:
        return
    assert result["outcome"] == "needs_review"
    assert result.get("source") == "runtime"
    assert "菜单区域" in result["reason"] or result.get("diagnostic", {}).get("code")
    assert len(handler.requests) == 3


def test_stale_frame_action_is_rejected_before_transport_execution(tmp_path):
    from menu_capture.ui_tars_trial import _exchange

    read_fd, write_fd = os.pipe()
    png = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(png, format="PNG")
    frame_path = tmp_path / "frame.png"
    frame_path.write_bytes(png.getvalue())
    events = [
        {"type": "request", "id": "1", "method": "screenshot", "params": {}},
        {"type": "request", "id": "2", "method": "click", "params": {"frame_id": "stale", "x": 5, "y": 5}},
    ]
    os.write(write_fd, b"".join(json.dumps(item).encode() + b"\n" for item in events))

    class Child:
        stdin = io.BytesIO()
        stdout = os.fdopen(read_fd, "rb", buffering=0)

    actions = []
    try:
        with pytest.raises(TrialProtocolError, match="同轮新鲜截图"):
            _exchange(
                Child(), {},
                observe=lambda _seq: {"path": str(frame_path), "width": 20, "height": 10,
                                      "window_width": 20, "window_height": 10},
                execute=lambda *args: actions.append(args), check_stop=lambda: None,
            )
        assert actions == []
    finally:
        os.close(write_fd)


def test_windows_pipe_transport_reads_jsonl_and_reports_eof():
    from menu_capture.ui_tars_trial import _exchange

    read_fd, write_fd = os.pipe()
    events = [{"type": "result", "outcome": "needs_review", "reason": "窗口需要人工确认"}]
    os.write(write_fd, b"".join(json.dumps(item).encode() + b"\n" for item in events))
    os.close(write_fd)

    class Child:
        stdin = io.BytesIO()
        stdout = os.fdopen(read_fd, "rb", buffering=0)

    child = Child()
    result, frame = _exchange(
        child, {}, observe=lambda *_: pytest.fail("无需观察"),
        execute=lambda *_: pytest.fail("无需操作"), check_stop=lambda: None,
        _windows_pipe=True,
    )
    assert result == {"outcome": "needs_review", "reason": "窗口需要人工确认"}
    assert frame is None
    child.stdout.close()

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    child.stdout = os.fdopen(read_fd, "rb", buffering=0)
    with pytest.raises(TrialProtocolError, match="返回有效终态前退出"):
        _exchange(
            child, {}, observe=lambda *_: pytest.fail("无需观察"),
            execute=lambda *_: pytest.fail("无需操作"), check_stop=lambda: None,
            _windows_pipe=True,
        )
    child.stdout.close()


def test_windows_pipe_transport_keeps_existing_message_size_limit():
    from menu_capture.ui_tars_trial import _exchange

    # Windows 匿名管道容量小于这条超限消息。必须边读边写，否则 os.write 会在读取线程启动前永久阻塞。
    read_fd, write_fd = os.pipe()
    payload = b"x" * 32_769

    def write_payload():
        try:
            view = memoryview(payload)
            while view:
                written = os.write(write_fd, view)
                if written <= 0:
                    break
                view = view[written:]
        except OSError:
            return
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass

    writer = threading.Thread(target=write_payload, daemon=True)
    writer.start()

    class Child:
        stdin = io.BytesIO()
        stdout = os.fdopen(read_fd, "rb", buffering=0)

    child = Child()
    try:
        with pytest.raises(TrialProtocolError, match="超过大小上限"):
            _exchange(
                child, {}, observe=lambda *_: pytest.fail("无需观察"),
                execute=lambda *_: pytest.fail("无需操作"), check_stop=lambda: None,
                _windows_pipe=True,
            )
    finally:
        child.stdout.close()
        writer.join(timeout=5)
        assert not writer.is_alive()


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_stop_check_interrupts_live_sdk_loop(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [_tool_call({"action": "left_click", "coordinate": [375, 450]})]
    handler.delay_seconds = 0.3
    observe, _frames = _frame_factory(tmp_path)
    started = time.monotonic()
    actions = []

    class StopRequested(Exception):
        pass

    def check_stop():
        if handler.requests:
            raise StopRequested
        if time.monotonic() - started > 5:
            raise AssertionError("SDK did not reach the local model fixture")

    with pytest.raises(StopRequested):
        run_navigation(
            _model(server), "测试门店", 2, 10,
            observe=observe, execute=lambda *args: actions.append(args), check_stop=check_stop,
        )
    assert handler.requests
    assert actions == []


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
@pytest.mark.parametrize("resize_fails", [False, True], ids=["resized", "resize-failed"])
def test_runner_resizes_before_fresh_agent_frame_and_stages_capture(
    model_server, tmp_path, monkeypatch, resize_fails,
):
    server, handler = model_server
    handler.responses = [
        _tool_call({"action": "left_click", "coordinate": [375, 450]}),
        _terminal("success", menu_box=[100, 100, 900, 900]),
    ]
    input_dir, output_dir = tmp_path / "inbox", tmp_path / "official-output"
    input_dir.mkdir()
    batch_id, snapshot_id = "1" * 32, "2" * 64
    trial_input = tmp_path / "data/materials/published" / snapshot_id
    source = trial_input / "Trial Shop" / "qr.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (320, 200), "white").save(source)
    (trial_input / ".materials-snapshot.json").write_text(json.dumps({"snapshot_id": snapshot_id}))

    configs = ConfigStore(tmp_path / "settings")
    configs.save({
        "input_dir": str(input_dir), "output_dir": str(output_dir), "people_count": 2,
        "model": {"provider": "openai_compatible", "base_url": "https://legacy.example/v1",
                  "model": "legacy", "api_key": "legacy-test-key"},
    })
    store = TaskStore(tmp_path / "data/tasks.sqlite3")
    materials = MaterialService(store.path.parent, configs, store)
    with store.connect() as db:
        db.execute("""INSERT INTO material_batches(id,source_kind,source_root,status,created_at,updated_at)
                      VALUES (?, 'folder', ?, 'analyzed', '', '')""", (batch_id, str(input_dir)))
        version_id = materials.versions.remember(db, batch_id, "试验批次", "v1.0.0", snapshot_id, "", 1, activate=False)
    store.scan(trial_input)
    task = store.tasks()[0]
    profile_path = tmp_path / "uipath.profile.json"
    profile_path.write_text(json.dumps({
        "mode": "attached", "selected_task_ids": [], "max_tasks": 10,
    }), encoding="utf-8")

    class MockRobot:
        def __init__(self, _ipc_dir, _output_root):
            self.commands = []
            self.resized = False
            self._window_selector = None
            self._window_pixels = None

        def run(self, command, timeout=90, *, cancel_event=None):
            self.commands.append(dict(command))
            command_id = command.get("id", "mock")
            if command["action"] == "resize":
                assert command["width"] == 240
                assert command["height"] == 800
                self.resized = True
                self._window_selector = None
                self._window_pixels = None
                if resize_fails:
                    raise RobotCommandError("mock resize failure")
                return {"id": command_id, "ok": True, "error": ""}
            if command["action"] == "discover":
                return {"id": command_id, "ok": True, "windows": [{
                    "title": "Trial Shop", "app": "WeChat", "role": "AXWindow",
                    "position": {"x": 10, "y": 20, "width": 240, "height": 480},
                }]}
            if command["action"] in {"screenshot", "scroll_capture"}:
                path = Path(command["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                # 黑白交替保证相邻页差异超过“到底”阈值，最后一页重复模拟滚动到底。
                self.shots = getattr(self, "shots", 0) + 1
                if command["action"] == "screenshot":
                    color = (12, 34, 56) if self.resized else (255, 255, 255)
                else:
                    self.scroll_shots = getattr(self, "scroll_shots", 0) + 1
                    # 首页用黑底，避免与 Windows 未重排时的白色首帧相同而被误判“已到底”。
                    color = (0, 0, 0) if self.scroll_shots == 1 else (255, 255, 255)
                Image.new("RGB", (320, 200), color).save(path)
                self._window_selector = command.get("selector")
                self._window_pixels = (320, 200)
                return {"id": command_id, "ok": True, "error": "", "path": str(path),
                        "width": 320, "height": 200, "window_width": 160, "window_height": 100}
            return {"id": command_id, "ok": True, "error": ""}

        def request_stop(self):
            pass

    monkeypatch.setenv("GUI_PLUS_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("GUI_PLUS_API_KEY", "local-trial-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    from types import SimpleNamespace
    import sys
    monkeypatch.setitem(sys.modules, "screeninfo", SimpleNamespace(get_monitors=lambda: [
        SimpleNamespace(is_primary=True, x=0, y=0, width=1200, height=900),
    ]))
    runner = UiPathRunner(configs, store, profile_path, transport_factory=MockRobot)
    original_task = store.get(task["id"])

    started = runner.start(ui_tars_trial=True, trial_task_id=task["id"])
    deadline = time.monotonic() + 12
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.02)

    assert not runner.running, "单店试验应在测试时限内收尾"
    report_path = Path(started["result_file"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    commands = runner.transport.commands
    windows_mode = sys.platform == "win32"
    if resize_fails and windows_mode:
        pytest.skip("Windows 使用当前捕获的窗口尺寸，不做窗口重排，resize 故障注入不适用")
    resize_index = next((i for i, command in enumerate(commands) if command["action"] == "resize"), None)
    if windows_mode:
        assert resize_index is None
    else:
        assert resize_index is not None
    if resize_fails:
        assert report["navigation"] == {
            "outcome": "needs_review", "reason": "mock resize failure", "source": "runtime",
        }
        assert report["runtime_status"] == "failed"
        assert report["runtime_code"] == "runtime"
        assert report["runtime_error"] == "mock resize failure"
        assert report["agent_outcome"] == ""
        assert report["capture_status"] == "not_started"
        assert handler.requests == []
        assert not any(command["action"] in {"screenshot", "click"} for command in commands)
        return
    first_screenshot_index = next(i for i, command in enumerate(commands) if command["action"] == "screenshot")
    if not windows_mode:
        assert resize_index < first_screenshot_index
    # Windows 不重排窗口，截图保持窗口原始白底；Mac 重排后应换成本轮底色。
    expected_pixel = (255, 255, 255) if windows_mode else (12, 34, 56)
    assert report["material"]["material_version_id"] == version_id
    assert report["material"]["material_batch_id"] == batch_id
    assert report["material"]["material_snapshot"] == snapshot_id
    assert (report_path.parent / report["initial_frame"]).is_file()
    with Image.open(report_path.parent / report["initial_frame"]) as initial_frame:
        assert initial_frame.getpixel((0, 0)) == expected_pixel
    request_frame = _image_data_bytes(handler.requests[0]["body"]["messages"])
    assert request_frame is not None
    with Image.open(io.BytesIO(request_frame)) as image:
        assert image.getpixel((0, 0)) == expected_pixel
    assert report["timing_ms"]["total"] >= report["timing_ms"]["navigation"]
    assert configs.load().input_dir == str(input_dir)
    assert report["navigation"]["outcome"] == "success"
    assert report["navigation"]["reason"] == "已核对页面"
    assert "evidence" not in report["navigation"]
    assert report["navigation"]["frame"] == str(Path("evidence") / "ui-tars-002.png")
    with Image.open(report_path.parent / report["navigation"]["frame"]) as terminal_frame:
        region = report["navigation"]["menu_region"]
        assert 0 <= region["x"] and region["x"] + region["width"] <= terminal_frame.width
        assert 0 <= region["y"] and region["y"] + region["height"] <= terminal_frame.height
    assert report["capture"]["status"] == "staged"
    # 至少 3 页才算“可滚动菜单”；1–2 页会被转人工复核。
    assert len(report["capture"]["pages"]) >= 3
    assert report["agent_outcome"] == "success"
    assert report["agent_reason"] == "已核对页面"
    assert report["runtime_status"] == "ok"
    assert report["runtime_code"] == ""
    assert report["capture_status"] == "staged"
    assert report["metrics"]["navigation_actions"] == 1
    assert (report_path.parent / report["capture"]["pages"][0]).is_file()
    assert report_path.parent.parent.parent.name == ".uipath-runs"
    assert not (output_dir / "Trial Shop").exists()
    assert store.get(task["id"]) == original_task
    assert store.latest_run() is None
    click_indexes = [i for i, command in enumerate(commands) if command["action"] == "click"]
    assert len(click_indexes) == 1
    assert any(i > click_indexes[0] and command["action"] == "screenshot"
               for i, command in enumerate(commands))
    assert any(command["action"] == "scroll_capture" for command in commands)
    assert len(handler.requests) == 2
    assert all(request["body"]["model"] == GUI_PLUS_MODEL for request in handler.requests)
    assert "2 人" in json.dumps(handler.requests[0]["body"]["messages"], ensure_ascii=False)


def _all_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_all_text(nested) for nested in value.values())
    if isinstance(value, list):
        return "\n".join(_all_text(nested) for nested in value)
    return ""


def _image_refs(value: object) -> list[dict]:
    if isinstance(value, dict):
        if value.get("type") == "image_ref":
            return [value]
        return [item for nested in value.values() for item in _image_refs(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _image_refs(nested)]
    return []


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_model_call_trace_keeps_raw_text_but_references_images(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [
        _tool_call({"action": "left_click", "coordinate": [375, 450]}),
        _terminal("success", menu_box=[100, 100, 900, 900]),
    ]
    observe, _frames = _frame_factory(tmp_path)
    trace_path = tmp_path / "agent-calls.jsonl"

    result, _terminal_frame = run_navigation(
        _model(server), "测试门店", 2, 4,
        observe=observe, execute=lambda command, frame_id: None, check_stop=lambda: None,
        trace_path=trace_path,
        trace_meta={"run_id": "run-1", "task_id": "task-1", "store_name": "测试门店"},
    )

    assert result["outcome"] == "success"
    text = trace_path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    kinds = {row["type"] for row in rows}
    assert {"session", "request", "response", "action", "tool", "screenshot", "result"} <= kinds
    session = next(row for row in rows if row["type"] == "session")
    assert session["run_id"] == "run-1" and session["task_id"] == "task-1"
    request = next(row for row in rows if row["type"] == "request")
    assert request["request"]["stream_options"]["include_usage"] is True
    refs = _image_refs(request["request"])
    assert refs and all(item["sha256"] and item["bytes"] > 0 for item in refs)
    assert "data:image" not in text
    assert any(row["type"] == "response" and "usage" in row for row in rows)
    assert any(row["type"] == "action" and row["ok"] and "raw" in row for row in rows)


def test_agent_usage_is_written_to_existing_ledger(tmp_path):
    from menu_capture.model_usage import UsageLedger
    from menu_capture.uipath_runner import UiPathRunner

    store = TaskStore(tmp_path / "tasks.sqlite3")
    runner = object.__new__(UiPathRunner)
    runner.usage_ledger = UsageLedger(store)
    runner.run_id = "run-agent"
    trace_path = tmp_path / "agent-calls.jsonl"
    trace_path.write_text("\n".join([
        json.dumps({"type": "request", "at": "2026-11-09T02:00:00.000Z", "call_id": 1}),
        json.dumps({"type": "response", "at": "2026-11-09T02:00:03.000Z", "duration_ms": 3000,
                    "call_id": 1, "request_id": "chatcmpl-1", "returned_model": GUI_PLUS_MODEL,
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 34, "total_tokens": 1234}}),
        json.dumps({"type": "request", "at": "2026-11-09T02:00:04.000Z", "call_id": 2}),
        json.dumps({"type": "response", "at": "2026-11-09T02:00:06.000Z", "duration_ms": 2100,
                    "call_id": 2, "request_id": "chatcmpl-2", "returned_model": GUI_PLUS_MODEL, "usage": None}),
    ]) + "\n", encoding="utf-8")

    recorded = runner._record_agent_usage(
        trace_path, {"id": "task-1", "source_name": "测试门店"},
        TrialModel("/usr/bin/node", "http://127.0.0.1:9/v1", "secret-key"),
    )

    assert recorded == {"model_calls": 2, "with_usage": 1, "failed": 0, "skipped": 0}
    with store.connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT source,run_id,task_id,source_name,status,usage_state,input_tokens,output_tokens,started_at "
            "FROM model_calls ORDER BY started_at")]
    assert [row["usage_state"] for row in rows] == ["complete", "missing"]
    assert all(row["source"] == "agent" and row["run_id"] == "run-agent"
               and row["task_id"] == "task-1" and row["source_name"] == "测试门店" for row in rows)
    assert rows[0]["input_tokens"] == 1200 and rows[0]["output_tokens"] == 34
    # 开始时间取请求发出时刻，而不是响应完成时刻。
    assert rows[0]["started_at"] == "2026-11-09T02:00:00.000+00:00"


def test_agent_usage_records_failed_calls_and_skips_reimport(tmp_path):
    from menu_capture.model_usage import UsageLedger
    from menu_capture.uipath_runner import UiPathRunner

    store = TaskStore(tmp_path / "tasks.sqlite3")
    runner = object.__new__(UiPathRunner)
    runner.usage_ledger = UsageLedger(store)
    runner.run_id = "run-agent"
    trace_path = tmp_path / "agent-calls.jsonl"
    trace_path.write_text("\n".join([
        json.dumps({"type": "request", "at": "2026-11-09T02:00:00.000Z", "call_id": 1}),
        json.dumps({"type": "response", "at": "2026-11-09T02:00:08.000Z", "duration_ms": 8000,
                    "call_id": 1, "request_id": "chatcmpl-1", "returned_model": GUI_PLUS_MODEL,
                    "usage": {"prompt_tokens": 900, "completion_tokens": 20, "total_tokens": 920}}),
        json.dumps({"type": "request", "at": "2026-11-09T02:01:00.000Z", "call_id": 2}),
        json.dumps({"type": "error", "at": "2026-11-09T02:01:02.500Z", "call_id": 2, "code": "runtime"}),
        json.dumps({"type": "request", "at": "2026-11-09T02:02:00.000Z", "call_id": 3}),
    ]) + "\n", encoding="utf-8")
    model = TrialModel("/usr/bin/node", "http://127.0.0.1:9/v1", "secret-key")

    first = runner._record_agent_usage(trace_path, {"id": "task-1"}, model)
    second = runner._record_agent_usage(trace_path, {"id": "task-1"}, model)

    assert first == {"model_calls": 3, "with_usage": 1, "failed": 2, "skipped": 0}
    assert second == {"model_calls": 0, "with_usage": 0, "failed": 0, "skipped": 3}
    with store.connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT status,error_type,elapsed_ms,usage_state,input_tokens FROM model_calls ORDER BY started_at")]
    assert [row["status"] for row in rows] == ["success", "error", "error"]
    assert rows[0]["elapsed_ms"] == 8000 and rows[0]["input_tokens"] == 900
    assert rows[1]["error_type"] == "runtime" and rows[1]["elapsed_ms"] == 2500
    # 只有 request 没有收尾记录，说明调用在响应前中断。
    assert rows[2]["error_type"] == "interrupted" and rows[2]["usage_state"] == "missing"


def test_host_trace_keeps_failed_tool_attempt():
    runner = object.__new__(UiPathRunner)
    runner._trace_click = {}

    def fail(_command):
        raise RobotCommandError("foreground=false")

    runner._send = fail
    records: list[dict] = []
    with pytest.raises(RobotCommandError):
        runner._execute_trial_action({"selector": "window:one", "title": ""},
                                     {"action": "click", "x": 10, "y": 20}, "1",
                                     lambda kind, **fields: records.append({"kind": kind, **fields}))

    assert len(records) == 1
    assert records[0]["kind"] == "tool" and records[0]["ok"] is False
    assert "RobotCommandError" in records[0]["error"]
    assert records[0]["params"] == {"action": "click", "x": 10, "y": 20}


def test_trial_window_calibration_checks_both_axes(tmp_path):
    from menu_capture.window_contract import window_selector

    runner = object.__new__(UiPathRunner)
    runner._move_window_on_screen = lambda _selector: None
    window = {"app": "Weixin.exe", "title": "点餐", "cls": "QtWindow",
              "position": {"x": 0, "y": 0, "width": 240, "height": 400}}
    selector = window_selector(window, window["title"])

    def frame(width, height, window_width, window_height):
        return {"action": "screenshot", "selector": selector,
                "path": str(tmp_path / "window-prepare.png"),
                "width": width, "height": height,
                "window_width": window_width, "window_height": window_height}

    def send(command, shot):
        if command.get("action") == "discover":
            return {"windows": [window]}
        return shot

    runner._send = lambda command: send(command, frame(320, 200, 240, 150))
    records: list[dict] = []
    runner._prepare_trial_window(selector, tmp_path,
                                lambda kind, **fields: records.append({"kind": kind, **fields}))
    assert records[0]["scale_x"] == pytest.approx(320 / 240, abs=1e-4)
    assert records[0]["scale_y"] == pytest.approx(200 / 150, abs=1e-4)

    # 纵向比例与横向不一致，说明截图和窗口尺寸不同源，必须降级而不是带错坐标启动。
    runner._send = lambda command: send(command, frame(320, 200, 240, 600))
    with pytest.raises(NeedsReview, match="宽高比例"):
        runner._prepare_trial_window(selector, tmp_path, lambda *args, **kwargs: None)


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_stream_usage_tail_chunk_reaches_trace_with_exact_tokens(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [
        {"content": _tool_call({"action": "left_click", "coordinate": [375, 450]}),
         "usage": {"prompt_tokens": 1200, "completion_tokens": 20, "total_tokens": 1220}},
        {"content": _terminal("success", menu_box=[100, 100, 900, 900]),
         "usage": {"prompt_tokens": 1531, "completion_tokens": 47, "total_tokens": 1578}},
    ]
    observe, _frames = _frame_factory(tmp_path)
    trace_path = tmp_path / "agent-calls.jsonl"

    result, _terminal_frame = run_navigation(
        _model(server), "测试门店", 2, 4,
        observe=observe, execute=lambda command, frame_id: None, check_stop=lambda: None,
        trace_path=trace_path,
    )

    assert result["outcome"] == "success"
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    responses = [row for row in rows if row["type"] == "response"]
    assert [row["usage"]["total_tokens"] for row in responses] == [1220, 1578]
    assert [row["usage"]["prompt_tokens"] for row in responses] == [1200, 1531]


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_window_tools_feed_titles_back_and_reobserve_after_switch(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [
        _tool_call({"action": "list_windows"}),
        _tool_call({"action": "switch_window", "title": "门店B"}),
        _terminal("success", menu_box=[100, 100, 900, 900]),
    ]
    base_observe, _frames = _frame_factory(tmp_path)
    active = {"selector": "门店A"}
    observed, commands = [], []

    def observe(sequence):
        observed.append(active["selector"])
        return base_observe(sequence)

    def execute(command, _frame_id):
        commands.append(dict(command))
        if command["action"] == "windows":
            return {"windows": [{"title": "门店A"}, {"title": "门店B"}]}
        if command["action"] == "switch_window":
            active["selector"] = command["title"]
            return {"switched": True, "title": command["title"]}
        return {}

    result, terminal_frame = run_navigation(
        _model(server), "门店B", 2, 4,
        observe=observe, execute=execute, check_stop=lambda: None,
    )

    assert result["outcome"] == "success"
    assert commands == [{"action": "windows"}, {"action": "switch_window", "title": "门店B"}]
    # 首帧、列窗口后的同窗口截图、切换后的新窗口截图。
    assert observed == ["门店A", "门店A", "门店B"]
    assert terminal_frame["width"] == 320
    assert "可切换的小程序窗口" in _all_text(handler.requests[1]["body"]["messages"])
    assert "门店B" in _all_text(handler.requests[1]["body"]["messages"])
    assert "已切换到窗口" in _all_text(handler.requests[2]["body"]["messages"])


def test_window_and_handoff_contract_rejects_mismatched_evidence(tmp_path):
    from types import SimpleNamespace

    from menu_capture.ui_tars_trial import _exchange

    read_fd, write_fd = os.pipe()
    png = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(png, format="PNG")
    frame_path = tmp_path / "frame.png"
    frame_path.write_bytes(png.getvalue())
    events = [
        {"type": "request", "id": "1", "method": "screenshot", "params": {}},
        {"type": "request", "id": "2", "method": "windows", "params": {"frame_id": "1"}},
        {"type": "request", "id": "3", "method": "switch_window", "params": {"frame_id": "1", "title": "门店B"}},
        {"type": "result", "outcome": "needs_review", "reason": "核对完成"},
    ]
    os.write(write_fd, b"".join(json.dumps(item).encode() + b"\n" for item in events))

    class Child:
        stdin = io.BytesIO()
        stdout = os.fdopen(read_fd, "rb", buffering=0)

    commands = []

    def execute(command, _frame_id):
        commands.append(command)
        if command["action"] == "windows":
            return {"windows": [{"title": "门店A"}, {"title": "门店B"}]}
        if command["action"] == "switch_window":
            return {"switched": True, "title": command["title"]}
        return {}

    try:
        result, _frame = _exchange(
            Child(), {},
            observe=lambda _seq: {"path": str(frame_path), "width": 20, "height": 10,
                                  "window_width": 20, "window_height": 10},
            execute=execute, check_stop=lambda: None,
        )
        assert result["outcome"] == "needs_review"
        assert commands == [{"action": "windows"}, {"action": "switch_window", "title": "门店B"}]
        replies = [json.loads(line) for line in Child.stdin.getvalue().decode().splitlines()]
        assert replies[2]["result"] == {"windows": [{"title": "门店A"}, {"title": "门店B"}]}
        assert replies[3]["result"] == {"switched": True, "title": "门店B"}
    finally:
        os.close(write_fd)

    from menu_capture.uipath_runner import UiPathRunner

    runner = object.__new__(UiPathRunner)
    runner.transport = SimpleNamespace(_window_selector="门店B", _window_pixels=(20, 10))
    frame = {"path": str(frame_path), "width": 20, "height": 10}
    runner._validate_trial_handoff({"menu_region": {"x": 2, "y": 1, "width": 10, "height": 5}}, frame, "门店B")
    with pytest.raises(TrialProtocolError, match="当前小程序窗口"):
        runner._validate_trial_handoff({"menu_region": {"x": 2, "y": 1, "width": 10, "height": 5}}, frame, "门店A")
    with pytest.raises(TrialProtocolError, match="超出当前截图"):
        runner._validate_trial_handoff({"menu_region": {"x": 15, "y": 1, "width": 10, "height": 5}}, frame, "门店B")


def test_switch_window_updates_active_selector_by_exact_title():
    from menu_capture.uipath_runner import UiPathRunner

    runner = object.__new__(UiPathRunner)
    windows = [{
        "title": title, "app": "WeChat", "role": "AXWindow",
        "position": {"x": 0, "y": 0, "width": 240, "height": 480},
    } for title in ("门店A", "门店B")]
    runner._send = lambda _command: {"windows": windows}
    moved = []
    runner._move_window_on_screen = lambda selector: moved.append(selector)
    active = {"selector": "门店A", "title": ""}

    assert runner._switch_trial_window(active, "门店C")["switched"] is False
    assert active["selector"] == "门店A"
    assert runner._switch_trial_window(active, "门店B")["switched"] is True
    assert active["title"] == "门店B"
    assert moved == [active["selector"]]


@pytest.mark.skipif(os.environ.get("GUI_PLUS_LIVE_PROBE") != "1", reason="仅在显式 GUI_PLUS_LIVE_PROBE=1 时调用 GUI-Plus")
@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
@pytest.mark.parametrize("width,height,square,description", [
    (880, 1100, (705, 995, 90), "右下方"),
    (1200, 800, (120, 600, 100), "左下方"),
])
def test_live_gui_plus_pixel_contract_before_mouse(tmp_path, width, height, square, description):
    if not (os.environ.get("GUI_PLUS_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")):
        pytest.skip("live probe 未提供 GUI_PLUS_API_KEY 或 DASHSCOPE_API_KEY")

    model = trial_model_from_env()
    observe, frames = _frame_factory(tmp_path, width=width, height=height, square=square)
    actions = []

    class ClickObserved(Exception):
        pass

    def stop_after_click(command, _frame_id):
        actions.append(command)
        raise ClickObserved

    with pytest.raises(ClickObserved):
        _run_agent(
            model,
            f"这是合成坐标校准图。请把下一步动作定为点击{description}紫色方块的中心。",
            2,
            observe=observe,
            execute=stop_after_click,
            check_stop=lambda: None,
        )

    assert len(frames) == 1
    assert len(actions) == 1 and actions[0]["action"] == "click"
    left, top, side = square
    assert left <= actions[0]["x"] <= left + side
    assert top <= actions[0]["y"] <= top + side


def test_reference_image_is_downscaled_and_missing_file_is_ignored(tmp_path):
    from menu_capture.ui_tars_trial import reference_image

    source = tmp_path / "qr-source.jpg"
    Image.new("RGB", (1800, 1400), "white").save(source)
    reference = reference_image(source)
    assert reference["mime"] == "image/jpeg"
    assert reference["width"] * reference["height"] <= 640_000
    assert reference_image(tmp_path / "missing.jpg") is None


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_source_image_is_sent_as_reference_context(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [_terminal("needs_review", reason="已看到本轮二维码原图")]
    observe, _frames = _frame_factory(tmp_path)
    source = tmp_path / "qr-source.jpg"
    Image.new("RGB", (1200, 900), "white").save(source)

    result, _terminal_frame = run_navigation(
        _model(server), "测试门店", 2, 2,
        observe=observe, execute=lambda command, frame_id: None, check_stop=lambda: None,
        source_image=source,
    )

    assert result["outcome"] == "needs_review"
    content = [item for request in handler.requests[:1]
               for message in request["body"]["messages"] if isinstance(message.get("content"), list)
               for item in message["content"]]
    assert len([item for item in content if item.get("type") == "image_url"]) == 2
    assert any("二维码原图" in item.get("text", "") for item in content)


@pytest.mark.skipif(not _runtime_available(), reason="需要本机安装 Node.js 与 @gui-agent/agent-sdk")
def test_qr_hint_is_injected_into_agent_instruction(model_server, tmp_path):
    server, handler = model_server
    handler.responses = [_terminal("unqualified", reason="这是大众点评的团购界面，不是点餐码")]
    observe, _frames = _frame_factory(tmp_path)

    result, _terminal_frame = run_navigation(
        _model(server), "测试门店", 2, 2,
        observe=observe, execute=lambda command, frame_id: None, check_stop=lambda: None,
        qr_hint="\n二维码原图的初步判断（只作参考）：平台=大众点评，用途=买单/结账。",
    )

    assert result["outcome"] == "unqualified"
    body = json.dumps(handler.requests[0]["body"], ensure_ascii=False)
    assert "二维码原图的初步判断" in body
