"""统一采集结果的业务含义。轮次状态、任务归属和同步状态由各自模块维护。"""

CAPTURE_RESULT_LABELS = {
    "captured": "已截图待复核",
    "review": "待检查",
    "rejected": "不合格",
}
REVIEW_STATUSES = {"needs_review", "needs_retry", "error", "blocked", "failed"}


def waiting_for_materials(task: dict) -> bool:
    return task.get("stage") == "returned_to_materials"


def capture_outcome(task: dict) -> str:
    if waiting_for_materials(task):
        return ""
    if task.get("reject_reason"):
        return "rejected"
    if task.get("status") == "captured":
        return "captured"
    if task.get("status") in REVIEW_STATUSES:
        return "review"
    return ""
