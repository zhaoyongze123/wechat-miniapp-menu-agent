from __future__ import annotations

from pathlib import Path

import pytest

from menu_capture.vision import qr_detection


def _probe(monkeypatch, platform: str, available: dict[str, bool], ocr: dict | None = None,
           worker: bool = False) -> dict:
    monkeypatch.setattr(qr_detection.sys, "platform", platform)
    monkeypatch.setattr(qr_detection, "_module_available", lambda name: available.get(name, False))
    monkeypatch.setattr(qr_detection, "_package_version", lambda *names: "9.9.9")
    monkeypatch.setattr(qr_detection, "_worker_installed", lambda: worker)
    monkeypatch.setattr(
        qr_detection, "_ocr_capability",
        lambda: ocr or {"backend": "tesseract", "available": True, "detail": "OCR Tesseract"},
    )
    return qr_detection.probe_detector_capability()


def test_linux_probe_reports_shared_opencv_zxing_capability(monkeypatch) -> None:
    capability = _probe(
        monkeypatch, "linux", {"cv2": True, "zxingcpp": True},
        ocr={"backend": "rapidocr", "available": True, "detail": "OCR RapidOCR"}, worker=True,
    )
    assert capability["available"] is True
    assert capability["backend"] == "opencv_zxing"
    assert capability["degraded"] is False
    assert capability["opencv_version"] == "9.9.9"
    assert capability["zxingcpp_version"] == "9.9.9"
    assert capability["mini_program_worker"] == {"available": True, "enabled": False}


def test_linux_probe_marks_missing_zxing_and_ocr_fallback_as_degraded(monkeypatch) -> None:
    capability = _probe(monkeypatch, "linux", {"cv2": True, "zxingcpp": False})
    assert capability["available"] is True
    assert capability["degraded"] is True
    assert "ZXing-C++ 缺失" in capability["detail"]


def test_linux_probe_reports_systemic_failure_without_opencv(monkeypatch) -> None:
    capability = _probe(monkeypatch, "linux", {"cv2": False})
    assert capability["available"] is False
    assert "OpenCV 缺失" in capability["detail"]
    with pytest.raises(qr_detection.DetectorUnavailable):
        qr_detection.require_detector()


def test_darwin_probe_requires_apple_vision(monkeypatch) -> None:
    capability = _probe(monkeypatch, "darwin", {"Vision": False})
    assert capability["available"] is False
    assert capability["backend"] == "apple_vision"


def test_detect_qr_dispatches_apple_vision_on_darwin(monkeypatch) -> None:
    monkeypatch.setattr(qr_detection.sys, "platform", "darwin")
    monkeypatch.setattr(qr_detection, "apple_vision_detections", lambda _path: [{"marker": "vision"}])
    assert qr_detection.detect_qr(Path("x.png")) == [{"marker": "vision"}]


def test_detect_qr_uses_shared_opencv_implementation_on_linux(monkeypatch) -> None:
    monkeypatch.setattr(qr_detection.sys, "platform", "linux")
    monkeypatch.setattr(qr_detection, "qr_detections", lambda _path: [{"marker": "opencv"}])
    assert qr_detection.detect_qr(Path("x.png")) == [{"marker": "opencv"}]


def test_detect_qr_raises_detector_unavailable_when_opencv_missing(monkeypatch) -> None:
    monkeypatch.setattr(qr_detection.sys, "platform", "linux")

    def missing():
        raise qr_detection.DetectorUnavailable("OpenCV 不可用")

    monkeypatch.setattr(qr_detection, "_opencv", missing)
    with pytest.raises(qr_detection.DetectorUnavailable):
        qr_detection.detect_qr(Path("x.png"))
