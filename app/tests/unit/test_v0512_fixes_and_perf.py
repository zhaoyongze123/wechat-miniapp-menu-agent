"""v0.5.12 回归修复与性能优化定向测试。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from menu_capture.capture.uipath_runner import NeedsReview, UiPathRunner
from menu_capture.materials.rejections import RejectionService
from menu_capture.storage.store import TaskStore
from menu_capture.vision.model import parse_json


class TestParseJsonResilience:
    """修复 AI 门店名称识别遇到尾部多余文本导致 Extra data 报错。"""

    def test_extra_data_after_json_object(self):
        raw = '{"store_name":"老乡鸡(临港万达店)","reason":"页面顶部标题可见"}\n注：根据截图左上角识别。'
        parsed = parse_json(raw)
        assert parsed == {"store_name": "老乡鸡(临港万达店)", "reason": "页面顶部标题可见"}

    def test_think_tag_and_fenced_json_with_trailing_prose(self):
        raw = (
            "<think>先看标题栏，显示的是杨记隆府。</think>\n"
            "识别结果如下：\n"
            "```json\n"
            '{"store_name":"杨记隆府(桐梓林店)","reason":"顶部导航栏可见"}\n'
            "```\n"
            "以上为本次识别结果。"
        )
        parsed = parse_json(raw)
        assert parsed["store_name"] == "杨记隆府(桐梓林店)"

    def test_two_concatenated_json_objects_returns_first(self):
        raw = '{"store_name":"随和家宴","reason":"顶部可见"}{"extra":true}'
        parsed = parse_json(raw)
        assert parsed["store_name"] == "随和家宴"

    def test_non_dict_or_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_json("这里没有任何 JSON 对象")
        with pytest.raises(ValueError):
            parse_json('["not", "a", "dict"]')


class TestWorkerReadinessSharedMode:
    """修复共享采集端 profile 残留旧任务 ID 误报“请先选择用于采集的发布版本”。"""

    def test_validate_source_shared_message(self):
        runner = UiPathRunner.__new__(UiPathRunner)
        with pytest.raises(NeedsReview, match="所选门店分配已完成或已收回"):
            runner._validate_source({"id": "t1"}, "", trial_source=True)
        with pytest.raises(NeedsReview, match="请先选择用于采集的发布版本"):
            runner._validate_source({"id": "t1"}, "", trial_source=False)

    def test_idle_readiness_ignores_stale_shared_task_ids(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.sqlite3")
        profile_path = tmp_path / "uipath.profile.json"
        profile_path.write_text(json.dumps({
            "mode": "automatic",
            "launch_mode": "manual",
            "selected_task_ids": ["stale-task-1", "stale-task-2"],
            "paste_shortcut": "^v",
            "send_key": "{ENTER}",
            "close_key": "^w",
            "wechat_input_x_ratio": 0.5,
            "wechat_input_y_ratio": 0.85,
            "coordinates_verified": True,
            "wechat_selector": "wechat",
            "preview_selector": "preview",
        }), encoding="utf-8")

        settings = SimpleNamespace(input_dir="", output_dir=str(tmp_path / "out"))
        configs = MagicMock()
        configs.load.return_value = settings

        runner = UiPathRunner.__new__(UiPathRunner)
        runner.store = store
        runner.configs = configs
        runner.profile_path = profile_path
        runner.project_dir = tmp_path
        runner.assignment_source = lambda _tid: ""  # 所有旧任务均已完成/收回
        runner.clipboard_preparer = lambda _p: None
        runner.clipboard_text_preparer = lambda _t: None
        runner._last_readiness = None

        result = runner.readiness(capture_mode="manual")
        assert result["selection_missing"] is True
        assert all("请先选择用于采集的发布版本" not in issue for issue in result["issues"])
        assert len(result["issues"]) == 1


class TestBatchStoreNameRowsPerformance:
    """验证 store_name_rows 与 _all 使用批量接口而非逐条 N+1 查库。"""

    def test_store_name_rows_uses_batch_providers(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.sqlite3")
        with store.connect() as db:
            for i in range(15):
                img = tmp_path / f"store_{i}" / "qr.png"
                img.parent.mkdir(parents=True, exist_ok=True)
                img.write_bytes(b"fake")
                db.execute(
                    "INSERT INTO tasks(id,source_name,source_path,source_hash,status,stage,message,output_dir,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"t{i}", f"Store-{i}", str(img), f"hash-{i}", "captured", "done", "", "", "2026-11-27T10:00:00+08:00"),
                )

        batch_proj_calls = 0
        batch_name_calls = 0

        def proj_batch(ids):
            nonlocal batch_proj_calls
            batch_proj_calls += 1
            return {tid: "proj-1" for tid in ids}

        def names_batch(ids, project_id=None):
            nonlocal batch_name_calls
            batch_name_calls += 1
            return {tid: f"Observed-{tid}" for tid in ids}

        svc = RejectionService(
            store,
            materials=None,
            observed_names_batch=names_batch,
            task_project_ids_provider=proj_batch,
        )
        rows = svc.store_name_rows(project_id="proj-1")
        assert len(rows) == 15
        assert batch_proj_calls == 1
        assert batch_name_calls == 1
        assert rows[0]["observed_store_name"].startswith("Observed-")
