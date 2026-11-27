"""SQLite 版本化前向顺序迁移基础设施。

职责：
1. 维护统一的 `schema_migrations` 审计表，记录每次结构变更的版本号、说明与应用时间；
2. 保证迁移严格按声明顺序幂等执行，杜绝“索引早于补列创建”类顺序故障（PR #149）；
3. 零外部依赖，兼容既有存量数据库与只读回滚探针。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    apply: Callable[[sqlite3.Connection], None]


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> list[str]:
    ensure_migration_table(conn)
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def apply_migrations(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> list[str]:
    """按声明顺序幂等应用未执行的迁移，返回本次新应用的版本列表。"""
    ensure_migration_table(conn)
    existing = set(applied_versions(conn))
    newly_applied: list[str] = []
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for migration in migrations:
        if migration.version in existing:
            continue
        migration.apply(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, description, applied_at) VALUES(?, ?, ?)",
            (migration.version, migration.description, stamp),
        )
        existing.add(migration.version)
        newly_applied.append(migration.version)
    return newly_applied
