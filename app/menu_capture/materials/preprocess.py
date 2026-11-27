"""本地二维码素材筛选。只保存载荷哈希，不保留二维码原文。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from PIL import Image, ImageFilter, ImageStat

from menu_capture.vision.qr_detection import DetectorUnavailable, detect_qr, require_detector

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MIN_QR_EDGE_PX = 80
CROP_MARGIN_RATIO = 0.80

# URL 优先判定用途：链接特征比周边 OCR 更可靠，OCR 只在链接看不出用途时兜底。
_ORDERING_URL_TERMS = (
    "diancai", "dine", "dishes", "tableno", "rms.meituan.com/dc", "rms.meituan.com/diancan",
    # 微信 URL Link 的 /q/ 前缀；/r/ 是通用短链，不能按前缀定性。
    "weixin.qq.com/q/",
)
# 仅保留明确的非点餐域名。
_NON_ORDERING_URL_TERMS = ("wjx.cn",)
# 已验证的具体入口路径优先于宽泛域名/关键词：同一域名下不同路径可能用途相反。
_ORDERING_PATH_RULES = (
    ("rms.meituan.com", "/diancan"),
    ("rms.meituan.com", "/dc"),
    ("weixin.qq.com", "/q/"),
)
# 实测：同一 host+path 的 dprebate/rebate 既可能打开点餐菜单（5/5 合格门店截图），
# 也可能打开团购券/打卡页（海报实测）。差异在 ?code= 参数，而参数不落库，
# 因此路径只能判为 ambiguous，必须由周边 OCR 决定。
_AMBIGUOUS_PATH_RULES = (
    ("apimobile.meituan.com", "/qrcode/dprebate"),
    ("apimobile.meituan.com", "/qrcode/rebate"),
)
_ENTERPRISE_HOSTS = {"work.weixin.qq.com", "qy.weixin.qq.com"}
# 合格门店（有截图输出）验证过的点餐 SaaS 域名，路径随机串不稳定，按域名判定。
_ORDERING_HOSTS = {
    "app.welcrm.com", "qrse.keruyun.com", "qr.supermenus.cn", "2dfire.cn", "www.2dfire.com",
    "qr.qmai.cn", "rom.paidui.com", "pos.flyhand.com", "qr61.cn", "sh6.co",
    "cscte.fuioupay.com", "hljh.pinzhikeji.net", "wx-kaci.shouqianba.com",
}
# 餐饮 SaaS/POS 域名但路径不足以单独定性：必须同时命中周边 OCR 的点餐文字才认定为点餐。
# qrcode.dianping.com 既可能是“扫码点餐”，也可能是团购/买单入口，必须由 OCR 区分。
_LIKELY_ORDERING_HOSTS = {
    "qrcode.dianping.com", "m.restosuite.cn", "m.hualala.com", "applet.mtcbj.com", "pos.mzdpgroup.com",
    "d.hocyun.com", "m.sqbe.cn", "yun.bypos.net", "cy.wrhmx.com", "ogs-api.gmonline-all.com",
}


def _path_rule_matches(host: str, path: str, host_suffix: str, path_prefix: str) -> bool:
    """按“域名后缀 + 路径前缀”匹配具体入口，避免同一个词出现在随机路径里被误判。"""
    if not (host == host_suffix or host.endswith("." + host_suffix)):
        return False
    if path_prefix.endswith("/"):
        return path.startswith(path_prefix)
    return path == path_prefix or path.startswith(path_prefix + "/")


def _payload_purpose(payload) -> str:
    """按链接特征给出用途提示，返回 ordering/not_ordering/ordering_candidate/enterprise_wechat/conflict/unknown。

    判定分两级：具体入口路径优先；具体路径给不出结论时再看宽泛域名和关键词。
    同级证据相互矛盾（同时命中点餐与非点餐）时不强行裁定，返回 conflict 交给人工。
    """
    if not isinstance(payload, str) or not payload:
        return "unknown"
    raw = payload.lower()
    try:
        parsed = urlsplit(payload)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
    except ValueError:
        host, path = "", ""
    if host in _ENTERPRISE_HOSTS or host.endswith(".work.weixin.qq.com"):
        return "enterprise_wechat"
    if any(_path_rule_matches(host, path, rule_host, rule_path) for rule_host, rule_path in _AMBIGUOUS_PATH_RULES):
        return "ordering_candidate"
    if any(_path_rule_matches(host, path, rule_host, rule_path) for rule_host, rule_path in _ORDERING_PATH_RULES):
        return "ordering"
    # 具体路径得不出结论后，域名与关键词属于同一级证据；两类同时命中时转人工。
    ordering_broad = host in _ORDERING_HOSTS or any(term in raw for term in _ORDERING_URL_TERMS)
    non_ordering_broad = any(term in raw for term in _NON_ORDERING_URL_TERMS)
    if ordering_broad and non_ordering_broad:
        return "conflict"
    if ordering_broad:
        return "ordering"
    if non_ordering_broad:
        return "not_ordering"
    if host in _LIKELY_ORDERING_HOSTS:
        return "ordering_candidate"
    return "unknown"


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
                record["dropped_detections"] = record.get("dropped_detections", 0) + 1
                continue
            x, y, width, height = map(float, coords)
            # 单个越界框只丢弃自身，不能让整张图判定失败：EXIF 旋转、缩放换算都可能产生个别脏框。
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > image_width or y + height > image_height:
                record["dropped_detections"] = record.get("dropped_detections", 0) + 1
                continue
            payload = detection.get("payload")
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest() if isinstance(payload, str) and payload else None
            # 本地只保存用途提示与散列，不记录二维码内容；用途先按链接判定，OCR 只做兜底。
            short_edge, sharpness, quality_score = _box_scores(path, {"x": x, "y": y, "width": width, "height": height})
            qr_likeness = detection.get("qr_likeness") is not False
            qr_contained = detection.get("qr_contained") is not False
            qr_centered = detection.get("qr_centered") is not False
            qr_fill = detection.get("qr_fill")
            if isinstance(qr_fill, (int, float)) and not isinstance(qr_fill, bool):
                qr_fill = round(float(qr_fill), 4)
            else:
                qr_fill = None
            # 质量门禁只拦截有明确结构/几何问题的候选，仍在清单中保留，交给人工复核。
            quality, quality_reason = "ok", ""
            if not qr_likeness:
                quality, quality_reason = "rejected", "候选框未通过二维码结构校验"
            elif not qr_contained:
                quality, quality_reason = "rejected", "二维码超出候选框"
            elif qr_fill is not None and qr_fill < 0.15:
                quality, quality_reason = "rejected", "二维码面积占候选框过低"
            elif not qr_centered:
                quality, quality_reason = "rejected", "二维码明显偏离候选框中心"
            # 只比较同码的可解码候选。尺寸和边缘变化是排序启发式，不代表微信扫码成功率。
            candidate = {
                "box": {"x": x, "y": y, "width": width, "height": height},
                "payload_sha256": payload_hash,
                "purpose_hint": _payload_purpose(payload),
                "short_edge_px": short_edge,
                "sharpness_score": sharpness,
                "quality_score": quality_score,
                "qr_likeness": qr_likeness,
                "qr_fill": qr_fill,
                "qr_contained": qr_contained,
                "qr_centered": qr_centered,
                "quality": quality,
                **({"quality_reason": quality_reason} if quality_reason else {}),
                "selected": False,
            }
            # 微信圆形小程序码标准解码器解不出内容，只由 worker 定位；带上标记供前端与人工识别。
            if detection.get("kind") == "wechat_mini_program":
                candidate["kind"] = "wechat_mini_program"
            record["candidates"].append(candidate)
        # 全部检测框都越界说明坐标换算整体失真，仍按图片检查失败处理，避免静默丢码。
        if detections and not record["candidates"]:
            raise ValueError("二维码位置全部超出原图边界")
    except DetectorUnavailable:
        # 检测器不可用是系统性失败，不能吞成单图 error 后让任务照常 completed。
        raise
    except Exception as exc:
        # 不记录异常文本，防止某些解码器把条码内容写入错误消息。
        record["error"] = f"本地图片/二维码检查失败（{type(exc).__name__})"
    return record


def _inspect_image(path: Path, relative: str, inspector: Callable[[Path], list[dict]]) -> dict:
    record, size = _prepare_image(path, relative)
    if size is None:
        return record
    return _detect_square(path, record, size, inspector)


def inspect_store_images(
    images: list[dict],
    resolve_path: Callable[[dict], Path],
    inspector: Callable[[Path], list[dict]],
    on_unit: Callable[[], None] | None = None,
) -> dict:
    """对门店内每一张图都执行方形二维码检测；不做图片数分流。"""
    stats = {"square_checked": True, "square_ms": 0.0, "image_errors": 0, "detected_images": 0}
    unit = on_unit or (lambda: None)
    started = time.perf_counter()
    for image in images:
        path = resolve_path(image)
        meta = {key: image[key] for key in ("display_name", "origin", "supplement_store_id") if key in image}
        record, size = _prepare_image(path, image["path"])
        image.clear()
        image.update(record)
        image.update(meta)
        if size is not None:
            _detect_square(path, image, size, inspector)
        if image.get("error"):
            stats["image_errors"] += 1
        elif image["candidates"]:
            stats["detected_images"] += 1
        unit()
    stats["square_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return stats


def _store_detection_state(stats: dict) -> str:
    """门店检测状态：区分单图异常、正常未识别与识别到候选，供统计与人工复核如实展示。"""
    if stats["image_errors"]:
        return "image_error"
    if stats["detected_images"]:
        return "detected"
    return "not_detected"


def _select_store(store: dict) -> tuple[tuple[int, int] | None, str]:
    if any("error" in image for image in store["images"]):
        return None, "至少一张原图读取或二维码检查失败，需人工复核"
    candidates = [candidate for image in store["images"] for candidate in image["candidates"]]
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


def _apply_orientation(image: Image.Image, orientation: int) -> Image.Image:
    """按 EXIF Orientation 把裁剪结果转正；只处理常见的 3/6/8。"""
    transpose = getattr(Image, "Transpose", Image)
    if orientation == 3:
        return image.transpose(transpose.ROTATE_180)
    if orientation == 6:
        return image.transpose(transpose.ROTATE_270)
    if orientation == 8:
        return image.transpose(transpose.ROTATE_90)
    return image


def _crop_candidate(source_path: Path, box: dict, destination: Path,
                    margin_ratio: float = CROP_MARGIN_RATIO) -> None:
    # 自动检测候选按比例扩边；人工框选传 0，保证落图与框选完全一致。
    with Image.open(source_path) as image:
        image.load()
        orientation = int(image.getexif().get(274, 1) or 1)
        width, height = image.size
        qr_width, qr_height = box["width"], box["height"]
        left = max(0, int(math.floor(box["x"] - qr_width * margin_ratio)))
        top = max(0, int(math.floor(box["y"] - qr_height * margin_ratio)))
        right = min(width, int(math.ceil(box["x"] + qr_width * (1 + margin_ratio))))
        bottom = min(height, int(math.ceil(box["y"] + qr_height * (1 + margin_ratio))))
        cropped = image.crop((left, top, right, bottom))
        # 裁剪坐标始终基于原图像素；输出按 EXIF 方向转正，保证人工框选、自动候选
        # 与最终发送到聊天的二维码方向完全一致。
        cropped = _apply_orientation(cropped, orientation)
        if cropped.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
            cropped = cropped.convert("RGB")
        # PNG 压缩级别 1 为无损快速压缩，像素与默认级别 6 完全一致，批量发布提速 3-4 倍。
        cropped.save(destination, format="PNG", compress_level=1)


def _crop_candidate_with_pixel_digest(source_path: Path, box: dict, destination: Path,
                                      margin_ratio: float = CROP_MARGIN_RATIO) -> str:
    """裁剪并落盘 PNG，同时直接从内存图像计算 RGBA 像素摘要，避免落盘后再次解码 PNG。"""
    import struct

    with Image.open(source_path) as image:
        image.load()
        orientation = int(image.getexif().get(274, 1) or 1)
        width, height = image.size
        qr_width, qr_height = box["width"], box["height"]
        left = max(0, int(math.floor(box["x"] - qr_width * margin_ratio)))
        top = max(0, int(math.floor(box["y"] - qr_height * margin_ratio)))
        right = min(width, int(math.ceil(box["x"] + qr_width * (1 + margin_ratio))))
        bottom = min(height, int(math.ceil(box["y"] + qr_height * (1 + margin_ratio))))
        cropped = image.crop((left, top, right, bottom))
        cropped = _apply_orientation(cropped, orientation)
        if cropped.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
            cropped = cropped.convert("RGB")
        cropped.save(destination, format="PNG", compress_level=1)
        rgba = cropped.convert("RGBA")
        digest = hashlib.sha256(struct.pack(">II", *rgba.size))
        digest.update(rgba.tobytes())
        return digest.hexdigest()


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
    on_unit: Callable[[], None] | None = None,
    resolve_path: Callable[[dict], Path] | None = None,
) -> dict:
    """对单个门店的每张图执行方形二维码检测，返回补充了耗时字段的门店字典。

    只做检测，不做“选哪张图”的语义决策；决策层（MaterialService）在整批和逐店
    两条路径上都调用同一份 _classify_and_select，保证结果一致。
    """
    unit = on_unit or (lambda: None)
    resolve = resolve_path or (lambda image: Path(source_root) / image["path"])
    stats = inspect_store_images(
        store["images"], resolve, inspector or detect_qr, unit,
    )
    store["square_checked"] = stats["square_checked"]
    store["square_ms"] = stats["square_ms"]
    store["detection_state"] = _store_detection_state(stats)
    return store


def build_materials(
    input_root: Path,
    output_root: Path,
    inspector: Callable[[Path], list[dict]] | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """按图片直接父目录分店，对每张图做方形二维码检测并筛出每店一张候选。

    inspector(path) 返回 ``[{"box": {"x", "y", "width", "height"}, "payload": str|None}]``。
    payload 只用于进程内计算 SHA256，manifest/review/crop 均不保存它。
    progress 收到的值只有 done/total 计数。
    """
    source, target = _ensure_paths(Path(input_root), Path(output_root))
    inspect = inspector or detect_qr
    if inspector is None:
        # 命令行/无注入场景先做能力预检：依赖缺失时直接失败，不逐个图片吞异常。
        require_detector()
    stores, duplicate_names = discover_stores(source)
    total_images = sum(len(store["images"]) for store in stores)
    total_units = total_images
    done_units = 0

    def unit() -> None:
        nonlocal done_units
        done_units += 1
        _safe_progress(progress, done_units, total_units)

    for store in stores:
        analyze_store(store, source, inspector=inspect, on_unit=unit)

    stage = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
    stage.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        collectible_root = stage / "可采集"
        for store in stores:
            if store["source_name"] in duplicate_names:
                store["reason"] = "不同父路径存在同名店铺目录，拒绝归档以防混店"
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
