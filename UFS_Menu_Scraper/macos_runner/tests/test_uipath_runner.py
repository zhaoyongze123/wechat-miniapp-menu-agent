from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from menu_capture.config import ConfigStore
from menu_capture.store import TaskStore
from menu_capture.model import Region
from menu_capture.scene_agent import SceneDecision
from menu_capture.uipath_runner import (
    FileRobotTransport,
    NeedsReview,
    RobotProtocolError,
    RobotTimeout,
    UiPathRunner,
)
import menu_capture.uipath_runner as uipath_runner_module

class FakeAgentModel:
    node = "/test/node"
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key = "test-key"

    def close(self):
        pass

@pytest.fixture(autouse=True)
def _configured_gui_agent_model(monkeypatch):
    monkeypatch.setattr(uipath_runner_module, "trial_model_from_env", lambda *_args: FakeAgentModel())

def _image(path: Path, color: str = "white", size=(240, 160)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)

def _profile(path: Path, task_id: str, **overrides) -> None:
    profile = {
        "mode": "attached",
        "wechat_selector": "recorded-wechat-selector",
        "miniapp_selector": "recorded-miniapp-selector",
        "preview_selector": "recorded-preview-selector",
        "selected_task_id": task_id,
        "wechat_input_x_ratio": 0.68,
        "wechat_input_y_ratio": 0.90,
        "paste_shortcut": "cmd+v",
        "send_key": "return",
        "close_key": "cmd+w",
        "coordinates_verified": True,
        "scroll_units": 4,
        "max_tasks": 1,
    }
    if overrides.get("mode", "attached") == "automatic":
        profile["selected_task_ids"] = [task_id]
    profile.update(overrides)
    path.write_text(json.dumps(profile), encoding="utf-8")

def _setup(tmp_path: Path, *, mode="attached", transport_factory=None):
    input_dir, output_dir = tmp_path / "inbox", tmp_path / "output"
    _image(input_dir / "Shop A" / "menu-a.png")
    _image(input_dir / "Shop B" / "menu-b.png", "gray")
    configs = ConfigStore(tmp_path / "settings")
    configs.save({
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "model": {"provider": "openai_compatible", "base_url": "https://vision.example/v1", "model": "vision", "api_key": "secret"},
    })
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.scan(input_dir)
    tasks = store.tasks()
    selected = next(task for task in tasks if task["source_name"] == "Shop A")
    other = next(task for task in tasks if task["source_name"] == "Shop B")
    profile_path = tmp_path / "uipath.profile.json"
    _profile(profile_path, selected["id"], mode=mode)
    runner = UiPathRunner(
        configs,
        store,
        profile_path,
        transport_factory=transport_factory or (lambda ipc, out: FakeActor(ipc, out)),
    )
    return runner, store, selected, other, profile_path, output_dir

def _set_auto_authorize(runner: UiPathRunner, enabled: bool) -> None:
    settings = runner.configs.load()
    object.__setattr__(settings, "auto_authorize", enabled)
    runner.configs.load = lambda: settings

def _agent_menu_success(*_args, observe, **_kwargs):
    frame = observe(1)
    return ({"outcome": "success", "reason": "菜单可采集", "source": "agent",
             "menu_region": {"x": 20, "y": 20, "width": 180, "height": 120}}, frame)

class FakeCliProcess:
    def __init__(self, pid=43210, exit_code=None):
        self.pid = pid
        self.exit_code = exit_code
        self.wait_calls = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.exit_code is None:
            self.exit_code = 0
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = -15

    def kill(self):
        self.killed = True
        self.exit_code = -9

def test_file_assistant_ocr_uses_chat_content_boundary_not_sidebar_text():
    # 真实 864px 截图中聊天标题 OCR 框从 x=234 开始，侧栏/会话区分界约在 x=220；
    # Windows 微信 4.x 侧栏是固定宽度（约 232px），不随窗口缩放。
    chat_x = 244 if sys.platform == "win32" else 234
    assert UiPathRunner._assistant_active(
        [{"text": "文件传输助手", "x": chat_x, "y": 20}], 864, 958
    )
    assert not UiPathRunner._assistant_active(
        [{"text": "文件传输助手", "x": 116, "y": 66}], 864, 958
    )

class FakeActor:
    def __init__(self, ipc_dir: Path, output_dir: Path):
        self.ipc_dir, self.output_dir = Path(ipc_dir), Path(output_dir)
        self.commands = []
        self.scrolls = 0
        self.screenshot_count = 0
        self.stop_requested = False
        self.run_timeouts = []
        self._window_bounds = (120.0, 80.0)
        self._window_selector = "recorded-miniapp-selector"
        self._window_pixels = None
        self._recognize_count = 0
        self._active_miniapp = None

    def run(self, command, timeout=90, *, cancel_event=None):
        self.run_timeouts.append(timeout)
        if cancel_event and cancel_event.is_set():
            from menu_capture.uipath_runner import RobotCancelled
            raise RobotCancelled("paused")
        command = {"id": "fake-" + str(len(self.commands)), **command}
        self.commands.append(command)
        if command["action"] == "discover":
            windows = [{"title": "微信", "app": "WeChat", "role": "AXWindow", "position": {"x": 0, "y": 0, "width": 864, "height": 958}}]
            miniapp = self._active_miniapp
            if miniapp is None and self._recognize_count == 0:
                miniapp = {"title": "Shop A", "app": "WeChat", "role": "AXWindow", "position": {"x": 0, "y": 35, "width": 310, "height": 800}}
            if miniapp is not None:
                windows.append(miniapp)
            return {"id": command["id"], "ok": True, "windows": windows}
        if command["action"] == "click" and command.get("selector") == "recorded-preview-selector":
            self._recognize_count += 1
            self._active_miniapp = {"title": f"Shop {self._recognize_count}", "app": "WeChat", "role": "AXWindow", "position": {"x": 0, "y": 35, "width": 310, "height": 800}}
        if command["action"] == "key" and command.get("key") == "cmd+w":
            self._active_miniapp = None
        if command["action"] == "stop":
            self.stop_requested = True
        if command["action"] in {"scroll", "scroll_capture"}:
            self.scrolls += 1
        if command["action"] not in {"screenshot", "scroll_capture"}:
            return {"id": command["id"], "ok": True, "error": ""}
        self.screenshot_count += 1
        self._window_selector = command["selector"]
        self._window_pixels = (240, 160)
        path = Path(command["path"])
        # 菜单页黑白交替，每 3 次滚动重复一次，模拟“滚动到底”；跨门店仍能稳定复现。
        color = "black" if self.scrolls % 3 == 1 else "white"
        _image(path, color, size=(240, 160))
        return {
            "id": command["id"], "ok": True, "error": "", "path": str(path),
            "width": 240, "height": 160, "window_width": 120, "window_height": 80,
        }

    def request_stop(self):
        self.commands.append({"action": "stop"})
        self.stop_requested = True

class PagingActor(FakeActor):
    def __init__(self, ipc_dir: Path, output_dir: Path, *, changing_forever=False, page_limit=None):
        super().__init__(ipc_dir, output_dir)
        self.page = 0
        self.changing_forever = changing_forever
        self.page_limit = page_limit

    def run(self, command, timeout=90, *, cancel_event=None):
        command = {"id": "fake-" + str(len(self.commands)), **command}
        self.commands.append(command)
        if command["action"] in {"scroll", "scroll_capture"}:
            if command.get("direction") == "Up":
                self.page = max(0, self.page - 1)
            elif self.page_limit is None or self.page < self.page_limit - 1:
                self.page += 1
        if command["action"] not in {"screenshot", "scroll_capture"}:
            return {"id": command["id"], "ok": True, "error": ""}
        self.screenshot_count += 1
        if self.changing_forever:
            color = "white" if self.screenshot_count % 2 else "black"
        else:
            color = ((self.page * 31) % 256, (self.page * 67) % 256, (self.page * 97) % 256)
        path = Path(command["path"])
        _image(path, color, size=(240, 160))
        return {
            "id": command["id"], "ok": True, "error": "", "path": str(path),
            "width": 240, "height": 160, "window_width": 120, "window_height": 80,
        }

@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_attached_mode_waits_for_manual_main_and_publishes_agent_capture(tmp_path: Path, monkeypatch, platform) -> None:
    monkeypatch.setattr(uipath_runner_module, "sys", SimpleNamespace(platform=platform))
    runner, store, selected, other, _, output_root = _setup(tmp_path)

    def navigate(_model, _name, _people, _steps, *, observe, **_kwargs):
        frame = observe(1)
        return ({"outcome": "success", "reason": "菜单可采集", "source": "agent",
                 "menu_region": {"x": 20, "y": 20, "width": 180, "height": 120}}, frame)

    monkeypatch.setattr(uipath_runner_module, "run_navigation", navigate)

    assert runner.readiness()["ready"] is True
    started = runner.start()
    assert started["task_id"] == selected["id"]
    assert started["in_RunDirectory"] == str(runner.run_directory)
    assert started["main_file"] == "Main.xaml"
    assert started["startup_timeout_seconds"] == 300
    assert (runner.run_directory / "entry.json").is_file()
    assert started["message"].startswith("请在 UiPath Studio 手动运行")
    assert runner.thread is not None
    runner.thread.join(timeout=5)
    assert not runner.thread.is_alive()

    record = store.get(selected["id"])
    assert record["status"] == "captured", record["message"]
    assert runner.transport.run_timeouts[0] == 300
    assert runner.transport.stop_requested is True
    if platform == "win32":
        assert not any(command["action"] in {"resize", "capture_rect"} for command in runner.transport.commands)
    assert store.get(other["id"])["status"] == "pending"
    report_path = next((output_root / ".uipath-runs").glob("*/gui-agent/*/navigation-result.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["agent_outcome"] == "success"
    assert report["capture_status"] == "staged"
    assert list(Path(record["output_dir"]).glob("page-*.png"))
    assert Path(record["output_dir"]).is_relative_to(output_root)

def test_precheck_result_is_persisted_when_window_preparation_fails(tmp_path: Path, monkeypatch) -> None:
    """导航前失败也要把先验落盘，便于复盘错码原因。"""
    runner, _, selected, _, _, output_root = _setup(tmp_path)
    from menu_capture import qr_precheck
    monkeypatch.setattr(qr_precheck, "classify_image",
                        lambda *_args, **_kwargs: qr_precheck.QrHint(
                            status="ok", platform="dianping", purpose="group_buy",
                            evidence="点评扫码买单", confidence=0.9))

    def fail_prepare(*_args, **_kwargs):
        raise RuntimeError("window preparation failed")

    monkeypatch.setattr(runner, "_prepare_trial_window", fail_prepare)

    runner.start()
    runner.thread.join(timeout=10)
    assert not runner.thread.is_alive()

    report_path = next((output_root / ".uipath-runs").glob("*/gui-agent/*/navigation-result.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["runtime_status"] == "failed"
    assert report["qr_precheck"]["status"] == "ok"
    assert report["qr_precheck"]["purpose"] == "group_buy"
    assert report["qr_precheck"]["evidence"] == "点评扫码买单"

def _configure_cli_launch(runner, profile_path: Path, project_dir: Path, monkeypatch, **profile_overrides):
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "Main.xaml").write_text("<Activity />", encoding="utf-8")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile.update(launch_mode="cli", **profile_overrides)
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    runner.project_dir = project_dir
    monkeypatch.setattr("menu_capture.uipath_runner.shutil.which", lambda name, path=None: "/test/bin/uip" if name == "uip" else None)

def test_cli_launch_passes_private_run_directory_and_reaps_own_process(tmp_path: Path, monkeypatch):
    runner, store, selected, _, profile_path, _ = _setup(tmp_path)
    project_dir = tmp_path / "project"
    _configure_cli_launch(runner, profile_path, project_dir, monkeypatch)
    process = FakeCliProcess()
    captured = {}

    def process_factory(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return process

    runner.process_factory = process_factory
    monkeypatch.setattr(uipath_runner_module, "run_navigation", _agent_menu_success)
    started = runner.start()
    runner.thread.join(timeout=5)
    assert not runner.thread.is_alive()
    assert started["launch_mode"] == "cli"
    assert started["cli_pid"] == process.pid
    assert store.get(selected["id"])["status"] == "captured"
    command = captured["command"]
    args_path = runner.run_directory / "args.json"
    assert command == [
        "/test/bin/uip", "rpa", "run",
        "--project-dir", str(project_dir),
        "--file-path", "Main.xaml",
        "--input-arguments-file", str(args_path),
        "--timeout", "1260",
        "--output", "json",
    ]
    assert json.loads(args_path.read_text(encoding="utf-8")) == {"in_RunDirectory": str(runner.run_directory)}
    if sys.platform != "win32":
        # Windows 没有 POSIX 权限位，0600 只在 POSIX 平台成立。
        assert args_path.stat().st_mode & 0o777 == 0o600
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == str(project_dir)
    assert captured["kwargs"]["env"]["UIPATH_CLI_DISABLE_VERSION_SYNC"] == "true"
    if sys.platform != "win32":
        assert Path(runner._cli_log_path).stat().st_mode & 0o777 == 0o600
    metadata = json.loads(runner._cli_metadata_path.read_text(encoding="utf-8"))
    assert metadata["pid"] == process.pid and metadata["exit_code"] == 0
    assert process.wait_calls

def test_cli_readiness_and_startup_failure_are_fast_and_safe(tmp_path: Path, monkeypatch):
    runner, store, selected, _, profile_path, _ = _setup(tmp_path)
    _configure_cli_launch(runner, profile_path, tmp_path / "missing-project", monkeypatch)
    (runner.project_dir / "Main.xaml").unlink()
    assert any("缺少 Main.xaml" in issue for issue in runner.readiness()["issues"])
    with pytest.raises(ValueError, match="尚未就绪"):
        runner.start()

    (runner.project_dir / "Main.xaml").write_text("<Activity />", encoding="utf-8")
    runner.process_factory = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("sensitive cli detail"))
    with pytest.raises(ValueError, match="UiPath CLI 启动失败") as exc:
        runner.start()
    assert "sensitive cli detail" not in str(exc.value)
    assert runner.thread is None
    assert store.get(selected["id"])["status"] == "pending"

def test_cli_exit_before_handshake_marks_task_without_waiting_startup_timeout(tmp_path: Path, monkeypatch):
    runner, store, selected, _, profile_path, _ = _setup(tmp_path)
    _configure_cli_launch(runner, profile_path, tmp_path / "project", monkeypatch)
    process = FakeCliProcess(exit_code=37)
    runner.process_factory = lambda *args, **kwargs: process
    started_at = time.monotonic()
    runner.start()
    runner.thread.join(timeout=2)
    assert not runner.thread.is_alive()
    assert time.monotonic() - started_at < 2
    assert store.get(selected["id"])["status"] == "needs_review"
    assert "exit code 37" in store.get(selected["id"])["message"]
    metadata = json.loads(runner._cli_metadata_path.read_text(encoding="utf-8"))
    assert metadata["exit_code"] == 37
    assert process.wait_calls

def test_cli_handshake_keeps_one_command_id_when_actor_reply_is_delayed(tmp_path: Path):
    runner, _, _, _, _, _ = _setup(tmp_path)
    ipc = tmp_path / "delayed-ipc"
    runner.transport = FileRobotTransport(ipc, ipc, poll_interval=.01)
    runner._cli_process = FakeCliProcess()
    seen = []

    def delayed_actor():
        deadline = time.monotonic() + 4
        while not (ipc / "command.json").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        command = json.loads((ipc / "command.json").read_text())
        seen.append(command["id"])
        time.sleep(1.2)  # 超过旧实现的一秒重发间隔。
        (ipc / "reply.json").write_text(json.dumps({"id": command["id"], "ok": True, "error": ""}))

    actor = threading.Thread(target=delayed_actor)
    actor.start()
    runner._await_cli_startup()
    actor.join(timeout=3)
    assert not actor.is_alive()
    assert json.loads((ipc / "command.json").read_text())["id"] == seen[0]
    assert len(list((ipc / "commands").glob("*.json"))) == 1

def test_cli_pause_waits_for_store_boundary_and_reaps_only_its_process(tmp_path: Path, monkeypatch):
    runner, store, selected, _, profile_path, _ = _setup(tmp_path)
    _configure_cli_launch(runner, profile_path, tmp_path / "project", monkeypatch)
    process = FakeCliProcess()
    runner.process_factory = lambda *args, **kwargs: process

    def pause_after_observe(*args, **kwargs):
        runner.pause()
        return _agent_menu_success(*args, **kwargs)

    monkeypatch.setattr(uipath_runner_module, "run_navigation", pause_after_observe)
    runner.start()
    runner.thread.join(timeout=5)
    assert not runner.thread.is_alive()
    assert runner.transport.stop_requested is True
    assert process.wait_calls
    assert store.get(selected["id"])["status"] == "captured"
    assert any(command.get("action") == "scroll_capture" for command in runner.transport.commands)
    assert store.latest_run()["status"] == "completed"

def test_attached_mode_no_longer_requires_recorded_window_selector(tmp_path: Path) -> None:
    runner, _, _, _, profile_path, _ = _setup(tmp_path)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["miniapp_selector"] = ""
    profile["coordinates_verified"] = False
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    assert runner.readiness()["ready"] is True

def test_window_discovery_ignores_wechat_main_and_refuses_ambiguous():
    main = {"title": "微信", "app": "WeChat", "role": "AXWindow", "position": {"width": 864, "height": 958}}
    mini = {"title": "聚味美砂锅鲜炒鸡", "app": "WeChat", "role": "AXWindow", "position": {"width": 310, "height": 1011}}
    assert "聚味美砂锅鲜炒鸡" in UiPathRunner._select_miniapp_selector([main, mini])
    with pytest.raises(NeedsReview):
        UiPathRunner._select_miniapp_selector([mini, {**mini, "title": "另一家"}])

def test_annotation_people_grid_target_uses_regular_numeric_row():
    runner = object.__new__(UiPathRunner)
    rows = []
    for label, center_x in ((1, 50), (2, 150), (3, 250)):
        rows.append({"text": str(label), "x": (center_x - 3) * 3, "y": 2400,
                     "width": 18, "height": 30, "confidence": .99})

    decision = runner._local_people_grid_target(((310, 1011), rows), 1)

    assert decision is not None
    assert decision.target_label == "1"
    assert decision.x == pytest.approx(50)
    assert runner._last_target_resolution["method"] == "local_people_grid_geometry"

def test_annotation_prepare_action_finds_visible_menu_entry():
    runner = object.__new__(UiPathRunner)
    rows = [{"text": "开始点餐", "x": 90, "y": 180, "width": 150, "height": 36, "confidence": .99}]

    decision = runner._local_prepare_action(((240, 160), rows), people_count=1, auto_authorize=False)

    assert decision is not None
    assert decision.target_label == "开始点餐"
    assert decision.x == pytest.approx(55)
    assert decision.y == pytest.approx(66)

def test_annotation_button_visual_state_distinguishes_disabled_and_enabled(tmp_path: Path):
    path = tmp_path / "frame.png"
    image = Image.new("RGB", (200, 100), "white")
    image.paste((238, 238, 238), (10, 50, 80, 80))
    image.paste((52, 120, 246), (100, 50, 190, 80))
    image.save(path)

    disabled = UiPathRunner._button_visual_state(path, {"x": 10, "y": 50, "width": 70, "height": 30}, (200, 100))
    enabled = UiPathRunner._button_visual_state(path, {"x": 100, "y": 50, "width": 90, "height": 30}, (200, 100))

    assert disabled["available"] is False
    assert enabled["available"] is True

def test_annotation_local_button_resolution_selects_matching_roi_candidate(tmp_path: Path):
    path = tmp_path / "frame.png"
    image = Image.new("RGB", (310, 1312), "white")
    image.paste((52, 120, 246), (120, 270, 180, 290))
    image.save(path)
    rows = [
        {"text": "确定", "x": 480, "y": 280, "width": 240, "height": 80, "confidence": .9},
        {"text": "取消", "x": 760, "y": 280, "width": 240, "height": 80, "confidence": .9},
    ]
    runner = object.__new__(UiPathRunner)
    runner.ocr_reader = lambda _: rows
    runner._active_ocr_result = ((310, 1312), [])
    decision = SceneDecision(
        action="click", x=150, y=380, target_label="确定", reason="点击确定继续",
        expects_transition=True, overlay_visible=False, login_required=False,
        phone_required=False, consent_required=False, transaction_visible=False,
    )

    resolved = runner._local_button_candidates_near(path, decision)

    assert resolved is not None
    assert resolved["text"] == "确定"
    assert resolved["center_image_px"]["x"] == pytest.approx(150)

def test_annotation_store_match_uses_brand_but_not_city_or_single_character():
    assert UiPathRunner._store_matches("上海＿海底捞＿001", ["海底捞火锅"])
    assert not UiPathRunner._store_matches("上海＿海底捞＿001", ["上海市"])
    assert not UiPathRunner._store_matches("上海＿海底捞＿001", ["捞"])

def test_annotation_menu_preflight_resolves_clear_menu_and_rejects_dimmed_overlay(tmp_path: Path):
    runner, _, _, _, _, _ = _setup(tmp_path)
    runner.ocr_reader = lambda _path: _menu_anchor_ocr_rows()
    path = tmp_path / "menu.png"
    source_name = "ZA0070005＿老胖串店＿济南市＿老胖串店中央厨房"
    _image(path, "white", size=(414, 780))

    clear_page = runner._local_ready_preflight(path, source_name)
    _image(path, "#707070", size=(414, 780))
    dimmed_page = runner._local_ready_preflight(path, source_name)

    assert clear_page is not None and clear_page.ready_evidence_ok
    assert dimmed_page is None

def test_automatic_readiness_allows_missing_ocr(tmp_path: Path) -> None:
    runner, _, _, _, profile_path, _ = _setup(tmp_path, mode="automatic")
    runner.clipboard_preparer = lambda _path: None
    runner.clipboard_text_preparer = lambda _text: None
    runner.clipboard_text_preparer = lambda _text: None
    runner.ocr_reader = None
    status = runner.readiness()
    assert status["ready"] is True, status["issues"]

def test_automatic_window_discovery_selects_only_window_added_after_qr(tmp_path: Path) -> None:
    runner, _, _, _, profile_path, _ = _setup(tmp_path, mode="automatic")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["miniapp_selector"] = ""
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    runner.clipboard_preparer = lambda _path: None
    runner.clipboard_text_preparer = lambda _text: None
    runner.ocr_reader = lambda _path: []
    runner.qr_detector = lambda _path: []
    assert runner.readiness()["ready"] is True

    main = {"title": "微信", "app": "WeChat", "role": "AXWindow", "position": {"x": 0, "y": 0, "width": 864, "height": 958}}
    stale = {"title": "旧店小程序", "app": "WeChat", "role": "AXWindow", "position": {"x": 0, "y": 35, "width": 310, "height": 900}}
    opened = {"title": "扫码新店", "app": "WeChat", "role": "AXWindow", "position": {"x": 0, "y": 35, "width": 310, "height": 900}}
    delta = UiPathRunner._new_windows([main, stale], [main, stale, opened])
    selector = UiPathRunner._select_miniapp_selector(delta)
    assert "扫码新店" in selector
    with pytest.raises(NeedsReview):
        UiPathRunner._select_miniapp_selector(UiPathRunner._new_windows([main, stale], [main, stale]))

def test_automatic_profile_requires_preview_window_but_no_legacy_button_coordinates(tmp_path: Path) -> None:
    runner, _, _, _, profile_path, _ = _setup(tmp_path, mode="automatic")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile.pop("preview_selector")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    assert any("preview_selector" in issue for issue in runner.readiness()["issues"])
    profile["preview_selector"] = "recorded-preview-selector"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    assert not any("recognize" in issue for issue in runner.readiness()["issues"])

def test_attached_readiness_does_not_require_wechat_selector(tmp_path: Path) -> None:
    runner, _, _, _, profile_path, _ = _setup(tmp_path)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["wechat_selector"] = ""
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    assert runner.readiness()["ready"] is True

def _menu_anchor_ocr_rows():
    def row(text, x, y, width, height, confidence=0.98):
        return {
            "text": text, "x": x * 3, "y": y * 3,
            "width": width * 3, "height": height * 3, "confidence": confidence,
        }

    return [
        row("老胖串店龙湖店", 12, 250, 122, 22),
        row("头牌肉串", 15, 321, 56, 16),
        row("头牌肉串", 109, 348, 56, 16),
        row("第二香串", 15, 385, 56, 16),
        row("新品推荐", 15, 451, 56, 16),
        row("精酿鲜啤", 15, 520, 56, 16),
        row("原创烤串", 15, 580, 56, 16),
        row("精选烤串", 15, 645, 56, 16),
        row("青梅羊排串（串）", 108, 624, 127, 19),
        row("¥3.9/串", 110, 703, 47, 17, confidence=0.3),
        row("首页", 18, 762, 25, 18),
        row("扫码点餐", 185, 762, 54, 18),
        row("我的", 376, 762, 25, 18),
    ]

def test_task_deadline_starts_after_manual_uipath_handshake(tmp_path: Path, monkeypatch) -> None:
    reached_handshake, release_handshake = threading.Event(), threading.Event()
    runner, store, selected, _, _, _ = _setup(tmp_path)
    actor_holder = {}

    class DelayedHandshakeActor(FakeActor):
        def run(self, command, timeout=90, *, cancel_event=None):
            if command.get("action") == "noop" and not reached_handshake.is_set():
                self.run_timeouts.append(timeout)
                reached_handshake.set()
                assert release_handshake.wait(timeout=2)
            return super().run(command, timeout=timeout, cancel_event=cancel_event)

    runner.transport_factory = lambda ipc, out: actor_holder.setdefault("actor", DelayedHandshakeActor(ipc, out))
    monkeypatch.setattr(uipath_runner_module, "run_navigation", _agent_menu_success)
    runner.start()
    assert reached_handshake.wait(timeout=2)
    assert runner._deadline == 0
    release_handshake.set()
    runner.thread.join(timeout=5)
    assert not runner.thread.is_alive()
    assert actor_holder["actor"].run_timeouts[0] == 300
    assert store.get(selected["id"])["status"] == "captured"

def _service_transport_command(path: Path, *, wrong_id=False):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.005)
    command = json.loads(path.read_text(encoding="utf-8"))
    requested = Path(command["path"])
    _image(requested, size=(40, 30))
    response = {
        "id": "wrong" if wrong_id else command["id"], "ok": True, "error": "",
        "path": str(requested), "width": 40, "height": 30,
        "window_width": 20, "window_height": 15,
        "target_window": {
            "selector": command.get("selector"), "app": "WeChat", "title": "微信",
            "foreground": True, "capture_scope": "window",
        },
    }
    path.parent.joinpath("reply.json").write_text(json.dumps(response), encoding="utf-8")

def test_file_transport_validates_reply_id_and_window_point_bounds(tmp_path: Path) -> None:
    run_dir, output_dir = tmp_path / "ipc", tmp_path / "output"
    run_dir.mkdir()
    output_dir.mkdir()
    selector = "<wnd app='WeChat' title='微信' />"
    transport = FileRobotTransport(run_dir, output_dir, poll_interval=0.005)
    worker = threading.Thread(target=_service_transport_command, args=(transport.command_path,))
    worker.start()
    screenshot_path = output_dir / "screen.png"
    reply = transport.run({"action": "screenshot", "selector": selector, "path": str(screenshot_path)}, timeout=1)
    worker.join(timeout=1)
    assert reply["width"] == 40

    observed = {}

    def acknowledge_coordinate():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            command = json.loads(transport.command_path.read_text(encoding="utf-8"))
            if command.get("action") == "click":
                observed.update(command)
                transport.reply_path.write_text(json.dumps({"id": command["id"], "ok": True, "error": ""}), encoding="utf-8")
                return
            time.sleep(0.005)

    worker = threading.Thread(target=acknowledge_coordinate)
    worker.start()
    transport.run({"action": "click", "selector": selector, "x": 2, "y": 4}, timeout=1)
    worker.join(timeout=1)
    assert observed["expected_window_width"] == 20
    assert observed["expected_window_height"] == 15

    with pytest.raises(RobotProtocolError, match="最近一次同 selector 截图"):
        transport.run({"action": "click", "selector": "different-window", "x": 2, "y": 4}, timeout=1)

    before = json.loads(transport.command_path.read_text(encoding="utf-8"))
    with pytest.raises(RobotProtocolError, match="超出窗口 points 边界"):
        transport.run({"action": "click", "selector": selector, "x": 20, "y": 4}, timeout=1)
    after = json.loads(transport.command_path.read_text(encoding="utf-8"))
    assert after["id"] == before["id"]

def test_file_transport_rejects_mismatched_id_and_never_retries_on_timeout(tmp_path: Path) -> None:
    run_dir, output_dir = tmp_path / "ipc", tmp_path / "output"
    run_dir.mkdir()
    output_dir.mkdir()
    transport = FileRobotTransport(run_dir, output_dir, poll_interval=0.005)
    worker = threading.Thread(target=_service_transport_command, args=(transport.command_path,), kwargs={"wrong_id": True})
    worker.start()
    with pytest.raises(RobotProtocolError, match="id 与当前命令不匹配"):
        transport.run({"action": "screenshot", "selector": "recorded", "path": str(output_dir / "bad.png")}, timeout=1)
    worker.join(timeout=1)

    with pytest.raises(RobotTimeout, match="没有重发"):
        transport.run({"action": "noop"}, timeout=0.03)
    assert not transport.command_path.exists()

def test_transport_rejects_screenshot_paths_outside_task_output(tmp_path: Path) -> None:
    ipc, out = tmp_path / "ipc", tmp_path / "out"
    ipc.mkdir()
    out.mkdir()
    transport = FileRobotTransport(ipc, out)
    with pytest.raises(RobotProtocolError, match="必须位于当前任务输出目录"):
        transport.run({"action": "screenshot", "selector": "recorded", "path": str(tmp_path / "outside.png")})

def test_capture_menu_keeps_partial_images_but_fails_when_page_limit_is_exhausted(tmp_path: Path) -> None:
    runner, _, selected, _, _, _ = _setup(tmp_path)
    output = tmp_path / "partial-output"
    output.mkdir()
    actor = PagingActor(tmp_path / "ipc", output, page_limit=None)
    runner.transport = actor
    profile = {"scroll_units": 1}
    settings = runner.configs.load()
    settings.capture.max_pages = 2
    frame = runner._screenshot(output, "miniapp", "initial.png")

    with pytest.raises(NeedsReview, match="达到 max_pages=2"):
        runner._capture_menu("miniapp", frame, Region(x=20, y=20, width=180, height=120), settings, profile, output)
    assert len(list(output.glob("page-*.png"))) == 2

def test_capture_continues_until_two_adjacent_frames_match(tmp_path: Path) -> None:
    runner, _, selected, _, _, _ = _setup(tmp_path)
    output = tmp_path / "paging-output"
    output.mkdir()
    actor = PagingActor(tmp_path / "ipc", output, page_limit=8)
    runner.transport = actor
    profile = {"scroll_units": 1}
    settings = runner.configs.load()
    settings.capture.max_pages = 12
    frame = runner._screenshot(output, "miniapp", "initial.png")
    pages = runner._capture_menu("miniapp", frame, Region(x=20, y=20, width=180, height=120), settings, profile, output)
    assert len(pages) == 8
    ups = [command for command in actor.commands if command.get("action") == "scroll" and command.get("direction") == "Up"]
    assert not ups
    downs = [c for c in actor.commands if c.get("action") == "scroll_capture" and c.get("direction") == "Down"]
    assert len(downs) == 8
    # 滚动落点在菜单区域上部约 30% 高度。
    assert all(c["y"] == pytest.approx((20 + 120 * 0.30) * 120 / 240) for c in downs)
    assert all(c["x"] == pytest.approx((20 + 180 * 0.65) * 120 / 240) for c in downs)

def test_automatic_two_task_batch_reuses_one_actor_and_closes_between_stores(tmp_path: Path, monkeypatch) -> None:
    runner, store, first, second, profile_path, output_root = _setup(tmp_path, mode="automatic")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["selected_task_ids"] = [second["id"], first["id"]]
    profile["max_tasks"] = 10
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(uipath_runner_module, "run_navigation", _agent_menu_success)
    runner.clipboard_preparer = lambda _path: None
    runner.clipboard_text_preparer = lambda _text: None

    def open_source(task, current_profile, *_args):
        current_profile["miniapp_selector"] = f"window:{task['id']}"
        return {"ok": True}

    def prepare_window(selector, *_args):
        runner.transport._window_selector = selector
        runner.transport._window_pixels = (240, 160)

    monkeypatch.setattr(runner, "_open_source_automatic", open_source)
    monkeypatch.setattr(runner, "_prepare_trial_window", prepare_window)
    monkeypatch.setattr(runner, "_return_to_wechat", lambda *_args: None)

    ready = runner.readiness()
    assert ready["ready"] is True
    assert ready["experimental_batch"] is True
    start_result = runner.start()
    actor = runner.transport
    runner.thread.join(timeout=10)

    assert not runner.thread.is_alive()
    assert store.get(first["id"])["status"] == "captured", store.get(first["id"])
    assert store.get(second["id"])["status"] == "captured", store.get(second["id"])
    assert start_result["task_count"] == 2
    assert start_result["experimental_batch"] is True
    assert Path(store.get(first["id"])["output_dir"]) != Path(store.get(second["id"])["output_dir"])
    assert Path(store.get(first["id"])["output_dir"]) == output_root / "Shop A"
    assert Path(store.get(second["id"])["output_dir"]) == output_root / "Shop B"
    assert Path(store.get(first["id"])["output_dir"]).is_relative_to(output_root)
    assert Path(store.get(second["id"])["output_dir"]).is_relative_to(output_root)
    for selected in (first, second):
        output = Path(store.get(selected["id"])["output_dir"])
        assert list(output.iterdir())
        assert all(path.is_file() and path.suffix == ".png" and path.name.startswith("page-") for path in output.iterdir())
    assert not (output_root / "100").exists()
    assert [command["action"] for command in actor.commands].count("scroll_capture") >= 2

def test_automatic_publish_replaces_only_the_selected_store_and_keeps_only_menu_pages(tmp_path: Path) -> None:
    runner, _, selected, other, _, output_root = _setup(tmp_path, mode="automatic")
    selected_dir = output_root / selected["source_name"]
    other_dir = output_root / other["source_name"]
    selected_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    (selected_dir / "old-page.png").write_bytes(b"old")
    (other_dir / "keep.png").write_bytes(b"keep")

    staging = runner._staging_dir(output_root, selected, "attempt-test")
    staging.mkdir(parents=True)
    _image(staging / "page-0001.png")
    _image(staging / "page-0002.png", "gray")
    _image(staging / "evidence" / "initial.png")
    (staging / "snapshotproof.json").write_text("{}", encoding="utf-8")
    (staging / "result.json").write_text("{}", encoding="utf-8")

    published = runner._publish_store_output(output_root, selected, staging)

    assert published == selected_dir
    assert sorted(path.name for path in published.iterdir()) == ["page-0001.png", "page-0002.png"]
    assert (other_dir / "keep.png").read_bytes() == b"keep"
    assert not staging.exists()

def test_automatic_failed_retry_preserves_published_store_output_and_cleans_staging(tmp_path: Path) -> None:
    runner, store, selected, _, _, output_root = _setup(tmp_path, mode="automatic")
    destination = output_root / selected["source_name"]
    destination.mkdir(parents=True)
    (destination / "previous.png").write_bytes(b"previous")
    runner.clipboard_preparer = lambda path: None
    runner.clipboard_text_preparer = lambda _text: None
    runner.ocr_reader = lambda path: []
    runner.qr_detector = lambda path: []

    def fail_capture(*args, **kwargs):
        raise NeedsReview("模拟采集失败")

    runner._run_task = fail_capture
    runner.start()
    runner.thread.join(timeout=5)

    assert not runner.thread.is_alive()
    assert store.get(selected["id"])["status"] == "needs_review"
    assert (destination / "previous.png").read_bytes() == b"previous"
    assert not any(path.is_file() for path in (output_root / ".uipath-staging").rglob("*"))

def test_automatic_selection_rejects_two_tasks_for_same_store_output(tmp_path: Path) -> None:
    runner, store, selected, _, profile_path, _ = _setup(tmp_path, mode="automatic")
    input_root = Path(runner.configs.load().input_dir)
    _image(input_root / selected["source_name"] / "second-qr.png", "gray")
    store.scan(input_root)
    same_store = [task for task in store.tasks() if task["source_name"] == selected["source_name"]]
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["selected_task_ids"] = [task["id"] for task in same_store]
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    readiness = runner.readiness()

    assert readiness["ready"] is False
    assert any("同一个门店输出目录" in issue for issue in readiness["issues"])

def test_automatic_batch_is_capped_by_max_tasks(tmp_path: Path) -> None:
    runner, store, selected, other, profile_path, _ = _setup(tmp_path, mode="automatic")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["selected_task_ids"] = [selected["id"], other["id"]]
    profile["max_tasks"] = 1
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    runner.clipboard_preparer = lambda _path: None
    runner.clipboard_text_preparer = lambda _text: None
    runner.ocr_reader = lambda _path: []
    runner._launch_cli = lambda *args, **kwargs: None
    runner._worker = lambda *args, **kwargs: None

    started = runner.start()
    runner.thread.join(timeout=2)

    assert started["task_count"] == 1
    assert store.latest_run()["task_ids"] == [selected["id"]]

def test_fused_ocr_merges_scales_into_the_3x_coordinate_space(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    path = tmp_path / "frame.png"
    _image(path, size=(240, 160))

    def reader(image_path: Path) -> list[dict]:
        with Image.open(image_path) as image:
            scale = image.width // 240
        return [{"text": "同意", "x": 20 * scale, "y": 10 * scale,
                 "width": 30 * scale, "height": 12 * scale, "confidence": 0.9}]

    runner.ocr_reader = reader
    base = runner._read_scaled_ocr(path)
    fused = runner._read_fused_scaled_ocr(path, base)

    assert base[0] == fused[0] == (240, 160)
    assert len(fused[1]) == 1
    assert fused[1][0]["x"] == pytest.approx(60)
    assert fused[1][0]["y"] == pytest.approx(30)
    assert fused[1][0]["width"] == pytest.approx(90)
    assert runner._last_ocr_meta["fused"] is True
    assert runner._last_ocr_meta["fusion_rows_by_scale"] == {"2": 1, "4": 1}

@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不使用 capture_rect 快路径，直接走 UiPath 窗口截图")
def test_fast_window_capture_rejects_stale_rect_and_wrong_size(tmp_path):
    runner = object.__new__(UiPathRunner)
    runner._fast_capture_enabled = True
    runner.transport = FileRobotTransport(tmp_path / "ipc")
    runner.transport._window_origin = (10, 20)
    runner.transport._window_bounds = (240, 800)
    runner.transport._window_pixels = (480, 1600)
    runner.transport._window_selector = "sel"

    def wrong_size(command, timeout=None):
        _image(Path(command["path"]), "white", (480, 800))
        return {"path": command["path"]}

    runner._send = wrong_size
    assert runner._fast_window_capture(tmp_path, "sel", "shot.png") is None

    # 换了窗口就不能再用旧矩形，必须重新走完整截图。
    runner.transport._window_selector = "other"
    assert runner._fast_window_capture(tmp_path, "sel", "shot.png") is None
    runner.transport._window_selector = "sel"

    def good(command, timeout=None):
        _image(Path(command["path"]), "white", (480, 1600))
        return {"path": command["path"]}

    runner._send = good
    frame = runner._fast_window_capture(tmp_path, "sel", "shot.png")
    assert frame["width"] == 480 and frame["height"] == 1600
    assert frame["window_x"] == 10 and frame["window_width"] == 240

    def dark(command, timeout=None):
        _image(Path(command["path"]), "black", (480, 1600))
        return {"path": command["path"]}

    runner._send = dark
    assert runner._fast_window_capture(tmp_path, "sel", "dark.png") is None

@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不使用 capture_rect 快路径，直接走 UiPath 窗口截图")
def test_screenshot_reuses_confirmed_rect_for_same_window(tmp_path):
    runner = object.__new__(UiPathRunner)
    runner._fast_capture_enabled = True
    runner.transport = FileRobotTransport(tmp_path / "ipc")
    runner.transport._window_origin = (10, 20)
    runner.transport._window_bounds = (240, 800)
    runner.transport._window_pixels = (480, 1600)
    runner.transport._window_selector = "wechat"
    actions = []

    def send(command, timeout=None):
        actions.append(command["action"])
        _image(Path(command["path"]), "white", (480, 1600))
        return {"path": command["path"]}

    runner._send = send
    frame = runner._screenshot(tmp_path, "wechat", "wechat-after-send.png")
    assert frame["width"] == 480
    assert actions == ["capture_rect"]

    # 换到另一个窗口时必须退回完整 UiPath 截图，不能沿用旧矩形。
    actions.clear()
    runner._screenshot(tmp_path, "preview", "image-preview.png")
    assert actions == ["screenshot"]

def test_build_cache_skips_rebuild_only_when_sources_unchanged(tmp_path):
    project = tmp_path / "proj"
    (project / ".local" / "install").mkdir(parents=True)
    (project / "Main.xaml").write_text("<Activity/>", encoding="utf-8")
    (project / "project.json").write_text("{}", encoding="utf-8")
    runner = object.__new__(UiPathRunner)
    runner.project_dir = project
    runner._build_fingerprint = runner._project_build_fingerprint()
    runner._build_cache_path = tmp_path / "out" / ".uipath-build" / "state.json"

    assert runner._build_cache_matches() is False
    runner._record_build_cache()
    assert runner._build_cache_matches() is True

    (project / "Main.xaml").write_text("<Activity x:Class='Main'/>", encoding="utf-8")
    runner._build_fingerprint = runner._project_build_fingerprint()
    assert runner._build_cache_matches() is False

def _evidence_case(root: Path, name: str, *, outcome: str, capture: str, age_seconds: float) -> Path:
    case = root / ".uipath-runs" / "run-1" / "gui-agent" / name
    case.mkdir(parents=True)
    _image(case / "ui-tars-001.png", "white")
    _image(case / "ui-tars-handoff.png", "gray")
    (case / "agent-calls.jsonl").write_text("{}\n", encoding="utf-8")
    (case / "navigation-result.json").write_text(json.dumps({
        "agent_outcome": outcome,
        "capture": {"status": capture},
        "navigation": {"frame": str(case / "ui-tars-handoff.png")},
    }), encoding="utf-8")
    stamp = time.time() - age_seconds
    for path in [case / "ui-tars-001.png", case / "ui-tars-handoff.png",
                 case / "agent-calls.jsonl", case / "navigation-result.json"]:
        os.utime(path, (stamp, stamp))
    return case


def test_prune_keeps_success_evidence_today_and_trims_it_after_one_day(tmp_path):
    runner, _store, _selected, _other, _profile_path, output_dir = _setup(tmp_path)
    fresh = _evidence_case(output_dir, "fresh", outcome="success", capture="staged", age_seconds=60)
    stale = _evidence_case(output_dir, "stale", outcome="success", capture="staged", age_seconds=25 * 3600)

    runner._prune_run_evidence(output_dir, 1024)

    # 当天成功门店保留完整过程截图，供人工复核。
    assert (fresh / "ui-tars-001.png").is_file()
    # 一天后只留导航结果、Agent 日志和交接帧。
    assert not (stale / "ui-tars-001.png").exists()
    assert (stale / "ui-tars-handoff.png").is_file()
    assert (stale / "navigation-result.json").is_file()
    assert (stale / "agent-calls.jsonl").is_file()


def test_prune_deletes_failed_evidence_after_two_days_only(tmp_path):
    runner, _store, _selected, _other, _profile_path, output_dir = _setup(tmp_path)
    recent = _evidence_case(output_dir, "recent-failure", outcome="needs_review", capture="not_started",
                            age_seconds=25 * 3600)
    expired = _evidence_case(output_dir, "old-failure", outcome="needs_review", capture="not_started",
                             age_seconds=49 * 3600)

    runner._prune_run_evidence(output_dir, 1024)

    assert (recent / "ui-tars-001.png").is_file()
    assert not expired.exists()


def test_send_button_state_detects_pasted_content(tmp_path):
    # 微信发送按钮：输入框为空时灰色，有内容时变绿；用它判断大图是否已附着。
    empty = tmp_path / "empty.png"
    attached = tmp_path / "attached.png"
    _image(empty, "#EDEDED", size=(999, 749))
    _image(attached, "#EDEDED", size=(999, 749))
    with Image.open(attached) as image:
        ImageDraw.Draw(image).rectangle((919, 696, 969, 726), fill="#07C160")
        image.save(attached)

    assert UiPathRunner._send_button_active(empty) is False
    assert UiPathRunner._send_button_active(attached) is True
    assert UiPathRunner._send_button_active(tmp_path / "missing.png") is False


def test_image_attach_timeout_scales_with_pixel_count():
    # 12MP 手机原图约 15 秒；小图最低 3 秒；超大图封顶 20 秒，避免单店无限等。
    assert UiPathRunner._image_attach_timeout((3024, 4032)) == pytest.approx(15.193, abs=.001)
    assert UiPathRunner._image_attach_timeout((800, 600)) == pytest.approx(3.48, abs=.01)
    assert UiPathRunner._image_attach_timeout((0, 0)) == pytest.approx(3.0)
    assert UiPathRunner._image_attach_timeout((12000, 9000)) == 20.0
