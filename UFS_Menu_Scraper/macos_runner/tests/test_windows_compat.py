from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from menu_capture import robot_profile, uipath_cli, uipath_runner, window_contract
from menu_capture.instance_lock import instance_lock


def test_instance_lock_excludes_another_service_and_keeps_file(tmp_path):
    path = tmp_path / "runner.lock"
    path.write_bytes(b"owner")
    code = (
        "import sys; from pathlib import Path; "
        "from menu_capture.instance_lock import InstanceLockedError, instance_lock\n"
        "try:\n"
        "    with instance_lock(Path(sys.argv[1])):\n"
        "        pass\n"
        "except InstanceLockedError:\n"
        "    sys.exit(23)\n"
    )
    command = [sys.executable, "-c", code, str(path)]
    with instance_lock(path):
        assert subprocess.run(command, timeout=5).returncode == 23
    assert subprocess.run(command, timeout=5).returncode == 0
    assert path.read_bytes() == b"owner"


def test_windows_uipath_npm_entry_avoids_batch_shell(monkeypatch, tmp_path):
    shim = tmp_path / "uip.cmd"
    package = tmp_path / "node_modules/@uipath/cli"
    entry = package / "dist/index.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    manifest = package / "package.json"
    manifest.write_text(json.dumps({"bin": {"uip": "dist/index.js"}}), encoding="utf-8")
    monkeypatch.setattr(uipath_cli, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(uipath_cli.shutil, "which", {"uip": str(shim), "node": "node.exe"}.get)

    assert uipath_cli.cli_entry() == ["node.exe", str(entry.resolve())]
    manifest.write_text(json.dumps({"bin": {"uip": "../../../other.js"}}), encoding="utf-8")
    assert uipath_cli.cli_entry() == []


def test_windows_profile_defaults_and_executable_validation(monkeypatch):
    monkeypatch.setattr(robot_profile, "sys", SimpleNamespace(platform="win32"))
    profile = robot_profile.RobotProfile(wechat_selector="<wnd app='weixin.exe' title='微信' />")
    assert "[d(ctrl)]" in profile.paste_shortcut
    assert "[k(f4)]" in profile.close_key
    with pytest.raises(ValidationError):
        robot_profile.RobotProfile(wechat_selector="<wnd app='FakeWeChat.exe' />")


def test_windows_window_observation_preserves_class_and_checks_process(monkeypatch):
    windows = SimpleNamespace(platform="win32")
    monkeypatch.setattr(window_contract, "sys", windows)
    monkeypatch.setattr(uipath_runner, "sys", windows)
    geometry = {"x": 0, "y": 0, "width": 340, "height": 700}
    observed = {"app": "Weixin.exe", "title": "点餐", "cls": "QtWindow", "role": "AXWindow", "position": geometry}
    selectors = uipath_runner.UiPathRunner._miniapp_candidates([
        observed, dict(observed, title="Weixin"), dict(observed, app="Other.exe")
    ])
    assert len(selectors) == 1
    selector = selectors[0]["selector"]
    assert 'cls="QtWindow"' in selector and "AXWindow" not in selector
    command = {"action": "screenshot", "selector": selector}
    reply = {"target_window": {"app": "Weixin", "title": "点餐", "foreground": True, "capture_scope": "window"}}
    window_contract.validate_target_window(command, reply)
    reply["target_window"]["app"] = "other.exe"
    with pytest.raises(ValueError, match="app"):
        window_contract.validate_target_window(command, reply)


def test_windows_handoff_rejects_ambiguous_window_before_screenshot(monkeypatch, tmp_path):
    windows = SimpleNamespace(platform="win32")
    monkeypatch.setattr(window_contract, "sys", windows)
    monkeypatch.setattr(uipath_runner, "sys", windows)
    window = {"app": "Weixin.exe", "title": "点餐", "cls": "QtWindow",
              "position": {"x": 0, "y": 0, "width": 320, "height": 640}}
    selector = window_contract.window_selector(window, window["title"])
    runner = object.__new__(uipath_runner.UiPathRunner)
    calls = []
    runner._send = lambda command: calls.append(command) or {"windows": [window, window.copy()]}

    with pytest.raises(uipath_runner.NeedsReview, match="唯一"):
        runner._prepare_trial_window(selector, tmp_path)
    assert calls == [{"action": "discover"}]


def test_windows_cleanup_protects_recorded_wechat_windows(monkeypatch, tmp_path):
    windows = SimpleNamespace(platform="win32")
    monkeypatch.setattr(window_contract, "sys", windows)
    monkeypatch.setattr(uipath_runner, "sys", windows)
    window = {"app": "Weixin.exe", "title": "点餐", "cls": "MiniProgram",
              "position": {"x": 0, "y": 0, "width": 320, "height": 640}}
    selector = window_contract.window_selector(window, window["title"])
    profile = {"miniapp_selector": selector, "wechat_selector": "<wnd app='weixin.exe' title='微信' />",
               "preview_selector": "<wnd app='weixin.exe' cls='MiniProgram' />", "close_key": "recorded-close"}
    runner = object.__new__(uipath_runner.UiPathRunner)
    runner._guard = lambda: None
    calls = []
    runner._send = lambda command: calls.append(command) or {"windows": [window]}
    runner._screenshot = lambda *_args: {}
    runner._read_assistant_context = lambda *_args: True

    with pytest.raises(uipath_runner.NeedsReview, match="无法安全区分"):
        runner._close_to_file_assistant(profile, tmp_path, 0.5)
    assert calls == [{"action": "discover"}]
    profile["preview_selector"] = "<wnd app='weixin.exe' cls='ImagePreview' />"
    runner._close_to_file_assistant(profile, tmp_path, 0.5)
    assert calls[2:] == [
        {"action": "key", "selector": selector, "key": "recorded-close"},
        {"action": "noop", "seconds": 0.5},
    ]
