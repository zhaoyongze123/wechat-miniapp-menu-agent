"""待检查/复核域：待检查清单、批量重跑与按原批次审核通过。"""
from __future__ import annotations

from fastapi import FastAPI

from menu_capture.api.context import ApiContext
from menu_capture.capture.capture_results import capture_outcome
from menu_capture.quality.failures import describe as describe_failure

# 跨批次待检查重跑的上限：仅用于一次人工批量操作，超过这个量级应拆成多轮。
REVIEW_RERUN_LIMIT = 300


def _review_queue(ctx: ApiContext, batch_ids: str, query: str, project_id: str) -> dict:
    """待检查清单：跨批次读取任务状态，同一门店只保留最新一条。"""
    # 与批次管理、工作台保持同一口径：只取未删除批次的最新发布快照任务，
    # 旧版本遗留的失败任务不进入清单，避免同一门店重复计数或被重跑旧素材。
    current_tasks = {row["id"]: row for row in ctx.materials.effective_capture_tasks(project_id=project_id)
                     if capture_outcome(row) == "review"}
    candidates = current_tasks.values()
    # 待检查入口只展示批次名和阶段。版本差异、素材计数留给批次管理页。
    with ctx.store.connect() as db:
        batches = [dict(row) for row in db.execute(
            "SELECT id,name,capture_rounds,manual_stage,approved_at FROM material_batches "
            "WHERE deleted_at='' AND project_id=?", (project_id,)
        )]
    workflow = {batch["id"]: ctx.materials.workflow_stage(batch) for batch in batches}
    names = {batch["id"]: batch["name"] or batch["id"] for batch in batches}
    # 历史记录需要真实轮次身份：取每个任务最近一次动作所属轮次，避免前端按门店名猜。
    with ctx.store.connect() as db:
        run_by_task = {
            row["id"]: row["run_id"]
            for row in db.execute(
                "SELECT c.task_id AS id,c.run_id FROM capture_run_results c "
                "JOIN runs r ON r.id=c.run_id WHERE c.project_id=? "
                "AND r.updated_at=(SELECT MAX(r2.updated_at) FROM capture_run_results c2 "
                "JOIN runs r2 ON r2.id=c2.run_id WHERE c2.task_id=c.task_id AND c2.project_id=?)",
                (project_id, project_id),
            )
            if row["run_id"]
        }
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
        # 与打回接口共用同一判定，避免列表显示可操作但实际被拒。
        blocker = ctx.rejections.return_blocker(task["id"], current=True, project_id=project_id)
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
            "run_id": run_by_task.get(task["id"], ""),
            "batch_id": batch_id,
            "batch_name": task.get("material_batch_name") or names.get(batch_id, ""),
            "workflow": workflow.get(batch_id, {}),
            "can_return_to_materials": not blocker,
            "return_to_materials_disabled_reason": blocker,
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


def register(app: FastAPI, ctx: ApiContext) -> None:
    materials, runner, hub = ctx.materials, ctx.runner, ctx.hub
    control_lock, idle = ctx.control_lock, ctx.idle

    @app.get("/api/reviews/pending")
    def pending_reviews(batch_ids: str = "all", q: str = "", project_id: str | None = None):
        scope = ctx.project_scope(project_id)
        return _review_queue(ctx, batch_ids, q, scope)

    @app.post("/api/reviews/pending/rerun")
    def rerun_pending_reviews(payload: dict):
        task_ids = payload.get("task_ids")
        if set(payload) - {"task_ids", "name", "project_id"}:
            raise ValueError("待检查重跑参数无效")
        if (not isinstance(task_ids, list) or not task_ids or len(task_ids) > REVIEW_RERUN_LIMIT
                or any(not isinstance(value, str) or not value.strip() for value in task_ids)):
            raise ValueError(f"请选择 1 到 {REVIEW_RERUN_LIMIT} 个待检查任务")
        selected = [value.strip() for value in task_ids]
        if len(set(selected)) != len(selected):
            raise ValueError("待检查任务列表不能包含重复项")
        name = payload.get("name", "")
        if not isinstance(name, str) or len(name.strip()) > 80:
            raise ValueError("任务名称无效")
        with control_lock:
            idle()
            scope = ctx.project_scope(payload.get("project_id"), write=True)
            current_ids = {row["task_id"] for row in _review_queue(ctx, "all", "", scope)["records"]}
            if any(task_id not in current_ids for task_id in selected):
                raise ValueError("所选任务不属于当前项目的待检查清单")
            if ctx.worker:
                ctx.worker.check_start(selected, project_id=scope)
            result = runner.start(ui_tars_trial=True, trial_task_ids=selected, cross_batch=True,
                                  capture_mode="auto", run_name=name.strip(), project_id=scope)
            # 启动成功后才累加轮次，避免校验失败也把批次标成“已重跑”。
            materials.register_capture_round(selected, project_id=scope)
            return result

    @app.post("/api/reviews/approve")
    def approve_reviews(payload: dict):
        """最终按原批次审核通过；一次可提交多个批次，批次号与归属不变。"""
        batch_ids = payload.get("batch_ids") if isinstance(payload, dict) else None
        if (not isinstance(batch_ids, list) or not 1 <= len(batch_ids) <= 100
                or any(not isinstance(value, str) or not value.strip() for value in batch_ids)
                or set(payload) - {"batch_ids", "project_id"}):
            raise ValueError("请选择 1 到 100 个原批次")
        results = []
        with control_lock:
            scope = ctx.project_scope(payload.get("project_id"), write=True)
            selected = list(dict.fromkeys(value.strip() for value in batch_ids))
            if hub:
                for batch_id in selected:
                    ctx.batch_scope(batch_id, scope, write=True)
                    hub.require_unassigned(batch_id)
            else:
                for batch_id in selected:
                    ctx.batch_scope(batch_id, scope, write=True)
            for batch_id in selected:
                results.append(materials.set_approved(batch_id, True, project_id=scope))
        return {"ok": True, "batches": results}
