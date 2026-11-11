from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PIL import Image

from menu_capture import qr_precheck
from menu_capture.qr_precheck import AsyncQrPrecheck, QrHint, _parse_hint


def test_parse_hint_reads_platform_purpose_and_evidence() -> None:
    hint = _parse_hint('{"platform":"dianping","purpose":"group_buy",'
                       '"evidence":"点评扫码买单 优惠立减","confidence":0.82}', 320)
    assert hint.status == "ok"
    assert hint.platform == "dianping"
    assert hint.purpose == "group_buy"
    assert hint.evidence == "点评扫码买单 优惠立减"
    assert hint.confidence == pytest.approx(0.82)
    assert hint.elapsed_ms == 320


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
    text = QrHint(status="ok", platform="dianping", purpose="payment", evidence="点评扫码买单").instruction()
    assert "大众点评" in text
    assert "unqualified" in text
    assert "点评扫码买单" in text


def test_instruction_is_empty_without_usable_hint() -> None:
    assert QrHint(status="timeout").instruction() == ""
    assert QrHint(status="ok").instruction() == ""


def test_instruction_for_wechat_ordering_still_allows_menu() -> None:
    text = QrHint(status="ok", platform="wechat", purpose="ordering").instruction()
    assert "点餐" in text
    assert "团购或买单页" in text


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
