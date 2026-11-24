"""Windows 独立更新执行器：安装前重新校验、静默安装、健康验证与自动回滚。

设计边界：
- 只接受下载状态机产出的产物身份（版本、资产名、大小、SHA-256），安装前再次校验；
- 复用 Inno Setup 安装器、Launch.ps1 与 rollback.ps1，不自行复制程序文件；
- 必须由独立进程执行：守护进程会终止并重启采集端，工作线程无法观察重启后的健康状态；
- 采集未空闲或无法确定上一版本时拒绝安装，保证任何失败都有明确回滚目标；
- 安装失败、版本登记异常或健康检查超时，自动回滚到上一版本并如实记录结果。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

from .updates import Version, _SHA256_RE, _file_sha256, _normalize_arch

INSTALL_TIMEOUT_SECONDS = 900
HEALTH_TIMEOUT_SECONDS = 240
HEALTH_INTERVAL_SECONDS = 3
ACTIVE_STATUSES = {"starting", "installing", "verifying", "restarting", "rolling_back"}
ASSET_KIND = {"windows": "windows_installer"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def verify_artifact(
    download_dir: Path,
    state: dict,
    *,
    expected_version: str,
    platform: str,
    arch: str = "",
) -> dict:
    """安装前重新核对产物身份：版本、平台/架构、名称、大小、路径与 SHA-256。

    不信任下载结束时留下的 completed 状态；文件可能已被删除、替换或损坏。
    """
    if not isinstance(state, dict) or state.get("status") != "completed":
        raise ValueError("尚未完成下载，不能安装")
    version = str(state.get("version") or "")
    if not version or version != str(expected_version or ""):
        raise ValueError("下载版本与安装目标不一致，请重新下载目标版本")
    kind = ASSET_KIND.get(platform, "")
    if not kind or str(state.get("asset_kind") or "") != kind:
        raise ValueError("更新包平台与本机不一致")
    expected_arch = _normalize_arch(arch)
    if expected_arch and _normalize_arch(state.get("arch")) != expected_arch:
        raise ValueError("更新包架构与本机不一致")
    name = str(state.get("asset_name") or "")
    if not name or Path(name).name != name:
        raise ValueError("更新包名称无效")
    sha = str(state.get("sha256") or "").lower()
    if not _SHA256_RE.fullmatch(sha):
        raise ValueError("下载记录缺少有效 SHA-256")
    try:
        size = int(state.get("total") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        raise ValueError("下载记录大小无效")
    # 路径由服务端按 数据目录/版本/资产名 推导，绝不使用状态文件里的任意路径。
    expected = (Path(download_dir) / version / name).resolve()
    recorded = str(state.get("path") or "")
    if recorded and Path(recorded).resolve() != expected:
        raise ValueError("更新包路径与版本目录不一致")
    if not expected.is_file() or expected.is_symlink():
        raise ValueError("更新包文件不存在或不是普通文件，请重新下载")
    if expected.stat().st_size != size:
        raise ValueError("更新包大小与下载记录不一致，请重新下载")
    if _file_sha256(expected) != sha:
        raise ValueError("更新包 SHA-256 校验失败，请重新下载")
    return {
        "version": version,
        "asset_kind": kind,
        "asset_name": name,
        "sha256": sha,
        "size": size,
        "path": str(expected),
    }


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _run_command(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            args, timeout=timeout, capture_output=True, text=True, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return completed.returncode, output


def _probe_version(port: int, timeout: float = 2.0) -> str:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/version", timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
        return str(payload.get("version") or "")
    except Exception:
        return ""


class InstallOrchestrator:
    """在独立进程中执行一次安装；所有外部命令与健康探测均可注入以便离线验证。"""

    def __init__(
        self,
        *,
        job: dict,
        journal_path: Path,
        run_command: Callable[[list[str], float], tuple[int, str]] = _run_command,
        probe_version: Callable[[int], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        health_timeout: float = HEALTH_TIMEOUT_SECONDS,
    ) -> None:
        self.job = job
        self.journal_path = Path(journal_path)
        self.run_command = run_command
        self.probe_version = probe_version or _probe_version
        self.sleep = sleep
        self.clock = clock
        self.health_timeout = health_timeout
        self.install_root = Path(str(job.get("install_root") or ""))
        self.target = str(job.get("target_version") or "")
        self.previous = str(job.get("previous_version") or "")

    def execute(self) -> dict:
        self._write("installing", "正在运行安装程序")
        try:
            self._validate_job()
            self._run_installer()
            self._write("verifying", "安装完成，正在核对目标版本文件")
            self._verify_installed()
            self._write("restarting", "等待采集端切换到目标版本")
            if not self._wait_for_version(self.target):
                raise RuntimeError(f"服务未在 {self.health_timeout:.0f} 秒内切换到 {self.target}")
            self._write("completed", f"已切换到 {self.target}")
            return self.journal()
        except Exception as exc:
            message = str(exc) or "安装失败"
            if self._should_rollback():
                self._write("rolling_back", f"安装失败，正在回滚：{message}")
                rollback_ok, rollback_message = self._rollback()
                if rollback_ok and self._wait_for_version(self.previous):
                    self._write("rolled_back", f"{message}；已回滚到 {self.previous}")
                else:
                    self._write(
                        "failed",
                        f"{message}；回滚失败：{rollback_message or '服务未恢复'}",
                    )
            else:
                self._write("failed", message)
            return self.journal()

    # ---------- 步骤 ----------

    def _validate_job(self) -> None:
        if not self.install_root.is_absolute() or not self.install_root.is_dir():
            raise ValueError("安装目录无效")
        if self.install_root.is_symlink():
            raise ValueError("安装目录不允许是符号链接")
        if not self.previous or not self.target:
            raise ValueError("缺少上一版本或目标版本")
        if Version.parse(self.target) <= Version.parse(self.previous):
            raise ValueError("目标版本必须高于当前版本")
        artifact = Path(str(self.job.get("artifact_path") or ""))
        if not artifact.is_absolute() or not artifact.is_file():
            raise ValueError("更新包不存在")
        expected_sha = str(self.job.get("sha256") or "").lower()
        if not _SHA256_RE.fullmatch(expected_sha):
            raise ValueError("更新包 SHA-256 无效")
        if artifact.stat().st_size != int(self.job.get("size") or 0):
            raise ValueError("更新包大小与安装任务不一致")
        if _file_sha256(artifact) != expected_sha:
            raise ValueError("更新包 SHA-256 校验失败")

    def _run_installer(self) -> None:
        artifact = str(self.job.get("artifact_path") or "")
        code, output = self.run_command([
            artifact,
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            f"/LOG={self.journal_path.parent / 'update-install.log'}",
        ], INSTALL_TIMEOUT_SECONDS)
        if code != 0:
            raise RuntimeError(f"安装程序退出码 {code}：{output.strip()[:500]}")

    def _verify_installed(self) -> None:
        release = self.install_root / "releases" / self.target
        required = [
            release / "python" / "python.exe",
            release / "app" / "menu_capture" / "server.py",
        ]
        for path in required:
            if not path.is_file():
                raise RuntimeError(f"目标版本安装不完整，缺少 {path.name}")
        manifest_path = release / "release-manifest.json"
        manifest = _read_json(manifest_path, {})
        if str(manifest.get("version") or "") != self.target:
            raise RuntimeError("目标版本发布清单版本不匹配")
        entry = str(manifest.get("frontend_entry") or "")
        expected = str(manifest.get("frontend_sha256") or "").lower()
        if entry and expected:
            frontend = release / "app" / "web" / "react" / entry
            if not frontend.is_file() or _file_sha256(frontend) != expected:
                raise RuntimeError("目标版本前端入口哈希不匹配")
        registered = ""
        try:
            registered = (self.install_root / "current-version").read_text(encoding="utf-8").strip()
        except OSError:
            registered = ""
        if registered != self.target:
            raise RuntimeError(f"版本登记未切换到 {self.target}（当前 {registered or '空'}）")

    def _wait_for_version(self, version: str) -> bool:
        deadline = self.clock() + self.health_timeout
        while self.clock() < deadline:
            if self.probe_version(int(self.job.get("port") or 0)) == version:
                return True
            self.sleep(HEALTH_INTERVAL_SECONDS)
        return False

    def _should_rollback(self) -> bool:
        # 只有安装器可能已改动安装目录时才需要回滚；命令执行前失败无需触碰现有版本。
        try:
            registered = (self.install_root / "current-version").read_text(encoding="utf-8").strip()
        except OSError:
            return False
        return registered == self.target

    def _rollback(self) -> tuple[bool, str]:
        script = self.install_root / "rollback.ps1"
        if not script.is_file():
            return False, f"缺少回滚脚本 {script}"
        powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        code, output = self.run_command([
            str(powershell),
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-Version", self.previous,
            "-TimeoutSeconds", str(int(self.health_timeout)),
        ], self.health_timeout + 60)
        return code == 0, output.strip()[:500]

    # ---------- 日志 ----------

    def _write(self, status: str, message: str, error: str = "") -> None:
        journal = self.journal()
        journal.update({
            "status": status,
            "message": message,
            "error": error or (message if status in {"failed", "rolled_back"} else ""),
            "updated_at": _utc_now(),
        })
        if status in {"completed", "rolled_back", "failed"}:
            journal["finished_at"] = _utc_now()
        _write_json(self.journal_path, journal)

    def journal(self) -> dict:
        return _read_json(self.journal_path, {
            "status": "starting",
            "target_version": self.target,
            "previous_version": self.previous,
            "started_at": _utc_now(),
            "finished_at": "",
            "message": "",
            "error": "",
            "updated_at": _utc_now(),
        })


class InstallController:
    """服务端安装入口：只负责授权、状态机与独立进程启动，不直接执行安装。"""

    def __init__(
        self,
        *,
        download_dir: Path,
        install_root: Path | None,
        data_dir: Path,
        role: str,
        platform: str,
        arch: str,
        port: int,
        latest_provider: Callable[[], dict],
        artifact_provider: Callable[[], dict],
        current_version: Callable[[], str],
        idle_check: Callable[[], tuple[bool, str]],
        supported: bool = False,
        popen: Callable[..., Any] = subprocess.Popen,
        python: str | None = None,
        app_dir: Path | None = None,
    ) -> None:
        self.download_dir = Path(download_dir)
        self.install_root = Path(install_root) if install_root is not None else None
        self.journal_path = Path(data_dir) / "update-install.json"
        self.job_path = Path(data_dir) / "update-install-job.json"
        self.role = role
        self.platform = platform
        self.arch = arch
        self.port = port
        self.latest_provider = latest_provider
        self.artifact_provider = artifact_provider
        self.current_version = current_version
        self.idle_check = idle_check
        self.popen = popen
        self.python = python or sys.executable
        self.app_dir = Path(app_dir) if app_dir is not None else Path(__file__).resolve().parent.parent
        self._supported = bool(supported and self.install_root is not None
                               and role == "worker" and platform == "windows")
        # 单进程内串行化「检查状态 → 校验产物 → 写任务 → 启动进程」，避免两个请求同时通过空闲检查。
        self._lock = threading.Lock()

    @property
    def supported(self) -> bool:
        return self._supported

    def active(self) -> bool:
        return str(_read_json(self.journal_path, {}).get("status") or "") in ACTIVE_STATUSES

    def status(self) -> dict:
        journal = _read_json(self.journal_path, {})
        status = str(journal.get("status") or "idle")
        return {
            "supported": self.supported,
            "reason": "" if self.supported else "安装执行器需在 Windows 采集端并显式启用后才能使用",
            "active": status in ACTIVE_STATUSES,
            **journal,
        }

    def start(self, version: str, confirm: str) -> dict:
        with self._lock:
            return self._start_locked(version, confirm)

    def _start_locked(self, version: str, confirm: str) -> dict:
        if not self.supported:
            raise ValueError("当前环境未启用自动安装")
        version = str(version or "").strip()
        if not version or version != str(confirm or "").strip():
            raise ValueError("请确认要安装的版本号")
        if self.active():
            raise ValueError("已有安装任务正在执行")
        latest = self.latest_provider()
        if not isinstance(latest, dict) or not latest.get("eligible") or str(latest.get("version") or "") != version:
            raise ValueError("目标版本不是当前可更新的最新版本")
        previous = self.current_version().strip()
        if not previous:
            raise ValueError("无法确定当前运行版本，拒绝自动安装")
        if Version.parse(version) <= Version.parse(previous):
            raise ValueError("目标版本必须高于当前版本")
        artifact = verify_artifact(
            self.download_dir, self.artifact_provider(), expected_version=version,
            platform=self.platform, arch=self.arch,
        )
        # 产物必须与当前已验证发布清单一致；不能只信任本地下载状态。
        asset = next(
            (item for item in latest.get("assets") or []
             if isinstance(item, dict) and item.get("kind") == artifact["asset_kind"]),
            None,
        )
        if (
            asset is None
            or str(asset.get("name") or "") != artifact["asset_name"]
            or str(asset.get("sha256") or "").lower() != artifact["sha256"]
            or int(asset.get("size") or 0) != artifact["size"]
        ):
            raise ValueError("更新包与当前发布清单不一致，请重新检查更新并下载")
        idle, reason = self.idle_check()
        if not idle:
            raise ValueError(reason or "采集或标注仍在执行，暂不能安装")
        job = {
            **artifact,
            "install_root": str(self.install_root),
            "target_version": version,
            "previous_version": previous,
            "port": self.port,
            "created_at": _utc_now(),
        }
        _write_json(self.job_path, job)
        _write_json(self.journal_path, {
            "status": "starting",
            "target_version": version,
            "previous_version": previous,
            "started_at": _utc_now(),
            "finished_at": "",
            "message": "正在启动独立安装进程",
            "error": "",
            "updated_at": _utc_now(),
        })
        command = [
            self.python, "-m", "menu_capture.update_install",
            "--job", str(self.job_path), "--journal", str(self.journal_path),
        ]
        kwargs: dict[str, Any] = {
            "cwd": str(self.app_dir),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            # 独立进程不能随旧采集端一起被守护进程终止，否则无法完成健康验证与回滚。
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        self.popen(command, **kwargs)
        return self.status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行一次受控的应用更新安装")
    parser.add_argument("--job", required=True)
    parser.add_argument("--journal", required=True)
    args = parser.parse_args(argv)
    job = _read_json(Path(args.job), {})
    orchestrator = InstallOrchestrator(job=job, journal_path=Path(args.journal))
    try:
        orchestrator.execute()
    except Exception as exc:  # 独立进程不能把异常留给服务端，必须落盘
        _write_json(Path(args.journal), {
            **orchestrator.journal(),
            "status": "failed",
            "message": "安装进程异常退出",
            "error": str(exc),
            "finished_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
