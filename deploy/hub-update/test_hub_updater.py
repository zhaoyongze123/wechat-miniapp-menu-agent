"""Hub 宿主机更新执行器单测：只覆盖契约与事务编排，真实 docker 由 CI 验收。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import hub_updater
import pytest
from hub_updater import (
    Config,
    HubUpdater,
    UpdateError,
    _client_allowed,
    validate_payload,
)

TOKEN = "t" * 32


REGISTRY = "ghcr.io/zhaoyongze123/wechat-miniapp-menu-agent"
DIGEST = "sha256:" + "a" * 64


def make_config(tmp_path: Path, registry: str = "", *, require_oci: bool = False) -> Config:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    env = {
        "HUB_UPDATE_TOKEN": TOKEN,
        "HUB_UPDATE_COMPOSE_FILE": str(compose),
        "HUB_UPDATE_PROJECT": "menu-capture",
        "HUB_UPDATE_STATE_DIR": str(tmp_path / "state"),
        "HUB_UPDATE_ARCHIVE_DIR": str(tmp_path / "archives"),
        "HUB_UPDATE_DATA_VOLUME": "menu-capture_hub-data",
        "HUB_UPDATE_HEALTH_TIMEOUT": "1",
    }
    if registry:
        env["HUB_UPDATE_REGISTRY_REPO"] = registry
    if require_oci:
        env["HUB_UPDATE_REQUIRE_OCI"] = "1"
    return Config(env)


def archive_request(tmp_path: Path, version: str = "0.2.4", content: bytes = b"image-bytes") -> dict:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = f"hub-image-{version}.tar.gz"
    (archive_dir / name).write_bytes(content)
    return {
        "version": version,
        "confirm": version,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "archive": name,
    }


class FakeDocker:
    def __init__(self, current: str = "menu-capture-hub:0.2.3", loaded: str = "menu-capture-hub:0.2.4",
                 volume_path: Path | None = None) -> None:
        self.current = current
        self.loaded = loaded
        self.volume_path = volume_path
        self.fail_switch = False
        self.switched: list[str] = []
        self.fail_load = False
        self.fail_pull = False
        self.pulled = ""
        # None 表示“回显刚拉取的引用”，用于制造摘要不一致场景时显式覆盖。
        self.repo_digests: list[str] | None = None
        self.platform = "linux/amd64"
        self.tags: list[tuple[str, str]] = []
        self.loads: list[str] = []
        # `docker compose version` 的输出决定是否使用 --wait。
        self.compose_version = "Docker Compose version v2.29.7"
        self.calls: list[list[str]] = []
        self.compose_calls: list[tuple[list[str], dict[str, str]]] = []

    def run(self, args: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(list(args))
        command = args[:2]
        if command == ["docker", "volume"]:
            return 0, "[]"
        if command == ["docker", "pull"]:
            assert args[2] == "--platform"
            assert args[3] == "linux/amd64"
            if self.fail_pull:
                return 1, "pull access denied"
            self.pulled = args[4]
            return 0, "pulled"
        if command == ["docker", "tag"]:
            self.tags.append((args[2], args[3]))
            return 0, ""
        if command == ["docker", "run"]:
            assert args[args.index("--network") + 1] == "none"
            assert "--pull=never" in args
            assert "--read-only" in args
            assert "--cap-drop=ALL" in args
            assert "--security-opt=no-new-privileges" in args
            assert args[args.index("--mount") + 1].endswith(",readonly")
            assert args[args.index("--entrypoint") + 1] == "python"
            assert args[-1] == "/hub-data"
            return 0, json.dumps(schema_probe_payload(self.volume_path))
        if command == ["docker", "load"]:
            if self.fail_load:
                return 1, "invalid archive"
            self.loads.append(args[3])
            return 0, f"Loaded image: {self.loaded}"
        if command == ["docker", "compose"] and args[2:] == ["version"]:
            return 0, self.compose_version
        if command == ["docker", "compose"] and "ps" in args:
            return 0, "cid-1"
        if command == ["docker", "inspect"]:
            if "Health" in args[3]:
                return 0, "healthy"
            return 0, self.current
        if command == ["docker", "image"]:
            if args[2] == "inspect" and "--format" in args:
                template = args[args.index("--format") + 1]
                if template == "{{json .}}":
                    os_name, _, architecture = self.platform.partition("/")
                    digests = self.repo_digests if self.repo_digests is not None else [self.pulled]
                    return 0, json.dumps({
                        "RepoDigests": digests, "Os": os_name, "Architecture": architecture,
                    })
            return 0, "[]"
        raise AssertionError(f"unexpected command: {args}")

    def run_env(self, args: list[str], timeout: float, env: dict[str, str]) -> tuple[int, str]:
        self.compose_calls.append((list(args), dict(env)))
        if self.fail_switch:
            return 1, "compose failed"
        self.current = env["MENU_CAPTURE_IMAGE"]
        self.switched.append(self.current)
        return 0, ""


def make_updater(tmp_path: Path, docker: FakeDocker, *, healthy: Callable[[str], dict],
                 registry: str = "", require_oci: bool = False) -> HubUpdater:
    volume_path = tmp_path / "volume"
    volume_path.mkdir(parents=True, exist_ok=True)
    docker.volume_path = volume_path
    return HubUpdater(make_config(tmp_path, registry, require_oci=require_oci), run=docker.run, run_env=docker.run_env,
                      health=healthy, sleep=lambda _seconds: None)


def create_hub_db(tmp_path: Path) -> Path:
    path = tmp_path / "volume" / "data" / "tasks.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending')")
        db.execute("CREATE TABLE events (task_id TEXT REFERENCES tasks(id))")
        db.execute("CREATE VIEW task_status AS SELECT id, status FROM tasks")
        db.execute("CREATE TRIGGER tasks_noop AFTER INSERT ON tasks BEGIN SELECT 1; END")
    return path


def schema_probe_payload(volume_path: Path | None) -> dict:
    if volume_path is None:
        return {"exists": False, "tables": [], "objects": []}
    path = volume_path / "data" / "tasks.sqlite3"
    if not path.exists():
        return {"exists": False, "tables": [], "objects": []}

    def quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    with sqlite3.connect(path) as db:
        tables = []
        for name, sql in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            indexes = []
            for row in db.execute(f"PRAGMA index_list({quote(name)})"):
                index_name = row[1]
                index_sql = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,),
                ).fetchone()[0]
                columns = [item[2] for item in db.execute(f"PRAGMA index_info({quote(index_name)})")]
                indexes.append({
                    "name": index_name, "unique": int(row[2]), "origin": row[3], "partial": int(row[4]),
                    "columns": columns, "sql": index_sql or "",
                })
            tables.append({
                "name": name, "sql": sql or "",
                "columns": [list(row) for row in db.execute(f"PRAGMA table_xinfo({quote(name)})")],
                "indexes": indexes,
                "foreign_keys": [list(row) for row in db.execute(f"PRAGMA foreign_key_list({quote(name)})")],
            })
        objects = [list(row) for row in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE type IN ('view','trigger') ORDER BY type,name"
        )]
    return {"exists": True, "tables": tables, "objects": objects}


def test_apply_switches_and_records_previous(tmp_path: Path) -> None:
    docker = FakeDocker()
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})

    status = updater.apply(archive_request(tmp_path))

    assert status["status"] == "completed"
    assert status["target_version"] == "0.2.4"
    assert status["previous_version"] == "0.2.3"
    assert docker.switched == ["menu-capture-hub:0.2.4"]
    assert (tmp_path / "state" / "hub-update-audit.jsonl").is_file()


def test_apply_health_failure_rolls_back(tmp_path: Path) -> None:
    docker = FakeDocker()
    calls = {"count": 0}

    def healthy(version: str) -> dict:
        calls["count"] += 1
        if version == "0.2.4":
            return {"ok": False, "version": "", "detail": "版本接口不可用"}
        return {"ok": True, "version": version}

    status = make_updater(tmp_path, docker, healthy=healthy).apply(archive_request(tmp_path))

    assert status["status"] == "rolled_back"
    assert docker.switched == ["menu-capture-hub:0.2.4", "menu-capture-hub:0.2.3"]
    journal = updater_journal(tmp_path)
    assert "回滚" in journal["message"]
    assert journal["error"]


def test_apply_rejects_downgrade(tmp_path: Path) -> None:
    docker = FakeDocker(current="menu-capture-hub:0.2.5")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})

    with pytest.raises(UpdateError, match="禁止降级"):
        updater.apply(archive_request(tmp_path))
    assert docker.switched == []


def test_apply_rejects_checksum_mismatch(tmp_path: Path) -> None:
    request = archive_request(tmp_path)
    request["sha256"] = "0" * 64
    updater = make_updater(tmp_path, FakeDocker(), healthy=lambda version: {"ok": True, "version": version})

    with pytest.raises(UpdateError, match="SHA-256"):
        updater.apply(request)
    assert updater.journal()["status"] == "failed"
    assert updater.journal()["error"]


def test_load_must_contain_expected_tag(tmp_path: Path) -> None:
    docker = FakeDocker(loaded="menu-capture-hub:latest")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})

    with pytest.raises(UpdateError, match="归档未包含"):
        updater.apply(archive_request(tmp_path))
    assert docker.switched == []


def test_rollback_uses_previous_version(tmp_path: Path) -> None:
    docker = FakeDocker(current="menu-capture-hub:0.2.4")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})
    updater.journal_path.parent.mkdir(parents=True, exist_ok=True)
    hub_updater._write_json(updater.journal_path, {
        "status": "completed", "target_version": "0.2.4", "previous_version": "0.2.3", "pid": 0,
        "schema_before": updater._schema_snapshot(),
    })

    status = updater.rollback({"version": "0.2.3", "confirm": "0.2.3"})

    assert status["status"] == "rolled_back"
    assert docker.switched == ["menu-capture-hub:0.2.3"]


def test_rollback_health_failure_restores_running_version(tmp_path: Path) -> None:
    """回滚目标起不来时必须切回原版本，不能让采集服务停在停机状态。"""
    docker = FakeDocker(current="menu-capture-hub:0.2.4")

    def healthy(version: str) -> dict:
        if version == "0.2.3":
            return {"ok": False, "version": "", "detail": "容器启动失败"}
        return {"ok": True, "version": version}

    updater = make_updater(tmp_path, docker, healthy=healthy)
    updater.journal_path.parent.mkdir(parents=True, exist_ok=True)
    hub_updater._write_json(updater.journal_path, {
        "status": "completed", "target_version": "0.2.4", "previous_version": "0.2.3", "pid": 0,
        "schema_before": updater._schema_snapshot(),
    })

    with pytest.raises(UpdateError, match="恢复运行 0.2.4"):
        updater.rollback({"version": "0.2.3", "confirm": "0.2.3"})

    assert docker.switched == ["menu-capture-hub:0.2.3", "menu-capture-hub:0.2.4"]
    assert updater.journal()["status"] == "failed"
    assert "0.2.4" in updater.journal()["message"]


def test_rollback_rejects_unrecorded_version(tmp_path: Path) -> None:
    updater = make_updater(tmp_path, FakeDocker(), healthy=lambda version: {"ok": True, "version": version})

    with pytest.raises(UpdateError, match="没有可回滚"):
        updater.rollback({"version": "0.2.0", "confirm": "0.2.0"})


def test_apply_auto_rollback_allows_new_table_migration(tmp_path: Path) -> None:
    database = create_hub_db(tmp_path)
    docker = FakeDocker()
    migrated = False

    def healthy(version: str) -> dict:
        nonlocal migrated
        if version == "0.2.4":
            if not migrated:
                with sqlite3.connect(database) as db:
                    db.execute("CREATE TABLE material_batches (id TEXT PRIMARY KEY)")
                migrated = True
            return {"ok": False, "version": "", "detail": "candidate health failed"}
        return {"ok": True, "version": version}

    updater = make_updater(tmp_path, docker, healthy=healthy)

    status = updater.apply(archive_request(tmp_path))

    assert status["status"] == "rolled_back"
    assert docker.switched == ["menu-capture-hub:0.2.4", "menu-capture-hub:0.2.3"]


def test_apply_auto_rollback_stops_after_new_column_with_default(tmp_path: Path) -> None:
    database = create_hub_db(tmp_path)
    docker = FakeDocker()
    migrated = False

    def healthy(version: str) -> dict:
        nonlocal migrated
        if version == "0.2.4":
            if not migrated:
                with sqlite3.connect(database) as db:
                    db.execute("ALTER TABLE tasks ADD COLUMN note TEXT NOT NULL DEFAULT ''")
                migrated = True
            return {"ok": False, "version": "", "detail": "candidate health failed"}
        return {"ok": True, "version": version}

    updater = make_updater(tmp_path, docker, healthy=healthy)

    with pytest.raises(UpdateError, match="新增列 tasks.note 的旧版本写入兼容性无法证明"):
        updater.apply(archive_request(tmp_path))

    assert docker.switched == ["menu-capture-hub:0.2.4"]
    assert updater.journal()["status"] == "needs_manual_recovery"


def test_apply_auto_rollback_stops_after_destructive_schema_change(tmp_path: Path) -> None:
    database = create_hub_db(tmp_path)
    docker = FakeDocker()
    migrated = False

    def healthy(version: str) -> dict:
        nonlocal migrated
        if version == "0.2.4":
            if not migrated:
                with sqlite3.connect(database) as db:
                    db.execute("DROP TABLE tasks")
                migrated = True
            return {"ok": False, "version": "", "detail": "candidate health failed"}
        return {"ok": True, "version": version}

    updater = make_updater(tmp_path, docker, healthy=healthy)

    with pytest.raises(UpdateError, match="已停止自动回退"):
        updater.apply(archive_request(tmp_path))

    assert docker.switched == ["menu-capture-hub:0.2.4"]
    assert updater.journal()["status"] == "needs_manual_recovery"


def test_explicit_rollback_allows_new_table_migration(tmp_path: Path) -> None:
    database = create_hub_db(tmp_path)
    docker = FakeDocker(current="menu-capture-hub:0.2.4")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})
    schema_before = updater._schema_snapshot()
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE material_batches (id TEXT PRIMARY KEY)")
    hub_updater._write_json(updater.journal_path, {
        "status": "completed", "target_version": "0.2.4", "previous_version": "0.2.3", "pid": 0,
        "schema_before": schema_before,
    })

    status = updater.rollback({"version": "0.2.3", "confirm": "0.2.3"})

    assert status["status"] == "rolled_back"
    assert docker.switched == ["menu-capture-hub:0.2.3"]


def test_explicit_rollback_blocks_new_column_with_default(tmp_path: Path) -> None:
    database = create_hub_db(tmp_path)
    docker = FakeDocker(current="menu-capture-hub:0.2.4")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})
    schema_before = updater._schema_snapshot()
    with sqlite3.connect(database) as db:
        db.execute("ALTER TABLE tasks ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    hub_updater._write_json(updater.journal_path, {
        "status": "completed", "target_version": "0.2.4", "previous_version": "0.2.3", "pid": 0,
        "schema_before": schema_before,
    })

    with pytest.raises(UpdateError, match="新增列 tasks.note 的旧版本写入兼容性无法证明"):
        updater.rollback({"version": "0.2.3", "confirm": "0.2.3"})

    assert docker.switched == []
    assert updater.journal()["status"] == "needs_manual_recovery"


def test_explicit_rollback_blocks_incompatible_schema_change(tmp_path: Path) -> None:
    database = create_hub_db(tmp_path)
    docker = FakeDocker(current="menu-capture-hub:0.2.4")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})
    schema_before = updater._schema_snapshot()
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE tasks")
    hub_updater._write_json(updater.journal_path, {
        "status": "completed", "target_version": "0.2.4", "previous_version": "0.2.3", "pid": 0,
        "schema_before": schema_before,
    })

    with pytest.raises(UpdateError, match="已停止自动回退"):
        updater.rollback({"version": "0.2.3", "confirm": "0.2.3"})

    assert docker.switched == []
    assert updater.journal()["status"] == "needs_manual_recovery"


def test_schema_guard_blocks_new_unique_index_on_existing_table(tmp_path: Path) -> None:
    database = create_hub_db(tmp_path)
    updater = make_updater(tmp_path, FakeDocker(), healthy=lambda version: {"ok": True, "version": version})
    before = updater._schema_snapshot()
    with sqlite3.connect(database) as db:
        db.execute("CREATE UNIQUE INDEX tasks_status_unique ON tasks(status)")

    compatible, reason = updater._schema_compatibility(before, updater._schema_snapshot())

    assert not compatible
    assert "唯一索引" in reason


def test_schema_probe_reads_real_temporary_sqlite_schema(tmp_path: Path) -> None:
    database = create_hub_db(tmp_path)
    before = database.read_bytes()
    completed = subprocess.run(
        [sys.executable, "-c", hub_updater.SCHEMA_PROBE, str(database.parent.parent)],
        check=True, capture_output=True, text=True, timeout=5,
    )
    payload = json.loads(completed.stdout)
    assert payload["exists"]
    assert {table["name"] for table in payload["tables"]} == {"tasks", "events"}
    assert {(row[0], row[1]) for row in payload["objects"]} == {
        ("trigger", "tasks_noop"), ("view", "task_status"),
    }
    assert database.read_bytes() == before


def test_schema_probe_recovers_wal_database_when_shm_locked(tmp_path: Path) -> None:
    """只读挂载卷上的 WAL 库无法建立 -shm 锁，首次读表即 CANTOPEN。

    探针必须回退到复制副本读取，而不是把系统性失败误判为“架构不兼容”。
    """
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("需要非 root 的 POSIX 环境才能模拟 -shm 不可读")
    database = create_hub_db(tmp_path)
    holder = sqlite3.connect(database)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("CREATE TABLE wal_only (id TEXT PRIMARY KEY)")
        holder.commit()
        shm = Path(str(database) + "-shm")
        assert shm.is_file()
        before = database.read_bytes()
        shm.chmod(0o000)
        completed = subprocess.run(
            [sys.executable, "-c", hub_updater.SCHEMA_PROBE, str(database.parent.parent)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        payload = json.loads(completed.stdout)
        assert payload["exists"]
        assert "wal_only" in {table["name"] for table in payload["tables"]}
        assert database.read_bytes() == before
    finally:
        Path(str(database) + "-shm").chmod(0o644)
        holder.close()


def test_schema_guard_blocks_new_constraint_on_existing_table(tmp_path: Path) -> None:
    create_hub_db(tmp_path)
    updater = make_updater(tmp_path, FakeDocker(), healthy=lambda version: {"ok": True, "version": version})
    before = updater._schema_snapshot()
    after = json.loads(json.dumps(before))
    after["tables"]["tasks"]["constraints"].append("CHECK (status <> 'invalid')")

    compatible, reason = updater._schema_compatibility(before, after)

    assert not compatible
    assert "约束发生变化" in reason


def test_schema_guard_blocks_new_foreign_key_on_existing_table(tmp_path: Path) -> None:
    database = create_hub_db(tmp_path)
    updater = make_updater(tmp_path, FakeDocker(), healthy=lambda version: {"ok": True, "version": version})
    before = updater._schema_snapshot()
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE statuses (code TEXT PRIMARY KEY);
            CREATE TABLE events_new (task_id TEXT REFERENCES tasks(id), status TEXT REFERENCES statuses(code));
            INSERT INTO events_new SELECT task_id, NULL FROM events;
            DROP TABLE events;
            ALTER TABLE events_new RENAME TO events;
        """)

    compatible, reason = updater._schema_compatibility(before, updater._schema_snapshot())

    assert not compatible
    assert "外键发生变化" in reason


def test_schema_sql_normalization_preserves_quoted_whitespace() -> None:
    before = "CREATE TABLE t (value TEXT DEFAULT 'two  spaces')"
    same = "CREATE  TABLE t (value TEXT DEFAULT 'two  spaces')"
    changed = "CREATE TABLE t (value TEXT DEFAULT 'two spaces')"

    assert hub_updater._table_definitions(before)[0] == hub_updater._table_definitions(same)[0]
    assert hub_updater._table_definitions(before)[0] != hub_updater._table_definitions(changed)[0]


def test_active_transaction_blocks_second_apply(tmp_path: Path) -> None:
    updater = make_updater(tmp_path, FakeDocker(), healthy=lambda version: {"ok": True, "version": version})
    updater.journal_path.parent.mkdir(parents=True, exist_ok=True)
    hub_updater._write_json(updater.journal_path, {"status": "switching", "pid": os.getpid()})

    with pytest.raises(UpdateError, match="正在执行"):
        updater.apply(archive_request(tmp_path))


def test_stale_active_journal_is_resolved(tmp_path: Path) -> None:
    docker = FakeDocker(current="menu-capture-hub:0.2.4")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})
    updater.journal_path.parent.mkdir(parents=True, exist_ok=True)
    hub_updater._write_json(updater.journal_path, {
        "status": "switching", "target_version": "0.2.4", "pid": 999999,
    })

    status = updater.status()

    assert status["status"] == "completed"
    assert "0.2.4" in status["message"]


def test_validation_error_is_distinct_from_state_error(tmp_path: Path) -> None:
    """请求格式非法应抛 ValidationError（映射 400），状态冲突只抛 UpdateError（映射 409）。"""
    with pytest.raises(hub_updater.ValidationError):
        validate_payload(
            {"version": "0.2.4", "confirm": "0.2.4", "archive": "../x.tar.gz",
             "sha256": "a", "size": 1},
            make_config(tmp_path),
        )
    docker = FakeDocker(current="menu-capture-hub:0.2.5")
    updater = make_updater(tmp_path, docker, healthy=lambda version: {"ok": True, "version": version})
    with pytest.raises(UpdateError) as excinfo:
        updater.apply(archive_request(tmp_path))
    assert not isinstance(excinfo.value, hub_updater.ValidationError)


@pytest.mark.parametrize("payload", [
    {"version": "0.2", "confirm": "0.2", "sha256": "a" * 64, "size": 1, "archive": "a.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.3", "sha256": "a" * 64, "size": 1, "archive": "a.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 63, "size": 1, "archive": "a.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 0, "archive": "a.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1, "archive": "a.tar.gz", "url": "https://github.com/x"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1, "archive": "a.tar.gz", "extra": 1},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1,
     "url": "http://github.com/releases/download/v0.2.4/hub-image-0.2.4.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1,
     "url": "https://evil.example.com/releases/download/v0.2.4/hub-image-0.2.4.tar.gz"},
    {"version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 1,
     "url": "https://github.com/owner/repo/archive/main.zip"},
])
def test_validate_payload_rejects_invalid(tmp_path: Path, payload: dict) -> None:
    with pytest.raises(UpdateError):
        validate_payload(payload, make_config(tmp_path))


def test_validate_accepts_release_url(tmp_path: Path) -> None:
    payload = {
        "version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 10,
        "url": "https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/releases/download/v0.2.4/hub-image-0.2.4.tar.gz",
    }
    assert validate_payload(payload, make_config(tmp_path))["version"] == "0.2.4"


def test_validate_rejects_archive_traversal(tmp_path: Path) -> None:
    payload = {
        "version": "0.2.4", "confirm": "0.2.4", "sha256": "a" * 64, "size": 10,
        "archive": "../hub-image-0.2.4.tar.gz",
    }
    with pytest.raises(UpdateError, match="文件名"):
        validate_payload(payload, make_config(tmp_path))


def test_client_allowlist() -> None:
    assert _client_allowed("127.0.0.1", ["127.0.0.1/32"])
    assert _client_allowed("172.18.0.1", ["172.16.0.0/12"])
    assert not _client_allowed("10.0.0.5", ["127.0.0.1/32"])
    assert not _client_allowed("not-an-ip", ["0.0.0.0/0"])


def updater_journal(tmp_path: Path) -> dict:
    import json

    return json.loads((tmp_path / "state" / "hub-update.json").read_text(encoding="utf-8"))


# ---------- OCI 分层交付 ----------

def oci_request(tmp_path: Path, **overrides: object) -> dict:
    payload = archive_request(tmp_path)
    payload.update({"image": REGISTRY, "digest": DIGEST})
    payload.update(overrides)
    return payload


def healthy(version: str) -> dict:
    return {"ok": True, "version": version}


def test_status_declares_oci_capability_only_when_configured(tmp_path: Path) -> None:
    plain = make_updater(tmp_path, FakeDocker(), healthy=healthy)
    assert plain.status()["capabilities"] == {"oci": False, "oci_only": False}
    enabled = make_updater(tmp_path, FakeDocker(), healthy=healthy, registry=REGISTRY)
    assert enabled.status()["capabilities"] == {"oci": True, "oci_only": False}
    strict = make_updater(tmp_path, FakeDocker(), healthy=healthy, registry=REGISTRY, require_oci=True)
    assert strict.status()["capabilities"] == {"oci": True, "oci_only": True}


def test_apply_oci_pulls_digest_and_never_loads_archive(tmp_path: Path) -> None:
    docker = FakeDocker()
    updater = make_updater(tmp_path, docker, healthy=healthy, registry=REGISTRY, require_oci=True)

    result = updater.apply(oci_request(tmp_path))

    assert result["status"] == "completed"
    assert docker.pulled == f"{REGISTRY}@{DIGEST}"
    assert docker.tags == [(f"{REGISTRY}@{DIGEST}", "menu-capture-hub:0.2.4")]
    assert docker.loads == []
    assert updater.journal()["delivery"] == "oci"


def test_apply_oci_uses_docker_native_commands_in_order(tmp_path: Path) -> None:
    """pull → image inspect → tag → compose up，且参数固定为 digest/linux amd64。"""
    docker = FakeDocker()
    updater = make_updater(tmp_path, docker, healthy=healthy, registry=REGISTRY)

    updater.apply(oci_request(tmp_path))

    pull = next(call for call in docker.calls if call[:2] == ["docker", "pull"])
    inspect = next(call for call in docker.calls if call[:4] == ["docker", "image", "inspect", "--format"])
    tag = next(call for call in docker.calls if call[:2] == ["docker", "tag"])
    assert pull == ["docker", "pull", "--platform", "linux/amd64", f"{REGISTRY}@{DIGEST}"]
    assert inspect == ["docker", "image", "inspect", "--format", "{{json .}}", f"{REGISTRY}@{DIGEST}"]
    assert tag == ["docker", "tag", f"{REGISTRY}@{DIGEST}", "menu-capture-hub:0.2.4"]
    assert docker.calls.index(pull) < docker.calls.index(inspect) < docker.calls.index(tag)

    args, env = docker.compose_calls[0]
    assert args == [
        "docker", "compose", "-f", str(updater.config.compose_file), "-p", "menu-capture",
        "up", "-d", "--no-build", "--pull", "never",
        "--wait", "--wait-timeout", "180", "hub",
    ]
    assert env["MENU_CAPTURE_IMAGE"] == "menu-capture-hub:0.2.4"


def test_apply_oci_falls_back_to_polling_without_compose_wait(tmp_path: Path) -> None:
    """Compose 不支持 --wait 时降级为应用层健康轮询，命令不得携带 --wait。"""
    docker = FakeDocker()
    docker.compose_version = "Docker Compose version v2.5.0"
    updater = make_updater(tmp_path, docker, healthy=healthy, registry=REGISTRY)

    result = updater.apply(oci_request(tmp_path))

    assert result["status"] == "completed"
    args, _ = docker.compose_calls[0]
    assert "--wait" not in args
    assert "--wait-timeout" not in args
    audit = (tmp_path / "state" / "hub-update-audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "compose"' in audit
    assert '"result": "unsupported"' in audit


def test_apply_oci_pull_failure_falls_back_to_archive(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.fail_pull = True
    updater = make_updater(tmp_path, docker, healthy=healthy, registry=REGISTRY)

    result = updater.apply(oci_request(tmp_path))

    assert result["status"] == "completed"
    assert docker.tags == []
    assert docker.loads and docker.loads[0].endswith("hub-image-0.2.4.tar.gz")
    assert updater.journal()["delivery"] == "archive"
    audit = (tmp_path / "state" / "hub-update-audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "pull"' in audit
    assert '"result": "failed"' in audit


def test_require_oci_rejects_archive_before_taking_action(tmp_path: Path) -> None:
    docker = FakeDocker()
    updater = make_updater(tmp_path, docker, healthy=healthy, registry=REGISTRY, require_oci=True)
    with pytest.raises(UpdateError, match="仅允许 OCI 分层更新"):
        updater.apply(archive_request(tmp_path))
    assert docker.calls == []
    assert updater.journal() == {}


def test_require_oci_pull_failure_does_not_fall_back_or_switch(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.fail_pull = True
    updater = make_updater(tmp_path, docker, healthy=healthy, registry=REGISTRY, require_oci=True)

    with pytest.raises(UpdateError, match="已禁止下载或加载完整归档"):
        updater.apply(oci_request(tmp_path))

    assert updater.journal()["status"] == "failed"
    assert updater.journal()["delivery"] == "oci"
    assert ["docker", "pull", "--platform", "linux/amd64", f"{REGISTRY}@{DIGEST}"] in docker.calls
    assert docker.loads == []
    assert docker.switched == []
    assert docker.current == "menu-capture-hub:0.2.3"


def test_require_oci_needs_explicit_registry(tmp_path: Path) -> None:
    with pytest.raises(UpdateError, match="必须配置 HUB_UPDATE_REGISTRY_REPO"):
        make_config(tmp_path, require_oci=True).validate()


def test_apply_oci_digest_mismatch_falls_back_to_archive(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.repo_digests = [f"{REGISTRY}@sha256:{'b' * 64}"]
    updater = make_updater(tmp_path, docker, healthy=healthy, registry=REGISTRY)

    result = updater.apply(oci_request(tmp_path))

    assert result["status"] == "completed"
    assert docker.tags == []
    assert updater.journal()["delivery"] == "archive"


def test_apply_oci_platform_mismatch_falls_back_to_archive(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.platform = "linux/arm64"
    updater = make_updater(tmp_path, docker, healthy=healthy, registry=REGISTRY)

    result = updater.apply(oci_request(tmp_path))

    assert result["status"] == "completed"
    assert docker.tags == []
    assert updater.journal()["delivery"] == "archive"


def test_validate_accepts_oci_fields_when_enabled(tmp_path: Path) -> None:
    parsed = validate_payload(oci_request(tmp_path), make_config(tmp_path, REGISTRY))

    assert parsed["image"] == REGISTRY
    assert parsed["digest"] == DIGEST


def test_validate_rejects_oci_when_executor_disabled(tmp_path: Path) -> None:
    with pytest.raises(UpdateError, match="未启用"):
        validate_payload(oci_request(tmp_path), make_config(tmp_path))


def test_validate_requires_image_and_digest_together(tmp_path: Path) -> None:
    payload = archive_request(tmp_path)
    payload["image"] = REGISTRY

    with pytest.raises(UpdateError, match="同时提供"):
        validate_payload(payload, make_config(tmp_path, REGISTRY))


@pytest.mark.parametrize("image,digest,message", [
    (REGISTRY, "sha256:" + "a" * 63, "digest"),
    ("ghcr.io/other/repo", DIGEST, "允许清单"),
    ("docker.io/owner/repo", DIGEST, "允许清单"),
])
def test_validate_rejects_untrusted_oci(tmp_path: Path, image: str, digest: str, message: str) -> None:
    payload = oci_request(tmp_path, image=image, digest=digest)

    with pytest.raises(UpdateError, match=message):
        validate_payload(payload, make_config(tmp_path, REGISTRY))


@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5"])
def test_wait_timeout_must_be_positive_integer(tmp_path: Path, value: str) -> None:
    with pytest.raises(UpdateError, match="HUB_UPDATE_WAIT_TIMEOUT"):
        Config({"HUB_UPDATE_TOKEN": TOKEN, "HUB_UPDATE_WAIT_TIMEOUT": value})


def test_wait_timeout_defaults_to_180(tmp_path: Path) -> None:
    assert Config({"HUB_UPDATE_TOKEN": TOKEN}).wait_timeout == 180


@pytest.mark.parametrize("wait_result,last_error,expected", [
    (258, 0, True), (0, 0, False), (0xffffffff, 0, True), (258, 87, False), (258, 5, True),
])
def test_windows_pid_liveness_uses_read_only_native_query(monkeypatch, wait_result, last_error, expected):
    import ctypes
    from types import SimpleNamespace

    closed = []

    def open_process(access, inherit, pid):
        assert (access, inherit, pid) == (0x00100000, False, 1234)
        return 44 if last_error == 0 else 0

    kernel32 = SimpleNamespace(
        OpenProcess=open_process,
        WaitForSingleObject=lambda handle, timeout: wait_result,
        CloseHandle=lambda handle: closed.append(handle) or True,
    )
    monkeypatch.setattr(hub_updater, "os", SimpleNamespace(
        name="nt", kill=lambda *_: pytest.fail("os.kill must not be called"),
    ))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    assert hub_updater._pid_alive(1234) is expected
    assert closed == ([44] if last_error == 0 else [])
