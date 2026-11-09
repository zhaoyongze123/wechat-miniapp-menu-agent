from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from menu_capture.config import ConfigStore
from menu_capture.store import TaskStore
from menu_capture.scene_agent import SceneAgent
from menu_capture.model import Region
from menu_capture.scene_agent import SceneDecision
from menu_capture.scene_agent import SceneError
from menu_capture.uipath_runner import (
    AuthorizationUnavailable,
    FileRobotTransport,
    NeedsRetry,
    NeedsReview,
    RobotProtocolError,
    RobotTimeout,
    UiPathRunner,
)


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


def _setup(tmp_path: Path, *, mode="attached", model_factory=None, transport_factory=None):
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
        model_factory=model_factory or (lambda config, key: FakeModel()),
        transport_factory=transport_factory or (lambda ipc, out: FakeActor(ipc, out)),
    )
    return runner, store, selected, other, profile_path, output_dir


def scene_response(action="ready", **changes) -> str:
    data = {
        "action": action,
        "reason": "菜名和价格可见",
        "x": None,
        "y": None,
        "pixels": 0,
        "menu_region": {"x": 20, "y": 20, "width": 180, "height": 120} if action == "ready" else None,
        "categories": ["热菜"],
        "overlay_visible": False,
        "login_required": False,
        "phone_required": False,
        "consent_required": False,
        "transaction_visible": False,
        "target_label": "",
        "item_names_visible": ["示例菜"],
        "prices_visible": ["¥18"],
        "observed_store_text": ["Shop A"],
        "selected_people_count": None,
    }
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


def _set_auto_authorize(runner: UiPathRunner, enabled: bool) -> None:
    settings = runner.configs.load()
    object.__setattr__(settings, "auto_authorize", enabled)
    runner.configs.load = lambda: settings


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


class FakeModel:
    def __init__(self, callback=None, responses=None):
        self.callback = callback
        self.responses = list(responses or [])
        self.calls = 0
        self.closed = False

    def _request(self, image, prompt):
        self.calls += 1
        if self.callback:
            self.callback()
        return self.responses.pop(0) if self.responses else scene_response()

    def close(self):
        self.closed = True


def test_ready_ignores_unused_click_coordinates_but_validates_menu_region(tmp_path: Path):
    path = tmp_path / "menu.png"
    _image(path)
    model = FakeModel(responses=[scene_response(x=9999, y=9999, target_label=None)])
    decision = SceneAgent(model).decide(path, "只读菜单判断")
    assert decision.ready_evidence_ok
    assert decision.target_label == ""


def test_file_assistant_ocr_uses_chat_content_boundary_not_sidebar_text():
    # 真实 864px 截图中聊天标题 OCR 框从 x=234 开始，侧栏/会话区分界约在 x=220。
    assert UiPathRunner._assistant_active(
        [{"text": "文件传输助手", "x": 234, "y": 20}], 864, 958
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
        path = Path(command["path"])
        color = "white" if self.scrolls == 0 else "gray"
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


def test_attached_mode_waits_for_manual_main_and_archives_only_selected_task(tmp_path: Path) -> None:
    runner, store, selected, other, _, output_root = _setup(tmp_path)

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
    assert store.get(other["id"])["status"] == "pending"
    proof_path = Path(record["output_dir"]) / "snapshotproof.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["task_id"] == selected["id"]
    assert proof["source_hash"] == selected["source_hash"]
    assert proof["coverage_verified"] is False
    assert proof["pages"]
    assert Path(record["output_dir"]).is_relative_to(output_root)
    assert len(list(output_root.glob("Shop A/*/*/snapshotproof.json"))) == 1


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
    assert args_path.stat().st_mode & 0o777 == 0o600
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == str(project_dir)
    assert captured["kwargs"]["env"]["UIPATH_CLI_DISABLE_VERSION_SYNC"] == "true"
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
    runner.model_factory = lambda config, key: FakeModel(callback=runner.pause, responses=[scene_response(
        action="click", x=90, y=70, menu_region=None,
        target_label="菜单", item_names_visible=[], prices_visible=[],
    )])
    runner.start()
    runner.thread.join(timeout=5)
    assert not runner.thread.is_alive()
    assert runner.transport.stop_requested is True
    assert process.wait_calls
    assert store.get(selected["id"])["status"] == "captured"
    assert any(command.get("action") == "click" for command in runner.transport.commands)
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


def test_automatic_window_discovery_selects_only_window_added_after_qr(tmp_path: Path) -> None:
    runner, _, _, _, profile_path, _ = _setup(tmp_path, mode="automatic")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["miniapp_selector"] = ""
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    runner.clipboard_preparer = lambda _path: None
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


def test_scene_prompt_includes_configured_prompt_people_and_fixed_safety_afterward() -> None:
    prompt = SceneAgent._prompt("phase", (240, 160), [], "记录菜名及价格", 4)
    assert prompt.index("记录菜名及价格") < prompt.index("固定安全规则")
    assert "本次就餐人数为 4 人" in prompt
    assert "不得点击菜品价格、加号、购物车、去结算" in prompt


def test_auto_authorize_prompt_overrides_legacy_user_block_and_false_keeps_blocking() -> None:
    legacy = "遇到隐私协议、手机号必须 blocked"
    allowed = SceneAgent._prompt("phase", (240, 160), [], legacy, 1, True)
    denied = SceneAgent._prompt("phase", (240, 160), [], legacy, 1, False)
    assert allowed.index(legacy) < allowed.index("此设置覆盖用户配置提示词中的旧授权阻止要求")
    assert "验证码、扫码登录、没有可用凭据" in allowed
    assert "此设置覆盖用户配置提示词中的授权许可要求" in denied


def test_store_identity_uses_brand_segment_and_rejects_city_or_single_character() -> None:
    assert UiPathRunner._store_matches("上海＿海底捞＿001", ["海底捞火锅"]) is True
    assert UiPathRunner._store_matches("上海＿海底捞＿001", ["上海市"]) is False
    assert UiPathRunner._store_matches("上海＿海底捞＿001", ["捞"]) is False


@pytest.mark.parametrize("label", ["¥18", "+", "购物车", "去结算"])
def test_unqualified_or_transaction_click_target_never_reaches_actor(tmp_path: Path, label: str) -> None:
    def factory(config, key):
        return FakeModel(responses=[scene_response(
            action="click", x=100, y=80, menu_region=None,
            target_label=label, item_names_visible=[], prices_visible=[],
        )])

    runner, store, selected, _, _, _ = _setup(tmp_path, model_factory=factory)
    runner.start()
    runner.thread.join(timeout=5)
    assert store.get(selected["id"])["status"] == "needs_review"
    assert not any(command.get("action") == "click" for command in runner.transport.commands)


def test_model_click_is_sent_without_button_whitelist(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    actor = FakeActor(tmp_path / "ipc", tmp_path / "output")
    runner.transport = actor
    decision = SceneDecision.model_validate_json(scene_response(action="click", x=100, y=80, menu_region=None,
        target_label="知道了", reason="关闭提示", overlay_visible=True))
    runner._apply_scene_action(decision, {"width": 240, "height": 160}, "recorded-miniapp-selector", {})
    assert actor.commands[-1]["action"] == "click"


def test_people_grid_can_locate_low_confidence_digit_among_other_numbers() -> None:
    rows = [{"text": str(n), "x": 120*n, "y": 2400, "width": 18, "height": 30,
             "confidence": 0.5 if n == 1 else 0.99} for n in range(1, 5)]
    assert UiPathRunner._find_ocr_target(rows, (310, 1011), "1", 1, require_people_grid=True) == (43.0, 805.0)
    assert UiPathRunner._find_ocr_target(rows[:1], (310, 1011), "1", 1, require_people_grid=True) is None


def test_local_people_rule_moves_from_one_person_to_start_button() -> None:
    runner = object.__new__(UiPathRunner)
    rows = [{"text": str(n), "x": 120*n, "y": 2400, "width": 18, "height": 30,
             "confidence": 0.5 if n == 1 else 0.99} for n in range(1, 5)]
    rows.append({"text": "开始点餐", "x": 300, "y": 2800, "width": 150, "height": 30, "confidence": 0.99})
    assert runner._local_prepare_action(((310, 1011), rows), 1, True).target_label == "1"
    assert runner._local_prepare_action(((310, 1011), rows), 1, True, people_selected=True).target_label == "开始点餐"
    rows[-1]["text"] = "1人开始点餐"
    assert runner._local_prepare_action(((310, 1011), rows), 1, True, people_selected=True).target_label == "1人开始点餐"


def test_local_prepare_ocr_clicks_explicit_menu_entry_without_model(tmp_path: Path) -> None:
    runner, _, _, _, _, output_root = _setup(tmp_path)
    actor = FakeActor(tmp_path / "ipc", output_root)
    runner.transport = actor
    rows = [{"text": "开始点餐", "x": 90, "y": 180, "width": 150, "height": 36, "confidence": 0.99}]
    decision = runner._local_prepare_action(((240, 160), rows), people_count=1, auto_authorize=False)
    assert decision is not None
    assert decision.target_label == "开始点餐"
    assert decision.x == pytest.approx(55)
    assert decision.y == pytest.approx(66)
    frame = {"width": 240, "height": 160}
    runner._apply_scene_action(decision, frame, "recorded-miniapp-selector", {}, people_count=1)
    assert actor.commands[-1]["action"] == "click"


def test_local_prepare_ocr_requires_auto_authorize_and_ignores_payment_text() -> None:
    runner = object.__new__(UiPathRunner)
    consent_rows = [{"text": "同意并继续", "x": 60, "y": 60, "width": 120, "height": 30, "confidence": 0.99}]
    assert runner._local_prepare_action(((240, 160), consent_rows), 1, False) is None
    consent = runner._local_prepare_action(((240, 160), consent_rows), 1, True)
    assert consent is not None and consent.consent_required
    payment_rows = consent_rows + [{"text": "立即支付", "x": 20, "y": 120, "width": 90, "height": 30, "confidence": 0.99}]
    assert runner._local_prepare_action(((240, 160), payment_rows), 1, True) is None


def test_local_authorization_accepts_variants_and_excludes_negative_text() -> None:
    runner = object.__new__(UiPathRunner)
    def row(text):
        return {"text": text, "x": 60, "y": 60, "width": 120, "height": 30, "confidence": 0.99}
    assert runner._local_prepare_action(((240, 160), [row("授权后进入")]), 1, True).target_label == "授权后进入"
    assert runner._local_prepare_action(((240, 160), [row("不同意授权")]), 1, True) is None


def test_notification_permission_clicks_decline_even_with_auto_authorize() -> None:
    runner = object.__new__(UiPathRunner)
    rows = [
        {"text": "允许「示例门店」发送通知", "x": 30, "y": 40, "width": 180, "height": 24, "confidence": 0.9},
        {"text": "不允许", "x": 40, "y": 90, "width": 70, "height": 28, "confidence": 0.9},
        {"text": "允许", "x": 130, "y": 90, "width": 70, "height": 28, "confidence": 0.9},
    ]
    decision = runner._local_prepare_action(((240, 160), rows), 1, True)
    assert decision is not None
    assert decision.target_label == "不允许"
    assert decision.consent_required is False
    assert decision.x == pytest.approx(25)
    assert decision.y == pytest.approx(34.667, rel=1e-3)


def test_notification_permission_uses_enabled_allow_when_decline_missing() -> None:
    runner = object.__new__(UiPathRunner)
    rows = [
        {"text": "是否允许开启通知提醒", "x": 30, "y": 40, "width": 180, "height": 24, "confidence": 0.9},
        {"text": "允许", "x": 130, "y": 90, "width": 70, "height": 28, "confidence": 0.9},
    ]
    # 弹窗的目的只是关掉它：拒绝按钮不存在时，可用的允许按钮同样可以点。
    decision = runner._local_prepare_action(((240, 160), rows), 1, True)
    assert decision is not None
    assert decision.target_label == "允许"


def test_button_visual_state_uses_screen_baseline_for_theme_colors(tmp_path: Path) -> None:
    path = tmp_path / "frame.png"
    image = Image.new("RGB", (200, 120), "white")
    for x in (10, 50, 90, 130):
        image.paste((0, 0, 0), (x, 10, x + 30, 26))
    image.paste((235, 235, 235), (10, 60, 70, 80))
    image.paste((205, 205, 205), (18, 66, 62, 74))
    image.paste((40, 40, 40), (110, 66, 180, 74))
    image.save(path)
    # OCR 行坐标是 3 倍放大图上的坐标。
    rows = [
        {"text": "a", "x": 30, "y": 30, "width": 120, "height": 66, "confidence": .9},
        {"text": "b", "x": 150, "y": 30, "width": 120, "height": 66, "confidence": .9},
        {"text": "c", "x": 270, "y": 30, "width": 120, "height": 66, "confidence": .9},
        {"text": "d", "x": 390, "y": 30, "width": 120, "height": 66, "confidence": .9},
    ]
    baseline = UiPathRunner._button_visual_baseline(path, rows, (200, 120))
    assert baseline is not None

    disabled = UiPathRunner._button_visual_state(
        path, {"x": 10, "y": 60, "width": 60, "height": 20}, (200, 120), baseline)
    enabled = UiPathRunner._button_visual_state(
        path, {"x": 100, "y": 64, "width": 90, "height": 12}, (200, 120), baseline)

    assert disabled["available"] is False
    assert enabled["available"] is True


def test_local_roi_button_picks_expected_enabled_candidate(tmp_path: Path) -> None:
    from menu_capture.scene_agent import SceneDecision
    path = tmp_path / "frame.png"
    image = Image.new("RGB", (310, 1312), "white")
    image.paste((52, 120, 246), (120, 270, 180, 290))
    image.save(path)
    # ROI 裁剪为 (0,200)-(310,560) 后放大 4 倍；这里返回该 4x 图上的坐标。
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

    chosen = runner._local_button_candidates_near(path, decision)

    assert chosen is not None
    assert chosen["text"] == "确定"
    assert chosen["center_image_px"]["x"] == pytest.approx(150)
    assert chosen["center_image_px"]["y"] == pytest.approx(280)


def test_local_roi_button_refuses_ambiguous_candidates(tmp_path: Path) -> None:
    from menu_capture.scene_agent import SceneDecision
    path = tmp_path / "frame.png"
    image = Image.new("RGB", (310, 1312), "white")
    image.paste((52, 120, 246), (120, 270, 300, 290))
    image.save(path)
    rows = [
        {"text": "确定", "x": 480, "y": 280, "width": 240, "height": 80, "confidence": .9},
        {"text": "确定", "x": 600, "y": 280, "width": 240, "height": 80, "confidence": .9},
    ]
    runner = object.__new__(UiPathRunner)
    runner.ocr_reader = lambda _: rows
    runner._active_ocr_result = ((310, 1312), [])
    decision = SceneDecision(
        action="click", x=150, y=380, target_label="确定", reason="点击确定继续",
        expects_transition=True, overlay_visible=False, login_required=False,
        phone_required=False, consent_required=False, transaction_visible=False,
    )

    assert runner._local_button_candidates_near(path, decision) is None
    assert runner._last_target_resolution["reason"] == "ambiguous_button"


def test_button_visual_state_rejects_flat_grey_and_accepts_colored(tmp_path: Path) -> None:
    grey = tmp_path / "grey.png"
    Image.new("RGB", (120, 40), (238, 238, 238)).save(grey)
    state = UiPathRunner._button_visual_state(
        grey, {"x": 10, "y": 5, "width": 100, "height": 30}, (120, 40))
    assert state["available"] is False

    colored = tmp_path / "colored.png"
    image = Image.new("RGB", (120, 40), (255, 255, 255))
    for x in range(10, 110):
        for y in range(10, 30):
            image.putpixel((x, y), (52, 120, 246))
    image.save(colored)
    assert UiPathRunner._button_visual_state(
        colored, {"x": 10, "y": 10, "width": 100, "height": 20}, (120, 40))["available"] is True


def test_people_choice_uses_unique_3x_ocr_box_instead_of_model_coordinate(tmp_path: Path) -> None:
    runner, _, _, _, _, output_root = _setup(tmp_path)
    actor = FakeActor(tmp_path / "ipc", output_root)
    runner.transport = actor
    ocr_paths = []

    def fake_ocr(path: Path):
        ocr_paths.append(Path(path))
        with Image.open(path) as image:
            assert image.size == (720, 480)
        rows = [
            {"text": str(number), "x": 30 * number, "y": 30 * number, "width": 24, "height": 24, "confidence": 0.95}
            for number in range(1, 13)
        ]
        rows[0].update(x=300, y=240, width=30, height=30)
        return rows

    runner.ocr_reader = fake_ocr
    frame_path = output_root / "people-frame.png"
    _image(frame_path, size=(240, 160))
    frame = {"path": frame_path, "width": 240, "height": 160, "window_width": 120, "window_height": 80}
    decision = SceneDecision.model_validate(json.loads(scene_response(
        action="click", x=200, y=120, menu_region=None, overlay_visible=True,
        target_label="1", reason="选择配置的1人就餐人数", item_names_visible=[], prices_visible=[],
    )))
    result = runner._apply_scene_action(
        decision, frame, "recorded-miniapp-selector", {}, people_count=1,
    )
    assert result == "people_selected"
    assert actor.commands[-1]["x"] == pytest.approx(52.5)
    assert actor.commands[-1]["y"] == pytest.approx(42.5)
    assert len(ocr_paths) == 1 and not ocr_paths[0].exists()


def test_scene_agent_resolves_out_of_bounds_model_click_from_unique_exact_ocr_target(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    image_path = tmp_path / "scene.png"
    _image(image_path, size=(240, 160))
    scaled_paths = []

    def fake_ocr(path: Path):
        scaled_paths.append(Path(path))
        with Image.open(path) as image:
            assert image.size == (720, 480)
        return [{"text": " 确定 ", "x": 300, "y": 210, "width": 60, "height": 30, "confidence": 0.92}]

    runner.ocr_reader = fake_ocr
    model = FakeModel(responses=[scene_response(
        action="click", x=500, y=930, menu_region=None, overlay_visible=True,
        target_label="确定", item_names_visible=[], prices_visible=[],
    )])
    agent = SceneAgent(model, coordinate_resolver=lambda path, decision: runner._resolve_click_coordinates(path, decision, 1))
    decision = agent.decide(image_path, "人数确认")
    assert (decision.x, decision.y) == pytest.approx((110, 75))
    assert agent.last_coordinate_source == "local_target_resolver"
    assert len(scaled_paths) == 1 and not scaled_paths[0].exists()


def test_scene_agent_keeps_ambiguous_or_unresolved_model_coordinates_for_bounds_rejection(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    image_path = tmp_path / "scene.png"
    _image(image_path, size=(240, 160))
    runner.ocr_reader = lambda path: [
        {"text": "确定", "x": 100, "y": 100, "width": 40, "height": 20, "confidence": 0.95},
        {"text": " 确 定 ", "x": 300, "y": 200, "width": 40, "height": 20, "confidence": 0.96},
    ]
    response = scene_response(action="click", x=500, y=930, menu_region=None, overlay_visible=True,
                              target_label="确定", item_names_visible=[], prices_visible=[])
    resolver = lambda path, decision: runner._resolve_click_coordinates(path, decision, 1)
    agent = SceneAgent(FakeModel(responses=[response]), coordinate_resolver=resolver)
    with pytest.raises(SceneError, match="超出当前截图"):
        agent.decide(image_path, "人数确认")

    runner.ocr_reader = None
    agent = SceneAgent(FakeModel(responses=[response]), coordinate_resolver=resolver)
    with pytest.raises(SceneError, match="超出当前截图"):
        agent.decide(image_path, "人数确认")

    valid_response = scene_response(action="click", x=110, y=75, menu_region=None, overlay_visible=True,
                                    target_label="确定", item_names_visible=[], prices_visible=[])
    agent = SceneAgent(FakeModel(responses=[valid_response]), coordinate_resolver=resolver)
    decision = agent.decide(image_path, "人数确认")
    assert (decision.x, decision.y) == (110, 75)
    assert agent.last_coordinate_source == "model"


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


def test_ready_region_resolver_uses_exact_menu_and_category_ocr_anchors(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    runner.ocr_reader = lambda path: _menu_anchor_ocr_rows()
    image_path = tmp_path / "menu.png"
    _image(image_path, size=(414, 780))
    response = json.loads(scene_response(
        action="ready", menu_region={"x": 215, "y": 360, "width": 230, "height": 450},
        categories=["头牌肉串", "第二香串", "新品推荐"],
        item_names_visible=["青梅羊排串（串）"], prices_visible=["3.90 元"],
    ))
    agent = SceneAgent(
        FakeModel(responses=[json.dumps(response, ensure_ascii=False)]),
        region_resolver=runner._resolve_ready_region,
    )
    decision = agent.decide(image_path, "菜单准备")
    assert agent.last_region_source == "local_ocr_menu_anchors"
    assert decision.menu_region.x == pytest.approx(79)
    assert decision.menu_region.y == pytest.approx(340)
    assert decision.menu_region.y > 335  # 跑马灯结束后再开始滚动ROI。
    assert decision.menu_region.y + decision.menu_region.height < 764
    assert decision.menu_region.x + decision.menu_region.width <= 414


def test_readonly_menu_anchors_promote_model_scroll_to_ready(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    runner.ocr_reader = lambda path: _menu_anchor_ocr_rows()
    image_path = tmp_path / "prepare.png"
    _image(image_path, size=(414, 780))
    response = json.loads(scene_response(
        action="scroll", x=500, y=930, pixels=600, menu_region=None,
        target_label="", categories=["头牌肉串", "第二香串", "新品推荐"],
        item_names_visible=["青梅羊排串（串）"], prices_visible=["3.9"],
    ))
    agent = SceneAgent(
        FakeModel(responses=[json.dumps(response, ensure_ascii=False)]),
        region_resolver=runner._resolve_ready_region,
    )
    decision = agent.decide(image_path, "菜单准备")
    assert decision.action == "ready"
    assert decision.x is None and decision.y is None and decision.pixels == 0
    assert decision.item_names_visible == ["青梅羊排串(串)"]
    assert decision.prices_visible == ["¥3.9/串"]
    assert decision.categories[:3] == ["头牌肉串", "第二香串", "新品推荐"]
    assert agent.last_region_source == "local_ocr_menu_anchors"


def test_model_empty_evidence_uses_store_verified_local_menu_template(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    runner.ocr_reader = lambda path: _menu_anchor_ocr_rows()
    image_path = tmp_path / "prepare.png"
    _image(image_path, size=(414, 780))
    response = json.loads(scene_response(
        action="scroll", x=9999, y=9999, pixels=600, menu_region=None,
        target_label="", categories=[], item_names_visible=[], prices_visible=[],
        observed_store_text=[],
    ))
    source_name = "ZA0070005＿老胖串店＿济南市＿老胖串店中央厨房"
    agent = SceneAgent(
        FakeModel(responses=[json.dumps(response, ensure_ascii=False)]),
        region_resolver=lambda path, decision: runner._resolve_ready_region(path, decision, source_name),
    )
    decision = agent.decide(image_path, "菜单准备")
    assert decision.action == "ready"
    assert decision.observed_store_text == ["老胖串店龙湖店"]
    assert decision.categories == ["头牌肉串", "第二香串", "精酿鲜啤", "原创烤串", "精选烤串"]
    assert decision.item_names_visible == ["青梅羊排串(串)"]
    assert decision.prices_visible == ["¥3.9/串"]
    assert agent.last_region_source == "local_ocr_menu_anchors"


def test_local_menu_preflight_rejects_dimmed_overlay_even_with_same_ocr(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    runner.ocr_reader = lambda path: _menu_anchor_ocr_rows()
    path = tmp_path / "menu.png"
    source_name = "ZA0070005＿老胖串店＿济南市＿老胖串店中央厨房"
    _image(path, "white", size=(414, 780))
    assert runner._local_ready_preflight(path, source_name).ready_evidence_ok
    _image(path, "#707070", size=(414, 780))
    assert runner._local_ready_preflight(path, source_name) is None


@pytest.mark.parametrize("missing", ["brand", "bottom_navigation"])
def test_model_empty_evidence_does_not_use_unverified_local_template(tmp_path: Path, missing: str) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    rows = _menu_anchor_ocr_rows()
    if missing == "brand":
        rows = [row for row in rows if row["text"] != "老胖串店龙湖店"]
    else:
        rows = [row for row in rows if row["text"] not in {"首页", "扫码点餐", "我的"}]
    runner.ocr_reader = lambda path: rows
    image_path = tmp_path / "prepare.png"
    _image(image_path, size=(414, 780))
    response = json.loads(scene_response(
        action="scroll", x=9999, y=9999, pixels=600, menu_region=None,
        target_label="", categories=[], item_names_visible=[], prices_visible=[], observed_store_text=[],
    ))
    source_name = "ZA0070005＿老胖串店＿济南市＿老胖串店中央厨房"
    agent = SceneAgent(
        FakeModel(responses=[json.dumps(response, ensure_ascii=False)]),
        region_resolver=lambda path, decision: runner._resolve_ready_region(path, decision, source_name),
    )
    with pytest.raises(SceneError, match="超出当前截图"):
        agent.decide(image_path, "菜单准备")
    assert agent.last_region_source == "model"


@pytest.mark.parametrize("case", ["one_category", "different_dish"])
def test_ready_region_resolver_does_not_guess_without_strong_matching_anchors(tmp_path: Path, case: str) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    rows = _menu_anchor_ocr_rows()
    if case == "one_category":
        rows = [row for row in rows if row["text"] != "第二香串" and row["text"] != "新品推荐"]
    else:
        rows = [row for row in rows if row["text"] != "青梅羊排串（串）"]
        rows.append({"text": "香辣牛肉", "x": 324, "y": 1872, "width": 300, "height": 48, "confidence": 0.99})
    runner.ocr_reader = lambda path: rows
    image_path = tmp_path / "menu.png"
    _image(image_path, size=(414, 780))
    response = json.loads(scene_response(
        action="ready", menu_region={"x": 215, "y": 360, "width": 230, "height": 450},
        categories=["头牌肉串", "第二香串", "新品推荐"],
        item_names_visible=["青梅羊排串（串）"], prices_visible=["3.9"],
    ))
    agent = SceneAgent(
        FakeModel(responses=[json.dumps(response, ensure_ascii=False)]),
        region_resolver=runner._resolve_ready_region,
    )
    with pytest.raises(SceneError, match="菜单区域无效"):
        agent.decide(image_path, "菜单准备")
    assert agent.last_region_source == "model"
    inspection = image_path.parent / "scene-inspection-menu.json"
    assert inspection.is_file()
    assert "reason" not in json.loads(inspection.read_text(encoding="utf-8"))


@pytest.mark.parametrize("blocked", ["overlay", "login", "blocked"])
def test_readonly_menu_promotion_does_not_override_blocked_or_overlay_flags(tmp_path: Path, blocked: str) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    runner.ocr_reader = lambda path: _menu_anchor_ocr_rows()
    image_path = tmp_path / "prepare.png"
    _image(image_path, size=(414, 780))
    values = {
        "action": "click", "x": 500, "y": 930, "menu_region": None,
        "categories": ["头牌肉串", "第二香串"],
        "item_names_visible": ["青梅羊排串（串）"], "prices_visible": ["3.9"],
        "observed_store_text": ["Shop A"],
    }
    if blocked == "overlay":
        values.update(overlay_visible=True, target_label="点餐")
    elif blocked == "login":
        values.update(login_required=True, target_label="微信登录")
    else:
        values.update(action="blocked", target_label="")
    decision = SceneDecision.model_validate(json.loads(scene_response(**values)))
    resolved = runner._resolve_ready_region(image_path, decision)
    assert resolved.action == decision.action
    assert resolved.menu_region == decision.menu_region


def test_same_ready_uses_region_iou_and_normalized_menu_evidence() -> None:
    left = SceneDecision.model_validate(json.loads(scene_response(
        action="ready", menu_region={"x": 70, "y": 300, "width": 330, "height": 450},
        item_names_visible=["青梅羊排串（串）"], prices_visible=["3.90 元"],
    )))
    right = SceneDecision.model_validate(json.loads(scene_response(
        action="ready", menu_region={"x": 95, "y": 320, "width": 300, "height": 420},
        item_names_visible=["青梅羊排串(串)"], prices_visible=["¥3.9"],
    )))
    assert UiPathRunner._same_ready(left, right) is True
    different_menu = right.model_copy(update={"item_names_visible": ["香辣牛肉"]})
    assert UiPathRunner._same_ready(left, different_menu) is False


def test_scene_validation_error_reports_safe_field_paths_without_raw_values(tmp_path: Path) -> None:
    image_path = tmp_path / "scene.png"
    _image(image_path, size=(240, 160))
    bad = json.loads(scene_response())
    bad["selected_people_count"] = 99
    agent = SceneAgent(FakeModel(responses=[json.dumps(bad)]))
    with pytest.raises(SceneError, match=r"selected_people_count\(less_than_equal\)") as exc:
        agent.decide(image_path, "schema test")
    assert "99" not in str(exc.value)


def test_prepare_keeps_verified_identity_through_people_dialog_and_confirms_count(tmp_path: Path) -> None:
    runner, _, selected, _, _, output_root = _setup(tmp_path)
    actor = FakeActor(tmp_path / "ipc", output_root)
    runner.transport = actor

    def decision(action="ready", **fields):
        return SceneDecision.model_validate(json.loads(scene_response(action=action, **fields)))

    decisions = [
        decision(action="click", x=100, y=80, menu_region=None, overlay_visible=True,
                 target_label="点餐", reason="门店品牌Shop A匹配来源，请进入菜单"),
        decision(action="click", x=100, y=80, menu_region=None, overlay_visible=True,
                 target_label="1", reason="人数弹窗中选择配置的1人", observed_store_text=[],
                 item_names_visible=[], prices_visible=[]),
        decision(action="click", x=100, y=140, menu_region=None, overlay_visible=True,
                 target_label="确定", reason="确认刚选择的1人就餐人数", selected_people_count=1, observed_store_text=[],
                 item_names_visible=[], prices_visible=[]),
        decision(observed_store_text=["Shop A"]),
        decision(observed_store_text=["Shop A"]),
    ]

    class Agent:
        def decide(self, image_path, phase, history, candidates=None):
            return decisions.pop(0)

    output_dir = output_root / "prepare-test"
    output_dir.mkdir(parents=True)
    frame = runner._screenshot(output_dir, "recorded-miniapp-selector", "initial.png")
    region, _, _, identity_evidence = runner._prepare_until_ready(
        selected, Agent(), frame, "recorded-miniapp-selector", "准备测试",
        runner.configs.load(), output_dir, {"scroll_units": 2},
    )
    click_commands = [command for command in actor.commands if command.get("action") == "click"]
    assert len(click_commands) == 3
    assert region.width > 0
    assert identity_evidence == ["Shop A"]
    summaries = sorted((output_dir / "evidence").glob("decision-*.json"))
    assert len(summaries) == 3
    assert all(json.loads(path.read_text(encoding="utf-8"))["reason_saved"] is False for path in summaries)


@pytest.mark.parametrize("observed", [[], ["Shop B"]])
def test_prepare_does_not_block_on_missing_or_different_store_name(tmp_path: Path, observed: list[str]) -> None:
    runner, _, selected, _, _, output_root = _setup(tmp_path)
    runner.transport = FakeActor(tmp_path / "ipc", output_root)
    decision = SceneDecision.model_validate_json(scene_response(observed_store_text=observed))
    class Agent:
        def decide(self, *_args):
            return decision
    output_dir = output_root / "store-name"
    output_dir.mkdir(parents=True)
    frame = runner._screenshot(output_dir, "recorded-miniapp-selector", "initial.png")
    _, _, _, identity_evidence = runner._prepare_until_ready(
        selected, Agent(), frame, "recorded-miniapp-selector", "准备测试",
        runner.configs.load(), output_dir, {"scroll_units": 2},
    )
    assert identity_evidence == observed


def test_model_can_click_phone_authorization(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    actor = FakeActor(tmp_path / "ipc", tmp_path / "output")
    runner.transport = actor
    phone = SceneDecision.model_validate_json(scene_response(action="click", x=70, y=80, menu_region=None,
        phone_required=True, target_label="手机号快捷授权"))
    runner._apply_scene_action(phone, {"width": 240, "height": 160}, "recorded-miniapp-selector", {}, auto_authorize=True)
    assert actor.commands[-1]["action"] == "click"


def test_transaction_page_never_clicks_even_with_auto_authorize(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    actor = FakeActor(tmp_path / "ipc", tmp_path / "output")
    runner.transport = actor
    decision = SceneDecision.model_validate(json.loads(scene_response(
        action="click", x=70, y=80, menu_region=None, overlay_visible=True,
        consent_required=True, transaction_visible=True, target_label="同意并支付", reason="订单支付确认",
    )))
    with pytest.raises(NeedsReview, match="交易或支付"):
        runner._apply_scene_action(decision, {"width": 240, "height": 160, "window_width": 120, "window_height": 80}, "recorded-miniapp-selector", {}, auto_authorize=True)
    assert not any(command.get("action") == "click" for command in actor.commands)


def test_auto_authorize_consent_can_be_handled_before_store_name_is_visible(tmp_path: Path) -> None:
    consent = scene_response(
        action="click", x=100, y=80, menu_region=None, overlay_visible=True,
        consent_required=True, target_label="同意并继续", reason="这是进入菜单所需的隐私协议同意",
        observed_store_text=[], item_names_visible=[], prices_visible=[],
    )
    responses = [consent, scene_response(observed_store_text=["Shop A"]), scene_response(observed_store_text=["Shop A"])]
    runner, store, selected, _, _, _ = _setup(tmp_path, model_factory=lambda config, key: FakeModel(responses=responses))
    _set_auto_authorize(runner, True)
    runner.start()
    runner.thread.join(timeout=5)
    assert store.get(selected["id"])["status"] == "captured"
    assert any(command.get("action") == "click" for command in runner.transport.commands)


def test_authorization_unavailable_closes_and_skips_to_next_selected_automatic_task(tmp_path: Path) -> None:
    runner, store, first, second, profile_path, _ = _setup(tmp_path, mode="automatic")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["selected_task_ids"] = [first["id"], second["id"]]
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _set_auto_authorize(runner, True)
    state = {"source_name": "", "task_id": ""}
    by_source = {task["source_path"]: task for task in store.tasks()}

    class LoginModel(FakeModel):
        def _request(self, image, prompt):
            self.calls += 1
            if state["source_name"] == "Shop A":
                return scene_response(
                    action="blocked", menu_region=None, login_required=True,
                    reason="扫码登录或验证码不可自动完成", observed_store_text=[],
                    item_names_visible=[], prices_visible=[],
                )
            return scene_response(observed_store_text=[state["source_name"]])

    def prepare(path: Path) -> None:
        task = by_source[str(path)]
        state.update(source_name=task["source_name"], task_id=task["id"])

    runner.model_factory = lambda config, key: LoginModel()
    runner.clipboard_preparer = prepare
    runner.ocr_reader = lambda path: [
        {"text": "文件传输助手", "x": 200, "y": 5},
        {"text": state["task_id"], "x": 150, "y": 40, "height": 10},
    ]
    runner.qr_detector = lambda path: [{"x": 100, "y": 80, "width": 30, "height": 30}]

    runner.start()
    actor = runner.transport
    runner.thread.join(timeout=10)
    assert store.get(first["id"])["status"] == "needs_review"
    assert store.get(first["id"])["stage"] == "authorization_unavailable"
    assert store.get(second["id"])["status"] == "captured"
    assert "扫码登录" not in store.get(first["id"])["message"]
    close_commands = [command for command in actor.commands if command.get("action") == "key" and command.get("key") == profile["close_key"]]
    assert len(close_commands) == 2
    assert all("Shop " in command.get("selector", "") for command in close_commands)


def test_task_deadline_starts_after_manual_uipath_handshake(tmp_path: Path) -> None:
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
    runner.start()
    assert reached_handshake.wait(timeout=2)
    assert runner._deadline == 0
    release_handshake.set()
    runner.thread.join(timeout=5)
    assert not runner.thread.is_alive()
    assert actor_holder["actor"].run_timeouts[0] == 300
    assert store.get(selected["id"])["status"] == "captured"


def test_pause_after_model_returns_click_finishes_current_store(tmp_path: Path) -> None:
    callbacks = {}

    def factory(config, key):
        return FakeModel(callback=lambda: callbacks["runner"].pause(), responses=[scene_response(
            action="click", x=90, y=70, menu_region=None,
            target_label="菜单", item_names_visible=[], prices_visible=[],
        )])

    runner, store, selected, _, _, _ = _setup(tmp_path, model_factory=factory)
    callbacks["runner"] = runner
    runner.start()
    runner.thread.join(timeout=5)

    commands = runner.transport.commands
    assert any(command.get("action") == "click" for command in commands)
    assert store.get(selected["id"])["status"] == "captured"
    assert any(command.get("action") == "stop" for command in commands)


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
    model = FakeModel()
    pages = runner._capture_menu("miniapp", frame, Region(x=20, y=20, width=180, height=120), settings, profile, output)
    assert len(pages) == 8
    assert model.calls == 0
    ups = [command for command in actor.commands if command.get("action") == "scroll" and command.get("direction") == "Up"]
    assert not ups
    assert len([c for c in actor.commands if c.get("action") == "scroll_capture" and c.get("direction") == "Down"]) == 8


def test_automatic_two_task_batch_reuses_one_actor_and_closes_between_stores(tmp_path: Path) -> None:
    runner, store, first, second, profile_path, output_root = _setup(tmp_path, mode="automatic")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["selected_task_ids"] = [second["id"], first["id"]]
    profile["max_tasks"] = 1  # 旧批次上限不能截断显式选择列表。
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    state = {"source_name": "", "task_id": ""}
    by_source = {task["source_path"]: task for task in store.tasks()}

    class IdentityModel(FakeModel):
        def _request(self, image, prompt):
            self.calls += 1
            return scene_response(observed_store_text=[state["source_name"]])

    def prepare_clipboard(path: Path) -> None:
        task = by_source[str(path)]
        state.update(source_name=task["source_name"], task_id=task["id"])

    runner.model_factory = lambda config, key: IdentityModel()
    runner.clipboard_preparer = prepare_clipboard
    runner.ocr_reader = lambda path: [
        {"text": "文件传输助手", "x": 200, "y": 5},
        {"text": state["task_id"], "x": 150, "y": 40, "height": 10},
    ]
    runner.qr_detector = lambda path: [{"x": 100, "y": 80, "width": 30, "height": 30}]

    ready = runner.readiness()
    assert ready["ready"] is True
    assert ready["experimental_batch"] is True
    start_result = runner.start()
    actor = runner.transport
    runner.thread.join(timeout=10)

    assert not runner.thread.is_alive()
    assert store.get(first["id"])["status"] == "captured"
    assert store.get(second["id"])["status"] == "captured"
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
    sent_labels = [command.get("text", "") for command in actor.commands if command.get("action") == "type_text"]
    assert second["id"] in sent_labels[0] and first["id"] in sent_labels[1]
    close_commands = [command for command in actor.commands if command.get("action") == "key" and command.get("key") == profile["close_key"]]
    assert len(close_commands) == 2
    assert all("Shop " in command.get("selector", "") for command in close_commands)
    assert len([command for command in actor.commands if command.get("action") == "noop"]) >= 1
    actions = [command["action"] for command in actor.commands]
    double_click_index = actions.index("double_click")
    preview_screenshot_index = next(i for i, command in enumerate(actor.commands) if command.get("path", "").endswith("image-preview.png"))
    recognize_click_index = next(i for i, command in enumerate(actor.commands) if command.get("action") == "click" and command.get("selector") == "recorded-preview-selector")
    miniapp_discover_index = actions.index("discover", double_click_index + 1)
    assert double_click_index < preview_screenshot_index < recognize_click_index < miniapp_discover_index
    recognize_click = actor.commands[recognize_click_index]
    assert (recognize_click["x"], recognize_click["y"]) == (539, 20)


def test_automatic_batch_continues_when_store_name_differs(tmp_path: Path) -> None:
    runner, store, first, second, profile_path, _ = _setup(tmp_path, mode="automatic")
    input_root = Path(runner.configs.load().input_dir)
    _image(input_root / "Shop C" / "menu-c.png", "blue")
    store.scan(input_root)
    third = next(task for task in store.tasks() if task["source_name"] == "Shop C")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["selected_task_ids"] = [third["id"], first["id"]]
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    state = {"source_name": "", "observed": "", "task_id": ""}
    by_source = {task["source_path"]: task for task in store.tasks()}

    class IdentityModel(FakeModel):
        def _request(self, image, prompt):
            self.calls += 1
            return scene_response(observed_store_text=[state["observed"]])

    def prepare_clipboard(path: Path) -> None:
        task = by_source[str(path)]
        state.update(
            source_name=task["source_name"],
            observed="Wrong Brand" if task["id"] == second["id"] else task["source_name"],
            task_id=task["id"],
        )

    runner.model_factory = lambda config, key: IdentityModel()
    runner.clipboard_preparer = prepare_clipboard
    runner.ocr_reader = lambda path: [
        {"text": "文件传输助手", "x": 200, "y": 5},
        {"text": state["task_id"], "x": 150, "y": 40, "height": 10},
    ]
    runner.qr_detector = lambda path: [{"x": 100, "y": 80, "width": 30, "height": 30}]
    runner.start()
    runner.thread.join(timeout=10)

    assert store.get(first["id"])["status"] == "captured"
    assert store.get(second["id"])["status"] == "pending"
    assert store.get(third["id"])["status"] == "captured"
    sent_labels = [command.get("text", "") for command in runner.transport.commands if command.get("action") == "type_text"]
    assert third["id"] in sent_labels[0] and first["id"] in sent_labels[1]


def test_automatic_publish_replaces_only_the_selected_store_and_keeps_only_menu_pages(tmp_path: Path) -> None:
    runner, _, selected, other, _, output_root = _setup(tmp_path, mode="automatic")
    selected_dir = output_root / selected["source_name"]
    other_dir = output_root / other["source_name"]
    selected_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    (selected_dir / "old-page.png").write_bytes(b"old")
    (other_dir / "keep.png").write_bytes(b"keep")

    staging = runner._automatic_staging_dir(output_root, selected, "attempt-test")
    staging.mkdir(parents=True)
    _image(staging / "page-0001.png")
    _image(staging / "page-0002.png", "gray")
    _image(staging / "evidence" / "initial.png")
    (staging / "snapshotproof.json").write_text("{}", encoding="utf-8")
    (staging / "result.json").write_text("{}", encoding="utf-8")

    published = runner._publish_automatic_output(output_root, selected, staging)

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
    runner.ocr_reader = lambda path: []
    runner.qr_detector = lambda path: []

    def fail_capture(*args, **kwargs):
        raise NeedsReview("模拟采集失败")

    runner._run_task = fail_capture
    runner.start()
    runner.thread.join(timeout=5)

    assert not runner.thread.is_alive()
    assert store.get(selected["id"])["status"] == "needs_review"
    assert Path(store.get(selected["id"])["output_dir"]) == destination
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


def _consent_decision() -> SceneDecision:
    return SceneDecision(
        action="click", reason="勾选隐私协议", x=25, y=476,
        target_label="我已了解以下规则并同意所有政策",
        target_box=Region(x=14, y=470, width=160, height=14),
        overlay_visible=True, login_required=False, phone_required=False,
        consent_required=True, transaction_visible=False,
    )


def test_page_change_detects_small_checkbox_change_near_click_point() -> None:
    before = Image.new("RGB", (310, 1010), "white")
    after = before.copy()
    # 真实复选框勾选约 10×10 px，且只是浅灰描边变化，全图平均差远低于 5%。
    for x in range(20, 30):
        for y in range(471, 481):
            after.putpixel((x, y), (200, 200, 200))

    runner = object.__new__(UiPathRunner)
    changed, detail = runner._evaluate_page_change(
        before, after, _consent_decision(), {"x": 25, "y": 476})

    assert detail["global_difference"] < 0.05
    assert changed is True
    assert detail["reason"] == "local_target_change"
    assert detail["roi_ratio"] >= UiPathRunner.ROI_CHANGE_RATIO_THRESHOLD


def test_page_change_reports_unchanged_when_nothing_moves() -> None:
    frame = Image.new("RGB", (310, 1010), "white")

    runner = object.__new__(UiPathRunner)
    changed, detail = runner._evaluate_page_change(
        frame, frame.copy(), _consent_decision(), {"x": 25, "y": 476})

    assert changed is False
    assert detail["reason"] == "no_visible_change"
    assert detail["roi_ratio"] == 0


def test_page_change_still_detects_full_page_jump() -> None:
    before = Image.new("RGB", (310, 1010), "white")
    after = Image.new("RGB", (310, 1010), "black")

    runner = object.__new__(UiPathRunner)
    changed, detail = runner._evaluate_page_change(
        before, after, _consent_decision(), {"x": 25, "y": 476})

    assert changed is True
    assert detail["reason"] == "global_change"


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


def test_ready_preflight_accepts_strong_menu_without_store_name(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    rows = [row for row in _menu_anchor_ocr_rows() if row["text"] != "老胖串店龙湖店"]
    rows.append({"text": "香辣牛肉", "x": 108 * 3, "y": 560 * 3,
                 "width": 100 * 3, "height": 19 * 3, "confidence": 0.98})
    rows.append({"text": "¥4.9", "x": 110 * 3, "y": 600 * 3,
                 "width": 40 * 3, "height": 17 * 3, "confidence": 0.9})
    runner.ocr_reader = lambda path: rows
    path = tmp_path / "menu.png"
    _image(path, "white", size=(414, 780))
    source_name = "ZA0070005＿老胖串店＿济南市＿老胖串店中央厨房"

    decision = runner._local_ready_preflight(path, source_name)

    assert decision is not None
    assert decision.action == "ready"
    assert "门店标识未确认" in decision.reason
    assert decision.observed_store_text == []


def test_local_rule_ranks_same_text_candidates_and_skips_consumed_point(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    _set_auto_authorize(runner, True)
    rows = [
        {"text": "请阅读并同意《用户协议》", "x": 90, "y": 300, "width": 600, "height": 45, "confidence": 0.9},
        {"text": "同意", "x": 90, "y": 900, "width": 180, "height": 70, "confidence": 0.92},
        {"text": "同意", "x": 300, "y": 1100, "width": 150, "height": 60, "confidence": 0.96},
    ]

    first = runner._local_prepare_action(((414, 780), rows), 1, True)
    assert first is not None and first.target_label == "同意"
    assert first.x == pytest.approx((300 + 75) / 3)
    assert len(runner._last_target_resolution["alternatives"]) == 2

    runner._consumed_local_points.append((first.x, first.y))
    second = runner._local_prepare_action(((414, 780), rows), 1, True)

    assert second is not None
    assert (second.x, second.y) != (first.x, first.y)
    assert runner._last_target_resolution["method"] == "local_exact_button_ranked"


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


def test_capture_after_action_falls_back_to_uipath_screenshot(tmp_path):
    runner = object.__new__(UiPathRunner)
    runner._fast_capture_enabled = True
    runner.transport = FileRobotTransport(tmp_path / "ipc")
    runner._fast_window_capture = lambda *args, **kwargs: None
    calls = []

    def fake_screenshot(output_dir, selector, name, desktop=False):
        calls.append(name)
        path = Path(output_dir) / name
        _image(path, "white")
        return {"path": str(path)}

    runner._screenshot = fake_screenshot
    runner._send = lambda payload: None
    before = tmp_path / "before.png"
    _image(before, "white")

    frame = runner._capture_after_action(tmp_path, "sel", "after.png", before, settle_seconds=0)

    assert calls[0] == "after.png"
    assert Path(frame["path"]).exists()


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


def test_dual_column_menu_is_ready_without_store_name(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    path = tmp_path / "menu.png"
    _image(path, "white", size=(310, 1011))

    def row(text: str, x: int, y: int, width: int, height: int, confidence: float = 0.9) -> dict:
        return {"text": text, "x": x, "y": y, "width": width, "height": height,
                "confidence": confidence}

    rows = [
        # 左侧分类列（3x 坐标系，x < 0.3 * 310 * 3）
        row("热菜", 10, 360, 60, 40), row("凉菜", 10, 660, 60, 40),
        row("主食", 10, 960, 60, 40),
        # 窄窗口常见的“菜名 + 价格”合并行
        row("招牌牛肉¥58", 90, 500, 520, 40), row("酸菜鱼¥48", 90, 700, 460, 40),
        row("手撕包菜¥28", 90, 900, 520, 40),
    ]
    runner.ocr_reader = lambda _path: rows

    decision = runner._local_ready_preflight(path, "测试门店", ((310, 1011), rows))

    assert decision is not None
    assert decision.action == "ready"
    assert "双列菜单结构" in decision.reason
    assert decision.observed_store_text == []
    assert len(decision.item_names_visible) >= 3
    assert len(decision.prices_visible) >= 3


def test_store_header_identity_catches_platform_remembered_store():
    # 共用点餐平台小程序会沿用上一次门店；顶部门店名必须作为身份证据。
    def rows(header):
        return [{"text": header, "y": 40, "height": 20},
                {"text": "招牌菜", "y": 900, "height": 20}]

    assert UiPathRunner._identity_mismatch(
        "E00682＿骏丰酒楼＿北京市＿骏丰酒楼（黄编店）",
        UiPathRunner._store_header_values(rows("东来顺（双井店）〉"))) == "页面显示 东来顺(双井店)〉"
    assert UiPathRunner._identity_mismatch(
        "E00656＿茶记餐厅＿广州市＿茶记餐厅（市桥店）",
        UiPathRunner._store_header_values(rows("茶记餐厅（祈福店）＞"))) == "页面显示 祈福店"
    assert UiPathRunner._identity_mismatch(
        "E01447＿金掌勺＿北京市＿金掌勺铁锅炖（总店）",
        UiPathRunner._store_header_values(rows("金掌勺-太阳宫凯德店"))) == "页面显示 太阳宫凯德店"
    # 门店一致（含分店一致）时不能误拦。
    assert UiPathRunner._identity_mismatch(
        "E00682＿骏丰酒楼＿北京市＿骏丰酒楼（黄编店）",
        UiPathRunner._store_header_values(rows("骏丰酒楼（黄编店）〉"))) is None


def test_verify_captured_menu_flags_order_detail_as_retry(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    output = tmp_path / "verify-order"
    output.mkdir()
    _image(output / "page-0001.png")
    runner.ocr_reader = lambda path: [{"text": "订单详情"}, {"text": "下单成功，坐等开吃"},
                                      {"text": "订单编号 123"}]
    with pytest.raises(NeedsRetry) as error:
        runner._verify_captured_menu(
            {"source_name": "E00001＿测试店＿上海市＿测试店（总店）"}, ["page-0001.png"], output)
    assert error.value.code == "existing_order"


def test_verify_captured_menu_rejects_two_page_platform_overlay(tmp_path: Path) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    output = tmp_path / "verify-overlay"
    output.mkdir()
    _image(output / "page-0001.png")
    _image(output / "page-0002.png")
    rows = {
        output / "page-0001.png": [{"text": "小伙伴已在美团在线点菜"}, {"text": "¥88/份"}],
        output / "page-0002.png": [{"text": "¥68/份"}],
    }
    runner.ocr_reader = lambda path: rows[Path(path)]
    with pytest.raises(NeedsReview) as error:
        runner._verify_captured_menu(
            {"source_name": "E00397＿郭姐＿上海市＿郭姐（柳州路店）"},
            ["page-0001.png", "page-0002.png"], output)
    assert error.value.code == "overlay_blocked"


def test_clear_scroll_overlay_ignores_header_close_icon(tmp_path: Path, monkeypatch) -> None:
    runner, _, _, _, _, _ = _setup(tmp_path)
    frame_path = tmp_path / "frame.png"
    _image(frame_path, size=(310, 1310))
    frame = {"path": str(frame_path), "width": 310, "height": 1310,
             "window_width": 240, "window_height": 1014}
    sent: list[dict] = []
    runner._send = lambda command: sent.append(command) or {}

    monkeypatch.setattr("menu_capture.uipath_runner.locate_close_icon",
                        lambda path, x=None, y=None: {"center_image_px": {"x": 300, "y": 30},
                                                      "score": 0.95})
    assert runner._clear_scroll_overlay(frame, "miniapp", tmp_path) is False
    assert sent == []

    monkeypatch.setattr("menu_capture.uipath_runner.locate_close_icon",
                        lambda path, x=None, y=None: {"center_image_px": {"x": 155, "y": 655},
                                                      "score": 0.95})
    attempted: set[tuple[int, int]] = set()
    assert runner._clear_scroll_overlay(frame, "miniapp", tmp_path, attempted) is True
    assert sent[0]["action"] == "click"
    assert sent[0]["x"] == pytest.approx(120.0)
    assert sent[0]["y"] == pytest.approx(507.0)
    # 同一坐标只点一次，避免把菜单里的圆形图标当关闭键反复点。
    sent.clear()
    assert runner._clear_scroll_overlay(frame, "miniapp", tmp_path, attempted) is False
    assert sent == []
