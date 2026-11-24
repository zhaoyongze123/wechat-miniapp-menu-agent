"""素材批次、选码、分析作业和版本发布 HTTP 协议。"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from pathlib import Path
from typing import Literal
from fastapi import HTTPException, Request
from fastapi.responses import Response


logger = logging.getLogger(__name__)


def register_materials_routes(
    app: FastAPI,
    *,
    control_lock,
    hub,
    idle,
    materials,
    runner,
    store,
) -> None:
    @app.get("/api/materials/batches")
    def material_batches(view: Literal["full", "options"] = "full"):
        return {"batches": materials.batch_options() if view == "options" else materials.list_batches()}

    @app.get("/api/materials/batch-management")
    def manage_material_batches(deleted: bool = False):
        return {"batches": materials.batch_management.list(deleted=deleted)}

    @app.post("/api/materials/batches")
    def import_local_materials(payload: dict):
        if set(payload) - {"input_dir", "name", "description"} or not isinstance(payload.get("name"), str):
            raise ValueError("请填写批次名称")
        if not isinstance(payload.get("input_dir"), str) or not payload["input_dir"].strip():
            raise ValueError("请选择素材目录")
        with control_lock:
            idle()
            return materials.create_local_batch(Path(payload["input_dir"]),
                                                name=payload["name"], description=payload.get("description", ""))

    @app.post("/api/materials/uploads")
    def begin_material_upload(payload: dict):
        if set(payload) - {"name", "description"} or not isinstance(payload.get("name"), str):
            raise ValueError("请填写批次名称")
        with control_lock:
            idle()
            return materials.begin_upload(name=payload["name"], description=payload.get("description", ""))

    @app.patch("/api/materials/batches/{batch_id}")
    def edit_material_batch(batch_id: str, payload: dict):
        if set(payload) - {"name", "description"} or not isinstance(payload.get("name"), str):
            raise ValueError("请填写批次名称")
        with control_lock:
            return materials.batch_management.update(batch_id, name=payload["name"], description=payload.get("description", ""))

    @app.delete("/api/materials/batches/{batch_id}")
    def delete_material_batch(batch_id: str):
        with control_lock:
            try:
                if hub:
                    hub.require_unassigned(batch_id)
                return {"ok": True, "batch": materials.batch_management.delete(batch_id)}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/batches/{batch_id}/restore")
    def restore_material_batch(batch_id: str):
        with control_lock:
            return materials.batch_management.restore(batch_id)

    @app.delete("/api/materials/batches/{batch_id}/permanent")
    def purge_material_batch(batch_id: str):
        with control_lock:
            try:
                if hub:
                    hub.require_unassigned(batch_id)
                return {"ok": True, **materials.batch_management.purge(batch_id)}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/selection/clear")
    def clear_material_selection(payload: dict):
        if set(payload) - {"batch_id"}:
            raise ValueError("清除选择参数无效")
        with control_lock:
            idle()
            try:
                result = materials.batch_management.clear_selection(batch_id=payload.get("batch_id"))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            runner.invalidate_readiness()
            return result

    @app.put("/api/materials/uploads/{batch_id}/file")
    async def upload_material_file(batch_id: str, path: str, request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 50 * 1024 * 1024:
                raise HTTPException(413, "单张图片不能超过 50 MB")
            data.extend(chunk)
        return materials.add_upload_file(batch_id, path, bytes(data))

    @app.post("/api/materials/uploads/{batch_id}/finish")
    def finish_material_upload(batch_id: str):
        return materials.finish_upload(batch_id)

    @app.post("/api/materials/uploads/{batch_id}/abort")
    def abort_material_upload(batch_id: str):
        with control_lock:
            return materials.abort_upload(batch_id)

    @app.post("/api/materials/batches/{batch_id}/analyze")
    def analyze_materials(batch_id: str):
        try:
            return materials.start_analysis(batch_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/batches/{batch_id}/reanalyze")
    def reanalyze_materials(batch_id: str, payload: dict):
        store_ids = payload.get("store_ids")
        if not isinstance(store_ids, list) or not store_ids or any(not isinstance(item, str) for item in store_ids):
            raise HTTPException(422, "store_ids 必须是非空字符串数组") from None
        if len(store_ids) > 2000:
            raise HTTPException(422, "单次最多重新分析 2000 家门店") from None
        try:
            return materials.start_reanalysis(batch_id, store_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/materials/batches/{batch_id}/retry-failed")
    def retry_failed_materials(batch_id: str):
        try:
            return materials.retry_failed(batch_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    def _batch_name(batch_id: str) -> str:
        return materials.batch_name(batch_id)

    @app.get("/api/materials/jobs")
    def material_jobs(active: bool = True, batch_id: str = "", limit: int = 20):
        jobs = materials.list_jobs(active=active, batch_id=batch_id, limit=max(1, min(100, limit)))
        return {"jobs": [{**job, "batch_name": job.get("batch_name") or _batch_name(job["batch_id"])}
                         for job in jobs]}

    @app.get("/api/materials/jobs/{job_id}")
    def material_job(job_id: str, events: bool = True):
        try:
            return materials.job_detail(job_id, events=events)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/materials/jobs/{job_id}/pause")
    def pause_material_job(job_id: str):
        try:
            return materials.pause_job(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/jobs/{job_id}/resume")
    def resume_material_job(job_id: str):
        try:
            return materials.resume_job(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/materials/jobs/{job_id}/cancel")
    def cancel_material_job(job_id: str):
        try:
            return materials.cancel_job(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/materials/batches/{batch_id}")
    def material_batch(batch_id: str):
        return materials.get_batch(batch_id)

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}")
    def material_store(batch_id: str, store_id: str):
        return materials.get_store(batch_id, store_id)

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/uploads")
    def begin_material_store_upload(batch_id: str, store_id: str):
        with control_lock:
            idle()
            return materials.begin_store_upload(batch_id, store_id)

    @app.put("/api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}/file")
    async def upload_material_store_file(batch_id: str, store_id: str, upload_id: str, filename: str, request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 50 * 1024 * 1024:
                raise HTTPException(413, "单张图片不能超过 50 MB")
            data.extend(chunk)
        with control_lock:
            idle()
            return materials.add_store_upload_file(batch_id, store_id, upload_id, filename, bytes(data))

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}/commit")
    def commit_material_store_upload(batch_id: str, store_id: str, upload_id: str, payload: dict):
        if set(payload) - {"mode", "files"} or "mode" not in payload or "files" not in payload:
            raise ValueError("补图提交参数无效")
        with control_lock:
            idle()
            return materials.commit_store_upload(batch_id, store_id, upload_id, payload["mode"], payload["files"])

    @app.delete("/api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}")
    def abort_material_store_upload(batch_id: str, store_id: str, upload_id: str):
        with control_lock:
            idle()
            materials.abort_store_upload(batch_id, store_id, upload_id)
            return {"ok": True}

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/reanalyze")
    def reanalyze_material_store(batch_id: str, store_id: str):
        with control_lock:
            idle()
            return materials.reanalyze_store(batch_id, store_id)

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}/images/{image_index}")
    def material_image(batch_id: str, store_id: str, image_index: int):
        result = materials.preview_original(batch_id, store_id, image_index)
        return Response(result["bytes"], media_type=result["media_type"])

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}/images/{image_index}/candidates/{candidate_index}")
    def material_candidate(batch_id: str, store_id: str, image_index: int, candidate_index: int):
        result = materials.preview_candidate(batch_id, store_id, image_index, candidate_index)
        return Response(result["bytes"], media_type=result["media_type"])

    @app.get("/api/materials/batches/{batch_id}/stores/{store_id}/selected")
    def material_selected_candidate(batch_id: str, store_id: str):
        """当前门店正在使用的候选二维码；未选码的门店返回 404，不猜测。"""
        record = materials.get_store(batch_id, store_id)
        selected = record.get("selected") or {}
        if selected.get("image_index") is None or selected.get("candidate_index") is None:
            raise HTTPException(404, "该门店尚未选择二维码")
        result = materials.preview_candidate(batch_id, store_id, int(selected["image_index"]),
                                             int(selected["candidate_index"]))
        return Response(result["bytes"], media_type=result["media_type"],
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/materials/batches/{batch_id}/stores/{store_id}/decision")
    def decide_material_store(batch_id: str, store_id: str, payload: dict):
        allowed = {"decision", "image_index", "candidate_index", "box", "reject_reason", "reject_detail"}
        if set(payload) - allowed or "decision" not in payload:
            raise ValueError("确认参数无效")
        if payload["decision"] == "skip" and not payload.get("reject_reason"):
            raise ValueError("请选择不合格原因")
        try:
            return materials.decide_store(batch_id, store_id, **payload)
        except Exception as exc:
            box = payload.get("box") if isinstance(payload.get("box"), dict) else {}
            logger.warning(
                "decision failed batch=%s store=%s decision=%s image=%s candidate=%s "
                "box=%s error=%r",
                batch_id, store_id, payload.get("decision"), payload.get("image_index"),
                payload.get("candidate_index"),
                {key: box.get(key) for key in ("x", "y", "width", "height")},
                exc,
            )
            raise

    @app.post("/api/materials/batches/{batch_id}/accept-recommendations")
    def accept_material_recommendations(batch_id: str):
        return materials.accept_recommendations(batch_id)

    @app.post("/api/materials/batches/{batch_id}/publish")
    def publish_materials(batch_id: str, payload: dict | None = None):
        with control_lock:
            fields = payload or {}
            materials.versions.validate(fields.get("name"), fields.get("version"), fields.get("description", ""))
            run = store.latest_run()
            can_activate = not runner.running and not (run and run["status"] in {"paused", "blocked", "interrupted"})
            result = materials.publish_snapshot(
                batch_id, fields.get("description", ""), name=fields["name"], version=fields["version"],
                activate=can_activate,
            )
            if can_activate:
                runner.invalidate_readiness()
            if hub:
                hub.reconcile_assignments()
            return result

    @app.get("/api/materials/rework")
    def material_rework(batch_ids: str = "all", q: str = ""):
        """跨批次返工清单：待处理 / 不合格 / 已选码待发布。"""
        wanted = None if batch_ids in {"", "all"} else [value for value in batch_ids.split(",") if value]
        records = materials.rework_records(wanted)
        needle = q.strip().casefold()
        if needle:
            records = [row for row in records
                       if needle in (row["store_name"] + " " + row["batch_name"]).casefold()]
        return {
            "records": records,
            "summary": {
                "stores": len(records),
                "pending": sum(row["category"] == "pending" for row in records),
                "rejected": sum(row["category"] == "rejected" for row in records),
                "unpublished": sum(row["category"] == "unpublished" for row in records),
                "batches": len({row["batch_id"] for row in records}),
            },
        }

    @app.post("/api/materials/rework/return")
    def return_material_rework(payload: dict):
        """打回选错码的门店：恢复待处理，等待人工在原批次重新选码。"""
        if set(payload) != {"batch_id", "store_id"}:
            raise ValueError("打回参数无效")
        with control_lock:
            idle()
            result = materials.return_rework_store(str(payload["batch_id"]), str(payload["store_id"]))
            if hub:
                hub.reconcile_assignments()
            return result

    @app.post("/api/materials/rework/publish")
    def publish_material_rework(payload: dict):
        """统一发布已处理门店：按原批次分组，各自在最新快照上增量更新。"""
        payload = payload or {}
        if set(payload) - {"store_ids", "description"}:
            raise ValueError("发布参数无效")
        store_ids = payload.get("store_ids")
        if not isinstance(store_ids, list) or not store_ids:
            raise ValueError("请勾选要发布的门店")
        groups: dict[str, list[str]] = {}
        for value in store_ids:
            if not isinstance(value, str) or ":" not in value:
                raise ValueError("门店发布标识无效")
            batch_id, _, store_id = value.partition(":")
            if not batch_id or not store_id:
                raise ValueError("门店发布标识无效")
            groups.setdefault(batch_id, []).append(store_id)
        names = {batch["batch_id"]: batch.get("name", "") for batch in materials.list_batches()}
        description = str(payload.get("description") or "跨批次返工发布")
        results, errors = [], []
        with control_lock:
            idle()
            for batch_id, ids in groups.items():
                try:
                    result = materials.publish_snapshot(batch_id, description, store_ids=ids, activate=False)
                    if hub:
                        hub.reconcile_assignments()
                    results.append({
                        "batch_id": batch_id, "batch_name": names.get(batch_id, ""),
                        "snapshot_id": result["snapshot_id"],
                        "version": result["release"]["version"],
                        "already_published": result["already_published"],
                        "task_scan": result["task_scan"],
                        "store_updates": result["store_updates"],
                    })
                except Exception as exc:
                    errors.append({"batch_id": batch_id, "batch_name": names.get(batch_id, ""),
                                   "error": str(exc)})
        return {"results": results, "errors": errors}

    @app.post("/api/materials/batches/{batch_id}/activate")
    def activate_material_version(batch_id: str, payload: dict):
        with control_lock:
            idle()
            run = store.latest_run()
            if run and run["status"] in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "请先继续或终止当前采集，再切换素材版本")
            result = materials.activate_version(batch_id, payload.get("version", ""))
            runner.invalidate_readiness()
            return result

    @app.post("/api/materials/batches/{batch_id}/stage")
    def set_material_batch_stage(batch_id: str, payload: dict):
        with control_lock:
            return materials.set_stage(batch_id, payload.get("stage", ""))

    @app.post("/api/materials/batches/{batch_id}/approve")
    def approve_material_batch(batch_id: str):
        with control_lock:
            if hub:
                hub.require_unassigned(batch_id)
            return materials.set_approved(batch_id, True)

    @app.post("/api/materials/batches/{batch_id}/approve/undo")
    def unapprove_material_batch(batch_id: str):
        with control_lock:
            return materials.set_approved(batch_id, False)
