"""素材发布版本的元数据。原始清洗决策与不可变发布清单分别保存。"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .store import TaskStore, now


class MaterialVersions:
    def __init__(self, store: TaskStore, published_root: Path):
        self.store, self.published_root = store, published_root
        with store.connect() as db:
            if "name" not in {row["name"] for row in db.execute("PRAGMA table_info(material_batches)")}:
                db.execute("ALTER TABLE material_batches ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            for batch in db.execute("SELECT id,created_at FROM material_batches WHERE name='' ").fetchall():
                # 旧批次保留 ID 和更新时间，只补充日期及短 ID 作为可辨识名称。
                stamp = (batch["created_at"] or "")[:16].replace("T", " ") or "未知日期"
                db.execute("UPDATE material_batches SET name=? WHERE id=? AND name=''",
                           (f"清洗 {stamp} {batch['id'][:6]}", batch["id"]))
            db.executescript("""
                CREATE TABLE IF NOT EXISTS material_versions (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, version TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, description TEXT NOT NULL,
                    published_at TEXT NOT NULL, store_count INTEGER NOT NULL,
                    UNIQUE(batch_id, version)
                );
                CREATE TABLE IF NOT EXISTS material_selection (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), version_id TEXT NOT NULL
                );
            """)
            # 只迁移旧数据库中明确关联到批次的快照，不猜测其他目录的归属。
            for batch in db.execute("SELECT * FROM material_batches WHERE snapshot_id!='' AND deleted_at='' ").fetchall():
                if db.execute("SELECT 1 FROM material_versions WHERE batch_id=?", (batch["id"],)).fetchone():
                    continue
                marker = published_root / batch["snapshot_id"] / ".materials-snapshot.json"
                if not marker.is_file():
                    continue
                manifest = json.loads(marker.read_text(encoding="utf-8"))
                old = db.execute("SELECT * FROM material_releases WHERE snapshot_id=?", (batch["snapshot_id"],)).fetchone()
                db.execute(
                    "INSERT INTO material_versions VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, batch["id"], "历史版本", batch["snapshot_id"],
                     old["description"] if old else "", old["published_at"] if old else batch["updated_at"],
                     len(manifest["stores"])),
                )

    @staticmethod
    def batch_name(batch) -> str:
        return batch["name"] or ("清洗 " + batch["created_at"][:16].replace("T", " "))

    def records(self, batch_id: str, *, include_deleted: bool = False) -> list[dict]:
        deleted_filter = "" if include_deleted else "AND b.deleted_at=''"
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT v.*, b.name, b.created_at FROM material_versions v "
                f"JOIN material_batches b ON b.id=v.batch_id WHERE batch_id=? {deleted_filter} ORDER BY v.rowid DESC", (batch_id,),
            )]

    def active(self, root: Path) -> dict | None:
        root = root.expanduser().resolve()
        if root.parent != self.published_root.resolve():
            return None
        with self.store.connect() as db:
            row = db.execute(
                "SELECT v.*, b.name, b.created_at FROM material_selection s "
                "JOIN material_versions v ON v.id=s.version_id JOIN material_batches b ON b.id=v.batch_id "
                "WHERE s.singleton=1 AND v.snapshot_id=? AND b.deleted_at=''", (root.name,),
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT v.*, b.name, b.created_at FROM material_versions v "
                    "JOIN material_batches b ON b.id=v.batch_id WHERE v.snapshot_id=? AND b.deleted_at='' ORDER BY v.rowid DESC LIMIT 1",
                    (root.name,),
                ).fetchone()
        return self.public(dict(row), root, active_id=row["id"]) if row else None

    def public(self, row: dict, root: Path, active_id: str | None = None) -> dict:
        return {
            "release_id": row["id"], "batch_id": row["batch_id"], "batch_name": self.batch_name(row),
            "snapshot_id": row["snapshot_id"], "version": row["version"], "description": row["description"],
            "published_at": row["published_at"], "store_count": row["store_count"],
            "active": row["id"] == active_id,
        }

    def list(self, batch_id: str, root: Path, *, include_deleted: bool = False) -> list[dict]:
        current = self.active(root)
        return [self.public(row, root, current["release_id"] if current else None)
                for row in self.records(batch_id, include_deleted=include_deleted)]

    def find(self, batch_id: str, version: str) -> dict | None:
        return next((row for row in self.records(batch_id) if row["version"] == version), None)

    def suggested(self, batch_id: str) -> str:
        versions = [tuple(map(int, row["version"][1:].split("."))) for row in self.records(batch_id)
                    if re.fullmatch(r"v\d+\.\d+\.\d+", row["version"])]
        if not versions:
            return "v1.0.0"
        major, minor, patch = max(versions)
        return f"v{major}.{minor}.{patch + 1}"

    def clear_active_selection(self, batch_id: str) -> bool:
        """清除只指向此批次的选用记录；调用方负责先确认运行安全。"""
        with self.store.connect() as db:
            row = db.execute("SELECT s.version_id FROM material_selection s "
                             "JOIN material_versions v ON v.id=s.version_id "
                             "WHERE s.singleton=1 AND v.batch_id=?", (batch_id,)).fetchone()
            if row is None:
                return False
            db.execute("DELETE FROM material_selection WHERE singleton=1 AND version_id=?", (row["version_id"],))
        return True

    @staticmethod
    def validate(name: str, version: str, description: str) -> None:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("请填写批次名称（最多 80 字）")
        if not isinstance(version, str) or not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
            raise ValueError("版本号请使用 v1.0.0 格式")
        if len(version) > 40:
            raise ValueError("版本号过长")
        if not isinstance(description, str) or len(description.strip()) > 200:
            raise ValueError("版本描述最多 200 字")

    @staticmethod
    def remember(db, batch_id: str, name: str, version: str, snapshot_id: str,
                 description: str, count: int, *, activate: bool = True) -> str:
        batch = db.execute("SELECT deleted_at FROM material_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None or batch["deleted_at"]:
            raise ValueError("回收站批次不能发布或选用")
        row = db.execute("SELECT * FROM material_versions WHERE batch_id=? AND version=?", (batch_id, version)).fetchone()
        if row:
            if row["snapshot_id"] != snapshot_id or row["description"] != description:
                raise ValueError("该版本已经发布，请填写新的版本号")
            version_id = row["id"]
        else:
            version_id = uuid.uuid4().hex
            db.execute("INSERT INTO material_versions VALUES(?,?,?,?,?,?,?)",
                       (version_id, batch_id, version, snapshot_id, description, now(), count))
        db.execute("UPDATE material_batches SET name=?,snapshot_id=?,updated_at=? WHERE id=?",
                   (name, snapshot_id, now(), batch_id))
        if activate:
            db.execute("INSERT INTO material_selection VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET version_id=excluded.version_id",
                       (version_id,))
        return version_id

    def changes(self, batch_id: str, snapshot_id: str) -> dict:
        with self.store.connect() as db:
            rows = db.execute("SELECT store_id,selected_source_hash,selected_json FROM material_decisions "
                              "WHERE batch_id=? AND decision='confirmed'", (batch_id,)).fetchall()
        selected = {row["store_id"]: (row["selected_source_hash"], json.loads(row["selected_json"]).get("box"))
                    for row in rows}
        manifest = json.loads((self.published_root / snapshot_id / ".materials-snapshot.json").read_text(encoding="utf-8")) if snapshot_id else {"stores": []}
        previous = {row["store_id"]: (row["source_hash"], row["box"]) for row in manifest["stores"]}
        return {
            "added": len(selected.keys() - previous.keys()), "removed": len(previous.keys() - selected.keys()),
            "changed": sum(selected[key] != previous[key] for key in selected.keys() & previous.keys()),
        }
