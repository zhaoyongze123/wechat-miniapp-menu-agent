"""macOS 原生能力：权限查询、Apple Vision 文字识别与二维码定位。

仅读取本机权限状态和已保存的图片，不做任何鼠标、键盘或窗口操作；
桌面交互统一由 UiPath 执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ApplicationServices
import Quartz
import Vision
from Foundation import NSError, NSURL


@dataclass
class Window:
    id: int
    pid: int
    title: str
    x: float
    y: float
    width: float
    height: float


@dataclass
class Frame:
    path: Path
    window: Window
    width: int
    height: int


class MacDesktop:
    """只提供只读能力：权限状态与图像识别。"""

    def permissions(self) -> dict[str, bool]:
        """只查询权限状态，不请求授权，也不打开系统设置。"""
        accessibility = bool(ApplicationServices.AXIsProcessTrusted())
        preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        screen_recording = bool(preflight()) if preflight else False
        return {"accessibility": accessibility, "screen_recording": screen_recording}

    def ocr(self, frame: Frame) -> list[dict[str, Any]]:
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(["zh-Hans", "en-US"])
        request.setUsesLanguageCorrection_(True)
        url = NSURL.fileURLWithPath_(str(frame.path.expanduser().resolve(strict=True)))
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        success, error = handler.performRequests_error_([request], None)
        if not success:
            # NSError 明确保留桥接错误信息，避免 Foundation 错误被吞掉。
            detail = str(error) if isinstance(error, NSError) else repr(error)
            raise RuntimeError(f"Apple Vision OCR 失败：{detail}")
        observations = request.results() or []
        output: list[dict[str, Any]] = []
        for observation in observations:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            candidate = candidates[0]
            box = observation.boundingBox()
            # Vision 框原点在左下角且归一化；转成截图左上角像素坐标。
            output.append(
                {
                    "text": str(candidate.string()),
                    "x": float(box.origin.x * frame.width),
                    "y": float((1.0 - box.origin.y - box.size.height) * frame.height),
                    "width": float(box.size.width * frame.width),
                    "height": float(box.size.height * frame.height),
                    "confidence": float(candidate.confidence()),
                }
            )
        return output

    def detect_qr(self, frame: Frame) -> list[dict[str, float]]:
        request = Vision.VNDetectBarcodesRequest.alloc().init()
        request.setSymbologies_([Vision.VNBarcodeSymbologyQR])
        url = NSURL.fileURLWithPath_(str(frame.path.expanduser().resolve(strict=True)))
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        success, error = handler.performRequests_error_([request], None)
        if not success:
            detail = str(error) if isinstance(error, NSError) else repr(error)
            raise RuntimeError(f"Apple Vision 二维码检测失败：{detail}")
        output: list[dict[str, float]] = []
        for observation in request.results() or []:
            box = observation.boundingBox()
            # 不读取或返回 payload，避免把二维码内容写入日志或调用方状态。
            output.append(
                {
                    "x": float(box.origin.x * frame.width),
                    "y": float((1.0 - box.origin.y - box.size.height) * frame.height),
                    "width": float(box.size.width * frame.width),
                    "height": float(box.size.height * frame.height),
                }
            )
        return output
