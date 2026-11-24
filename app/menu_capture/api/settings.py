"""本机设置、连接诊断与日志管理 HTTP 协议。"""
from __future__ import annotations

from fastapi import FastAPI
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask


def register_settings_routes(
    app: FastAPI,
    *,
    configs,
    connections,
    control_lock,
    debug_log,
    environment,
    idle,
    profiles,
    remote_token,
    runner,
    token,
) -> None:
    @app.get("/api/config")
    def configuration():
        return configs.public()

    @app.post("/api/select-directory")
    def select_directory(payload: dict):
        kind = payload.get("kind")
        prompts = {
            "materials": "选择素材目录",
            "input": "选择输入目录",
            "output": "选择输出目录",
        }
        if kind not in prompts:
            raise ValueError("目录类型无效")
        prompt = prompts[kind]
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],
                capture_output=True, text=True, timeout=900,
            )
            if result.returncode:
                if "-128" in result.stderr:
                    return {"cancelled": True}
                raise HTTPException(500, "系统目录选择器打开失败")
            selected = result.stdout.strip()
        elif os.name == "nt":
            script = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
                f"$dialog.Description = '{prompt}'; "
                "$dialog.ShowNewFolderButton = $true; "
                "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ [Console]::Write($dialog.SelectedPath) }"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
            )
            if result.returncode:
                raise HTTPException(500, "系统目录选择器打开失败")
            selected = result.stdout.strip()
            if not selected:
                return {"cancelled": True}
        else:
            raise HTTPException(501, "当前系统暂不支持原生目录选择")
        path = Path(selected).expanduser().resolve()
        if not path.is_dir():
            raise HTTPException(400, "所选目录不可用")
        return {"cancelled": False, "path": str(path)}

    @app.get("/api/session")
    def local_session(request: Request):
        # 远程控制与本机控制台使用不同权限范围的令牌。
        return {"token": remote_token if request.state.remote_control else token}

    @app.post("/api/config")
    def configure(payload: dict):
        with control_lock:
            idle()
            previous_model = connections._fingerprint()
            result = configs.save(payload)
            debug_log.configure(result["debug_logging"]["max_storage_mb"])
            runner.invalidate_readiness()
            if previous_model != connections._fingerprint():
                environment.invalidate()
            else:
                environment.invalidate("uipath_desktop")
            connections.invalidate()
            return result

    @app.get("/api/doctor")
    def doctor():
        return environment.snapshot()

    @app.get("/api/environment")
    def environment_status():
        return environment.snapshot()

    @app.get("/api/connections")
    def connection_status():
        return connections.snapshot()

    @app.post("/api/connections/connect")
    def connection_start(payload: dict):
        if not isinstance(payload.get("force", False), bool):
            raise ValueError("force 必须是布尔值")
        if runner.running:
            raise HTTPException(409, "采集运行中，暂不能执行环境检测。")
        connections.connect(force=payload.get("force", True), wait=True)
        return connections.snapshot()

    @app.post("/api/environment/check")
    def environment_check():
        return environment.check()

    @app.post("/api/environment/test")
    def environment_test(payload: dict):
        with control_lock:
            idle()
            check_id = payload.get("id")
            if not isinstance(check_id, str):
                raise ValueError("测试项 ID 无效")
            try:
                return environment.run_test(check_id)
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/environment/settings")
    def environment_open_settings(payload: dict):
        with control_lock:
            idle()
            pane = payload.get("pane")
            if not isinstance(pane, str):
                raise ValueError("系统设置项目无效")
            try:
                return environment.open_settings(pane)
            except RuntimeError as exc:
                raise HTTPException(400, str(exc)) from None

    @app.post("/api/test-model")
    def test_model():
        with control_lock:
            idle()
            snapshot = environment.run_test("model_service")
            check = next(item for item in snapshot["checks"] if item["id"] == "model_service")
            return {"ok": check["status"] == "ready", "message": check["summary"], "checked_at": check["checked_at"]}

    @app.get("/api/robot/profile")
    def robot_profile():
        return profiles.load()

    @app.post("/api/robot/profile")
    def save_robot_profile(payload: dict):
        with control_lock:
            idle()
            previous = profiles.load()
            result = profiles.save(payload)
            runner.invalidate_readiness()
            # 任务勾选变化不影响桌面权限探测；只有窗口目标/模式改变才让实测结果过期。
            relevant = ("mode", "wechat_selector", "miniapp_selector")
            if any(previous.get(key) != result.get(key) for key in relevant):
                environment.invalidate("uipath_desktop")
                connections.invalidate()
            return result

    @app.get("/api/debug/summary")
    def debug_summary():
        limit = configs.load().debug_logging.max_storage_mb
        return debug_log.summary(limit)

    @app.get("/api/debug/runs/{run_id}/export")
    def export_debug_run(run_id: str):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise HTTPException(400, "运行编号无效")
        handle = tempfile.NamedTemporaryFile(prefix="menu-capture-debug-", suffix=".zip", delete=False)
        path = Path(handle.name)
        handle.close()
        try:
            debug_log.export_run(run_id, path)
        except Exception:
            path.unlink(missing_ok=True)
            raise HTTPException(500, "调试包导出失败") from None
        return FileResponse(
            path, media_type="application/zip", filename=f"menu-capture-{run_id[:8]}-debug.zip",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @app.post("/api/debug/clear")
    def clear_debug_logs():
        with control_lock:
            idle()
            return debug_log.clear()
