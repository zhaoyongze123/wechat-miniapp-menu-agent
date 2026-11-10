from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

from menu_capture.preprocess import build_materials


PAYLOAD = "https://private.example/qr-secret"


def _image(path: Path, color: str = "white", size=(240, 160)) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_result(output: Path) -> dict:
    return json.loads((output / "manifest.json").read_text(encoding="utf-8"))


def test_same_payload_selects_largest_qr_and_keeps_originals_unchanged(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    small = source / "Shop One" / "a.png"
    large = source / "Shop One" / "b.jpg"
    small_hash = _image(small)
    large_hash = _image(large, "gray")
    payload_hash = hashlib.sha256(PAYLOAD.encode()).hexdigest()
    detections = {
        "a.png": [{"box": {"x": 10, "y": 10, "width": 80, "height": 80}, "payload": PAYLOAD}],
        "b.jpg": [{"box": {"x": 30, "y": 20, "width": 100, "height": 90}, "payload": PAYLOAD}],
    }
    progress = []

    manifest = build_materials(source, output, inspector=lambda path: detections[path.name], progress=progress.append)

    assert manifest["summary"] == {"total_stores": 1, "total_images": 2, "selected_stores": 1, "review_stores": 0}
    store = manifest["stores"][0]
    assert store["status"] == "selected"
    assert store["selected_source"] == "Shop One/b.jpg"
    assert store["reason"].endswith("待微信验证")
    assert store["images"][0]["candidates"][0]["payload_sha256"] == payload_hash
    assert all("payload" not in candidate for image in store["images"] for candidate in image["candidates"])
    assert PAYLOAD not in (output / "manifest.json").read_text(encoding="utf-8")
    assert hashlib.sha256(small.read_bytes()).hexdigest() == small_hash
    assert hashlib.sha256(large.read_bytes()).hexdigest() == large_hash
    assert (output / "可采集" / "Shop One" / "二维码.png").is_file()
    assert progress == [{"done": 1, "total": 2}, {"done": 2, "total": 2}]
    with Image.open(output / "可采集" / "Shop One" / "二维码.png") as cropped:
        assert cropped.size == (210, 160)  # 80% 周边留白；超出原图的部分由图像边界裁掉。


def test_no_detection_is_reviewed_and_not_reported_as_qr(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "Shop" / "menu.webp")
    manifest = build_materials(source, output, inspector=lambda _path: [])

    store = manifest["stores"][0]
    assert store["status"] == "needs_review"
    assert store["images"][0]["detection_count"] == 0
    assert store["images"][0]["candidates"] == []
    assert "未检测到二维码" in store["reason"]
    assert not (output / "可采集").exists()
    review = json.loads((output / "待人工确认.json").read_text(encoding="utf-8"))
    assert review["summary"]["count"] == 1


def test_same_code_at_same_size_prefers_sharper_edges(tmp_path: Path) -> None:
    source = tmp_path / "originals"
    store = source / "Shop"
    store.mkdir(parents=True)
    image = Image.new("RGB", (160, 160), "white")
    draw = ImageDraw.Draw(image)
    for x in range(20, 140, 8):
        for y in range(20, 140, 8):
            if ((x - 20) // 8 + (y - 20) // 8) % 2:
                draw.rectangle((x, y, x + 7, y + 7), fill="black")
    image.save(store / "a-sharp.png")
    image.filter(ImageFilter.GaussianBlur(3)).save(store / "z-blurry.png")
    result = build_materials(source, tmp_path / "result", inspector=lambda _: [
        {"box": {"x": 20, "y": 20, "width": 120, "height": 120}, "payload": PAYLOAD}
    ])
    selected = result["stores"][0]
    assert selected["selected_source"] == "Shop/a-sharp.png"
    assert selected["images"][0]["candidates"][0]["sharpness_score"] > selected["images"][1]["candidates"][0]["sharpness_score"]


def test_different_decodable_payloads_block_store_selection(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "Shop" / "one.jpg")
    _image(source / "Shop" / "two.jpeg", "gray")
    detections = {
        "one.jpg": [{"box": {"x": 0, "y": 0, "width": 90, "height": 90}, "payload": "A"}],
        "two.jpeg": [{"box": {"x": 20, "y": 20, "width": 120, "height": 120}, "payload": "B"}],
    }
    store = build_materials(source, output, inspector=lambda path: detections[path.name])["stores"][0]
    assert store["status"] == "needs_review"
    assert "不同二维码载荷" in store["reason"]
    assert store["selected_source"] is None
    assert not (output / "可采集").exists()


def test_small_code_and_payloadless_code_require_manual_review(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "Shop" / "small.png")
    detections = {
        "small.png": [{"box": {"x": 5, "y": 5, "width": 79, "height": 90}, "payload": "same"}],
    }
    store = build_materials(source, output, inspector=lambda path: detections[path.name])["stores"][0]
    assert store["status"] == "needs_review"
    assert "最短边小于 80px" in store["reason"]
    assert store["images"][0]["candidates"][0]["short_edge_px"] == 79
    assert not (output / "可采集").exists()


def test_unknown_payload_candidate_blocks_otherwise_selectable_unique_payload(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "Shop" / "known.png")
    _image(source / "Shop" / "unknown.png", "gray")
    detections = {
        "known.png": [{"box": {"x": 5, "y": 5, "width": 110, "height": 110}, "payload": PAYLOAD}],
        "unknown.png": [{"box": {"x": 10, "y": 10, "width": 120, "height": 120}, "payload": None}],
    }
    store = build_materials(source, output, inspector=lambda path: detections[path.name])["stores"][0]
    assert store["status"] == "needs_review"
    assert "无法解码载荷" in store["reason"]
    assert store["selected_source"] is None
    assert not (output / "可采集").exists()


def test_inspection_error_is_reviewed_without_echoing_exception_text(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "Shop" / "broken-source.png")

    def broken(_path):
        raise RuntimeError(PAYLOAD)

    store = build_materials(source, output, inspector=broken)["stores"][0]
    serialized = (output / "manifest.json").read_text(encoding="utf-8")
    assert store["status"] == "needs_review"
    assert store["images"][0]["detection_count"] == 0
    assert PAYLOAD not in serialized
    assert "RuntimeError" in store["images"][0]["error"]


def test_duplicate_store_basename_under_different_parents_is_not_archived(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "North" / "Same Shop" / "one.png")
    _image(source / "South" / "Same Shop" / "two.png", "gray")
    detected = lambda _path: [{"box": {"x": 10, "y": 10, "width": 100, "height": 100}, "payload": PAYLOAD}]

    manifest = build_materials(source, output, inspector=detected)

    assert len(manifest["stores"]) == 2
    assert all(item["status"] == "needs_review" for item in manifest["stores"])
    assert all("同名店铺目录" in item["reason"] for item in manifest["stores"])
    assert not (output / "可采集").exists()


def test_symlinks_are_ignored_and_input_output_overlap_or_existing_output_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _image(source / "Shop" / "real.png")
    outside = tmp_path / "outside.png"
    _image(outside)
    try:
        (source / "Shop" / "linked.png").symlink_to(outside)
        (source / "linked-folder").symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        pytest.skip("当前文件系统不支持创建 symlink")

    output = tmp_path / "result"
    manifest = build_materials(source, output, inspector=lambda _path: [])
    assert manifest["summary"]["total_images"] == 1
    with pytest.raises(ValueError, match="不能与素材目录相同或互相包含"):
        build_materials(source, source / "nested-output", inspector=lambda _path: [])
    with pytest.raises(FileExistsError, match="必须不存在"):
        build_materials(source, output, inspector=lambda _path: [])


def test_invalid_detection_bounds_go_to_review_without_a_partial_artifact(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "Shop" / "bad.png")
    inspector = lambda _path: [{"box": {"x": 200, "y": 0, "width": 80, "height": 80}, "payload": PAYLOAD}]
    store = build_materials(source, output, inspector=inspector)["stores"][0]
    assert store["status"] == "needs_review"
    assert "ValueError" in store["images"][0]["error"]
    assert not (output / "可采集").exists()


CIRCLE_BOX = {"x": 20, "y": 20, "width": 120, "height": 120}


def _circle(path: Path) -> dict | None:
    if path.parent.name == "A":
        return {"box": CIRCLE_BOX, "detector_confidence": 0.99}
    return None


def test_round_one_hit_skips_square_detection_for_that_store_only(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "A" / "a.jpg")
    _image(source / "A" / "b.jpg", "gray")
    _image(source / "B" / "b.jpg")
    _image(source / "C" / "c.jpg")
    square_calls: list[str] = []
    circle_calls: list[str] = []
    progress: list[dict] = []

    def square(path: Path) -> list[dict]:
        square_calls.append(f"{path.parent.name}/{path.name}")
        if path.parent.name == "B":
            return [{"box": {"x": 20, "y": 20, "width": 120, "height": 120}, "payload": PAYLOAD}]
        return []

    def circle(path: Path) -> dict | None:
        circle_calls.append(f"{path.parent.name}/{path.name}")
        return _circle(path)

    manifest = build_materials(source, output, inspector=square, circle_inspector=circle, progress=progress.append)
    stores = {item["source_name"]: item for item in manifest["stores"]}

    # 门店 A 第一张图命中圆形码后，剩余图片不再做圆形检测，也整店不做方形检测。
    assert circle_calls == ["A/a.jpg", "B/b.jpg", "C/c.jpg"]
    # 只有未命中圆形的 B、C 进入第二轮方形检测。
    assert square_calls == ["B/b.jpg", "C/c.jpg"]
    assert stores["A"]["circle_hit"] is True
    assert stores["A"]["square_checked"] is False
    assert stores["A"]["status"] == "selected"
    assert "第一轮命中" in stores["A"]["reason"]
    assert stores["B"]["status"] == "selected"
    assert stores["B"]["square_checked"] is True
    assert stores["C"]["status"] == "needs_review"
    assert stores["C"]["circle_hit"] is False
    circle_candidate = stores["A"]["images"][0]["candidates"][0]
    assert circle_candidate["payload_sha256"] is None
    assert circle_candidate["code_type"] == "mini_program_code"
    assert circle_candidate["selected"] is True
    assert (output / "可采集" / "A" / "二维码.png").is_file()
    assert manifest["summary"]["circle_hit_stores"] == 1
    assert manifest["summary"]["square_checked_stores"] == 2
    assert manifest["summary"]["detection_rounds"] == 2
    assert progress[-1] == {"done": 8, "total": 8}


def test_circle_hit_on_second_image_skips_all_later_images(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "A" / "a.jpg")
    _image(source / "A" / "b.jpg", "gray")
    _image(source / "A" / "c.jpg", "black")
    circle_calls: list[str] = []
    square_calls: list[str] = []

    def circle(path: Path) -> dict | None:
        circle_calls.append(path.name)
        return {"box": CIRCLE_BOX, "detector_confidence": 0.99} if path.name == "b.jpg" else None

    store = build_materials(
        source, output,
        inspector=lambda path: square_calls.append(path.name) or [],
        circle_inspector=circle,
    )["stores"][0]

    assert circle_calls == ["a.jpg", "b.jpg"]
    assert square_calls == []
    assert [image.get("scan_status") for image in store["images"]] == [None, "circle_hit", "skipped_after_circle_hit"]
    assert store["status"] == "selected"
    assert store["selected_source"] == "A/b.jpg"
    # 未检测图片仍保留 path/sha256/candidates，前端可继续预览原图。
    assert store["images"][0]["candidates"] == []
    assert len(store["images"][1]["candidates"]) == 1
    assert store["images"][2]["candidates"] == []
    assert all(image["sha256"] for image in store["images"])


def test_circle_error_is_review_not_silent_miss(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "A" / "a.jpg")

    def broken(_path: Path):
        raise RuntimeError(PAYLOAD)

    store = build_materials(
        source, output,
        inspector=lambda _path: [{"box": {"x": 20, "y": 20, "width": 120, "height": 120}, "payload": PAYLOAD}],
        circle_inspector=broken,
    )["stores"][0]

    assert store["circle_error"] is True
    assert store["status"] == "needs_review"
    assert "检测异常" in store["reason"]
    assert store["images"][0]["candidates"][0]["selected"] is False
    assert store["images"][0]["candidates"][0]["purpose_hint"] == "unknown"
    serialized = (output / "manifest.json").read_text(encoding="utf-8")
    assert PAYLOAD not in serialized


def test_circle_candidates_use_original_pixels_and_crop_artifact(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    image_path = source / "A" / "a.png"
    _image(image_path, size=(800, 600))
    box = {"x": 200, "y": 150, "width": 240, "height": 240}
    store = build_materials(
        source, output,
        inspector=lambda _path: [],
        circle_inspector=lambda _path: {"box": box, "detector_confidence": 0.42},
    )["stores"][0]

    candidate = store["images"][0]["candidates"][0]
    assert candidate["box"] == {"x": 200.0, "y": 150.0, "width": 240.0, "height": 240.0}
    assert candidate["short_edge_px"] == 240
    assert candidate["purpose"] == "ordering"
    assert candidate["purpose_source"] == "mini_program_code_priority"
    with Image.open(output / "可采集" / "A" / "二维码.png") as cropped:
        assert cropped.size == (624, 582)


def test_out_of_bounds_circle_result_is_treated_as_error_not_miss(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "A" / "a.png", size=(240, 160))
    square_calls: list[str] = []
    store = build_materials(
        source, output,
        inspector=lambda path: square_calls.append(path.name) or [],
        circle_inspector=lambda _path: {"box": {"x": 200, "y": 0, "width": 200, "height": 100}},
    )["stores"][0]

    assert store["circle_hit"] is False
    assert store["circle_error"] is True
    assert store["images"][0]["circle_error"] == "circle_detector_error"
    assert store["status"] == "needs_review"
    assert "检测异常" in store["reason"]
    # 圆形检查异常仍会执行方形检测，但不能自动确认。
    assert square_calls == ["a.png"]


def test_circle_round_only_runs_for_stores_with_two_to_five_images(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "result"
    _image(source / "One" / "a.jpg")
    for name in ("a", "b", "c"):
        _image(source / "Three" / f"{name}.jpg", "gray")
    for name in ("a", "b", "c", "d", "e", "f"):
        _image(source / "Six" / f"{name}.jpg", "black")
    circle_calls: list[str] = []
    square_calls: list[str] = []
    progress: list[dict] = []

    def square(path: Path) -> list[dict]:
        square_calls.append(f"{path.parent.name}/{path.name}")
        return []

    def circle(path: Path) -> dict | None:
        circle_calls.append(f"{path.parent.name}/{path.name}")
        return None

    manifest = build_materials(
        source, output, inspector=square, circle_inspector=circle,
        progress=progress.append, circle_image_range=(2, 5),
    )
    stores = {item["source_name"]: item for item in manifest["stores"]}

    assert circle_calls == ["Three/a.jpg", "Three/b.jpg", "Three/c.jpg"]
    assert len(square_calls) == 10
    assert stores["One"]["circle_skipped"] == "single_image"
    assert stores["Six"]["circle_skipped"] == "many_images"
    assert stores["Three"]["circle_skipped"] == ""
    assert stores["Three"]["circle_images"] == 3
    # 跳过圆形阶段的门店每图只计 1 个单元，2–5 张门店计 2 个。
    assert progress[-1] == {"done": 13, "total": 13}
