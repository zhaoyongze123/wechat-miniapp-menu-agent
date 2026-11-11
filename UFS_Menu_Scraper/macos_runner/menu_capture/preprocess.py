"""本地二维码素材筛选。只保存载荷哈希，不保留二维码原文。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from PIL import Image, ImageFilter, ImageStat

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MIN_QR_EDGE_PX = 80
CROP_MARGIN_RATIO = 0.80
# 超过该图片数的门店不做任何检测，直接转人工。
MAX_STORE_IMAGES = 13
# 只有该区间内的门店跑方形检测；1 张门店直接用整图候选。
SQUARE_IMAGE_RANGE = (2, MAX_STORE_IMAGES)


def _apple_vision_inspector(path: Path) -> list[dict]:
    """使用本机二维码扫描器；payload 只在本函数调用链内存活。"""
    if sys.platform == "win32":
        from .windows_image import qr_detections
        return qr_detections(path)
    if sys.platform != "darwin":
        raise RuntimeError("默认二维码检查器仅支持 macOS Apple Vision 或 Windows OpenCV")
    import Vision
    from Foundation import NSURL

    with Image.open(path) as image:
        width, height = image.size
    request = Vision.VNDetectBarcodesRequest.alloc().init()
    request.setSymbologies_([Vision.VNBarcodeSymbologyQR])
    url = NSURL.fileURLWithPath_(str(path.expanduser().resolve(strict=True)))
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    success, _error = handler.performRequests_error_([request], None)
    if not success:
        raise RuntimeError("Apple Vision 二维码识别失败")

    detections = []
    for observation in request.results() or []:
        box = observation.boundingBox()
        payload = observation.payloadStringValue()
        detections.append({
            "box": {
                "x": float(box.origin.x * width),
                "y": float((1.0 - box.origin.y - box.size.height) * height),
                "width": float(box.size.width * width),
                "height": float(box.size.height * height),
            },
            "payload": str(payload) if payload is not None else None,
        })
    return detections


def _normalized_output_path(output_root: Path) -> Path:
    # strict=False 会解析已有父路径中的 symlink，同时允许新建尚不存在的输出目录。
    return output_root.expanduser().absolute().resolve(strict=False)


def _ensure_paths(input_root: Path, output_root: Path) -> tuple[Path, Path]:
    source = input_root.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError("素材输入路径必须是文件夹")
    target = _normalized_output_path(output_root)
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("输出目录不能与素材目录相同或互相包含")
    if os.path.lexists(output_root.expanduser()):
        raise FileExistsError("输出目录必须不存在；请使用新的空闲路径")
    return source, target


def _enumerate_images(root: Path) -> list[Path]:
    images: list[Path] = []
    walk_errors: list[OSError] = []
    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_errors.append):
        folder = Path(current)
        dirs[:] = sorted(name for name in dirs if not (folder / name).is_symlink())
        for name in sorted(files):
            image = folder / name
            if image.is_symlink() or image.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                resolved = image.resolve(strict=True)
            except OSError:
                images.append(image)
                continue
            if root in resolved.parents:
                images.append(image)
    if walk_errors:
        raise PermissionError("素材目录中有无法读取的文件夹；未发布不完整清单") from None
    return sorted(images, key=lambda path: path.relative_to(root).as_posix())


def _safe_progress(progress, done: int, total: int) -> None:
    if progress is not None:
        progress({"done": done, "total": total})


def _box_scores(path: Path, box: dict) -> tuple[int, float, float]:
    """按候选框在原图上计算清晰度与尺寸分，用于稳定排序。"""
    x, y, width, height = box["x"], box["y"], box["width"], box["height"]
    with Image.open(path) as source_image:
        patch = source_image.crop((int(x), int(y), int(x + width), int(y + height)))
        patch = patch.convert("L").resize((256, 256))
        edges = patch.filter(ImageFilter.Kernel((3, 3), (0, -1, 0, -1, 4, -1, 0, -1, 0), scale=1))
        sharpness = ImageStat.Stat(edges.crop((1, 1, 255, 255))).var[0]
    short_edge = int(round(min(width, height)))
    quality_score = min(width, height) * math.sqrt(max(sharpness, 1.0))
    return short_edge, round(sharpness, 2), round(quality_score, 2)


def _prepare_image(path: Path, relative: str) -> tuple[dict, tuple[int, int] | None]:
    record = {"path": relative, "sha256": "", "detection_count": 0, "candidates": []}
    try:
        record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with Image.open(path) as image:
            image.load()
            size = image.size
    except Exception as exc:
        # 不记录异常文本，防止某些解码器把条码内容写入错误消息。
        record["error"] = f"本地图片/二维码检查失败（{type(exc).__name__}）"
        return record, None
    return record, size


def _detect_square(path: Path, record: dict, size: tuple[int, int], inspector: Callable[[Path], list[dict]]) -> dict:
    image_width, image_height = size
    try:
        detections = inspector(path)
        if not isinstance(detections, (list, tuple)):
            raise TypeError("inspector 必须返回检测列表")
        record["detection_count"] = len(detections)
        for detection in detections:
            if not isinstance(detection, dict):
                raise TypeError("inspector 检测项格式无效")
            box = detection.get("box")
            if not isinstance(box, dict):
                box = {key: detection.get(key) for key in ("x", "y", "width", "height")}
            coords = [box.get(key) for key in ("x", "y", "width", "height")]
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in coords):
                raise ValueError("二维码位置坐标无效")
            x, y, width, height = map(float, coords)
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > image_width or y + height > image_height:
                raise ValueError("二维码位置超出原图边界")
            payload = detection.get("payload")
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest() if isinstance(payload, str) and payload else None
            # 本地只保存用途提示与散列，不记录二维码内容；明确企业微信域名无需请求模型。
            try:
                host = (urlsplit(payload).hostname or "").lower() if isinstance(payload, str) else ""
            except ValueError:
                host = ""
            enterprise = host in {"work.weixin.qq.com", "qy.weixin.qq.com"} or host.endswith(".work.weixin.qq.com")
            short_edge, sharpness, quality_score = _box_scores(path, {"x": x, "y": y, "width": width, "height": height})
            # 只比较同码的可解码候选。尺寸和边缘变化是排序启发式，不代表微信扫码成功率。
            record["candidates"].append({
                "box": {"x": x, "y": y, "width": width, "height": height},
                "payload_sha256": payload_hash,
                "purpose_hint": "enterprise_wechat" if enterprise else "unknown",
                "short_edge_px": short_edge,
                "sharpness_score": sharpness,
                "quality_score": quality_score,
                "selected": False,
            })
    except Exception as exc:
        # 不记录异常文本，防止某些解码器把条码内容写入错误消息。
        record["error"] = f"本地图片/二维码检查失败（{type(exc).__name__})"
    return record


def _inspect_image(path: Path, relative: str, inspector: Callable[[Path], list[dict]]) -> dict:
    record, size = _prepare_image(path, relative)
    if size is None:
        return record
    return _detect_square(path, record, size, inspector)


def _circle_candidate(path: Path, detected: dict, size: tuple[int, int] | None = None) -> dict | None:
    box = detected.get("box") if isinstance(detected, dict) else None
    if not isinstance(box, dict):
        return None
    try:
        x, y, width, height = (float(box[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x, y, width, height)):
        return None
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        return None
    if size is not None and (x + width > size[0] + 0.5 or y + height > size[1] + 0.5):
        return None
    short_edge, sharpness, quality_score = _box_scores(path, box)
    return {
        "box": {"x": x, "y": y, "width": width, "height": height},
        # 圆形码不可本地解码，只保留原图 SHA256；payload 哈希固定为 null。
        "payload_sha256": None,
        "code_type": "mini_program_code",
        "source": "mini_program_code",
        "purpose": "ordering",
        "purpose_source": "mini_program_code_priority",
        "purpose_reason": "第一轮命中微信圆形小程序码，待微信实际验证",
        "short_edge_px": short_edge,
        "sharpness_score": sharpness,
        "quality_score": quality_score,
        "detector_confidence": detected.get("detector_confidence"),
        "selected": False,
    }


def _decode_circle(path: Path, circle_inspector: Callable[[Path], dict | None]) -> tuple[dict | None, str | None]:
    try:
        return circle_inspector(path), None
    except Exception as exc:  # noqa: BLE001 - 异常只落受控短码
        from .miniapp_code import error_code

        return None, error_code(exc)


def _circle_applies(image_count: int, circle_image_range: tuple[int, int] | None) -> bool:
    """按门店图片数决定是否跑圆形码检测；None 表示不做分流。"""
    if circle_image_range is None:
        return True
    return circle_image_range[0] <= image_count <= circle_image_range[1]


def inspect_store_images(
    images: list[dict],
    resolve_path: Callable[[dict], Path],
    inspector: Callable[[Path], list[dict]] | None,
    circle_inspector: Callable[[Path], dict | None] | None,
    on_unit: Callable[[], None] | None = None,
    skip_square_on_circle_hit: bool = True,
    circle_image_range: tuple[int, int] | None = None,
    square_image_range: tuple[int, int] | None = None,
    circle_gate: Callable[[list[dict]], bool] | None = None,
    max_images: int | None = None,
) -> dict:
    """按门店图片数选择检测：先方形，方形没有可用候选时才补圆形。

    - max_images：张数超过上限整店不检测（too_many_images）；
    - square_image_range：只有该区间内的门店跑方形检测；
    - circle_image_range：只有该区间内的门店可能跑圆形检测；
    - circle_gate：方形检测后调用，返回 True 表示“没有可用方形候选”，才继续跑圆形；
    - skip_square_on_circle_hit 保留为兼容参数：新流程方形始终先跑，圆形只做兜底。
    """
    stats = {"circle_hit": False, "circle_error": False, "square_checked": False,
             "circle_images": 0, "skipped_images": 0, "circle_ms": 0.0, "square_ms": 0.0,
             "circle_skipped_reason": "", "square_skipped_reason": ""}
    unit = on_unit or (lambda: None)
    count = len(images)

    def prepare(image: dict) -> tuple[Path, tuple[int, int] | None]:
        path = resolve_path(image)
        meta = {key: image[key] for key in ("display_name", "origin", "supplement_store_id") if key in image}
        record, size = _prepare_image(path, image["path"])
        image.clear()
        image.update(record)
        image.update(meta)
        return path, size

    if max_images is not None and count > max_images:
        stats["square_skipped_reason"] = stats["circle_skipped_reason"] = "too_many_images"
        for image in images:
            prepare(image)
        return stats

    square_allowed = square_image_range is None or square_image_range[0] <= count <= square_image_range[1]
    if square_allowed and inspector is not None:
        stats["square_checked"] = True
        square_started = time.perf_counter()
        for image in images:
            path, size = prepare(image)
            if size is not None:
                image["scan_status"] = "square_checked"
                _detect_square(path, image, size, inspector)
            unit()
        stats["square_ms"] = round((time.perf_counter() - square_started) * 1000, 1)
    else:
        stats["square_skipped_reason"] = (
            "single_image" if square_image_range is not None and count < square_image_range[0] else "many_images"
        )
        for image in images:
            prepare(image)

    if circle_inspector is None or not _circle_applies(count, circle_image_range):
        if circle_inspector is not None and circle_image_range is not None:
            stats["circle_skipped_reason"] = (
                "single_image" if count < circle_image_range[0] else "many_images"
            )
        return stats
    if circle_gate is not None and not circle_gate(images):
        # 方形已经给出可用候选，圆形阶段整店跳过；进度由调用方补齐。
        stats["circle_skipped_reason"] = "square_candidate_found"
        return stats

    circle_started = time.perf_counter()
    for image in images:
        path = resolve_path(image)
        if image.get("error"):
            unit()
            continue
        if stats["circle_hit"]:
            image["scan_status"] = "skipped_after_circle_hit"
            stats["skipped_images"] += 1
            unit()
            continue
        stats["circle_images"] += 1
        detected, error = _decode_circle(path, circle_inspector)
        if error is not None:
            stats["circle_error"] = True
            image["circle_error"] = error
            unit()
            continue
        if detected is not None:
            try:
                with Image.open(path) as source_image:
                    size = source_image.size
            except Exception:
                size = None
            candidate = _circle_candidate(path, detected, size) if size is not None else None
            if candidate is None:
                # 检测器返回越界/非法坐标视为检测异常，不能当成“确认无圆码”。
                stats["circle_error"] = True
                image["circle_error"] = "circle_detector_error"
                unit()
                continue
            stats["circle_hit"] = True
            image["candidates"] = [candidate]
            image["detection_count"] = 1
            image["scan_status"] = "circle_hit"
            unit()
            continue
        unit()
    stats["circle_ms"] = round((time.perf_counter() - circle_started) * 1000, 1)
    return stats


def _select_circle_candidate(store: dict) -> tuple[int, int] | None:
    """圆形码命中店只按清晰度/尺寸稳定选一张，不参与普通码载荷竞争。"""
    eligible = [
        (image_index, candidate_index, candidate)
        for image_index, image in enumerate(store["images"])
        for candidate_index, candidate in enumerate(image["candidates"])
        if candidate.get("code_type") == "mini_program_code"
    ]
    if not eligible:
        return None
    image_index, candidate_index, _candidate = max(
        eligible,
        key=lambda item: (
            item[2].get("quality_score", 0),
            item[2].get("short_edge_px", 0),
            store["images"][item[0]]["path"],
        ),
    )
    return image_index, candidate_index


def _select_store(store: dict) -> tuple[tuple[int, int] | None, str]:
    if any("error" in image for image in store["images"]):
        return None, "至少一张原图读取或二维码检查失败，需人工复核"
    candidates = [
        candidate
        for image in store["images"]
        for candidate in image["candidates"]
        if candidate.get("code_type") != "mini_program_code"
    ]
    if not candidates:
        return None, "未检测到二维码，需人工复核"
    if any(not candidate["payload_sha256"] for candidate in candidates):
        return None, "同店存在无法解码载荷的二维码候选，不能排除混入其他码，需人工复核"
    payload_hashes = {candidate["payload_sha256"] for candidate in candidates if candidate["payload_sha256"]}
    if not payload_hashes:
        return None, "检测到二维码但没有可解码载荷，需人工复核"
    if len(payload_hashes) > 1:
        return None, "同店检测到不同二维码载荷，无法自动确定候选，需人工复核"
    eligible = [
        (image_index, candidate_index, candidate)
        for image_index, image in enumerate(store["images"])
        for candidate_index, candidate in enumerate(image["candidates"])
        if candidate["payload_sha256"] in payload_hashes and candidate["short_edge_px"] >= MIN_QR_EDGE_PX
    ]
    if not eligible:
        return None, f"唯一可解码二维码的原图最短边小于 {MIN_QR_EDGE_PX}px，需人工复核"
    image_index, candidate_index, _candidate = max(
        eligible,
        key=lambda item: (item[2].get("quality_score", item[2]["short_edge_px"]), item[2]["short_edge_px"], store["images"][item[0]]["path"]),
    )
    return (image_index, candidate_index), "同店只有一种可解码载荷；已结合二维码尺寸和边缘清晰度推荐一张，待微信验证"


def _crop_candidate(source_path: Path, box: dict, destination: Path) -> None:
    with Image.open(source_path) as image:
        image.load()
        width, height = image.size
        qr_width, qr_height = box["width"], box["height"]
        left = max(0, int(math.floor(box["x"] - qr_width * CROP_MARGIN_RATIO)))
        top = max(0, int(math.floor(box["y"] - qr_height * CROP_MARGIN_RATIO)))
        right = min(width, int(math.ceil(box["x"] + qr_width * (1 + CROP_MARGIN_RATIO))))
        bottom = min(height, int(math.ceil(box["y"] + qr_height * (1 + CROP_MARGIN_RATIO))))
        cropped = image.crop((left, top, right, bottom))
        if cropped.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
            cropped = cropped.convert("RGB")
        cropped.save(destination, format="PNG")


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def discover_stores(source: Path) -> tuple[list[dict], set[str]]:
    """按图片直接父目录生成门店骨架，并返回跨父目录重名集合。

    整批分析（build_materials）和逐店分析（job worker）必须共用这一函数，
    否则两边对“哪些目录算同一家店”的判断会出现偏差。
    """
    images = _enumerate_images(source)
    stores_by_folder: dict[str, dict] = {}
    for image_path in images:
        relative_path = image_path.relative_to(source).as_posix()
        relative_folder = image_path.parent.relative_to(source).as_posix() or "."
        source_name = image_path.parent.name or source.name
        store = stores_by_folder.setdefault(relative_folder, {
            "source_name": source_name,
            "source_folder": relative_folder,
            "status": "needs_review",
            "reason": "等待二维码检查",
            "selected_source": None,
            "selected_artifact": None,
            "images": [],
        })
        store["images"].append({"path": relative_path})
    stores = [stores_by_folder[key] for key in sorted(stores_by_folder)]
    folders_by_name: dict[str, list[dict]] = {}
    for store in stores:
        folders_by_name.setdefault(store["source_name"], []).append(store)
    duplicate_names = {
        name for name, group in folders_by_name.items()
        if len({item["source_folder"] for item in group}) > 1
    }
    return stores, duplicate_names


def analyze_store(
    store: dict,
    source_root: Path,
    *,
    inspector: Callable[[Path], list[dict]] | None = None,
    circle_inspector: Callable[[Path], dict | None] | None = None,
    circle_priority: bool = True,
    circle_image_range: tuple[int, int] | None = None,
    square_image_range: tuple[int, int] | None = None,
    circle_gate: Callable[[list[dict]], bool] | None = None,
    max_images: int | None = None,
    on_unit: Callable[[], None] | None = None,
    resolve_path: Callable[[dict], Path] | None = None,
) -> dict:
    """对单个门店执行图片准备与两轮检测，返回补充了统计字段的门店字典。

    只做检测，不做“选哪张图”的语义决策；决策层（MaterialService）在整批和逐店
    两条路径上都调用同一份 _classify_and_select，保证结果一致。
    """
    unit = on_unit or (lambda: None)
    resolve = resolve_path or (lambda image: Path(source_root) / image["path"])
    stats = inspect_store_images(
        store["images"],
        resolve,
        inspector or _apple_vision_inspector,
        circle_inspector,
        unit,
        skip_square_on_circle_hit=circle_priority,
        circle_image_range=circle_image_range,
        square_image_range=square_image_range,
        circle_gate=circle_gate,
        max_images=max_images,
    )
    if stats["circle_skipped_reason"] == "square_candidate_found":
        # 圆形未跑时把预留的单元补齐，避免整批进度停在 90%。
        for _image in store["images"]:
            unit()
    store["circle_hit"] = stats["circle_hit"]
    store["circle_error"] = stats["circle_error"]
    store["square_checked"] = stats["square_checked"]
    store["circle_ms"] = stats["circle_ms"]
    store["square_ms"] = stats["square_ms"]
    store["circle_images"] = stats["circle_images"]
    store["skipped_images"] = stats["skipped_images"]
    store["circle_skipped"] = stats["circle_skipped_reason"]
    return store


def build_materials(
    input_root: Path,
    output_root: Path,
    inspector: Callable[[Path], list[dict]] | None = None,
    progress: Callable[[dict], None] | None = None,
    circle_inspector: Callable[[Path], dict | None] | None = None,
    circle_priority: bool = True,
    circle_image_range: tuple[int, int] | None = None,
    square_image_range: tuple[int, int] | None = None,
    circle_gate: Callable[[list[dict]], bool] | None = None,
    max_images: int | None = None,
) -> dict:
    """按图片直接父目录分店，先圆形码后方形码两轮筛出待微信验证的候选。

    inspector(path) 返回 ``[{"box": {"x", "y", "width", "height"}, "payload": str|None}]``；
    circle_inspector(path) 返回结构验证通过的圆形码结果或 None。circle_priority=True 时
    圆形码命中店不再执行方形检测与用途 OCR；关闭时圆形码只作辅助候选，第二轮照常运行。
    progress 收到的值只有 done/total 计数。
    """
    source, target = _ensure_paths(Path(input_root), Path(output_root))
    inspect = inspector or _apple_vision_inspector
    stores, duplicate_names = discover_stores(source)
    total_images = sum(len(store["images"]) for store in stores)

    def store_units(store: dict) -> int:
        count = len(store["images"])
        if max_images is not None and count > max_images:
            return 0
        units = count if square_image_range is None or square_image_range[0] <= count <= square_image_range[1] else 0
        if circle_inspector is not None and _circle_applies(count, circle_image_range):
            units += count
        return units

    total_units = sum(store_units(store) for store in stores)
    done_units = 0

    def unit() -> None:
        nonlocal done_units
        done_units += 1
        _safe_progress(progress, done_units, total_units)

    for store in stores:
        analyze_store(
            store, source,
            inspector=inspect,
            circle_inspector=circle_inspector,
            circle_priority=circle_priority,
            circle_image_range=circle_image_range,
            square_image_range=square_image_range,
            circle_gate=circle_gate,
            max_images=max_images,
            on_unit=unit,
        )

    stage = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
    stage.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        collectible_root = stage / "可采集"
        for store in stores:
            if store["source_name"] in duplicate_names:
                store["reason"] = "不同父路径存在同名店铺目录，拒绝归档以防混店"
                continue
            circle = _select_circle_candidate(store) if store.get("circle_hit") else None
            if circle is not None and not circle_priority:
                # 辅助模式：方形码链路优先；只有整店没有可解码码时才用圆形码兜底。
                has_decodable = any(
                    candidate.get("payload_sha256") and candidate.get("code_type") != "mini_program_code"
                    for image in store["images"] for candidate in image["candidates"]
                )
                if has_decodable:
                    circle = None
            if circle is not None:
                # 第一轮命中即最高优先级：跳过普通码载荷竞争，只做稳定选择与裁剪。
                image_index, candidate_index = circle
                image_record = store["images"][image_index]
                candidate = image_record["candidates"][candidate_index]
                original = source / image_record["path"]
                artifact = collectible_root / store["source_name"] / "二维码.png"
                try:
                    artifact.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    if source in artifact.resolve(strict=False).parents:
                        raise ValueError("归档目标解析后进入素材目录")
                    _crop_candidate(original, candidate["box"], artifact)
                except Exception as exc:
                    artifact.unlink(missing_ok=True)
                    store["reason"] = f"二维码候选裁剪失败（{type(exc).__name__}），需人工复核"
                    continue
                candidate["selected"] = True
                store["status"] = "selected"
                store["reason"] = "第一轮命中微信圆形小程序码，待微信实际验证"
                store["selected_source"] = image_record["path"]
                store["selected_artifact"] = artifact.relative_to(stage).as_posix()
                continue
            if store.get("circle_error"):
                # 圆形扫描异常不等于“确认无圆码”，方形结果只能参考，必须人工复核。
                store["reason"] = "圆形小程序码检测异常，方形候选仅作参考，需人工复核"
                continue
            selected, reason = _select_store(store)
            if selected is None:
                store["reason"] = reason
                continue
            image_index, candidate_index = selected
            image_record = store["images"][image_index]
            candidate = image_record["candidates"][candidate_index]
            original = source / image_record["path"]
            artifact = collectible_root / store["source_name"] / "二维码.png"
            try:
                artifact.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                if source in artifact.resolve(strict=False).parents:
                    raise ValueError("归档目标解析后进入素材目录")
                _crop_candidate(original, candidate["box"], artifact)
            except Exception as exc:
                artifact.unlink(missing_ok=True)
                store["reason"] = f"二维码候选裁剪失败（{type(exc).__name__}），需人工复核"
                continue
            candidate["selected"] = True
            store["status"] = "selected"
            store["reason"] = reason
            store["selected_source"] = image_record["path"]
            store["selected_artifact"] = artifact.relative_to(stage).as_posix()

        review = [
            {"source_name": item["source_name"], "source_folder": item["source_folder"], "reason": item["reason"], "images": [image["path"] for image in item["images"]]}
            for item in stores if item["status"] != "selected"
        ]
        summary = {
            "total_stores": len(stores),
            "total_images": total_images,
            "selected_stores": sum(item["status"] == "selected" for item in stores),
            "review_stores": sum(item["status"] != "selected" for item in stores),
        }
        if circle_inspector is not None:
            summary.update({
                "detection_rounds": 2,
                "circle_hit_stores": sum(bool(item.get("circle_hit")) for item in stores),
                "circle_error_stores": sum(bool(item.get("circle_error")) and not item.get("circle_hit") for item in stores),
                "square_checked_stores": sum(bool(item.get("square_checked")) for item in stores),
            })
        manifest = {
            "input_root": str(source),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stores": stores,
            "summary": summary,
        }
        _atomic_json(stage / "manifest.json", manifest)
        _atomic_json(stage / "待人工确认.json", {"stores": review, "summary": {"count": len(review)}})
        stage.rename(target)
        return manifest
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地按店筛选并裁切二维码候选，不上传素材、不修改原图")
    parser.add_argument("--input", required=True, type=Path, help="素材目录")
    parser.add_argument("--output", required=True, type=Path, help="必须尚不存在的输出目录")
    args = parser.parse_args(argv)

    def show_progress(value: dict) -> None:
        if value["total"]:
            print(f"进度：{value['done']}/{value['total']} 张")

    try:
        result = build_materials(args.input, args.output, progress=show_progress)
    except Exception as exc:
        parser.error(f"预处理失败（{type(exc).__name__}）")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
