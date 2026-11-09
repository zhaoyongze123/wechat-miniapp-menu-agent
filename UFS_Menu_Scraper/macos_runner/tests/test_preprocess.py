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
