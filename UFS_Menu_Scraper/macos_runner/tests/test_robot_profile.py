import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from menu_capture.robot_profile import ProfileStore, RobotProfile
from menu_capture.server import create_app


def test_profile_requires_complete_wechat_window_selector():
    with pytest.raises(ValueError, match="微信进程"):
        RobotProfile(miniapp_selector="<wnd app='Safari' title='*' />")
    with pytest.raises(ValueError, match="XML"):
        RobotProfile(miniapp_selector="只有店名")
    with pytest.raises(ValueError):
        RobotProfile(miniapp_selector="<!DOCTYPE secret SYSTEM 'file:///etc/passwd'><wnd/>")
    assert RobotProfile(miniapp_selector="<wnd app='WeChat' title='*'/>").miniapp_selector


def test_profile_coordinates_are_explicit_and_private(tmp_path: Path):
    assert RobotProfile().launch_mode == "manual"
    assert RobotProfile(launch_mode="cli").launch_mode == "cli"
    assert RobotProfile(mode="automatic", selected_task_ids=["task-a", "task-b"]).selected_task_ids == ["task-a", "task-b"]
    with pytest.raises(ValueError):
        RobotProfile(launch_mode="shell")
    store = ProfileStore(tmp_path / "profile.json")
    result = store.save(RobotProfile().model_dump())
    assert result["coordinates_verified"] is False
    if sys.platform != "win32":
        # Windows 没有 POSIX 权限位，0600 只在 POSIX 平台成立。
        assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        RobotProfile(wechat_input_x_ratio=float("inf"))
    with pytest.raises(ValueError):
        RobotProfile(max_tasks=11)


def test_profile_load_migrates_legacy_recognition_fields_once(tmp_path: Path):
    path = tmp_path / "profile.json"
    original = RobotProfile(mode="automatic", wechat_selector="<wnd app='WeChat' title='*'/>").model_dump()
    original.update({
        "preview_recognize_x": 120,
        "preview_recognize_y": 240,
        "recognize_selector": "<wnd app='WeChat' title='recognize'/>",
    })
    path.write_text(json.dumps(original), encoding="utf-8")

    result = ProfileStore(path).load()

    assert result["mode"] == "automatic"
    assert result["wechat_selector"] == "<wnd app='WeChat' title='*'/>"
    assert not {"preview_recognize_x", "preview_recognize_y", "recognize_selector"} & result.keys()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted == result
    assert not {"preview_recognize_x", "preview_recognize_y", "recognize_selector"} & persisted.keys()


def test_profile_save_rejects_legacy_recognition_fields_and_public_model_omits_them(tmp_path: Path):
    legacy_fields = {"preview_recognize_x", "preview_recognize_y", "recognize_selector"}
    assert not legacy_fields & RobotProfile().model_dump().keys()

    payload = RobotProfile().model_dump()
    payload["recognize_selector"] = ""
    with pytest.raises(ValueError):
        ProfileStore(tmp_path / "profile.json").save(payload)


def test_server_never_prepares_robot_without_calibrated_profile(tmp_path: Path):
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"X-Capture-Token": app.state.token}
    assert client.get("/api/status").json()["engine_ready"] is None  # 尚未手动诊断时保持未知，不能假定已测试失败。
    assert client.post("/api/robot/profile", json=RobotProfile().model_dump()).status_code == 403
    assert client.post("/api/robot/profile", json=RobotProfile().model_dump(), headers=headers).status_code == 200
    rejected = client.post("/api/start", json={}, headers=headers)
    assert rejected.status_code == 400
    assert not app.state.runner.running
    assert app.state.runner.run_directory is None
