from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from menu_capture.store import TaskStore, now


def _image(path: Path, color: str = "navy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (9, 7), color).save(path)
    return path


def _snapshot(parent: Path, snapshot_id: str, *, shop: str = "Cafe", color: str = "navy") -> Path:
    root = parent / snapshot_id
    root.mkdir(parents=True, exist_ok=True)
    (root / ".materials-snapshot.json").write_text(
        json.dumps({"snapshot_id": snapshot_id}), encoding="utf-8"
    )
    _image(root / shop / "qr.png", color)
    return root


def _insert_legacy(store: TaskStore, task_id: str, path: Path, digest: str, **values) -> None:
    defaults = {
        "source_name": path.parent.name,
        "source_path": str(path),
        "source_hash": digest,
        "status": "pending",
        "stage": "queued",
        "message": "",
        "output_dir": "",
        "updated_at": now(),
        "reject_reason": "",
        "reject_detail": "",
        "rejected_at": "",
        "source_key": "",
    }
    defaults.update(values)
    with store.connect() as db:
        db.execute(
            "INSERT INTO tasks(id,source_name,source_path,source_hash,status,stage,message,output_dir,"
            "updated_at,reject_reason,reject_detail,rejected_at,source_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, defaults["source_name"], defaults["source_path"], defaults["source_hash"],
             defaults["status"], defaults["stage"], defaults["message"], defaults["output_dir"],
             defaults["updated_at"], defaults["reject_reason"], defaults["reject_detail"],
             defaults["rejected_at"], defaults["source_key"]),
        )


def test_unchanged_scan_is_idempotent(tmp_path: Path) -> None:
    root = _snapshot(tmp_path / "published", "a" * 64)
    store = TaskStore(tmp_path / "tasks.sqlite3")

    assert store.scan(root) == {"added": 1, "total": 1}
    original = store.tasks()[0]
    assert store.scan(root) == {"added": 0, "total": 1}
    assert store.tasks() == [original]


def test_moved_snapshot_preserves_task_state_and_updates_source_path(tmp_path: Path) -> None:
    snapshot_id = "b" * 64
    old_root = _snapshot(tmp_path / "before", snapshot_id)
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.scan(old_root)
    task = store.tasks()[0]
    output = tmp_path / "capture" / "Cafe"
    _image(output / "page-1.png", "white")
    store.update(task["id"], status="rejected", stage="review", message="manual review",
                 output_dir=str(output), reject_reason="wrong_store", reject_detail="detail",
                 rejected_at="2026-11-05T12:00:00+08:00")

    moved_root = _snapshot(tmp_path / "after" / "another-parent", snapshot_id)
    assert store.scan(moved_root) == {"added": 0, "total": 1}
    moved = store.get(task["id"])
    assert moved["id"] == task["id"]
    assert moved["source_path"] == str(moved_root / "Cafe" / "qr.png")
    assert moved["status"] == "rejected"
    assert moved["reject_reason"] == "wrong_store"
    assert moved["reject_detail"] == "detail"
    assert moved["output_dir"] == str(output)
    assert (output / "page-1.png").exists()


def test_scan_adopts_legacy_row_at_current_path_and_keeps_its_id(tmp_path: Path) -> None:
    root = _snapshot(tmp_path / "parent", "c" * 64)
    image = root / "Cafe" / "qr.png"
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    store = TaskStore(tmp_path / "tasks.sqlite3")
    output = tmp_path / "old-output"
    _image(output / "page-1.png", "white")
    _insert_legacy(store, "legacy-task-id", image, digest, status="captured", stage="captured",
                   output_dir=str(output))

    assert store.scan(root) == {"added": 0, "total": 1}
    task = store.get("legacy-task-id")
    assert task["source_key"]
    assert task["status"] == "captured"
    assert task["output_dir"] == str(output)


def test_moved_snapshot_adopts_legacy_task_by_snapshot_id_relative_path_and_hash(tmp_path: Path) -> None:
    snapshot_id = "d" * 64
    current = _snapshot(tmp_path / "new-location", snapshot_id)
    image = current / "Cafe" / "qr.png"
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    missing_old_path = tmp_path / "removed-old-root" / snapshot_id / "Cafe" / "qr.png"
    store = TaskStore(tmp_path / "tasks.sqlite3")
    _insert_legacy(store, "old-id", missing_old_path, digest, status="rejected",
                   reject_reason="review", reject_detail="keep me", rejected_at="yesterday")

    assert store.scan(current) == {"added": 0, "total": 1}
    adopted = store.get("old-id")
    assert adopted["source_path"] == str(image)
    assert adopted["status"] == "rejected"
    assert adopted["reject_detail"] == "keep me"


def test_equal_shop_and_qr_in_different_snapshots_are_distinct_tasks(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    first = _snapshot(tmp_path / "one", "e" * 64)
    second = _snapshot(tmp_path / "two", "f" * 64)
    store.scan(first)
    store.scan(second)
    tasks = store.tasks()
    assert len(tasks) == 2
    assert tasks[0]["source_name"] == tasks[1]["source_name"] == "Cafe"
    assert tasks[0]["source_hash"] == tasks[1]["source_hash"]
    assert tasks[0]["id"] != tasks[1]["id"]


def test_identical_raw_directories_have_distinct_task_identity(tmp_path: Path) -> None:
    first = _image(tmp_path / "raw-a" / "Cafe" / "qr.png")
    second = _image(tmp_path / "raw-b" / "Cafe" / "qr.png")
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.scan(first.parent.parent)
    store.scan(second.parent.parent)
    assert len(store.tasks()) == 2
    assert store.tasks()[0]["source_hash"] == store.tasks()[1]["source_hash"]
    assert store.tasks()[0]["id"] != store.tasks()[1]["id"]


@pytest.mark.parametrize("marker", ["not-json", '{"snapshot_id":"bad"}', "[]"])
def test_malformed_or_mismatched_snapshot_marker_fails_explicitly(tmp_path: Path, marker: str) -> None:
    root = tmp_path / ("1" * 64)
    root.mkdir()
    (root / ".materials-snapshot.json").write_text(marker, encoding="utf-8")
    with pytest.raises(ValueError, match="素材快照标记"):
        TaskStore(tmp_path / "tasks.sqlite3").scan(root)


def test_ambiguous_legacy_candidates_raise_without_deleting_or_changing_rows(tmp_path: Path) -> None:
    snapshot_id = "1" * 64
    root = _snapshot(tmp_path / "current", snapshot_id)
    image = root / "Cafe" / "qr.png"
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    legacy = tmp_path / "vanished" / snapshot_id / "Cafe" / "qr.png"
    store = TaskStore(tmp_path / "tasks.sqlite3")
    _insert_legacy(store, "legacy-a", legacy, digest, status="rejected", reject_reason="a")
    _insert_legacy(store, "legacy-b", legacy, digest, status="captured", output_dir="/old/output")
    before = {row["id"]: dict(row) for row in store.tasks()}

    with pytest.raises(ValueError, match="多个可接管"):
        store.scan(root)
    assert {row["id"]: dict(row) for row in store.tasks()} == before


def test_concurrent_published_snapshot_scans_insert_only_one_task(tmp_path: Path) -> None:
    root = _snapshot(tmp_path / "published", "2" * 64)
    store = TaskStore(tmp_path / "tasks.sqlite3")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store.scan(root), range(2)))
    assert sorted(result["added"] for result in results) == [0, 1]
    assert all(result["total"] == 1 for result in results)
    assert len(store.tasks()) == 1
