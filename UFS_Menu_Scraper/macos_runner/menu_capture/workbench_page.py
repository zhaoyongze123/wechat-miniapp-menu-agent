"""共享工作台门店分页与汇总，按当前有效任务计算筛选和勾选状态。"""
from collections import Counter

from .failures import describe


REVIEW_STATES = {"needs_review", "error", "blocked"}
ATTENTION_STATES = REVIEW_STATES | {"needs_retry"}
SELECTED_FIELDS = ("id", "source_name", "status", "reject_reason", "runnable")


def workbench_page(tasks, *, snapshot, query, status, sort, page, size, selected_ids):
    scope = tasks if snapshot is None else [row for row in tasks if row["snapshot_id"] == snapshot]
    counts = dict(Counter(row["status"] for row in scope))
    counts["rejected"] = sum(bool(row.get("reject_reason")) for row in scope)
    counts["attention"] = sum(row["status"] in ATTENTION_STATES and not row.get("reject_reason") for row in scope)
    selected = [{field: row.get(field) for field in SELECTED_FIELDS}
                for row in tasks if row["id"] in selected_ids]

    needle = query.strip().casefold()
    rows = [row for row in scope if needle in f"{row['source_name']} {row['source_path']}".casefold()]
    if status == "review":
        rows = [row for row in rows if row["status"] in REVIEW_STATES and not row.get("reject_reason")]
    elif status == "rejected":
        rows = [row for row in rows if row.get("reject_reason")]
    elif status != "all":
        rows = [row for row in rows if row["status"] == status]

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
    return {"records": [row | describe(row.get("error_code", "")) for row in rows[first:first + size]],
            "counts": counts, "selected": selected, "page": page, "pages": pages, "total": total}
