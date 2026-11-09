from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
import pytest

from menu_capture.scene_agent import SceneDecision
from menu_capture.model import Region
from menu_capture.uipath_runner import UiPathRunner


ROOT = Path(__file__).resolve().parents[1]
REAL_RESTING_MENU = ROOT / "data/action-evidence/a861554b3c9c4290a6186ba2f95f840f.png"
REAL_OCR_LOG = ROOT / "data/debug-logs/segments/20261105-222155-646412-0001.jsonl"


def _row(text: str, x: float, y: float, width: float, height: float = 36,
         confidence: float = 1.0) -> dict:
    return {"text": text, "x": x, "y": y, "width": width, "height": height,
            "confidence": confidence}


def _real_action_ocr_rows() -> list[dict]:
    """原样读取 action 1283 的完整 OCR，避免用手工删减行替代现场输入。"""
    if not REAL_OCR_LOG.is_file():
        pytest.skip("本地未提供真实 OCR 日志")
    with REAL_OCR_LOG.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("action_id") == 1283:
                rows = record.get("details", {}).get("ocr_rows")
                assert isinstance(rows, list) and rows
                return rows
    raise AssertionError("原始日志中找不到 action 1283 OCR")


def _runner() -> UiPathRunner:
    runner = UiPathRunner.__new__(UiPathRunner)
    runner.ocr_reader = lambda _path: []
    runner.debug_log = object()  # 阻止诊断摘要写入截图目录。
    runner._last_ocr_meta = {}
    return runner


def _decision(**overrides) -> SceneDecision:
    values = {
        "action": "blocked",
        "reason": "页面底部显示‘当前门店已休息’，门店处于非营业状态。",
        "x": None, "y": None, "pixels": 0, "menu_region": None,
        "categories": [], "overlay_visible": False, "login_required": False,
        "phone_required": False, "consent_required": False, "transaction_visible": False,
        "target_label": "", "item_names_visible": [], "prices_visible": [],
        "observed_store_text": [], "selected_people_count": None, "expects_transition": False,
    }
    values.update(overrides)
    return SceneDecision(**values)


def test_real_resting_screenshot_is_promoted_from_local_menu_layout() -> None:
    if not REAL_RESTING_MENU.is_file() or not REAL_OCR_LOG.is_file():
        pytest.skip("本地未提供真实休息截图或对应 OCR 日志")
    with Image.open(REAL_RESTING_MENU) as image:
        assert image.size == (310, 1312)
    rows = _real_action_ocr_rows()
    assert any(row.get("text") == "当前门店已休息" for row in rows)
    assert any(row.get("text") == "订单" for row in rows)
    runner = _runner()
    result = runner._resolve_ready_region(
        REAL_RESTING_MENU, _decision(), ocr_result=((310, 1312), rows)
    )
    assert result.action == "ready"
    assert result.ready_evidence_ok
    assert {"烤鸭", "配菜"}.issubset(result.categories)
    assert "经典蒸饼" in result.item_names_visible
    assert "¥3/包" in result.prices_visible
    assert result.menu_region is not None
    assert 250 < result.menu_region.y < 400
    assert "距离" not in " ".join(result.categories)


def test_resting_menu_is_not_promoted_when_authorization_overlay_is_flagged() -> None:
    result = _runner()._resolve_ready_region(
        REAL_RESTING_MENU, _decision(consent_required=True),
        ocr_result=((310, 1312), _real_action_ocr_rows()),
    )
    assert result.action == "blocked"


def test_service_paused_text_is_not_treated_as_resting_menu() -> None:
    rows = _real_action_ocr_rows() + [_row("服务暂停", 30, 3500, 100)]
    result = _runner()._resolve_ready_region(
        REAL_RESTING_MENU, _decision(), ocr_result=((310, 1312), rows)
    )
    assert result.action == "blocked"


def test_rest_reason_containing_service_error_is_not_overridden() -> None:
    rows = _real_action_ocr_rows()
    reason = "当前门店已休息，但服务暂停，无法提供菜单。"
    result = _runner()._resolve_ready_region(
        REAL_RESTING_MENU, _decision(reason=reason), ocr_result=((310, 1312), rows)
    )
    assert result.action == "blocked"


def test_resting_notice_on_blank_page_is_not_enough_to_promote() -> None:
    rows = [_row("当前门店已休息", 348.75, 3757.8, 228.6)]
    result = _runner()._resolve_ready_region(
        REAL_RESTING_MENU, _decision(), ocr_result=((310, 1312), rows)
    )
    assert result.action == "blocked"


def test_no_region_after_inference_keeps_original_blocked_decision() -> None:
    runner = _runner()
    # 模拟候选抽取阶段已有菜名/价格结论，但标准 region resolver 因缺少坐标锚点无法定位。
    runner._resting_menu_ocr_evidence = lambda *_args: {
        "categories": ["烤鸭", "配菜"],
        "item_names_visible": ["经典蒸饼"],
        "prices_visible": ["¥3/包"],
    }
    rows = [_row("当前门店已休息", 348.75, 3757.8, 228.6)]
    result = runner._resolve_ready_region(
        REAL_RESTING_MENU, _decision(), ocr_result=((310, 1312), rows)
    )
    assert result.action == "blocked"


def test_narrow_ready_region_is_recomputed_when_only_resting_notice_is_visible() -> None:
    def row(text: str, x: float, y: float, width: float, height: float = 30) -> dict:
        return {"text": text, "x": x * 3, "y": y * 3, "width": width * 3,
                "height": height * 3, "confidence": 0.95}

    rows = [
        row("烤鸭", 10, 300, 40),
        row("配菜", 10, 400, 40),
        row("火刻北京烤鸭", 120, 300, 150, 40),
        row("¥79/份起", 120, 350, 90),
        row("当前门店已休息", 120, 900, 150),
    ]
    decision = _decision(
        action="ready",
        overlay_visible=True,
        categories=["烤鸭", "配菜"],
        item_names_visible=["火刻北京烤鸭"],
        prices_visible=["¥79/份起"],
        menu_region=Region(x=41.76, y=467.09, width=54.13, height=555.05),
    )

    result = _runner()._resolve_ready_region(
        REAL_RESTING_MENU, decision, "", ((310, 1011), rows)
    )

    assert result.action == "ready"
    assert result.overlay_visible is False
    assert result.menu_region is not None
    assert result.menu_region.width > 310 * 0.2
    assert result.menu_region.x < 100
