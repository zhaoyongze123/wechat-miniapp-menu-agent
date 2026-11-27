from __future__ import annotations

from pathlib import Path
from PIL import Image
from fastapi.testclient import TestClient
import pytest

from menu_capture.api.server import create_app
from menu_capture.vision.store_name_recognizer import recognize_store_name


class _StubModel:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def _request(self, _image, _prompt: str) -> str:
        return self.payload


def _png(path: Path, color: tuple[int, int, int] = (255, 255, 255)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color).save(path)
    return path


def _scan_tasks(store, root: Path, names: list[str]) -> list[dict]:
    src = root / "input"
    for idx, name in enumerate(names):
        _png(src / name / "qr.png", (255 - idx * 20, 255, 255))
    store.scan(src)
    by_name = {task["source_name"]: task for task in store.tasks()}
    return [by_name[name] for name in names]


def test_recognize_store_name_parses_and_sanitizes(tmp_path: Path) -> None:
    image = _png(tmp_path / "shot.png")
    result = recognize_store_name(
        _StubModel('{"store_name":"老乡鸡(上海临港万达广场店)","reason":"顶部导航栏明确显示门店标题"}'),
        image,
    )
    assert result["store_name"] == "老乡鸡(上海临港万达广场店)"
    assert "顶部导航栏" in result["reason"]


def test_manual_recognize_and_manual_save_do_not_interrupt_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(tmp_path)
    store = app.state.store
    runner = app.state.runner
    [task] = _scan_tasks(store, tmp_path, ["E02636 老乡鸡"])
    store.create_run("run-manual", "automatic", [task["id"]], capture_mode="manual")
    runner.run_id = "run-manual"
    runner.current_task = task

    shot_dir = tmp_path / "shots"
    shot_dir.mkdir()
    with runner._manual_cond:
        runner._manual_selector = "miniapp-window"
        runner._manual_output_dir = shot_dir
    runner._set_manual_state(task, 0, 3, "waiting_human", waiting=True)
    state = runner.manual_state()
    assert state is not None
    expected = {"run_id": state["run_id"], "task_id": state["task_id"], "wait_id": state["wait_id"]}

    def fake_screenshot(output_dir: Path, _selector: str, name: str) -> dict:
        p = _png(output_dir / name)
        return {"path": str(p), "width": 64, "height": 64}

    monkeypatch.setattr(runner, "_screenshot", fake_screenshot)

    recognized = runner.manual_recognize_store_name(
        expected=expected,
        recognizer=lambda _path: {"store_name": "老乡鸡(临港万达店)", "reason": "招牌可见"},
    )
    assert recognized["ok"] is True
    assert recognized["stale"] is False
    assert recognized["observed_store_name"] == "老乡鸡(临港万达店)"
    assert store.observed_store_name("run-manual", task["id"]) == "老乡鸡(临港万达店)"
    # 独立辅助能力不得改动 waiting 与 decision 状态
    after = runner.manual_state()
    assert after is not None and after["waiting"] is True and after["decision"] == ""
    assert after["observed_store_name"] == "老乡鸡(临港万达店)"

    saved = runner.manual_set_store_name("老乡鸡(上海临港万达店)", expected=expected)
    assert saved["observed_store_name"] == "老乡鸡(上海临港万达店)"
    assert store.observed_store_name("run-manual", task["id"]) == "老乡鸡(上海临港万达店)"


def test_manual_recognize_discards_result_when_store_switched_mid_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path)
    store = app.state.store
    runner = app.state.runner
    task1, task2 = _scan_tasks(store, tmp_path, ["门店1", "门店2"])
    store.create_run("run-manual", "automatic", [task1["id"], task2["id"]], capture_mode="manual")
    runner.run_id = "run-manual"

    shot_dir = tmp_path / "shots"
    shot_dir.mkdir()
    with runner._manual_cond:
        runner._manual_selector = "miniapp-window"
        runner._manual_output_dir = shot_dir
    runner._set_manual_state(task1, 0, 2, "waiting_human", waiting=True)
    state1 = runner.manual_state()
    assert state1 is not None
    expected1 = {"run_id": state1["run_id"], "task_id": state1["task_id"], "wait_id": state1["wait_id"]}

    monkeypatch.setattr(
        runner, "_screenshot",
        lambda output_dir, _selector, name: {"path": str(_png(output_dir / name)), "width": 64, "height": 64},
    )

    def slow_recognizer(_path: str) -> dict:
        # 模拟识别期间用户已点击截图切换到门店2
        runner._set_manual_state(task2, 1, 2, "waiting_human", waiting=True)
        return {"store_name": "门店1实际名称", "reason": "识别完成"}

    result = runner.manual_recognize_store_name(expected=expected1, recognizer=slow_recognizer)
    assert result["stale"] is True
    assert store.observed_store_name("run-manual", task1["id"]) == ""
    assert store.observed_store_name("run-manual", task2["id"]) == ""


def test_manual_recognize_does_not_overwrite_manual_edit_mid_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path)
    store = app.state.store
    runner = app.state.runner
    [task] = _scan_tasks(store, tmp_path, ["门店A"])
    store.create_run("run-manual", "automatic", [task["id"]], capture_mode="manual")
    runner.run_id = "run-manual"
    runner.current_task = task

    shot_dir = tmp_path / "shots"
    shot_dir.mkdir()
    with runner._manual_cond:
        runner._manual_selector = "miniapp-window"
        runner._manual_output_dir = shot_dir
    runner._set_manual_state(task, 0, 1, "waiting_human", waiting=True)
    state = runner.manual_state()
    assert state is not None
    expected = {"run_id": state["run_id"], "task_id": state["task_id"], "wait_id": state["wait_id"]}

    monkeypatch.setattr(
        runner, "_screenshot",
        lambda output_dir, _selector, name: {"path": str(_png(output_dir / name)), "width": 64, "height": 64},
    )

    def recognizer_during_manual_edit(_path: str) -> dict:
        # 模拟 AI 识别尚未返回时，人工先手动保存了名称
        runner.manual_set_store_name("人工手填准确店名", expected=expected)
        return {"store_name": "AI滞后识别名", "reason": "招牌"}

    result = runner.manual_recognize_store_name(expected=expected, recognizer=recognizer_during_manual_edit)
    assert result["stale"] is True
    assert result["observed_store_name"] == "人工手填准确店名"
    assert store.observed_store_name("run-manual", task["id"]) == "人工手填准确店名"


def test_manual_store_name_api_routes_and_failure_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path)
    store = app.state.store
    runner = app.state.runner
    [task] = _scan_tasks(store, tmp_path, ["E001 霸王茶姬"])
    store.create_run("run-api", "automatic", [task["id"]], capture_mode="manual")
    runner.run_id = "run-api"
    runner.current_task = task
    shot_dir = tmp_path / "shots"
    shot_dir.mkdir()
    with runner._manual_cond:
        runner._manual_selector = "miniapp-window"
        runner._manual_output_dir = shot_dir
    runner._set_manual_state(task, 0, 1, "waiting_human", waiting=True)

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    token = client.get("/api/session").json()["token"]
    headers = {"x-capture-token": token, "origin": "http://127.0.0.1:8765"}
    manual_info = client.get("/api/manual/state", headers=headers).json()
    manual = manual_info["manual"]
    context = {
        "device_id": manual_info["device_id"],
        "run_id": manual["run_id"],
        "task_id": manual["task_id"],
        "wait_id": manual["wait_id"],
    }

    # 1. 模型调用超时返回 504，不阻塞后续人工保存和截图
    monkeypatch.setattr(
        runner, "manual_recognize_store_name",
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("upstream timeout")),
    )
    timeout_res = client.post("/api/manual/recognize-store-name", headers=headers, json={"context": context})
    assert timeout_res.status_code == 504
    assert runner.manual_state()["waiting"] is True

    # 2. 手动保存接口正常落库
    save_res = client.post(
        "/api/manual/store-name",
        headers=headers,
        json={"context": context, "store_name": "霸王茶姬(上海静安寺店)"},
    )
    assert save_res.status_code == 200
    assert save_res.json()["observed_store_name"] == "霸王茶姬(上海静安寺店)"
    assert store.observed_store_name("run-api", task["id"]) == "霸王茶姬(上海静安寺店)"

    # 3. 过期的 wait_id 拒绝写入，返回 409
    bad_ctx = {**context, "wait_id": "stale-wait-id"}
    stale_res = client.post(
        "/api/manual/store-name",
        headers=headers,
        json={"context": bad_ctx, "store_name": "串店名称"},
    )
    assert stale_res.status_code == 409
    assert store.observed_store_name("run-api", task["id"]) == "霸王茶姬(上海静安寺店)"
