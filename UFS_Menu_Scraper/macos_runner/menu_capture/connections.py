"""面向用户的连接生命周期；环境探针只提供事实，本层负责恢复、缓存与展示状态。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .environment import EnvironmentDiagnostics, _now


class ConnectionManager:
    MODEL_TTL = 86400
    DESKTOP_TTL = 120
    FAILURE_COOLDOWN = 60

    def __init__(self, environment: EnvironmentDiagnostics, control_lock: threading.RLock, root: Path):
        self.environment = environment
        self.runner = environment.runner
        self.control_lock = control_lock
        self.path = root / "data/connections.json"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._busy = False
        self._stage = "等待连接"
        self._next_check = 0.0
        self._events = deque(maxlen=80)
        self._worker: threading.Thread | None = None
        self._reset_attempted = False
        self._last_error = ""
        self._desktop_attempts = 0
        self._permission_signature = None
        self._restore()

    def _fingerprint(self) -> str:
        config = self.environment.configs
        model = config.load().model
        # 缓存仅保存不可逆摘要；密钥和接口请求不会进入诊断文件。
        value = json.dumps(model.model_dump(), sort_keys=True) + config.key()
        return hashlib.sha256(value.encode()).hexdigest()

    def _restore(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self._events.extend(data.get("events", [])[-80:])
            cached = data.get("model")
            if cached and cached.get("fingerprint") == self._fingerprint():
                age = time.time() - cached["epoch"]
                if 0 <= age < self.MODEL_TTL:
                    result = cached["result"]
                    result["at"] = time.monotonic() - age
                    self.environment._tests["model_service"] = result
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _persist(self) -> None:
        with self._lock, self.environment._lock:
            result = self.environment._tests.get("model_service")
            data = {"events": list(self._events)}
            if result:
                data["model"] = {"fingerprint": self._fingerprint(),
                                 "epoch": time.time() - (time.monotonic() - result["at"]), "result": result}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, self.path)

    def _event(self, stage: str, status: str, message: str) -> None:
        with self._lock:
            self._stage = stage
            self._events.append({"time": _now(), "stage": stage, "status": status, "message": message})
        self._persist()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def close(self) -> None:
        self._stop.set()

    def invalidate(self) -> None:
        with self._lock:
            self._next_check = 0

    def connect(self, force: bool = False, *, wait: bool = False, timeout: float = 180) -> None:
        worker = None
        with self._lock:
            if self._busy:
                worker = self._worker
            elif self._stop.is_set() or self.runner.running:
                return
            elif not force and time.monotonic() < self._next_check:
                return
            else:
                if force:
                    self._desktop_attempts = 0
                    self._reset_attempted = False
                    cached = self.environment._tests.get("model_service", {})
                    if cached.get("model_service", {}).get("status") != "ready":
                        self.environment.invalidate("model_service")
                self._busy = True
                self._last_error = ""
                self._stage = "发现本机环境"
                self._worker = threading.Thread(target=self._connect, name="capture-connections", daemon=True)
                self._worker.start()
                worker = self._worker
        # 用户主动发起检查时同步等这一轮结束；不需要前端靠定时轮询等待结果。
        if wait and worker and worker is not threading.current_thread():
            worker.join(timeout=max(0, float(timeout)))

    def _connect(self) -> None:
        failed = True
        try:
            # 与开始采集共用互斥锁，保证探测/恢复不会和真实桌面任务并发。
            with self.control_lock:
                if self.runner.running or self._stop.is_set():
                    return
                self._event("发现本机环境", "checking", "开始本轮连接检查；不会退出微信或重启正在执行的任务。")
                cheap = self.environment.check(include_task_checks=False)
                by_id = {c["id"]: c for c in cheap["checks"]}
                for check_id, app in (("wechat_process", "/Applications/WeChat.app"),
                                      ("uipath_assistant", str(Path.home() / "Applications/UiPath Assistant.app"))):
                    if by_id.get(check_id, {}).get("status") in {"error", "warning"} and Path(app).exists():
                        self._event("启动依赖", "checking", "正在打开微信" if check_id == "wechat_process" else "正在打开自动化引擎")
                        subprocess.run(["open", "-g", "-a", app], capture_output=True, timeout=10, check=False)
                if by_id.get("uipath_cli", {}).get("status") == "ready" and by_id.get("uipath_project", {}).get("status") == "ready":
                    permissions = self.environment.system_permissions() if hasattr(self.environment, "system_permissions") else {}
                    signature = tuple((key, value["status"]) for key, value in sorted(permissions.items()))
                    if signature != self._permission_signature:
                        self._permission_signature = signature
                        self._desktop_attempts = 0
                        self._reset_attempted = False
                    desktop = self.environment._tests.get("uipath_desktop", {})
                    healthy = desktop.get("uipath_accessibility", {}).get("status") == "ready"
                    self._event("桌面连接", "checking", "检测系统授权、执行进程、微信登录和实际截图能力。")
                    # 连续失败最多执行三轮；之后只轻量查询授权变化，避免不断启动执行器。
                    if healthy or self._desktop_attempts < 3:
                        self._desktop_attempts += 1
                        self.environment.run_test("uipath_desktop")
                    else:
                        self._event("桌面连接", "warning", "连续连接失败，已停止重复启动执行器；授权状态变化或点击重新连接后再尝试。")
                    desktop = self.environment._tests.get("uipath_desktop", {})
                    granted = desktop.get("uipath_system_accessibility", {}).get("status") == "ready"
                    failed_runtime = desktop.get("uipath_accessibility", {}).get("status") == "error"
                    if granted and failed_runtime and not self._reset_attempted and not self.runner.running:
                        self._reset_attempted = True
                        # 检测到其他执行器时无法判断其任务归属，保守跳过重置，绝不终止它。
                        processes = subprocess.run(["ps", "-axo", "pid,ppid,etime,command"], capture_output=True, text=True, timeout=5)
                        active = [line.strip() for line in processes.stdout.splitlines() if "UiPath.Executor.NetCore.dll" in line]
                        if active:
                            self._event("执行进程身份", "warning", "检测到现存执行器，未重置该进程：" + "；".join(active)[:1800])
                        else:
                            self._recover_host()
                if self.environment._model_configured() and not self._stop.is_set():
                    test = self.environment._tests.get("model_service", {})
                    cached = test.get("model_service", {})
                    age = time.monotonic() - test.get("at", 0)
                    # 重连不会重复消耗成功的视觉测试；配置变化会显式清空此缓存。
                    if not (cached and age < self.MODEL_TTL):
                        self._event("模型连接", "checking", "使用生成色块验证视觉输入能力；成功后缓存 24 小时。")
                        for attempt in range(3):
                            self.environment.run_test("model_service")
                            value = self.environment._tests["model_service"]["model_service"]
                            if value["status"] == "ready" or not value.get("transient") or attempt == 2:
                                break
                            self._event("模型重试", "checking", f"临时网络失败，退避后重试（{attempt + 1}/2）。")
                            if self._stop.wait(2 ** attempt):
                                return
                # busy 期间的聚合不覆盖已完成探针；以底层结果决定最终状态。
                modules = self._modules(self.environment.snapshot(include_task_checks=False), checking=False)
                failed = any(m["status"] != "ready" for m in modules)
                self._event("连接完成", "warning" if failed else "ready",
                            "存在需要处理的连接问题，已列出修复入口。" if failed else "微信、自动化引擎和 AI 模型已就绪。")
        except Exception as exc:
            self._last_error = f"连接流程异常：{type(exc).__name__}"
            self._event("连接失败", "error", f"连接流程异常：{type(exc).__name__}；请查看高级诊断。")
        finally:
            with self._lock:
                self._busy = False
                self._next_check = time.monotonic() + (self.FAILURE_COOLDOWN if failed else self.DESKTOP_TTL)

    def _recover_host(self) -> None:
        self._event("恢复辅助进程", "checking", "系统授权已开启但运行时访问失败，尝试一次官方 UIAutomation 宿主重连。")
        env = os.environ.copy()
        env["UIPATH_CLI_DISABLE_VERSION_SYNC"] = "true"
        reset = subprocess.run([shutil.which("uip") or "uip", "rpa", "uia", "reset", "--project-dir",
                                str(self.environment.project_dir), "--timeout", "12"],
                               capture_output=True, text=True, timeout=18, env=env, check=False)
        if reset.returncode == 0 and not self.runner.running and not self._stop.is_set():
            self.environment.run_test("uipath_desktop")
        else:
            self._event("恢复辅助进程", "warning", "辅助进程重连未成功；保留原始测试结果。")

    def _modules(self, raw: dict, *, checking: bool) -> list[dict]:
        checks = {c["id"]: c for c in raw["checks"]}
        reconnect = {"kind": "reconnect", "label": "重新连接"}

        def module(key, name, required):
            rows = [checks[r] for r in required if r in checks]
            bad = next((r for status in ("error", "warning", "unknown") for r in rows if r["status"] == status), None)
            status = bad["status"] if bad else "ready"
            if checking and status != "ready":
                status = "checking"
            return {"id": key, "name": name, "status": status,
                    "summary": bad["summary"] if bad else "连接正常", "detail": bad.get("detail", "") if bad else "实际能力检测通过。",
                    "checked_at": min((r["checked_at"] for r in rows), default=raw["checked_at"]),
                    "actions": [] if status in {"ready", "checking"} else [reconnect]}

        engine = module("engine", "自动化引擎", ["uipath_cli", "uipath_project", "uipath_accessibility", "uipath_screenshot"])
        system = checks.get("uipath_system_accessibility", {})
        if engine["status"] not in {"ready", "checking"}:
            if system.get("status") == "warning":
                engine.update(status="warning", summary="自动化引擎需要授权，完成后将自动重新检测。",
                              actions=[{"kind": "open_settings", "target": "accessibility", "label": "打开系统设置"}])
            elif system.get("status") == "ready":
                engine["summary"] = "系统授权已开启，但自动化执行连接失败。"
                engine["detail"] += "\n已区分 UIAutomation 宿主授权与 Robot 执行能力；不会要求反复开启同一个开关。"
        wechat_required = ["wechat_process", "wechat_login"]
        try:
            profile = json.loads(self.runner.profile_path.read_text(encoding="utf-8"))
            if profile.get("mode") == "automatic":
                wechat_required.append("file_transfer_assistant")
        except (AttributeError, OSError, ValueError):
            pass
        wechat = module("wechat", "微信", wechat_required)
        if wechat["status"] not in {"ready", "checking"}:
            if checks.get("wechat_process", {}).get("status") == "ready" and engine["status"] != "ready":
                wechat.update(status="unknown" if not checking else "checking", summary="微信已打开，等待自动化引擎确认登录状态。",
                              detail="自动化执行连接尚未成功，因此未能读取微信界面；微信进程存在不代表已经登录。", actions=[])
            elif checks.get("wechat_login", {}).get("status") == "warning":
                wechat.update(status="warning", summary="请在微信完成扫码登录，软件将自动重新检测。")
            elif "file_transfer_assistant" in wechat_required and checks.get("file_transfer_assistant", {}).get("status") == "warning":
                wechat.update(status="warning", summary="请在微信打开文件传输助手，软件将自动重新检测。")
        model = module("model", "AI 模型", ["model_service"])
        if not self.environment._model_configured():
            model.update(status="warning", summary="请先配置模型接口和密钥。", actions=[{"kind": "settings", "target": "model", "label": "配置模型"}])
        return [wechat, engine, model]

    def snapshot(self) -> dict:
        raw = self.environment.snapshot(include_task_checks=False)
        with self._lock:
            busy = self._busy
            modules = self._modules(raw, checking=busy)
            if self._last_error and not busy:
                engine = next(m for m in modules if m["id"] == "engine")
                engine.update(status="error", summary=self._last_error, actions=[{"kind": "reconnect", "label": "重新连接"}])
            status = "ready" if all(m["status"] == "ready" for m in modules) else "checking" if busy else next(
                (s for s in ("error", "warning", "unknown") if any(m["status"] == s for m in modules)), "unknown")
            blockers = [m["name"] + "：" + m["summary"] for m in modules if m["status"] != "ready"]
            return {"status": status, "busy": busy, "checked_at": raw["checked_at"],
                    "summary": "采集环境已就绪" if status == "ready" else "正在检查采集环境…" if busy else "连接需要处理，请查看下方提示。",
                    "modules": modules, "preflight": {"ready": not blockers, "blockers": blockers},
                    "diagnostics": {"stage": self._stage, "events": list(self._events), "checks": raw["checks"]}}
