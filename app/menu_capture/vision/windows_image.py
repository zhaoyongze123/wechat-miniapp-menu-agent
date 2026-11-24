"""Windows-only adapters for local OCR, QR scanning, and clipboard preparation.

This module deliberately imports Win32 APIs only inside clipboard calls so the
Mac runtime can import shared image tooling without loading Windows libraries.
"""
from __future__ import annotations

import csv
import ctypes
import io
import json
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

from PIL import Image

# 单图总预算与工作分辨率：原图 4000px 级直接多尺度放大和切块会拖到 9 秒以上。
_WORK_LONG_EDGE = 2400
_DETECT_BUDGET_SECONDS = 1.8
# 圆形小程序码定位能力保留，但当前不接入主链路：所有素材统一按方形二维码处理，
# 圆形码无法定位时直接进入人工，避免圆形候选参与自动推荐。
_ENABLE_WEAPP_LOCATE = False
_WEAPP_WORKER = Path(__file__).resolve().parents[2] / "qr_detector" / "detect.mjs"
_WEAPP_TIMEOUT = 0.9


def _tesseract_command() -> str:
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    command = configured or shutil.which("tesseract")
    if not command:
        raise RuntimeError("未找到 Tesseract OCR；请安装 Tesseract 并配置 chi_sim、eng 语言数据，或设置 TESSERACT_CMD")
    return command


_rapid_engine = None


def _rapidocr(path: Path) -> list[dict]:
    """RapidOCR：微信小程序的细字中文识别率明显高于 Tesseract。"""
    global _rapid_engine
    if _rapid_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid_engine = RapidOCR()
    result, _ = _rapid_engine(str(path))
    rows = []
    for item in result or []:
        box, text, score = item[0], item[1], item[2]
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        rows.append({
            "text": str(text),
            "x": float(min(xs)),
            "y": float(min(ys)),
            "width": float(max(xs) - min(xs)),
            "height": float(max(ys) - min(ys)),
            "confidence": float(score),
        })
    return rows


def image_ocr(path: Path) -> list[dict]:
    """优先 RapidOCR；不可用时回退 Tesseract。坐标均为原图像素。"""
    image_path = Path(path).expanduser().resolve(strict=True)
    try:
        return _rapidocr(image_path)
    except ImportError:
        return _tesseract_ocr(image_path)


def _tesseract_ocr(path: Path) -> list[dict]:
    """Run Tesseract in TSV mode and return word-grouped lines in source pixels."""
    image_path = Path(path).expanduser().resolve(strict=True)
    command = _tesseract_command()
    try:
        result = subprocess.run(
            [command, "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "11", "tsv"],
            check=False,
            capture_output=True,
            input=image_path.read_bytes(),
            timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tesseract OCR 超时（45 秒）") from exc
    except OSError as exc:
        raise RuntimeError("无法启动 Tesseract OCR") from exc
    if result.returncode != 0:
        # Avoid surfacing command stderr: it can contain local paths or image text.
        raise RuntimeError(f"Tesseract OCR 执行失败（退出码 {result.returncode}）")

    lines: OrderedDict[tuple[str, ...], list[tuple[int, int, int, int, float, str]]] = OrderedDict()
    stdout = result.stdout.decode("utf-8-sig", errors="replace") if isinstance(result.stdout, bytes) else result.stdout
    # TSV 从不加引号；识别文本里出现一个 " 时，默认的引号处理会把后面所有行吞进同一字段。
    reader = csv.DictReader(io.StringIO(stdout), delimiter="\t", quoting=csv.QUOTE_NONE)
    required = {"level", "page_num", "block_num", "par_num", "line_num", "left", "top", "width", "height", "conf", "text"}
    if not required.issubset(reader.fieldnames or []):
        raise RuntimeError("Tesseract OCR 返回的 TSV 格式无效")
    for row in reader:
        text = (row.get("text") or "").strip()
        if row.get("level") != "5" or not text:
            continue
        try:
            key = tuple(row[name] or "0" for name in ("page_num", "block_num", "par_num", "line_num"))
            left, top = int(row["left"]), int(row["top"])
            width, height = int(row["width"]), int(row["height"])
            confidence = max(0.0, min(1.0, float(row["conf"]) / 100.0))
        except (TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        lines.setdefault(key, []).append((left, top, width, height, confidence, text))

    output = []
    for words in lines.values():
        left = min(word[0] for word in words)
        top = min(word[1] for word in words)
        right = max(word[0] + word[2] for word in words)
        bottom = max(word[1] + word[3] for word in words)
        output.append({
            "text": " ".join(word[5] for word in words),
            "x": float(left),
            "y": float(top),
            "width": float(right - left),
            "height": float(bottom - top),
            "confidence": sum(word[4] for word in words) / len(words),
        })
    return output


def _qr_polygon_box(polygon) -> tuple[float, float, float, float] | None:
    corners = polygon.reshape(-1, 2)
    left = float(min(point[0] for point in corners))
    top = float(min(point[1] for point in corners))
    right = float(max(point[0] for point in corners))
    bottom = float(max(point[1] for point in corners))
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _qr_polygon_area(polygon) -> float:
    """鞋带公式计算解码器返回的二维码四边形面积，用于判断框是否过大。"""
    corners = polygon.reshape(-1, 2)
    if len(corners) < 3:
        return 0.0
    total = 0.0
    for index in range(len(corners)):
        x1, y1 = corners[index]
        x2, y2 = corners[(index + 1) % len(corners)]
        total += float(x1) * float(y2) - float(x2) * float(y1)
    return abs(total) / 2.0


def _qr_structure(crop) -> tuple[bool, int, float]:
    """未解码候选的二维码结构门禁：找定位图案 + 黑白跳变密度。

    纹理误检通常既没有 2 个以上定位图案，跳变密度也明显偏低；
    真实小码即使解不出内容，通常仍能保留定位图案和高频模块。
    """
    import cv2

    if crop is None or not getattr(crop, "size", 0):
        return False, 0, 0.0
    height, width = crop.shape[:2]
    if min(width, height) < 40 or width / height < 0.72 or width / height > 1.38:
        return False, 0, 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (128, 128), interpolation=cv2.INTER_AREA)
    _, binary = cv2.threshold(resized, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    rows = binary[:, 1:] != binary[:, :-1]
    cols = binary[1:, :] != binary[:-1, :]
    transitions = float((rows.mean() + cols.mean()) / 2)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    finders = 0
    if hierarchy is not None:
        hierarchy = hierarchy[0]
        for index, contour in enumerate(contours):
            x, y, box_width, box_height = cv2.boundingRect(contour)
            if box_width < 10 or box_height < 10 or not (0.55 <= box_width / box_height <= 1.8):
                continue
            if cv2.contourArea(contour) < 80:
                continue
            child = hierarchy[index][2]
            if child < 0:
                continue
            center_x, center_y = x + box_width / 2, y + box_height / 2
            for grand in (child, hierarchy[child][2] if hierarchy[child][2] >= 0 else -1):
                if grand < 0:
                    continue
                gx, gy, gw, gh = cv2.boundingRect(contours[grand])
                if gw < 5 or gh < 5 or not (0.55 <= gw / gh <= 1.8):
                    continue
                ratio = (gw * gh) / max(1, box_width * box_height)
                if not (0.04 <= ratio <= 0.45):
                    continue
                if abs(gx + gw / 2 - center_x) <= box_width * 0.25 and abs(gy + gh / 2 - center_y) <= box_height * 0.25:
                    finders += 1
                    break
    likeness = transitions >= 0.12 and (finders >= 3 or (finders >= 2 and transitions >= 0.16))
    return likeness, finders, round(transitions, 4)


def _qr_scan(detector, image) -> list[dict]:
    """一次多码检测，未命中再走单码接口；返回原图坐标的框与解析内容。"""
    import cv2

    hits: list[dict] = []
    try:
        found, decoded, points, _straight = detector.detectAndDecodeMulti(image)
    except (AttributeError, cv2.error):
        found, decoded, points = False, (), None
    if found and points is not None:
        for payload, polygon in zip(decoded or (), points):
            box = _qr_polygon_box(polygon)
            if box:
                hits.append({
                    "box": box, "payload": str(payload) if payload else None,
                    "qr_box": box, "qr_area": _qr_polygon_area(polygon),
                })
    if hits:
        return hits
    try:
        payload, points, _straight = detector.detectAndDecode(image)
    except cv2.error:
        return []
    if points is None:
        return []
    box = _qr_polygon_box(points)
    if not box:
        return []
    return [{
        "box": box, "payload": str(payload) if payload else None,
        "qr_box": box, "qr_area": _qr_polygon_area(points),
    }]


def _qr_merge(items: list[dict]) -> list[dict]:
    """同一码在多种预处理下会重复命中；按中心距离合并，优先保留能解码的结果。"""
    merged: list[dict] = []
    order = sorted(items, key=lambda item: (bool(item["payload"]), item["box"][2] * item["box"][3]),
                   reverse=True)
    for item in order:
        box = item["box"]
        center = (box[0] + box[2] / 2, box[1] + box[3] / 2)
        for kept in merged:
            other = kept["box"]
            other_center = (other[0] + other[2] / 2, other[1] + other[3] / 2)
            distance = ((center[0] - other_center[0]) ** 2 + (center[1] - other_center[1]) ** 2) ** 0.5
            if distance < max(box[2], box[3], other[2], other[3]) * 0.6:
                # 合并时保留下载器几何：框可能来自更大的 OpenCV 命中，
                # 但二维码真实四边形必须来自能解码的那一项，才能判断包含度。
                if item.get("payload") and not kept.get("payload"):
                    kept["payload"] = item["payload"]
                if item.get("payload") and item.get("qr_box"):
                    kept["qr_box"] = item["qr_box"]
                    kept["qr_area"] = item.get("qr_area", 0.0)
                break
        else:
            merged.append(item)
    return merged


def _zxing_scan(image) -> list[dict]:
    """zxing-cpp 解码：对手机拍摄的小码、透视变形码召回优于 OpenCV。"""
    try:
        import zxingcpp
    except ImportError:
        return []
    try:
        results = zxingcpp.read_barcodes(image)
    except Exception:
        return []
    hits: list[dict] = []
    for result in results:
        text = getattr(result, "text", "") or ""
        position = getattr(result, "position", None)
        if not text or position is None:
            continue
        xs = [position.top_left.x, position.top_right.x, position.bottom_right.x, position.bottom_left.x]
        ys = [position.top_left.y, position.top_right.y, position.bottom_right.y, position.bottom_left.y]
        left, top = float(min(xs)), float(min(ys))
        width, height = float(max(xs) - left), float(max(ys) - top)
        if width > 0 and height > 0:
            hits.append({
                "box": (left, top, width, height), "payload": text,
                "qr_box": (left, top, width, height), "qr_area": width * height,
            })
    return hits


def _qr_output(hits: list[dict]) -> list[dict]:
    output = []
    for item in _qr_merge(hits):
        box = item["box"]
        qr_box = item.get("qr_box") or box
        qr_area = float(item.get("qr_area") or (qr_box[2] * qr_box[3]))
        box_area = max(1.0, box[2] * box[3])
        fill = qr_area / box_area
        # 二维码四边形必须落在检测框内；留 2% 容差，避免浮点误差误杀。
        tolerance_x, tolerance_y = box[2] * 0.02, box[3] * 0.02
        contained = (
            qr_box[0] >= box[0] - tolerance_x
            and qr_box[1] >= box[1] - tolerance_y
            and qr_box[0] + qr_box[2] <= box[0] + box[2] + tolerance_x
            and qr_box[1] + qr_box[3] <= box[1] + box[3] + tolerance_y
        )
        # 框明显偏大且二维码挤在一侧时，裁剪图大部分是无关背景；与“框太松”同样交人工。
        offset_x = abs((qr_box[0] + qr_box[2] / 2) - (box[0] + box[2] / 2)) / max(1.0, box[2])
        offset_y = abs((qr_box[1] + qr_box[3] / 2) - (box[1] + box[3] / 2)) / max(1.0, box[3])
        centered = fill >= 0.5 or (offset_x <= 0.3 and offset_y <= 0.3)
        record = {
            "box": {"x": box[0], "y": box[1], "width": box[2], "height": box[3]},
            "payload": item["payload"],
            "qr_fill": round(min(1.0, fill), 4),
            "qr_contained": bool(contained),
            "qr_centered": bool(centered),
            # 已解码的码一定是真二维码；未解码的码由 _annotate_quality 补结构门禁。
            "qr_likeness": bool(item["payload"]) or item.get("qr_likeness", True),
        }
        if item.get("kind"):
            record["kind"] = item["kind"]
        output.append(record)
    return output


def _annotate_quality(image, hits: list[dict]) -> None:
    """给未解码框补二维码结构门禁；不改变已解码框的结论。"""
    import cv2

    for item in hits:
        if item.get("payload"):
            continue
        box = item["box"]
        height, width = image.shape[:2]
        left, top = max(0, int(box[0])), max(0, int(box[1]))
        right, bottom = min(width, int(box[0] + box[2])), min(height, int(box[1] + box[3]))
        crop = image[top:bottom, left:right]
        likeness, _finders, _transitions = _qr_structure(crop)
        item["qr_likeness"] = likeness
        # 未解码框没有解码器四边形，面积占比交给结构门禁；这里只标记是否存在有效结构。
        item.setdefault("qr_area", box[2] * box[3] if likeness else 0.0)


def _weapp_locate(path: Path, timeout: float, size: tuple[int, int] | None = None) -> list[dict]:
    """微信圆形小程序码不是标准 QR，通用解码器只能定位；这里调用现成 Node worker。

    worker 按行应答且进程常驻，所以保持 stdin 打开直到读到一行，再直接杀掉进程。
    """
    node = shutil.which("node")
    if not node or not _WEAPP_WORKER.is_file() or timeout <= 0.05:
        return []
    try:
        process = subprocess.Popen(
            [node, str(_WEAPP_WORKER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=str(_WEAPP_WORKER.parent),
        )
    except OSError:
        return []
    killer = threading.Timer(timeout, process.kill)
    killer.start()
    try:
        process.stdin.write(json.dumps({"id": "1", "path": str(path)}) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
    except (OSError, ValueError):
        return []
    finally:
        killer.cancel()
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass
    try:
        response = json.loads(line)
    except ValueError:
        return []
    result = response.get("result") if response.get("ok") else None
    if not isinstance(result, dict) or result.get("kind") != "wechat-mini-program-code":
        return []
    points = [point for point in (result.get("locatorPoints") or []) if isinstance(point, dict)]
    bounds = result.get("badgeBounds")
    if isinstance(bounds, dict):
        points.append({"x": bounds.get("x"), "y": bounds.get("y")})
        points.append({"x": (bounds.get("x") or 0) + (bounds.get("width") or 0),
                       "y": (bounds.get("y") or 0) + (bounds.get("height") or 0)})
    coords = [(point.get("x"), point.get("y")) for point in points]
    coords = [(x, y) for x, y in coords if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    if not coords:
        return []
    left = float(min(x for x, _y in coords))
    top = float(min(y for _x, y in coords))
    width = float(max(x for x, _y in coords)) - left
    height = float(max(y for _x, y in coords)) - top
    if size is not None:
        # 组件坐标可能落在图像边界上，钳制后再交给上层做边界校验。
        image_width, image_height = size
        left = max(0.0, min(left, image_width - 1.0))
        top = max(0.0, min(top, image_height - 1.0))
        width = min(width, image_width - left)
        height = min(height, image_height - top)
    if width <= 0 or height <= 0:
        return []
    return [{"box": (left, top, width, height), "payload": None, "kind": "wechat_mini_program"}]


def _qr_refine(image, box: tuple[float, float, float, float]) -> str | None:
    """按候选框裁剪并放大后重新解码；框坐标与传入图像一致。"""
    import cv2

    x, y, width, height = box
    margin = 0.2
    left = max(0, int(x - width * margin))
    top = max(0, int(y - height * margin))
    right = int(x + width * (1 + margin))
    bottom = int(y + height * (1 + margin))
    crop = image[top:bottom, left:right]
    if not crop.size:
        return None
    for scale in (3, 5):
        hits = _zxing_scan(cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC))
        if hits:
            return hits[0]["payload"]
    hits = _qr_scan(cv2.QRCodeDetector(),
                    cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC))
    return next((item["payload"] for item in hits if item["payload"]), None)


def qr_detections(path: Path, budget_seconds: float = _DETECT_BUDGET_SECONDS) -> list[dict]:
    """多解码器 + 多尺度检测二维码；返回框均为原图像素坐标。

    现场拍摄的台卡二维码常只有几十像素并带透视变形，单一 OpenCV 全图检测会直接漏掉。
    顺序：zxing 原图 → 缩小到工作分辨率后 OpenCV/CLAHE/放大/九宫格。
    所有阶段共享单图时间预算，超时即返回当前结果，避免个别大图拖到 9 秒。
    圆形微信小程序码不参与检测，统一留给人工。
    """
    import cv2
    import numpy as np

    image_path = Path(path).expanduser().resolve(strict=True)
    # cv2.imread 在 Windows 上按 ANSI 打开路径，中文目录会直接读失败；改为先读字节再解码。
    try:
        buffer = np.frombuffer(image_path.read_bytes(), dtype=np.uint8)
    except OSError as exc:
        raise RuntimeError("OpenCV 无法读取图片") from exc
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if image is None:
        raise RuntimeError("OpenCV 无法读取图片")
    detector = cv2.QRCodeDetector()
    deadline = time.monotonic() + budget_seconds

    # 大图先等比缩小；后续所有放大和切块都基于工作图，坐标再按比例还原。
    height, width = image.shape[:2]
    work_scale = min(1.0, _WORK_LONG_EDGE / max(height, width))
    if work_scale < 1.0:
        work = cv2.resize(image, None, fx=work_scale, fy=work_scale, interpolation=cv2.INTER_AREA)
    else:
        work = image
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    enhanced = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)

    # 原图用 zxing 全分辨率定位；OpenCV 在工作分辨率上定位，避免 4032px 原图检测拖慢。
    direct = _zxing_scan(image)
    for item in _qr_scan(detector, work):
        box = item["box"]
        direct.append({
            "box": (box[0] / work_scale, box[1] / work_scale, box[2] / work_scale, box[3] / work_scale),
            "payload": item["payload"],
        })
    direct = _qr_merge(direct)
    if direct:
        # 只对少量未解码框做裁剪重试，避免为已知解不出的码耗尽预算。
        unresolved = [item for item in direct if not item.get("payload")]
        for item in unresolved[:2]:
            if deadline - time.monotonic() < 0.25:
                break
            payload = _qr_refine(image, item["box"])
            if payload:
                item["payload"] = payload
        _annotate_quality(image, direct)
        return _qr_output(direct)

    hits: list[dict] = []

    def collect(found: list[dict], scale: float, offset: tuple[float, float] = (0.0, 0.0)) -> None:
        """把检测坐标按 scale 还原成原图坐标；offset 用于九宫格切块。"""
        for item in found:
            box = item["box"]
            hits.append({
                "box": (offset[0] + box[0] / scale, offset[1] + box[1] / scale,
                        box[2] / scale, box[3] / scale),
                "payload": item["payload"],
            })

    def finish() -> list[dict]:
        """对仍未解码的定位框逐个裁剪放大重试；已解码的框不重复处理。"""
        if time.monotonic() < deadline:
            unresolved = [item for item in hits if not item.get("payload") and not item.get("kind")]
            for item in unresolved[:2]:
                if deadline - time.monotonic() < 0.25:
                    break
                box = item["box"]
                payload = _qr_refine(enhanced, tuple(value * work_scale for value in box))
                if payload:
                    item["payload"] = payload
        _annotate_quality(image, hits)
        return _qr_output(hits)

    if any(item["payload"] for item in hits):
        return finish()

    # 圆形小程序码当前不接入：不预留 worker 时间，也不产生圆形候选。
    worker_ready = _ENABLE_WEAPP_LOCATE and bool(shutil.which("node")) and _WEAPP_WORKER.is_file()
    reserve = _WEAPP_TIMEOUT if worker_ready else 0.0
    work_long, work_short = max(enhanced.shape), min(enhanced.shape)
    scales = [2] if work_long <= 1200 else [1.5]
    if work_short <= 900:
        scales.append(3)
    for scale in scales:
        if time.monotonic() + reserve >= deadline:
            break
        enlarged = cv2.resize(enhanced, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        collect(_zxing_scan(enlarged) + _qr_scan(detector, enlarged), work_scale * scale)

    # 放大阶段已经定位到码时不再跑九宫格；一个都没定位到才进入更贵的切块兜底。
    if hits:
        return finish()

    # 仍无任何候选时切九宫格：避免整图缩小后小码像素不足以支撑定位。
    grid_height, grid_width = enhanced.shape[:2]
    for row in range(3):
        if time.monotonic() + reserve >= deadline:
            break
        for column in range(3):
            if time.monotonic() + reserve >= deadline:
                break
            offset_x, offset_y = column * grid_width // 3, row * grid_height // 3
            crop = enhanced[offset_y:(row + 1) * grid_height // 3,
                            offset_x:(column + 1) * grid_width // 3]
            if not crop.size:
                continue
            block = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            collect(_zxing_scan(block) + _qr_scan(detector, block), work_scale * 2,
                    (offset_x / work_scale, offset_y / work_scale))

    # 通用解码完全没有结果时，再让微信小程序码 worker 只做定位；用途仍由周边 OCR 判断。
    if worker_ready:
        remaining = min(_WEAPP_TIMEOUT, deadline - time.monotonic())
        if remaining > 0.05:
            hits.extend(_weapp_locate(image_path, remaining, (width, height)))
    return finish()


def _set_clipboard_data(format_id: int, data: bytes) -> None:
    if os.name != "nt":
        raise RuntimeError("Windows 剪贴板只能在 Windows 上使用")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.DestroyWindow.restype = ctypes.c_bool
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    # OpenClipboard(NULL) + EmptyClipboard leaves a NULL owner, which makes
    # SetClipboardData fail. A temporary message-only STATIC HWND supplies a
    # valid owner without creating a visible desktop window.
    message_only_parent = ctypes.c_void_p(-3)  # HWND_MESSAGE
    owner = user32.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 0, 0, message_only_parent, None, None, None)
    if not owner:
        raise RuntimeError("无法创建 Windows 剪贴板所有者窗口")
    if not user32.OpenClipboard(owner):
        user32.DestroyWindow(owner)
        raise RuntimeError(f"无法打开 Windows 剪贴板（错误码 {ctypes.get_last_error()}）")
    handle = None
    transferred = False
    try:
        if not user32.EmptyClipboard():
            raise RuntimeError("无法清空 Windows 剪贴板")
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not handle:
            raise MemoryError("无法分配剪贴板内存")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise MemoryError("无法锁定剪贴板内存")
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(format_id, handle):
            raise RuntimeError("无法设置 Windows 剪贴板数据")
        # Successful SetClipboardData transfers ownership to Windows.
        transferred = True
    finally:
        user32.CloseClipboard()
        user32.DestroyWindow(owner)
        if handle and not transferred:
            kernel32.GlobalFree(handle)


def prepare_clipboard_image(path: Path) -> None:
    """Publish a DIB image to the Windows clipboard; UiPath performs paste/send."""
    with Image.open(path) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="BMP")
    bmp = buffer.getvalue()
    if len(bmp) <= 14 or bmp[:2] != b"BM":
        raise RuntimeError("无法编码 Windows 剪贴板图片")
    _set_clipboard_data(8, bmp[14:])  # CF_DIB excludes the BMP file header.


def prepare_clipboard_text(text: str) -> None:
    """Publish Unicode text to the Windows clipboard; UiPath performs paste/send."""
    _set_clipboard_data(13, (str(text) + "\0").encode("utf-16-le"))  # CF_UNICODETEXT
