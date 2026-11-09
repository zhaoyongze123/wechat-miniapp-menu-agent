from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ConfigStore
from .uipath_runner import FileRobotTransport, UiPathRunner


_STATUS = {"checking", "ready", "warning", "error", "unknown"}
_TEST_TTL_SECONDS = 15 * 60
_SETTINGS_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
    "screen_recording": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture",
    "automation": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Automation",
    "full_disk_access": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _item(
    check_id: str,
    name: str,
    status: str,
    summary: str,
    detail: str = "",
    *,
    checked_at: str | None = None,
    actions: list[dict[str, str]] | None = None,
) -> dict:
    if status not in _STATUS:
        raise ValueError("环境检查状态无效")
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "checked_at": checked_at or _now(),
        "summary": summary,
        "detail": detail,
        "actions": actions or [],
    }


class EnvironmentDiagnostics:
    """汇总便宜的本机检查，并缓存用户主动触发的模型/UiPath实测结果。"""

    def __init__(self, configs: ConfigStore, runner: UiPathRunner, project_dir: Path):
        self.configs, self.runner = configs, runner
        self.project_dir = Path(project_dir).expanduser().resolve()
        self._lock = threading.RLock()
        self._test_lock = threading.Lock()
        self._cheap_cache: tuple[float, dict, bool] | None = None
        self._tests: dict[str, dict] = {}

    def invalidate(self, test_id: str | None = None) -> None:
        with self._lock:
            self._cheap_cache = None
            if test_id:
                self._tests.pop(test_id, None)
            else:
                self._tests.clear()

    def snapshot(self, *, refresh: bool = False, include_task_checks: bool = True,
                 task_ids_override: list[str] | None = None) -> dict:
        with self._lock:
            now = time.monotonic()
            if not refresh and self._cheap_cache and now - self._cheap_cache[0] < 15 and self._cheap_cache[2] == include_task_checks:
                cheap = self._cheap_cache[1]
            else:
                cheap = self._collect_cheap(include_task_checks=include_task_checks)
                self._cheap_cache = (now, cheap, include_task_checks)
            checks = [dict(value) for value in cheap["checks"]]
            checks.extend(self._test_state(check_id, name, message, actions)
                          for check_id, name, message, actions in self._test_definitions())
            by_id = {check["id"]: check for check in checks}
            blockers = []
            if include_task_checks:
                readiness = self.runner.readiness(task_ids_override=task_ids_override)
                profile_item = self._profile_check(readiness)
                checks.append(profile_item)
                blockers = self._blockers(by_id, profile_item, readiness.get("mode", "attached"))
            return {
                "checked_at": cheap["checked_at"],
                "preflight": {"ready": not blockers, "blockers": blockers},
                "checks": checks,
            }

    def check(self, *, include_task_checks: bool = True) -> dict:
        return self.snapshot(refresh=True, include_task_checks=include_task_checks)

    def run_test(self, test_id: str) -> dict:
        if test_id not in {"model_service", "uipath_desktop", "wechat"}:
            raise ValueError("不支持此环境测试项")
        if self.runner.running:
            raise RuntimeError("采集运行中，不能同时执行环境测试")
        # 耗时的桌面/网络测试不占状态锁，前端仍能取得“连接中”状态。
        with self._test_lock:
            if test_id == "model_service":
                result = self._test_model()
            else:
                result = self._test_uipath_and_wechat()
        with self._lock:
            self._tests[test_id] = result
            if test_id == "wechat":
                self._tests["uipath_desktop"] = result
            if test_id == "uipath_desktop":
                self._tests["wechat"] = result
            self._cheap_cache = None
        return self.snapshot(refresh=True, include_task_checks=False)

    @staticmethod
    def open_settings(pane: str) -> dict:
        if pane not in _SETTINGS_URLS:
            raise ValueError("不支持打开此系统设置页面")
        if sys.platform != "darwin":
            raise RuntimeError("此系统设置入口只适用于 macOS")
        result = subprocess.run(
            ["open", _SETTINGS_URLS[pane]],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            # 深链在部分 macOS 版本上会变化，退回系统设置应用，不尝试修改授权。
            fallback = subprocess.run(["open", "-a", "System Settings"], capture_output=True, timeout=8, check=False)
            if fallback.returncode != 0:
                raise RuntimeError("无法打开系统设置；请从 Apple 菜单手动打开“系统设置 > 隐私与安全性”")
            return {"ok": True, "message": "已打开系统设置；请在“隐私与安全性”中选择对应项目"}
        return {"ok": True, "message": "已打开对应系统隐私设置；授权需由你在系统设置中完成"}

    @staticmethod
    def _automation_permission_unknown(check_id: str, name: str) -> dict:
        """macOS 的 Automation 授权按目标应用记录，不能从另一进程可靠读取。"""
        actions = ([{"id": "open_settings", "label": "查看自动化设置", "kind": "open_settings", "target": "automation"}]
                   if sys.platform == "darwin" else [])
        return _item(
            check_id, name, "unknown", "无法直接读取本机服务的自动化控制授权",
            "控制台服务不负责操控微信；macOS 不提供稳定的跨进程授权查询接口。系统设置可查看授权对象；只读 UiPath 测试不模拟点击，因此不会把它误报为已授权。",
            actions=actions,
        )

    def assert_preflight(self, task_ids_override: list[str] | None = None) -> dict:
        """启动任务前刷新静态状态，并要求近期真实 UI 与模型测试通过。"""
        snapshot = self.snapshot(refresh=True, task_ids_override=task_ids_override)
        if not snapshot["preflight"]["ready"]:
            raise RuntimeError("运行前检查未通过：" + "；".join(snapshot["preflight"]["blockers"]))
        return snapshot

    def _collect_cheap(self, *, include_task_checks: bool = True) -> dict:
        checked_at = _now()
        settings = self.configs.load()
        checks = [
            self._permission_check("python_accessibility", "辅助功能（本机配置服务）", "accessibility"),
            self._permission_check("python_screen_recording", "屏幕录制（本机配置服务）", "screen_recording"),
            self._automation_permission_unknown("python_automation", "自动化控制（本机配置服务）"),
            self._wechat_process_check(),
            self._assistant_process_check(),
            self._cli_check(),
            self._project_check(),
        ]
        if include_task_checks:
            checks.extend([
                self._directory_check("input_directory", "素材目录读取", settings.input_dir, writable=False),
                self._directory_check("output_directory", "输出目录写入", settings.output_dir, writable=True),
            ])
        return {"checked_at": checked_at, "checks": checks}

    @staticmethod
    def _permission_check(check_id: str, name: str, permission: str) -> dict:
        actions = ([{"id": "open_settings", "label": "打开系统设置", "kind": "open_settings",
                     "target": "accessibility" if permission == "accessibility" else "screen_recording"}]
                   if sys.platform == "darwin" else [])
        if sys.platform != "darwin":
            return _item(check_id, name, "unknown", "当前平台不支持此权限检测", "本版本的桌面权限诊断面向 macOS。", actions=actions)
        try:
            from .macos import MacDesktop
            state = MacDesktop().permissions().get(permission)
        except Exception as exc:
            return _item(check_id, name, "unknown", "无法读取本机服务进程权限", type(exc).__name__, actions=actions)
        if state is None:
            return _item(check_id, name, "unknown", "系统未提供此权限状态", "该结果仅代表运行控制台的 Python 进程，不代表 UiPath。", actions=actions)
        return _item(
            check_id, name, "ready" if state else "warning",
            "本机 Python 进程已授权" if state else "本机 Python 进程未授权",
            "这是控制台服务进程的状态；UiPath UIAutomation 权限需通过 UiPath 测试单独确认。",
            actions=actions,
        )

    @staticmethod
    def _process_names() -> list[str] | None:
        if sys.platform != "darwin":
            return None
        try:
            result = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.splitlines() if result.returncode == 0 else None

    def _wechat_process_check(self) -> dict:
        actions = [{"id": "test_wechat", "label": "检测微信状态", "kind": "test", "target": "wechat"}]
        if sys.platform != "darwin":
            return _item("wechat_process", "微信客户端", "unknown", "当前平台不支持微信进程检测", actions=actions)
        try:
            from Cocoa import NSWorkspace
            running = any(
                app.bundleIdentifier() == "com.tencent.xinWeChat"
                for app in NSWorkspace.sharedWorkspace().runningApplications()
            )
        except Exception:
            names = self._process_names()
            if names is None:
                return _item("wechat_process", "微信客户端", "unknown", "无法读取微信进程状态", actions=actions)
            running = any("wechat" in Path(value.strip()).name.casefold() or "xinwechat" in value.casefold() for value in names)
        return _item("wechat_process", "微信客户端", "ready" if running else "error",
                     "检测到微信正在运行" if running else "未检测到微信进程",
                     "进程存在不等于已登录；登录状态请运行下方的微信界面测试。", actions=actions)

    def _assistant_process_check(self) -> dict:
        actions = [{"id": "test_uipath", "label": "测试 UiPath 桌面能力", "kind": "test", "target": "uipath_desktop"}]
        names = self._process_names()
        if names is None:
            return _item("uipath_assistant", "UiPath Assistant", "unknown", "无法确认 Assistant 是否运行", actions=actions)
        found = any("uipath" in Path(value.strip()).name.casefold() and "assistant" in Path(value.strip()).name.casefold() for value in names)
        return _item("uipath_assistant", "UiPath Assistant", "ready" if found else "warning",
                     "检测到 UiPath Assistant 进程" if found else "未检测到 UiPath Assistant 进程",
                     "实际 UIAutomation 能力仍以 UiPath 桌面测试结果为准。", actions=actions)

    @staticmethod
    def _cli_check() -> dict:
        actions = [{"id": "test_uipath", "label": "测试 UiPath 桌面能力", "kind": "test", "target": "uipath_desktop"}]
        cli = shutil.which("uip")
        if not cli:
            return _item("uipath_cli", "UiPath CLI", "error", "未找到 uip 命令", "请安装 UiPath CLI 并确保它位于服务 PATH 中。", actions=actions)
        try:
            result = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _item("uipath_cli", "UiPath CLI", "error", "UiPath CLI 无法启动", type(exc).__name__, actions=actions)
        if result.returncode != 0:
            return _item("uipath_cli", "UiPath CLI", "error", "UiPath CLI 版本检查失败", "命令存在，但未能正常返回版本。", actions=actions)
        version = (result.stdout or result.stderr).strip().splitlines()
        return _item("uipath_cli", "UiPath CLI", "ready", "UiPath CLI 可运行",
                     (version[0][:120] if version else "版本命令正常返回"), actions=actions)

    def _project_check(self) -> dict:
        actions = [{"id": "test_uipath", "label": "测试 UiPath 桌面能力", "kind": "test", "target": "uipath_desktop"}]
        main = self.project_dir / "Main.xaml"
        project = self.project_dir / "project.json"
        if not main.is_file() or not project.is_file():
            return _item("uipath_project", "UiPath 项目文件", "error", "缺少 Main.xaml 或 project.json", str(self.project_dir), actions=actions)
        try:
            json.loads(project.read_text(encoding="utf-8"))
            if not os.access(main, os.R_OK):
                raise OSError("Main.xaml 不可读")
        except (OSError, ValueError):
            return _item("uipath_project", "UiPath 项目文件", "error", "UiPath 项目文件不可读或 project.json 无效", str(self.project_dir), actions=actions)
        return _item("uipath_project", "UiPath 项目文件", "ready", "Main.xaml 与项目配置可读取", str(self.project_dir), actions=actions)

    @staticmethod
    def _directory_check(check_id: str, name: str, configured: str, *, writable: bool) -> dict:
        actions = [{"id": "recheck", "label": "重新检测", "kind": "recheck"}]
        if not configured.strip():
            return _item(check_id, name, "error", "未配置目录路径", "请在配置页填写本机绝对路径。", actions=actions)
        path = Path(configured).expanduser()
        try:
            if writable:
                path.mkdir(parents=True, exist_ok=True)
                if not path.is_dir():
                    raise NotADirectoryError("目标不是目录")
                fd, temp_name = tempfile.mkstemp(prefix=".menu-capture-check-", dir=path)
                try:
                    os.write(fd, b"ok")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                    Path(temp_name).unlink(missing_ok=True)
                summary = "目录可写；临时探测文件已删除"
            else:
                with os.scandir(path) as entries:
                    next(entries, None)
                summary = "目录存在且可读取"
        except OSError as exc:
            reason = "无法写入目录" if writable else "目录不存在或无法读取"
            return _item(check_id, name, "error", reason, f"{type(exc).__name__}: {str(exc)[:200]}", actions=actions)
        return _item(check_id, name, "ready", summary, str(path.resolve()), actions=actions)

    @staticmethod
    def _test_definitions() -> tuple[tuple[str, str, str, list[dict[str, str]]], ...]:
        return (
            ("uipath_system_accessibility", "UiPath 系统辅助功能授权", "官方 CLI 查询的宿主授权状态；与实际运行能力分开记录。",
             [{"id": "open_settings", "label": "打开辅助功能设置", "kind": "open_settings", "target": "accessibility"}]),
            ("uipath_system_screen_recording", "UiPath 系统屏幕录制授权", "官方 CLI 查询的宿主授权状态；与实际截图能力分开记录。",
             [{"id": "open_settings", "label": "打开屏幕录制设置", "kind": "open_settings", "target": "screen_recording"}]),
            ("model_service", "视觉模型多模态请求", "将发送程序生成的测试色块图；不会发送素材或桌面截图。",
             [{"id": "test_model", "label": "测试模型连接", "kind": "test", "target": "model_service"}]),
            ("uipath_accessibility", "辅助功能（UiPath UIAutomation）", "仅通过 UiPath 实际窗口枚举测试判断；不使用 Python 服务状态代替。",
             [{"id": "test_uipath", "label": "测试 UiPath 桌面能力", "kind": "test", "target": "uipath_desktop"},
              {"id": "open_settings", "label": "打开辅助功能设置", "kind": "open_settings", "target": "accessibility"}]),
            ("uipath_screenshot", "屏幕录制（UiPath）", "仅在 UiPath 成功保存并校验临时窗口截图后标记正常。",
             [{"id": "test_uipath", "label": "测试截图能力", "kind": "test", "target": "uipath_desktop"},
              {"id": "open_settings", "label": "打开屏幕录制设置", "kind": "open_settings", "target": "screen_recording"}]),
            ("uipath_automation", "自动化控制（UiPath → 微信）", "macOS 不允许配置服务直接读取另一个进程的 TCC 授权状态；这里不会把 Python 权限当成 UiPath 已授权。",
             [{"id": "open_settings", "label": "查看自动化设置", "kind": "open_settings", "target": "automation"}]),
            ("wechat_login", "微信登录状态", "需通过微信窗口本地 OCR 判断；仅检测到进程不能证明已登录。",
             [{"id": "test_wechat", "label": "检测微信状态", "kind": "test", "target": "wechat"}]),
            ("file_transfer_assistant", "文件传输助手窗口", "自动打开模式需要当前聊天窗口为文件传输助手。",
             [{"id": "test_wechat", "label": "检测微信状态", "kind": "test", "target": "wechat"}]),
            ("attached_miniapp", "已打开的小程序窗口", "仅单店接管模式需要；自动打开模式会在扫码后发现新窗口。",
             [{"id": "test_uipath", "label": "测试小程序窗口", "kind": "test", "target": "uipath_desktop"}]),
        )

    def _test_state(self, check_id: str, name: str, explanation: str, actions: list[dict[str, str]]) -> dict:
        permissions = getattr(self, "_permissions_cache", None)
        if permissions and check_id in permissions[1]:
            value = permissions[1][check_id]
            return _item(check_id, name, value["status"], value["summary"], value.get("detail", ""),
                         checked_at=value.get("checked_at"), actions=actions)
        cached = self._tests.get("model_service" if check_id == "model_service" else "uipath_desktop")
        field = check_id
        if check_id == "model_service":
            configured = self._model_configured()
            if not configured:
                return _item(check_id, name, "error", "模型接口、模型名或密钥未配置", "请先完成配置，再运行多模态连接测试。", actions=actions)
        if cached and field in cached:
            value = cached[field]
            age = time.monotonic() - cached["at"]
            if age <= (86400 if check_id == "model_service" else _TEST_TTL_SECONDS):
                return _item(check_id, name, value["status"], value["summary"], value.get("detail", explanation), checked_at=cached["checked_at"], actions=actions)
            return _item(check_id, name, "unknown", "上次实测结果已过期", f"{value['summary']}。请重新运行测试。", checked_at=cached["checked_at"], actions=actions)
        if check_id == "model_service" and self._model_configured():
            return _item(check_id, name, "unknown", "配置已填写，尚未验证实际多模态请求", explanation, actions=actions)
        return _item(check_id, name, "unknown", "尚未实测", explanation, actions=actions)

    def _model_configured(self) -> bool:
        settings = self.configs.load()
        return bool(settings.model.base_url and settings.model.model and self.configs.key())

    @staticmethod
    def _profile_check(readiness: dict) -> dict:
        if readiness.get("ready"):
            return _item("capture_profile", "采集任务与 UiPath 配置", "ready", "当前选择、来源文件和自动化参数通过静态检查",
                         "开始时仍会重新核对一次任务状态与来源文件。")
        issues = readiness.get("issues") or ["采集配置未就绪"]
        return _item("capture_profile", "采集任务与 UiPath 配置", "error", "；".join(str(value) for value in issues)[:400],
                     "请检查自动化方式、已选门店、窗口选择器和坐标校准。")

    def _blockers(self, checks: dict[str, dict], profile: dict, mode: str) -> list[str]:
        required = ["input_directory", "output_directory", "model_service", "uipath_accessibility", "uipath_screenshot", "wechat_login"]
        if mode == "automatic":
            required.append("file_transfer_assistant")
        elif mode == "attached":
            required.append("attached_miniapp")
        required.append("capture_profile")
        blockers: list[str] = []
        for check_id in required:
            value = checks.get(check_id) or profile
            if value.get("status") != "ready":
                blockers.append(f"{value.get('name', check_id)}：{value.get('summary', '尚未就绪')}")
        return blockers

    def _test_model(self) -> dict:
        checked_at = _now()
        result = {"at": time.monotonic(), "checked_at": checked_at}
        try:
            from .model import ModelError, VisionModel
            model = VisionModel(self.configs.load().model, self.configs.key())
            if hasattr(model, "usage_ledger"):
                model.usage_ledger = getattr(self.runner, "usage_ledger", None)
            try:
                tested = model.test()
            finally:
                model.close()
            status = "ready" if tested.get("ok") else "error"
            result["model_service"] = {"status": status, "summary": tested.get("message", "模型多模态测试未通过"),
                                       "detail": "使用程序生成的色块图进行视觉识别；未发送用户素材或屏幕图像。"}
        except Exception as exc:
            # 异常文本可能包含带查询参数的接口地址；不把原始异常内容写进页面。
            safe_message = str(exc) if isinstance(exc, ModelError) else ""
            transient = "模型网络请求失败" in safe_message or any(f"HTTP {code}" in safe_message for code in (408, 429, 500, 502, 503, 504))
            result["model_service"] = {
                "status": "error", "summary": "模型多模态请求失败",
                "detail": f"错误类型：{type(exc).__name__}。{safe_message} 请检查接口地址、认证信息、模型名称和服务可达性。",
                "transient": transient,
            }
        return result

    def _test_uipath_and_wechat(self) -> dict:
        checked_at = _now()
        result: dict[str, Any] = {"at": time.monotonic(), "checked_at": checked_at}
        temp = None
        work = None
        process = None
        transport = None
        log_stream = None
        permissions = self.system_permissions()
        result.update(permissions)
        try:
            if sys.platform != "darwin":
                raise RuntimeError("UiPath 桌面诊断当前仅支持 macOS")
            cli = shutil.which("uip")
            if not cli:
                raise RuntimeError("未找到 UiPath CLI（uip）")
            temp = tempfile.TemporaryDirectory(prefix="menu-capture-uipath-check-")
            work = Path(temp.name)
            capture_dir = work / "captures"
            capture_dir.mkdir(mode=0o700)
            args_path = work / "args.json"
            args_path.write_text(json.dumps({"in_RunDirectory": str(work)}, ensure_ascii=False), encoding="utf-8")
            args_path.chmod(0o600)
            log_stream = (work / "cli.log").open("wb")
            command = [cli, "rpa", "run", "--project-dir", str(self.project_dir), "--file-path", "Main.xaml",
                       "--input-arguments-file", str(args_path), "--timeout", "90", "--output", "json"]
            environment = os.environ.copy()
            dotnet_root = Path.home() / ".dotnet"
            if dotnet_root.is_dir():
                environment["DOTNET_ROOT"] = str(dotnet_root)
                environment["PATH"] = str(dotnet_root) + os.pathsep + environment.get("PATH", "")
            environment["UIPATH_CLI_DISABLE_VERSION_SYNC"] = "true"
            process = self.runner.process_factory(command, cwd=str(self.project_dir), env=environment,
                                                  stdin=subprocess.DEVNULL, stdout=log_stream, stderr=subprocess.STDOUT)
            transport = FileRobotTransport(work, capture_dir, poll_interval=0.05)
            discovered = transport.run({"action": "discover"}, timeout=35, process=process)
            windows = UiPathRunner._window_list(discovered.get("windows"))
            if not windows:
                result["uipath_accessibility"] = {
                    "status": "warning", "summary": "UiPath 已连接，但没有枚举到可用桌面窗口",
                    "detail": "请确认微信已打开，并在 UiPath 实测时保持桌面未锁定。",
                }
            else:
                result["uipath_accessibility"] = {
                    "status": "ready", "summary": "UiPath 成功枚举桌面窗口",
                    "detail": "这是 UiPath UIAutomation 实测；不引用 Python 服务进程权限。",
                }
            profile = self.runner.readiness().get("profile", {})
            mode = profile.get("mode", "automatic")
            wechat_selector = str(profile.get("wechat_selector") or "")
            if not wechat_selector:
                wechat_selector = self._find_wechat_selector(windows)
            if not wechat_selector:
                result["uipath_screenshot"] = {"status": "warning", "summary": "窗口枚举成功，但没有可截图的微信主窗口",
                                                "detail": "请打开微信主窗口后重新测试。"}
                result["wechat_login"] = {"status": "unknown", "summary": "无法确认微信登录状态", "detail": "没有可识别的微信主窗口。"}
                result["file_transfer_assistant"] = {"status": "unknown", "summary": "无法确认文件传输助手", "detail": "没有可识别的微信主窗口。"}
            else:
                wechat_path = capture_dir / "wechat-diagnostic.png"
                screenshot = transport.run({"action": "screenshot", "selector": wechat_selector, "path": str(wechat_path)}, timeout=25, process=process)
                quality = self._screenshot_content_quality(wechat_path)
                if quality["near_black"]:
                    detail = (
                        f"截图命令返回 {screenshot['width']}×{screenshot['height']} 图片，但像素近乎全黑 "
                        f"（暗像素占比 {quality['dark_fraction']:.1%}）。窗口枚举成功不代表像素已捕获；"
                        "请检查 UiPath 的屏幕录制权限及权限变更后的 Assistant 重启状态。"
                    )
                    result["uipath_screenshot"] = {"status": "error", "summary": "UiPath 返回了近乎全黑的微信截图", "detail": detail}
                    result["wechat_login"] = {"status": "unknown", "summary": "截图画面为黑帧，无法判断微信登录状态", "detail": detail}
                    result["file_transfer_assistant"] = {"status": "unknown", "summary": "截图画面为黑帧，无法确认文件传输助手", "detail": detail}
                else:
                    result["uipath_screenshot"] = {"status": "ready", "summary": "UiPath 成功截取微信窗口",
                                                    "detail": f"临时截图 {screenshot['width']}×{screenshot['height']}；像素内容可读；测试结束自动删除，不进入采集输出。"}
                    try:
                        from .image_tools import image_ocr
                        rows = image_ocr(wechat_path)
                        login, assistant = self._classify_wechat_screen(rows, screenshot["width"], screenshot["height"])
                        result["wechat_login"] = login
                        result["file_transfer_assistant"] = assistant
                    except Exception as exc:
                        result["wechat_login"] = {"status": "unknown", "summary": "截图成功，但本地 OCR 无法判断登录状态", "detail": type(exc).__name__}
                        result["file_transfer_assistant"] = {"status": "unknown", "summary": "本地 OCR 无法确认文件传输助手", "detail": type(exc).__name__}
                if mode == "attached":
                    try:
                        miniapp_selector = UiPathRunner._select_miniapp_selector(windows)
                        miniapp_path = capture_dir / "miniapp-diagnostic.png"
                        miniapp = transport.run({"action": "screenshot", "selector": miniapp_selector, "path": str(miniapp_path)}, timeout=25, process=process)
                        miniapp_quality = self._screenshot_content_quality(miniapp_path)
                        if miniapp_quality["near_black"]:
                            result["attached_miniapp"] = {"status": "error", "summary": "小程序窗口返回近乎全黑截图",
                                                           "detail": "窗口已找到，但截图像素近乎全黑；请检查 UiPath 屏幕录制能力。"}
                        else:
                            result["attached_miniapp"] = {"status": "ready", "summary": "检测到已打开的小程序窗口并成功截图",
                                                           "detail": f"临时截图 {miniapp['width']}×{miniapp['height']}；像素内容可读，图像不会保留。"}
                    except Exception as exc:
                        result["attached_miniapp"] = {"status": "error", "summary": "没有可唯一识别并截图的小程序窗口",
                                                       "detail": str(exc)[:240]}
            result["uipath_automation"] = {"status": "unknown", "summary": "控制授权状态未单独读取",
                                            "detail": "本次探测执行窗口枚举和截图，不点击页面；Automation 授权由 macOS 控制，需在系统设置查看。"}
        except Exception as exc:
            message = str(exc)
            if work is not None:
                try:
                    log_text = (work / "cli.log").read_text(encoding="utf-8", errors="replace")
                    for marker in ("请允许 UiPath UIAutomation", "Source:", "Message:"):
                        index = log_text.rfind(marker)
                        if index >= 0:
                            fragment = log_text[index:index + 360]
                            if "请允许 UiPath UIAutomation" in fragment:
                                message = fragment.replace("\\r", "\n").replace("\\n", "\n").splitlines()[0]
                                break
                            if "Message:" in fragment:
                                message = fragment.split("Message:", 1)[1].replace("\\r", "\n").replace("\\n", "\n").splitlines()[0].strip()
                                break
                except OSError:
                    pass
            permission_failure = "访问权限控制此计算机" in message or "辅助功能" in message or "Accessibility" in message
            system_granted = permissions.get("uipath_system_accessibility", {}).get("status") == "ready"
            status = "error" if permission_failure else "warning"
            result.setdefault("uipath_accessibility", {"status": status,
                           "summary": ("系统授权已开启，但自动化执行进程访问失败" if system_granted else "UiPath 执行进程报告辅助功能访问失败") if permission_failure else "UiPath 桌面枚举测试失败",
                           "detail": ("系统授权和实际执行属于不同检测层级；请检查运行进程身份及授权后仍在运行的旧进程。\n" if permission_failure else "") + (message[:500] or type(exc).__name__)})
            result.setdefault("uipath_screenshot", {"status": "unknown", "summary": "未能执行截图测试",
                           "detail": "请先通过 UiPath 窗口枚举测试。"})
            result.setdefault("uipath_automation", {"status": "unknown", "summary": "未能验证 UiPath 对微信的控制能力",
                           "detail": "请检查 macOS 隐私设置后重新测试。"})
            result.setdefault("wechat_login", {"status": "unknown", "summary": "未能确认微信登录状态", "detail": message[:240]})
            result.setdefault("file_transfer_assistant", {"status": "unknown", "summary": "未能确认文件传输助手", "detail": message[:240]})
            if self.runner.readiness().get("mode") == "attached":
                result.setdefault("attached_miniapp", {"status": "unknown", "summary": "未能检测已打开的小程序窗口", "detail": "UiPath 桌面测试未通过。"})
        finally:
            if transport is not None:
                try:
                    transport.request_stop()
                except Exception:
                    pass
            if process is not None:
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=4)
                except Exception:
                    pass
            if log_stream is not None:
                log_stream.close()
            if temp is not None:
                temp.cleanup()
        return result

    @staticmethod
    def _screenshot_content_quality(path: Path) -> dict[str, Any]:
        """窗口截图命令可能成功但只返回黑帧；用小缩略图判断像素是否真的到达。"""
        from PIL import Image

        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail((64, 64))
            pixels = [image.getpixel((x, y)) for y in range(image.height) for x in range(image.width)]
        if not pixels:
            return {"near_black": True, "dark_fraction": 1.0, "mean_luma": 0.0}
        dark_fraction = sum(1 for pixel in pixels if max(pixel) <= 12) / len(pixels)
        mean_luma = sum(sum(pixel) / 3 for pixel in pixels) / len(pixels)
        return {
            "near_black": dark_fraction >= 0.985 and mean_luma <= 12,
            "dark_fraction": dark_fraction,
            "mean_luma": mean_luma,
        }

    def system_permissions(self) -> dict:
        """通过官方 CLI 查询 UIAutomation 宿主自己的权限，不能代替 Robot 执行能力。"""
        cache = getattr(self, "_permissions_cache", None)
        if cache and time.monotonic() - cache[0] < 10:
            return cache[1]
        fields = (("uipath_system_accessibility", "UiPath 系统辅助功能授权", "辅助功能权限"),
                  ("uipath_system_screen_recording", "UiPath 系统屏幕录制授权", "屏幕录制权限"))
        try:
            env = os.environ.copy()
            env["UIPATH_CLI_DISABLE_VERSION_SYNC"] = "true"
            completed = subprocess.run([shutil.which("uip") or "uip", "rpa", "uia", "permissions", "get",
                                        "--project-dir", str(self.project_dir), "--timeout", "12"],
                                       capture_output=True, text=True, timeout=18, env=env, check=False)
            output = completed.stdout.strip()
            values = {}
            for key, name, label in fields:
                lines = [line for line in output.splitlines() if label in line]
                line = lines[-1] if lines else ""
                enabled = "启用" in line and "未启用" not in line and "禁用" not in line
                disabled = "未启用" in line or "禁用" in line
                values[key] = {"status": "ready" if enabled else "warning" if disabled else "unknown",
                               "checked_at": _now(),
                               "summary": line or "官方 CLI 未返回可识别的权限状态",
                               "detail": "检测范围：UiPath UIAutomation 宿主。Robot 执行能力另行实测。"}
            self._permissions_cache = (time.monotonic(), values)
            return values
        except (OSError, subprocess.TimeoutExpired):
            return {key: {"status": "unknown", "summary": "未能查询 UiPath 系统授权", "detail": "官方 CLI 查询未完成；实际桌面能力另行实测。"} for key, _, _ in fields}

    @staticmethod
    def _find_wechat_selector(windows: list[dict]) -> str:
        candidates = []
        for window in windows:
            if not isinstance(window, dict):
                continue
            title = str(window.get("title") or "").strip()
            app = str(window.get("app") or "")
            app_name = app.casefold()
            if not title or ("wechat" not in app_name and "xinwechat" not in app_name and "微信" not in app):
                continue
            if title in {"图片和视频", "聊天记录"}:
                continue
            position = window.get("position") or {}
            width, height = position.get("width", 0), position.get("height", 0)
            # 小程序窗口通常是竖屏；微信聊天主窗口通常为横向。用窗口形状排除小程序，
            # 再优先选出文件传输助手会话，避免把小程序误当成微信主窗口来判断登录。
            if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 100 and height > 100 and width < height * 0.8:
                continue
            priority = 2 if title in {"微信", "WeChat", "文件传输助手"} else 1
            area = width * height if isinstance(width, (int, float)) and isinstance(height, (int, float)) else 0
            candidates.append((priority, area, window, title, app))
        if not candidates:
            return ""
        _, _, window, title, app = max(candidates, key=lambda item: (item[0], item[1]))
        attrs = {"app": app or "WeChat", "title": title}
        if window.get("role"):
            attrs["role"] = str(window["role"])
        import xml.etree.ElementTree as ET
        return ET.tostring(ET.Element("wnd", attrs), encoding="unicode", short_empty_elements=True)

    @staticmethod
    def _classify_wechat_screen(rows: list[dict], width: int, height: int) -> tuple[dict, dict]:
        text = " ".join(str(item.get("text", "")) for item in rows).replace(" ", "").replace("\u3000", "")
        if "扫码登录" in text or "扫码登陆" in text:
            login = {"status": "error", "summary": "微信当前未登录，需要手机扫码登录", "detail": "检测到微信扫码登录页面。"}
            assistant = {"status": "warning", "summary": "微信未登录，无法确认文件传输助手", "detail": "登录后打开文件传输助手并重新测试。"}
            return login, assistant
        if not text:
            unknown = {"status": "unknown", "summary": "OCR 未识别到微信界面文字", "detail": "无法据此判断登录状态。"}
            return unknown, dict(unknown)
        header = any(
            "文件传输助手" in str(item.get("text", "")).replace(" ", "")
            and float(item.get("x", 0)) > width * 0.3
            and float(item.get("y", height)) < height * 0.14
            for item in rows
        )
        if header:
            return ({"status": "ready", "summary": "微信已登录", "detail": "检测到文件传输助手聊天页标题。"},
                    {"status": "ready", "summary": "文件传输助手当前已打开", "detail": "自动打开模式可直接使用当前会话。"})
        if any(token in text for token in ("通讯录", "朋友圈", "文件传输助手", "微信支付")) or ("聊天" in text and "登录" not in text):
            return ({"status": "ready", "summary": "检测到已登录的微信主界面", "detail": "未发现扫码登录提示。"},
                    {"status": "warning", "summary": "当前不是文件传输助手会话", "detail": "自动打开模式请先切换到文件传输助手。"})
        unknown = {"status": "unknown", "summary": "微信窗口存在，但无法确认登录状态", "detail": "当前页面不符合已知登录或扫码登录界面。"}
        return unknown, dict(unknown)

    @staticmethod
    def _model_test_actions():
        return [{"id": "test_model", "label": "测试模型连接", "kind": "test", "target": "model_service"}]

