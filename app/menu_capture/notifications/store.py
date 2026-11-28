"""告警通知投递审计与去重/限频持久化存储（基于既有 SQLite 顺序迁移框架）。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from menu_capture.notifications.models import AlertEvent, EmailNotificationConfig, utc_now_iso
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


def _migrate_worker_email_recipients(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_notification_recipients (
            worker_id TEXT PRIMARY KEY,
            recipients_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        )
        """
    )


class NotificationStore:
    """持久化邮件等外部渠道的去重、限频、设备专属收件人与投递状态。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            apply_migrations(
                conn,
                [
                    Migration(
                        version="20261128_02_notification_deliveries",
                        description="新增统一通知中心外部渠道投递记录与去重限频索引",
                        apply=_migrate_notification_deliveries,
                    ),
                    Migration(
                        version="20261128_03_worker_email_recipients",
                        description="新增采集设备独立收件邮箱路由表",
                        apply=_migrate_worker_email_recipients,
                    ),
                ],
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-16384")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")
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

    def recent(self, *, channel: str = "email", limit: int = 15, device_id: str = "") -> list[dict]:
        capped = max(1, min(100, limit))
        with self.connect() as conn:
            if device_id:
                rows = conn.execute(
                    """
                    SELECT id, event_id, dedupe_key, event_type, severity, channel,
                           title, status, attempts, error, device_id, device_name, created_at, sent_at
                    FROM notification_deliveries
                    WHERE channel=? AND device_id=?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (channel, device_id, capped),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, event_id, dedupe_key, event_type, severity, channel,
                           title, status, attempts, error, device_id, device_name, created_at, sent_at
                    FROM notification_deliveries
                    WHERE channel=?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (channel, capped),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_worker_recipients(self, worker_id: str) -> list[str]:
        if not worker_id:
            return []
        with self.connect() as conn:
            row = conn.execute(
                "SELECT recipients_json FROM worker_notification_recipients WHERE worker_id=?",
                (worker_id,),
            ).fetchone()
        if not row or not row["recipients_json"]:
            return []
        try:
            items = json.loads(str(row["recipients_json"]))
            return [str(item) for item in items if isinstance(item, str) and item.strip()] if isinstance(items, list) else []
        except ValueError:
            return []

    def set_worker_recipients(self, worker_id: str, recipients: list[str]) -> list[str]:
        if not worker_id or not worker_id.strip():
            raise ValueError("设备标识不能为空")
        validated = EmailNotificationConfig.model_validate({"recipients": recipients}).recipients
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO worker_notification_recipients(worker_id, recipients_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    recipients_json=excluded.recipients_json,
                    updated_at=excluded.updated_at
                """,
                (worker_id.strip(), json.dumps(validated, ensure_ascii=False), now),
            )
        return validated

    def list_worker_recipients(self) -> dict[str, list[str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT worker_id, recipients_json FROM worker_notification_recipients"
            ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            try:
                items = json.loads(str(row["recipients_json"]))
                result[str(row["worker_id"])] = (
                    [str(item) for item in items if isinstance(item, str) and item.strip()]
                    if isinstance(items, list)
                    else []
                )
            except ValueError:
                result[str(row["worker_id"])] = []
        return result
