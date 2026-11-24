"""统一采集结果的业务含义。轮次状态、任务归属和同步状态由各自模块维护。"""

CAPTURE_RESULT_LABELS = {
    "captured": "已截图待复核",
    "review": "待检查",
    "rejected": "不合格",
}
REVIEW_STATUSES = {"needs_review", "needs_retry", "error", "blocked", "failed"}
# 门店唯一状态：每家门店在每个批次里恰好落进一个桶，统计、筛选、导出都以此为准。
STORE_STATE_LABELS = {
    "rejected": "不合格",
    "returned": "待重新选码",
    "review": "待检查",
    "captured": "已截图待复核",
    "running": "采集中",
    "pending": "待采集",
}


def waiting_for_materials(task: dict) -> bool:
    return task.get("stage") == "returned_to_materials"


def store_state(task: dict) -> str:
    """门店唯一状态，优先级：不合格 > 待重新选码 > 已采集 > 待检查/采集中 > 待采集。

    reject_reason 与 status 是两条独立事实：标记不合格不修改 status，
    因此计数和筛选必须先经过这里，禁止直接按 status 分桶，否则同一门店会被算两次。
    采集端会把共享端下发的权威状态写进 shared_state，审核结果以共享端为准。
    """
    shared = task.get("shared_state")
    if shared in STORE_STATE_LABELS:
        return shared
    if task.get("reject_reason"):
        return "rejected"
    if waiting_for_materials(task):
        return "returned"
    status = task.get("status")
    if status == "captured":
        return "captured"
    if status == "running":
        return "running"
    if status in REVIEW_STATUSES:
        return "review"
    return "pending"


def state_counts(tasks) -> dict:
    """互斥计数，各项之和等于门店总数；键名兼容控制台已有统计卡片。"""
    counts = {key: 0 for key in STORE_STATE_LABELS}
    for task in tasks:
        counts[store_state(task)] += 1
    counts["needs_review"] = counts["review"]
    counts["attention"] = counts["review"]
    return counts


def capture_outcome(task: dict) -> str:
    """兼容旧的三分类结果口径，供历史运行与导出使用。"""
    state = store_state(task)
    return state if state in {"captured", "review", "rejected"} else ""


# 人工批量采集与待检查重跑共用服务端上限。
REVIEW_RERUN_LIMIT = 300
