"""合格成品目录同步规则：业务状态变化到目录增删替换的端到端单元测试。"""
from __future__ import annotations

import json
from pathlib import Path

from menu_capture.shared import delivery as delivery_module
from menu_capture.shared.delivery import DeliveryService, safe_directory_name


def _task(tmp_path: Path, *, task_id: str, batch: str, store: str, name: str,
          pages: int = 3, status: str = "captured", project_id: str = "p-q3",
          project_code: str = "Q3", project_name: str = "项目 Q3", **extra) -> dict:
    output = tmp_path / "data" / "shared-results" / task_id / "artifacts"
    output.mkdir(parents=True, exist_ok=True)
    for index in range(1, pages + 1):
        (output / f"page-{index:04d}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + f"page-{index}".encode() * 10)
    # 非菜单截图与元数据绝不能进入交付目录。
    (output / "wechat-safe-start.png").write_bytes(b"evidence")
    (output / "manifest.json").write_text("{}", encoding="utf-8")
    return {
        "id": task_id, "project_id": project_id, "project_code": project_code, "project_name": project_name,
        "batch_id": batch, "batch_name": f"批次 {batch}",
        "store_id": store, "source_name": name, "output_dir": str(output),
        "status": status, "updated_at": "2026-11-25T00:00:00+00:00", **extra,
    }


def _service(tmp_path: Path, tasks: list[dict], *, configured: bool = True) -> DeliveryService:
    host = tmp_path / "host"
    if configured:
        # 模拟宿主机 bind mount 已存在；交付根由服务在挂载内创建。
        host.mkdir(parents=True, exist_ok=True)
    root = str(host) if configured else ""
    projects = {
        "p-q3": {"id": "p-q3", "code": "Q3", "name": "项目 Q3"},
        "p-q4": {"id": "p-q4", "code": "Q4", "name": "项目 Q4"},
    }
    return DeliveryService(
        tmp_path / "data",
        root=root,
        host_path="D:\\MenuCapture" if configured else "",
        task_provider=lambda: list(tasks),
        project_provider=lambda project_id: projects[project_id],
    )


def test_unconfigured_reports_reason_without_writing(tmp_path):
    service = _service(tmp_path, [], configured=False)

    status = service.status()

    assert status["configured"] is False
    assert status["state"] == "unconfigured"
    assert status["host_path"] == ""
    assert not (tmp_path / "host").exists()


def test_sync_copies_only_menu_pages_and_reports_counts(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=3)
    service = _service(tmp_path, [task])

    service.reconcile()

    target = tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A"
    assert sorted(path.name for path in target.iterdir()) == [
        "page-0001.png", "page-0002.png", "page-0003.png",
    ]
    status = service.status()
    assert status["state"] == "synced"
    total = sum(len(b"\x89PNG\r\n\x1a\n" + f"page-{index}".encode() * 10) for index in range(1, 4))
    assert status["counts"] == {
        "qualified": 1, "ready": 1, "pending": 0, "failed": 0, "images": 3, "bytes": total,
    }
    assert status["content_digest"]
    assert status["inventory"]["verified"]["images"] == 3
    assert service.stores()["items"][0]["status"] == "ready"


def test_recapture_replaces_directory_without_stale_pages(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=10)
    service = _service(tmp_path, [task])
    service.reconcile()

    output = Path(task["output_dir"])
    for index in range(7, 11):
        (output / f"page-{index:04d}.png").unlink()
    task["updated_at"] = "2026-11-25T01:00:00+00:00"
    service.reconcile()

    target = tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A"
    assert sorted(path.name for path in target.iterdir()) == [
        f"page-{index:04d}.png" for index in range(1, 7)
    ]


def test_rejected_or_returned_store_is_removed(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A")
    service = _service(tmp_path, [task])
    service.reconcile()
    assert (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A").is_dir()

    task["reject_reason"] = "二维码无法识别"
    service.reconcile()
    assert not (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A").exists()
    assert service.status()["counts"]["qualified"] == 0

    # 撤销不合格后自动恢复。
    task.pop("reject_reason")
    service.reconcile()
    assert (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A").is_dir()


def test_qualified_but_missing_screenshots_is_pending_not_fake_ready(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A")
    service = _service(tmp_path, [task])
    service.reconcile()

    for path in Path(task["output_dir"]).glob("page-*.png"):
        path.unlink()
    service.reconcile()

    assert not (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A").exists()
    status = service.status()
    assert status["counts"]["qualified"] == 1
    assert status["counts"]["ready"] == 0
    assert status["counts"]["pending"] == 1
    assert status["state"] == "syncing"
    assert "缺少菜单截图" in service.stores()["items"][0]["reason"]


def test_same_store_in_different_batches_stays_independent(tmp_path):
    older = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=2,
                  batch_version="1", material_version="1", updated_at="2026-11-20T00:00:00+00:00")
    newer = _task(tmp_path, task_id="a2", batch="b2", store="E02412", name="门店 A", pages=3,
                  batch_version="1", material_version="2", updated_at="2026-11-24T00:00:00+00:00")
    service = _service(tmp_path, [older, newer])

    service.reconcile()

    # 门店身份是批次 + 门店，目录名只是展示名称：同名跨批次不能互相覆盖。
    names = sorted(path.name for path in (tmp_path / "host" / "项目" / "Q3" / "合格").iterdir())
    assert len(names) == 2
    assert all((tmp_path / "host" / "项目" / "Q3" / "合格" / name).is_dir() for name in names)
    assert service.status()["counts"]["qualified"] == 2


def test_same_batch_rerun_keeps_only_latest_round(tmp_path):
    older = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=2,
                  batch_version="1", material_version="1", updated_at="2026-11-20T00:00:00+00:00")
    newer = _task(tmp_path, task_id="a2", batch="b1", store="E02412", name="门店 A", pages=3,
                  batch_version="1", material_version="2", updated_at="2026-11-24T00:00:00+00:00")
    service = _service(tmp_path, [older, newer])

    service.reconcile()

    names = sorted(path.name for path in (tmp_path / "host" / "项目" / "Q3" / "合格").iterdir())
    assert names == ["门店 A"]
    target = tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A"
    assert sorted(path.name for path in target.iterdir()) == [
        "page-0001.png", "page-0002.png", "page-0003.png",
    ]
    assert service.status()["counts"]["qualified"] == 1


def test_same_store_name_with_different_ids_stays_independent(tmp_path):
    first = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=2)
    second = _task(tmp_path, task_id="a2", batch="b2", store="E02413", name="门店 A", pages=3)
    service = _service(tmp_path, [first, second])

    service.reconcile()

    names = sorted(path.name for path in (tmp_path / "host" / "项目" / "Q3" / "合格").iterdir())
    assert len(names) == 2
    assert service.status()["counts"]["ready"] == 2


def test_same_store_in_two_projects_has_scoped_paths_and_status(tmp_path):
    first = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="同名门店")
    second = _task(tmp_path, task_id="a2", batch="b2", store="E02412", name="同名门店",
                   project_id="p-q4", project_code="Q4", project_name="项目 Q4")
    tasks = [first, second]
    service = _service(tmp_path, tasks)

    service.reconcile()

    q3 = tmp_path / "host" / "项目" / "Q3" / "合格" / "同名门店"
    q4 = tmp_path / "host" / "项目" / "Q4" / "合格" / "同名门店"
    assert q3.is_dir() and q4.is_dir()
    assert service.status("p-q3")["counts"]["qualified"] == 1
    assert service.status("p-q4")["counts"]["qualified"] == 1
    assert service.stores(project_id="p-q3")["total"] == 1
    assert service.stores(project_id="p-q4")["total"] == 1
    q4_generation = service.status("p-q4")["generation"]

    tasks.remove(first)
    service.reconcile()

    assert not q3.exists()
    assert q4.is_dir()
    assert service.status("p-q4")["generation"] == q4_generation


def test_task_without_project_identity_is_pending_and_never_projected(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="待核验门店")
    task.pop("project_id")
    task.pop("project_code")
    service = _service(tmp_path, [task])

    service.reconcile()

    assert not (tmp_path / "host" / "项目" / "Q3" / "合格" / "待核验门店").exists()
    assert service.status()["counts"]["qualified"] == 0
    pending = service.status()["inventory"]["pending_verification"]
    assert pending["count"] == 1
    assert "项目归属" in pending["items"][0]["reason"]


def test_lost_project_metadata_preserves_existing_projection_for_review(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="待核验门店")
    service = _service(tmp_path, [task])
    service.reconcile()
    target = tmp_path / "host" / "项目" / "Q3" / "合格" / "待核验门店"
    assert target.is_dir()

    task.pop("project_id")
    task.pop("project_code")
    service.reconcile()

    # 项目归属缺失时不能把已有成果误认为已失效并自动移除。
    assert target.is_dir()
    assert service.status()["inventory"]["pending_verification"]["count"] == 1


def test_v2_unscoped_index_and_old_delivery_root_are_preserved(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A")
    old_root = tmp_path / "host" / "合格"
    old_root.mkdir(parents=True, exist_ok=True)
    old_directory = old_root / "旧目录"
    old_directory.mkdir()
    (old_directory / "page-0001.png").write_bytes(b"legacy")
    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    (data_root / "delivery-index.json").write_text(json.dumps({
        "version": 2,
        "stores": {"batch:old|store:old": {"directory": "旧目录", "status": "ready"}},
        "directories": {"batch:old|store:old": "旧目录"},
    }), encoding="utf-8")
    service = _service(tmp_path, [task])

    service.reconcile()

    assert (old_directory / "page-0001.png").read_bytes() == b"legacy"
    index = json.loads((data_root / "delivery-index.json").read_text(encoding="utf-8"))
    assert "batch:old|store:old" in index["stores"]
    assert index["version"] == 3


def test_symlink_project_root_is_rejected_without_writing_outside_mount(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A")
    service = _service(tmp_path, [task])
    outside = tmp_path / "outside"
    outside.mkdir()
    projects_root = tmp_path / "host" / "项目"
    projects_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        projects_root.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        import pytest
        pytest.skip(f"当前平台无法创建目录符号链接：{error}")

    service.reconcile()

    assert list(outside.iterdir()) == []
    assert service.status("p-q3")["counts"]["failed"] == 1


def test_unchanged_store_is_not_rebuilt(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A")
    service = _service(tmp_path, [task])
    service.reconcile()
    first_generation = service.status()["generation"]

    service.reconcile()

    assert service.status()["generation"] == first_generation


def test_manually_deleted_delivery_file_is_repaired(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A")
    service = _service(tmp_path, [task])
    service.reconcile()
    (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A" / "page-0002.png").unlink()

    service.reconcile()

    assert (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A" / "page-0002.png").is_file()
    # 修复只重建目录，业务状态（任务数据）不被修改。
    assert task["status"] == "captured"
    assert "reject_reason" not in task


def test_directory_names_are_windows_safe_and_stable_across_restart(tmp_path):
    assert safe_directory_name('门店:A/B*?') == "门店_A_B__"
    assert safe_directory_name("CON") == "_CON"
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店:A")
    service = _service(tmp_path, [task])
    service.reconcile()
    name = service.stores()["items"][0]["directory_name"]

    restarted = _service(tmp_path, [task])
    restarted.reconcile()

    assert restarted.stores()["items"][0]["directory_name"] == name
    assert (tmp_path / "host" / "项目" / "Q3" / "合格" / name).is_dir()


def _legacy_dir(base: Path, store: str, pages: int = 2, *, batch: str = "") -> Path:
    folder = base / batch / store if batch else base / store
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1, pages + 1):
        (folder / f"page-{index:04d}.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + f"legacy-{index}".encode() * 8)
    return folder


def test_legacy_output_without_business_record_goes_pending(tmp_path):
    _legacy_dir(tmp_path / "output", "门店 L", pages=4)
    service = _service(tmp_path, [])

    service.reconcile()

    # 文件系统不是第二事实源：没有权威业务记录的历史目录不得直接进入交付目录。
    assert not (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 L").exists()
    status = service.status()
    assert status["counts"]["qualified"] == 0
    assert status["inventory"]["discovered"]["output"] == 1
    assert status["inventory"]["legacy_included"] == 0
    pending = status["inventory"]["pending_verification"]
    assert pending["count"] == 1 and pending["stores"] == 1
    assert pending["items"][0]["name"] == "门店 L"
    assert "待核验" in pending["items"][0]["reason"]


def test_legacy_directory_with_same_name_is_not_linked_to_other_project_state(tmp_path):
    rejected = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", status="captured")
    rejected["reject_reason"] = "二维码无法识别"
    _legacy_dir(tmp_path / "output", "门店 A", pages=3)
    service = _service(tmp_path, [rejected])

    service.reconcile()

    assert not (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A").exists()
    status = service.status()
    assert status["counts"]["qualified"] == 0
    assert status["inventory"]["excluded"]["reasons"] == []
    pending = status["inventory"]["pending_verification"]
    assert pending["count"] == 1
    assert "待核验" in pending["items"][0]["reason"]


def test_deleted_batch_legacy_directory_is_not_reincluded(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=2)
    _legacy_dir(tmp_path / "output", "门店 A", pages=3)
    tasks = [task]
    service = _service(tmp_path, tasks)
    service.reconcile()
    assert (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A").is_dir()

    # 批次被删除后当前有效任务消失，历史目录不能把门店重新带回合格目录。
    tasks.clear()
    service.reconcile()

    assert not (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A").exists()
    status = service.status()
    assert status["counts"]["qualified"] == 0
    assert status["inventory"]["pending_verification"]["count"] == 1


def test_ambiguous_legacy_rounds_are_reported_not_guessed(tmp_path):
    _legacy_dir(tmp_path / "output", "门店 M", pages=2, batch="batch-1")
    _legacy_dir(tmp_path / "output", "门店 M", pages=5, batch="batch-2")
    service = _service(tmp_path, [])

    service.reconcile()

    assert not (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 M").exists()
    status = service.status()
    assert status["counts"]["ready"] == 0
    pending = status["inventory"]["pending_verification"]
    assert pending["count"] == 2 and pending["stores"] == 1
    assert all("多轮" in item["reason"] for item in pending["items"])


def test_legacy_sources_only_pending_valid_menu_screenshots(tmp_path):
    _legacy_dir(tmp_path / "legacy-sources" / "1", "门店 S", pages=3)
    broken = tmp_path / "legacy-sources" / "2" / "坏图门店"
    broken.mkdir(parents=True)
    (broken / "page-0001.png").write_bytes(b"not-a-png")
    service = _service(tmp_path, [])

    service.reconcile()

    assert not (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 S").exists()
    assert not (tmp_path / "host" / "项目" / "Q3" / "合格" / "坏图门店").exists()
    status = service.status()
    assert status["inventory"]["discovered"]["legacy_sources"] == 2
    assert status["inventory"]["excluded"]["reasons"] == [{"reason": "非有效菜单截图", "count": 1}]
    assert status["inventory"]["pending_verification"]["count"] == 1


def test_restart_resumes_initialization_from_persisted_index(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=2)
    first = _service(tmp_path, [task])
    first.reconcile()
    generation = first.status()["generation"]

    restarted = _service(tmp_path, [task])
    restarted.reconcile()

    assert restarted.status()["generation"] == generation
    assert (tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A" / "page-0002.png").is_file()


def test_reveal_is_rejected_when_hub_runs_in_container(tmp_path, monkeypatch):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A")
    service = _service(tmp_path, [task])
    service.reconcile()
    monkeypatch.setattr(delivery_module.sys, "platform", "linux")

    status = service.status()
    assert status["open_supported"] is False
    assert "容器" in status["open_reason"] or "无法直接打开" in status["open_reason"]
    try:
        service.reveal()
    except ValueError as error:
        assert "无法直接打开" in str(error) or "复制路径" in str(error)
    else:
        raise AssertionError("容器内不应允许打开宿主机目录")


def test_failed_store_enters_backoff_and_status_partial(tmp_path, monkeypatch):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A")
    service = _service(tmp_path, [task])

    def boom(*_args, **_kwargs):
        raise OSError("磁盘已满")

    monkeypatch.setattr(delivery_module, "_copy_and_hash", boom)
    service.reconcile()

    status = service.status()
    assert status["state"] == "partial"
    assert status["counts"]["failed"] == 1
    index = json.loads((tmp_path / "data" / "delivery-index.json").read_text(encoding="utf-8"))
    assert index["failures"]["project:p-q3|batch:b1|store:E02412"]["error"] == "磁盘已满"


def test_same_size_different_content_is_never_reused(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=2)
    service = _service(tmp_path, [task])
    service.reconcile()
    target = tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A"

    # 模拟重采：文件名与大小完全相同，但画面内容不同。
    output = Path(task["output_dir"])
    for index in (1, 2):
        page = output / f"page-{index:04d}.png"
        original = page.read_bytes()
        page.write_bytes(original[:8] + b"X" * (len(original) - 8))
    task["updated_at"] = "2026-11-25T02:00:00+00:00"
    service.reconcile()

    for index in (1, 2):
        source = output / f"page-{index:04d}.png"
        assert (target / f"page-{index:04d}.png").read_bytes() == source.read_bytes()


def test_same_size_tampering_of_delivered_file_is_detected(tmp_path):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=2)
    service = _service(tmp_path, [task])
    service.reconcile()
    delivered = tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A" / "page-0001.png"
    original = delivered.read_bytes()
    delivered.write_bytes(original[:8] + b"Y" * (len(original) - 8))

    service.reconcile()

    # 只比大小无法发现同尺寸篡改，必须按内容摘要修复。
    assert delivered.read_bytes() == Path(task["output_dir"], "page-0001.png").read_bytes()


def test_publish_is_abandoned_when_round_changes_mid_sync(tmp_path, monkeypatch):
    task = _task(tmp_path, task_id="a1", batch="b1", store="E02412", name="门店 A", pages=2)
    service = _service(tmp_path, [task])
    service.reconcile()
    target = tmp_path / "host" / "项目" / "Q3" / "合格" / "门店 A"
    before = (target / "page-0001.png").read_bytes()

    output = Path(task["output_dir"])
    for index in (1, 2):
        page = output / f"page-{index:04d}.png"
        page.write_bytes(b"\x89PNG\r\n\x1a\n" + b"Z" * (len(page.read_bytes()) - 8))
    task["updated_at"] = "2026-11-25T03:00:00+00:00"

    original = service._still_current
    monkeypatch.setattr(service, "_still_current", lambda _record: False)
    service.reconcile()
    # 复核失败时不得发布过期轮次，旧目录保持原样等待下一轮修正。
    assert (target / "page-0001.png").read_bytes() == before

    monkeypatch.setattr(service, "_still_current", original)
    service.reconcile()
    assert (target / "page-0001.png").read_bytes() == (output / "page-0001.png").read_bytes()
