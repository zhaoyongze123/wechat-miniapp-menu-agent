from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from menu_capture.config import ConfigStore
from menu_capture.debug_log import DebugLog
from menu_capture.scene_agent import SceneDecision
from menu_capture.store import TaskStore
from menu_capture.uipath_runner import UiPathRunner


def _log(tmp_path: Path, **kwargs) -> DebugLog:
    return DebugLog(tmp_path, **kwargs)


def _runner(tmp_path: Path, log: DebugLog, transport=None):
    input_dir = tmp_path / "inbox"
    image_path = input_dir / "Example Store" / "menu.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (24, 16), "white").save(image_path)
    configs = ConfigStore(tmp_path / "settings")
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.scan(input_dir)
    task = store.tasks()[0]
    runner = UiPathRunner(
        configs, store, tmp_path / "uipath.profile.json", debug_log=log,
        transport_factory=lambda ipc, output: transport,
    )
    runner.transport = transport
    runner.current_task = task | {"stage": "menu"}
    runner.run_id = "test-run"
    runner._trace_attempt = "attempt-1"
    runner._trace_step = 3
    return runner, store, runner.current_task


class FakeTransport:
    def __init__(self, reply: dict | None = None):
        self.reply = reply or {"ok": True, "action": "screenshot", "width": 240, "height": 160}
        self.commands: list[dict] = []

    def run(self, command: dict, **kwargs) -> dict:
        self.commands.append(dict(command))
        return dict(self.reply)


def test_action_is_async_sanitized_and_export_includes_referenced_evidence(tmp_path: Path) -> None:
    log = _log(tmp_path)
    try:
        source = tmp_path / "frame.png"
        source.write_bytes(b"small image bytes")
        image_name = log.save_image(source)
        assert image_name is not None

        assert log.emit_action({
            "action_id": 42,
            "run_id": "run-42",
            "details": {
                "evidence": image_name,
                "message": "Authorization: Bearer bearer-secret api_key=key-secret",
            },
        })
        # API 只确认排入异步队列；等待后台线程后再验证落盘结果。
        assert log.action_state(42) in {"pending", "ready"}
        log.flush()

        row = log.action_record(42)
        assert row is not None
        assert "bearer-secret" not in json.dumps(row)
        assert "key-secret" not in json.dumps(row)
        assert "[REDACTED]" in row["details"]["message"]

        archive_path = tmp_path / "run.zip"
        log.export_run("run-42", archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            assert "actions.jsonl" in archive.namelist()
            assert f"evidence/{image_name}" in archive.namelist()
            exported = json.loads(archive.read("actions.jsonl").splitlines()[0])
            assert exported["action_id"] == 42
    finally:
        log.close()


def test_segment_rotation_prunes_old_actions_and_their_unreferenced_media(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(DebugLog, "SEGMENT_BYTES", 300)
    log = _log(tmp_path)
    try:
        # 小配额把正常轮转/清理路径压缩到几条测试记录内。
        log._max_bytes = 950
        image = tmp_path / "evidence.png"
        image.write_bytes(b"evidence")
        image_name = log.save_image(image)
        assert image_name is not None
        log.flush()
        assert log.emit_action({"action_id": 1, "run_id": "r", "details": {"image": image_name, "padding": "x" * 180}})
        log.flush()
        assert log.action_state(1) == "ready"

        for action_id in (2, 3, 4, 5):
            assert log.emit_action({"action_id": action_id, "run_id": "r", "details": {"padding": "y" * 180}})
            log.flush()

        assert log.action_state(1) == "expired"
        assert not (log.media_dir / image_name).exists()
        assert log.summary()["total_bytes"] <= log._max_bytes
    finally:
        log.close()


def test_disk_write_failure_is_contained_in_background_worker(tmp_path: Path, monkeypatch) -> None:
    log = _log(tmp_path)

    def fail_open_segment():
        raise OSError("simulated disk full")

    monkeypatch.setattr(log, "_open_segment", fail_open_segment)
    try:
        assert log.emit_action({"action_id": 99, "run_id": "r", "details": {}})
        log.flush()
        assert log._worker.is_alive()
        assert log.action_state(99) == "dropped"
        assert log.emit("still_running", task_id="task")
    finally:
        log.close()


def test_idle_flush_disk_error_does_not_kill_background_worker(tmp_path: Path) -> None:
    log = _log(tmp_path)

    class FailingStream:
        def flush(self) -> None:
            raise OSError("simulated disk full during idle flush")

        def close(self) -> None:
            return None

    try:
        assert log.emit_action({"action_id": 100, "run_id": "r", "details": {}})
        log.flush()
        with log._lock:
            log._current_stream = FailingStream()

        deadline = time.monotonic() + 1.0
        while log._worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert log._worker.is_alive()
    finally:
        log.close()


def test_oversized_record_is_truncated_to_the_record_limit(tmp_path: Path) -> None:
    log = _log(tmp_path)
    try:
        assert log.emit_action({
            "action_id": 7,
            "run_id": "large",
            "details": {"arbitrary_payload": ["z" * 16_000 for _ in range(400)]},
        })
        log.flush()

        location = log._action_index[7]
        assert location[2] <= log.MAX_RECORD_BYTES
        row = log.action_record(7)
        assert row is not None and row.get("record_truncated") is True
    finally:
        log.close()


def test_send_traces_command_id_parameters_and_reply_with_fake_transport(tmp_path: Path) -> None:
    log = _log(tmp_path / "data")
    transport = FakeTransport({"ok": True, "action": "screenshot", "width": 240, "height": 160})
    runner, store, task = _runner(tmp_path, log, transport)
    try:
        reply = runner._send({
            "id": "command-123",
            "action": "screenshot",
            "path": str(tmp_path / "private" / "frame.png"),
            "selector": "recorded-selector",
            "text": "sensitive text omitted from debug parameters",
        })
        assert reply["width"] == 240
        log.flush()

        action = store.actions(task["id"])[0]
        debug = log.action_record(action["id"])
        assert debug is not None
        assert debug["details"]["command_id"] == "command-123"
        assert debug["details"]["command_parameters"] == {
            "action": "screenshot",
            "path_basename": "frame.png",
            "selector": "recorded-selector",
        }
        assert debug["details"]["reply_summary"] == {
            "ok": True, "action": "screenshot", "width": 240, "height": 160,
        }
        assert transport.commands[0]["id"] == "command-123"
    finally:
        log.close()


def test_trace_exception_persists_call_stack_in_debug_record(tmp_path: Path) -> None:
    log = _log(tmp_path / "data")
    runner, store, task = _runner(tmp_path, log)
    try:
        with pytest.raises(ValueError, match="intentional trace failure"):
            with runner._trace("local_ocr", target="开始点餐"):
                raise ValueError("intentional trace failure")
        log.flush()

        action = store.actions(task["id"])[0]
        assert action["result"] == "error"
        debug = log.action_record(action["id"])
        assert debug is not None
        assert debug["result"] == "error"
        assert debug["details"]["error_type"] == "ValueError"
        assert "Traceback (most recent call last)" in debug["details"]["error_stack"]
        assert "intentional trace failure" in debug["details"]["error_stack"]
    finally:
        log.close()


def test_local_ocr_uses_dictionary_box_center_and_reuses_cached_rows(tmp_path: Path) -> None:
    log = _log(tmp_path / "data")
    runner, _store, _task = _runner(tmp_path, log)
    image_path = tmp_path / "menu.png"
    Image.new("RGB", (240, 160), "white").save(image_path)
    rows = [{"text": "开始点餐", "x": 90, "y": 180, "width": 150, "height": 36, "confidence": 0.99}]
    try:
        detail = runner._find_ocr_target_detail(rows, (240, 160), "开始点餐")
        assert detail["matched"] is True
        assert detail["center_image_px"] == {"x": 55.0, "y": 66.0}

        local = runner._local_prepare_action(((240, 160), rows), people_count=1, auto_authorize=False)
        assert local is not None
        assert (local.x, local.y) == (55.0, 66.0)

        calls: list[Path] = []
        runner.ocr_reader = lambda path: calls.append(Path(path)) or rows
        runner._active_ocr_path = str(image_path.resolve())
        runner._active_ocr_result = ((240, 160), rows)
        model_decision = SceneDecision.model_validate({
            "action": "click", "reason": "model suggestion", "x": 1, "y": 2,
            "target_label": "开始点餐", "overlay_visible": False,
            "login_required": False, "phone_required": False,
            "consent_required": False, "transaction_visible": False,
        })
        resolved = runner._resolve_click_coordinates(image_path, model_decision, people_count=1)
        assert (resolved.x, resolved.y) == (55.0, 66.0)
        assert calls == []
        assert runner._last_ocr_meta["cache_reused"] is True
    finally:
        log.close()


def test_send_succeeds_when_debug_log_rejects_enqueue(tmp_path: Path, monkeypatch) -> None:
    log = _log(tmp_path / "data")
    transport = FakeTransport()
    runner, store, task = _runner(tmp_path, log, transport)
    monkeypatch.setattr(log, "emit_action", lambda _record: False)
    try:
        reply = runner._send({"id": "rejected-log-command", "action": "noop"})
        assert reply["ok"] is True
        assert len(transport.commands) == 1
        action = store.actions(task["id"])[0]
        assert action["result"] == "ok"
        assert action["details"]["debug_state"] == "dropped"
    finally:
        log.close()


def test_send_succeeds_when_background_disk_write_fails(tmp_path: Path, monkeypatch) -> None:
    log = _log(tmp_path / "data")
    transport = FakeTransport()
    runner, store, task = _runner(tmp_path, log, transport)

    def fail_open_segment():
        raise OSError("simulated disk full")

    monkeypatch.setattr(log, "_open_segment", fail_open_segment)
    try:
        reply = runner._send({"id": "disk-error-command", "action": "noop"})
        assert reply["ok"] is True
        assert len(transport.commands) == 1
        log.flush()
        action = store.actions(task["id"])[0]
        assert action["result"] == "ok"
        assert log.action_state(action["id"]) == "dropped"
        assert log._worker.is_alive()
    finally:
        log.close()
