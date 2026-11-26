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
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import urlopen

from menu_capture.updates import update_patch
from menu_capture.updates.updates import (
    ALLOWED_ASSET_HOSTS,
    INCREMENTAL_ENV,
    PATCH_ASSET_KIND,
    UNSIGNED_ENV,
    Version,
    _SHA256_RE,
    _downloadable_asset,
    _file_sha256,
    _http_open,
    _int_header,
    _is_reparse,
    _normalize_arch,
    _safe_asset_url,
)

INSTALL_TIMEOUT_SECONDS = 900
HEALTH_TIMEOUT_SECONDS = 240
HEALTH_INTERVAL_SECONDS = 3
ACTIVE_STATUSES = {"starting", "installing", "verifying", "restarting", "rolling_back"}
ASSET_KIND = {"windows": "windows_installer"}
# 安装任务契约版本：控制器写、独立执行器读，字段只在 build_job 中定义一次。
# schema 2 增加增量补丁模式；执行器仍兼容读取 schema 1 的全量任务。
JOB_SCHEMA = 2
SUPPORTED_JOB_SCHEMAS = {1, 2}
# 兼容没有 pid 的旧任务：超过该时限仍未推进，视为执行器已异常退出。
STALE_JOB_GRACE_SECONDS = 120
# 执行器全部内部超时之和约 1680 秒（安装 900 + 健康 240 + 回滚 300 + 回滚后健康 240），
# 再留 5 分钟余量；超过该窗口仍无任何推进，即使 pid 仍存活（可能被复用）也判定事务失联，
# 否则更新失败后状态会永久停在 installing，界面再也无法重试。
MAX_ACTIVE_SECONDS = INSTALL_TIMEOUT_SECONDS + 2 * HEALTH_TIMEOUT_SECONDS + 600
# 超时只代表疑似失联。执行器仍存活时不能解除安装互斥，否则会与旧进程同时操作版本目录与 journal。
STALLED_MESSAGE = "安装执行器长时间无响应，仍在等待其退出，请勿重复发起安装"


def build_job(
    *,
    artifact: dict,
    install_root: Path,
    download_dir: Path,
    data_dir: Path,
    target_version: str,
    previous_version: str,
    port: int,
    arch: str = "x64",
    patch: dict | None = None,
    fallback: dict | None = None,
) -> dict:
    """生成安装任务文件内容，作为控制器与执行器之间唯一的字段契约。

    patch 非空时使用增量模式；fallback 描述同一版本的完整安装包，用于补丁失败后
    在同一授权内自动回退。两者都只携带已校验的身份信息，不携带任意路径。
    """
    job = {
        "schema": JOB_SCHEMA,
        "download_dir": str(download_dir),
        "data_dir": str(data_dir),
        "install_root": str(install_root),
        "target_version": target_version,
        "previous_version": previous_version,
        "arch": str(arch or "x64"),
        "port": int(port),
        "created_at": _utc_now(),
    }
    if patch is not None:
        job.update({
            "asset_kind": PATCH_ASSET_KIND,
            "patch_path": patch["path"],
            "patch_asset_name": patch["asset_name"],
            "patch_sha256": patch["sha256"],
            "patch_size": int(patch["size"]),
            "base_version": patch["base_version"],
            "base_commit": str(patch.get("base_commit") or ""),
            "target_commit": str(patch.get("target_commit") or ""),
            "patch_catalog": str(patch.get("catalog") or ""),
        })
        if fallback is not None:
            job.update({
                "fallback_name": fallback["asset_name"],
                "fallback_sha256": fallback["sha256"],
                "fallback_size": int(fallback["size"]),
                "fallback_url": fallback["url"],
            })
        return job
    job.update({
        "artifact_path": artifact["path"],
        "asset_name": artifact["asset_name"],
        "asset_kind": artifact["asset_kind"],
        "sha256": artifact["sha256"],
        "size": int(artifact["size"]),
    })
    return job


def _safe_join(base: Path, *parts: str) -> Path:
    """按受控片段拼接路径并逐级拒绝链接，返回解析后的真实路径。

    不能先 resolve() 再判断 is_symlink()：resolve 会把链接展开，检查必然失效。
    这里的顺序是校验片段 → 逐级检查链接 → 解析后确认仍位于 base 之内。
    """
    base = Path(base)
    current = _prepare_path(base, *parts)
    try:
        root = base.resolve(strict=True)
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise ValueError("更新包文件不存在或不是普通文件，请重新下载") from exc
    if resolved != root and root not in resolved.parents:
        raise ValueError("更新包路径超出下载目录")
    return resolved


def _prepare_path(base: Path, *parts: str) -> Path:
    """拼接尚不存在的目标路径；逐级拒绝链接，最终路径不要求已存在。"""
    base = Path(base)
    if _is_reparse(base):
        raise ValueError("下载目录不允许是符号链接或重解析点")
    for part in parts:
        text = str(part or "")
        if text in {"", ".", ".."} or any(char in text for char in "/\\:"):
            raise ValueError("更新包路径包含非法片段")
    current = base
    for part in parts:
        current = current / part
        if _is_reparse(current):
            raise ValueError("更新包路径经过符号链接或重解析点")
    return current


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _artifact_identity(
    download_dir: Path,
    state: dict,
    *,
    expected_version: str,
    platform: str,
    arch: str = "",
    expected_kind: str | None = None,
) -> dict:
    """绑定可信下载记录与约定路径。文件可用性由安装方式决定。"""
    if not isinstance(state, dict) or state.get("status") != "completed":
        raise ValueError("尚未完成下载，不能安装")
    version = str(state.get("version") or "")
    if not version or version != str(expected_version or ""):
        raise ValueError("下载版本与安装目标不一致，请重新下载目标版本")
    kind = expected_kind or ASSET_KIND.get(platform, "")
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
    expected = _prepare_path(download_dir, version, name).resolve()
    recorded = str(state.get("path") or "")
    if recorded:
        try:
            recorded_path = Path(recorded).resolve()
        except OSError as exc:
            raise ValueError("更新包路径与版本目录不一致") from exc
        if recorded_path != expected:
            raise ValueError("更新包路径与版本目录不一致")
    return {
        "version": version,
        "asset_kind": kind,
        "asset_name": name,
        "sha256": sha,
        "size": size,
        "path": str(expected),
    }


def verify_artifact(
    download_dir: Path,
    state: dict,
    *,
    expected_version: str,
    platform: str,
    arch: str = "",
    expected_kind: str | None = None,
) -> dict:
    """安装前重新核对产物身份：版本、平台/架构、名称、大小、路径与 SHA-256。

    不信任下载结束时留下的 completed 状态；文件可能已被删除、替换或损坏。
    增量补丁由调用方显式传入 expected_kind，避免把补丁误当完整安装包。
    """
    artifact = _artifact_identity(
        download_dir, state, expected_version=expected_version,
        platform=platform, arch=arch, expected_kind=expected_kind,
    )
    expected = _safe_join(download_dir, artifact["version"], artifact["asset_name"])
    if not expected.is_file() or expected.is_symlink():
        raise ValueError("更新包文件不存在或不是普通文件，请重新下载")
    if expected.stat().st_size != artifact["size"]:
        raise ValueError("更新包大小与下载记录不一致，请重新下载")
    if _file_sha256(expected) != artifact["sha256"]:
        raise ValueError("更新包 SHA-256 校验失败，请重新下载")
    return artifact


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


def _verify_catalog_signature(patch_path: Path, catalog_path: Path) -> dict:
    """用已签名的文件目录校验补丁 ZIP，作为与 Authenticode 等价的可信证明。

    Test-FileCatalog 核对 ZIP 的 SHA-256 是否与目录一致，Get-AuthenticodeSignature
    核对目录本身由受信任证书签名；两者都通过才算有效。
    """
    if sys.platform != "win32":
        return {"status": "unknown", "detail": "当前平台无法验证目录签名"}
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    patch_literal = str(patch_path).replace("'", "''")
    catalog_literal = str(catalog_path).replace("'", "''")
    script = (
        "$env:PSModulePath = "
        "([IO.Path]::Combine($env:SystemRoot, 'system32\\WindowsPowerShell\\v1.0\\Modules')) + ';' + "
        "([IO.Path]::Combine($env:ProgramFiles, 'WindowsPowerShell\\Modules')); "
        "Import-Module Microsoft.PowerShell.Security -ErrorAction Stop; "
        f"$test = Test-FileCatalog -Path '{patch_literal}' -CatalogFilePath '{catalog_literal}' "
        "-CatalogVersion 2 -Detailed; "
        f"$sig = Get-AuthenticodeSignature -LiteralPath '{catalog_literal}'; "
        "[pscustomobject]@{ test = $test.Status.ToString(); signature = $sig.Status.ToString() } | "
        "ConvertTo-Json -Compress"
    )
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
    test_status = str(payload.get("test") or "").lower()
    signature = str(payload.get("signature") or "").lower()
    if test_status != "valid" or signature != "valid":
        return {"status": "invalid", "detail": f"目录校验 {test_status or '未知'}，签名 {signature or '未知'}"}
    return {"status": "valid", "detail": ""}


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
        verify_catalog: Callable[[Path, Path], dict] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        health_timeout: float = HEALTH_TIMEOUT_SECONDS,
    ) -> None:
        self.job = job
        self.journal_path = Path(journal_path)
        self.run_command = run_command
        self.probe_version = probe_version
        self.verify_publisher = verify_publisher
        self.verify_catalog = verify_catalog or _verify_catalog_signature
        self.sleep = sleep
        self.clock = clock
        self.health_timeout = health_timeout
        self.install_root = Path(str(job.get("install_root") or ""))
        self.target = str(job.get("target_version") or "")
        self.previous = str(job.get("previous_version") or "")
        self.data_dir = Path(str(job.get("data_dir") or "")) if job.get("data_dir") else None
        self.arch = _normalize_arch(job.get("arch")) or "x64"
        # 增量模式：补丁与同一版本的完整安装包身份都在任务里，执行器不信任下载状态。
        self.patch_mode = str(job.get("asset_kind") or "") == PATCH_ASSET_KIND
        self.patch: Path | None = None
        self.patch_base_dir: Path | None = None
        self.base_version = str(job.get("base_version") or "")
        self.base_commit = ""
        self.artifact: Path | None = None
        self.expected_commit = ""
        self.schema_before: list = []
        # 失败清理只允许触达本次事务确实创建的目录；绝不按版本号猜测并删除既有目录。
        self._stage_owned = False
        self._target_created = False

    def execute(self) -> dict:
        self._write("installing", "正在校验安装包")
        try:
            self._validate_job()
            if self.patch_mode:
                self._install_with_patch()
            else:
                self._install_full()
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
                    # 回滚成功后清理增量留下的目标目录，避免下次完整安装被 onlyifdoesntexist 跳过。
                    if self.patch_mode:
                        self._cleanup_target_best_effort()
                    self._write("rolled_back", f"{message}；已回滚到 {self.previous}")
                else:
                    self._write(
                        "failed",
                        f"{message}；回滚失败：{rollback_message or '服务未恢复'}",
                    )
            else:
                # 安装器未切换版本指针就失败时不会触发回滚；本次事务创建的目标目录必须清掉，
                # 否则残缺目录会让下一次安装因“目标版本目录已存在”永久失败。
                self._cleanup_target_best_effort()
                self._write("failed", message)
            return self.journal()

    def _install_full(self) -> None:
        publisher = _check_publisher(self.verify_publisher(self.artifact))
        self._write("installing", f"安装包校验通过（签名状态：{publisher.get('status')}），正在运行安装程序")
        # 任务校验时目标目录必须不存在；此后由安装器创建的内容都属于本次事务。
        self._target_created = True
        self._run_installer()

    def _install_with_patch(self) -> None:
        """先尝试增量；补丁在切换版本前失败时，同一授权内回退一次完整安装包。"""
        try:
            # 文件和基线不可用属于增量回退范围，任务身份和路径越界仍在 _validate_job 拒绝。
            self._verify_patch_applicability()
            self._verify_patch_trust()
            self._apply_patch()
        except Exception as exc:
            self._cleanup_stage()
            self._cleanup_target_best_effort()
            fallback = str(self.job.get("fallback_url") or "")
            if not fallback:
                raise RuntimeError(f"增量更新失败，且没有可用的完整安装包回退：{exc}") from exc
            self._write("installing", f"增量更新失败（{exc}），正在下载完整安装包回退")
            self.artifact = self._download_fallback(fallback)
            # 回退仍走现有可信发布校验，不因改用 ZIP 而放宽签名要求。
            self._install_full()
            return
        # 目标目录已完成重建，从这里开始失败由现有回滚规则处理。
        self._register_version()

    # ---------- 步骤 ----------

    def _validate_job(self) -> None:
        if int(self.job.get("schema") or 0) not in SUPPORTED_JOB_SCHEMAS:
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
        download_dir = str(self.job.get("download_dir") or "")
        if not download_dir or not Path(download_dir).is_absolute():
            raise ValueError("下载目录无效")
        releases = self.install_root / "releases"
        if _is_reparse(releases):
            raise ValueError("版本目录不允许是符号链接或重解析点")
        target_dir = releases / self.target
        if target_dir.exists() or _is_reparse(target_dir):
            raise ValueError("目标版本目录已存在，已拒绝覆盖安装")
        if self.patch_mode:
            if int(self.job.get("schema") or 0) != 2:
                raise ValueError("增量任务必须使用 schema 2")
            self._validate_patch_job(Path(download_dir))
        else:
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
            manifest = _read_json(releases / self.target / "release-manifest.json", {})
            self.expected_commit = str(manifest.get("git_commit") or "")
            self.artifact = artifact
            if self.data_dir is not None:
                self.schema_before = _schema_fingerprint(self.data_dir)

    def _validate_patch_job(self, download_dir: Path) -> None:
        """只校验任务身份与路径安全；补丁适用性延后到可回退的阶段判断。

        补丁文件缺失、大小/哈希不符、基线版本或 commit 不匹配都只说明“增量不可用”，
        必须能改走同一授权内的完整安装包；而路径越界、名称非法、回退身份缺失属于
        整个安装任务不可信，直接拒绝，避免带着不可兜底的任务继续执行。
        """
        patch = _prepare_path(
            download_dir, self.target, str(self.job.get("patch_asset_name") or "")
        )
        recorded = str(self.job.get("patch_path") or "")
        if not recorded or Path(recorded).resolve() != patch.resolve():
            raise ValueError("安装任务中的补丁路径与下载目录不一致")
        expected_sha = str(self.job.get("patch_sha256") or "").lower()
        if not _SHA256_RE.fullmatch(expected_sha):
            raise ValueError("补丁 SHA-256 无效")
        try:
            expected_size = int(self.job.get("patch_size") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("补丁大小无效") from exc
        if expected_size <= 0:
            raise ValueError("补丁大小无效")
        catalog = str(self.job.get("patch_catalog") or "")
        if catalog:
            _prepare_path(download_dir, self.target, catalog)
        self._validate_fallback_job()
        self.patch = patch
        self._patch_sha256 = expected_sha
        self._patch_size = expected_size
        self.expected_commit = str(self.job.get("target_commit") or "")
        if self.data_dir is not None:
            self.schema_before = _schema_fingerprint(self.data_dir)

    def _validate_fallback_job(self) -> None:
        """回退安装包身份必须一开始就合法，否则无法保证同一次授权内可以兜底。"""
        name = str(self.job.get("fallback_name") or "")
        if not name or Path(name).name != name:
            raise ValueError("回退安装包名称无效")
        sha = str(self.job.get("fallback_sha256") or "").lower()
        if not _SHA256_RE.fullmatch(sha):
            raise ValueError("回退安装包 SHA-256 无效")
        try:
            size = int(self.job.get("fallback_size") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("回退安装包大小无效") from exc
        if size <= 0:
            raise ValueError("回退安装包大小无效")
        url = str(self.job.get("fallback_url") or "")
        if not url:
            raise ValueError("缺少完整安装包回退地址，无法保证同一次授权内兜底")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_ASSET_HOSTS:
            raise ValueError("回退安装包地址不在允许的发布域名内")

    def _verify_patch_applicability(self) -> None:
        """增量是否适用于本机基线；失败只影响补丁，调用方会回退完整安装包。"""
        assert self.patch is not None
        patch = self.patch
        if not patch.is_file() or patch.is_symlink() or _is_reparse(patch):
            raise ValueError("补丁文件不存在或不可用")
        if patch.stat().st_size != self._patch_size:
            raise ValueError("补丁大小与安装任务不一致")
        if _file_sha256(patch) != self._patch_sha256:
            raise ValueError("补丁 SHA-256 校验失败")
        base_version = str(self.job.get("base_version") or "")
        if base_version != self.previous:
            raise ValueError("增量补丁的基线版本与当前版本不一致")
        releases = self.install_root / "releases"
        if not releases.is_dir() or releases.is_symlink() or _is_reparse(releases):
            raise ValueError("本地版本目录不可用")
        base_dir = self.install_root / "releases" / base_version
        if not base_dir.is_dir() or base_dir.is_symlink() or _is_reparse(base_dir):
            raise ValueError("本地基线版本目录不可用")
        base_manifest = _read_json(base_dir / "release-manifest.json", {})
        if str(base_manifest.get("version") or "") != base_version:
            raise ValueError("本地基线的发布清单版本不匹配")
        self.base_commit = str(base_manifest.get("git_commit") or "")
        if self.job.get("base_commit") and self.base_commit != self.job["base_commit"]:
            raise ValueError("本地基线的构建提交与补丁不一致")
        self.patch_base_dir = base_dir

    def _verify_patch_trust(self) -> None:
        """与完整安装包同级的可信校验：签名目录，或沿用既有 unsigned 放行口径。"""
        if os.environ.get(INCREMENTAL_ENV) != "1":
            raise ValueError("未启用增量更新，已回退完整安装包")
        catalog_name = str(self.job.get("patch_catalog") or "")
        if not catalog_name:
            if os.environ.get(UNSIGNED_ENV) != "1":
                raise ValueError("增量补丁缺少签名目录，已拒绝增量更新")
            return
        if Path(catalog_name).name != catalog_name:
            raise ValueError("补丁目录名称无效")
        catalog = _safe_join(
            Path(str(self.job.get("download_dir") or "")), self.target, catalog_name
        )
        result = self.verify_catalog(self.patch, catalog)
        if str(result.get("status") or "") != "valid":
            detail = str(result.get("detail") or "").strip()
            raise ValueError(f"补丁签名目录校验失败（{result.get('status') or '未知'}）{('：' + detail) if detail else ''}")

    def _apply_patch(self) -> None:
        assert self.patch is not None and self.patch_base_dir is not None
        releases = self.install_root / "releases"
        target_dir = releases / self.target
        if target_dir.exists():
            raise ValueError("目标版本目录已存在，已拒绝增量重建")
        stage = releases / f".{self.target}.patch-tmp"
        if stage.exists() or stage.is_symlink():
            # 无法确认这个暂存目录属于本次事务，宁可拒绝增量也不能删除别人的数据。
            raise ValueError("发现未清理的补丁暂存目录，已拒绝增量重建")
        self._stage_owned = True
        try:
            result = update_patch.apply(
                self.patch_base_dir,
                stage,
                self.patch,
                from_version=self.previous,
                to_version=self.target,
                platform="windows",
                arch=self.arch,
                from_commit=self.base_commit,
                to_commit=str(self.job.get("target_commit") or ""),
                scope_root=self.install_root,
            )
        except update_patch.PatchError as exc:
            raise ValueError(f"增量补丁不可用：{exc}") from exc
        if target_dir.exists():
            shutil.rmtree(stage, ignore_errors=True)
            self._stage_owned = False
            raise ValueError("目标版本目录在重建期间被创建，已拒绝覆盖")
        os.replace(stage, target_dir)
        self._stage_owned = False
        self._target_created = True
        self._write(
            "installing",
            f"增量重建完成（修改 {result['changed']} 个、删除 {result['deleted']} 个），正在登记版本",
        )

    def _register_version(self) -> None:
        script = self.install_root / "Launch.ps1"
        if not script.is_file():
            raise RuntimeError("缺少启动脚本，无法登记目标版本")
        powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        code, output = self.run_command([
            str(powershell),
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-Action", "Register",
            "-Version", self.target,
        ], 120)
        if code != 0:
            raise RuntimeError(f"版本登记失败（退出码 {code}）：{output.strip()[:300]}")

    def _download_fallback(self, url: str) -> Path:
        """按发布清单身份下载同一版本的完整安装包，用于补丁失败后的唯一一次回退。"""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_ASSET_HOSTS:
            raise ValueError("回退安装包地址不在允许的发布域名内")
        name = str(self.job.get("fallback_name") or "")
        if not name or Path(name).name != name:
            raise ValueError("回退安装包名称无效")
        expected_sha = str(self.job.get("fallback_sha256") or "").lower()
        if not _SHA256_RE.fullmatch(expected_sha):
            raise ValueError("回退安装包 SHA-256 无效")
        size = int(self.job.get("fallback_size") or 0)
        if size <= 0:
            raise ValueError("回退安装包大小无效")
        download_dir = Path(str(self.job.get("download_dir") or ""))
        target = _prepare_path(download_dir, self.target, name)
        if target.is_file() and target.stat().st_size == size and _file_sha256(target) == expected_sha:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        part = _prepare_path(download_dir, self.target, name + ".part")
        free = shutil.disk_usage(target.parent).free
        if free < size + 64 * 1024 * 1024:
            raise ValueError("磁盘剩余空间不足以回退完整安装包")
        written = 0
        digest = hashlib.sha256()
        with _http_open(url, {"Accept": "application/octet-stream", "User-Agent": "menu-capture-updater"}, 30.0) as opened:
            status, headers, response = opened
            if status != 200:
                raise RuntimeError(f"回退安装包下载失败（HTTP {status}）")
            length = _int_header(headers, "content-length")
            if length is not None and length != size:
                raise ValueError("回退安装包响应大小与发布清单不一致")
            with part.open("wb") as handle:
                for chunk in iter(lambda: response.read(256 * 1024), b""):
                    written += len(chunk)
                    if written > size:
                        part.unlink(missing_ok=True)
                        raise ValueError("回退安装包内容超过发布清单声明的大小")
                    digest.update(chunk)
                    handle.write(chunk)
        if written != size or digest.hexdigest() != expected_sha:
            part.unlink(missing_ok=True)
            raise ValueError("回退安装包完整性校验失败")
        os.replace(part, target)
        return target

    def _cleanup_stage(self) -> None:
        """只删除本次事务创建且可确认归属的补丁暂存目录。"""
        if not self._stage_owned:
            return
        stage = self.install_root / "releases" / f".{self.target}.patch-tmp"
        if stage.is_symlink():
            return
        shutil.rmtree(stage, ignore_errors=True)
        self._stage_owned = False

    def _cleanup_target_best_effort(self) -> None:
        """只清理本次事务创建的目标版本目录，且绝不动正在运行或既有完整版本。"""
        if not self._target_created:
            return
        target = self.install_root / "releases" / self.target
        if not target.is_dir() or target.is_symlink() or _is_reparse(target):
            return
        try:
            registered = (self.install_root / "current-version").read_text(encoding="utf-8").strip()
        except OSError:
            registered = ""
        # 运行指针仍指向目标，或目标恰是上一版本时，必须保留现场交给既有回滚/人工处理。
        if registered == self.target or self.target in {self.previous, registered}:
            return
        shutil.rmtree(target, ignore_errors=True)
        self._target_created = False

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
        authorization_check: Callable[[str], bool] | None = None,
        authorization_release: Callable[[str], None] | None = None,
        filesystem_lock: threading.Lock | None = None,
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
        # 调度路径在真正拉起进程前必须消费授权：授权消费与取消都走更新服务锁，只会有一个成功。
        # 回调会在持有本对象 _lock 时调用，因此对方不得反向回调本对象（单向锁序：安装锁 → 更新锁）。
        self.authorization_check = authorization_check
        self.authorization_release = authorization_release
        # 与存储清理互斥：清理持锁删除期间不允许启动安装，避免删掉安装正在读取的产物。
        self.filesystem_lock = filesystem_lock
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
        """事件循环等不能阻塞的调用方使用：只读任务文件，不取锁、不刷新、不写盘。

        写入端使用临时文件 + 原子替换，读取不会看到半份内容；读取失败按“非活动”处理，
        真正的互斥仍由 gate + active() 在请求线程内闭合。
        """
        return str(_read_json(self.journal_path, {}).get("status") or "") in ACTIVE_STATUSES

    def status(self) -> dict:
        with self._lock:
            self._refresh()
            journal = _read_json(self.journal_path, {})
            stalled = self._is_stalled(journal)
        status = str(journal.get("status") or "idle")
        payload = {
            "supported": self.supported,
            "reason": "" if self.supported else "安装执行器需在 Windows 采集端并显式启用后才能使用",
            "active": status in ACTIVE_STATUSES,
            "stalled": stalled,
            **journal,
        }
        if stalled:
            # 疑似失联只作为本次查询的派生提示：journal 由执行器进程独占写入，
            # 父进程改写会覆盖执行器的真实进度，并与其并发写同一个临时文件。
            payload["message"] = STALLED_MESSAGE
        return payload

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

    def _is_stalled(self, journal: dict) -> bool:
        """只读判断“疑似失联”：执行器仍存活但长时间无任何推进。

        仅用于状态查询派生，绝不回写 journal；安装互斥照旧由 status 仍为活动态保持。
        """
        if str(journal.get("status") or "") not in ACTIVE_STATUSES:
            return False
        pid = int(journal.get("pid") or 0)
        if not pid or not self.pid_alive(pid):
            return False
        age = self._active_age_seconds(journal)
        return age is not None and age > MAX_ACTIVE_SECONDS

    def _refresh(self) -> None:
        journal = _read_json(self.journal_path, {})
        if str(journal.get("status") or "") not in ACTIVE_STATUSES:
            return
        pid = int(journal.get("pid") or 0)
        if pid:
            if self.pid_alive(pid):
                # 超时只代表疑似失联：执行器仍存活时若解除互斥，会与旧进程同时操作
                # 版本目录、版本指针与 journal，因此保持活动态，提示由 status 派生。
                return
            self._resolve_stale(journal)
            return
        # 没有 pid：任务刚写出时留给执行器登记，超过宽限期才判定异常退出。
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

    def start(self, version: str, confirm: str, *, require_authorization: bool = False) -> dict:
        """授权并启动一次安装。

        锁契约：安装锁只在“确认空闲 + 写任务 + 启动进程”这个短窗口内持有。
        最新版本、下载状态、当前运行版本与整包 SHA-256 都会进入更新服务锁或做长耗时 I/O，
        必须在锁外完成；持安装锁调用它们会与状态接口（持更新锁读取安装快照）形成 ABBA 死锁。

        require_authorization 仅用于后台调度：真正拉起进程前消费一次更新授权，
        使用户在锁外校验期间取消更新时安装不会继续执行。
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
        # 完整安装包在锁外校验。补丁只核对发布身份与路径，文件和基线交由独立执行器检查，
        # 这样补丁无法使用时，执行器还能在同一次授权内校验并运行完整安装包。
        state = self.artifact_provider()
        use_patch = isinstance(state, dict) and state.get("asset_kind") == PATCH_ASSET_KIND
        patch_meta: dict | None = None
        fallback_meta: dict | None = None
        if use_patch:
            artifact = _artifact_identity(
                self.download_dir, state, expected_version=version, platform=self.platform,
                arch=self.arch, expected_kind=PATCH_ASSET_KIND,
            )
            base_version = str(state.get("from_version") or "")
            patch_asset = next(
                (item for item in latest.get("patches") or []
                 if isinstance(item, dict)
                 and str(item.get("name") or "") == artifact["asset_name"]
                 and str(item.get("from") or "") == base_version
                 and item.get("platform") == self.platform
                 and _normalize_arch(item.get("arch")) == _normalize_arch(self.arch)),
                None,
            )
            if patch_asset is None:
                raise ValueError("增量补丁与当前发布清单不一致，请重新检查更新")
            if (
                str(patch_asset.get("sha256") or "").lower() != artifact["sha256"]
                or int(patch_asset.get("size") or 0) != artifact["size"]
            ):
                raise ValueError("增量补丁身份与发布清单不一致，请重新下载")
            full_asset = _downloadable_asset(latest, self.platform, _normalize_arch(self.arch))
            if full_asset is None:
                raise ValueError("发布清单缺少完整安装包，无法保证增量失败时回退")
            catalog = str(patch_asset.get("catalog") or "")
            if catalog:
                _prepare_path(self.download_dir, version, catalog)
            fallback_name = str(full_asset.get("name") or "")
            _prepare_path(self.download_dir, version, fallback_name)
            fallback_url = _safe_asset_url(
                str(full_asset.get("url") or ""), str(latest.get("tag") or f"v{version}"),
            )
            patch_meta = {
                "path": artifact["path"],
                "asset_name": artifact["asset_name"],
                "sha256": artifact["sha256"],
                "size": artifact["size"],
                "base_version": base_version,
                "target_commit": str(latest.get("git_commit") or ""),
                "catalog": catalog,
            }
            fallback_meta = {
                "asset_name": fallback_name,
                "sha256": str(full_asset.get("sha256") or ""),
                "size": int(full_asset.get("size") or 0),
                "url": fallback_url,
            }
        else:
            artifact = verify_artifact(
                self.download_dir, state, expected_version=version, platform=self.platform, arch=self.arch,
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
            claimed = False
            if require_authorization:
                # 授权消费与取消共用更新服务锁：先到者生效，不会出现“取消成功但安装仍启动”。
                if self.authorization_check is None or not self.authorization_check(version):
                    raise ValueError("更新授权已取消，安装未启动")
                claimed = True
            filesystem_lock = self.filesystem_lock
            acquired_fs = False
            if filesystem_lock is not None:
                # 不等待：清理可能持续数十秒，长时间占着安装锁会把状态接口一起拖住。
                if not filesystem_lock.acquire(blocking=False):
                    self._release_claim(claimed, version)
                    raise ValueError("存储清理进行中，请稍后重试")
                acquired_fs = True
            try:
                job = build_job(
                    artifact=artifact,
                    install_root=self.install_root,
                    download_dir=self.download_dir,
                    data_dir=self.data_dir,
                    target_version=version,
                    previous_version=previous,
                    port=self.port,
                    arch=self.arch,
                    patch=patch_meta,
                    fallback=fallback_meta,
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
                # spawn 之后 journal 归子进程独占：执行器启动后的第一件事就是带自身 pid 写入
                # （execute → _write）。父进程再读-改-写会覆盖执行器进度，并与其共用同一个
                # .tmp 临时文件造成写坏；子进程登记 pid 之前的空窗由 _refresh 的
                # STALE_JOB_GRACE_SECONDS 兜底，不会被误判为异常退出。
                self.popen(command, **kwargs)
            except BaseException:
                # 未成功启动时归还授权，避免后台调度因为一次可重试失败而永久失去授权。
                self._release_claim(claimed, version)
                raise
            finally:
                if acquired_fs:
                    filesystem_lock.release()
        return self.status()

    def _release_claim(self, claimed: bool, version: str) -> None:
        if claimed and self.authorization_release is not None:
            self.authorization_release(version)


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
