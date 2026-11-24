"""统一格式化采集轮次，供本机历史和 Hub 同步记录使用。"""

from collections import Counter
from datetime import datetime, timezone

from .capture_results import CAPTURE_RESULT_LABELS

ACTIVE_RUN_STATUSES = {
    "queued", "running", "pause_requested", "paused", "interrupted", "blocked", "resuming", "terminating",
}


def history_timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def capture_run_title(run: dict, total: int, batch_name: str) -> str:
    """任务名以显式保存值为准；旧占位名按可信模式和数量生成，避免历史记录都叫 automatic。"""
    saved = (run.get("name") or "").strip()
    if saved and saved != "automatic":
        return saved
    if batch_name:
        return batch_name
    label = {"auto": "全自动采集", "manual": "半自动采集"}.get(run.get("capture_mode") or "", "批量采集")
    return f"{label} · {total} 家" if total else label

def capture_history_row(run: dict, recorded: list[dict], task_index: dict) -> dict:
    """旧轮次保留门店明细和批次信息，轮次结果仅采用当时留存的记录。"""
    batch_id = run.get("material_batch_id") or ""
    index = {row["task_id"]: row for row in recorded}
    outcomes = Counter(row["outcome"] for row in recorded)
    batches = {}
    entries = []
    ids = run.get("task_ids") or []
    processed = max(sum(outcomes[key] for key in CAPTURE_RESULT_LABELS),
                    min(len(ids), max(0, int(run.get("current_index") or 0))))
    for task_id in ids:
        result = index.get(task_id, {})
        task = task_index.get(task_id, {})
        source_batch = result.get("batch_id") or task.get("batch_id") or batch_id
        name = result.get("batch_name") or task.get("batch_name") or source_batch
        version = result.get("version") or task.get("version") or ""
        if source_batch:
            batch = batches.setdefault(source_batch, {"id": source_batch, "name": name, "versions": [], "stores": 0})
            batch["stores"] += 1
            if version and version not in batch["versions"]:
                batch["versions"].append(version)
        outcome = result.get("outcome", "")
        label = CAPTURE_RESULT_LABELS.get(outcome, "未记录结果" if not result else "未处理")
        if result and not outcome and task_id == run.get("current_task_id") and run["status"] == "running":
            label = "正在处理"
        entries.append({"task_id": task_id, "store_name": result.get("store_name") or task.get("source_name", ""),
                        "batch_name": name, "version": version, "status": outcome, "label": label,
                        "message": result.get("message", ""), "reject_reason": result.get("reject_reason", "")})
    batch_list = list(batches.values())
    batch_name = batch_list[0]["name"] if len(batch_list) == 1 else ""
    title = capture_run_title(run, len(ids), batch_name)
    return {"kind": "capture", "id": run["id"], "title": title, "mode": run.get("mode", ""),
            "name": (run.get("name") or ""),
            "batch_id": batch_list[0]["id"] if len(batch_list) == 1 else "",
            "batch_name": batch_name, "batches": batch_list,
            "status": run.get("status", ""), "created_at": run.get("created_at", ""),
            "updated_at": run.get("updated_at", ""), "message": run.get("message", ""),
            "counts": {"total": len(ids), "processed": processed, "done": outcomes["captured"], "attention": outcomes["review"],
                       "rejected": outcomes["rejected"], "recorded": sum(outcomes[key] for key in CAPTURE_RESULT_LABELS),
                       "unknown": len(ids)-len(recorded), "failed": 0},
            "tasks": entries}

