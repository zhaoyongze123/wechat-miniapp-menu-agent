"""采集队列、轮次控制和半自动操作 HTTP 协议。"""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import Query

from fastapi import FastAPI
import socket
import sys
from pathlib import Path
from fastapi import HTTPException, Request
from menu_capture.core.failures import describe as describe_failure
from ..capture.status import build_capture_status
from ..capture.results import REVIEW_RERUN_LIMIT


logger = logging.getLogger(__name__)


def register_capture_routes(
    app: FastAPI,
    *,
    configs,
    connections,
    control_lock,
    environment,
    hub,
    idle,
    materials,
    profiles,
    remote_control_host,
    runner,
    store,
    task_center,
    worker,
) -> None:
    @app.get("/api/status")
    def status(queue_page: Annotated[int | None, Query(ge=1)] = None,
               queue_size: Annotated[int, Query(ge=1, le=100)] = 50,
               queue_scope: Literal["all", "current"] = "all",
               queue_batch: Annotated[str, Query(max_length=128)] = "",
               queue_q: Annotated[str, Query(max_length=300)] = "",
               queue_filter: Literal["all", "pending", "running", "captured", "rejected", "review", "returned"] = "all",
               queue_sort: Literal["source", "recent", "status"] = "source",
               selected: Annotated[str, Query(max_length=9000)] = "",
               include_events: bool = True):
        return build_capture_status(
            configs=configs,
            materials=materials,
            hub=hub,
            store=store,
            worker=worker,
            runner=runner,
            profiles=profiles,
            task_center=task_center,
            queue_page=queue_page,
            queue_size=queue_size,
            queue_scope=queue_scope,
            queue_batch=queue_batch,
            queue_q=queue_q,
            queue_filter=queue_filter,
            queue_sort=queue_sort,
            selected=selected,
            include_events=include_events,
        )

    @app.get("/api/capture/queue")
    def capture_queue(batch_ids: str = "all", q: str = ""):
        """跨批次有效任务清单：列表、统计、全选和开跑共用这一份数据。"""
        wanted = None if batch_ids in {"", "all"} else [value for value in batch_ids.split(",") if value]
        records = worker.tasks() if worker else materials.effective_capture_tasks()
        if wanted is not None:
            records = [row for row in records if row["batch_id"] in set(wanted)]
        needle = q.strip().casefold()
        if needle:
            records = [row for row in records
                       if needle in (row["source_name"] + " " + row["batch_name"]).casefold()]
        batches = {}
        for row in records:
            item = batches.setdefault(row["batch_id"], {"batch_id": row["batch_id"], "batch_name": row["batch_name"],
                                                        "version": row["version"], "stores": 0})
            item["stores"] += 1
            item["version"] = max(item["version"], row["version"])
        enriched = [row | describe_failure(row.get("reject_reason") or row.get("error_code", ""),
                                           row.get("stage", ""), row.get("message", ""))
                    for row in records]
        return {
            "records": enriched,
            "batches": sorted(batches.values(), key=lambda item: item["batch_name"]),
            "summary": {"stores": len(enriched),
                        "runnable": sum(bool(row["runnable"]) for row in enriched),
                        "batches": len(batches)},
        }

    @app.post("/api/scan")
    def scan():
        with control_lock:
            idle()
            config = configs.load()
            if not config.input_dir:
                raise HTTPException(400, "请先选择用于采集的发布版本")
            if ((Path(config.input_dir) / ".materials-snapshot.json").is_file()
                    and store.material_binding(config.input_dir) is None):
                raise HTTPException(400, "素材版本已移入回收站或尚未发布，请重新选择采集版本")
            try:
                result = store.scan(Path(config.input_dir))
                runner.invalidate_readiness()
            except OSError:
                raise HTTPException(400, "素材目录不存在或无法读取") from None
            environment.invalidate("uipath_desktop")
            connections.invalidate()
            store.event(f"扫描完成：新增 {result['added']} 个素材任务")
            root_dir = Path(config.input_dir).expanduser().resolve()
            current_total = sum(root_dir in Path(task["source_path"]).parents for task in store.tasks())
            return {**result, "current_total": current_total, "material_release": materials.active_release(root_dir)}

    @app.post("/api/start")
    def start():
        with control_lock:
            idle()
            run = store.latest_run()
            if run and run["status"] in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "请先继续或终止当前批次")
            runner.start()
            return status()

    @app.post("/api/resume")
    def resume(payload: dict | None = None):
        with control_lock:
            idle()
            run = store.latest_run()
            if not run or run["status"] not in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "当前没有可恢复的暂停或阻塞批次")
            payload = payload or {}
            mode = payload.get("mode", "")
            if payload and (set(payload) != {"mode"} or mode != "manual" or sys.platform != "win32"
                            or run.get("capture_mode") != "auto" or run["status"] != "paused"):
                raise ValueError("只有已暂停的 Windows 全自动批次可以切换半自动")
            profile = profiles.load()
            # 批量采集按本轮锁定的模式恢复，不再受当前机器人连接方式影响。
            if not run.get("capture_mode") and profile.get("mode") != run["mode"]:
                raise HTTPException(409, "采集方式与原批次不一致；请恢复原采集方式后继续")
            remaining = run["task_ids"][run["current_index"]:]
            if not remaining:
                raise HTTPException(409, "此批次没有尚未完成的门店")
            try:
                if worker:
                    worker.check_start(remaining)
                if mode:
                    runner.start(resume_run=run, capture_mode=mode)
                else:
                    runner.start(resume_run=run)
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from None
            return status()

    @app.post("/api/ui-tars/trial")
    def start_ui_tars_trial(payload: dict | None = None):
        payload = payload or {}
        task_id = payload.get("task_id")
        task_ids = payload.get("task_ids")
        if set(payload) == {"task_ids"}:
            if not isinstance(task_ids, list) or not task_ids or any(
                not isinstance(value, str) or not value.strip() for value in task_ids
            ):
                raise ValueError("全自动试验需要有效的门店任务列表")
            with control_lock:
                idle()
                return runner.start(ui_tars_trial=True, trial_task_ids=[value.strip() for value in task_ids])
        if set(payload) - {"task_id"} or (payload and (not isinstance(task_id, str) or not task_id.strip())):
            raise ValueError("试验需要指定已有的门店任务")
        with control_lock:
            idle()
            if task_id:
                return runner.start(ui_tars_trial=True, trial_task_id=task_id.strip())
            return runner.start(ui_tars_trial=True)

    @app.post("/api/capture/start")
    def start_capture(payload: dict | None = None):
        """统一开跑入口：本轮模式与任务清单在这里锁定，两种模式共用同一批门店。"""
        payload = payload or {}
        if set(payload) - {"mode", "task_ids", "name"}:
            raise ValueError("开跑参数无效")
        mode = payload.get("mode")
        if mode not in {"auto", "manual"}:
            raise ValueError("请选择全自动 Agent 或半自动人工")
        name = payload.get("name", "")
        if not isinstance(name, str) or len(name.strip()) > 80:
            raise ValueError("任务名称无效")
        task_ids = payload.get("task_ids")
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= REVIEW_RERUN_LIMIT:
            raise ValueError(f"请选择 1 到 {REVIEW_RERUN_LIMIT} 家门店")
        if any(not isinstance(value, str) or not value.strip() for value in task_ids):
            raise ValueError("门店任务编号无效")
        if mode == "manual" and sys.platform != "win32":
            raise HTTPException(501, "半自动人工首版仅支持 Windows")
        unique_ids = list(dict.fromkeys(value.strip() for value in task_ids))
        with control_lock:
            idle()
            run = store.latest_run()
            if run and run["status"] in {"paused", "blocked", "interrupted"}:
                raise HTTPException(409, "请先继续或终止当前批次")
            try:
                if worker:
                    worker.check_start(unique_ids)
                result = runner.start(ui_tars_trial=True, trial_task_ids=unique_ids,
                                      cross_batch=True, capture_mode=mode, run_name=name.strip())
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(400, str(exc)) from None
            if worker:
                return {**result, "status": status()}
            try:
                # 采集轮次与审核失效跟随实际开跑，失败不影响已启动的批次。
                materials.register_capture_round(unique_ids)
            except Exception:
                logger.exception("记录采集轮次失败")
            return {**result, "status": status()}

    def manual_device() -> tuple[str, str]:
        local_name = socket.gethostname()
        connection = worker.connection if worker else {}
        return connection.get("id") or local_name, connection.get("name") or local_name

    def _manual_action(request: Request, kind: str, payload: dict) -> dict:
        expected = payload.get("context")
        if request.state.remote_control or expected is not None:
            keys = {"device_id", "run_id", "task_id", "wait_id"}
            if (not isinstance(expected, dict) or set(expected) != keys
                    or any(not isinstance(expected[key], str) or not expected[key] for key in keys)
                    or expected["device_id"] != manual_device()[0]):
                raise HTTPException(409, "采集端或门店上下文已失效，请重读当前状态")
        try:
            result = runner.manual_action(kind, str(payload.get("reason") or ""),
                                          str(payload.get("detail") or ""), expected=expected)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if request.state.remote_control:
            return result | {"manual": runner.manual_state()}
        return result | {"manual": runner.manual_state(), "status": status()}

    @app.get("/api/manual/state")
    def manual_state():
        device_id, device_name = manual_device()
        run = store.latest_run()
        return {"device_id": device_id, "device_name": device_name, "manual": runner.manual_state(),
                "run_name": run.get("name") or "半自动采集" if run else "",
                "run_status": run["status"] if run else "",
                "remote_url": f"https://{remote_control_host}/remote-control" if remote_control_host else ""}

    @app.post("/api/manual/capture")
    def manual_capture(request: Request, payload: dict | None = None):
        payload = payload or {}
        if set(payload) - {"context"}:
            raise ValueError("截图参数无效")
        with control_lock:
            return _manual_action(request, "capture", payload)

    @app.post("/api/manual/reject")
    def manual_reject(request: Request, payload: dict | None = None):
        payload = payload or {}
        if set(payload) - {"reason", "detail", "context"}:
            raise ValueError("不合格参数无效")
        with control_lock:
            return _manual_action(request, "reject", payload)

    @app.post("/api/manual/skip")
    def manual_skip(request: Request, payload: dict | None = None):
        payload = payload or {}
        if set(payload) - {"context"}:
            raise ValueError("跳过参数无效")
        with control_lock:
            return _manual_action(request, "skip", payload)

    @app.post("/api/manual/reason")
    def manual_reason(payload: dict):
        if set(payload) != {"reason"}:
            raise ValueError("不合格原因参数无效")
        return {"ok": True, "reason": runner.set_manual_reason(str(payload["reason"]))}

    @app.post("/api/stop")
    def stop():
        with control_lock:
            run = store.latest_run()
            if run and run["mode"] == "attached":
                raise HTTPException(409, "单店采集可以终止后重试")
            runner.pause()
            return status()

    @app.post("/api/terminate")
    def terminate():
        with control_lock:
            runner.terminate()
            return status()
