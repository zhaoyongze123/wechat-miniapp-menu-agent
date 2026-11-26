"""Windows 统一启动器：Velopack 生命周期、单实例、Worker 守护与受控更新应用。

启动顺序（不可调整）：
1. `VelopackApp().set_auto_apply_on_startup(False).run()` 必须最先执行：
   处理安装/更新/卸载钩子，并确保下载完成的更新不会在用户未授权时自动应用。
2. 再解析数据目录、获取单实例互斥、检查端口冲突。
3. 最后才启动业务 Worker（数据库、端口监听与后台任务都在 Worker 进程内）。

为什么由启动器应用更新：Velopack 更新会重命名整个 `current` 目录，运行在该目录内的
进程必须先正常退出。启动器收到 Worker 的应用请求后，先让 Worker 优雅停止，再调用
SDK 的应用接口，避免框架强制结束正在采集的进程。
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

PACK_ID = "Yipu.MenuCapture"
DEFAULT_PORT = 8765
MUTEX_PREFIX = "Local\\MenuCaptureLauncher-"
WORKER_STOP_TIMEOUT = 120
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


def _read_token(data_root: Path) -> str:
    try:
        return _token_path(data_root).read_text(encoding="ascii").strip()
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

    # 卸载的受控业务退出必须由独立入口处理；快速 hook 不允许绕过安装事务。

    (velopack.App()
        .set_auto_apply_on_startup(False)
        .on_first_run(lambda *_a, **_k: _ensure_autostart(data_root))
        .on_restarted(lambda *_a, **_k: _log(data_root, "Velopack 更新后重启"))
        .run())


# ---------- 单实例与端口 ----------

def _acquire_mutex() -> tuple[bool, int]:
    name = f"{MUTEX_PREFIX}{os.environ.get('USERNAME') or 'default'}"
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    already = ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    return (not already), handle


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
    target = _install_root() / "MenuCapture.exe"
    if link.exists() or not target.is_file():
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


# ---------- Worker 生命周期 ----------

def _worker_env(data_root: Path, token: str, version: str) -> dict[str, str]:
    root = _install_root()
    current = _current_dir()
    env = dict(os.environ)
    env.update({
        "MENU_CAPTURE_ROLE": "worker",
        "MENU_CAPTURE_VERSION": version,
        "MENU_CAPTURE_HOME": str(data_root),
        "MENU_CAPTURE_BIND": "127.0.0.1",
        "MENU_CAPTURE_PORT": str(_port()),
        "MENU_CAPTURE_COORDINATOR_TOKEN": token,
        "MENU_CAPTURE_VELOPACK_ROOT": str(root),
        "MENU_CAPTURE_VELOPACK_CURRENT": str(current),
        "MENU_CAPTURE_VELOPACK_PACKAGES": str(root / "packages"),
        "TESSERACT_CMD": str(current / "tesseract" / "tesseract.exe"),
        "TESSDATA_PREFIX": str(current / "tesseract" / "tessdata"),
        "PYTHONNOUSERSITE": "1",
    })
    env["PATH"] = os.pathsep.join([
        str(current / "node"), str(current / "python"), env.get("PATH", ""),
    ])
    return {key: value for key, value in env.items() if value}


def _port() -> int:
    try:
        return int(os.environ.get("MENU_CAPTURE_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def _read_version(current: Path) -> str:
    try:
        payload = json.loads((current / "release-manifest.json").read_text(encoding="utf-8"))
        version = payload.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    except (OSError, ValueError, AttributeError):
        pass
    raise RuntimeError("缺少有效的运行时发布清单")



def _start_worker(data_root: Path, token: str) -> subprocess.Popen:
    current = _current_dir()
    python = current / "python" / "python.exe"
    if not python.is_file():
        raise RuntimeError(f"缺少打包运行时：{python}")
    logs = data_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for name in ("service.stdout.log", "service.stderr.log"):
        target = logs / name
        if target.is_file():
            try:
                target.replace(target.with_name(name + ".previous"))
            except OSError:
                pass
    stdout = (logs / "service.stdout.log").open("ab")
    stderr = (logs / "service.stderr.log").open("ab")
    command = [str(python), "-X", "utf8", "-E", "-s", "-B", "-m", "menu_capture.api.server"]
    process = subprocess.Popen(
        command,
        cwd=str(current / "app"),
        env=_worker_env(data_root, token, _read_version(current)),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    _log(data_root, f"Worker 已启动，PID {process.pid}")
    return process


def _request_worker_shutdown(data_root: Path, transaction: dict) -> bool:
    token = str(os.environ.get("MENU_CAPTURE_COORDINATOR_TOKEN") or "") or _read_token(data_root)
    if not token:
        return False
    request = urllib.request.Request(
        f"http://127.0.0.1:{_port()}/api/internal/shutdown",
        data=json.dumps({
            "transaction_id": transaction["transaction_id"],
            "target_version": transaction["target_version"],
        }).encode("utf-8"),
        method="POST",
        headers={"X-Coordinator-Token": token, "Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read()).get("ok") is True
    except Exception as exc:
        _log(data_root, f"请求 Worker 正常退出失败：{exc}")
        return False


# ---------- 主循环 ----------

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--rollback-to" in args:
        _message("直接版本切换已禁用，请通过受控恢复流程处理。")
        return 2

    data_root = _data_root()
    # 启动器是 PyInstaller 入口，业务模块位于同目录的 app/；显式加入导入路径，
    # 不把整套业务代码再打进启动器运行时。
    app_dir = _current_dir() / "app"
    if app_dir.is_dir() and str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
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
        # 同步到本进程环境：适配层与应用接口都需要同一份安装布局与协调令牌。
        os.environ["MENU_CAPTURE_COORDINATOR_TOKEN"] = token
        os.environ["MENU_CAPTURE_VELOPACK_ROOT"] = str(_install_root())
        os.environ["MENU_CAPTURE_VELOPACK_CURRENT"] = str(_current_dir())
        os.environ["MENU_CAPTURE_VELOPACK_PACKAGES"] = str(_install_root() / "packages")
        _write_token(data_root, token)
        _ensure_autostart(data_root)
        worker = _start_worker(data_root, token)
        # 旧 update-apply-request / rollback-request 文件不再是可执行指令。
        # 独立恢复协调器接入前不调用原生更新器；避免不具备恢复能力的切换。
        from menu_capture.updates.install_transaction import InstallTransaction, TransactionError

        transaction = InstallTransaction(data_root / "data")
        while True:
            try:
                journal = transaction.read()
                if journal.get("status") == "prepared":
                    transaction.transition(
                        journal["transaction_id"], "prepared", "failed",
                        message="安全恢复协调器尚未接入，未停止 Worker 或切换版本",
                    )
            except TransactionError as exc:
                _log(data_root, f"安装事务需人工确认：{exc}")
            if worker.poll() is not None:
                _log(data_root, f"Worker 退出，退出码 {worker.returncode}，5 秒后重启")
                time.sleep(5)
                worker = _start_worker(data_root, token)
                continue
            time.sleep(2)
    finally:
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
