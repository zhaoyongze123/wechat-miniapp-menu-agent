import csv
import json
import threading
import time
import types
from pathlib import Path

import pytest
from PIL import Image

from menu_capture.annotation import (
    AnnotationError,
    AnnotationSession,
    archive_samples,
    image_effect,
    map_box_to_window,
    suggest_target,
)
from menu_capture.config import ConfigStore, Settings
from menu_capture.model import Region
from menu_capture.rejections import RejectionService
from menu_capture.scene_agent import SceneDecision
from menu_capture.store import TaskStore
from menu_capture.uipath_runner import RobotCommandError, UiPathRunner


class FakeConfigs:
    def __init__(self, settings):
        self.settings = settings

    def load(self):
        return self.settings


class FakeProfiles:
    def __init__(self, profile):
        self.profile = profile

    def load(self):
        return dict(self.profile)


class FakeStore:
    def __init__(self, tasks):
        self.tasks = {task["id"]: task for task in tasks}

    def get(self, task_id):
        return dict(self.tasks[task_id])


class FakeExecutor:
    def __init__(self, fail_open=False, fail_click=False, same_frame=False):
        self.fail_open = fail_open
        self.fail_click = fail_click
        self.same_frame = same_frame
        self.work_dir = None
        self.clicks = []
        self.closed = 0
        self.shot = 0
        self.screenshots = []
        self.resizes = []

    def running(self):
        return False

    def attach(self, run_directory, output_root, work_dir, profile):
        self.work_dir = Path(work_dir)

    def launch(self, run_directory, settings, profile, task_count):
        return None

    def await_startup(self):
        return None

    def ensure_safe_start(self, profile, settle_seconds):
        return None

    def _write(self, name):
        self.shot += 1
        path = self.work_dir / "evidence" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        colour = (20, 120, 100) if self.same_frame or self.shot % 2 else (220, 220, 220)
        Image.new("RGB", (200, 400), colour).save(path)
        return {"path": str(path), "width": 200, "height": 400,
                "window_width": 100, "window_height": 200}

    def open_store(self, task, profile, work_dir, settle_seconds):
        if self.fail_open:
            raise RuntimeError("二维码没有打开小程序")
        profile["miniapp_selector"] = "<wnd app='WeChat' title='测试门店' />"
        return self._write("miniapp-after-qr.png")

    def resize(self, selector, width, height, x, y):
        self.resizes.append({"selector": selector, "width": width, "height": height, "x": x, "y": y})
        return {"ok": True}

    def screenshot(self, selector, name):
        self.screenshots.append(name)
        return self._write(name)

    def screenshot_after_click(self, selector, name):
        return self.screenshot(selector, name)

    def click(self, selector, x, y):
        if self.fail_click:
            raise RuntimeError("UiPath 点击失败")
        self.clicks.append({"selector": selector, "x": x, "y": y})
        return {"ok": True}

    def wait(self, seconds):
        return {"ok": True}

    def close(self, profile, settle_seconds):
        self.closed += 1

    def request_stop(self):
        return None

    def reap(self):
        return None

    def detach(self):
        return None


def build_session(tmp_path, executor, *, ocr=None, rejector=None, tasks=None):
    settings = Settings(input_dir=str(tmp_path / "input"), output_dir=str(tmp_path / "output"))
    task = {"id": "task-1", "source_name": "测试门店", "source_path": str(tmp_path / "qr.png"),
            "source_hash": "hash", "status": "pending"}
    session = AnnotationSession(
        FakeConfigs(settings), FakeStore(tasks or [task]),
        FakeProfiles({"launch_mode": "cli", "wechat_selector": "<wnd app='WeChat' />",
                      "preview_selector": "<wnd app='WeChat' title='图片和视频' />",
                      "miniapp_selector": "", "close_key": "[d(hk)][d(command)]w[u(command)][u(hk)]"}),
        executor,
        root=tmp_path,
        ocr_reader=ocr or (lambda _: []),
        activator=lambda: None,
        rejector=rejector,
    )
    return session, (tasks or [task])[0]


class FakeRejector:
    def __init__(self):
        self.calls = []

    def __call__(self, task_id, reason, detail):
        self.calls.append({"task_id": task_id, "reason": reason, "detail": detail})
        return {"id": task_id}


def test_map_box_to_window_converts_retina_pixels_to_points():
    mapping = map_box_to_window({"x": 40, "y": 80, "width": 40, "height": 20},
                                (200, 400), (100, 200))
    assert mapping["center_px"] == {"x": 60.0, "y": 90.0}
    assert mapping["center_points"] == {"x": 30.0, "y": 45.0}
    assert mapping["scale"] == {"x": 0.5, "y": 0.5}
    assert mapping["box_px"] == {"x": 40.0, "y": 80.0, "width": 40.0, "height": 20.0}


@pytest.mark.parametrize("box", [
    {"x": -40, "y": 10, "width": 30, "height": 30},
    {"x": 10, "y": 10, "width": 30, "height": 600},
    {"x": 10, "y": 10, "width": 2, "height": 30},
])
def test_map_box_to_window_rejects_invalid_box(box):
    with pytest.raises(ValueError):
        map_box_to_window(box, (200, 400), (100, 200))


def test_scene_categories_keep_page_state_in_sync():
    from menu_capture.annotation import _resolve_page_state
    assert _resolve_page_state("miniapp_home", "") == "miniapp_home"
    assert _resolve_page_state("store_select", "unknown") == "store_select"
    assert _resolve_page_state("people_grid", "") == "people_select"
    assert _resolve_page_state("consent_checkbox", "") == "consent"
    assert _resolve_page_state("other", "") == "unknown"


def test_scene_layouts_stay_inside_the_current_category(monkeypatch):
    from menu_capture import annotation
    # 归纳前变体表为空：只标场景，任何样式都按未细分处理。
    assert annotation._resolve_layout("miniapp_home", "home_tabbar_scan") == ""
    monkeypatch.setitem(annotation.LAYOUTS, "miniapp_home",
                        (("home_tabbar_scan", "底部标签栏入口"),))
    assert annotation._resolve_layout("miniapp_home", "home_tabbar_scan") == "home_tabbar_scan"
    # 别的场景的变体不能串台，否则两层数据对不上。
    assert annotation._resolve_layout("miniapp_home", "permission_location") == ""
    assert annotation._resolve_layout("other", "close_ad_overlay") == ""
    assert annotation._resolve_layout("people_grid", "") == ""


def test_session_click_records_layout_for_scene_variant(tmp_path, monkeypatch):
    from menu_capture import annotation
    monkeypatch.setitem(annotation.LAYOUTS, "miniapp_home",
                        (("home_tabbar_scan", "底部标签栏入口"),))
    executor = FakeExecutor()
    session, task = build_session(tmp_path, executor)
    session.start([task["id"]])
    session.wait_background()
    box = {"x": 40, "y": 80, "width": 40, "height": 20}
    state = session.click(box, "miniapp_home", "", "扫码点餐", False, "", "home_tabbar_scan")
    assert state["steps"][0]["layout"] == "home_tabbar_scan"
    # 与场景不匹配的样式按未细分落盘，不写入脏值。
    state = session.click(box, "miniapp_home", "", "扫码点餐", False, "", "permission_location")
    assert state["steps"][1]["layout"] == ""
    session.complete()
    session.wait_background()
    manifest = tmp_path / "素材" / "规则样本" / "manifest.csv"
    with manifest.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert list(rows[0].keys())[:3] == ["file", "category", "layout"]
    assert [row["layout"] for row in rows] == ["home_tabbar_scan", ""]


def test_suggest_target_prefers_visible_people_choice():
    rows = [
        {"text": "请选择用餐人数", "x": 20, "y": 120, "width": 160, "height": 24},
        {"text": "2人", "x": 40, "y": 180, "width": 50, "height": 24},
    ]
    suggestion = suggest_target(rows, 200, 400)
    assert suggestion["category"] == "people_grid"
    assert suggestion["page_state"] == "people_select"
    assert suggestion["box_px"]["y"] <= 120
    assert suggestion["matched_text"] == "请选择用餐人数"


def test_suggest_target_returns_none_without_clear_evidence():
    assert suggest_target([{"text": "欢迎光临", "x": 1, "y": 1, "width": 50, "height": 10}], 200, 400) is None


def test_archive_samples_writes_manifest_and_marks_hard_negative(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    before = store / "before.png"
    after = store / "after.png"
    Image.new("RGB", (60, 80), "white").save(before)
    Image.new("RGB", (60, 80), "gray").save(after)
    steps = [{
        "index": 2, "task_id": "task-1", "category": "close_icon", "page_state": "ad_or_overlay",
        "target": "关闭广告", "hard_negative": True, "before": {"path": str(before)},
        "after": {"path": str(after)}, "center_points": {"x": 12.5, "y": 34.25},
        "box_px": {"x": 10, "y": 20, "width": 30, "height": 40},
        "effect": {"changed": False, "diff_score": 0.1}, "matched_text": "关闭广告",
    }]
    records = archive_samples(tmp_path / "samples", {"id": "task-1", "source_name": "测试门店"},
                              Settings(people_count=2), steps, "session-1")
    assert records and (tmp_path / "samples" / "close_icon" / Path(records[0]["before"]).name).is_file()
    with (tmp_path / "samples" / "manifest.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["category"] == "close_icon"
    assert rows[0]["sub_type"] == "hard_negative"
    assert rows[0]["configured_people"] == "2"
    assert rows[0]["expected_x"] == "12.5"
    assert rows[0]["expected_y"] == "34.25"


def test_session_click_archives_mapped_coordinates(tmp_path):
    executor = FakeExecutor()
    session, task = build_session(tmp_path, executor, ocr=lambda _: [
        {"text": "请选择用餐人数", "x": 20, "y": 100, "width": 160, "height": 30},
    ])
    state = session.start([task["id"]])
    assert state["status"] == "ready"
    assert state["frame"]["scale_x"] == 0.5
    # 类别建议在后台计算，不阻塞首屏。
    session.wait_background()
    assert session.state()["suggestion"]["category"] == "people_grid"

    state = session.click({"x": 40, "y": 80, "width": 40, "height": 20},
                          "people_grid", "", "选择 2 人", False)
    assert state["status"] == "clicked"
    # 页面状态由类别推导，前端不再单独提交。
    assert state["steps"][0]["page_state"] == "people_select"
    assert executor.clicks == [{"selector": "<wnd app='WeChat' title='测试门店' />", "x": 30.0, "y": 45.0}]
    assert state["last_click"]["center_points"] == {"x": 30.0, "y": 45.0}
    # 打开门店后统一窗口几何，点击时复用当前画面不再截图。
    assert executor.resizes[0]["width"] == 240
    assert executor.screenshots == ["opened-normalized.png", "step-01-after.png"]
    assert (Path(session.work_dir) / "steps" / "001.json").is_file()

    state = session.complete()
    assert state["status"] == "closing"
    session.wait_background()
    state = session.state()
    assert state["status"] == "finished"
    manifest = tmp_path / "素材" / "规则样本" / "manifest.csv"
    with manifest.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["category"] == "people_grid"
    assert rows[0]["expected_x"] == "30.0"


def test_session_open_failure_can_skip_to_finish(tmp_path):
    executor = FakeExecutor(fail_open=True)
    session, task = build_session(tmp_path, executor)
    state = session.start([task["id"]])
    assert state["status"] == "open_failed"
    state = session.skip("二维码无法打开")
    assert state["status"] == "finished"
    assert state["skipped"][0]["reason"] == "二维码无法打开"


def test_session_reject_marks_task_and_advances(tmp_path):
    executor = FakeExecutor(fail_open=True)
    rejector = FakeRejector()
    session, task = build_session(tmp_path, executor, rejector=rejector)
    state = session.start([task["id"]])
    assert state["status"] == "open_failed"
    state = session.reject("invalid_qr", "二维码已过期")
    assert state["status"] == "finished"
    assert rejector.calls == [{"task_id": task["id"], "reason": "invalid_qr", "detail": "二维码已过期"}]
    assert state["rejected"][0]["reason"] == "invalid_qr"


def test_mark_annotation_task_allows_pending_and_prevents_recapture():
    class Store:
        def __init__(self):
            self.task = {"id": "task-1", "status": "pending", "stage": "queued",
                         "source_name": "测试门店", "source_path": "/tmp/qr.png"}
            self.events = []

        def get(self, task_id):
            return dict(self.task)

        def update(self, task_id, **changes):
            self.task.update(changes)

        def event(self, message, level="info", task_id=None):
            self.events.append((message, level, task_id))

    store = Store()
    service = RejectionService(store, None)
    updated = service.mark_annotation_task("task-1", "invalid_qr", "二维码过期")
    assert updated["status"] == "needs_review"
    assert updated["stage"] == "annotation_rejected"
    assert updated["reject_reason"] == "invalid_qr"
    assert updated["reject_detail"] == "二维码过期"
    assert store.events[0][1] == "warning"
    with pytest.raises(ValueError):
        service.mark_annotation_task("task-1", "invalid_qr")


def test_session_click_failure_returns_state_instead_of_raising(tmp_path):
    executor = FakeExecutor(fail_click=True)
    session, task = build_session(tmp_path, executor)
    session.start([task["id"]])
    state = session.click({"x": 40, "y": 80, "width": 40, "height": 20},
                          "people_grid", "people_select", "选择 2 人", False)
    assert state["status"] == "ready"
    assert "点击未完成或结果未知" in state["message"]
    assert state["steps"] == []


def test_screenshot_retries_once_after_foreground_race(tmp_path):
    class Dummy:
        def __init__(self):
            self.calls = []

        def _fast_window_capture(self, *_args, **_kwargs):
            return None

        def _send(self, command):
            self.calls.append(dict(command))
            if len(self.calls) == 1:
                raise RobotCommandError("UiPath 命令失败：目标窗口 foreground=false；已拒绝窗口截图或键盘输入")
            Path(command["path"]).write_bytes(b"png")
            return {"path": command["path"], "width": 200, "height": 400,
                    "window_width": 100, "window_height": 200}

    dummy = Dummy()
    dummy._screenshot = types.MethodType(UiPathRunner._screenshot, dummy)
    reply = dummy._screenshot(tmp_path, "<wnd app='WeChat' />", "retry.png")
    assert reply["width"] == 200
    assert len(dummy.calls) == 2
    assert dummy.calls[0]["path"] == dummy.calls[1]["path"]


def test_pause_blocks_actions_and_resume_restores_state(tmp_path):
    executor = FakeExecutor()
    session, task = build_session(tmp_path, executor)
    assert session.start([task["id"]])["status"] == "ready"
    state = session.pause()
    assert state["status"] == "paused"
    assert state["paused"] is True
    with pytest.raises(AnnotationError, match="暂停"):
        session.click({"x": 40, "y": 80, "width": 40, "height": 20},
                      "people_grid", "people_select", "选择 2 人", False)
    with pytest.raises(AnnotationError, match="暂停"):
        session.skip()
    state = session.resume()
    assert state["status"] == "ready"
    assert executor.clicks == []


def test_complete_skips_positive_clicks_without_visible_change(tmp_path):
    executor = FakeExecutor(same_frame=True)
    session, task = build_session(tmp_path, executor)
    session.start([task["id"]])
    state = session.click({"x": 40, "y": 80, "width": 40, "height": 20},
                          "people_grid", "people_select", "选择 2 人", False)
    assert state["last_click"]["effect"]["changed"] is False
    assert "无效点击" in state["message"]
    state = session.complete()
    session.wait_background()
    state = session.state()
    assert state["status"] == "finished"
    assert state["completed"][0]["invalid"] == 1
    assert state["completed"][0]["archived"] == 0
    assert not (tmp_path / "素材" / "规则样本" / "manifest.csv").exists()


def test_current_steps_only_contains_current_store(tmp_path):
    executor = FakeExecutor()
    tasks = [
        {"id": "task-1", "source_name": "门店一", "source_path": str(tmp_path / "1.png"),
         "source_hash": "h1", "status": "pending"},
        {"id": "task-2", "source_name": "门店二", "source_path": str(tmp_path / "2.png"),
         "source_hash": "h2", "status": "pending"},
    ]
    session, _ = build_session(tmp_path, executor, tasks=tasks)
    session.start([task["id"] for task in tasks])
    state = session.click({"x": 40, "y": 80, "width": 40, "height": 20},
                          "people_grid", "people_select", "选择 2 人", False)
    assert len(state["current_steps"]) == 1
    assert state["current_steps"][0]["task_id"] == "task-1"
    state = session.complete()
    assert state["status"] == "closing"
    session.wait_background()
    state = session.state()
    assert state["index"] == 1
    assert state["status"] == "ready"
    assert state["task"]["id"] == "task-2"
    assert state["current_steps"] == []
    assert state["completed"][0]["task_id"] == "task-1"


def test_click_recaptures_when_current_frame_is_stale(tmp_path):
    executor = FakeExecutor()
    session, task = build_session(tmp_path, executor)
    session.start([task["id"]])
    session.wait_background()
    # 超过复用时效后必须重新截图，避免把过期画面当作点击前对照。
    session._frame_at = time.monotonic() - 120
    session.click({"x": 40, "y": 80, "width": 40, "height": 20},
                  "people_grid", "people_select", "选择 2 人", False)
    assert executor.screenshots == ["opened-normalized.png", "step-01-before.png", "step-01-after.png"]
    session.stop()


def test_suggestion_runs_in_background_and_keeps_page_responsive(tmp_path):
    release = threading.Event()
    rows = [{"text": "请选择用餐人数", "x": 20, "y": 100, "width": 160, "height": 30}]

    def slow_ocr(_):
        release.wait(5)
        return rows

    executor = FakeExecutor()
    session, task = build_session(tmp_path, executor, ocr=slow_ocr)
    state = session.start([task["id"]])
    # 首屏先返回，不等 OCR。
    assert state["status"] == "ready"
    assert state["suggestion"] is None
    release.set()
    session.wait_background()
    assert session.state()["suggestion"]["category"] == "people_grid"
    session.stop()


def test_activate_wechat_skips_open_when_wechat_is_frontmost(tmp_path, monkeypatch):
    executor = FakeExecutor()
    session, task = build_session(tmp_path, executor)
    session.activator = None
    monkeypatch.setattr("menu_capture.annotation.sys.platform", "darwin")
    monkeypatch.setattr("menu_capture.annotation.AnnotationSession._frontmost_bundle",
                        staticmethod(lambda: "com.tencent.xinWeChat"))
    calls = []
    monkeypatch.setattr("menu_capture.annotation.subprocess.run",
                        lambda *args, **kwargs: calls.append(args[0]) or types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("menu_capture.annotation.time.sleep", lambda _: None)
    session._activate_wechat()
    assert calls == []


def _suggestion(source="model", x=20.0, y=30.0, size=(200, 400)):
    return {"source": source, "target_label": "同意", "method": "local_exact_button",
            "x": x, "y": y, "image_size": {"width": size[0], "height": size[1]}}


def test_attribute_flags_model_wrong_when_human_picks_another_target():
    result = AnnotationSession._attribute(
        _suggestion("model"), {"x": 150.0, "y": 300.0}, (200, 400), {"changed": True})

    assert result["adopted"] is False
    assert result["attribution"] == "model_wrong"
    assert result["deviation_px"] > 24


def test_attribute_flags_local_rule_when_local_suggestion_is_rejected():
    result = AnnotationSession._attribute(
        _suggestion("local_prepare"), {"x": 150.0, "y": 300.0}, (200, 400), {"changed": True})

    assert result["attribution"] == "local_rule_wrong"


def test_attribute_flags_coordinate_when_adopted_but_click_had_no_effect():
    result = AnnotationSession._attribute(
        _suggestion("model"), {"x": 22.0, "y": 31.0}, (200, 400), {"changed": False})

    assert result["adopted"] is True
    assert result["attribution"] == "coordinate_or_target_wrong"


def test_attribute_ignores_suggestion_from_a_different_frame():
    result = AnnotationSession._attribute(
        _suggestion("model", size=(310, 1011)), {"x": 22.0, "y": 31.0}, (200, 400), {"changed": True})

    assert result["attribution"] == "suggestion_from_other_frame"
    assert result["deviation_px"] is None


def test_attribute_reports_missing_suggestion():
    result = AnnotationSession._attribute(None, {"x": 22.0, "y": 31.0}, (200, 400), {"changed": True})

    assert result["attribution"] == "no_suggestion"
    assert result["adopted"] is None


def test_decision_payload_exposes_source_and_box(tmp_path):
    runner = UiPathRunner(ConfigStore(tmp_path), TaskStore(tmp_path / "tasks.sqlite3"),
                          tmp_path / "profile.json")
    decision = SceneDecision(
        action="click", reason="本地规则", x=25.0, y=480.0, target_label="我已了解",
        target_box=Region(x=18, y=473, width=160, height=13),
        overlay_visible=True, login_required=False, phone_required=False,
        consent_required=True, transaction_visible=False,
    )

    payload = AnnotationSession._decision_payload(
        decision, "local_prepare", runner, (None, []),
        {"image_width": 310, "image_height": 1011}, None,
        meta={"chain": ["本地就绪", "本地动作"], "hit_stage": "local_prepare"})

    assert payload["source"] == "local_prepare"
    assert payload["source_detail"].startswith("本地规则 · OCR 文字定位")
    assert payload["chain"] == ["本地就绪", "本地动作"]
    assert payload["hit_stage"] == "local_prepare"
    assert payload["target_label"] == "我已了解"
    assert payload["box_px"] == {"x": 18.0, "y": 473.0, "width": 160.0, "height": 13.0}
    assert payload["image_size"] == {"width": 310, "height": 1011}
    assert payload["error"] is None


def test_decision_source_detail_separates_local_and_model():
    assert "本地规则" in AnnotationSession._describe_source("local_ready", None, None, None)
    assert "多尺度 OCR" in AnnotationSession._describe_source(
        "local_fused_prepare", "local_exact_button", None, None)
    assert AnnotationSession._describe_source(
        "local_prepare", "local_template_button", None, None) == "素材匹配 · 模板定位"
    assert AnnotationSession._describe_source(
        "model", None, "local", "model") == "模型判断 · 坐标来自本地解析 · 区域来自模型"


def test_progress_context_tracks_people_selection_and_consent(tmp_path):
    session = object.__new__(AnnotationSession)
    session.settings = Settings(input_dir=str(tmp_path / "in"), output_dir=str(tmp_path / "out"))
    session.people_selected = False
    session.consent_checked = False

    assert session._update_progress_context(
        {"method": "local_people_grid_ocr", "target_label": "1"}, "", "人数选择") == "people_selected"
    assert session.people_selected is True
    assert session._update_progress_context(
        {"method": "local_people_grid_ocr", "target_label": "1"}, "", "人数选择") is None
    assert session._update_progress_context(
        {"method": "local_consent_checkbox_first"}, "隐私协议", "授权") == "consent_checked"
    assert session.consent_checked is True


def test_decide_chain_passes_selected_people_context_to_local_rules(tmp_path):
    """已选人数后必须把进度传给本地规则，否则会一直建议重复点人数而不去点“开始点餐”。"""
    session = object.__new__(AnnotationSession)
    session.configs = ConfigStore(tmp_path / "cfg")
    session.people_selected = True
    session.consent_checked = True
    settings = Settings(input_dir=str(tmp_path / "in"), output_dir=str(tmp_path / "out"))
    runner = object.__new__(UiPathRunner)
    runner.ocr_reader = object()
    seen: dict = {}
    runner._local_ready_preflight = lambda path, name, ocr: None

    def fake_local(ocr, people, auto, people_selected, **kwargs):
        seen["people_selected"] = people_selected
        seen["consent_checked"] = kwargs.get("consent_checked")
        return None

    runner._local_prepare_action = fake_local
    runner._read_fused_scaled_ocr = lambda path, base: (None, [])

    session._decide_with_chain(runner, tmp_path / "f.png", {"source_name": "店"}, settings, ([], []))

    assert seen == {"people_selected": True, "consent_checked": True}


def test_copy_before_reuses_the_same_file_after_a_failed_click(tmp_path):
    """点击失败后当前画面已经是本步 before，再次确认不能因为自复制而报 SameFileError。"""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    frame_path = evidence / "step-01-before.png"
    frame_path.write_bytes(b"png")
    session = object.__new__(AnnotationSession)
    session.work_dir = str(tmp_path)

    result = session._copy_before(1, {"path": str(frame_path), "width": 310, "height": 1011})

    assert Path(result["path"]) == frame_path
    assert frame_path.read_bytes() == b"png"
    assert result["width"] == 310


def test_copy_before_saves_a_separate_copy_from_the_live_frame(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    live = tmp_path / "frame-0001.png"
    live.write_bytes(b"png")
    session = object.__new__(AnnotationSession)
    session.work_dir = str(tmp_path)

    result = session._copy_before(2, {"path": str(live), "width": 310, "height": 1011})

    assert Path(result["path"]) == evidence / "step-02-before.png"
    assert (evidence / "step-02-before.png").read_bytes() == b"png"


def _stub_chain(*, ready=None, local=None, fused=None, fused_ready=None, fused_local=None):
    """按真实调用顺序记录本地就绪 → 本地动作 → 多尺度 OCR → 再判定的命中情况。"""
    runner = object.__new__(UiPathRunner)
    runner.ocr_reader = object()
    calls: list[str] = []
    fused_rows = (fused or (None, []))[1]
    fused_ready_result = [fused_ready]

    def fake_ready(path, name, ocr):
        calls.append("ready")
        if ocr[1] is fused_rows:
            return fused_ready_result[0]
        return ready

    def fake_local(ocr, people, auto, people_selected, **_kwargs):
        calls.append("local")
        return fused_local if ocr[1] is fused_rows else local

    def fake_fused(path, base):
        calls.append("fused")
        return fused or (None, [])

    runner._local_ready_preflight = fake_ready
    runner._local_prepare_action = fake_local
    runner._read_fused_scaled_ocr = fake_fused
    runner._target_candidates = lambda ocr: []
    return runner, calls


def test_decide_chain_matches_batch_order_and_reports_hit_stage(tmp_path):
    session = object.__new__(AnnotationSession)
    session.configs = ConfigStore(tmp_path / "cfg")
    session.people_selected = False
    session.consent_checked = False
    ready_decision = SceneDecision(
        action="ready", reason="本地核验", menu_region=Region(x=0, y=0, width=10, height=10),
        overlay_visible=False, login_required=False, phone_required=False,
        consent_required=False, transaction_visible=False,
        item_names_visible=["菜"], prices_visible=["¥1"],
    )
    runner, calls = _stub_chain(ready=ready_decision)
    settings = Settings(input_dir=str(tmp_path / "in"), output_dir=str(tmp_path / "out"))

    result = session._decide_with_chain(runner, tmp_path / "f.png", {"source_name": "店"}, settings, ([], []))

    assert calls == ["ready"]
    assert result["source"] == "local_ready"
    assert result["hit_stage"] == "local_ready"
    assert result["decision"] is ready_decision


def test_decide_chain_reports_fused_ocr_hit(tmp_path):
    session = object.__new__(AnnotationSession)
    session.configs = ConfigStore(tmp_path / "cfg")
    session.people_selected = False
    session.consent_checked = False
    settings = Settings(input_dir=str(tmp_path / "in"), output_dir=str(tmp_path / "out"))
    local_decision = SceneDecision(
        action="click", reason="本地", x=5.0, y=6.0, target_label="扫码点餐",
        overlay_visible=True, login_required=False, phone_required=False,
        consent_required=False, transaction_visible=False,
    )
    runner, calls = _stub_chain(
        fused=(["img"], [{"text": "扫码点餐"}]), fused_local=local_decision)

    result = session._decide_with_chain(runner, tmp_path / "f.png", {"source_name": "店"},
                                        settings, ([], []))

    assert calls == ["ready", "local", "fused", "ready", "local"]
    assert result["source"] == "local_fused_prepare"
    assert result["hit_stage"] == "fused_ocr"
    assert result["chain"] == ["本地就绪", "本地动作", "多尺度OCR"]
    assert result["decision"] is local_decision


def test_decide_chain_records_model_usage_as_annotation(tmp_path, monkeypatch):
    from menu_capture.model_usage import UsageLedger

    ledger = UsageLedger(TaskStore(tmp_path / "tasks.sqlite3"))
    session = object.__new__(AnnotationSession)
    session.configs = ConfigStore(tmp_path / "cfg")
    session.people_selected = False
    session.consent_checked = False
    session.usage_ledger = ledger
    settings = Settings(input_dir=str(tmp_path / "in"), output_dir=str(tmp_path / "out"))
    runner, _ = _stub_chain()
    decision = SceneDecision(
        action="click", reason="模型", x=1.0, y=2.0, target_label="开始点餐",
        overlay_visible=False, login_required=False, phone_required=False,
        consent_required=False, transaction_visible=False,
    )

    class FakeVision:
        def __init__(self, config, key, *, usage_ledger=None):
            assert usage_ledger is ledger

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def decide(self, *_args, **_kwargs):
            call_id = ledger.start_call(provider="p", model="m")
            ledger.finish_call(call_id, status="success", elapsed_ms=5, http_status=200,
                               response={"usage": {"prompt_tokens": 3, "completion_tokens": 4}})
            return decision

    monkeypatch.setattr("menu_capture.annotation.VisionModel", FakeVision)
    monkeypatch.setattr("menu_capture.annotation.SceneAgent", FakeAgent)

    result = session._decide_with_chain(runner, tmp_path / "f.png",
                                        {"id": "t1", "source_name": "测试店"}, settings, ([], []))

    calls = ledger.query()["calls"]
    assert result["source"] == "model"
    assert len(calls) == 1
    assert calls[0]["source"] == "annotation"
    assert calls[0]["task_id"] == "t1"
    assert calls[0]["source_name"] == "测试店"


def test_archive_samples_records_suggestion_and_attribution(tmp_path):
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    Image.new("RGB", (20, 40), "white").save(before)
    Image.new("RGB", (20, 40), "gray").save(after)
    settings = Settings(input_dir=str(tmp_path / "in"), output_dir=str(tmp_path / "out"))
    step = {
        "index": 1, "task_id": "task-1", "category": "consent_checkbox", "page_state": "consent",
        "target": "同意", "before": {"path": str(before)}, "after": {"path": str(after)},
        "box_px": {"x": 4, "y": 8, "width": 10, "height": 10},
        "center_points": {"x": 9.0, "y": 13.0},
        "suggestion": {"source": "model", "target_label": "同意", "method": "local_exact_button"},
        "attribution": {"adopted": True, "deviation_px": 2.5, "attribution": "none"},
    }

    archive_samples(tmp_path / "samples", {"id": "task-1", "source_name": "测试门店"},
                    settings, [step], "session-1")

    with (tmp_path / "samples" / "manifest.csv").open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["suggestion_source"] == "model"
    assert rows[0]["suggestion_label"] == "同意"
    assert rows[0]["adopted"] == "True"
    assert rows[0]["deviation_px"] == "2.5"
    assert rows[0]["attribution"] == "none"


def test_history_reads_archived_session_steps(tmp_path):
    session, task = build_session(tmp_path, FakeExecutor())
    session_dir = Path(session.configs.load().output_dir) / "诊断" / "annotation" / "20261106-120000-demo"
    steps_dir = session_dir / "steps"
    steps_dir.mkdir(parents=True)
    Image.new("RGB", (200, 400), "white").save(session_dir / "step-01-before.png")
    Image.new("RGB", (200, 400), "gray").save(session_dir / "step-01-after.png")
    step = {
        "index": 1, "task_id": task["id"], "at": "2026-11-06T12:00:01+08:00",
        "category": "menu_entry", "page_state": "store_home", "target": "扫码点餐",
        "box_px": {"x": 10, "y": 20, "width": 60, "height": 24},
        "center_points": {"x": 40.0, "y": 32.0},
        "effect": {"changed": True, "diff_score": 12},
        "before": {"name": "step-01-before.png", "width": 200, "height": 400},
        "after": {"name": "step-01-after.png", "width": 200, "height": 400},
    }
    (steps_dir / "001.json").write_text(json.dumps(step, ensure_ascii=False), encoding="utf-8")
    (session_dir / "session.json").write_text(
        json.dumps({"started_at": "2026-11-06T12:00:00+08:00",
                    "finished_at": "2026-11-06T12:05:00+08:00"}), encoding="utf-8")

    history = session.history(task["id"])

    assert history["session_id"] == "20261106-120000-demo"
    assert history["finished_at"] == "2026-11-06T12:05:00+08:00"
    assert len(history["steps"]) == 1
    public = history["steps"][0]
    assert public["box_px"]["width"] == 60
    assert public["before"]["url"] == (
        "/api/annotation/frame?session=20261106-120000-demo&name=step-01-before.png")
    assert "path" not in public["before"]
    frame = session.frame_path("20261106-120000-demo", "step-01-before.png")
    assert frame.read_bytes()


def test_frame_path_rejects_unknown_or_escaping_session(tmp_path):
    session, _ = build_session(tmp_path, FakeExecutor())
    with pytest.raises(FileNotFoundError):
        session.frame_path("../../etc", "passwd")
    with pytest.raises(FileNotFoundError):
        session.frame_path("not-exists", "step-01-before.png")


def _write_pair(tmp_path, *, paint):
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    Image.new("RGB", (200, 400), "white").save(before)
    image = Image.new("RGB", (200, 400), "white")
    paint(image)
    image.save(after)
    return before, after


def test_image_effect_detects_small_control_change_via_click_region(tmp_path):
    # 选人数按钮的选中态只改几十个像素，全图均值差远低于 1.5，必须靠局部区域识别。
    def paint(image):
        for x in range(100, 114):
            for y in range(200, 214):
                image.putpixel((x, y), (200, 40, 40))

    before, after = _write_pair(tmp_path, paint=paint)
    global_only = image_effect(before, after)
    effect = image_effect(before, after,
                          box={"x": 92, "y": 192, "width": 30, "height": 30},
                          center={"x": 107, "y": 207})

    assert global_only["diff_score"] < 1.5
    assert global_only["changed"] is False
    assert effect["changed"] is True
    assert effect["roi_pixels"] >= 24


def test_image_effect_ignores_changes_outside_the_click_region(tmp_path):
    def paint(image):
        for x in range(10, 30):
            for y in range(20, 40):
                image.putpixel((x, y), (10, 10, 10))

    before, after = _write_pair(tmp_path, paint=paint)
    effect = image_effect(before, after,
                          box={"x": 92, "y": 192, "width": 30, "height": 30},
                          center={"x": 107, "y": 207})

    assert effect["roi_pixels"] == 0
    assert effect["changed"] is False


def test_image_effect_still_flags_large_page_change(tmp_path):
    def paint(image):
        for y in range(0, 400, 4):
            for x in range(0, 200, 2):
                image.putpixel((x, y), (30, 30, 30))

    before, after = _write_pair(tmp_path, paint=paint)
    effect = image_effect(before, after,
                          box={"x": 92, "y": 192, "width": 30, "height": 30},
                          center={"x": 107, "y": 207})

    assert effect["diff_score"] >= 1.5
    assert effect["changed"] is True
