"""微信圆形小程序码第一轮检测。

OpenCV 只负责在原始像素坐标里提候选 ROI；是否真是微信圆形码由 Node worker
调用 ``@weapp-vite/qr`` 做三定位点 + 右下角徽标 + 环状纹理的结构验证。
npm 包的 ``confidence`` 是启发式分数，不参与可信判定。

依赖：opencv-python-headless、Pillow、numpy；``qr_detector/`` 下执行过 ``npm ci``。
"""
from __future__ import annotations

import json
import math
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

MAX_DECODE_PIXELS = 45_000_000
MAX_ROI_PER_IMAGE = 36
MAX_ORIENTATIONS = 4
CANVAS = 384
REQUEST_TIMEOUT = 2.0
PREVIEW_EDGE = 1600
_MINIAPP_KIND = "wechat-mini-program-code"
_WORKER_DIR = Path(__file__).resolve().parents[1] / "qr_detector"
_NODE_MODULE = Path("node_modules") / "@weapp-vite" / "qr" / "package.json"


class CircleDetectorUnavailable(RuntimeError):
    """Node worker 或依赖未就绪；不能把第一轮当成已完成。"""

    code = "circle_detector_unavailable"


class CircleDetectorTimeout(RuntimeError):
    code = "circle_timeout"


class CircleDetectorError(RuntimeError):
    code = "circle_detector_error"


class CircleImageTooLarge(RuntimeError):
    code = "image_too_large"


def _error_code(exc: BaseException) -> str:
    return getattr(exc, "code", None) or "circle_detector_error"


class NodeCircleWorker:
    """一个批次常驻的 Node 子进程，按请求 id 匹配结果。"""

    def __init__(self, worker_dir: Path | None = None):
        directory = Path(worker_dir or _WORKER_DIR)
        entry = directory / "detect.mjs"
        node = shutil.which("node")
        if not node or not entry.is_file() or not (directory / _NODE_MODULE).is_file():
            raise CircleDetectorUnavailable("圆形码检测 worker 未安装")
        self._directory = directory
        self._node = node
        self._entry = entry
        self._process: subprocess.Popen | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._start()

    def _start(self) -> None:
        self._queue = queue.Queue()
        self._process = subprocess.Popen(
            [self._node, str(self._entry)],
            cwd=str(self._directory),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        threading.Thread(target=self._read, args=(self._process, self._queue), daemon=True).start()

    @staticmethod
    def _read(process: subprocess.Popen, output: queue.Queue[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            output.put(line)
        output.put("")

    def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _request_once(self, path: Path) -> dict | None:
        request_id = uuid.uuid4().hex
        process = self._process
        if process is None or process.stdin is None:
            raise CircleDetectorError("worker 未启动")
        try:
            process.stdin.write(json.dumps({"id": request_id, "path": str(path)}) + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise CircleDetectorError("worker 写入失败") from exc
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CircleDetectorTimeout("圆形码结构识别超时")
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise CircleDetectorTimeout("圆形码结构识别超时") from None
            if not line:
                raise CircleDetectorError("worker 已退出")
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict) or payload.get("id") != request_id:
                continue
            if not payload.get("ok"):
                raise CircleDetectorError(str(payload.get("error_code") or "detector_error"))
            result = payload.get("result")
            return result if isinstance(result, dict) else None

    def verify(self, path: Path) -> dict | None:
        """识别单个候选图；超时/崩溃时重启 worker 且只重试一次。"""
        with self._lock:
            try:
                return self._request_once(path)
            except (CircleDetectorTimeout, CircleDetectorError):
                self._stop()
                self._start()
                return self._request_once(path)

    def close(self) -> None:
        with self._lock:
            self._stop()


def _rotated_points(points: list[tuple[float, float]], orientation: int) -> list[tuple[float, float]]:
    """把旋转后的画布坐标还原成未旋转画布坐标（画布是正方形）。"""
    last = CANVAS - 1
    if orientation == 90:
        return [(last - y, x) for x, y in points]
    if orientation == 180:
        return [(last - x, last - y) for x, y in points]
    if orientation == 270:
        return [(y, last - x) for x, y in points]
    return list(points)


def _right_angle_locators(points: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """三点中找直角顶点，返回 (A, 两条边长度比, 夹角)。"""
    best = None
    for index in range(3):
        a = points[index]
        b, c = (points[(index + 1) % 3], points[(index + 2) % 3])
        ab = math.dist(a, b)
        ac = math.dist(a, c)
        if ab <= 0 or ac <= 0:
            continue
        cosine = ((b[0] - a[0]) * (c[0] - a[0]) + (b[1] - a[1]) * (c[1] - a[1])) / (ab * ac)
        angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
        if best is None or abs(angle - 90) < abs(best[2] - 90):
            best = (a, max(ab, ac) / min(ab, ac), angle)
    return best


def _validate_structure(result: dict) -> tuple[list[tuple[float, float]], dict] | None:
    """结构可信判定：三定位点、徽标、直角与边长比例；不采信 confidence。"""
    if result.get("kind") != _MINIAPP_KIND:
        return None
    raw = result.get("locatorPoints")
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    points = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        x, y = item.get("x"), item.get("y")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (x, y)):
            return None
        if not (0 <= x <= CANVAS and 0 <= y <= CANVAS):
            return None
        points.append((float(x), float(y)))
    badge = result.get("badgeBounds")
    if not isinstance(badge, dict):
        return None
    if any(isinstance(badge.get(key), bool) or not isinstance(badge.get(key), (int, float)) or not math.isfinite(badge[key])
           for key in ("x", "y", "width", "height")):
        return None
    if badge["width"] <= 0 or badge["height"] <= 0:
        return None
    geometry = _right_angle_locators(points)
    if geometry is None:
        return None
    _a, ratio, angle = geometry
    if not (0.60 <= ratio <= 1.67) or not (65.0 <= angle <= 115.0):
        return None
    return points, badge


def _normalize_canvas(canvas: Image.Image) -> Image.Image:
    """把背景漂白到接近纯白并保留徽标颜色。

    npm 包把 ``255-r + 255-g + 255-b > 80`` 当作前景，实拍图偏米色的桌面会被
    当成一个整体连通域，导致定位点检测直接失效。这里用自适应阈值只保留深色
    结构，其余像素写白。
    """
    rgb = np.array(canvas.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10)
    normalized = np.full_like(rgb, 255)
    normalized[mask == 0] = rgb[mask == 0]
    return Image.fromarray(normalized)


def _preview_rois(gray: np.ndarray) -> list[dict]:
    """在缩略图上生成候选 ROI（缩略图坐标）。"""
    height, width = gray.shape[:2]
    min_side = min(width, height)
    rois: list[dict] = []

    def add(left: float, top: float, size: float, source: str) -> None:
        if size <= 0:
            return
        left = max(0, min(width - 1, left))
        top = max(0, min(height - 1, top))
        right = max(left + 1, min(width, left + size))
        bottom = max(top + 1, min(height, top + size))
        rois.append({"left": left, "top": top, "size": max(right - left, bottom - top), "source": source})

    for kernel_size in (5, 9):
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        binary = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
        binary = cv2.threshold(binary, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            long_edge = max(w, h)
            if not (40 <= long_edge <= 0.70 * min_side):
                continue
            ratio = w / h if h else 0
            if not (0.75 <= ratio <= 1.33):
                continue
            patch = gray[y:y + h, x:x + w]
            if patch.size == 0 or float(patch.std()) < 12.0:
                continue
            size = int(round(long_edge * 1.15))
            add(x + w / 2 - size / 2, y + h / 2 - size / 2, size, "contour")

    blurred = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=40, param1=100, param2=24,
        minRadius=18, maxRadius=max(18, min_side // 3),
    )
    for circle in (circles[0] if circles is not None else []):
        cx, cy, radius = (float(value) for value in circle[:3])
        add(cx - radius * 1.1, cy - radius * 1.1, radius * 2.2, "hough")

    if not rois:
        # 受控兜底：2x2、3x3 重叠块，不做无界滑窗。
        for grid in (2, 3):
            side = int(min_side / grid * 1.4)
            for row in range(grid):
                for column in range(grid):
                    add(min_side * column / grid, min_side * row / grid, side, "grid")
    elif abs(width - height) <= 0.08 * max(width, height):
        add(0, 0, min_side, "full")

    merged: list[dict] = []
    for roi in rois:
        cx, cy = roi["left"] + roi["size"] / 2, roi["top"] + roi["size"] / 2
        duplicate = any(
            math.hypot(cx - kept["left"] - kept["size"] / 2, cy - kept["top"] - kept["size"] / 2)
            < 0.12 * max(roi["size"], kept["size"])
            and 0.90 <= roi["size"] / kept["size"] <= 1.11
            for kept in merged
        )
        # 只合并几乎完全重合的框：尺寸/中心不同的候选都留给结构验证判断，
        # 以免把真正贴合码区的框提前挤掉。
        if not duplicate:
            merged.append(roi)
    return merged[:MAX_ROI_PER_IMAGE]


class MiniProgramCodeDetector:
    """第一轮：按图返回可信的圆形小程序码候选（原图坐标）。"""

    def __init__(self, worker_dir: Path | None = None, work_dir: Path | None = None):
        self._work_dir = Path(work_dir) if work_dir else None
        self._temp_owned = self._work_dir is None
        self._worker = NodeCircleWorker(worker_dir)

    def _roi_dir(self) -> Path:
        if self._work_dir is None:
            import tempfile

            self._work_dir = Path(tempfile.mkdtemp(prefix="circle-roi-"))
        self._work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._work_dir

    def detect(self, path: Path) -> dict | None:
        """返回 ``{"box", "locator_points", "badge_bounds", "detector_confidence"}`` 或 None。"""
        try:
            with Image.open(path) as image:
                image.load()
                width, height = image.size
                if width * height > MAX_DECODE_PIXELS:
                    raise CircleImageTooLarge("图片像素过大")
                scale = min(1.0, PREVIEW_EDGE / max(width, height))
                preview = image.convert("RGB").resize(
                    (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                    Image.Resampling.BILINEAR,
                )
        except CircleImageTooLarge:
            raise
        except Exception as exc:
            raise CircleDetectorError("图片读取失败") from exc

        gray = cv2.cvtColor(np.array(preview), cv2.COLOR_RGB2GRAY)
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        rois = _preview_rois(gray)
        if not rois:
            return None
        preview_width, preview_height = preview.size
        scale_x, scale_y = width / preview_width, height / preview_height

        for index, roi in enumerate(rois):
            box_left = max(0.0, roi["left"] * scale_x)
            box_top = max(0.0, roi["top"] * scale_y)
            box_size = roi["size"] * max(scale_x, scale_y)
            padding = box_size * 0.10
            left = max(0, int(math.floor(box_left - padding)))
            top = max(0, int(math.floor(box_top - padding)))
            right = min(width, int(math.ceil(box_left + box_size + padding)))
            bottom = min(height, int(math.ceil(box_top + box_size + padding)))
            if right - left < 40 or bottom - top < 40:
                continue
            with Image.open(path) as source:
                source.load()
                region = source.convert("RGB").crop((left, top, right, bottom))
            canvas, fit_scale, pad_x, pad_y = _fit_square(region)
            canvas = _normalize_canvas(canvas)
            roi_path = self._roi_dir() / f"roi-{index}.png"
            try:
                for orientation in (0, 90, 180, 270)[:MAX_ORIENTATIONS]:
                    rotated = canvas if orientation == 0 else canvas.rotate(orientation, expand=False)
                    rotated.save(roi_path, format="PNG")
                    result = self._worker.verify(roi_path)
                    validated = _validate_structure(result) if result else None
                    if validated is None:
                        continue
                    points, badge = validated
                    points = _rotated_points(points, orientation)
                    found = self._to_original(
                        points, badge, orientation, left, top, fit_scale, pad_x, pad_y, width, height, result,
                    )
                    if found is not None:
                        return found
            finally:
                roi_path.unlink(missing_ok=True)
        return None

    @staticmethod
    def _to_original(points, badge, orientation, left, top, fit_scale, pad_x, pad_y, width, height, result):
        def to_image(point):
            x = left + (point[0] - pad_x) / fit_scale
            y = top + (point[1] - pad_y) / fit_scale
            return x, y

        mapped = [to_image(point) for point in points]
        geometry = _right_angle_locators(mapped)
        if geometry is None:
            return None
        corner, _ratio, _angle = geometry
        legs = [math.dist(corner, point) for point in mapped if point != corner]
        if not legs:
            return None
        # 三定位点构成码区方形：直角顶点为 A，第四点 D = B + C - A。
        b, c = [point for point in mapped if point != corner][:2]
        d = (b[0] + c[0] - corner[0], b[1] + c[1] - corner[1])
        side = max(legs) * 1.6 * 1.05
        center_x = (corner[0] + d[0]) / 2
        center_y = (corner[1] + d[1]) / 2
        half = side / 2
        x0 = max(0.0, center_x - half)
        y0 = max(0.0, center_y - half)
        x1 = min(float(width), center_x + half)
        y1 = min(float(height), center_y + half)
        if x1 - x0 < 40 or y1 - y0 < 40:
            return None
        badge_points = [
            to_image((badge["x"], badge["y"])),
            to_image((badge["x"] + badge["width"], badge["y"] + badge["height"])),
        ]
        badge_bounds = {
            "x": max(0.0, min(badge_points[0][0], badge_points[1][0])),
            "y": max(0.0, min(badge_points[0][1], badge_points[1][1])),
            "width": abs(badge_points[1][0] - badge_points[0][0]),
            "height": abs(badge_points[1][1] - badge_points[0][1]),
        }
        confidence = result.get("confidence")
        return {
            "box": {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0},
            "locator_points": [{"x": point[0], "y": point[1]} for point in mapped],
            "badge_bounds": badge_bounds,
            "detector_confidence": float(confidence) if isinstance(confidence, (int, float)) and math.isfinite(confidence) else None,
            "roi_source": "structure_verified",
        }

    def close(self) -> None:
        self._worker.close()
        if self._temp_owned and self._work_dir is not None:
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._work_dir = None

    def __enter__(self) -> "MiniProgramCodeDetector":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _fit_square(region: Image.Image) -> tuple[Image.Image, float, float, float]:
    """等比缩放并居中留白到 384×384，返回画布与坐标映射参数。"""
    width, height = region.size
    scale = min(CANVAS / width, CANVAS / height)
    resized = region.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", (CANVAS, CANVAS), "white")
    pad_x = (CANVAS - resized.width) / 2
    pad_y = (CANVAS - resized.height) / 2
    canvas.paste(resized, (round(pad_x), round(pad_y)))
    return canvas, scale, pad_x, pad_y


def error_code(exc: BaseException) -> str:
    """把检测异常转成受控的短码，供 manifest 记录。"""
    return _error_code(exc)
