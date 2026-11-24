"""从当前生效素材生成待检查清单。"""
from __future__ import annotations

from menu_capture.core.failures import describe as describe_failure
from menu_capture.capture.results import capture_outcome


def review_queue(batch_ids: str, query: str, *, store, materials) -> dict:
    """待检查清单：跨批次读取任务状态，同一门店只保留最新一条。"""
    # 与批次管理、工作台保持同一口径：只取未删除批次的最新发布快照任务，
    # 旧版本遗留的失败任务不进入清单，避免同一门店重复计数或被重跑旧素材。
    current_tasks = {row["id"]: row for row in materials.effective_capture_tasks()
                     if capture_outcome(row) == "review"}
    candidates = current_tasks.values()
    # 待检查入口只展示批次名和阶段。版本差异、素材计数留给批次管理页。
    with store.connect() as db:
        batches = [dict(row) for row in db.execute(
            "SELECT id,name,capture_rounds,manual_stage,approved_at FROM material_batches WHERE deleted_at=''"
        )]
    workflow = {batch["id"]: materials.workflow_stage(batch) for batch in batches}
    names = {batch["id"]: batch["name"] or batch["id"] for batch in batches}
    selected = set()
    if batch_ids and batch_ids != "all":
        selected = {value.strip() for value in batch_ids.split(",") if value.strip()}
    normalized = query.strip().casefold()
    records = []
    for task in candidates:
        batch_id = current_tasks[task["id"]]["batch_id"]
        if selected and batch_id not in selected:
            continue
        if normalized and normalized not in (task.get("source_name") or "").casefold():
            continue
        failure = describe_failure(task.get("error_code", ""), task.get("stage", ""),
                                   task.get("message", ""))
        records.append({
            "task_id": task["id"],
            "store_name": task["source_name"],
            "status": task["status"],
            "stage": task["stage"],
            "message": task["message"],
            "error_code": task.get("error_code", ""),
            "group": failure["group"],
            "label": failure["label"],
            "bucket": failure["bucket"],
            "bucket_label": failure["bucket_label"],
            "source_path": task["source_path"],
            "output_dir": task["output_dir"],
            "reject_reason": task.get("reject_reason", ""),
            "reject_detail": task.get("reject_detail", ""),
            "updated_at": task.get("updated_at", ""),
            "batch_id": batch_id,
            "batch_name": task.get("material_batch_name") or names.get(batch_id, ""),
            "workflow": workflow.get(batch_id, {}),
        })
    # 有效任务清单已按原批次和门店归属取最新版，保留显示编号相近的各条任务。
    records.sort(key=lambda row: (row["updated_at"], row["store_name"]), reverse=True)
    options = {}
    for record in records:
        if record["batch_id"]:
            options[record["batch_id"]] = {
                "id": record["batch_id"],
                "name": record["batch_name"] or record["batch_id"],
                "workflow": record["workflow"],
            }
    return {
        "records": records,
        "batches": sorted(options.values(), key=lambda item: (item["name"], item["id"])),
        "summary": {
            "stores": len(records),
            "batches": len(options),
            "needs_review": sum(row["status"] == "needs_review" for row in records),
            # 清单里的门店都已落定，可以直接批量重跑；单独保留该字段方便前端展示。
            "retryable": len(records),
        },
    }
