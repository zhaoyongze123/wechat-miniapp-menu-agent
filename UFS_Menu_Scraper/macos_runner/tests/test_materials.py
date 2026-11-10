from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from menu_capture.config import ConfigStore
from menu_capture.materials import MaterialService
from menu_capture.store import TaskStore


PAYLOAD = "https://private.example/material-secret"


def _png(path: Path, color: str = "white", size=(240, 160)) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path.read_bytes()


def _bytes_png(color: str = "white", size=(240, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _setup(tmp_path: Path, inspector=None, ocr_reader=None, circle_inspector=None, circle_mode=None):
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
        circle_inspector=circle_inspector,
        circle_mode=circle_mode,
    )
    return service, config, store, input_root, root


def _detector(_path: Path) -> list[dict]:
    return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": PAYLOAD}]


def _wait(service: MaterialService, batch_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        batch = service.get_batch(batch_id)
        if batch["status"] not in {"queued", "analyzing"}:
            return batch
        time.sleep(0.01)
    raise AssertionError("素材分析 worker 在 5 秒内未结束")


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
    service, _, _, input_root, _ = _setup(
        tmp_path,
        ocr_reader=lambda _path: [{"text": "关注公众号", "x": 40, "y": 130, "width": 100, "height": 20}],
    )
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

    monkeypatch.setattr("menu_capture.materials.MAX_FILES", 0)
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
    # 门店只有 1 张素材：未定位到二维码也按整图推荐。
    assert store["analysis_status"] == "selected"
    assert store["recommendation"] is True
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["images"][0]["candidates"][0]["box"] == {"x": 0.0, "y": 0.0, "width": 240.0, "height": 160.0}
    assert detail["images"][0]["candidates"][0]["purpose_source"] == "single_image"
    confirmed = service.decide_store(
        batch["batch_id"], store["store_id"], "confirm", image_index=0,
        box={"x": 40, "y": 30, "width": 90, "height": 90},
    )
    assert confirmed["decision"] == "confirmed"
    assert confirmed["selected"] == {
        "image_index": 0, "candidate_index": None,
        "box": {"x": 40.0, "y": 30.0, "width": 90.0, "height": 90.0},
    }
    assert service.publish_snapshot(batch["batch_id"])["task_scan"]["added"] == 1


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
                candidate.pop("purpose", None)
                candidate.pop("purpose_reason", None)
    manifest_path.write_text(json.dumps(old_manifest, ensure_ascii=False), encoding="utf-8")
    adopted = service.adopt_manifest(manifest_path)
    adopted_store = adopted["stores"][0]
    assert adopted["status"] == "completed"
    assert adopted_store["recommendation"] is True
    assert adopted_store["decision"] == "pending"
    assert adopted["summary"]["confirmed_stores"] == 0
    adopted_detail = service.get_store(adopted["batch_id"], adopted_store["store_id"])
    assert adopted_detail["images"][0]["candidates"][0]["purpose"] == "unknown"


def test_confirmed_decision_prevents_publish_after_source_root_path_escape(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(tmp_path)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    service.decide_store(batch["batch_id"], store["store_id"], "confirm", 0, 0)
    # Store detail paths are relative and traversal cannot be introduced by the caller's id/index.
    with pytest.raises(ValueError, match="素材店铺不存在"):
        service.get_store(batch["batch_id"], "../../outside")


def test_interrupted_analysis_is_marked_and_never_automatically_restarted(tmp_path: Path) -> None:
    service, _, task_store, input_root, root = _setup(tmp_path)
    _png(input_root / "Shop" / "menu.png")
    batch = service.create_local_batch(input_root)
    with task_store.connect() as db:
        db.execute("UPDATE material_batches SET status='analyzing' WHERE id=?", (batch["batch_id"],))

    restarted = MaterialService(root, service.configs, task_store, inspector=_detector)
    status = restarted.get_batch(batch["batch_id"])
    assert status["status"] == "interrupted"
    assert status["stores"] == []
    with pytest.raises(ValueError, match="只有 ready_to_analyze"):
        restarted.start_analysis(batch["batch_id"])


def test_only_one_analysis_worker_runs_and_decisions_are_locked_while_analyzing(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def slow_inspector(path: Path) -> list[dict]:
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=3)
        return _detector(path)

    service, _, _, input_root, _ = _setup(tmp_path, inspector=slow_inspector)
    _png(input_root / "First Shop" / "one.png")
    _png(input_root / "Second Shop" / "two.png", "gray")
    first = service.create_local_batch(input_root / "First Shop")
    second = service.create_local_batch(input_root / "Second Shop")
    service.start_analysis(first["batch_id"])
    assert entered.wait(timeout=3)
    with pytest.raises(RuntimeError, match="已有素材批次正在分析"):
        service.start_analysis(second["batch_id"])
    with pytest.raises(ValueError, match="分析未完成"):
        service.decide_store(first["batch_id"], "unknown", "skip")
    release.set()
    assert _wait(service, first["batch_id"])["status"] == "completed"
    service._worker.join(timeout=3)
    assert _wait_after_start(service, second["batch_id"])["status"] == "completed"


def test_local_ocr_selects_point_ordering_code_and_keeps_safe_fields(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
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


def test_multiple_local_ordering_codes_choose_the_clearest_even_with_different_payloads(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
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
    assert store["recommendation"] is True
    assert store["analysis_status"] == "selected"
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert [c["selected"] for c in detail["images"][0]["candidates"]] == [False, True]


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


def test_unreadable_code_with_point_ordering_text_is_recommended_for_bulk_accept(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=lambda _path: [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": None}],
    )
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    candidate = batch["stores"][0]
    assert candidate["recommendation"] is True
    assert candidate["analysis_status"] == "selected"
    assert "未能由本地解码器读出" in candidate["reason"]
    assert service.accept_recommendations(batch["batch_id"])["confirmed"] == 1


def test_single_image_without_located_qr_recommends_the_whole_image(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(
        tmp_path,
        inspector=lambda _path: [],
        ocr_reader=lambda _path: [{"text": "扫码点餐", "x": 30, "y": 50, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu.png", size=(240, 160))
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is True
    assert store["analysis_status"] == "selected"
    assert "仅有 1 张素材" in store["reason"]
    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["images"][0]["ordering_text_seen"] is True
    assert detail["images"][0]["candidates"][0]["source"] == "single_image"
    assert service.accept_recommendations(batch["batch_id"])["confirmed"] == 1


def test_multi_image_unknown_purpose_candidate_is_recommended(tmp_path: Path) -> None:
    service, _, _, input_root, _ = _setup(
        tmp_path,
        ocr_reader=lambda _path: [{"text": "欢迎光临", "x": 40, "y": 130, "width": 100, "height": 20}],
    )
    _png(input_root / "Shop" / "menu-a.png")
    _png(input_root / "Shop" / "menu-b.png", color="blue")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["recommendation"] is True
    assert store["analysis_status"] == "selected"
    assert service.accept_recommendations(batch["batch_id"])["confirmed"] == 1


def test_enterprise_wechat_payload_is_excluded_without_ocr_or_model(tmp_path: Path) -> None:
    def detector(_path):
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": "https://work.weixin.qq.com/secret-value"}]
    def unexpected_ocr(_path: Path):
        raise AssertionError("企业微信码可由本地载荷直接识别")
    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector, ocr_reader=unexpected_ocr)
    _png(input_root / "Shop" / "menu.png")
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
    def detector(_path):
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
    batch = _analyze(service, input_root)
    candidates = service.get_store(batch["batch_id"], batch["stores"][0]["store_id"])["images"][0]["candidates"]
    assert [candidate["purpose"] for candidate in candidates] == ["enterprise_wechat", "ordering"]
    assert batch["stores"][0]["analysis_status"] == "selected"


def test_legacy_candidate_without_purpose_is_still_bulk_accepted(tmp_path: Path) -> None:
    service, _, store, input_root, _ = _setup(tmp_path)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    sid = batch["stores"][0]["store_id"]
    with store.connect() as db:
        row = db.execute("SELECT store_json FROM material_decisions WHERE batch_id=? AND store_id=?", (batch["batch_id"], sid)).fetchone()
        legacy = json.loads(row[0])
        legacy["images"][0]["candidates"][0].pop("purpose", None)
        db.execute("UPDATE material_decisions SET store_json=? WHERE batch_id=? AND store_id=?", (json.dumps(legacy), batch["batch_id"], sid))
    assert service.get_store(batch["batch_id"], sid)["recommendation"] is True
    assert service.accept_recommendations(batch["batch_id"])["confirmed"] == 1
    assert service.get_store(batch["batch_id"], sid)["decision"] == "confirmed"


def test_enterprise_wechat_candidate_is_not_recommended_and_never_bulk_confirmed(tmp_path: Path) -> None:
    def detector(_path: Path) -> list[dict]:
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": "https://work.weixin.qq.com/secret-value"}]

    def unexpected_ocr(_path: Path):
        raise AssertionError("企业微信码可由本地载荷直接识别")

    service, _, _, input_root, _ = _setup(tmp_path, inspector=detector, ocr_reader=unexpected_ocr)
    _png(input_root / "Shop" / "menu.png")
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
    batch = _analyze(service, input_root)
    service._worker.join(timeout=3)
    ids = {store["source_name"]: store["store_id"] for store in batch["stores"]}
    assert service.accept_recommendations(batch["batch_id"], [ids["A"]])["confirmed"] == 1
    with pytest.raises(ValueError, match="未知门店"):
        service.start_reanalysis(batch["batch_id"], ["0" * 24])
    with pytest.raises(ValueError, match="都已确认"):
        service.start_reanalysis(batch["batch_id"], [ids["A"]])
    inspected.clear()
    service.start_reanalysis(batch["batch_id"], [ids["A"], ids["B"]])
    result = _wait(service, batch["batch_id"])
    assert result["status"] == "completed"
    assert result["progress"]["done"] == result["progress"]["total"]
    assert inspected == ["b.png"]
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

CIRCLE_BOX = {"x": 30, "y": 20, "width": 100, "height": 100}


def test_circle_mini_program_code_without_payload_is_recommended_confirmed_and_published(tmp_path: Path) -> None:
    square_calls: list[Path] = []

    def square(path: Path) -> list[dict]:
        square_calls.append(path)
        return []

    service, config, task_store, input_root, _ = _setup(tmp_path, inspector=square, circle_inspector=lambda _path: {"box": CIRCLE_BOX, "detector_confidence": 0.99})
    _png(input_root / "Circle Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]

    # 两轮 + 分类共 3 个逻辑单位；圆形命中店不触发方形检测。
    assert batch["progress"] == {"done": 3, "total": 3}
    assert square_calls == []
    assert batch["summary"]["detection_stats"]["circle_enabled"] is True
    assert batch["summary"]["detection_stats"]["circle"]["hit_stores"] == 1
    assert batch["summary"]["detection_stats"]["square"]["scanned_stores"] == 0
    assert batch["summary"]["detection_rounds"] == 2

    detail = service.get_store(batch["batch_id"], store["store_id"])
    assert detail["decision"] == "pending"
    assert detail["recommendation"] is True
    candidate = detail["images"][0]["candidates"][0]
    assert candidate["payload_sha256"] is None
    assert candidate["code_type"] == "mini_program_code"
    assert candidate["source"] == "mini_program_code"
    assert candidate["detector"] == "weapp-vite-qr"
    assert candidate["detector_confidence"] == 0.99
    assert candidate["purpose"] == "ordering"
    assert candidate["purpose_source"] == "mini_program_code_priority"
    assert "待微信实际验证" in candidate["purpose_reason"]
    assert detail["images"][0]["scan_status"] == "circle_hit"

    candidate_preview = service.preview_candidate(batch["batch_id"], store["store_id"], 0, 0)
    assert candidate_preview["media_type"] == "image/png"

    accepted = service.accept_recommendations(batch["batch_id"])
    assert accepted["confirmed"] == 1
    result = service.publish_snapshot(batch["batch_id"])
    assert result["task_scan"]["added"] == 1
    assert (Path(result["input_dir"]) / "Circle Shop" / "二维码.png").is_file()


def test_circle_error_keeps_square_candidate_in_review(tmp_path: Path) -> None:
    def broken(_path: Path):
        raise RuntimeError(PAYLOAD)

    service, _, task_store, input_root, _ = _setup(tmp_path, circle_inspector=broken)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert store["analysis_status"] == "needs_review"
    assert store["recommendation"] is False

    detail = service.get_store(batch["batch_id"], store["store_id"])
    image = detail["images"][0]
    assert image["circle_error"] == "circle_detector_error"
    assert image["candidates"], "方形候选应保留用于人工参考"
    assert image["candidates"][0]["recommendation"] is False
    assert "检测异常" in detail["reason"]
    assert batch["summary"]["detection_stats"]["circle"]["error_stores"] == 1

    with task_store.connect() as db:
        saved = json.loads(db.execute("SELECT store_json FROM material_decisions WHERE batch_id=? AND store_id=?", (batch["batch_id"], store["store_id"])).fetchone()[0])
    assert saved["circle_error"] is True
    assert PAYLOAD not in json.dumps(saved, ensure_ascii=False)


def test_reanalysis_reruns_circle_round_and_confirmed_store_is_protected(tmp_path: Path) -> None:
    circle_calls: list[Path] = []

    def circle(path: Path) -> dict | None:
        circle_calls.append(path)
        return None

    service, _, _, input_root, _ = _setup(tmp_path, circle_inspector=circle)
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert len(circle_calls) == 1

    detail = service.reanalyze_store(batch["batch_id"], store["store_id"])
    assert len(circle_calls) == 2
    assert detail["recommendation"] is True
    assert detail["images"][0]["candidates"][0]["payload_sha256"]

    service.decide_store(batch["batch_id"], store["store_id"], "confirm", 0, 0)
    with pytest.raises(ValueError, match="已确认"):
        service.reanalyze_store(batch["batch_id"], store["store_id"])

def test_circle_assist_mode_still_runs_square_round_and_prefers_decodable_code(tmp_path: Path) -> None:
    square_calls: list[Path] = []

    def square(path: Path) -> list[dict]:
        square_calls.append(path)
        return [{"box": {"x": 30, "y": 20, "width": 100, "height": 100}, "payload": PAYLOAD}]

    service, _, _, input_root, _ = _setup(
        tmp_path, inspector=square, circle_inspector=lambda _path: {"box": CIRCLE_BOX, "detector_confidence": 0.99},
        circle_mode="assist",
    )
    _png(input_root / "Shop" / "menu.png")
    batch = _analyze(service, input_root)
    store = batch["stores"][0]
    assert len(square_calls) == 1, "辅助模式必须保留原有方形码检测"
    assert batch["progress"] == {"done": 3, "total": 3}

    detail = service.get_store(batch["batch_id"], store["store_id"])
    candidates = detail["images"][0]["candidates"]
    assert "mini_program_code" in {candidate.get("code_type") for candidate in candidates}
    recommended = [candidate for candidate in candidates if candidate["recommendation"]]
    assert len(recommended) == 1
    assert recommended[0]["payload_sha256"], "有可解码方形码时圆形候选只作辅助，不抢推荐"
