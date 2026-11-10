from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from menu_capture import model as model_module
from menu_capture.environment import EnvironmentDiagnostics, _item
from menu_capture.server import create_app


def test_output_probe_uses_and_removes_a_temporary_file(tmp_path: Path) -> None:
    output = tmp_path / "capture-output"
    result = EnvironmentDiagnostics._directory_check("output", "输出目录", str(output), writable=True)

    assert result["status"] == "ready"
    assert result["summary"] == "目录可写；临时探测文件已删除"
    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_wechat_selector_uses_chat_window_and_skips_portrait_miniprogram() -> None:
    windows = [
        {"app": "WeChat", "title": "门店点餐", "role": "AXWindow", "cls": "Qt51514QWindowIcon",
         "position": {"width": 420, "height": 900}},
        {"app": "WeChat", "title": "文件传输助手", "role": "AXWindow", "cls": "WeChatMainWndForPC",
         "position": {"width": 1100, "height": 760}},
    ]

    selector = EnvironmentDiagnostics._find_wechat_selector(windows)

    assert "title=\"文件传输助手\"" in selector
    identity, value = (("cls", "WeChatMainWndForPC") if sys.platform == "win32"
                       else ("role", "AXWindow"))
    assert f'{identity}="{value}"' in selector


def test_wechat_login_is_unknown_when_ocr_only_finds_app_name() -> None:
    login, assistant = EnvironmentDiagnostics._classify_wechat_screen(
        [{"text": "微信"}], width=1100, height=760
    )

    assert login["status"] == "unknown"
    assert assistant["status"] == "unknown"


def test_screenshot_content_quality_rejects_black_frame(tmp_path: Path) -> None:
    black_frame = tmp_path / "wechat-black.png"
    Image.new("RGB", (1800, 1988), (0, 0, 0)).save(black_frame)

    result = EnvironmentDiagnostics._screenshot_content_quality(black_frame)

    assert result["near_black"] is True
    assert result["dark_fraction"] == 1.0


def test_screenshot_content_quality_accepts_visible_window_pixels(tmp_path: Path) -> None:
    visible_frame = tmp_path / "wechat-visible.png"
    image = Image.new("RGB", (864, 958), (242, 243, 244))
    image.paste((20, 170, 120), (20, 20, 200, 80))
    image.save(visible_frame)

    result = EnvironmentDiagnostics._screenshot_content_quality(visible_frame)

    assert result["near_black"] is False


def test_model_error_detail_does_not_expose_raw_exception(monkeypatch, tmp_path: Path) -> None:
    app = create_app(tmp_path)

    def raise_with_url(*_args, **_kwargs):
        raise RuntimeError("request failed https://model.invalid/path?api_key=DO_NOT_RENDER")

    monkeypatch.setattr(model_module, "VisionModel", raise_with_url)
    result = app.state.environment._test_model()["model_service"]

    assert result["status"] == "error"
    assert "DO_NOT_RENDER" not in result["detail"]
    assert "RuntimeError" in result["detail"]


def test_environment_check_endpoint_does_not_run_expensive_tests(tmp_path: Path, monkeypatch) -> None:
    app = create_app(tmp_path)

    def unexpected_test():
        raise AssertionError("GET/check must not call model or desktop tests")

    monkeypatch.setattr(app.state.environment, "_test_model", unexpected_test)
    monkeypatch.setattr(app.state.environment, "_test_uipath_and_wechat", unexpected_test)
    client = TestClient(app, base_url="http://127.0.0.1")

    response = client.post("/api/environment/check", json={}, headers={"X-Capture-Token": app.state.token})

    assert response.status_code == 200
    data = response.json()
    assert data["preflight"]["ready"] is False
    assert all(isinstance(item, str) for item in data["preflight"]["blockers"])
    assert {check["status"] for check in data["checks"]} <= {"checking", "ready", "warning", "error", "unknown"}


def test_saving_same_robot_profile_preserves_recent_desktop_probe(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-Capture-Token": app.state.token}
    profile = client.get("/api/robot/profile").json()
    app.state.environment._tests["uipath_desktop"] = {
        "at": time.monotonic(),
        "checked_at": "2026-11-03T10:00:00+08:00",
        "uipath_accessibility": {"status": "ready", "summary": "实测通过"},
    }

    response = client.post("/api/robot/profile", json=profile, headers=headers)

    assert response.status_code == 200
    assert "uipath_desktop" in app.state.environment._tests


def test_preflight_blockers_are_user_readable_strings(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    environment = app.state.environment
    checks = {
        "input_directory": _item("input_directory", "素材目录读取", "error", "目录不可读"),
        "output_directory": _item("output_directory", "输出目录写入", "ready", "目录可写"),
        "model_service": _item("model_service", "视觉模型多模态请求", "ready", "连接正常"),
        "uipath_accessibility": _item("uipath_accessibility", "UiPath 辅助功能", "ready", "枚举成功"),
        "uipath_screenshot": _item("uipath_screenshot", "UiPath 屏幕录制", "ready", "截图成功"),
        "wechat_login": _item("wechat_login", "微信登录", "ready", "已登录"),
        "file_transfer_assistant": _item("file_transfer_assistant", "文件传输助手", "ready", "已打开"),
    }
    profile = _item("capture_profile", "采集配置", "ready", "静态检查通过")

    blockers = environment._blockers(checks, profile, "automatic")

    assert blockers == ["素材目录读取：目录不可读"]
