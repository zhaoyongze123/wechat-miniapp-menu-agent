"""不合格（打回）域：清单、导出、人工复核与撤销。"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

from menu_capture.api.context import ApiContext


def register(app: FastAPI, ctx: ApiContext) -> None:
    rejections, store, materials, hub = ctx.rejections, ctx.store, ctx.materials, ctx.hub
    control_lock, idle = ctx.control_lock, ctx.idle

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

    @app.get("/api/rejections/export-store-names.xlsx")
    def export_store_names():
        """全量门店名称导出：名称提取独立于采集结果，覆盖成功、待检查与不合格。"""
        content = rejections.export_store_names()
        filename = "全量门店名称.xlsx"
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=store-names.xlsx; filename*=UTF-8''{quote(filename)}"},
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
            # 采集电脑仍占用该门店时先阻塞，避免打回后迟到结果无处落地。
            if hub and hub.active_task_ids([task_id]):
                raise ValueError("该门店仍在采集电脑任务中，请等待完成或先收回后再打回")
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

    @app.post("/api/rejections/materials/{batch_id}/{store_id}/undo")
    def undo_material_rejection(batch_id: str, store_id: str):
        with control_lock:
            idle()
            current = materials.get_store(batch_id, store_id)
            if current["decision"] != "skipped":
                raise ValueError("该门店当前没有不合格标记")
            return materials.decide_store(batch_id, store_id, "pending")
