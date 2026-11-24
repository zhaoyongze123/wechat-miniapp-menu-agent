"""汇总采集状态与工作台事实，供 HTTP 及采集操作复用。"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from menu_capture.capture.outputs import batch_output_dir
from menu_capture.core.failures import describe as describe_failure
from menu_capture.capture.results import capture_outcome, state_counts, store_state
from menu_capture.capture.workbench import workbench_page


def _rejection_store_code(text: str) -> str:
    """提取门店编号（如 E02412），用于素材不合格与采集不合格按门店去重。"""
    name = Path(text or "").name
    match = re.match(r"^([A-Za-z]{1,4}\d+)", name)
    return (match.group(1) if match else name).upper()


def build_capture_status(
    *,
    configs,
    materials,
    hub,
    store,
    worker,
    runner,
    profiles,
    task_center,
    queue_page: int | None = None,
    queue_size: int = 50,
    queue_scope: str = "all",
    queue_batch: str = "",
    queue_q: str = "",
    queue_filter: str = "all",
    queue_sort: str = "source",
    selected: str = "",
    include_events: bool = True,
):
    # 任务表保留历史；更换成清洗后的目录时，只展示当前素材集，避免旧照片继续可选。
    settings = configs.load()
    input_dir = settings.input_dir
    current_root = Path(input_dir).expanduser().resolve() if input_dir else None
    release = materials.active_release(current_root) if current_root else None
    paged = hub is not None and queue_page is not None
    all_tasks = [] if paged else store.tasks()
    if hub:
        # 共享模式按各批次最新发布版本取数，不需要逐条解析旧任务的文件路径。
        tasks = materials.effective_capture_tasks()
        ready = {"ready": True, "issues": [], "launch_mode": "shared"}
    else:
        tasks = worker.tasks() if worker else [
            task for task in all_tasks if current_root and current_root in Path(task["source_path"]).resolve().parents
        ]
        ready = runner.last_readiness or {"ready": None, "issues": ["环境诊断可在设置中手动查看"],
                                          "launch_mode": profiles.load()["launch_mode"]}
    if sys.platform == "win32" and not hub:
        ready = ready | {"launch_mode": "native"}
    run = None if paged else store.latest_run()
    if run:
        by_id = {task["id"]: task for task in all_tasks}
        ids = run["task_ids"]
        progress = task_center.run_progress(run)
        processed = progress["processed"]
        processed_tasks = [by_id[task_id] for task_id in progress.get("done_ids", []) if task_id in by_id]
        captured = sum(task.get("status") == "captured" for task in processed_tasks)
        unqualified = sum(task.get("status") != "captured" and bool(task.get("reject_reason"))
                          for task in processed_tasks)
        # 跨批次重跑没有单一素材批次可展示：按任务来源快照数量识别。
        snapshots = {Path(by_id[task_id]["source_path"]).parent.parent.name
                     for task_id in ids if task_id in by_id}
        run.update(processed=processed, captured=captured, unqualified=unqualified,
                   review=processed-captured-unqualified,
                   remaining=len(ids)-processed, total=len(ids),
                   processed_ids=progress.get("done_ids", ids[:processed]),
                   cross_batch=len(snapshots) > 1, batch_count=len(snapshots),
                   task_list=[{"id": task_id, "name": by_id.get(task_id, {}).get("source_name", ""),
                               "status": by_id.get(task_id, {}).get("status", "")} for task_id in ids],
                   next_name=by_id.get(ids[processed], {}).get("source_name", "") if processed < len(ids) else "")
    if run:
        recorded = store.capture_run_results([run["id"]]).get(run["id"], [])
        if any(row["outcome"] for row in recorded):
            outcomes = Counter(row["outcome"] for row in recorded)
            processed_ids = [row["task_id"] for row in recorded if row["outcome"]]
            run.update(processed=len(processed_ids), processed_ids=processed_ids,
                       result_by_task={row["task_id"]: row["outcome"] for row in recorded if row["outcome"]},
                       captured=outcomes["captured"], review=outcomes["review"],
                       unqualified=outcomes["rejected"], remaining=len(run["task_ids"])-len(processed_ids))
    tasks = [task | {"capture_outcome": capture_outcome(task)} for task in tasks]
    # 素材清洗拒绝的门店可能没有采集任务，控制台必须与不合格清单同口径计入。
    material_rejected = 0
    if release:
        with store.connect() as db:
            rows = db.execute(
                "SELECT store_json FROM material_decisions WHERE batch_id=? AND decision='skipped'",
                (release["batch_id"],),
            ).fetchall()
        task_codes = {_rejection_store_code(task.get("source_name")) for task in tasks if task.get("reject_reason")
                      and (not paged or task.get("snapshot_id") == release["snapshot_id"])}
        material_codes = set()
        for row in rows:
            try:
                data = json.loads(row["store_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                data = {}
            code = _rejection_store_code(data.get("source_folder") or data.get("name") or "")
            if code:
                material_codes.add(code)
        material_rejected = len(material_codes - task_codes)
    # 全部批次口径与批次管理列表保持一致：只算未删除批次（回收站不计），
    # 并扣除已经有采集不合格记录的同一门店，避免同一家店被计两次。
    with store.connect() as db:
        all_rows = db.execute(
            "SELECT d.store_json FROM material_decisions d "
            "JOIN material_batches b ON b.id=d.batch_id WHERE d.decision='skipped' AND b.deleted_at=''"
        ).fetchall()
    capture_codes = {_rejection_store_code(task.get("source_name")) for task in store.rejections()}
    all_material_codes = set()
    for row in all_rows:
        try:
            data = json.loads(row["store_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            data = {}
        code = _rejection_store_code(data.get("source_folder") or data.get("name") or "")
        if code:
            all_material_codes.add(code)
    material_rejected_all = len(all_material_codes - capture_codes)
    launch_mode = ready.get("launch_mode", "manual")
    engine, engine_message = {
        "shared": ("共享工作台", "请将门店分配给已配对的 Windows 电脑"),
        "native": ("Windows 原生", "本机 Windows 桌面采集已就绪"),
        "cli": ("UiPath", "已就绪，点击开始后由 UiPath CLI 自动启动 Main.xaml"),
    }.get(launch_mode, ("UiPath", "已就绪，等待手动在 Studio 运行 Main.xaml"))
    queue = workbench_page(
        tasks, snapshot=(release["snapshot_id"] if release else "") if queue_scope == "current" else None,
        query=queue_q, status=queue_filter, sort=queue_sort, page=queue_page, size=queue_size,
        selected_ids=set(selected.split(",")), batch_id=queue_batch,
    ) if paged else None
    return {"execution_engine": engine, "launch_mode": launch_mode,
            "engine_ready": ready["ready"], "engine_message": "；".join(ready["issues"]) or engine_message,
            "robot_readiness": ready, "run_directory": str(getattr(runner, "run_directory", "") or ""),
            "running": runner.running, "stop_requested": runner.stop.is_set(),
            "run": run, "current_task": runner.current_task,
            "material_release": release,
            "batch_output_dir": (str(batch_output_dir(settings.output_dir,
                                                      release["batch_id"] if release else "",
                                                      release.get("batch_name", "") if release else ""))
                                 if settings.output_dir else ""),
            "input_name": "本机分配" if worker else (current_root.name if current_root else ""),
            "tasks": queue["records"] if queue else [task | describe_failure(task.get("error_code", ""), task.get("stage", ""), task.get("message", "")) | {"store_state": store_state(task)} for task in tasks],
            "counts": queue["counts"] if queue else state_counts(tasks),
            **({"queue": {key: value for key, value in queue.items() if key not in {"records", "counts"}}} if queue else {}),
            "material_rejected": material_rejected, "material_rejected_all": material_rejected_all,
            "events": store.events() if include_events else [],
            "manual": runner.manual_state() if runner.running else None}
