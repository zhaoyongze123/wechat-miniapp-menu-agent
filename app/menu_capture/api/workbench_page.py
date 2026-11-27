"""共享工作台门店分页与汇总，按当前有效任务计算筛选和勾选状态。"""
from menu_capture.capture.capture_results import capture_outcome, state_counts, store_state
from menu_capture.quality.failures import describe

SELECTED_FIELDS = ("id", "source_name", "status", "reject_reason", "runnable", "batch_name", "version")


def workbench_page(tasks, *, snapshot, query, status, sort, page, size, selected_ids, batch_id=""):
    batches = {row["batch_id"]: row.get("batch_name") or row["batch_id"]
               for row in tasks if row.get("batch_id")}
    scope = tasks
    if snapshot is not None:
        scope = [row for row in scope if row.get("batch_snapshot_id", row["snapshot_id"]) == snapshot]
    elif batch_id:
        scope = [row for row in scope if row.get("batch_id") == batch_id]
    selected = [{field: row.get(field) for field in SELECTED_FIELDS}
                for row in tasks if row["id"] in selected_ids]

    needle = query.strip().casefold()
    rows = [row for row in scope if needle in f"{row['source_name']} {row['source_path']}".casefold()]
    # 统计随批次和关键词变化，各状态保持可见，便于切换状态筛选。
    counts = state_counts(rows)
    if status != "all":
        rows = [row for row in rows if store_state(row) == status]

    def by_name(row):
        return row["source_name"].casefold(), row["id"]

    rows.sort(key=by_name)
    if sort == "recent":
        rows.sort(key=lambda row: row.get("updated_at", ""), reverse=True)
    elif sort == "status":
        rows.sort(key=lambda row: row["status"])

    total = len(rows)
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    first = (page - 1) * size
    return {"records": [row | {"capture_outcome": capture_outcome(row)}
                        | describe(row.get("error_code", ""), row.get("stage", ""), row.get("message", ""))
                        | {"store_state": store_state(row)} for row in rows[first:first + size]],
            "counts": counts, "selected": selected, "page": page, "pages": pages, "total": total,
            **({"batches": [{"id": key, "name": name} for key, name in sorted(batches.items(), key=lambda item: item[1])]}
               if batches else {})}
