from __future__ import annotations

import json
import time
from pathlib import Path

import menu_capture.uipath_runner as uipath_runner_module
from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.config import ConfigStore
from menu_capture.server import create_app
from menu_capture.store import TaskStore
from menu_capture.uipath_runner import UiPathRunner


def _published_task(root: Path, snapshot_id: str, shop_name: str) -> Path:
    snapshot = root / "materials" / "published" / snapshot_id
    source = snapshot / shop_name / "二维码.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (24, 18), "white").save(source)
    (snapshot / ".materials-snapshot.json").write_text(
        json.dumps({"snapshot_id": snapshot_id}), encoding="utf-8"
    )
    return snapshot


def test_batch_trial_endpoint_passes_published_task_ids_without_touching_official_state(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    first_snapshot = _published_task(tmp_path, "a" * 64, "测试店甲")
    second_snapshot = _published_task(tmp_path, "b" * 64, "测试店乙")
    app.state.store.scan(first_snapshot)
    app.state.store.scan(second_snapshot)
    tasks = app.state.store.tasks()
    task_ids = [task["id"] for task in tasks]

    # A prior blocked production run must remain untouched by an isolated trial.
    app.state.store.create_run("legacy-blocked", "queue", ["legacy-task"])
    with app.state.store.connect() as db:
        db.execute("UPDATE runs SET status='blocked',message='legacy failure' WHERE id='legacy-blocked'")
    before_run = app.state.store.get_run("legacy-blocked")
    before_tasks = [app.state.store.get(task_id) for task_id in task_ids]
    received = {}

    def fake_start(**kwargs):
        received.update(kwargs)
        return {"ok": True, "running": True, "task_ids": kwargs["trial_task_ids"]}

    monkeypatch.setattr(app.state.runner, "start", fake_start)
    client = TestClient(app, base_url="http://127.0.0.1")
    response = client.post(
        "/api/ui-tars/trial",
        json={"task_ids": task_ids},
        headers={"X-Capture-Token": app.state.token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["task_ids"] == task_ids
    assert received == {"ui_tars_trial": True, "trial_task_ids": task_ids}
    assert [app.state.store.get(task_id) for task_id in task_ids] == before_tasks
    assert app.state.store.get_run("legacy-blocked") == before_run
    assert app.state.store.latest_run()["id"] == "legacy-blocked"


def test_batch_trial_stops_before_next_store_when_safe_return_is_not_confirmed(tmp_path, monkeypatch):
    input_dir = tmp_path / "configured-input"
    input_dir.mkdir()
    output_dir = tmp_path / "official-output"
    configs = ConfigStore(tmp_path / "settings")
    configs.save({
        "input_dir": str(input_dir), "output_dir": str(output_dir),
        "model": {"provider": "openai_compatible", "base_url": "https://example.invalid/v1",
                  "model": "unused", "api_key": "unused"},
    })
    store = TaskStore(tmp_path / "data/tasks.sqlite3")
    snapshots = [
        _published_task(tmp_path / "data", "c" * 64, "阻塞店"),
        _published_task(tmp_path / "data", "d" * 64, "未启动店"),
    ]
    for snapshot in snapshots:
        store.scan(snapshot)
    tasks = store.tasks()
    task_ids = [task["id"] for task in tasks]
    profile_path = tmp_path / "uipath.profile.json"
    profile = {"mode": "automatic", "launch_mode": "cli", "max_tasks": 10}
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    class FakeRobot:
        def __init__(self, *_args):
            self.stopped = False

        def request_stop(self):
            self.stopped = True

    class FakeModel:
        api_key = "test-key"

    runner = UiPathRunner(configs, store, profile_path, transport_factory=FakeRobot)
    source_by_task = {task["id"]: str(Path(task["source_path"]).parent.parent) for task in tasks}
    runner._registered_trial_source = lambda task_id: source_by_task[task_id]
    store.material_binding = lambda _source: {}
    runner.readiness = lambda **_kwargs: {
        "ready": True, "issues": [], "profile": profile,
        "selected_task_ids": task_ids,
    }
    runner._launch_cli = lambda *_args, **_kwargs: None
    runner._await_cli_startup = lambda: None
    opened = []

    def open_source(task, selected_profile, *_args):
        opened.append(task["id"])
        selected_profile["miniapp_selector"] = f"window:{task['id']}"
        return {"ok": True}

    runner._open_source_automatic = open_source
    runner._move_window_on_screen = lambda _selector: None
    runner._set_stage = lambda *_args, **_kwargs: None
    runner._ensure_file_assistant = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("未回到助手"))
    runner._close_miniapp_windows = lambda *_args, **_kwargs: None
    monkeypatch.setattr(uipath_runner_module, "trial_model_from_env", lambda *_args: FakeModel())
    monkeypatch.setattr(uipath_runner_module, "run_navigation", lambda *_args, **_kwargs: (
        {"outcome": "needs_review", "reason": "测试终态", "source": "agent"}, None
    ))

    started = runner.start(ui_tars_trial=True, trial_task_ids=task_ids)
    deadline = time.monotonic() + 5
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)

    assert not runner.running, "试验线程应在测试时限内收尾"
    result_path = Path(started["result_file"])
    assert result_path.name == "batch-result.json"
    summary = json.loads(result_path.read_text(encoding="utf-8"))
    assert summary["status"] == "blocked"
    assert opened == [task_ids[0]]
    assert [entry["task_id"] for entry in summary["results"]] == [task_ids[0]]
    assert summary["results"][0]["wechat_return"]["status"] == "blocked"
    assert summary["unstarted_task_ids"] == [task_ids[1]]
    assert store.latest_run() is None
    assert [store.get(task_id)["status"] for task_id in task_ids] == ["pending", "pending"]
    assert not any(output_dir.glob("**/page-*.png"))


def test_batch_trial_reuses_one_robot_startup_and_writes_per_store_reports(tmp_path, monkeypatch):
    input_dir = tmp_path / "configured-input"
    input_dir.mkdir()
    output_dir = tmp_path / "official-output"
    configs = ConfigStore(tmp_path / "settings")
    configs.save({
        "input_dir": str(input_dir), "output_dir": str(output_dir),
        "model": {"provider": "openai_compatible", "base_url": "https://example.invalid/v1",
                  "model": "unused", "api_key": "unused"},
    })
    store = TaskStore(tmp_path / "data/tasks.sqlite3")
    snapshots = [
        _published_task(tmp_path / "data", "e" * 64, "试验店甲"),
        _published_task(tmp_path / "data", "f" * 64, "试验店乙"),
    ]
    for snapshot in snapshots:
        store.scan(snapshot)
    tasks = store.tasks()
    task_ids = [task["id"] for task in tasks]
    task_snapshot = [store.get(task_id) for task_id in task_ids]
    profile_path = tmp_path / "uipath.profile.json"
    profile = {"mode": "automatic", "launch_mode": "cli", "max_tasks": 10}
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    class FakeRobot:
        def __init__(self, *_args):
            self.stop_count = 0

        def request_stop(self):
            self.stop_count += 1

    class FakeModel:
        api_key = "test-key"

    runner = UiPathRunner(configs, store, profile_path, transport_factory=FakeRobot)
    source_by_task = {task["id"]: str(Path(task["source_path"]).parent.parent) for task in tasks}
    runner._registered_trial_source = lambda task_id: source_by_task[task_id]
    store.material_binding = lambda _source: {}
    runner.readiness = lambda **_kwargs: {
        "ready": True, "issues": [], "profile": profile,
        "selected_task_ids": task_ids,
    }
    runner._launch_cli = lambda *_args, **_kwargs: None
    startup_calls = []
    runner._await_cli_startup = lambda: startup_calls.append("startup")
    opened = []

    def open_source(task, selected_profile, *_args):
        opened.append(task["id"])
        selected_profile["miniapp_selector"] = f"window:{task['id']}"
        return {"ok": True}

    runner._open_source_automatic = open_source
    events = []

    def resize_window(selector, *_args, **_kwargs):
        events.append(("resize", selector))

    runner._prepare_trial_window = resize_window
    runner._set_stage = lambda *_args, **_kwargs: None
    runner._ensure_file_assistant = lambda *_args, **_kwargs: None
    runner._close_miniapp_windows = lambda *_args, **_kwargs: None
    runner._send = lambda command: {"windows": []} if command["action"] == "discover" else {}
    monkeypatch.setattr(uipath_runner_module, "trial_model_from_env", lambda *_args: FakeModel())
    def run_navigation(*_args, **_kwargs):
        events.append(("agent", None))
        return {"outcome": "unqualified", "reason": "页面明确显示门店不营业", "source": "agent"}, None

    monkeypatch.setattr(uipath_runner_module, "run_navigation", run_navigation)

    started = runner.start(ui_tars_trial=True, trial_task_ids=task_ids)
    deadline = time.monotonic() + 5
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)

    assert not runner.running, "试验线程应在测试时限内收尾"
    summary_path = Path(started["result_file"])
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert opened == task_ids
    assert events == [event for task_id in task_ids for event in (
        ("resize", f"window:{task_id}"), ("agent", None),
    )]
    assert startup_calls == ["startup"]
    assert summary["unstarted_task_ids"] == []
    assert [item["task_id"] for item in summary["results"]] == task_ids
    for item, task_id in zip(summary["results"], task_ids, strict=True):
        report_path = summary_path.parent / item["result_file"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["task_id"] == task_id
        assert report["navigation"]["outcome"] == "unqualified"
        assert report["wechat_return"]["status"] == "ready"
        assert not (report_path.parent / task_id).exists()
    assert [store.get(task_id) for task_id in task_ids] == task_snapshot
    assert store.latest_run() is None
    assert not any(output_dir.glob("**/page-*.png"))


def test_trial_handoff_failure_keeps_agent_decision(tmp_path, monkeypatch):
    input_dir = tmp_path / "configured-input"
    input_dir.mkdir()
    output_dir = tmp_path / "official-output"
    configs = ConfigStore(tmp_path / "settings")
    configs.save({
        "input_dir": str(input_dir), "output_dir": str(output_dir),
        "model": {"provider": "openai_compatible", "base_url": "https://example.invalid/v1",
                  "model": "unused", "api_key": "unused"},
    })
    store = TaskStore(tmp_path / "data/tasks.sqlite3")
    store.scan(_published_task(tmp_path / "data", "9" * 64, "交接失败店"))
    tasks = store.tasks()
    task_ids = [task["id"] for task in tasks]
    profile_path = tmp_path / "uipath.profile.json"
    profile = {"mode": "automatic", "launch_mode": "cli", "max_tasks": 10}
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    class FakeRobot:
        def __init__(self, *_args):
            pass

        def request_stop(self):
            pass

    class FakeModel:
        api_key = "test-key"

    runner = UiPathRunner(configs, store, profile_path, transport_factory=FakeRobot)
    source_by_task = {task["id"]: str(Path(task["source_path"]).parent.parent) for task in tasks}
    runner._registered_trial_source = lambda task_id: source_by_task[task_id]
    store.material_binding = lambda _source: {}
    runner.readiness = lambda **_kwargs: {
        "ready": True, "issues": [], "profile": profile, "selected_task_ids": task_ids,
    }
    runner._launch_cli = lambda *_args, **_kwargs: None
    runner._await_cli_startup = lambda: None

    def open_source(task, selected_profile, *_args):
        selected_profile["miniapp_selector"] = f"window:{task['id']}"
        return {"ok": True}

    runner._open_source_automatic = open_source
    runner._prepare_trial_window = lambda *_args, **_kwargs: None
    runner._set_stage = lambda *_args, **_kwargs: None
    runner._ensure_file_assistant = lambda *_args, **_kwargs: None
    runner._close_miniapp_windows = lambda *_args, **_kwargs: None
    monkeypatch.setattr(uipath_runner_module, "trial_model_from_env", lambda *_args: FakeModel())
    # Agent 判定可交接，但没有可用证据图：交接校验失败，原始判断必须保留在报告里。
    monkeypatch.setattr(uipath_runner_module, "run_navigation", lambda *_args, **_kwargs: (
        {"outcome": "success", "reason": "菜品与价格清晰可见", "source": "agent",
         "menu_region": {"x": 0, "y": 0, "width": 10, "height": 10}}, None
    ))

    started = runner.start(ui_tars_trial=True, trial_task_ids=task_ids)
    deadline = time.monotonic() + 5
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)

    summary_path = Path(started["result_file"])
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    report = json.loads((summary_path.parent / summary["results"][0]["result_file"])
                        .read_text(encoding="utf-8"))
    assert report["navigation"]["outcome"] == "needs_review"
    assert report["navigation"]["agent_outcome"] == "success"
    assert report["navigation"]["agent_reason"] == "菜品与价格清晰可见"
    # 顶层字段把 Agent 的业务判断与技术失败分开：success 仍保留，只是截图交接失败。
    assert report["agent_outcome"] == "success"
    assert report["agent_reason"] == "菜品与价格清晰可见"
    assert report["runtime_status"] == "failed"
    assert report["runtime_code"] == "runtime"
    assert report["runtime_error"]
    assert report["capture_status"] == "not_started"
    assert not any(output_dir.glob("**/page-*.png"))
