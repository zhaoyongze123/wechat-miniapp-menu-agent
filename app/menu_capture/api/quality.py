"""不合格与待检查结果的审核 HTTP 协议。"""
from __future__ import annotations

from fastapi import FastAPI
import re
from pathlib import Path
from urllib.parse import quote
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from menu_capture.capture.results import waiting_for_materials
from ..quality.review_queue import review_queue
from ..capture.results import REVIEW_RERUN_LIMIT


def register_quality_routes(
    app: FastAPI,
    *,
    control_lock,
    hub,
    idle,
    materials,
    rejections,
    runner,
    store,
) -> None:
    @app.get("/api/rejections")
    def list_rejections(source: str = "all", reason: str = "all", q: str = "", batch_id: str = "all",
                        reviewed: str = "all"):
        return rejections.list(source, reason, q, batch_id, reviewed)

    @app.get("/api/rejections/export.xlsx")
    def export_rejections(source: str = "all", reason: str = "all", q: str = "", batch_id: str = "all",
                          reviewed: str = "all"):
        content = rejections.export_xlsx(source, reason, q, batch_id, reviewed)
        filename = "不合格门店清单.xlsx"
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=unqualified-stores.xlsx; filename*=UTF-8''{quote(filename)}"},
        )

    @app.post("/api/rejections/review")
    def review_rejection(payload: dict):
        """人工复核不合格：只切换确认标记，不改变任务状态与原因。"""
        if set(payload) - {"source_type", "task_id", "batch_id", "store_id", "reviewed"}:
            raise ValueError("人工确认参数无效")
        if payload.get("source_type") not in {"capture", "materials"} or not isinstance(payload.get("reviewed"), bool):
            raise ValueError("人工确认参数无效")
        with control_lock:
            return rejections.mark_reviewed(
                payload["source_type"], task_id=str(payload.get("task_id") or ""),
                batch_id=str(payload.get("batch_id") or ""), store_id=str(payload.get("store_id") or ""),
                reviewed=payload["reviewed"],
            )

    @app.post("/api/rejections/undo-batch")
    def undo_rejections_batch(payload: dict):
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or set(payload) != {"items"}:
            raise ValueError("批量撤销参数无效")
        with control_lock:
            idle()
            return rejections.undo_many(items, protect_task=hub.protect_task if hub else None)

    @app.post("/api/tasks/{task_id}/reject")
    def reject_task(task_id: str, payload: dict):
        if set(payload) - {"reason", "detail"} or "reason" not in payload:
            raise ValueError("不合格原因参数无效")
        with control_lock:
            idle()
            if hub:
                hub.protect_task(task_id)
            return {"ok": True, "task": rejections.mark_task(task_id, payload["reason"], payload.get("detail", ""))}

    @app.post("/api/tasks/{task_id}/rejection/undo")
    def undo_task_rejection(task_id: str):
        with control_lock:
            idle()
            if hub:
                hub.protect_task(task_id)
            return {"ok": True, "task": rejections.undo_task(task_id)}

    @app.post("/api/rejections/tasks/{task_id}/return-to-materials")
    def return_rejection_to_materials(task_id: str):
        """打回当前门店，保留此前执行与迟到结果供回查。"""
        with control_lock:
            idle()
            result = rejections.return_to_materials(task_id)
            if hub:
                hub.reconcile_assignments()
            return result

    @app.get("/api/rejections/tasks/{task_id}/evidence")
    def rejection_evidence(task_id: str):
        task = store.get(task_id)
        name = str(task.get("reject_evidence") or "")
        if hub and Path(name).is_absolute():
            path = Path(name).resolve()
            if hub.root.resolve() not in path.parents or path.name != "evidence.png" or not path.is_file():
                raise HTTPException(404, "不合格证据不存在")
            return FileResponse(path, headers={"Cache-Control": "no-store"})
        if not re.fullmatch(r"[0-9a-f]{20}\.png", name):
            raise HTTPException(404)
        path = store.path.parent / "rejection-evidence" / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/reviews/pending")
    def pending_reviews(batch_ids: str = "all", q: str = ""):
        return review_queue(batch_ids, q, store=store, materials=materials)

    @app.post("/api/reviews/pending/rerun")
    def rerun_pending_reviews(payload: dict):
        task_ids = payload.get("task_ids")
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
            result = runner.start(ui_tars_trial=True, trial_task_ids=selected, cross_batch=True,
                                  capture_mode="auto", run_name=name.strip())
            # 启动成功后才累加轮次，避免校验失败也把批次标成“已重跑”。
            materials.register_capture_round(selected)
            return result

    @app.post("/api/reviews/approve")
    def approve_reviews(payload: dict):
        """最终按原批次审核通过；一次可提交多个批次，批次号与归属不变。"""
        batch_ids = payload.get("batch_ids") if isinstance(payload, dict) else None
        if (not isinstance(batch_ids, list) or not 1 <= len(batch_ids) <= 100
                or any(not isinstance(value, str) or not value.strip() for value in batch_ids)):
            raise ValueError("请选择 1 到 100 个原批次")
        results = []
        with control_lock:
            selected = list(dict.fromkeys(value.strip() for value in batch_ids))
            if hub:
                for batch_id in selected:
                    hub.require_unassigned(batch_id)
            for batch_id in selected:
                results.append(materials.set_approved(batch_id, True))
        return {"ok": True, "batches": results}

    @app.post("/api/rejections/materials/{batch_id}/{store_id}/undo")
    def undo_material_rejection(batch_id: str, store_id: str):
        with control_lock:
            idle()
            current = materials.get_store(batch_id, store_id)
            if current["decision"] != "skipped":
                raise ValueError("该门店当前没有不合格标记")
            return materials.decide_store(batch_id, store_id, "pending")

    @app.post("/api/tasks/{task_id}/retry")
    def retry(task_id: str):
        with control_lock:
            idle()
            if hub:
                hub.protect_task(task_id)
            task = store.get(task_id)
            if waiting_for_materials(task):
                raise HTTPException(409, "该门店待重新选码并发布")
            if task["status"] not in {"needs_review", "needs_retry", "error", "blocked", "captured"}:
                raise ValueError("此任务还没有可重试或重新采集的结果")
            # 重试开始不等于问题已经解决；不合格记录保留到新一轮成功采集。
            store.update(task_id, status="pending", stage="queued", message="已重新排队；旧输出保留")
            return {"ok": True}

    @app.post("/api/tasks/{task_id}/mark-review")
    def mark_task_review(task_id: str):
        """人工把已有采集结果的门店改为待检查；不合格记录必须先撤销，避免状态自相矛盾。"""
        with control_lock:
            idle()
            if hub:
                hub.protect_task(task_id)
            task = store.get(task_id)
            if task.get("reject_reason"):
                raise ValueError("请先撤销不合格标记，再改为待检查")
            if task["status"] == "needs_review":
                return {"ok": True, "task": task}
            if task["status"] not in {"captured", "needs_retry", "error", "blocked"}:
                raise ValueError("该门店还没有采集结果，不能改为待检查")
            store.update(task_id, status="needs_review")
            return {"ok": True, "task": store.get(task_id)}
