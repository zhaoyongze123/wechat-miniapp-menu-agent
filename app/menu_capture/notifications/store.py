"""告警通知投递审计与去重/限频持久化存储（基于既有 SQLite 顺序迁移框架）。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from menu_capture.notifications.models import AlertEvent, utc_now_iso
from menu_capture.storage.migrations import Migration, apply_migrations


def _migrate_notification_deliveries(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            channel TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            device_id TEXT NOT NULL DEFAULT '',
            device_name TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notification_deliveries_dedupe
        ON notification_deliveries(channel, dedupe_key, status, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notification_deliveries_sent
        ON notification_deliveries(channel, status, sent_at DESC)
        """
    )


class NotificationStore:
    """持久化邮件等外部渠道的去重、限频与投递状态，服务重启不丢失冷静期状态。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            apply_migrations(
                conn,
                [
                    Migration(
                        version="20261128_02_notification_deliveries",
                        description="新增统一通知中心外部渠道投递记录与去重限频索引",
                        apply=_migrate_notification_deliveries,
                    )
                ],
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def last_sent_time(self, channel: str, dedupe_key: str) -> datetime | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT sent_at FROM notification_deliveries
                WHERE channel=? AND dedupe_key=? AND status='sent' AND sent_at != ''
                ORDER BY sent_at DESC LIMIT 1
                """,
                (channel, dedupe_key),
            ).fetchone()
        if not row or not row["sent_at"]:
            return None
        try:
            parsed = datetime.fromisoformat(str(row["sent_at"]))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def count_sent_since(self, channel: str, since_iso: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT count(*) AS total FROM notification_deliveries
                WHERE channel=? AND status='sent' AND sent_at >= ?
                """,
                (channel, since_iso),
            ).fetchone()
        return int(row["total"]) if row else 0

    def record(
        self,
        event: AlertEvent,
        *,
        channel: str,
        status: str,
        attempts: int = 0,
        error: str = "",
        sent_at: str = "",
    ) -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO notification_deliveries(
                    event_id, dedupe_key, event_type, severity, channel,
                    title, body, device_id, device_name, project_id, run_id,
                    status, attempts, error, created_at, sent_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.dedupe_key,
                    event.event_type,
                    event.severity,
                    channel,
                    event.title,
                    event.body,
                    event.device_id,
                    event.device_name,
                    event.project_id,
                    event.run_id,
                    status,
                    attempts,
                    error[:300],
                    now,
                    sent_at,
                ),
            )
            return int(cur.lastrowid or 0)

    def recent(self, *, channel: str = "email", limit: int = 15) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, event_id, dedupe_key, event_type, severity, channel,
                       title, status, attempts, error, device_name, created_at, sent_at
                FROM notification_deliveries
                WHERE channel=?
                ORDER BY id DESC LIMIT ?
                """,
                (channel, max(1, min(100, limit))),
            ).fetchall()
        return [dict(row) for row in rows]
