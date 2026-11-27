"""旧版 SQLite 数据库前向迁移兼容性契约测试。

防止新增列/索引顺序错误（如 PR #149 在旧库补列前建索引导致 Hub 启动崩溃）
以及只读卷 WAL 探针失败（PR #127）再次复发。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from menu_capture.core.config import ConfigStore
from menu_capture.materials.materials import MaterialService
from menu_capture.shared.shared import SharedHub
from menu_capture.storage.store import TaskStore


def test_shared_hub_migrates_legacy_shared_assignments_table_before_indexing(tmp_path: Path) -> None:
    """模拟 v0.4.x 旧库（shared_assignments 缺少 project_id/project_code/project_name/store_id 列），
    验证初始化 SharedHub 时先补列再建索引且保留既有记录。"""
    db_path = tmp_path / "data" / "tasks.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE shared_workers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                last_seen TEXT NOT NULL DEFAULT '',
                pending_uploads INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE shared_assignments (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                batch_name TEXT NOT NULL,
                store TEXT NOT NULL,
                revision TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                source_suffix TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO shared_workers VALUES('w1', '旧机A', 'hash1', '2026-10-01T00:00:00Z', 0);
            INSERT INTO shared_assignments VALUES(
                'a1', 't1', 'w1', 'b1', '旧批次', '旧门店', 'v1.0.0', 'snap1', 'h1', '.png', 'completed', '2026-10-01T00:00:00Z'
            );
            """
        )

    store = TaskStore(db_path)
    configs = ConfigStore(tmp_path / "data" / "config.json")
    materials = MaterialService(tmp_path, configs, store, role="hub")
    hub = SharedHub(store, materials)

    with store.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(shared_assignments)")}
        assert {"project_id", "project_code", "project_name", "store_id"}.issubset(cols)
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(shared_assignments)")}
        assert "shared_assignments_project_status" in indexes
        row = conn.execute("SELECT id, store, status FROM shared_assignments WHERE id='a1'").fetchone()
        assert dict(row) == {"id": "a1", "store": "旧门店", "status": "completed"}

    # 确认 status() 读写链路正常且迁移审计表已登记
    status = hub.status(include_history=False)
    assert status["role"] == "hub"
    assert len(status["workers"]) == 1

    from menu_capture.storage.migrations import applied_versions

    with store.connect() as conn:
        versions = applied_versions(conn)
    assert "20261128_01_shared_assignments_project_cols_and_index" in versions

    # 再次初始化必须幂等，不重复报错或重复写审计记录
    SharedHub(store, materials)
    with store.connect() as conn:
        assert applied_versions(conn) == versions
