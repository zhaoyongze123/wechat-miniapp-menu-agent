"""门店截图、动作证据和任务附件 HTTP 协议。"""
from __future__ import annotations

from fastapi import FastAPI
import os
import re
import subprocess
import sys
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from menu_capture.capture.outputs import batch_output_dir


def register_evidence_routes(
    app: FastAPI,
    *,
    configs,
    debug_log,
    materials,
    runner,
    store,
    worker,
) -> None:
    @app.get("/api/tasks/{task_id}/source")
    def task_source_image(task_id: str):
        """任务当前使用的二维码裁剪图；不合格详情用它展示“当前使用的候选码”。"""
        task = store.get(task_id)
        path = Path(task["source_path"]).expanduser()
        if not path.is_file():
            raise HTTPException(404, "素材文件不存在")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.get("/api/tasks/{task_id}/last-frame")
    def task_last_frame(task_id: str):
        """待检查门店的“最后一帧”：取运行期目录里该任务最新的一张画面。

        待检查任务没有发布截图，动作记录也可能因调试日志轮转丢失证据字段；
        运行目录（.uipath-runs / .uipath-staging）仍保留当时的画面，用它兜底。
        """
        if not re.fullmatch(r"[0-9a-f]{20}", task_id):
            raise HTTPException(404, "任务编号无效")
        task = store.get(task_id)
        runtime = Path(configs.load().output_dir).expanduser().resolve().parent
        candidates: list[Path] = []
        runs = runtime / ".uipath-runs"
        if runs.is_dir():
            candidates.extend(runs.glob(f"*/ui-tars-trial/{task_id}/**/*.png"))
            candidates.extend(runs.glob(f"*/{task_id}/**/*.png"))
        staging = runtime / ".uipath-staging"
        if staging.is_dir():
            candidates.extend(staging.glob(f"{task_id}/**/*.png"))
        output = Path(task.get("output_dir") or "")
        if task.get("output_dir") and output.is_dir():
            candidates.extend(output.glob("*.png"))
        files = [path for path in candidates if path.is_file()]
        if not files:
            raise HTTPException(404, "没有可用截图")
        # 只取最近一次尝试的画面：旧批次的成功页和本轮收尾截图都不能冒充当前失败画面。
        def attempt(path: Path) -> Path:
            parts = path.parts
            if "ui-tars-trial" in parts:
                return Path(*parts[:parts.index("ui-tars-trial") + 1])
            for marker in (".uipath-runs", ".uipath-staging"):
                if marker in parts:
                    return Path(*parts[:parts.index(marker) + 2])
            return path.parent

        groups: dict[Path, list[Path]] = {}
        for path in files:
            groups.setdefault(attempt(path), []).append(path)
        latest = max(groups, key=lambda key: max(path.stat().st_mtime for path in groups[key]))
        scoped = groups[latest]
        cleanup = re.compile(r"(after-close|^wechat-|^desktop|zzz-failure)", re.I)
        usable = [path for path in scoped if not cleanup.search(path.name)] or scoped
        newest = max(usable, key=lambda path: path.stat().st_mtime)
        return FileResponse(newest, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/tasks/{task_id}/actions")
    def task_actions(task_id: str, before: int | None = None):
        actions = store.actions(task_id, before)
        for row in actions:
            compact = row.get("details") or {}
            if not compact.get("debug_enabled"):
                continue
            record = debug_log.action_record(row["id"])
            if record:
                row["details"] = record.get("details", compact) | {"debug_state": "ready"}
                row["debug_state"] = "ready"
            else:
                row["debug_state"] = debug_log.action_state(row["id"])
                row["details"] = compact | {"debug_state": row["debug_state"]}
        return {"actions": actions}

    @app.get("/api/action-evidence/{name}")
    def action_evidence(name: str):
        if not re.fullmatch(r"[0-9a-f]{32}\.png", name):
            raise HTTPException(404)
        path = store.path.parent / "action-evidence" / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.post("/api/reveal")
    def reveal(payload: dict):
        config = configs.load()
        if payload.get("path") not in {"input", "output"}:
            raise ValueError("只能打开素材目录或结果目录")
        selected = config.input_dir if payload["path"] == "input" else config.output_dir
        if not selected.strip():
            raise ValueError("请先选择采集素材版本" if payload["path"] == "input" else "请先设置输出目录")
        release = materials.active_release(Path(config.input_dir).resolve()) if config.input_dir else None
        path = (batch_output_dir(selected, release["batch_id"], release.get("batch_name", ""))
                if payload["path"] == "output" and release else Path(selected))
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.run(["/usr/bin/open", str(path)], check=True)
        return {"ok": True}

    @app.get("/api/artifacts/{task_id}")
    def artifacts(task_id: str):
        with runner.output_lock:
            task = store.get(task_id)
            if not task["output_dir"]:
                return {"files": []}
            folder = Path(task["output_dir"]).resolve()
            return {"files": [str(p.relative_to(folder)) for p in sorted(folder.rglob("*"))
                              if p.is_file() and p.suffix.lower() in {".png", ".json"}]}

    @app.get("/api/source/{task_id}")
    def source_image(task_id: str):
        task = store.get(task_id)
        source = Path(task["source_path"]).resolve()
        if worker and worker.source_root(task_id) and source.is_file():
            return FileResponse(source)
        input_dir = configs.load().input_dir
        if not input_dir.strip():
            raise HTTPException(404, "尚未选择采集素材版本")
        directory = Path(input_dir).resolve()
        if directory not in source.parents or not source.is_file() or source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(404, "素材不在当前输入目录中")
        return FileResponse(source)

    @app.get("/api/artifact/{task_id}/{relative_path:path}")
    def artifact(task_id: str, relative_path: str):
        with runner.output_lock:
            task = store.get(task_id)
            if not task["output_dir"]:
                raise HTTPException(404, "此任务还没有输出")
            directory = Path(task["output_dir"]).resolve()
            path = (directory / relative_path).resolve()
            if directory not in path.parents or not path.is_file() or path.suffix.lower() not in {".png", ".json"}:
                raise HTTPException(404, "输出文件不存在")
            # 在同一锁内读取，避免流式响应延迟打开时目录已被另一个素材版本替换。
            return Response(path.read_bytes(), media_type="image/png" if path.suffix.lower() == ".png" else "application/json")
