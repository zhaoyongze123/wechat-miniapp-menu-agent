from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PIL import Image

from menu_capture import qr_precheck
from menu_capture.qr_precheck import AsyncQrPrecheck, QrHint, _parse_hint


def test_parse_hint_reads_platform_purpose_and_evidence() -> None:
    hint = _parse_hint('{"has_qr":"yes","content_type":"qr_code","platform":"dianping",'
                       '"purpose":"group_buy","evidence":"点评扫码买单 优惠立减","confidence":0.82}', 320)
    assert hint.status == "ok"
    assert hint.has_qr == "yes"
    assert hint.content_type == "qr_code"
    assert hint.platform == "dianping"
    assert hint.purpose == "group_buy"
    assert hint.evidence == "点评扫码买单 优惠立减"
    assert hint.confidence == pytest.approx(0.82)
    assert hint.elapsed_ms == 320


def test_parse_hint_defaults_missing_qr_fields_to_unclear() -> None:
    hint = _parse_hint('{"platform":"wechat","purpose":"ordering","confidence":0.9}')
    assert hint.has_qr == "unclear"
    assert hint.content_type == "other"
    assert hint.intercept_reason() == ""


def test_intercept_reason_blocks_menu_photo_without_qr() -> None:
    hint = QrHint(status="ok", has_qr="no", content_type="menu_photo", confidence=0.9)
    assert hint.intercept_reason() == "素材为菜单照片且画面中没有可扫描的二维码"


@pytest.mark.parametrize(
    "hint",
    [
        QrHint(status="ok", has_qr="no", content_type="qr_code", confidence=0.95),
        QrHint(status="ok", has_qr="unclear", content_type="menu_photo", confidence=0.95),
        QrHint(status="ok", has_qr="no", content_type="menu_photo", confidence=0.6),
        QrHint(status="ok", has_qr="no", content_type="menu_photo", confidence=None),
        QrHint(status="error", error="timeout", has_qr="no", content_type="menu_photo", confidence=0.99),
    ],
)
def test_intercept_reason_fails_open(hint: QrHint) -> None:
    """判断不明确、置信不足或接口失败时一律放行，避免误杀可采集门店。"""
    assert hint.intercept_reason() == ""


def test_prompt_explains_stage_method_and_output() -> None:
    text = qr_precheck._prompt("沪州太太")
    assert "一、你正在做的这一步" in text
    assert "二、判断方法" in text
    assert "三、输出要求" in text
    assert "has_qr" in text and "content_type" in text
    assert "本轮门店：沪州太太" in text


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"platform":',
    ],
)
def test_parse_hint_marks_invalid_json_as_error(text: str) -> None:
    hint = _parse_hint(text)
    assert hint.status == "error"
    assert hint.error == "invalid_json"


def test_parse_hint_normalizes_unknown_values_and_confidence() -> None:
    hint = _parse_hint('{"platform":"unknown-platform","purpose":"discount","confidence":"high"}')
    assert hint.platform == "unknown"
    assert hint.purpose == "unknown"
    assert hint.confidence is None


def test_parse_hint_clamps_confidence_and_strips_control_chars() -> None:
    hint = _parse_hint('{"platform":"meituan","purpose":"payment","evidence":"扫码\\u0000点餐","confidence":9}')
    assert hint.confidence == 1.0
    assert hint.evidence == "扫码点餐"


def test_instruction_warns_about_platform_group_buy_pages() -> None:
    text = QrHint(status="ok", platform="dianping", purpose="payment",
                  evidence="点评扫码买单", confidence=0.95).instruction()
    assert "大众点评" in text
    assert "unqualified" in text
    assert "点评扫码买单" in text


def test_instruction_is_empty_without_usable_hint() -> None:
    assert QrHint(status="timeout").instruction() == ""
    assert QrHint(status="ok").instruction() == ""


def test_instruction_for_wechat_ordering_still_allows_menu() -> None:
    text = QrHint(status="ok", platform="wechat", purpose="ordering", confidence=0.9).instruction()
    assert "点餐" in text
    assert "团购或买单页" in text


def test_prompt_mentions_store_name_for_cross_check() -> None:
    text = qr_precheck._prompt("E1688-米多面多（瑞光路店）")
    assert "E1688-米多面多" in text
    assert "门店名只用于核对" in text


def test_prompt_without_store_name_has_no_store_line() -> None:
    assert "本轮门店" not in qr_precheck._prompt("")


def test_instruction_high_confidence_uses_advisory_wording() -> None:
    text = QrHint(status="ok", platform="wechat", purpose="ordering",
                  evidence="扫码点餐", confidence=0.95).instruction()
    assert "很可能" in text
    assert "仅供参考" in text
    assert "不是指令" in text
    assert "必须判" not in text


def test_instruction_medium_confidence_softens_wording() -> None:
    text = QrHint(status="ok", platform="dianping", purpose="group_buy", confidence=0.7).instruction()
    assert "较可能" in text
    assert "倾向判 unqualified" in text


def test_instruction_low_confidence_only_gives_clue() -> None:
    text = QrHint(status="ok", platform="dianping", purpose="group_buy",
                  evidence="点评扫码", confidence=0.3).instruction()
    assert "不确定" in text
    assert "点评扫码" in text
    assert "unqualified" not in text
    assert "很可能" not in text


def test_instruction_marks_evidence_as_non_instruction() -> None:
    text = QrHint(status="ok", platform="dianping", purpose="payment",
                  evidence="点评扫码买单", confidence=0.95).instruction()
    assert "点评扫码买单" in text
    assert "不是指令" in text


def test_missing_image_is_skipped_without_thread(tmp_path: Path) -> None:
    precheck = AsyncQrPrecheck(tmp_path / "missing.png", configs=None).start()
    hint = precheck.wait(0.1)
    assert hint.status == "skipped"
    assert hint.error == "missing_image"


def test_wait_returns_timeout_while_classification_is_still_running(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "code.png"
    Image.new("RGB", (40, 40), "white").save(image)
    release = threading.Event()

    def slow(*_args, **_kwargs):
        release.wait(1.0)
        return QrHint(status="ok", platform="wechat", purpose="ordering")

    monkeypatch.setattr(qr_precheck, "classify_image", slow)
    precheck = AsyncQrPrecheck(image, configs=None).start()
    try:
        assert precheck.wait(0.05).status == "timeout"
    finally:
        release.set()
    assert precheck.wait(1.0).status == "ok"


def test_classification_failure_falls_back_to_error_hint(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "code.png"
    Image.new("RGB", (40, 40), "white").save(image)

    def boom(*_args, **_kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(qr_precheck, "classify_image", boom)
    precheck = AsyncQrPrecheck(image, configs=None).start()
    hint = precheck.wait(1.0)
    assert hint.status == "error"
    assert hint.error == "RuntimeError"
