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
import base64
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

from menu_capture.updates.updates import Version, _SHA256_RE, _file_sha256, _is_reparse, _normalize_arch

INSTALL_TIMEOUT_SECONDS = 900
HEALTH_TIMEOUT_SECONDS = 240
HEALTH_INTERVAL_SECONDS = 3
ACTIVE_STATUSES = {"starting", "installing", "verifying", "restarting", "rolling_back"}
ASSET_KIND = {"windows": "windows_installer"}
# 安装任务契约版本：控制器写、独立执行器读，字段只在 build_job 中定义一次。
JOB_SCHEMA = 1
# 兼容没有 pid 的旧任务：超过该时限仍未推进，视为执行器已异常退出。
STALE_JOB_GRACE_SECONDS = 120
# 执行器全部内部超时之和约 1680 秒（安装 900 + 健康 240 + 回滚 300 + 回滚后健康 240），
# 再留 5 分钟余量；超过该窗口仍无任何推进，即使 pid 仍存活（可能被复用）也判定事务失联，
# 否则更新失败后状态会永久停在 installing，界面再也无法重试。
MAX_ACTIVE_SECONDS = INSTALL_TIMEOUT_SECONDS + 2 * HEALTH_TIMEOUT_SECONDS + 600


def build_job(
    *,
    artifact: dict,
    install_root: Path,
    download_dir: Path,
    data_dir: Path,
    target_version: str,
    previous_version: str,
    port: int,
) -> dict:
    """生成安装任务文件内容，作为控制器与执行器之间唯一的字段契约。"""
    return {
        "schema": JOB_SCHEMA,
        "artifact_path": artifact["path"],
        "asset_name": artifact["asset_name"],
        "asset_kind": artifact["asset_kind"],
        "sha256": artifact["sha256"],
        "size": int(artifact["size"]),
        "download_dir": str(download_dir),
        "data_dir": str(data_dir),
        "install_root": str(install_root),
        "target_version": target_version,
        "previous_version": previous_version,
        "port": int(port),
        "created_at": _utc_now(),
    }


def _safe_join(base: Path, *parts: str) -> Path:
    """按受控片段拼接路径并逐级拒绝链接，返回解析后的真实路径。

    不能先 resolve() 再判断 is_symlink()：resolve 会把链接展开，检查必然失效。
    这里的顺序是校验片段 → 逐级检查链接 → 解析后确认仍位于 base 之内。
    """
    base = Path(base)
    for part in parts:
        text = str(part or "")
        if text in {"", ".", ".."} or Path(text).name != text:
            raise ValueError("更新包路径包含非法片段")
    current = base
    for part in parts:
        current = current / part
        if current.exists() and _is_reparse(current):
            raise ValueError("更新包路径经过符号链接或重解析点")
    try:
        root = base.resolve(strict=True)
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise ValueError("更新包文件不存在或不是普通文件，请重新下载") from exc
    if resolved != root and root not in resolved.parents:
        raise ValueError("更新包路径超出下载目录")
    return resolved


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
    try:
        Version.parse(version)
    except ValueError as exc:
        raise ValueError("更新包版本号无效") from exc
    sha = str(state.get("sha256") or "").lower()
    if not _SHA256_RE.fullmatch(sha):
        raise ValueError("下载记录缺少有效 SHA-256")
    try:
        size = int(state.get("total") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        raise ValueError("下载记录大小无效")
    # 路径由服务端按 数据目录/版本/资产名 推导，绝不使用状态文件里的任意路径；
    # 拼装过程逐级拒绝链接与越界，避免下载目录被替换后指到别处。
    expected = _safe_join(download_dir, version, name)
    recorded = str(state.get("path") or "")
    if recorded:
        try:
            recorded_path = Path(recorded).resolve()
        except OSError as exc:
            raise ValueError("更新包路径与版本目录不一致") from exc
        if recorded_path != expected:
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


def _probe_status(port: int, timeout: float = 2.0) -> dict:
    """同时探测版本与角色：只报对版本号不足以证明切换后的服务真的可用。"""
    result: dict = {}
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/version", timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    for key in ("version", "frontend", "commit"):
        if key in payload:
            result[key] = str(payload.get(key) or "")
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as response:  # noqa: S310
            health = json.loads(response.read(16 * 1024).decode("utf-8"))
    except Exception:
        result["ok"] = False
        return result
    if isinstance(health, dict):
        result["ok"] = bool(health.get("ok"))
        result["role"] = str(health.get("role") or "")
    return result


def _verify_authenticode(path: Path) -> dict:
    """读取安装包 Authenticode 签名状态，作为可信发布证明的一部分。

    当前发布流程未做代码签名，因此“未签名”需运维显式放行；签名被篡改必须直接拒绝。
    """
    if sys.platform != "win32":
        return {"status": "unsigned", "detail": "当前平台无法验证代码签名"}
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # 单引号字符串里的引号用双写转义，避免安装包路径含引号时拼出可执行脚本。
    literal = str(path).replace("'", "''")
    # 进程可能继承 PowerShell 7 的 PSModulePath，Windows PowerShell 5.1 会因此无法加载
    # Microsoft.PowerShell.Security，这里显式重置为系统模块目录再调用签名命令。
    script = (
        "$env:PSModulePath = "
        "([IO.Path]::Combine($env:SystemRoot, 'system32\\WindowsPowerShell\\v1.0\\Modules')) + ';' + "
        "([IO.Path]::Combine($env:ProgramFiles, 'WindowsPowerShell\\Modules')); "
        "Import-Module Microsoft.PowerShell.Security -ErrorAction Stop; "
        f"$s = Get-AuthenticodeSignature -LiteralPath '{literal}'; "
        "$signer = if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' }; "
        "[pscustomobject]@{ status = $s.Status.ToString(); signer = $signer } | ConvertTo-Json -Compress"
    )
    # 用 UTF-16LE 编码的命令行参数，规避引号、中文路径和代码页差异带来的解析问题。
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    code, output = _run_command(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], 30,
    )
    payload = None
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                payload = None
            break
    if code != 0 or not isinstance(payload, dict):
        return {"status": "unknown", "detail": output.strip()[:300]}
    label = str(payload.get("status") or "").lower()
    mapping = {"valid": "valid", "notsigned": "unsigned"}
    status = mapping.get(label, f"invalid:{label or 'unknown'}")
    return {"status": status, "signer": str(payload.get("signer") or "")}


def _check_publisher(result: dict) -> dict:
    status = str(result.get("status") or "")
    if status == "valid":
        return result
    if status == "unsigned":
        if os.environ.get("MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE") != "1":
            raise ValueError("安装包未通过可信发布证明校验（无有效代码签名）")
        return result
    detail = str(result.get("detail") or "").strip()
    raise ValueError(f"安装包数字签名无效（{status or '未知'}）{('：' + detail) if detail else ''}")


def _schema_fingerprint(data_dir: Path) -> list:
    """记录数据库结构，用于回滚前判断新版本是否已经改动过存储。"""
    db_path = Path(data_dir) / "tasks.sqlite3"
    if not db_path.is_file():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )]
            return [
                [table, [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]]
                for table in tables
            ]
    except sqlite3.Error:
        return []


def _pid_alive(pid: int) -> bool:
    """判断安装进程是否仍在运行；Windows 上不能用 os.kill(pid, 0)。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class InstallOrchestrator:
    """在独立进程中执行一次安装；所有外部命令与健康探测均可注入以便离线验证。"""

    def __init__(
        self,
        *,
        job: dict,
        journal_path: Path,
        run_command: Callable[[list[str], float], tuple[int, str]] = _run_command,
        probe_version: Callable[[int], Any] = _probe_status,
        verify_publisher: Callable[[Path], dict] = _verify_authenticode,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        health_timeout: float = HEALTH_TIMEOUT_SECONDS,
    ) -> None:
        self.job = job
        self.journal_path = Path(journal_path)
        self.run_command = run_command
        self.probe_version = probe_version
        self.verify_publisher = verify_publisher
        self.sleep = sleep
        self.clock = clock
        self.health_timeout = health_timeout
        self.install_root = Path(str(job.get("install_root") or ""))
        self.target = str(job.get("target_version") or "")
        self.previous = str(job.get("previous_version") or "")
        self.data_dir = Path(str(job.get("data_dir") or "")) if job.get("data_dir") else None
        self.artifact: Path | None = None
        self.expected_commit = ""
        self.schema_before: list = []

    def execute(self) -> dict:
        self._write("installing", "正在校验安装包")
        try:
            self._validate_job()
            publisher = _check_publisher(self.verify_publisher(self.artifact))
            self._write("installing", f"安装包校验通过（签名状态：{publisher.get('status')}），正在运行安装程序")
            self._run_installer()
            self._write("verifying", "安装完成，正在核对目标版本文件")
            self._verify_installed()
            self._write("restarting", "等待采集端切换到目标版本")
            if not self._wait_for_version(self.target, self.expected_commit):
                raise RuntimeError(f"服务未在 {self.health_timeout:.0f} 秒内切换到 {self.target}")
            self._write("completed", f"已切换到 {self.target}")
            return self.journal()
        except Exception as exc:
            message = str(exc) or "安装失败"
            if self._should_rollback():
                # 新版本可能已经迁移数据；此时切回旧版本会读不懂结构，必须拦下交人工处理。
                if self.schema_before and _schema_fingerprint(self.data_dir) != self.schema_before:
                    self._write(
                        "failed",
                        f"{message}；检测到数据库结构已被新版本修改，已阻止自动回滚，请人工确认兼容后回滚",
                    )
                    return self.journal()
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
        if int(self.job.get("schema") or 0) != JOB_SCHEMA:
            raise ValueError("安装任务格式不受支持，请重新发起安装")
        if not self.install_root.is_absolute() or not self.install_root.is_dir():
            raise ValueError("安装目录无效")
        if _is_reparse(self.install_root):
            raise ValueError("安装目录不允许是符号链接")
        if self.install_root == self.install_root.parent or not (self.install_root / "releases").is_dir():
            raise ValueError("安装目录不是有效的多版本安装根")
        if not self.previous or not self.target:
            raise ValueError("缺少上一版本或目标版本")
        if Version.parse(self.target) <= Version.parse(self.previous):
            raise ValueError("目标版本必须高于当前版本")
        # 产物路径必须由下载目录 + 版本 + 资产名推导，并在此刻重新校验大小与 SHA-256。
        download_dir = str(self.job.get("download_dir") or "")
        if not download_dir or not Path(download_dir).is_absolute():
            raise ValueError("下载目录无效")
        artifact = _safe_join(Path(download_dir), self.target, str(self.job.get("asset_name") or ""))
        recorded = str(self.job.get("artifact_path") or "")
        if not recorded or Path(recorded).resolve() != artifact:
            raise ValueError("安装任务中的更新包路径与下载目录不一致")
        expected_sha = str(self.job.get("sha256") or "").lower()
        if not _SHA256_RE.fullmatch(expected_sha):
            raise ValueError("更新包 SHA-256 无效")
        if artifact.stat().st_size != int(self.job.get("size") or 0):
            raise ValueError("更新包大小与安装任务不一致")
        if _file_sha256(artifact) != expected_sha:
            raise ValueError("更新包 SHA-256 校验失败")
        manifest = _read_json(self.install_root / "releases" / self.target / "release-manifest.json", {})
        self.expected_commit = str(manifest.get("git_commit") or "")
        self.artifact = artifact
        if self.data_dir is not None:
            self.schema_before = _schema_fingerprint(self.data_dir)

    def _run_installer(self) -> None:
        assert self.artifact is not None
        log_path = self.journal_path.parent / "update-install.log"
        # 参数里不要自带引号：subprocess 会按 CreateProcess 规则再转义一次，
        # 带引号的 /DIR="..." 会被 Inno 解析成含反斜杠的非法路径并立即以退出码 1 结束。
        switches = [
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            # 显式指定安装目录，避免依赖安装器默认路径或历史注册表值把文件装到别处。
            f"/DIR={self.install_root}",
            f"/LOG={log_path}",
        ]
        code, output = self.run_command([str(self.artifact), *switches], INSTALL_TIMEOUT_SECONDS)
        if code != 0:
            hint = ""
            if not log_path.exists():
                hint = f"（安装器未生成日志，参数：{' '.join(switches)}）"
            raise RuntimeError(f"安装程序退出码 {code}：{output.strip()[:500]}{hint}")

    def _verify_installed(self) -> None:
        release = self.install_root / "releases" / self.target
        required = [
            release / "python" / "python.exe",
            release / "app" / "menu_capture" / "api" / "server.py",
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

    def _probe(self, port: int) -> dict:
        value = self.probe_version(port)
        if isinstance(value, str):
            return {"version": value}
        return value if isinstance(value, dict) else {}

    def _wait_for_version(self, version: str, expected_commit: str = "") -> bool:
        """等待运行中的采集端真正切到目标版本，并核对角色与构建提交。"""
        port = int(self.job.get("port") or 0)
        deadline = self.clock() + self.health_timeout
        while self.clock() < deadline:
            info = self._probe(port)
            if str(info.get("version") or "") == version:
                role = str(info.get("role") or "")
                if role and role != "worker":
                    self.sleep(HEALTH_INTERVAL_SECONDS)
                    continue
                if info.get("ok") is False:
                    self.sleep(HEALTH_INTERVAL_SECONDS)
                    continue
                if "frontend" in info and not info["frontend"]:
                    self.sleep(HEALTH_INTERVAL_SECONDS)
                    continue
                if expected_commit and str(info.get("commit") or "") != expected_commit:
                    self.sleep(HEALTH_INTERVAL_SECONDS)
                    continue
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
            # 记录执行进程，服务端重启后可据此判断事务是否仍有活着的执行器。
            "pid": os.getpid(),
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
        pid_alive: Callable[[int], bool] = _pid_alive,
    ) -> None:
        self.download_dir = Path(download_dir)
        self.install_root = Path(install_root) if install_root is not None else None
        self.data_dir = Path(data_dir)
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
        self.app_dir = Path(app_dir) if app_dir is not None else Path(__file__).resolve().parents[2]
        self.pid_alive = pid_alive
        self._supported = bool(supported and self.install_root is not None
                               and role == "worker" and platform == "windows")
        # 串行化「检查状态 → 校验产物 → 写任务 → 启动进程」，避免两个请求同时通过空闲检查；
        # 同一线程内的 active()/status() 会递归进入，必须用可重入锁。
        self._lock = threading.RLock()

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def gate(self) -> threading.RLock:
        """与采集/标注启动共享的互斥闸门，闭合空闲检查到写任务之间的竞态窗口。"""
        return self._lock

    def active(self) -> bool:
        with self._lock:
            self._refresh()
            return str(_read_json(self.journal_path, {}).get("status") or "") in ACTIVE_STATUSES

    def active_nonblocking(self) -> bool:
        """不能阻塞的调用方（HTTP 中间件）使用：拿不到锁时只读任务文件，绝不等待。

        与 active() 的差别仅是跳过 _refresh 的失联收尾；活动事务在持锁方下一次
        刷新时收尾，这里只回答“当前是否可能处于活动事务中”。
        """
        if self._lock.acquire(blocking=False):
            try:
                self._refresh()
                journal = _read_json(self.journal_path, {})
            finally:
                self._lock.release()
        else:
            journal = _read_json(self.journal_path, {})
        return str(journal.get("status") or "") in ACTIVE_STATUSES

    def status(self) -> dict:
        with self._lock:
            self._refresh()
            journal = _read_json(self.journal_path, {})
        status = str(journal.get("status") or "idle")
        return {
            "supported": self.supported,
            "reason": "" if self.supported else "安装执行器需在 Windows 采集端并显式启用后才能使用",
            "active": status in ACTIVE_STATUSES,
            **journal,
        }

    def recover(self) -> None:
        """服务启动时调用：把上次进程遗留的活动事务收尾，避免界面永久停在 installing。"""
        with self._lock:
            self._refresh()

    def _active_age_seconds(self, journal: dict) -> float | None:
        """活动事务最近一次推进距今的秒数；时间戳不可读时回退到任务文件 mtime。"""
        text = str(journal.get("updated_at") or journal.get("started_at") or "")
        if text:
            try:
                stamp = datetime.fromisoformat(text)
            except ValueError:
                stamp = None
            if stamp is not None:
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
        try:
            return max(0.0, time.time() - self.journal_path.stat().st_mtime)
        except OSError:
            return None

    def _refresh(self) -> None:
        journal = _read_json(self.journal_path, {})
        if str(journal.get("status") or "") not in ACTIVE_STATUSES:
            return
        age = self._active_age_seconds(journal)
        if age is not None and age > MAX_ACTIVE_SECONDS:
            # 超过执行器全部内部超时仍无推进：进程可能已卡死或 pid 被复用，必须收尾。
            self._resolve_stale(journal)
            return
        pid = int(journal.get("pid") or 0)
        if pid:
            if self.pid_alive(pid):
                return
        else:
            # 任务刚写出、执行器尚未记录 pid 时不判定超时。
            try:
                age = time.time() - self.journal_path.stat().st_mtime
            except OSError:
                return
            if age < STALE_JOB_GRACE_SECONDS:
                return
        self._resolve_stale(journal)

    def _resolve_stale(self, journal: dict) -> None:
        target = str(journal.get("target_version") or "")
        registered = ""
        try:
            registered = (self.install_root / "current-version").read_text(encoding="utf-8").strip()
        except (AttributeError, OSError):
            registered = ""
        try:
            running = str(self.current_version() or "").strip()
        except Exception:
            running = ""
        if registered and registered == target == running:
            status = "completed"
            message = f"安装进程已退出，服务已运行 {target}"
        elif registered == target:
            status = "failed"
            message = f"安装进程异常退出：版本登记为 {target}，运行版本为 {running or '未知'}，请检查或手动回滚"
        else:
            status = "failed"
            message = f"安装进程异常退出，安装未生效（登记版本 {registered or '空'}）"
        journal.update({
            "status": status,
            "message": message,
            "error": "" if status == "completed" else message,
            "finished_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        _write_json(self.journal_path, journal)

    def start(self, version: str, confirm: str) -> dict:
        """授权并启动一次安装。

        锁契约：安装锁只在“确认空闲 + 写任务 + 启动进程”这个短窗口内持有。
        最新版本、下载状态、当前运行版本与整包 SHA-256 都会进入更新服务锁或做长耗时 I/O，
        必须在锁外完成；持安装锁调用它们会与状态接口（持更新锁读取安装快照）形成 ABBA 死锁。
        """
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
        previous = str(self.current_version() or "").strip()
        if not previous:
            raise ValueError("无法确定当前运行版本，拒绝自动安装")
        if Version.parse(version) <= Version.parse(previous):
            raise ValueError("目标版本必须高于当前版本")
        # 整包 SHA-256 可能持续数秒到数十秒，绝不能在持锁期间执行。
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
        with self._lock:
            # 锁内重新确认：锁外校验期间可能有第二个请求已完成启动，或采集刚刚开始。
            self._refresh()
            journal = _read_json(self.journal_path, {})
            if str(journal.get("status") or "") in ACTIVE_STATUSES:
                raise ValueError("已有安装任务正在执行")
            idle, reason = self.idle_check()
            if not idle:
                raise ValueError(reason or "采集或标注仍在执行，暂不能安装")
            job = build_job(
                artifact=artifact,
                install_root=self.install_root,
                download_dir=self.download_dir,
                data_dir=self.data_dir,
                target_version=version,
                previous_version=previous,
                port=self.port,
            )
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
                self.python, "-m", "menu_capture.updates.update_install",
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
            process = self.popen(command, **kwargs)
            pid = int(getattr(process, "pid", 0) or 0)
            if pid:
                # 立即登记执行器进程，避免状态刷新把刚启动的任务误判为异常退出。
                recorded = _read_json(self.journal_path, {})
                recorded["pid"] = pid
                _write_json(self.journal_path, recorded)
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
