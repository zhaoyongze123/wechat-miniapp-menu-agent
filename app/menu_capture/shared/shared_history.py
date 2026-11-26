"""采集轮次历史与分配结果各自同步，旧采集端可继续使用原协议。"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from menu_capture.storage.store import TaskStore

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ShortText = Annotated[str, StringConstraints(max_length=1024)]
Message = Annotated[str, StringConstraints(max_length=2048)]


class RunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: Identifier
    name: ShortText
    mode: ShortText
    capture_mode: ShortText
    status: Identifier
    task_ids: list[Identifier] = Field(max_length=10_000)
    current_index: int = Field(ge=0)
    current_task_id: ShortText
    message: Message
    material_batch_id: ShortText
    created_at: ShortText
    updated_at: ShortText
    # 仅支持项目 scope 的 Hub 才会收到此可选字段；旧 DTO 仍可省略。
    project_id: Identifier | None = None


class StoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: Identifier
    store_name: ShortText
    batch_id: ShortText
    batch_name: ShortText
    version: ShortText
    outcome: ShortText
    reject_reason: ShortText
    message: Message
    # 旧 Hub 不接收此新字段；Worker 仅在能力协商成功后发送。
    project_id: Identifier | None = None


class HistoryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    generation: int = Field(ge=1, le=2**63 - 1)
    run: RunSnapshot
    results: list[StoreResult] = Field(max_length=10_000)

    @model_validator(mode="after")
    def verify_membership(self):
        members = set(self.run.task_ids)
        results = {item.task_id for item in self.results}
        if len(members) != len(self.run.task_ids) or len(results) != len(self.results) or not results <= members:
            raise ValueError("轮次逐店记录与门店集合不一致")
        return self


class WorkerHistoryDelivery:
    """版本号和本机业务改动同事务写入，只有 Hub 确认后才推进已同步版本。"""

    def __init__(self, store: TaskStore):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS worker_run_delivery (
                    run_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 1,
                    acknowledged INTEGER NOT NULL DEFAULT 0, worker_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TRIGGER IF NOT EXISTS queue_capture_run AFTER INSERT ON runs BEGIN
                    INSERT INTO worker_run_delivery(run_id) VALUES (NEW.id);
                END;
                CREATE TRIGGER IF NOT EXISTS queue_capture_run_update AFTER UPDATE ON runs BEGIN
                    INSERT INTO worker_run_delivery(run_id) VALUES (NEW.id)
                    ON CONFLICT(run_id) DO UPDATE SET generation=generation+1;
                END;
                CREATE TRIGGER IF NOT EXISTS queue_capture_members AFTER INSERT ON capture_run_results BEGIN
                    UPDATE worker_run_delivery SET generation=generation+1 WHERE run_id=NEW.run_id;
                END;
                CREATE TRIGGER IF NOT EXISTS queue_capture_result AFTER UPDATE ON capture_run_results BEGIN
                    UPDATE worker_run_delivery SET generation=generation+1 WHERE run_id=NEW.run_id;
                END;
                INSERT OR IGNORE INTO worker_run_delivery(run_id) SELECT id FROM runs;
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(worker_run_delivery)")}
            if "worker_id" not in columns:
                db.execute("ALTER TABLE worker_run_delivery ADD COLUMN worker_id TEXT NOT NULL DEFAULT ''")

    def pending_report(self, *, include_project_scope: bool = False, worker_id: str | None = None) -> dict | None:
        with self.store.connect() as db:
            # 同一读快照冻结序号、轮次和逐店结论，避免同步期间读取到半次修改。
            db.execute("BEGIN")
            if worker_id is None:
                row = db.execute("""
                    SELECT r.*, d.generation FROM worker_run_delivery d JOIN runs r ON r.id=d.run_id
                    WHERE d.generation > d.acknowledged
                    ORDER BY (d.acknowledged <> 0) DESC, r.rowid DESC LIMIT 1
                """).fetchone()
            else:
                row = db.execute("""
                    SELECT r.*, d.generation FROM worker_run_delivery d JOIN runs r ON r.id=d.run_id
                    WHERE d.generation > d.acknowledged AND d.worker_id=?
                    ORDER BY (d.acknowledged <> 0) DESC, r.rowid DESC LIMIT 1
                """, (worker_id,)).fetchone()
            if row is None:
                return None
            result_rows = db.execute("SELECT * FROM capture_run_results WHERE run_id=? ORDER BY rowid",
                                     (row["id"],))
            run = dict(row)
            run["task_ids"] = list(dict.fromkeys(json.loads(run["task_ids"])))
            run["message"] = run["message"][:2048]
            results = []
            for result in result_rows:
                record = {key: result[key] for key in StoreResult.model_fields if key != "project_id"}
                if include_project_scope:
                    record["project_id"] = str(result["project_id"] or "") or None
                record["message"] = record["message"][:2048]
                results.append(record)
        run_payload = {key: run[key] for key in RunSnapshot.model_fields if key != "project_id"}
        if include_project_scope:
            run_payload["project_id"] = str(run["project_id"] or "") or None
        return {"generation": run["generation"], "run": run_payload, "results": results}

    def acknowledge(self, run_id: str, generation: int) -> None:
        with self.store.connect() as db:
            db.execute("UPDATE worker_run_delivery SET acknowledged=MAX(acknowledged,?) WHERE run_id=?",
                       (generation, run_id))
