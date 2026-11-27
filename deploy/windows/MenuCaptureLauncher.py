"""Windows 统一启动器：Velopack 生命周期、单实例、Worker 守护与受控更新交接。

启动顺序（不可调整）：
1. `VelopackApp().set_auto_apply_on_startup(False).run()` 必须最先执行；
2. 再解析数据目录、获取单实例互斥、检查端口冲突；
3. 最后才启动业务 Worker。

恢复主体不是启动器本身：更新前把一次性 PowerShell 恢复协调器部署到 current 之外并等待就绪，
启动器只负责停止业务、交接官方 updater、以及新版本健康验证；协调器在新启动器完全无法启动时
仍能诊断、回滚或保留 recovery_required。
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PACK_ID = "Yipu.MenuCapture"
REPOSITORY = "zhaoyongze123/wechat-miniapp-menu-agent"
DEFAULT_PORT = 8765
MUTEX_PREFIX = "Local\\MenuCaptureLauncher-"
WORKER_STOP_TIMEOUT = 120
WORKER_START_TIMEOUT = 180
WATCHDOG_READY_TIMEOUT = 45
APPLY_STALL_SECONDS = 20 * 60
LOG_MAX_BYTES = 1024 * 1024


def _install_root() -> Path:
    # PyInstaller onedir 的 exe 位于 current 根；安装根是 current 的上一级。
    return Path(sys.executable).resolve().parent.parent


def _current_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _data_root() -> Path:
    override = str(os.environ.get("MENU_CAPTURE_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MenuCapture"


def _token_path(data_root: Path) -> Path:
    return data_root / "data" / "launcher-token"


def _write_token(data_root: Path, token: str) -> None:
    path = _token_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="ascii")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _log_path(data_root: Path) -> Path:
    logs = data_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "launcher.log"


def _log(data_root: Path, message: str) -> None:
    path = _log_path(data_root)
    try:
        if path.is_file() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".previous"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _message(text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "菜单采集工作台", 0x10)
    except Exception:  # noqa: BLE001 - 无桌面会话时不阻塞启动
        pass


# ---------- Velopack 生命周期 ----------

def _run_velopack_hooks(data_root: Path) -> None:
    import velopack

    (velopack.App()
        .set_auto_apply_on_startup(False)
        .on_first_run(lambda *_a, **_k: _ensure_autostart(data_root))
        .on_restarted(lambda *_a, **_k: _log(data_root, "Velopack 更新后重启"))
        .run())


# ---------- 单实例与端口 ----------

_KERNEL32 = None


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = kernel32
    return _KERNEL32


def _acquire_mutex() -> tuple[bool, int]:
    kernel32 = _kernel32()
    name = f"{MUTEX_PREFIX}{os.environ.get('USERNAME') or 'default'}"
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False, 0
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    return (not already), handle


def _close_handle(handle) -> None:
    try:
        _kernel32().CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _ensure_autostart(data_root: Path) -> None:
    """首次运行创建登录启动快捷方式；启动器自身保持幂等，不重复注册任务。"""
    startup = Path(os.environ.get("APPDATA") or "") / "Microsoft/Windows/Start Menu/Programs/Startup"
    if not startup.is_dir():
        return
    link = startup / "菜单采集后台.lnk"
    # Velopack 在安装根生成的 stub 文件名跟随 --packTitle（菜单采集工作台.exe），
    # 兼容直接以 MenuCapture.exe 命名或回退到 current/MenuCapture.exe。
    target = next(
        (candidate for candidate in (
            _install_root() / "菜单采集工作台.exe",
            _install_root() / "MenuCapture.exe",
            _current_dir() / "MenuCapture.exe",
        ) if candidate.is_file()),
        None,
    )
    if link.exists() or target is None:
        return
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
        "$s.TargetPath='{target}';$s.WorkingDirectory='{root}';$s.Save()"
    ).format(link=str(link), target=str(target), root=str(_install_root()))
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            check=False, creationflags=0x08000000, timeout=30,
        )
        _log(data_root, f"已创建登录启动快捷方式：{link}")
    except Exception as exc:  # noqa: BLE001 - 启动失败不阻止程序运行
        _log(data_root, f"创建登录启动快捷方式失败：{exc}")


# ---------- Job Object：可信进程树退出证据 ----------

class JobObject:
    def __init__(self):
        from ctypes import wintypes

        kernel32 = _kernel32()
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID]
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("无法创建进程 Job Object")

    def assign(self, pid: int) -> None:
        from ctypes import wintypes

        kernel32 = _kernel32()
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        # AssignProcessToJobObject 要求 SET_QUOTA 与 TERMINATE；缺任一权限都会失败。
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise OSError("无法打开 Worker 进程以纳入 Job Object")
        try:
            if not kernel32.AssignProcessToJobObject(self.handle, process):
                raise OSError("无法把 Worker 纳入 Job Object")
        finally:
            _close_handle(process)

    def active_processes(self) -> int:
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        info = Accounting()
        if not _kernel32().QueryInformationJobObject(
                self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise OSError("无法查询 Job Object 进程数")
        return int(info.ActiveProcesses)

    def close(self) -> None:
        _close_handle(self.handle)
        self.handle = None


# ---------- Worker 生命周期 ----------

def _port() -> int:
    try:
        return int(os.environ.get("MENU_CAPTURE_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def _read_release(current: Path) -> dict:
    try:
        payload = json.loads((current / "release-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        raise RuntimeError("缺少有效的运行时发布清单") from None
    if not isinstance(payload, dict) or not payload.get("version"):
        raise RuntimeError("缺少有效的运行时发布清单")
    return payload


def _worker_env(data_root: Path, token: str, version: str, *, home: Path, verify: bool,
                gate: Path, stop_nonce: str) -> dict[str, str]:
    current = _current_dir()
    env = dict(os.environ)
    env.update({
        "MENU_CAPTURE_ROLE": "worker",
        "MENU_CAPTURE_VERSION": version,
        "MENU_CAPTURE_HOME": str(home),
        "MENU_CAPTURE_BIND": "127.0.0.1",
        "MENU_CAPTURE_PORT": str(_port()),
        "MENU_CAPTURE_COORDINATOR_TOKEN": token,
        "MENU_CAPTURE_VELOPACK_ROOT": str(_install_root()),
        "MENU_CAPTURE_VELOPACK_CURRENT": str(current),
        "MENU_CAPTURE_VELOPACK_PACKAGES": str(_install_root() / "packages"),
        "MENU_CAPTURE_START_GATE": str(gate),
        "TESSERACT_CMD": str(current / "tesseract" / "tesseract.exe"),
        "TESSDATA_PREFIX": str(current / "tesseract" / "tessdata"),
        "PYTHONNOUSERSITE": "1",
    })
    if verify:
        env["MENU_CAPTURE_UPDATE_VERIFY"] = "1"
        env["MENU_CAPTURE_ALLOW_SELF_UPDATE"] = "0"
    else:
        env.setdefault("MENU_CAPTURE_ALLOW_SELF_UPDATE", "1")
    if stop_nonce:
        env["MENU_CAPTURE_LAUNCHER_STOP_NONCE"] = stop_nonce
    env["PATH"] = os.pathsep.join([
        str(current / "node"), str(current / "python"), env.get("PATH", ""),
    ])
    return {key: value for key, value in env.items() if value}


class WorkerHandle:
    def __init__(self, process: subprocess.Popen, job: JobObject):
        self.process = process
        self.job = job

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self):
        return self.process.poll()

    def close(self) -> None:
        try:
            self.job.close()
        except Exception:  # noqa: BLE001
            pass


def _start_worker(data_root: Path, token: str, *, home: Path | None = None,
                  verify: bool = False, stop_nonce: str = "") -> WorkerHandle:
    current = _current_dir()
    python = current / "python" / "python.exe"
    if not python.is_file():
        raise RuntimeError(f"缺少打包运行时：{python}")
    logs = data_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    prefix = "service.verify" if verify else "service"
    for name in (f"{prefix}.stdout.log", f"{prefix}.stderr.log"):
        target = logs / name
        if target.is_file():
            try:
                target.replace(target.with_name(name + ".previous"))
            except OSError:
                pass
    stdout = (logs / f"{prefix}.stdout.log").open("ab")
    stderr = (logs / f"{prefix}.stderr.log").open("ab")
    gate = data_root / "logs" / f"start-{uuid.uuid4().hex}.gate"
    gate.unlink(missing_ok=True)
    version = str(_read_release(current).get("version") or "")
    command = [str(python), "-X", "utf8", "-E", "-s", "-B", "-m", "menu_capture.api.server"]
    job = JobObject()
    process = None
    try:
        process = subprocess.Popen(
            command,
            cwd=str(current / "app"),
            env=_worker_env(data_root, token, version, home=home or data_root, verify=verify,
                            gate=gate, stop_nonce=stop_nonce),
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        # 先纳入 Job Object，再放行启动闸门，闭合子进程在纳管前逃逸的窗口。
        job.assign(process.pid)
        gate.write_text("go", encoding="ascii")
    except BaseException:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=10)
            except Exception as exc:  # noqa: BLE001
                job.close()
                raise RuntimeError("Worker 纳入进程树失败且无法确认子进程已退出") from exc
        job.close()
        raise
    _log(data_root, f"{'验证' if verify else '业务'} Worker 已启动，PID {process.pid}")
    return WorkerHandle(process, job)


def _wait_worker_exit(handle: WorkerHandle, *, timeout: int = WORKER_STOP_TIMEOUT) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if handle.poll() is not None and not _port_in_use(_port()):
            try:
                if handle.job.active_processes() == 0:
                    return True, ""
            except OSError as exc:
                return False, str(exc)
        time.sleep(0.5)
    if handle.poll() is None:
        return False, "Worker 未在超时内退出"
    if _port_in_use(_port()):
        return False, "Worker 已退出但端口仍被占用"
    try:
        if handle.job.active_processes() != 0:
            return False, "Worker 已退出但业务子进程仍存活"
    except OSError as exc:
        return False, str(exc)
    return False, "等待业务进程树退出超时"


def _wait_worker_health(port: int, version: str, commit: str,
                        timeout: int = WORKER_START_TIMEOUT) -> tuple[bool, str]:
    from menu_capture.updates import recovery

    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        ok, last = recovery.verify_health(port, version, commit)
        if ok:
            return True, ""
        time.sleep(1)
    return False, last or "Worker 健康检查超时"


# ---------- 启动器停止控制（测试/运维夹具，不进入安装事务） ----------

def _control_path(data_root: Path) -> Path:
    return data_root / "data" / "launcher-control.json"


def _request_launcher_stop(data_root: Path) -> int:
    token = _read_text(data_root / "data" / "launcher-control-token")
    if not token:
        return 1
    path = _control_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"command": "stop", "token": token, "nonce": token}), encoding="utf-8")
    temp.replace(path)
    # 等待现有启动器退出；不需要也不允许第二个启动器强制结束进程。
    pid_file = data_root / "data" / "launcher.pid"
    from menu_capture.updates import recovery

    try:
        identity = json.loads(_read_text(pid_file) or "{}")
        pid = int(identity.get("pid") or 0) if isinstance(identity, dict) else int(identity)
        started = identity.get("process_started_at") if isinstance(identity, dict) else None
        started = float(started) if started is not None else None
    except (TypeError, ValueError):
        pid, started = 0, None
    if pid <= 0:
        return 1

    deadline = time.monotonic() + WORKER_STOP_TIMEOUT
    while time.monotonic() < deadline:
        state = recovery.process_state(pid, started)
        state_value = getattr(state, "value", state)
        if state_value == "dead":
            return 0 if not recovery.port_in_use(_port()) else 1
        if state_value == "unknown":
            # 无法确认进程身份时不能把 PID 查询失败当成已停止。
            time.sleep(0.5)
            continue
        time.sleep(0.5)
    return 1


def _consume_stop_request(data_root: Path, token: str) -> bool:
    payload = _read_text(_control_path(data_root))
    if not payload:
        return False
    try:
        request = json.loads(payload)
    except ValueError:
        return False
    control_token = _read_text(data_root / "data" / "launcher-control-token")
    if (not isinstance(request, dict) or request.get("command") != "stop"
            or not control_token or request.get("token") != control_token
            or request.get("nonce") != control_token):
        return False
    try:
        _control_path(data_root).unlink()
    except OSError:
        pass
    return True


# ---------- 外部恢复协调器 ----------

def _watchdog_paths(data_root: Path) -> tuple[Path, Path, Path]:
    root = data_root / "recovery"
    return root / "update-watchdog.ps1", root / "watchdog-control.json", root / "watchdog-ready.json"


def _start_watchdog(data_root: Path, journal: dict, *, mode: str, launcher_pid: int) -> bool:
    from menu_capture.updates import recovery

    script, control_path, ready_path = _watchdog_paths(data_root)
    existing = recovery.read_json(ready_path, {})
    if (isinstance(existing, dict)
            and existing.get("transaction_id") == journal["transaction_id"]
            and isinstance(existing.get("watchdog_nonce"), str)
            and existing.get("watchdog_nonce")):
        try:
            existing_state = recovery.process_state(
                int(existing.get("pid") or 0), float(existing.get("process_started_at") or 0))
        except (TypeError, ValueError):
            existing_state = recovery.ProcessState.UNKNOWN
        existing_value = getattr(existing_state, "value", existing_state)
        if existing_value == "alive":
            _log(data_root, f"复用已就绪的外部恢复协调器，PID {existing.get('pid')}")
            return True
        if existing_value == "unknown":
            _log(data_root, "现有外部恢复协调器身份无法核验，拒绝启动第二个协调器")
            return False
    source = _current_dir() / "update-watchdog.ps1"
    if not source.is_file():
        _log(data_root, f"缺少外部恢复协调器脚本：{source}")
        return False
    script.parent.mkdir(parents=True, exist_ok=True)
    script_temp = script.with_suffix(".ps1.tmp")
    script_temp.write_bytes(source.read_bytes())
    if script_temp.stat().st_size == 0:
        script_temp.unlink(missing_ok=True)
        _log(data_root, "外部恢复协调器文件为空")
        return False
    script_temp.replace(script)
    ready_path.unlink(missing_ok=True)
    watchdog_nonce = uuid.uuid4().hex
    control = {
        "mode": mode, "transaction_id": journal["transaction_id"],
        "owner_token": journal.get("owner_token") or "",
        "watchdog_nonce": watchdog_nonce,
        "target_version": journal["target_version"], "target_commit": journal["target_commit"],
        "previous_version": journal["previous_version"], "previous_commit": journal["previous_commit"],
        "channel": journal["channel"], "install_root": str(_install_root()),
        "data_root": str(data_root), "port": _port(), "launcher_pid": launcher_pid,
        "recovery": journal["recovery_asset"], "schema_baseline": journal["schema_baseline"],
        "token_file": str(_token_path(data_root)),
        "stale_after_seconds": APPLY_STALL_SECONDS,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    recovery.write_json(control_path, control)
    try:
        # 协调器会跨越整目录切换，不能继承启动器位于 current 的工作目录句柄。
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-ControlFile", str(control_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(script.parent),
            creationflags=0x08000000,
        )
    except OSError as exc:
        _log(data_root, f"启动外部恢复协调器失败：{exc}")
        return False
    deadline = time.monotonic() + WATCHDOG_READY_TIMEOUT
    while time.monotonic() < deadline:
        ready = recovery.read_json(ready_path, {})
        if (isinstance(ready, dict)
                and ready.get("transaction_id") == journal["transaction_id"]
                and ready.get("watchdog_nonce") == watchdog_nonce):
            try:
                ready_pid = int(ready.get("pid") or 0)
                ready_started = float(ready.get("process_started_at") or 0)
                state = recovery.process_state(ready_pid, ready_started)
            except (TypeError, ValueError):
                state = recovery.ProcessState.UNKNOWN
            if getattr(state, "value", state) == "alive":
                _log(data_root, f"外部恢复协调器就绪，PID {ready_pid}")
                return True
        time.sleep(0.5)
    _log(data_root, "外部恢复协调器未在超时内就绪")
    return False


# ---------- 事务流程 ----------

class Context:
    def __init__(self, data_root: Path, token: str, backend, transaction, owner_token: str):
        self.data_root = data_root
        self.token = token
        self.backend = backend
        self.transaction = transaction
        self.owner_token = owner_token
        from menu_capture.updates import recovery

        self.process_started_at = recovery.current_process_created_at()
        self.worker: WorkerHandle | None = None
        self.held_workers: list[WorkerHandle] = []


def _claim(ctx: Context, journal: dict, phase: str) -> None:
    from menu_capture.updates import recovery

    owner = journal.get("owner_token") or ""
    takeover = None
    if owner and owner != ctx.owner_token:
        coordinator = journal.get("coordinator") or {}
        pid = coordinator.get("pid")
        started = coordinator.get("process_started_at")
        if not isinstance(pid, int) or not isinstance(started, (int, float)):
            raise RuntimeError("安装事务缺少可核验的协调器进程身份")
        state = recovery.process_state(pid, float(started))
        state_value = getattr(state, "value", state)
        if state_value != "dead":
            raise RuntimeError("安装事务仍由其他存活协调器持有")
        takeover = {"previous_pid": pid, "previous_process_started_at": float(started),
                    "previous_owner_token": owner, "observed_state": "dead",
                    "detected_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                    "reason": "按 PID 与操作系统创建时间确认旧协调器已退出"}
    ctx.transaction.claim(
        journal["transaction_id"], journal["status"], owner_token=ctx.owner_token,
        pid=os.getpid(), process_started_at=ctx.process_started_at, phase=phase,
        takeover_evidence=takeover,
    )


def _transition(ctx: Context, journal: dict, status: str, *, evidence=None, message="", error=""):
    return ctx.transaction.transition(
        journal["transaction_id"], journal["status"], status, owner_token=ctx.owner_token,
        message=message, error=error, evidence=evidence,
    )


def _recovery_workdir(data_root: Path) -> Path:
    path = data_root / "recovery" / "probe"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stop_worker_before_recovery(ctx: Context, journal: dict, worker: WorkerHandle,
                                 *, verify: bool) -> tuple[bool, str]:
    """回滚前确认候选进程树已退出；不以 API 失败、PID 消失或超时替代证据。"""
    from menu_capture.updates import recovery

    was_running = worker.poll() is None
    acknowledged = not was_running
    if was_running:
        if verify:
            acknowledged = recovery.request_verify_exit(
                _port(), ctx.token, transaction_id=journal["transaction_id"],
                target_version=journal["target_version"], verify_pid=worker.pid)
        else:
            acknowledged = recovery.request_recovery_shutdown(
                _port(), ctx.token, transaction_id=journal["transaction_id"],
                target_version=journal["target_version"])
        if not acknowledged:
            detail = "候选 Worker 未确认受控关闭，禁止启动恢复 updater"
            ctx.held_workers.append(worker)
            _log(ctx.data_root, detail)
            try:
                _transition(ctx, journal, "recovery_required", error=detail,
                            message="候选 Worker 关闭请求未获确认，禁止回滚")
            except Exception as exc:  # noqa: BLE001 - 保持原恢复闸门
                _log(ctx.data_root, f"写入恢复阻断状态失败：{exc}")
            return False, detail
    stopped, reason = _wait_worker_exit(worker)
    if stopped:
        worker.close()
        return True, ""
    ctx.held_workers.append(worker)
    detail = f"无法证明候选 Worker 进程树已退出：{reason}"
    _log(ctx.data_root, detail)
    try:
        _transition(ctx, journal, "recovery_required", error=detail,
                    message="候选进程仍活动或退出状态不确定，禁止执行回滚")
    except Exception as exc:  # noqa: BLE001 - 保持原恢复闸门，绝不继续安装
        _log(ctx.data_root, f"写入恢复阻断状态失败：{exc}")
    return False, detail


def _verify_home_path(data_root: Path, transaction_id: str) -> Path:
    """为候选版本生成与真实 data_root、install_root/current/packages 完全分离的隔离根目录。"""
    resolved_data = Path(data_root).resolve()
    protected = (
        resolved_data,
        _install_root().resolve(),
        _current_dir().resolve(),
        (_install_root() / "packages").resolve(),
    )

    def _overlaps(candidate: Path) -> bool:
        resolved = candidate.resolve()
        return any(
            resolved == root or resolved.is_relative_to(root) or root.is_relative_to(resolved)
            for root in protected
        )

    override = str(os.environ.get("MENU_CAPTURE_VERIFY_ROOT") or "").strip()
    if override:
        base = Path(override).expanduser()
        candidate = base / f"verify-{transaction_id}"
        if _overlaps(candidate):
            raise ValueError(f"MENU_CAPTURE_VERIFY_ROOT 与生产数据或安装目录重叠：{candidate}")
        return candidate

    sibling = resolved_data.parent / f"{resolved_data.name}-verify" / f"verify-{transaction_id}"
    if not _overlaps(sibling):
        return sibling
    fallback = Path(tempfile.gettempdir()).resolve() / "MenuCapture-verify" / f"verify-{transaction_id}"
    if _overlaps(fallback):
        raise ValueError(f"无法为候选验证分配独立根目录：{fallback}")
    return fallback


def _handle_prepared(ctx: Context, worker: WorkerHandle) -> str:
    from menu_capture.updates import recovery

    journal = ctx.transaction.read()
    # 先按事务所有权规则 claim，再做预检；否则预检失败无法合法写入 failed。
    _claim(ctx, journal, "prepared")
    journal = ctx.transaction.read()
    current = _current_dir()
    manifest = recovery.release_manifest(current)
    if (str(manifest.get("version") or "") != journal["previous_version"]
            or str(manifest.get("git_commit") or "") != journal["previous_commit"]):
        _transition(ctx, journal, "failed", error="运行版本与事务前置版本不一致",
                    message="未停止 Worker")
        return "keep"
    try:
        ctx.backend.verify_pending(journal["target_asset"], journal["channel"])
        ctx.backend.verify_recovery_package(journal["recovery_asset"])
    except Exception as exc:  # noqa: BLE001
        _transition(ctx, journal, "failed", error=str(exc), message="安装资产复核失败，未停止 Worker")
        return "keep"
    journal = _transition(ctx, journal, "stopping", evidence={
        "worker_pid": worker.pid, "launcher_pid": os.getpid(),
        "stopping_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }, message="正在正常停止业务 Worker")
    if not _start_watchdog(ctx.data_root, journal, mode="apply", launcher_pid=os.getpid()):
        journal = _transition(ctx, journal, "failed", error="外部恢复协调器未就绪",
                              message="未交接更新，保留旧 Worker")
        return "keep"
    shutdown_outcome = "accepted"
    shutdown_detail = ""
    if worker.poll() is None:
        shutdown_outcome, shutdown_detail = recovery.request_worker_shutdown_result(
            _port(), ctx.token, transaction_id=journal["transaction_id"],
            target_version=journal["target_version"])
    if shutdown_outcome == "rejected":
        # 仅在 Worker 明确拒绝且进程仍存活时才回退为 failed 并保留旧 Worker。
        if worker.poll() is None:
            journal = _transition(ctx, journal, "failed",
                                  error=shutdown_detail or "Worker 拒绝受控关闭",
                                  message="未交接更新，保留旧 Worker")
            return "keep"
        _transition(ctx, journal, "recovery_required",
                    error=f"Worker 拒绝关闭后进程状态异常：{shutdown_detail}",
                    message="Worker 退出状态不确定，保持恢复闸门")
        return "recovery"
    if shutdown_outcome == "transport_error":
        # HTTP 响应丢失不等于 Worker 未停止：必须核验真实进程树与端口，未知时保持闸门。
        ok, reason = _wait_worker_exit(worker)
        if not ok or worker.poll() != 0:
            _transition(
                ctx, journal, "recovery_required",
                error=f"Worker 关闭响应丢失且退出状态未确认（{shutdown_detail}）：{reason or f'退出码 {worker.poll()}'}",
                message="关闭状态不确定，保持恢复闸门",
            )
            return "recovery"
        _log(ctx.data_root, f"关闭响应丢失（{shutdown_detail}），但已通过 Job Object 与端口确认 Worker 正常退出")
    else:
        ok, reason = _wait_worker_exit(worker)
        if not ok:
            _transition(ctx, journal, "recovery_required", error=reason,
                        message="业务进程树未安全退出，禁止交接更新")
            return "recovery"
    journal = _transition(ctx, journal, "applying", evidence={
        "worker_exit_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "port_free": True, "job_active_processes": 0,
        "shutdown_ack": shutdown_outcome,
        "updater_handoff_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }, message="业务已安全退出，交接官方 updater")
    ctx.backend.apply_pending(journal["target_asset"], channel=journal["channel"])
    _log(ctx.data_root, "已交接 Velopack updater，启动器正常退出")
    return "exit"


def _verify_candidate(ctx: Context, journal: dict) -> str:
    from menu_capture.updates import recovery

    current = _current_dir()
    try:
        recovery.verify_release(current, journal["target_version"], journal["target_commit"])
    except ValueError as exc:
        _transition(ctx, journal, "recovery_required", error=str(exc),
                    message="安装目录身份与事务目标不一致")
        return "recovery"
    try:
        verify_home = _verify_home_path(ctx.data_root, journal["transaction_id"])
        recovery.prepare_verify_home(
            ctx.data_root, verify_home,
            transaction_id=journal["transaction_id"],
            forbidden_roots=(_install_root(), current, _install_root() / "packages"),
        )
    except Exception as exc:  # noqa: BLE001
        _transition(ctx, journal, "recovery_required", error=str(exc),
                    message="无法生成隔离验证数据副本")
        return "recovery"
    verify_worker = None
    try:
        verify_worker = _start_worker(ctx.data_root, ctx.token, home=verify_home, verify=True)
        ok, reason = _wait_worker_health(_port(), journal["target_version"], journal["target_commit"])
        if not ok:
            raise RuntimeError(f"隔离副本启动失败：{reason}")
        readable, reason = recovery.database_readable(verify_home / "data")
        if not readable:
            raise RuntimeError(reason)
        if not recovery.request_verify_exit(
                _port(), ctx.token, transaction_id=journal["transaction_id"],
                target_version=journal["target_version"], verify_pid=verify_worker.pid):
            raise RuntimeError("验证 Worker 未确认受控退出")
        stopped, reason = _wait_worker_exit(verify_worker)
        if not stopped:
            raise RuntimeError(reason)
        verify_worker = None
    except Exception as exc:  # noqa: BLE001
        if verify_worker is not None:
            stopped, stop_reason = _stop_worker_before_recovery(
                ctx, journal, verify_worker, verify=True)
            if not stopped:
                return "recovery"
        return _rollback_or_recovery(ctx, journal, f"隔离副本验证失败：{exc}")
    finally:
        if verify_worker is not None:
            verify_worker.close()
    ctx.transaction.add_evidence(journal["transaction_id"], owner_token=ctx.owner_token, patch={
        "verify_copy_ok": True,
        "verify_copy_ok_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    })
    journal = ctx.transaction.read()
    try:
        normal = _start_worker(ctx.data_root, ctx.token)
    except Exception as exc:  # noqa: BLE001
        return _rollback_or_recovery(ctx, journal, f"真实数据启动失败：{exc}")
    ok, reason = _wait_worker_health(_port(), journal["target_version"], journal["target_commit"])
    if not ok:
        stopped, _stop_reason = _stop_worker_before_recovery(ctx, journal, normal, verify=False)
        if not stopped:
            return "recovery"
        return _rollback_or_recovery(ctx, journal, f"真实数据健康检查失败：{reason}")
    readable, reason = recovery.database_readable(ctx.data_root / "data")
    if not readable:
        stopped, _stop_reason = _stop_worker_before_recovery(ctx, journal, normal, verify=False)
        if not stopped:
            return "recovery"
        return _rollback_or_recovery(ctx, journal, f"真实数据库不可读：{reason}")
    _transition(ctx, journal, "completed", evidence={
        "real_start_ok": True, "health_verified_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "installed_version": journal["target_version"], "installed_commit": journal["target_commit"],
    }, message="新版本已在真实数据上健康启动，安装完成")
    if not recovery.request_release_business_gate(
            _port(), ctx.token, transaction_id=journal["transaction_id"],
            target_version=journal["target_version"]):
        _log(ctx.data_root, "事务已完成，但放行后台业务闸门请求未获确认")
    try:
        if recovery.cleanup_verify_home(verify_home, data_root=ctx.data_root):
            _log(ctx.data_root, f"已清理隔离验证目录：{verify_home.name}")
        pruned = recovery.prune_recovery_baselines(ctx.data_root / "data", keep=1)
        if pruned:
            _log(ctx.data_root, f"已清理 {pruned} 个历史更新基线快照")
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响已完成的更新
        _log(ctx.data_root, f"清理隔离验证副本或历史快照失败：{exc}")
    ctx.worker = normal
    _log(ctx.data_root, "新版本安装验证完成，业务 Worker 继续运行")
    return "running"


def _rollback_or_recovery(ctx: Context, journal: dict, reason: str) -> str:
    from menu_capture.updates import recovery

    if journal["status"] not in {"applying", "verifying"}:
        return "recovery"
    recovery_ok = False
    try:
        ctx.backend.verify_recovery_package(journal["recovery_asset"])
        recovery_ok = True
    except Exception as exc:  # noqa: BLE001
        reason = f"{reason}；恢复包复核失败：{exc}"
    schema = journal.get("schema_baseline") or {}
    db_ok = recovery.database_unchanged(
        ctx.data_root / "data", schema.get("snapshot") or {}, _recovery_workdir(ctx.data_root))
    if not recovery_ok or not db_ok:
        _transition(ctx, journal, "recovery_required", error=reason,
                    message="无法证明可安全回滚，保留恢复闸门")
        return "recovery"
    journal = _transition(ctx, journal, "rolling_back", evidence={
        "rollback_reason": reason,
        "database_unchanged": True, "recovery_package_verified": True,
    }, message="新版本验证失败，开始一次受控回滚")
    if not _start_watchdog(ctx.data_root, journal, mode="rollback", launcher_pid=os.getpid()):
        _transition(ctx, journal, "recovery_required", error="外部恢复协调器未就绪",
                    message="无法安全执行回滚")
        return "recovery"
    try:
        recovery_channel = str(journal["recovery_asset"].get("channel") or "").strip().lower()
        ctx.backend.rollback(journal["recovery_asset"], channel=recovery_channel)
    except Exception as exc:  # noqa: BLE001 - updater 失败时保留唯一恢复闸门
        _transition(ctx, journal, "recovery_required", error=f"官方 updater 回滚失败：{exc}",
                    message="回滚交接失败，保留恢复闸门等待人工确认")
        return "recovery"
    _log(ctx.data_root, "已交接官方 updater 执行回滚，启动器正常退出")
    return "exit"


def _handle_after_update(ctx: Context) -> str:
    from menu_capture.updates import recovery

    journal = ctx.transaction.read()
    manifest = recovery.release_manifest(_current_dir())
    installed_version = str(manifest.get("version") or "")
    installed_commit = str(manifest.get("git_commit") or "")
    status = journal["status"]
    if status == "applying":
        if installed_version != journal["target_version"] or installed_commit != journal["target_commit"]:
            _transition(ctx, journal, "recovery_required",
                        error=f"实际安装版本 {installed_version}/{installed_commit} 与目标不一致",
                        message="更新未按预期应用，禁止继续启动业务")
            return "recovery"
        _claim(ctx, journal, "verifying")
        journal = _transition(ctx, journal, "verifying", evidence={
            "installed_version": installed_version, "installed_commit": installed_commit,
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        }, message="开始验证新版本")
    elif status == "verifying":
        if ctx.transaction.read().get("owner_token") != ctx.owner_token:
            _claim(ctx, journal, "verifying")
            journal = ctx.transaction.read()
    elif status == "rolling_back":
        if installed_version != journal["previous_version"] or installed_commit != journal["previous_commit"]:
            _transition(ctx, journal, "recovery_required",
                        error=f"回滚后实际版本 {installed_version}/{installed_commit} 与旧版本不一致",
                        message="回滚未按预期完成")
            return "recovery"
        try:
            recovery.verify_release(_current_dir(), journal["previous_version"], journal["previous_commit"])
        except ValueError as exc:
            _transition(ctx, journal, "recovery_required", error=str(exc),
                        message="回滚版本身份校验失败")
            return "recovery"
        try:
            worker = _start_worker(ctx.data_root, ctx.token)
        except Exception as exc:  # noqa: BLE001
            _transition(ctx, journal, "recovery_required", error=str(exc),
                        message="回滚后旧版本无法启动")
            return "recovery"
        ok, reason = _wait_worker_health(_port(), journal["previous_version"], journal["previous_commit"])
        if not ok:
            worker.close()
            _transition(ctx, journal, "recovery_required", error=reason,
                        message="回滚后旧版本健康检查失败")
            return "recovery"
        ctx.worker = worker
        _transition(ctx, journal, "rolled_back", evidence={
            "rollback_verified_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())},
            message="回滚完成，旧版本健康运行")
        if not recovery.request_release_business_gate(
                _port(), ctx.token, transaction_id=journal["transaction_id"],
                target_version=journal["previous_version"]):
            _log(ctx.data_root, "回滚已完成，但放行后台业务闸门请求未获确认")
        _log(ctx.data_root, "回滚完成，旧版本继续运行")
        return "running"
    else:
        return "unknown"
    return _verify_candidate(ctx, ctx.transaction.read())


# ---------- 主循环 ----------

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    data_root = _data_root()
    current = _current_dir()
    if "--rollback-to" in args:
        _message("直接版本切换已禁用，请通过受控恢复流程处理。")
        return 2
    # 启动器是 PyInstaller 入口，业务模块位于同目录的 app/；--stop 也需要导入 recovery，必须先加入路径。
    app_dir = current / "app"
    if app_dir.is_dir() and str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    if "--stop" in args:
        return _request_launcher_stop(data_root)
    _run_velopack_hooks(data_root)
    acquired, handle = _acquire_mutex()
    if not acquired:
        _log(data_root, "已有启动器实例，打开工作台后退出")
        if _port_in_use(_port()):
            os.startfile(f"http://127.0.0.1:{_port()}/")
        return 0
    try:
        if _port_in_use(_port()):
            _log(data_root, f"端口 {_port()} 已被占用，无法启动")
            _message(f"端口 {_port()} 已被占用。请关闭占用程序或修改 MENU_CAPTURE_PORT 后重试。")
            return 1
        token = uuid.uuid4().hex
        stop_nonce = uuid.uuid4().hex
        os.environ["MENU_CAPTURE_COORDINATOR_TOKEN"] = token
        os.environ["MENU_CAPTURE_VELOPACK_ROOT"] = str(_install_root())
        os.environ["MENU_CAPTURE_VELOPACK_CURRENT"] = str(current)
        os.environ["MENU_CAPTURE_VELOPACK_PACKAGES"] = str(_install_root() / "packages")
        _write_token(data_root, token)
        (data_root / "data" / "launcher-control-token").write_text(stop_nonce, encoding="ascii")
        _ensure_autostart(data_root)

        from menu_capture.updates.install_transaction import ACTIVE, InstallTransaction, TransactionError
        from menu_capture.updates.velopack_backend import VelopackBackend

        backend = VelopackBackend(REPOSITORY, data_root / "data", python=str(current / "python" / "python.exe"),
                                  app_dir=app_dir)
        transaction = InstallTransaction(data_root / "data")
        ctx = Context(data_root, token, backend, transaction, uuid.uuid4().hex)
        pid_path = data_root / "data" / "launcher.pid"
        pid_temp = pid_path.with_suffix(".tmp")
        pid_temp.write_text(json.dumps({"pid": os.getpid(),
                                        "process_started_at": ctx.process_started_at}), encoding="utf-8")
        pid_temp.replace(pid_path)
        worker = None
        while True:
            try:
                journal = transaction.read()
            except TransactionError as exc:
                _log(data_root, f"安装事务需人工确认：{exc}")
                _message(str(exc))
                return 3
            status = journal.get("status")
            if status == "recovery_required":
                _log(data_root, f"安装事务处于恢复闸门：{journal.get('message') or journal.get('error')}")
                _message("更新需要人工恢复确认，业务已保持停止。请查看 logs/launcher.log。")
                return 3
            if status in {"applying", "verifying", "rolling_back"}:
                if worker is not None:
                    worker.close()
                    worker = None
                try:
                    _claim(ctx, journal, status)
                except (RuntimeError, TransactionError) as exc:
                    _log(data_root, f"安装协调器所有权仍被占用或无法核验：{exc}")
                    _message("更新事务由其他协调器持有，或进程身份无法核验。业务启动已阻断。")
                    return 3
                journal = transaction.read()
                outcome = _handle_after_update(ctx)
                if outcome == "exit":
                    return 0
                if outcome == "recovery":
                    return 3
                if outcome == "running":
                    worker = ctx.worker
                    ctx.worker = None
            elif status == "prepared":
                if worker is None or worker.poll() is not None:
                    worker = _start_worker(data_root, token, stop_nonce=stop_nonce)
                try:
                    outcome = _handle_prepared(ctx, worker)
                except (RuntimeError, TransactionError) as exc:
                    _log(data_root, f"准备阶段所有权接管或事务更新失败：{exc}")
                    _message("更新事务所有权校验失败，已保留恢复闸门。")
                    return 3
                if outcome == "exit":
                    return 0
                if outcome == "recovery":
                    return 3
            elif status == "stopping":
                # 旧启动器停止后由外部协调器收尾；这里不并发启动业务。
                return 3
            else:
                if worker is None:
                    worker = _start_worker(data_root, token, stop_nonce=stop_nonce)
            if _consume_stop_request(data_root, token):
                _log(data_root, "收到受控停止请求")
                if worker is not None and worker.poll() is None:
                    from menu_capture.updates import recovery

                    acknowledged = recovery.request_launcher_stop(_port(), token, nonce=stop_nonce)
                    stopped, reason = _wait_worker_exit(worker, timeout=WORKER_STOP_TIMEOUT)
                    if not stopped:
                        _log(data_root, f"启动器停止失败，继续监督 Worker：{reason}")
                        continue
                    if not acknowledged:
                        _log(data_root, "Worker 已通过 Job Object 与端口证据确认退出；停止 API 未返回确认")
                    worker.close()
                    worker = None
                elif worker is not None:
                    stopped, reason = _wait_worker_exit(worker, timeout=1)
                    if not stopped:
                        _log(data_root, f"启动器停止状态不确定，继续监督 Worker：{reason}")
                        continue
                    worker.close()
                    worker = None
                elif _port_in_use(_port()):
                    _log(data_root, "Worker 句柄缺失但端口仍被占用；拒绝退出")
                    continue
                return 0
            if worker is not None and worker.poll() is not None:
                stopped, reason = _wait_worker_exit(worker, timeout=1)
                if not stopped:
                    _log(data_root, f"Worker 主进程退出但进程树状态未确认，保留监督句柄：{reason}")
                    time.sleep(2)
                    continue
                _log(data_root, f"Worker 退出，退出码 {worker.returncode}，5 秒后重启")
                worker.close()
                worker = None
                time.sleep(5)
                continue
            time.sleep(2)
    finally:
        _close_handle(handle)


if __name__ == "__main__":
    sys.exit(main())
