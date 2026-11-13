from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from menu_capture.config import ConfigStore
from menu_capture.store import TaskStore
from menu_capture.uipath_runner import AgentUnqualified, RobotTimeout, UiPathRunner


class _Model:
    api_key = "test-key"

    def close(self):
        pass


class _Transport:
    def request_stop(self):
        pass


def _setup(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    source = input_dir / "Shop A" / "qr.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (80, 80), "white").save(source)

    output_dir = tmp_path / "output"
    configs = ConfigStore(tmp_path / "settings")
    configs.save({
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "model": {"provider": "openai_compatible", "base_url": "https://unused.example/v1",
                  "model": "unused", "api_key": "unused"},
    })
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.scan(input_dir)
    task = store.tasks()[0]

    profile_path = tmp_path / "uipath.profile.json"
    profile_path.write_text(json.dumps({"mode": "attached", "selected_task_id": task["id"],
                                        "max_tasks": 1}), encoding="utf-8")
    runner = UiPathRunner(configs, store, profile_path)
    runner.run_id = "formal-test-run"
    runner.run_directory = output_dir / ".uipath-runs" / runner.run_id
    runner.run_directory.mkdir(parents=True)
    runner.transport = _Transport()
    store.create_run(runner.run_id, "attached", [task["id"]])
    monkeypatch.setattr(runner, "_await_cli_startup", lambda: None)
    monkeypatch.setattr(runner, "_reap_cli", lambda: None)
    settings = configs.load()
    staging = runner._staging_dir(output_dir, task, "formal-attempt")
    staging.mkdir(parents=True)
    return runner, store, task, settings, runner.profile_path.read_text(encoding="utf-8"), staging, _Model()


def _report(task: dict, *, outcome: str, reason: str = "", runtime_status: str = "ok") -> dict:
    return {
        "task_id": task["id"],
        "agent_outcome": outcome,
        "agent_reason": reason,
        "runtime_status": runtime_status,
        "runtime_error": reason if runtime_status != "ok" else "",
        "capture_status": "staged" if outcome == "success" else "not_started",
    }


def _run_worker(runner, store, task, settings, profile, staging, model):
    runner._worker(task, settings, json.loads(profile), staging, model)


def test_formal_success_publishes_agent_menu_pages(tmp_path: Path, monkeypatch):
    runner, store, task, settings, profile, staging, model = _setup(tmp_path, monkeypatch)
    calls = []

    def shared_case(current_task, current_settings, current_profile, evidence_dir, current_model,
                    *, automatic, capture_dir, trial=False):
        calls.append((automatic, capture_dir, trial))
        Image.new("RGB", (120, 90), "white").save(capture_dir / "page-0001.png")
        return _report(current_task, outcome="success")

    monkeypatch.setattr(runner, "_run_gui_agent_case", shared_case)
    _run_worker(runner, store, task, settings, profile, staging, model)

    result = store.get(task["id"])
    assert result["status"] == "captured"
    assert Path(result["output_dir"]).joinpath("page-0001.png").is_file()
    assert calls == [(False, staging, False)]
    assert not staging.exists()


def test_formal_needs_review_does_not_turn_reason_text_into_rejection(tmp_path: Path, monkeypatch):
    runner, store, task, settings, profile, staging, model = _setup(tmp_path, monkeypatch)
    reason = "页面文案显示门店已休息，但 Agent 判断证据不足，需人工检查"
    monkeypatch.setattr(runner, "_run_gui_agent_case",
                        lambda *args, **kwargs: _report(task, outcome="needs_review", reason=reason))

    _run_worker(runner, store, task, settings, profile, staging, model)

    result = store.get(task["id"])
    assert result["status"] == "needs_review"
    assert result["error_code"] == "agent_needs_review"
    assert result["reject_reason"] == ""
    assert store.get_run(runner.run_id)["status"] == "completed"


def test_formal_agent_unqualified_is_the_only_agent_outcome_marked_unqualified(tmp_path: Path, monkeypatch):
    runner, store, task, settings, profile, staging, model = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_run_gui_agent_case",
                        lambda *args, **kwargs: _report(task, outcome="unqualified", reason="二维码已失效"))

    _run_worker(runner, store, task, settings, profile, staging, model)

    result = store.get(task["id"])
    assert result["status"] == "needs_review"
    assert result["stage"] == "agent_unqualified"
    assert result["error_code"] == "agent_unqualified"
    assert result["reject_reason"] == "other"


def test_formal_timeout_marks_task_needs_review_and_finishes_run(tmp_path: Path, monkeypatch):
    runner, store, task, settings, profile, staging, model = _setup(tmp_path, monkeypatch)

    def timeout(*args, **kwargs):
        raise RobotTimeout("UiPath 命令超时")

    monkeypatch.setattr(runner, "_run_gui_agent_case", timeout)
    _run_worker(runner, store, task, settings, profile, staging, model)

    result = store.get(task["id"])
    assert result["status"] == "needs_review"
    assert result["error_code"] == "uipath_timeout"
    assert store.get_run(runner.run_id)["status"] == "completed"
    assert not (Path(settings.output_dir) / "Shop A").exists()


def test_terminate_before_recording_agent_result_does_not_mark_store_unqualified(tmp_path: Path, monkeypatch):
    runner, store, task, settings, profile, staging, model = _setup(tmp_path, monkeypatch)

    def result_after_stop(*args, **kwargs):
        runner.stop_event.set()
        return _report(task, outcome="unqualified", reason="二维码已失效")

    monkeypatch.setattr(runner, "_run_gui_agent_case", result_after_stop)
    _run_worker(runner, store, task, settings, profile, staging, model)

    result = store.get(task["id"])
    assert result["error_code"] == "terminated"
    assert result["reject_reason"] == ""
    assert store.get_run(runner.run_id)["status"] == "terminated"
