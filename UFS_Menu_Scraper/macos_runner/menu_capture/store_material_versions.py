"""批次保持不可变发布快照，采集身份和版本跟随单个门店。"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

from PIL import Image

from .store import TaskStore, now


def pixel_digest(path: Path) -> str:
    """以实际下发图的尺寸和解码颜色为准，忽略 PNG 元数据及压缩参数。"""
    with Image.open(path) as original:
        image = original.convert("RGBA")
        digest = hashlib.sha256(struct.pack(">II", *image.size))
        digest.update(image.tobytes())
    return digest.hexdigest()


class StoreMaterialVersions:
    def __init__(self, tasks: TaskStore, root: Path):
        self.tasks = tasks
        self.root = root
        with tasks.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS material_store_publications (
                    release_id TEXT NOT NULL, batch_id TEXT NOT NULL, store_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, pixel_hash TEXT NOT NULL,
                    PRIMARY KEY(release_id,store_id)
                );
                CREATE INDEX IF NOT EXISTS store_publication_history
                    ON material_store_publications(batch_id,store_id,revision DESC);
                CREATE TABLE IF NOT EXISTS material_store_returns (
                    id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, store_id TEXT NOT NULL,
                    returned_at TEXT NOT NULL, released_at TEXT NOT NULL DEFAULT '',
                    past_decision TEXT NOT NULL, past_task TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS store_unpublished_return
                    ON material_store_returns(batch_id,store_id) WHERE released_at='';
            """)

    def current(self, releases: set[str]) -> tuple[dict[str, list[dict]], set[tuple[str, str]]]:
        indexed: dict[str, list[dict]] = {}
        with self.tasks.connect() as db:
            for row in db.execute("SELECT * FROM material_store_publications"):
                if row["release_id"] in releases:
                    indexed.setdefault(row["release_id"], []).append(dict(row))
            returns = {(row["batch_id"], row["store_id"]) for row in db.execute(
                "SELECT batch_id,store_id FROM material_store_returns WHERE released_at=''")}
        return indexed, returns

    def state(self, batch_id: str, store_id: str) -> dict:
        with self.tasks.connect() as db:
            row = db.execute("""
                SELECT p.revision FROM material_store_publications p
                JOIN material_versions v ON v.id=p.release_id
                JOIN material_batches b ON b.id=v.batch_id AND b.snapshot_id=v.snapshot_id
                WHERE p.batch_id=? AND p.store_id=? ORDER BY v.rowid DESC LIMIT 1
            """, (batch_id, store_id)).fetchone()
            blocked = db.execute("""SELECT 1 FROM material_store_returns
                WHERE batch_id=? AND store_id=? AND released_at=''""", (batch_id, store_id)).fetchone()
        return {"material_version": row["revision"] if row else None, "awaiting_publish": bool(blocked)}

    def return_store(self, db, decision: dict, task_ids: list[str]) -> None:
        batch_id, store_id = decision["batch_id"], decision["store_id"]
        previous = []
        for task_id in task_ids:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is not None:
                previous.append(dict(row))
        db.execute("""
            INSERT OR IGNORE INTO material_store_returns(batch_id,store_id,returned_at,past_decision,past_task)
            VALUES(?,?,?,?,?)
        """, (batch_id, store_id, now(), json.dumps(decision, ensure_ascii=False),
              json.dumps(previous, ensure_ascii=False)))
        db.executemany("""
            UPDATE tasks SET status='blocked',stage='returned_to_materials',message='待重新选码并发布',
                reject_reason='',reject_detail='',rejected_at='',reject_evidence='',reject_reviewed_at='',error_code=''
            WHERE id=?
        """, ((task_id,) for task_id in task_ids))

    def prepare(self, batch_id: str, snapshot: Path, entries: list[dict],
                previous_release: dict | None) -> list[dict]:
        prior_entries = {}
        previous_root = None
        if previous_release:
            previous_root = self.root / previous_release["snapshot_id"]
            marker = previous_root / ".materials-snapshot.json"
            if marker.is_file():
                prior_entries = {row["store_id"]: row for row in json.loads(marker.read_text(encoding="utf-8"))["stores"]}
        with self.tasks.connect() as db:
            previous = {}
            for row in db.execute("""SELECT store_id,task_id,revision,pixel_hash FROM material_store_publications
                WHERE batch_id=? ORDER BY revision,rowid""", (batch_id,)):
                previous[row["store_id"]] = dict(row)
            result = []
            for entry in entries:
                store_id = entry["store_id"]
                relative = Path(entry["source_name"]) / "二维码.png"
                artifact = snapshot / relative
                digest = pixel_digest(artifact)
                old = previous.get(store_id)
                if old is None and previous_root and store_id in prior_entries:
                    prior_artifact = previous_root / prior_entries[store_id]["source_name"] / "二维码.png"
                    if prior_artifact.is_file():
                        row = db.execute("SELECT id FROM tasks WHERE source_path=? ORDER BY rowid DESC LIMIT 1",
                                         (str(prior_artifact),)).fetchone()
                        if row is not None:
                            old = {"task_id": row["id"], "pixel_hash": pixel_digest(prior_artifact), "revision": 1}
                changed = old is None or old["pixel_hash"] != digest
                task = db.execute("SELECT id FROM tasks WHERE source_path=? ORDER BY rowid DESC LIMIT 1",
                                  (str(artifact),)).fetchone()
                if task is None:
                    raise ValueError(f"找不到已发布二维码对应的采集记录：{entry['source_name']}")
                # 已回到历史图片时仍创建独立的有效采集身份，避免恢复历史审核。
                task_id = task["id"] if changed else old["task_id"]
                result.append({"store_id": store_id, "store_name": entry["source_name"], "task_id": task_id,
                               "revision": (old["revision"] + int(changed)) if old else 1,
                               "pixel_hash": digest, "changed": changed})
        return result

    def remember(self, db, batch_id: str, release_id: str, records: list[dict], confirmed: set[str]) -> list[dict]:
        result = []
        for row in records:
            db.execute("""
                INSERT OR IGNORE INTO material_store_publications
                    (release_id,batch_id,store_id,task_id,revision,pixel_hash) VALUES(?,?,?,?,?,?)
            """, (release_id, batch_id, row["store_id"], row["task_id"], row["revision"], row["pixel_hash"]))
            released = False
            if row["store_id"] in confirmed:
                released = bool(db.execute("""
                    UPDATE material_store_returns SET released_at=?
                    WHERE batch_id=? AND store_id=? AND released_at=''
                """, (now(), batch_id, row["store_id"])).rowcount)
            if released or row["changed"]:
                # 只切换本店当前采集资格，历史轮次与已提交证据保留。
                db.execute("""
                    UPDATE tasks SET status='pending',stage='queued',message='',output_dir='',
                        reject_reason='',reject_detail='',rejected_at='',reject_evidence='',
                        reject_reviewed_at='',error_code='',inherited_from=''
                    WHERE id=?
                """, (row["task_id"],))
            if row["store_id"] in confirmed:
                result.append({"store_id": row["store_id"], "store_name": row["store_name"],
                               "material_version": row["revision"], "changed": row["changed"],
                               "unblocked": released})
        return result
