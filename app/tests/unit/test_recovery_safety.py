from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from menu_capture.updates import recovery


def _create_data_root(root: Path) -> Path:
    data = root / "data"
    data.mkdir(parents=True)
    database = data / "tasks.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE tasks(id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE runs(id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE INDEX idx_tasks_name ON tasks(name);
            INSERT INTO tasks VALUES ('t1', '店铺一'), ('t2', '店铺二');
            INSERT INTO runs VALUES ('r1', 'completed');
            """
        )
    (root / "settings.json").write_text('{"mode":"test"}', encoding="utf-8")
    return data


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, dict]:
    source = tmp_path / "source"
    _create_data_root(source)
    destination = tmp_path / "verify"
    evidence = recovery.prepare_verify_home(source, destination, transaction_id="tx-1")
    return destination, destination / "data", evidence


def test_prepare_verify_home_requires_main_database(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "data").mkdir(parents=True)

    with pytest.raises(ValueError, match="tasks.sqlite3"):
        recovery.prepare_verify_home(source, tmp_path / "verify", transaction_id="tx-1")


def test_prepare_verify_home_rejects_destination_inside_production_data(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _create_data_root(source)

    with pytest.raises(ValueError, match="与生产数据目录完全分离"):
        recovery.prepare_verify_home(
            source, source / "recovery" / "verify", transaction_id="tx-1"
        )


def test_prepare_verify_home_rejects_empty_database_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _create_data_root(source)
    monkeypatch.setattr(recovery, "_sqlite_files", lambda _root: [])

    with pytest.raises(ValueError, match="清单为空"):
        recovery.prepare_verify_home(source, tmp_path / "verify", transaction_id="tx-1")


def test_prepare_verify_home_rejects_oversized_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    _create_data_root(source)
    monkeypatch.setattr(recovery, "MAX_SQLITE_BYTES", 1)

    with pytest.raises(ValueError, match="超过隔离快照上限"):
        recovery.prepare_verify_home(source, tmp_path / "verify", transaction_id="tx-1")


def test_prepare_verify_home_rejects_sqlite_snapshot_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _create_data_root(source)

    def fail_snapshot(_source: Path, _destination: Path) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recovery, "backup_sqlite", fail_snapshot)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        recovery.prepare_verify_home(source, tmp_path / "verify", transaction_id="tx-1")


def test_prepare_verify_home_does_not_ignore_required_small_file_copy_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _create_data_root(source)

    def fail_copy(_source: Path, _destination: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(recovery.shutil, "copy2", fail_copy)
    with pytest.raises(ValueError, match="无法复制隔离验证必需文件：settings.json"):
        recovery.prepare_verify_home(source, tmp_path / "verify", transaction_id="tx-1")


def test_database_readable_rejects_row_loss_in_isolated_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _destination, data_dir, _evidence = _prepare(tmp_path, monkeypatch)
    with sqlite3.connect(data_dir / "tasks.sqlite3") as db:
        db.execute("DELETE FROM tasks WHERE id = 't2'")

    readable, reason = recovery.database_readable(data_dir)
    assert readable is False
    assert "结构或行数不完整" in reason


def test_database_readable_rejects_missing_schema_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _destination, data_dir, _evidence = _prepare(tmp_path, monkeypatch)
    with sqlite3.connect(data_dir / "tasks.sqlite3") as db:
        db.execute("DROP INDEX idx_tasks_name")

    readable, reason = recovery.database_readable(data_dir)
    assert readable is False
    assert "结构或行数不完整" in reason


def test_database_readable_rejects_missing_main_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    readable, reason = recovery.database_readable(data_dir)
    assert readable is False
    assert reason == "缺少 tasks.sqlite3"


@pytest.mark.parametrize(
    ("state", "created_at", "expected"),
    [
        (recovery.ProcessState.ALIVE, 1234.0, recovery.ProcessState.ALIVE),
        (recovery.ProcessState.DEAD, None, recovery.ProcessState.DEAD),
        (recovery.ProcessState.UNKNOWN, None, recovery.ProcessState.UNKNOWN),
        # PID 仍存在，但创建时间变化表示原进程已退出且 PID 被复用。
        (recovery.ProcessState.ALIVE, 1235.0, recovery.ProcessState.DEAD),
    ],
)
def test_process_state_distinguishes_alive_dead_unknown_and_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
    state: recovery.ProcessState,
    created_at: float | None,
    expected: recovery.ProcessState,
) -> None:
    monkeypatch.setattr(recovery, "_process_snapshot", lambda _pid: (state, created_at))
    assert recovery.process_state(42, 1234.0) is expected


def test_process_alive_compatibility_treats_unknown_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        recovery, "_process_snapshot", lambda _pid: (recovery.ProcessState.UNKNOWN, None)
    )
    assert recovery.process_alive(42, 1234.0) is True


def test_current_process_creation_time_comes_from_os_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    value = time.time() - 10
    monkeypatch.setattr(recovery, "process_created_at", lambda pid: value if pid == os.getpid() else None)
    assert recovery.current_process_created_at() == value


def test_current_process_has_real_os_creation_time() -> None:
    created_at = recovery.current_process_created_at()
    assert 0 < created_at <= time.time()
    assert recovery.process_state(os.getpid(), created_at) is recovery.ProcessState.ALIVE


def test_snapshot_metadata_contains_schema_and_table_row_counts(tmp_path: Path) -> None:
    data_dir = _create_data_root(tmp_path)
    snapshot = recovery.snapshot_database(data_dir, tmp_path / "snapshot.sqlite3")

    assert snapshot["row_counts"] == {"runs": 1, "tasks": 2}
    assert {"tasks", "runs"} <= set(snapshot["schema"]["table_columns"])
    assert snapshot["schema"]["objects"]


def test_snapshot_tolerates_rows_written_while_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台同步在两次元数据读取之间新增行时，快照应成功并包含新增行，而不是误杀更新。"""
    data_dir = _create_data_root(tmp_path)
    database = data_dir / "tasks.sqlite3"
    real_backup = recovery.backup_sqlite
    calls = {"count": 0}

    def backup_with_concurrent_write(source: Path, destination: Path) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO tasks VALUES ('t3', '并发新增')")
        real_backup(source, destination)

    monkeypatch.setattr(recovery, "backup_sqlite", backup_with_concurrent_write)
    snapshot = recovery.snapshot_database(data_dir, tmp_path / "snapshot.sqlite3")

    assert snapshot["row_counts"]["tasks"] == 3
    assert calls["count"] == 1


def test_snapshot_still_rejects_a_copy_that_loses_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """放宽并发新增行不能削弱保护：副本少于源库已有行数必须重试后拒绝。"""
    data_dir = _create_data_root(tmp_path)
    real_backup = recovery.backup_sqlite
    monkeypatch.setattr(recovery, "SNAPSHOT_RETRY_DELAY", 0)

    def lossy_backup(source: Path, destination: Path) -> None:
        real_backup(source, destination)
        with sqlite3.connect(destination) as db:
            db.execute("DELETE FROM tasks WHERE id='t2'")

    monkeypatch.setattr(recovery, "backup_sqlite", lossy_backup)
    with pytest.raises(ValueError, match="一致性快照"):
        recovery.snapshot_database(data_dir, tmp_path / "snapshot.sqlite3")
    assert not (tmp_path / "snapshot.sqlite3").exists()


def test_verify_source_tolerates_rows_written_while_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _create_data_root(source)
    database = source / "data" / "tasks.sqlite3"
    real_backup = recovery.backup_sqlite
    calls = {"count": 0}

    def backup_with_concurrent_write(src: Path, destination: Path) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO runs VALUES ('r2', 'running')")
        real_backup(src, destination)

    monkeypatch.setattr(recovery, "backup_sqlite", backup_with_concurrent_write)
    evidence = recovery.prepare_verify_home(source, tmp_path / "verify", transaction_id="tx-race")

    assert evidence["databases"]["tasks.sqlite3"]["row_counts"]["runs"] == 2


def test_verify_source_evidence_cannot_be_replaced_by_an_empty_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _destination, data_dir, _evidence = _prepare(tmp_path, monkeypatch)
    database = data_dir / "tasks.sqlite3"
    database.unlink()
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE tasks(id TEXT PRIMARY KEY, name TEXT NOT NULL);"
            "CREATE TABLE runs(id TEXT PRIMARY KEY, status TEXT NOT NULL);"
            "CREATE INDEX idx_tasks_name ON tasks(name);"
        )

    readable, reason = recovery.database_readable(data_dir)
    assert readable is False
    assert "结构或行数不完整" in reason


def test_verify_evidence_file_is_persisted_with_database_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination, _data_dir, evidence = _prepare(tmp_path, monkeypatch)
    persisted = json.loads((destination / "verify-source.json").read_text(encoding="utf-8"))

    assert persisted == evidence
    assert "tasks.sqlite3" in persisted["databases"]


def test_request_recovery_shutdown_uses_scoped_endpoint_and_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    class Opener:
        def open(self, request: object, timeout: int) -> Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(recovery.urllib.request, "build_opener", lambda *_args: Opener())
    assert recovery.request_recovery_shutdown(
        8765, "secret", transaction_id="tx-1", target_version="0.3.4"
    ) is True

    request = captured["request"]
    assert request.full_url == "http://127.0.0.1:8765/api/internal/recovery-shutdown"
    assert request.get_method() == "POST"
    assert request.get_header("X-coordinator-token") == "secret"
    assert json.loads(request.data) == {"transaction_id": "tx-1", "target_version": "0.3.4"}


def test_request_recovery_shutdown_returns_false_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Opener:
        def open(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("connection refused")

    monkeypatch.setattr(recovery.urllib.request, "build_opener", lambda *_args: Opener())
    assert recovery.request_recovery_shutdown(
        8765, "secret", transaction_id="tx-1", target_version="0.3.4"
    ) is False


def test_prepare_verify_home_skips_recovery_baselines_and_runtime_artifacts(tmp_path: Path) -> None:
    """隔离验证只复制业务库与小配置，跳过历史 baseline 快照与 .uipath-runs 等运行产物。"""
    source = tmp_path / "source"
    data = _create_data_root(source)
    (data / "recovery").mkdir()
    (data / "recovery" / "baseline-20261127-old.sqlite3").write_bytes((data / "tasks.sqlite3").read_bytes())
    (data / "backups").mkdir()
    (data / "backups" / "backup.sqlite3").write_bytes((data / "tasks.sqlite3").read_bytes())
    (data / "shared-inputs").mkdir()
    (data / "shared-inputs" / "meta.json").write_text("{}", encoding="utf-8")
    (source / ".uipath-runs" / "run-1").mkdir(parents=True)
    (source / ".uipath-runs" / "run-1" / "entry.json").write_text("{}", encoding="utf-8")
    (source / ".uipath-staging" / "pkg").mkdir(parents=True)
    (source / ".uipath-staging" / "pkg" / "info.json").write_text("{}", encoding="utf-8")

    destination = tmp_path / "verify-tx-skip"
    evidence = recovery.prepare_verify_home(source, destination, transaction_id="tx-skip")

    assert list(evidence["databases"].keys()) == ["tasks.sqlite3"]
    assert not (destination / "data" / "recovery").exists()
    assert not (destination / "data" / "backups").exists()
    assert not (destination / "data" / "shared-inputs").exists()
    assert not (destination / ".uipath-runs").exists()
    assert not (destination / ".uipath-staging").exists()
    assert (destination / "settings.json").is_file()


def test_database_readable_does_not_scan_main_database_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _destination, data_dir, _evidence = _prepare(tmp_path, monkeypatch)
    real_metadata = recovery.database_metadata
    calls: list[Path] = []

    def counting_metadata(database: Path) -> dict:
        calls.append(Path(database).resolve())
        return real_metadata(database)

    monkeypatch.setattr(recovery, "database_metadata", counting_metadata)
    readable, reason = recovery.database_readable(data_dir)
    assert readable is True
    assert reason == ""
    assert calls == [(data_dir / "tasks.sqlite3").resolve()]


def test_cleanup_verify_home_and_prune_recovery_baselines(tmp_path: Path) -> None:
    data_root = tmp_path / "MenuCapture"
    data = _create_data_root(data_root)
    verify_dir = tmp_path / "MenuCapture-verify" / "verify-tx-clean"
    recovery.prepare_verify_home(data_root, verify_dir, transaction_id="tx-clean")

    assert recovery.cleanup_verify_home(data_root, data_root=data_root) is False
    assert recovery.cleanup_verify_home(tmp_path / "other", data_root=data_root) is False
    assert recovery.cleanup_verify_home(verify_dir, data_root=data_root) is True
    assert not verify_dir.exists()

    rec_dir = data / "recovery"
    rec_dir.mkdir()
    b1 = rec_dir / "baseline-1.sqlite3"
    b2 = rec_dir / "baseline-2.sqlite3"
    b3 = rec_dir / "baseline-3.sqlite3"
    other = rec_dir / "keep-me.json"
    for idx, f in enumerate((b1, b2, b3)):
        f.write_bytes(b"x")
        os.utime(f, (1000 + idx, 1000 + idx))
    other.write_text("{}", encoding="utf-8")

    removed = recovery.prune_recovery_baselines(data, keep=1)
    assert removed == 2
    assert not b1.exists()
    assert not b2.exists()
    assert b3.exists()
    assert other.exists()
