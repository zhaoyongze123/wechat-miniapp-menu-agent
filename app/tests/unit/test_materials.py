from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from menu_capture.core.config import ConfigStore
from menu_capture.materials.materials import MaterialService, _classify_local_purpose, _mkdir_ok
from menu_capture.storage.store import TaskStore


PAYLOAD = "https://private.example/material-secret"


def _png(path: Path, color: str = "white", size=(240, 160)) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path.read_bytes()


def _bytes_png(color: str = "white", size=(240, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _setup(tmp_path: Path, inspector=None, ocr_reader=None):
    root = tmp_path / "data"
    input_root = tmp_path / "inbox"
    output_root = tmp_path / "old-output"
    input_root.mkdir()
    config = ConfigStore(tmp_path / "config")
    config.save({
        "input_dir": str(input_root), "output_dir": str(output_root),
        "model": {"base_url": "https://vision.example/v1", "model": "vision", "api_key": "test-secret"},
    })
    store = TaskStore(tmp_path / "tasks.sqlite3")
    service = MaterialService(
        root, config, store, inspector=inspector or _detector,
        ocr_reader=ocr_reader or (lambda _path: [{"text": "扫码点餐", "x": 40, "y": 130, "width": 100, "height": 20}]),
    )
    return service, config, store, input_root, root


def _detector(_path: Path) -> list[dict]:
    return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": PAYLOAD}]


def _wait(service: MaterialService, batch_id: str) -> dict:
    """等待持久化 job 结束；批次状态只表示结果是否可用，不再承载运行态。"""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        batch = service.get_batch(batch_id)
        job = batch.get("job")
        if job is None:
            return batch
        if job["status"] == "completed":
            if batch["status"] == "completed":
                return batch
        elif job["status"] in {"failed", "cancelled", "paused", "interrupted"}:
            return batch
        time.sleep(0.02)
    raise AssertionError("素材分析 job 在 20 秒内未结束")


def _analyze(service: MaterialService, source: Path) -> dict:
    batch = service.create_local_batch(source)
    assert batch["status"] == "ready_to_analyze"
    assert batch["progress"]["done"] == 0
    service.start_analysis(batch["batch_id"])
    result = _wait(service, batch["batch_id"])
    assert result["status"] == "completed", result["error"]
    return result


def test_local_batch_requires_explicit_analysis_and_separates_recommendation_from_confirmation(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(tmp_path)
    original = input_root / "Brand A" / "menu.png"
    _png(original)

    batch = service.create_local_batch(input_root)
    assert batch["status"] == "ready_to_analyze"
    assert service.get_batch(batch["batch_id"])["stores"] == []
    completed = _wait_after_start(service, batch["batch_id"])

    store_row = completed["stores"][0]
    assert store_row["recommendation"] is True
    assert store_row["decision"] == "pending"
    assert completed["summary"]["selected_stores"] == 1
    assert completed["summary"]["confirmed_stores"] == 0
    assert completed["summary"]["pending_stores"] == 1
    assert service.get_store(batch["batch_id"], store_row["store_id"])["selected"] is None


def test_batch_summary_uses_local_ocr_final_store_decisions(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(tmp_path)
    _png(input_root / "Shop" / "menu.png")

    batch = _analyze(service, input_root)

    assert batch["stores"][0]["analysis_status"] == "selected"
    assert batch["stores"][0]["recommendation"] is True
    assert batch["summary"]["selected_stores"] == 1
    assert batch["summary"]["review_stores"] == 0


def _wait_after_start(service: MaterialService, batch_id: str) -> dict:
    service.start_analysis(batch_id)
    return _wait(service, batch_id)


def test_uploads_are_serial_file_bytes_with_limits_and_path_validation(tmp_path: Path, monkeypatch) -> None:
    service, _, _, _, _ = _setup(tmp_path)
    batch = service.begin_upload()
    assert batch["status"] == "uploading"
    with pytest.raises(ValueError, match="不能包含空段、. 或 .."):
        service.add_upload_file(batch["batch_id"], "Shop/../escape.png", _bytes_png())
    with pytest.raises(ValueError, match="不能是绝对路径"):
        service.add_upload_file(batch["batch_id"], "/tmp/escape.png", _bytes_png())
    with pytest.raises(ValueError, match="不能是绝对路径"):
        service.add_upload_file(batch["batch_id"], "C:\\escape.png", _bytes_png())
    with pytest.raises(ValueError, match="只允许上传"):
        service.add_upload_file(batch["batch_id"], "Shop/not-image.txt", b"not an image")
    with pytest.raises(ValueError, match="不是有效图片"):
        service.add_upload_file(batch["batch_id"], "Shop/fake.png", b"not an image")

    monkeypatch.setattr("menu_capture.materials.materials.MAX_FILES", 0)
    with pytest.raises(ValueError, match="超过 10000 限制"):
        service.add_upload_file(batch["batch_id"], "Shop/menu.png", _bytes_png())
    assert service.get_batch(batch["batch_id"])["progress"]["total"] == 0


def test_upload_batch_finishes_then_analyzes_one_file_at_a_time(tmp_path: Path) -> None:
    service, _, _, _, _ = _setup(tmp_path)
    batch = service.begin_upload()
    data = _bytes_png()
    upload = service.add_upload_file(batch["batch_id"], "Root/Shop/menu.png", data)
    assert upload["file_count"] == 1
    assert upload["total_bytes"] == len(data)
    ready = service.finish_upload(batch["batch_id"])
    assert ready["status"] == "ready_to_analyze"
    assert ready["progress"]["total"] == 1
    done = _wait_after_start(service, batch["batch_id"])
    assert done["status"] == "completed"
    assert done["summary"]["total_images"] == 1
    assert done["stores"][0]["source_folder"] == "Root/Shop"


def test_detail_preview_decision_and_publish_snapshot_are_safe_and_idempotent(tmp_path: Path) -> None:
    service, config, task_store, input_root, _ = _setup(tmp_path)
    original = input_root / "Shop One" / "menu.png"
    source_hash = hashlib.sha256(_png(original)).hexdigest()
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["decision"] == "pending"
    assert detail["selected"] is None
    assert detail["images"][0]["sha256"] == source_hash
    assert detail["images"][0]["candidates"][0]["recommendation"] is True
    assert "quality_score" in detail["images"][0]["candidates"][0]
    assert PAYLOAD not in json.dumps(detail, ensure_ascii=False)
    with task_store.connect() as db:
        saved_manifest = db.execute("SELECT manifest_json FROM material_batches WHERE id=?", (batch["batch_id"],)).fetchone()[0]
    assert PAYLOAD not in saved_manifest

    original_preview = service.preview_original(batch["batch_id"], store["store_id"], 0)
    candidate_preview = service.preview_candidate(batch["batch_id"], store["store_id"], 0, 0)
    assert original_preview["bytes"] == original.read_bytes()
    assert original_preview["media_type"] == "image/png"
    assert candidate_preview["media_type"] == "image/png"
    with Image.open(io.BytesIO(candidate_preview["bytes"])) as preview_image:
            # 所有门店都跑方形检测：按 0.8 倍码宽外扩后右侧到 210px。
            assert preview_image.size == (210, 160)

    accepted = service.accept_recommendations(batch["batch_id"])
    assert accepted["confirmed"] == 1
    assert service.get_store(batch["batch_id"], store["store_id"])["decision"] == "confirmed"
    result = service.publish_snapshot(batch["batch_id"])
    assert result["task_scan"]["added"] == 1
    assert config.load().input_dir == result["input_dir"]
    assert len(task_store.tasks()) == 1
    assert (Path(result["input_dir"]) / "Shop One" / "二维码.png").is_file()
    again = service.publish_snapshot(batch["batch_id"])
    assert again["snapshot_id"] == result["snapshot_id"]
    assert again["task_scan"]["added"] == 0
    assert again["already_published"] is True
    assert len(task_store.tasks()) == 1
    artifact = Path(result["input_dir"]) / "Shop One" / "二维码.png"
    artifact.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="图片缺失或 hash 已变化"):
        service.publish_snapshot(batch["batch_id"])


def test_unrecognized_image_can_be_manually_selected_by_box(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(tmp_path, inspector=lambda _path: [])
    _png(input_root / "Manual Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    # 未定位到二维码时不做任何自动推荐，全部留给人工。
    assert store["analysis_status"] == "needs_review"
    assert store["recommendation"] is False
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["images"][0]["candidates"] == []
    confirmed = service.decide_store(
        batch["batch_id"], store["store_id"], "confirm", image_index=0,
        box={"x": 40, "y": 30, "width": 90, "height": 90},
    )
    assert confirmed["decision"] == "confirmed"
    assert confirmed["selected"] == {
        "image_index": 0, "candidate_index": None,
        "box": {"x": 40.0, "y": 30.0, "width": 90.0, "height": 90.0},
        "crop_margin_ratio": 0.0, "box_space": "source",
    }
    result = service.publish_snapshot(batch["batch_id"])
    assert result["task_scan"]["added"] == 1
    # 人工框选即最终裁剪：产物尺寸必须与框选一致，不能带自动扩边。
    with Image.open(Path(result["input_dir"]) / "Manual Shop" / "二维码.png") as artifact:
        assert artifact.size == (90, 90)


def test_source_hash_change_rejects_confirmation_and_publish(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(tmp_path)
    original = input_root / "Brand" / "menu.png"
    _png(original)
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    _png(original, "black")
    with pytest.raises(ValueError, match="hash 已变化"):
        service.decide_store(batch["batch_id"], store["store_id"], "confirm", 0, 0)

    # 恢复分析时的原始字节并确认，再改源文件，发布阶段也必须拒绝。
    original.write_bytes(_png_bytes_for_source())
    service.decide_store(batch["batch_id"], store["store_id"], "confirm", 0, 0)
    _png(original, "blue")
    with pytest.raises(ValueError, match="hash 已变化"):
        service.publish_snapshot(batch["batch_id"])


def _png_bytes_for_source() -> bytes:
    return _bytes_png()


def test_skip_pending_and_adopted_recommendation_are_not_implicit_confirmations(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(tmp_path)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    original_store = batch["stores"][0]
    skipped = service.decide_store(batch["batch_id"], original_store["store_id"], "skip")
    assert skipped["decision"] == "skipped"
    pending = service.decide_store(batch["batch_id"], original_store["store_id"], "pending")
    assert pending["decision"] == "pending"

    manifest_path = service.batch_root / batch["batch_id"] / "analysis" / "manifest.json"
    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for store_data in old_manifest["stores"]:
        for image_data in store_data["images"]:
            for candidate in image_data["candidates"]:
                candidate.pop("sharpness_score", None)
                candidate.pop("quality_score", None)
    manifest_path.write_text(json.dumps(old_manifest, ensure_ascii=False), encoding="utf-8")
    adopted = service.adopt_manifest(manifest_path)
    adopted_store = adopted["stores"][0]
    assert adopted["status"] == "completed"
    assert adopted_store["recommendation"] is True
    assert adopted_store["decision"] == "pending"
    assert adopted["summary"]["confirmed_stores"] == 0
    adopted_detail = service.get_store(adopted["batch_id"], adopted_store["store_id"])
    assert adopted_detail["images"][0]["candidates"][0]["purpose"] == "ordering"


def test_confirmed_decision_prevents_publish_after_source_root_path_escape(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(tmp_path)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    service.decide_store(batch["batch_id"], store["store_id"], "confirm", 0, 0)
    # Store detail paths are relative and traversal cannot be introduced by the caller's id/index.
    with pytest.raises(ValueError, match="素材店铺不存在"):
        service.get_store(batch["batch_id"], "../../outside")


def test_legacy_running_batch_is_recovered_and_can_restart(tmp_path: Path) -> None:
    service, _, task_store, input_root, root = _setup(tmp_path)
    _png(input_root / "Shop" / "menu.png")
    batch = service.create_local_batch(input_root)
    with task_store.connect() as db:
        db.execute("UPDATE material_batches SET status='analyzing' WHERE id=?", (batch["batch_id"],))

    restarted = MaterialService(root, service.configs, task_store, inspector=_detector)
    status = restarted.get_batch(batch["batch_id"])
    # 旧版本遗留的运行态：没有决策行就回到可分析，由用户显式重新开始，不再永久中断。
    assert status["status"] == "ready_to_analyze"
    assert status["stores"] == []
    restarted.start_analysis(batch["batch_id"])
    assert _wait(restarted, batch["batch_id"])["status"] == "completed"


def test_only_one_analysis_job_runs_and_second_batch_is_queued(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def slow_inspector(path: Path) -> list[dict]:
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=3)
        return _detector(path)

    service, _, _, input_root, _ = _setup(tmp_path, inspector=slow_inspector)
    _png(input_root / "First Shop" / "one.png")
    _png(input_root / "First Shop" / "z-one.png", "gray")
    _png(input_root / "Second Shop" / "two.png", "gray")
    _png(input_root / "Second Shop" / "z-two.png", "black")
    first = service.create_local_batch(input_root / "First Shop")
    second = service.create_local_batch(input_root / "Second Shop")
    service.job_runner.start()
    try:
        service.start_analysis(first["batch_id"])
        assert entered.wait(timeout=3)
        # 全局单通道：第二个批次不再 409，而是排队等待第一个结束。
        assert service.start_analysis(second["batch_id"])["job"]["status"] == "queued"
        with pytest.raises(ValueError, match="分析未完成"):
            service.decide_store(first["batch_id"], "unknown", "skip")
        release.set()
        assert _wait(service, first["batch_id"])["status"] == "completed"
        assert _wait(service, second["batch_id"])["status"] == "completed"
    finally:
        service.job_runner.stop()


def test_local_ocr_selects_point_ordering_code_and_keeps_safe_fields(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        if path.name != "menu.png":
            return []
        return [
            {"box": {"x": 30, "y": 20, "width": 90, "height": 90}, "payload": "private-one"},
            {"box": {"x": 500, "y": 20, "width": 70, "height": 70}, "payload": "https://work.weixin.qq.com/private-two"},
        ]

    service, _, task_store, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        ocr_reader=lambda _path: [{"text": "扫码点餐", "x": 40, "y": 130, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu.png", size=(800, 300))
    _png(input_root / "Shop" / "z-extra.png", "gray")
    batch = _analyze(service, input_root)
    store = service.get_store(batch["batch_id"], batch["stores"][0]["store_id"])
    candidates = store["images"][0]["candidates"]
    assert candidates[0]["purpose"] == "ordering"
    assert candidates[0]["purpose_reason"] == "周边 OCR 识别到“扫码点餐”"
    assert candidates[1]["purpose"] == "enterprise_wechat"
    assert candidates[0]["recommendation"] is True, store["reason"]
    assert candidates[1]["recommendation"] is False
    serialized = json.dumps(store, ensure_ascii=False)
    assert "private-one" not in serialized and "test-secret" not in serialized
    with task_store.connect() as db:
        manifest = db.execute("SELECT manifest_json FROM material_batches WHERE id=?", (batch["batch_id"],)).fetchone()[0]
    assert "private-one" not in manifest and "test-secret" not in manifest


def test_two_local_ordering_codes_in_one_image_require_manual_review(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        if path.name != "menu.png":
            return []
        return [
            {"box": {"x": 20, "y": 20, "width": 100, "height": 100}, "payload": "private-one"},
            {"box": {"x": 450, "y": 20, "width": 160, "height": 160}, "payload": "private-two"},
        ]

    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        ocr_reader=lambda _path: [
            {"text": "扫码点餐", "x": 35, "y": 130, "width": 100, "height": 20},
            {"text": "请扫码点餐", "x": 480, "y": 210, "width": 100, "height": 20},
        ],
    )
    _png(input_root / "Shop" / "menu.png", size=(800, 300))
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is False
    assert store["analysis_status"] == "needs_review"
    assert "同一图片" in store["reason"]
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["images"][0]["is_multi_qr_image"] is True
    assert all(not c["selected"] for c in detail["images"][0]["candidates"])


def test_ambiguous_image_does_not_block_clear_ordering_code_from_other_image(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        if path.name == "multi.png":
            return [
                {"box": {"x": 20, "y": 20, "width": 100, "height": 100}, "payload": "multi-one"},
                {"box": {"x": 300, "y": 20, "width": 100, "height": 100}, "payload": "multi-two"},
            ]
        if path.name == "clear.png":
            return [{"box": {"x": 20, "y": 20, "width": 180, "height": 180}, "payload": "clear-one"}]
        return []

    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        ocr_reader=lambda path: [
            {"text": "扫码点餐", "x": 30, "y": 130, "width": 100, "height": 20},
            {"text": "扫码点餐", "x": 310, "y": 130, "width": 100, "height": 20},
        ] if path.name == "multi.png" else [
            {"text": "扫码点餐", "x": 30, "y": 210, "width": 120, "height": 24},
        ],
    )
    _png(input_root / "Shop" / "multi.png", size=(600, 300))
    _png(input_root / "Shop" / "clear.png", size=(400, 300))
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is True
    detail = service.get_store(batch["batch_id"], store["store_id"])
    images = {image["path"]: image for image in detail["images"]}
    assert all(not c["selected"] for c in images["Shop/multi.png"]["candidates"])
    assert any(c["selected"] for c in images["Shop/clear.png"]["candidates"])


def test_ordering_codes_from_different_images_choose_the_best_quality(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        size = 80 if path.name == "small.png" else 180
        return [{"box": {"x": 20, "y": 20, "width": size, "height": size}, "payload": path.stem}]

    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        ocr_reader=lambda _path: [
            {"text": "扫码点餐", "x": 25, "y": 220, "width": 120, "height": 24},
        ],
    )
    _png(input_root / "Shop" / "small.png", size=(400, 300))
    _png(input_root / "Shop" / "large.png", size=(400, 300))
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is True
    detail = service.get_store(batch["batch_id"], store["store_id"])
    selected_image = next(
        image["path"] for image in detail["images"] if any(c["selected"] for c in image["candidates"])
    )
    assert selected_image == "Shop/large.png"


def test_undecodable_candidate_still_counts_as_multi_code_image(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
        return [
            {"box": {"x": 20, "y": 20, "width": 120, "height": 120}, "payload": "decoded"},
            {"box": {"x": 300, "y": 20, "width": 120, "height": 120}, "payload": None},
        ]

    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        ocr_reader=lambda _path: [
            {"text": "扫码点餐", "x": 25, "y": 150, "width": 100, "height": 20},
        ],
    )
    _png(input_root / "Shop" / "menu.png", size=(500, 300))
    batch = _analyze(service, input_root)
    detail = service.get_store(batch["batch_id"], batch["stores"][0]["store_id"])
    assert detail["images"][0]["is_multi_qr_image"] is True
    assert detail["images"][0]["candidates"][1]["payload_sha256"] is None
    assert batch["stores"][0]["recommendation"] is True


def test_small_locally_decoded_ordering_code_is_not_rejected_by_arbitrary_pixel_limit(tmp_path: Path) -> None:
    def small_detector(_path: Path) -> list[dict]:
        return [{"box": {"x": 20, "y": 20, "width": 60, "height": 60}, "payload": "private-small"}]

    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=small_detector,
        ocr_reader=lambda _path: [{"text": "扫码点餐", "x": 25, "y": 90, "width": 90, "height": 18}],
    )
    _png(input_root / "Shop" / "menu.png", size=(240, 160))
    batch = _analyze(service, input_root)
    assert batch["stores"][0]["recommendation"] is True
    assert batch["stores"][0]["analysis_status"] == "selected"


def test_unreadable_code_with_point_ordering_text_is_recommended(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=lambda path: (
            [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": None}]
            if path.name == "menu.png" else []
        ),
    )
    _png(input_root / "Shop" / "menu.png")
    _png(input_root / "Shop" / "z-extra.png", "gray")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    # 未解码但周边识别到点餐文字：按点餐候选推荐，由微信侧最终验证。
    assert store["recommendation"] is True
    assert store["analysis_status"] == "selected"
    assert service.accept_recommendations(batch["batch_id"])["confirmed"] == 1


def test_multi_image_unknown_purpose_candidate_goes_to_manual_review(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(
        tmp_path,
        ocr_reader=lambda _path: [{"text": "欢迎光临", "x": 40, "y": 130, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu-a.png")
    _png(input_root / "Shop" / "menu-b.png", color="blue")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    # 用途不明不能自动推荐，也不再进入批量接受。
    assert store["recommendation"] is False
    assert store["analysis_status"] == "needs_review"
    assert service.accept_recommendations(batch["batch_id"])["confirmed"] == 0


def test_ocr_purpose_prefers_non_ordering_terms_over_weak_order() -> None:
    # 团购卡常写“扫码下单 享优惠”，必须先命中“抢购/团购”，不能当成点餐。
    assert _classify_local_purpose("扫码下单 单单享优惠 美团抢购")[0] == "not_ordering"
    assert _classify_local_purpose("扫码点餐 不用等")[0] == "ordering"
    assert _classify_local_purpose("扫码下单")[0] == "ordering"
    assert _classify_local_purpose("体验不满意 扫码来吐槽")[0] == "not_ordering"
    # 美团 dprebate 路径也用于打卡/收藏海报，必须靠周边文字排除。
    assert _classify_local_purpose("大众点评App扫码打卡 打卡+收藏")[0] == "not_ordering"
    assert _classify_local_purpose("打卡+收藏 送饮品")[0] == "not_ordering"
    assert _classify_local_purpose("点评优惠买单 扫码买券更优惠")[0] == "not_ordering"


def test_likely_ordering_host_with_nearby_ocr_text_is_recommended(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        return ([{"box": {"x": 30, "y": 20, "width": 100, "height": 100},
                  "payload": "https://m.restosuite.cn/open/app/x/table"}]
                if path.name == "menu.png" else [])

    service, _, _, input_root, _ = _setup(
        tmp_path, inspector=detector,
        ocr_reader=lambda _path: [{"text": "扫码点餐", "x": 35, "y": 130, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu.png")
    _png(input_root / "Shop" / "z-extra.png", "gray")
    batch = _analyze(service, input_root)
    assert batch["stores"][0]["recommendation"] is True
    detail = service.get_store(batch["batch_id"], batch["stores"][0]["store_id"])
    candidate = detail["images"][0]["candidates"][0]
    assert candidate["purpose"] == "ordering"
    assert candidate["purpose_source"] == "payload_url_ocr"
    assert candidate["purpose_reason"] == "餐饮点餐域名且周边 OCR 识别到点餐标识"


def test_likely_ordering_host_without_ocr_text_stays_unknown(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        suffix = "one" if path.name == "menu.png" else "two"
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100},
                 "payload": f"https://m.restosuite.cn/open/app/{suffix}/table"}]

    service, _, _, input_root, _ = _setup(
        tmp_path, inspector=detector,
        ocr_reader=lambda _path: [{"text": "欢迎光临", "x": 40, "y": 130, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu.png")
    _png(input_root / "Shop" / "z-extra.png", "gray")
    batch = _analyze(service, input_root)
    # 两个候选时唯一候选规则不生效，域名线索又无 OCR 佐证，保持 unknown 进人工。
    assert batch["stores"][0]["recommendation"] is False
    detail = service.get_store(batch["batch_id"], batch["stores"][0]["store_id"])
    assert {c["purpose"] for image in detail["images"] for c in image["candidates"]} == {"unknown"}


def test_enterprise_wechat_payload_is_excluded_without_ocr_or_model(tmp_path: Path) -> None:
    def detector(path):
        if path.name != "menu.png":
            return []
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": "https://work.weixin.qq.com/secret-value"}]
    def unexpected_ocr(_path: Path):
        raise AssertionError("企业微信码可由本地载荷直接识别")
    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector, ocr_reader=unexpected_ocr)
    _png(input_root / "Shop" / "menu.png")
    _png(input_root / "Shop" / "z-extra.png", "gray")
    batch = _analyze(service, input_root)
    store = service.get_store(batch["batch_id"], batch["stores"][0]["store_id"])
    candidate = store["images"][0]["candidates"][0]
    assert candidate["purpose"] == "enterprise_wechat"
    assert candidate["purpose_source"] == "payload_domain"
    assert candidate["recommendation"] is False
    assert store["analysis_status"] == "needs_review"
    assert "secret-value" not in json.dumps(candidate)
    assert batch["stores"][0]["analysis_status"] == "needs_review"
    assert "仅识别到企业微信码" in batch["stores"][0]["reason"]
    assert service.accept_recommendations(batch["batch_id"])["confirmed"] == 0


def test_enterprise_and_ordering_codes_in_one_image_select_the_local_order_code(tmp_path: Path) -> None:
    def detector(path):
        if path.name != "menu.png":
            return []
        return [
            {"box": {"x": 20, "y": 20, "width": 90, "height": 90}, "payload": "https://work.weixin.qq.com/a"},
            {"box": {"x": 500, "y": 20, "width": 100, "height": 100}, "payload": "private-order"},
        ]
    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        ocr_reader=lambda _path: [{"text": "美团扫码点餐", "x": 510, "y": 130, "width": 120, "height": 20}],
    )
    _png(input_root / "Shop" / "menu.png", size=(800, 300))
    _png(input_root / "Shop" / "z-extra.png", "gray")
    batch = _analyze(service, input_root)
    candidates = service.get_store(batch["batch_id"], batch["stores"][0]["store_id"])["images"][0]["candidates"]
    assert [candidate["purpose"] for candidate in candidates] == ["enterprise_wechat", "ordering"]
    assert batch["stores"][0]["analysis_status"] == "selected"


def test_legacy_candidate_without_purpose_is_recommended_as_unique_code(tmp_path: Path) -> None:
    service, _, store, input_root, _ = _setup(tmp_path)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    sid = batch["stores"][0]["store_id"]
    with store.connect() as db:
        row = db.execute("SELECT store_json FROM material_decisions WHERE batch_id=? AND store_id=?", (batch["batch_id"], sid)).fetchone()
        legacy = json.loads(row[0])
        legacy["images"][0]["candidates"][0].pop("purpose", None)
        db.execute("UPDATE material_decisions SET store_json=? WHERE batch_id=? AND store_id=?", (json.dumps(legacy), batch["batch_id"], sid))
    # 缺用途的历史数据按“唯一可解码候选”规则推荐，仍由人工或微信侧最终验证。
    assert service.get_store(batch["batch_id"], sid)["recommendation"] is True
    assert service.accept_recommendations(batch["batch_id"])["confirmed"] == 1
    assert service.get_store(batch["batch_id"], sid)["decision"] == "confirmed"


def test_enterprise_wechat_candidate_is_not_recommended_and_never_bulk_confirmed(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        if path.name != "menu.png":
            return []
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": "https://work.weixin.qq.com/secret-value"}]

    def unexpected_ocr(_path: Path):
        raise AssertionError("企业微信码可由本地载荷直接识别")

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector, ocr_reader=unexpected_ocr)
    _png(input_root / "Shop" / "menu.png")
    _png(input_root / "Shop" / "z-extra.png", "gray")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is False
    assert store["analysis_status"] == "needs_review"
    assert "仅识别到企业微信码" in store["reason"]
    assert service.accept_recommendations(batch["batch_id"])["confirmed"] == 0
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["decision"] == "pending"


def test_batch_reanalysis_reruns_only_selected_pending_stores(tmp_path: Path) -> None:
    inspected: list[str] = []

    def detector(path: Path) -> list[dict]:
        inspected.append(path.name)
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": PAYLOAD}]

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector)
    _png(input_root / "A" / "a.png")
    _png(input_root / "B" / "b.png")
    _png(input_root / "B" / "z-b.png", "gray")
    batch = _analyze(service, input_root)
    ids = {store["source_name"]: store["store_id"] for store in batch["stores"]}
    assert service.accept_recommendations(batch["batch_id"], [ids["A"]])["confirmed"] == 1
    with pytest.raises(ValueError, match="未知门店"):
        service.start_reanalysis(batch["batch_id"], ["0" * 24])
    with pytest.raises(ValueError, match="没有待审核"):
        service.start_reanalysis(batch["batch_id"], [ids["A"]])
    inspected.clear()
    service.start_reanalysis(batch["batch_id"], [ids["A"], ids["B"]])
    result = _wait(service, batch["batch_id"])
    assert result["status"] == "completed"
    assert result["progress"]["done"] == result["progress"]["total"]
    assert inspected == ["b.png", "z-b.png"]
    assert service.get_store(batch["batch_id"], ids["A"])["decision"] == "confirmed"
    assert service.get_store(batch["batch_id"], ids["B"])["decision"] == "pending"


def test_batch_versions_keep_snapshots_and_reuse_unchanged_capture(tmp_path: Path) -> None:
    service, config, tasks, source, _ = _setup(tmp_path)
    _png(source / "Shop A" / "code.png")
    _png(source / "Shop B" / "code.png", color="blue")
    batch = _analyze(service, source)
    batch_id = batch["batch_id"]
    a, b = batch["stores"]
    service.decide_store(batch_id, a["store_id"], "confirm", image_index=0, candidate_index=0)
    first = service.publish_snapshot(batch_id, name="测试门店", version="v1.0.0", description="首发")
    first_task = next(row for row in tasks.tasks() if row["source_name"] == "Shop A")
    output = tmp_path / "menu"
    _png(output / "page-0001.png")
    tasks.update(first_task["id"], status="captured", stage="captured", output_dir=str(output))

    service.decide_store(batch_id, b["store_id"], "confirm", image_index=0, candidate_index=0)
    assert service.get_batch(batch_id)["draft_changes"] == {"added": 1, "removed": 0, "changed": 0}
    with pytest.raises(ValueError, match="新的版本号"):
        service.publish_snapshot(batch_id, name="测试门店", version="v1.0.0", description="首发")
    assert config.load().input_dir == first["input_dir"]
    second = service.publish_snapshot(batch_id, name="测试门店", version="v1.1.0", description="补充一家")
    assert second["release"]["store_count"] == 2
    assert not (Path(first["input_dir"]) / "Shop B").exists()
    inherited = next(row for row in tasks.tasks() if row["source_name"] == "Shop A"
                     and Path(second["input_dir"]) in Path(row["source_path"]).parents)
    assert inherited["status"] == "captured"
    assert inherited["inherited_from"] == first_task["id"]

    service.decide_store(batch_id, a["store_id"], "skip")
    assert service.get_batch(batch_id)["draft_changes"] == {"added": 0, "removed": 1, "changed": 0}
    third = service.publish_snapshot(batch_id, name="测试门店", version="v2.0.0", description="移除一家")
    assert third["release"]["store_count"] == 1
    assert (Path(second["input_dir"]) / "Shop A").exists()
    service.activate_version(batch_id, "v1.0.0")
    assert config.load().input_dir == first["input_dir"]
    assert service.active_release(Path(first["input_dir"]))["version"] == "v1.0.0"
    assert len(service.get_batch(batch_id)["releases"]) == 3
    assert service.get_batch(batch_id)["suggested_version"] == "v2.0.1"
    with pytest.raises(ValueError, match="v1.0.0"):
        service.publish_snapshot(batch_id, name="测试门店", version="1.0", description="")


def test_publish_during_run_keeps_active_version_and_run_binding(tmp_path: Path) -> None:
    service, config, tasks, source, _ = _setup(tmp_path)
    _png(source / "Shop A" / "code.png")
    batch_id = _analyze(service, source)["batch_id"]
    service.accept_recommendations(batch_id)
    first = service.publish_snapshot(batch_id, name="门店集", version="v1.0.0")
    binding = tasks.material_binding(config.load().input_dir)
    run = tasks.create_run("test-run", "automatic", [tasks.tasks()[0]["id"]], material=binding)
    assert run["material_version"] == "v1.0.0"
    # 同样的清单可以赋予新的版本号，当前运行仍绑定旧版本。
    second = service.publish_snapshot(batch_id, name="门店集", version="v1.0.1", description="第二版", activate=False)
    assert not second["release"]["active"]
    assert service.active_release(Path(config.load().input_dir))["version"] == "v1.0.0"
    assert tasks.get_run("test-run")["material_version_id"] == first["release"]["release_id"]
    service.activate_version(batch_id, "v1.0.1")
    assert tasks.material_binding(config.load().input_dir)["version"] == "v1.0.1"
    assert tasks.get_run("test-run")["material_version"] == "v1.0.0"


def test_rejection_reason_is_persistent_and_cleared_on_review(tmp_path: Path) -> None:
    service, _, tasks, source, _ = _setup(tmp_path)
    _png(source / "Shop A" / "code.png")
    _png(source / "Shop B" / "code.png")
    batch = _analyze(service, source)
    bid, sid = batch["batch_id"], batch["stores"][0]["store_id"]
    rejected = service.decide_store(bid, sid, "skip", reject_reason="blur", reject_detail="码被遮住")
    assert rejected["decision"] == "skipped"
    assert {key: rejected["rejection"][key] for key in ("code", "label", "detail")} == {"code": "blur", "label": "图片模糊 / 无法识别", "detail": "码被遮住"}
    assert rejected["rejection"]["marked_at"]
    assert service.get_batch(bid)["summary"]["skipped_stores"] == 1
    service.accept_recommendations(bid)
    assert service.get_store(bid, sid)["decision"] == "skipped"
    published = service.publish_snapshot(bid, name="测试批次", version="v1.0.0")
    assert published["release"]["store_count"] == 1
    with tasks.connect() as db:
        assert db.execute("SELECT reject_detail FROM material_decisions WHERE batch_id=? AND store_id=?", (bid, sid)).fetchone()[0] == "码被遮住"
    pending = service.decide_store(bid, sid, "pending")
    assert pending["decision"] == "pending"
    assert pending["rejection"] is None
    assert service.get_batch(bid)["summary"]["skipped_stores"] == 0
    with pytest.raises(ValueError, match="其他原因"):
        service.decide_store(bid, sid, "skip", reject_reason="other")
    with pytest.raises(ValueError, match="有效"):
        service.decide_store(bid, sid, "skip", reject_reason=[])
    service.decide_store(bid, sid, "skip", reject_reason="other", reject_detail="店已停业")
    confirmed = service.decide_store(bid, sid, "confirm", image_index=0, candidate_index=0)
    assert confirmed["rejection"] is None
    assert confirmed["decision"] == "confirmed"


def test_legacy_batch_only_completed_when_every_store_has_decision(tmp_path: Path) -> None:
    service, _, task_store, input_root, root = _setup(tmp_path)
    _png(input_root / "甲店" / "a.png")
    _png(input_root / "乙店" / "b.png")
    batch = service.create_local_batch(input_root)
    assert _analyze(service, input_root)["status"] == "completed"
    batch_id = batch["batch_id"]

    # 只有部分门店有决策时，崩溃遗留的 analyzing 必须回到可分析，不能标记完成。
    with task_store.connect() as db:
        db.execute("DELETE FROM material_decisions WHERE batch_id=? AND source_name='乙店'", (batch_id,))
        db.execute("UPDATE material_batches SET status='analyzing' WHERE id=?", (batch_id,))
    recovered = MaterialService(root, service.configs, task_store, inspector=_detector)
    assert recovered.get_batch(batch_id)["status"] == "ready_to_analyze"

    # 全部门店都有决策时，才收敛为 completed。
    recovered.start_analysis(batch_id)
    assert _wait(recovered, batch_id)["status"] == "completed"
    with task_store.connect() as db:
        db.execute("UPDATE material_batches SET status='analyzing' WHERE id=?", (batch_id,))
    again = MaterialService(root, service.configs, task_store, inspector=_detector)
    assert again.get_batch(batch_id)["status"] == "completed"


def test_interrupted_job_blocks_new_job_until_cancelled(tmp_path: Path) -> None:
    from menu_capture.materials.material_jobs import JobStore

    service, _, _, _input_root, _root = _setup(tmp_path)
    store = JobStore(service.task_store)
    items = [{"store_id": "s1", "source_folder": "a"}]
    job = store.create("batch-x", "analyze", items)
    store.finish(job["id"], "interrupted", error="心跳超时")
    with pytest.raises(ValueError, match="已中断"):
        store.create("batch-x", "analyze", items)
    assert store.cancel(job["id"])
    assert store.create("batch-x", "retry_failed", items)["status"] == "queued"


def test_batch_and_per_store_decisions_are_identical(tmp_path: Path) -> None:
    """同一批样本：整批 build_materials 与逐店 job 产出的决策 JSON 必须一致。"""
    from menu_capture.materials.preprocess import build_materials, discover_stores

    service, _, _, input_root, _root = _setup(tmp_path)
    _png(input_root / "品牌A" / "门店" / "a.png")
    _png(input_root / "品牌A" / "门店" / "z-a.png", "gray")
    _png(input_root / "品牌B" / "门店" / "b.png", "black")
    _png(input_root / "品牌B" / "门店" / "z-b.png", "white")
    _png(input_root / "单独店" / "only.png")

    per_store_batch = _analyze(service, input_root)
    assert per_store_batch["status"] == "completed"
    with service.task_store.connect() as db:
        per_store = {
            row["source_folder"]: json.loads(row["store_json"])
            for row in db.execute("SELECT source_folder,store_json FROM material_decisions WHERE batch_id=?",
                                  (per_store_batch["batch_id"],))
        }

    output = tmp_path / "batch-analysis"
    manifest = build_materials(input_root, output, inspector=_detector)
    _stores, duplicates = discover_stores(input_root)
    service._classify_and_select(manifest, input_root, output, duplicate_names=duplicates)
    wholesale = {store["source_folder"]: store for store in manifest["stores"]}

    assert set(per_store) == set(wholesale)

    def decision(store: dict) -> dict:
        return {
            "status": store.get("status") or store.get("analysis_status"),
            "reason": store.get("reason", ""),
            "selected_source": store.get("selected_source"),
            "selected_artifact": store.get("selected_artifact"),
            "selected": [candidate.get("purpose") for image in store["images"]
                         for candidate in image.get("candidates", []) if candidate.get("selected")],
        }

    for folder in sorted(per_store):
        assert decision(per_store[folder]) == decision(wholesale[folder]), folder


def test_reanalysis_scope_is_pending_stores_only(tmp_path: Path) -> None:
    """重新分析范围只包含待审核门店；已确认、已判不合格的门店不参与。"""
    service, _, _, input_root, _ = _setup(tmp_path)
    _png(input_root / "Shop A" / "menu.png")
    _png(input_root / "Shop B" / "menu.png")
    batch = _analyze(service, input_root)
    batch_id = batch["batch_id"]
    stores = {store["source_name"]: store for store in batch["stores"]}
    shop_a, shop_b = stores["Shop A"], stores["Shop B"]
    service.decide_store(batch_id, shop_a["store_id"], "confirm", image_index=0,
                         box={"x": 40, "y": 30, "width": 90, "height": 90})
    service.decide_store(batch_id, shop_b["store_id"], "skip",
                         reject_reason="other", reject_detail="测试用不合格")
    with pytest.raises(ValueError, match="没有待审核"):
        service.start_reanalysis(batch_id, [shop_a["store_id"], shop_b["store_id"]])


def test_explicit_url_ordering_is_high_confidence_without_ocr(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100},
                 "payload": "https://rms.meituan.com/diancan/14/abc"}]

    def unexpected_ocr(_path: Path):
        raise AssertionError("URL 明确点餐时不应依赖 OCR")

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector, ocr_reader=unexpected_ocr)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is True
    assert store["recommendation_level"] == "high"
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["wechat_validation_status"] == "unverified"
    assert detail["images"][0]["candidates"][0]["purpose"] == "ordering"


def test_unique_unknown_candidate_is_low_confidence_recommendation(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(
        tmp_path,
        ocr_reader=lambda _path: [{"text": "欢迎光临", "x": 40, "y": 130, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is True
    assert store["recommendation_level"] == "low"
    detail = service.get_store(batch["batch_id"], store["store_id"])
    candidate = detail["images"][0]["candidates"][0]
    assert candidate["purpose"] == "unknown"
    assert "低可信" in store["reason"]
    assert detail["wechat_validation_status"] == "unverified"


def test_full_image_ordering_text_only_hints_missed_code_and_blocks_low_confidence(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": None}]

    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        # 文字离二维码超过 3 倍码宽，不能归属给该码，只能作为全图漏检提示。
        ocr_reader=lambda _path: [{"text": "扫码点餐", "x": 600, "y": 250, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu.png", size=(800, 300))
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is False
    assert "原图 OCR 发现点餐标识" in store["reason"]
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["images"][0]["possible_missed_code"] is True
    assert detail["images"][0]["candidates"][0]["purpose"] == "unknown"


def test_ordering_and_non_ordering_codes_in_one_image_recommend_ordering(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
        return [
            {"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": "private-order"},
            {"box": {"x": 500, "y": 20, "width": 100, "height": 100}, "payload": "https://www.wjx.cn/vm/survey.aspx"},
        ]

    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        ocr_reader=lambda _path: [{"text": "扫码点餐", "x": 40, "y": 130, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu.png", size=(800, 300))
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is True
    assert store["recommendation_level"] == "ocr"
    detail = service.get_store(batch["batch_id"], store["store_id"])
    candidates = detail["images"][0]["candidates"]
    assert [candidate["purpose"] for candidate in candidates] == ["ordering", "not_ordering"]
    assert candidates[0]["recommendation"] is True
    assert candidates[1]["recommendation"] is False


def test_image_error_with_unique_ordering_candidate_recommends_and_records_abnormality(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        if path.name == "broken.png":
            raise RuntimeError("read failed")
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": "private-order"}]

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector)
    _png(input_root / "Shop" / "broken.png")
    _png(input_root / "Shop" / "good.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is True
    assert store["recommendation_level"] == "ocr"
    assert "异常" in store["reason"]
    detail = service.get_store(batch["batch_id"], store["store_id"])
    broken = next(image for image in detail["images"] if image["path"].endswith("broken.png"))
    assert "error" in broken


def test_image_error_with_only_unknown_candidate_goes_to_review(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        if path.name == "broken.png":
            raise RuntimeError("read failed")
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": PAYLOAD}]

    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        ocr_reader=lambda _path: [{"text": "欢迎光临", "x": 40, "y": 130, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "broken.png")
    _png(input_root / "Shop" / "unknown.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is False
    assert store["recommendation_level"] == "none"
    assert "检查失败" in store["reason"]


def test_image_error_with_multiple_ordering_candidates_goes_to_review(tmp_path: Path) -> None:
    def detector(path: Path) -> list[dict]:
        if path.name == "broken.png":
            raise RuntimeError("read failed")
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": path.stem}]

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector)
    _png(input_root / "Shop" / "broken.png")
    _png(input_root / "Shop" / "one.png")
    _png(input_root / "Shop" / "two.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is False
    assert "多个明确点餐候选" in store["reason"]


def test_conflicting_url_evidence_goes_to_manual_review(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100},
                 "payload": "https://www.wjx.cn/vm/diancai.aspx"}]

    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=detector,
        ocr_reader=lambda _path: [{"text": "欢迎光临", "x": 40, "y": 130, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is False
    assert "冲突" in store["reason"]
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["images"][0]["candidates"][0]["purpose"] == "conflict"


def test_structurally_invalid_candidate_goes_to_manual_review(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100},
                 "payload": None, "qr_likeness": False}]

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    # 结构校验不通过的框不能靠周边 OCR 升级为点餐候选。
    assert store["recommendation"] is False
    assert "结构/包含度校验" in store["reason"]
    detail = service.get_store(batch["batch_id"], store["store_id"])
    candidate = detail["images"][0]["candidates"][0]
    assert candidate["quality"] == "rejected"
    assert candidate["purpose"] == "unknown"
    assert candidate["purpose_reason"] == "候选框未通过二维码结构校验"


def test_rejected_candidate_is_not_counted_as_multi_code_image(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
        return [
            {"box": {"x": 20, "y": 20, "width": 120, "height": 120},
             "payload": "https://rms.meituan.com/diancan/shop"},
            {"box": {"x": 300, "y": 20, "width": 120, "height": 120},
             "payload": None, "qr_likeness": False},
        ]

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector)
    _png(input_root / "Shop" / "menu.png", size=(500, 300))
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    detail = service.get_store(batch["batch_id"], store["store_id"])
    # 纹理误检框被质量门禁拒绝后，不再把单码图误判成多码图。
    assert not detail["images"][0].get("is_multi_qr_image")
    assert store["recommendation"] is True


def test_qr_fill_containment_and_offset_rejections_keep_reason(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
        return [
            {"box": {"x": 20, "y": 20, "width": 120, "height": 120},
             "payload": None, "qr_fill": 0.05},
            {"box": {"x": 300, "y": 20, "width": 120, "height": 120},
             "payload": None, "qr_contained": False},
            {"box": {"x": 20, "y": 160, "width": 120, "height": 120},
             "payload": None, "qr_fill": 0.4, "qr_centered": False},
        ]

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector)
    _png(input_root / "Shop" / "menu.png", size=(500, 300))
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is False
    assert "结构/包含度校验" in store["reason"]
    detail = service.get_store(batch["batch_id"], store["store_id"])
    reasons = {candidate["purpose_reason"] for candidate in detail["images"][0]["candidates"]}
    assert reasons == {
        "二维码面积占候选框过低", "二维码超出候选框", "二维码明显偏离候选框中心",
    }


def test_ordering_ocr_without_candidate_forces_rescan(tmp_path: Path) -> None:
    calls = {"count": 0}

    def detector(_path: Path) -> list[dict]:
        calls["count"] += 1
        if calls["count"] == 1:
            return []
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100},
                 "payload": "https://rms.meituan.com/diancan/shop"}]

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    # OCR 看到点餐文字但首轮 0 候选时，必须复扫一次而不是直接判定无码。
    assert calls["count"] >= 2
    assert store["recommendation"] is True


def test_manifest_sanitize_keeps_rejected_quality_fields() -> None:
    manifest = {
        "input_root": "/tmp/inbox",
        "stores": [{
            "source_name": "Shop", "source_folder": "Shop", "status": "needs_review",
            "images": [{
                "path": "menu.png", "sha256": "a" * 64,
                "candidates": [{
                    "box": {"x": 20, "y": 20, "width": 120, "height": 120},
                    "payload_sha256": None, "short_edge_px": 120,
                    "purpose": "unknown", "purpose_source": "local_ocr",
                    "purpose_reason": "二维码面积占候选框过低",
                    "quality": "rejected", "quality_reason": "二维码面积占候选框过低",
                    "qr_likeness": False, "qr_contained": True, "qr_fill": 0.05,
                }],
            }],
        }],
    }
    safe = MaterialService._sanitize_manifest(manifest)
    candidate = safe["stores"][0]["images"][0]["candidates"][0]
    assert candidate["quality"] == "rejected"
    assert candidate["quality_reason"] == "二维码面积占候选框过低"
    assert candidate["qr_likeness"] is False
    assert candidate["qr_contained"] is True
    assert candidate["qr_fill"] == 0.05


def test_mkdir_ok_does_not_stat_existing_dir(tmp_path, monkeypatch):
    """Windows 上 stat 已存在目录偶发 WinError 5，建目录不能依赖 exist_ok 的 stat 回退。"""
    target = tmp_path / "existing-store"
    target.mkdir()
    original = Path.stat

    def deny(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "拒绝访问")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny)
    _mkdir_ok(target)


def test_subset_publish_keeps_unselected_stores_and_reuses_unchanged_version(tmp_path: Path) -> None:
    """跨批次统一发布：只更新勾选门店，其余沿用上一版；内容未变时复用原版本。"""
    service, _, tasks, source, _ = _setup(tmp_path)
    _png(source / "Shop A" / "code.png")
    _png(source / "Shop B" / "code.png", color="blue")
    batch = _analyze(service, source)
    batch_id = batch["batch_id"]
    a, b = batch["stores"]
    service.decide_store(batch_id, a["store_id"], "confirm", image_index=0, candidate_index=0)
    first = service.publish_snapshot(batch_id, name="门店集", version="v1.0.0", description="首发")
    assert first["release"]["store_count"] == 1

    # B 还没选码：待处理会出现在返工表里，不能发布。
    rows = service.rework_records([batch_id])
    assert any(row["store_id"] == b["store_id"] and row["category"] == "pending" for row in rows)

    service.decide_store(batch_id, b["store_id"], "confirm", image_index=0, candidate_index=0)
    assert any(row["store_id"] == b["store_id"] and row["category"] == "unpublished"
               for row in service.rework_records([batch_id]))
    second = service.publish_snapshot(batch_id, description="补一家", store_ids=[b["store_id"]], activate=False)
    assert second["release"]["store_count"] == 2
    assert second["release"]["version"] == "v1.0.1"
    # 未勾选的 A 沿用上一版快照文件与版本，不重新裁剪。
    assert (Path(second["input_dir"]) / "Shop A" / "二维码.png").is_file()
    again = service.publish_snapshot(batch_id, description="补一家", store_ids=[b["store_id"]], activate=False)
    assert again["snapshot_id"] == second["snapshot_id"]
    assert again["release"]["version"] == "v1.0.1"
    assert again["already_published"]
    # 同一门店在当前快照里只有一个有效任务，重复发布不产生新任务。
    effective = [row for row in tasks.tasks() if Path(second["input_dir"]) in Path(row["source_path"]).parents]
    assert len(effective) == 2
    # 已经与快照一致的门店不再出现在「已选码待发布」。
    assert not any(row["store_id"] == b["store_id"] and row["category"] == "unpublished"
                   for row in service.rework_records([batch_id]))


def test_effective_capture_tasks_keep_unchanged_store_after_snapshot_update(tmp_path: Path) -> None:
    service, _, tasks, source, _ = _setup(tmp_path)
    _png(source / "Shop A" / "code.png")
    _png(source / "Shop B" / "code.png", color="blue")
    batch = _analyze(service, source)
    batch_id = batch["batch_id"]
    a, b = batch["stores"]
    service.decide_store(batch_id, a["store_id"], "confirm", image_index=0, candidate_index=0)
    first = service.publish_snapshot(batch_id, description="首发", activate=False)
    service.decide_store(batch_id, b["store_id"], "confirm", image_index=0, candidate_index=0)
    second = service.publish_snapshot(batch_id, description="补一家", store_ids=[b["store_id"]], activate=False)

    rows = service.effective_capture_tasks()
    assert {row["source_name"] for row in rows} == {"Shop A", "Shop B"}
    assert {row["version"] for row in rows} == {"v1"}
    assert {row["batch_version"] for row in rows} == {second["release"]["version"]}
    assert {row["batch_id"] for row in rows} == {batch_id}
    old = {row["id"] for row in tasks.tasks() if Path(first["input_dir"]) in Path(row["source_path"]).parents}
    assert old == {row["id"] for row in rows if row["source_name"] == "Shop A"}


def test_capture_round_and_new_version_clear_approval(tmp_path: Path) -> None:
    """已通过的批次换版或重采后必须回到待审核。"""
    service, _, tasks, source, _ = _setup(tmp_path)
    _png(source / "Shop A" / "code.png")
    _png(source / "Shop B" / "code.png", color="blue")
    batch = _analyze(service, source)
    batch_id = batch["batch_id"]
    a, b = batch["stores"]
    service.decide_store(batch_id, a["store_id"], "confirm", image_index=0, candidate_index=0)
    first = service.publish_snapshot(batch_id, description="首发", activate=False)
    service.set_approved(batch_id, True)
    assert service.get_batch(batch_id)["workflow"]["stage"] == "approved"

    task = next(row for row in tasks.tasks() if Path(first["input_dir"]) in Path(row["source_path"]).parents)
    assert service.register_capture_round([task["id"]]) == [batch_id]
    assert service.get_batch(batch_id)["workflow"]["stage"] != "approved"

    service.set_approved(batch_id, True)
    service.decide_store(batch_id, b["store_id"], "confirm", image_index=0, candidate_index=0)
    service.publish_snapshot(batch_id, description="补一家", store_ids=[b["store_id"]], activate=False)
    assert service.get_batch(batch_id)["workflow"]["stage"] != "approved"


def test_rejection_review_mark_is_persistent_and_resets_with_new_reason(tmp_path: Path) -> None:
    """不合格门店的人工确认标记：可反复切换，重新判原因时自动清空。"""
    service, _, _, source, _ = _setup(tmp_path)
    _png(source / "Shop A" / "code.png")
    batch = _analyze(service, source)
    batch_id, store_id = batch["batch_id"], batch["stores"][0]["store_id"]
    service.decide_store(batch_id, store_id, "skip", reject_reason="not_ordering", reject_detail="门贴码")
    assert service.mark_rejection_reviewed(batch_id, store_id, True)["rejection"]["reviewed_at"]
    assert service.mark_rejection_reviewed(batch_id, store_id, False)["rejection"]["reviewed_at"] == ""
    service.mark_rejection_reviewed(batch_id, store_id, True)
    service.decide_store(batch_id, store_id, "skip", reject_reason="store_closed")
    assert service.get_store(batch_id, store_id)["rejection"]["reviewed_at"] == ""
    service.decide_store(batch_id, store_id, "confirm", image_index=0, candidate_index=0)
    with pytest.raises(ValueError, match="没有不合格"):
        service.mark_rejection_reviewed(batch_id, store_id, True)


def test_manual_action_accepts_only_first_decision_per_store() -> None:
    import threading

    from menu_capture.capture.uipath_runner import UiPathRunner

    runner = object.__new__(UiPathRunner)
    runner.run_id = "run-1"
    runner._manual_cond = threading.Condition()
    runner._manual_state = {"task_id": "t1", "store_name": "Shop A", "index": 1, "total": 2,
                            "stage": "waiting_human", "waiting": True, "decision": ""}
    runner._manual_action = None
    runner._manual_reason = "not_ordering"
    runner._manual_last_result = ""
    assert runner.manual_action("capture")["action"] == "capture"
    with pytest.raises(ValueError, match="本店已处理"):
        runner.manual_action("skip")
    # 换店后动作通道重置，下一家仍然只接受第一次决定。
    runner._set_manual_state({"id": "t2", "source_name": "Shop B"}, 1, 2, "waiting_human", waiting=True)
    assert runner._manual_action is None
    with pytest.raises(ValueError, match="有效"):
        runner.manual_action("reject", "not-a-reason")
    assert runner.manual_action("reject", "not_ordering")["action"] == "reject"


def test_start_analysis_is_blocked_when_detector_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    """检测器不可用时必须阻止任务创建，批次保持 ready_to_analyze，不能静默 completed。"""
    service, _, _, input_root, _ = _setup(tmp_path)
    service.inspector = None
    _png(input_root / "Shop" / "menu.png")
    batch = service.create_local_batch(input_root)
    monkeypatch.setattr(
        "menu_capture.materials.materials.probe_detector_capability",
        lambda: {"backend": "opencv_zxing", "available": False, "detail": "OpenCV 缺失"},
    )
    with pytest.raises(ValueError, match="检测器不可用"):
        service.start_analysis(batch["batch_id"])
    current = service.get_batch(batch["batch_id"])
    assert current["status"] == "ready_to_analyze"
    assert current["job"] is None


def test_detection_stats_persist_scanned_state_and_not_detected(tmp_path: Path) -> None:
    """扫描标记与三类状态必须穿过清单清洗持久化，不能回退成 scanned_stores=0。"""
    service, _, _, input_root, _ = _setup(tmp_path, inspector=lambda _path: [])
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)

    stats = batch["summary"]["detection_stats"]["square"]
    assert stats["scanned_stores"] == 1
    assert stats["not_detected_stores"] == 1
    assert stats["image_error_stores"] == 0
    assert stats["detected_stores"] == 0
    assert batch["summary"]["detector_capability"]["backend"] == "injected"


def test_detection_stats_report_single_image_error(tmp_path: Path) -> None:
    def broken(_path):
        raise RuntimeError("boom")

    service, _, _, input_root, _ = _setup(tmp_path, inspector=broken)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)

    stats = batch["summary"]["detection_stats"]["square"]
    assert stats["image_error_stores"] == 1
    assert stats["not_detected_stores"] == 0


def test_job_fails_instead_of_completing_when_detector_breaks_mid_run(tmp_path: Path, monkeypatch) -> None:
    """预检通过后检测器中途失效，任务/批次必须显性失败，禁止以 completed 收尾。"""
    from menu_capture.materials import material_jobs
    from menu_capture.vision.qr_detection import DetectorUnavailable

    service, _, _, input_root, _ = _setup(tmp_path)
    service.inspector = None
    _png(input_root / "Shop" / "menu.png")
    monkeypatch.setattr(
        "menu_capture.materials.materials.probe_detector_capability",
        lambda: {"backend": "opencv_zxing", "available": True, "detail": "OpenCV 可用"},
    )

    def broken(_path):
        raise DetectorUnavailable("OpenCV 初始化失败")

    monkeypatch.setattr("menu_capture.materials.materials.detect_qr", broken)
    monkeypatch.setattr(material_jobs, "RETRY_DELAYS", (0, 0, 0))

    batch = service.create_local_batch(input_root)
    service.start_analysis(batch["batch_id"])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = service.get_batch(batch["batch_id"])
        if current["job"] is not None and current["job"]["status"] == "failed":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("检测器失效后任务未在 20 秒内失败")
    assert current["status"] != "completed"
    assert current["status"] == "ready_to_analyze"
