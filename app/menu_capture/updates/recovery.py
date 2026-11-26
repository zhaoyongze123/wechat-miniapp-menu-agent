"""恢复协调器的可测试探测逻辑：一致性副本、隔离验证与回滚资格判断。

原则：
- 真实数据库绝不作为新版本首次启动的试验品；先用 SQLite backup API 生成一致性副本，
  在隔离目录完成迁移/启动验证；
- 回滚前必须重新验证数据库未变化、恢复包摘要一致，不能只凭版本号或超时；
- 该模块只做只读探测与证据生成，不执行安装、差分或目录切换。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

SKIP_DIRS = {
    "materials", "output", "outputs", "screenshots", "logs", "debug-logs",
    "cache", "recovery", "web", "node_modules", "__pycache__",
}
SKIP_SUFFIXES = {
    ".log", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".mp4", ".mov", ".zip",
    ".nupkg", ".exe", ".dll", ".pdb", ".pyc",
}
MAX_COPY_BYTES = 2 * 1024 * 1024
MAX_SQLITE_BYTES = 4 * 1024 * 1024 * 1024
DB_NAMES = re.compile(r".+\.sqlite3?\Z")


class ProcessState(str, Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


def sha256_file(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _linux_process_created_at(pid: int) -> float | None:
    """读取 Linux /proc 的真实进程创建时间；信息不足时不猜测。"""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = stat_text[stat_text.rfind(")") + 2:].split()
        start_ticks = int(tail[19])  # /proc/<pid>/stat 的第 22 字段
        boot_line = next(
            line for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
            if line.startswith("btime ")
        )
        boot_time = int(boot_line.split()[1])
        return boot_time + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, StopIteration, ValueError, IndexError, ZeroDivisionError):
        return None


def _windows_process_snapshot(pid: int) -> tuple[ProcessState, float | None]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # OpenProcess 的 ERROR_INVALID_PARAMETER 明确表示 PID 不存在；访问拒绝及其他错误未知。
        return (ProcessState.DEAD if ctypes.get_last_error() == 87 else ProcessState.UNKNOWN), None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return ProcessState.UNKNOWN, None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        created_at = ticks / 10_000_000 - 11644473600
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return ProcessState.UNKNOWN, created_at
        if exit_code.value != 259:  # STILL_ACTIVE
            return ProcessState.DEAD, created_at
        return ProcessState.ALIVE, created_at
    finally:
        kernel32.CloseHandle(handle)


def process_created_at(pid: int) -> float | None:
    """返回操作系统记录的进程创建时间；无法可靠读取时返回 None。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_snapshot(pid)[1]
    if sys.platform.startswith("linux"):
        return _linux_process_created_at(pid)
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)], check=True,
                capture_output=True, text=True, timeout=3,
            )
            value = datetime.strptime(completed.stdout.strip(), "%a %b %d %H:%M:%S %Y")
            return value.timestamp()
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def current_process_created_at() -> float:
    """获取当前协调器进程的 OS 创建时间；不能用 Python 初始化时刻代替。"""
    created_at = process_created_at(os.getpid())
    if created_at is None:
        raise RuntimeError("无法读取当前协调器的操作系统创建时间")
    return created_at


def _process_snapshot(pid: int) -> tuple[ProcessState, float | None]:
    if os.name == "nt":
        return _windows_process_snapshot(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessState.DEAD, None
    except PermissionError:
        return ProcessState.UNKNOWN, None
    except OSError:
        return ProcessState.UNKNOWN, None
    return ProcessState.ALIVE, process_created_at(pid)


def process_state(pid: int, started_at: float | None = None) -> ProcessState:
    """区分进程存活、确证退出与无法确认，未知时不得接管旧所有者。"""
    if type(pid) is not int or pid <= 0:
        return ProcessState.DEAD
    state, created_at = _process_snapshot(pid)
    if state is not ProcessState.ALIVE:
        return state
    if started_at is None:
        return ProcessState.ALIVE
    if created_at is None:
        return ProcessState.UNKNOWN
    try:
        expected_created_at = float(started_at)
    except (TypeError, ValueError, OverflowError):
        return ProcessState.UNKNOWN
    if not math.isfinite(expected_created_at):
        return ProcessState.UNKNOWN
    # 比较 OS 创建时间，而不是调用方记录的应用初始化时刻；差异即代表 PID 已复用。
    return ProcessState.DEAD if abs(created_at - expected_created_at) > 0.001 else ProcessState.ALIVE


def process_alive(pid: int, started_at: float | None = None) -> bool:
    """兼容旧调用者；UNKNOWN 按仍存活处理，避免不安全地接管事务。"""
    return process_state(pid, started_at) is not ProcessState.DEAD


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def release_manifest(current_dir: Path) -> dict:
    payload = read_json(Path(current_dir) / "release-manifest.json", {})
    return payload if isinstance(payload, dict) else {}


def verify_release(current_dir: Path, version: str, commit: str) -> dict:
    """校验实际安装目录的版本、commit 与前端入口哈希。"""
    manifest = release_manifest(current_dir)
    if str(manifest.get("version") or "") != str(version):
        raise ValueError("安装目录版本与事务目标不一致")
    if str(manifest.get("git_commit") or "") != str(commit):
        raise ValueError("安装目录 commit 与事务目标不一致")
    entry = str(manifest.get("frontend_entry") or "")
    digest = str(manifest.get("frontend_sha256") or "").lower()
    relative = Path(entry)
    if (not entry or relative.is_absolute() or ".." in relative.parts
            or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("发布清单缺少有效前端入口指纹")
    target = Path(current_dir) / "web" / "react" / relative
    if not target.is_file() or sha256_file(target) != digest:
        raise ValueError("前端入口文件哈希与发布清单不一致")
    return manifest


def database_path(data_dir: Path) -> Path | None:
    path = Path(data_dir) / "tasks.sqlite3"
    return path if path.is_file() else None


def _sqlite_files(data_dir: Path) -> list[Path]:
    root = Path(data_dir)
    if not root.is_dir():
        raise ValueError(f"隔离源数据目录不存在：{root}")
    found: list[Path] = []

    def walk_error(error: OSError) -> None:
        raise ValueError(f"无法完整枚举 SQLite 数据目录：{error}") from error

    for directory, _dirs, names in os.walk(root, onerror=walk_error, followlinks=False):
        for name in names:
            if not DB_NAMES.fullmatch(name):
                continue
            item = Path(directory) / name
            try:
                if item.is_symlink() or not item.is_file():
                    raise ValueError(f"SQLite 数据库不是普通文件：{item}")
                item.stat()
            except OSError as exc:
                raise ValueError(f"无法读取 SQLite 文件属性：{item}") from exc
            found.append(item)
    return found


def backup_sqlite(source: Path, destination: Path) -> None:
    """使用 SQLite backup API 生成包含 WAL 已提交内容的一致性副本。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            dst.commit()


def _sqlite_storage_size(database: Path) -> int:
    """把主库和已提交内容可能仍在的 WAL 一并计入快照上限。"""
    total = 0
    for candidate in (Path(database), Path(f"{database}-wal")):
        try:
            if candidate.exists():
                if candidate.is_symlink() or not candidate.is_file():
                    raise ValueError(f"SQLite 文件不是普通文件：{candidate}")
                total += candidate.stat().st_size
        except OSError as exc:
            raise ValueError(f"无法读取 SQLite 文件大小：{candidate}") from exc
    return total


def schema_of(database: Path) -> dict | None:
    try:
        with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as db:
            objects = [list(row) for row in db.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]
            if not any(row[0] == "table" and not row[1].startswith("sqlite_") for row in objects):
                return None
            return {"user_version": db.execute("PRAGMA user_version").fetchone()[0],
                    "objects": objects}
    except (OSError, sqlite3.Error, ValueError):
        return None


def database_metadata(database: Path) -> dict:
    """记录数据库结构与每张业务表的行数，用于确认隔离副本完整。"""
    with closing(sqlite3.connect(f"{Path(database).resolve().as_uri()}?mode=ro", uri=True)) as db:
        objects = [list(row) for row in db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]
        tables = [str(row[1]) for row in objects if row[0] == "table" and not str(row[1]).startswith("sqlite_")]
        if not tables:
            raise ValueError(f"SQLite 数据库缺少业务表：{database}")
        table_columns: dict[str, list[dict[str, Any]]] = {}
        row_counts: dict[str, int] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            table_columns[table] = [
                {"name": str(row[1]), "type": str(row[2]), "notnull": int(row[3]),
                 "default": row[4], "pk": int(row[5])}
                for row in db.execute(f"PRAGMA table_info({quoted})")
            ]
            row_counts[table] = int(db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        integrity = db.execute("PRAGMA quick_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError(f"SQLite 完整性检查失败：{database}")
        return {
            "schema": {"user_version": db.execute("PRAGMA user_version").fetchone()[0],
                       "objects": objects, "table_columns": table_columns},
            "row_counts": row_counts,
        }


def database_readable(data_dir: Path) -> tuple[bool, str]:
    path = database_path(data_dir)
    if path is None:
        return False, "缺少 tasks.sqlite3"
    try:
        actual = database_metadata(path)
        if not {"tasks", "runs"} <= set(actual["row_counts"]):
            return False, "数据库缺少核心业务表"
        # 隔离验证必须有创建副本时的清单；不能让启动时新建的空库冒充被验证的真实数据。
        evidence_path = Path(data_dir).parent / "verify-source.json"
        if evidence_path.exists():
            evidence = read_json(evidence_path, {})
            expected = evidence.get("databases") if isinstance(evidence, dict) else None
            if not isinstance(expected, dict) or "tasks.sqlite3" not in expected:
                return False, "隔离验证缺少主数据库来源清单"
            for relative, baseline in expected.items():
                source = Path(relative)
                if (source.is_absolute() or ".." in source.parts or not isinstance(baseline, dict)):
                    return False, "隔离验证数据库来源清单无效"
                copied_path = Path(data_dir) / source
                if not copied_path.is_file():
                    return False, f"隔离数据库副本缺失：{relative}"
                copied = database_metadata(copied_path)
                if not _preserves_database(baseline, copied):
                    return False, f"隔离数据库结构或行数不完整：{relative}"
        return True, ""
    except (OSError, sqlite3.Error, ValueError) as exc:
        return False, f"数据库不可读：{exc}"


def snapshot_database(data_dir: Path, destination: Path) -> dict:
    """记录旧库一致性快照；回滚前重新生成并比对摘要，证明数据库未被新版本改写。"""
    path = database_path(data_dir)
    if path is None:
        raise ValueError("缺少可回滚数据库")
    metadata = database_metadata(path)
    backup_sqlite(path, destination)
    copied_metadata = database_metadata(destination)
    if metadata != copied_metadata:
        raise ValueError("SQLite 一致性快照的结构或关键表行数不一致")
    files = {}
    for suffix in ("", "-wal", "-shm"):
        item = Path(str(path) + suffix)
        if item.is_file():
            files[item.name] = {"sha256": sha256_file(item), "size": item.stat().st_size}
    return {"path": str(destination), "sha256": sha256_file(destination),
            **metadata,
            "size": destination.stat().st_size, "files": files, "created_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}


def _preserves_database(baseline: dict, current: dict) -> bool:
    """迁移后的隔离库必须保留旧表/字段/对象且不得减少既有表的行数。"""
    expected_schema = baseline.get("schema")
    actual_schema = current.get("schema")
    expected_counts = baseline.get("row_counts")
    actual_counts = current.get("row_counts")
    if not all(isinstance(item, dict) for item in (
            expected_schema, actual_schema, expected_counts, actual_counts)):
        return False
    expected_objects = {(str(row[0]), str(row[1])) for row in expected_schema.get("objects", [])
                        if isinstance(row, list) and len(row) >= 2}
    actual_objects = {(str(row[0]), str(row[1])) for row in actual_schema.get("objects", [])
                      if isinstance(row, list) and len(row) >= 2}
    if not expected_objects <= actual_objects:
        return False
    actual_columns = actual_schema.get("table_columns")
    expected_columns = expected_schema.get("table_columns")
    if not isinstance(actual_columns, dict) or not isinstance(expected_columns, dict):
        return False
    for table, columns in expected_columns.items():
        actual_table_columns = actual_columns.get(table, [])
        if not isinstance(columns, list) or not isinstance(actual_table_columns, list):
            return False
        by_name = {column.get("name"): column for column in actual_table_columns
                   if isinstance(column, dict)}
        for expected_column in columns:
            if not isinstance(expected_column, dict):
                return False
            actual_column = by_name.get(expected_column.get("name"))
            if actual_column is None:
                return False
            # 允许迁移追加字段，但已有字段类型、约束和默认值必须保留。
            if any(actual_column.get(key) != expected_column.get(key)
                   for key in ("type", "notnull", "default", "pk")):
                return False
    expected_definitions = {
        (str(row[0]), str(row[1])): row
        for row in expected_schema.get("objects", [])
        if isinstance(row, list) and len(row) >= 4 and row[0] in {"index", "trigger", "view"}
    }
    actual_definitions = {
        (str(row[0]), str(row[1])): row
        for row in actual_schema.get("objects", [])
        if isinstance(row, list) and len(row) >= 4
    }
    for key, definition in expected_definitions.items():
        if actual_definitions.get(key) != definition:
            return False
    for table, count in expected_counts.items():
        actual = actual_counts.get(table)
        if type(count) is not int or type(actual) is not int or actual < count:
            return False
    return True


def database_unchanged(data_dir: Path, snapshot: dict, workdir: Path) -> bool:
    expected = str(snapshot.get("sha256") or "") if isinstance(snapshot, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    probe = Path(workdir) / "rollback-probe.sqlite3"
    try:
        path = database_path(data_dir)
        if path is None:
            return False
        backup_sqlite(path, probe)
        return sha256_file(probe) == expected
    except (OSError, sqlite3.Error, ValueError):
        return False
    finally:
        probe.unlink(missing_ok=True)


def prepare_verify_home(
    data_root: Path,
    destination: Path,
    *,
    transaction_id: str,
    forbidden_roots: tuple[Path, ...] = (),
) -> dict:
    """构造隔离验证目录：数据库用一致性副本，配置/身份文件只复制小文件。"""
    data_root = Path(data_root)
    destination = Path(destination)
    data_root = data_root.resolve()
    destination = destination.resolve()
    if (destination == data_root or destination.is_relative_to(data_root)
            or data_root.is_relative_to(destination)):
        raise ValueError("隔离验证目录必须与生产数据目录完全分离")
    for forbidden in forbidden_roots:
        resolved_forbidden = Path(forbidden).resolve()
        if (destination == resolved_forbidden
                or destination.is_relative_to(resolved_forbidden)
                or resolved_forbidden.is_relative_to(destination)):
            raise ValueError(f"隔离验证目录不得与受保护安装目录重叠：{resolved_forbidden}")
    if destination.exists():
        raise ValueError(f"隔离验证目录已存在，拒绝复用旧副本：{destination}")
    destination.mkdir(parents=True)
    data_dir = destination / "data"
    data_dir.mkdir()
    copied: list[str] = []
    databases: dict[str, dict] = {}
    source_data = data_root / "data"
    if not source_data.is_dir() or database_path(source_data) is None:
        raise ValueError("隔离验证缺少主数据库 tasks.sqlite3")
    sqlite_files = _sqlite_files(source_data)
    if not sqlite_files or database_path(source_data) not in sqlite_files:
        raise ValueError("隔离验证数据库清单为空或缺少主数据库")
    for item in sqlite_files:
        size = _sqlite_storage_size(item)
        if size > MAX_SQLITE_BYTES:
            raise ValueError(f"SQLite 主库及 WAL 超过隔离快照上限：{item}")
        relative = item.relative_to(source_data)
        target = data_dir / relative
        source_metadata = database_metadata(item)
        backup_sqlite(item, target)
        copied_metadata = database_metadata(target)
        if source_metadata != copied_metadata:
            raise ValueError(f"SQLite 一致性副本的结构或关键表行数不一致：{relative}")
        databases[str(relative)] = {
            "sha256": sha256_file(target), "size": target.stat().st_size, **source_metadata,
        }
        copied.append(str(Path("data") / relative))
    if not databases or "tasks.sqlite3" not in databases:
        raise ValueError("隔离验证数据库清单为空或缺少主数据库")

    def walk_error(error: OSError) -> None:
        raise ValueError(f"无法完整枚举隔离验证所需文件：{error}") from error

    for directory, dirs, names in os.walk(data_root, topdown=True, onerror=walk_error, followlinks=False):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in names:
            item = Path(directory) / name
            relative = item.relative_to(data_root)
            if DB_NAMES.fullmatch(item.name) or item.name.endswith(("-wal", "-shm")):
                continue
            try:
                if item.is_symlink():
                    raise ValueError(f"隔离验证必需文件不能是符号链接：{relative}")
                if not item.is_file():
                    continue
                size = item.stat().st_size
            except OSError as exc:
                raise ValueError(f"无法读取隔离验证文件：{relative}") from exc
            if item.suffix.lower() in SKIP_SUFFIXES or size > MAX_COPY_BYTES:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(item, target)
            except OSError as exc:
                raise ValueError(f"无法复制隔离验证必需文件：{relative}") from exc
            copied.append(str(relative))
    evidence = {"transaction_id": transaction_id, "source": str(data_root),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                "databases": databases, "files": sorted(copied)}
    write_json(destination / "verify-source.json", evidence)
    return evidence


def decide_after_apply(*, installed_version: str, target_version: str, previous_version: str,
                       health_ok: bool, schema_ok: bool, recovery_ok: bool) -> str:
    """只输出三种结论；不以版本相等或超时自动放行。"""
    if installed_version == target_version:
        if health_ok and schema_ok:
            return "completed"
        if not health_ok and schema_ok and recovery_ok:
            return "rollback"
        return "recovery_required"
    if installed_version == previous_version:
        return "recovery_required"
    return "recovery_required"


def _http_json(url: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return json.loads(response.read(1024 * 1024).decode("utf-8"))


def verify_health(port: int, version: str, commit: str) -> tuple[bool, str]:
    base = f"http://127.0.0.1:{int(port)}"
    try:
        health = _http_json(base + "/healthz")
        if health.get("ok") is not True or health.get("role") != "worker":
            return False, "healthz 角色不匹配"
        info = _http_json(base + "/api/version")
        if str(info.get("version") or "") != str(version):
            return False, "运行版本与目标不一致"
        if str(info.get("commit") or "") != str(commit):
            return False, "运行 commit 与目标不一致"
        if not str(info.get("frontend") or ""):
            return False, "前端入口未报告"
        return True, ""
    except Exception as exc:  # noqa: BLE001 - 探测失败统一返回原因
        return False, f"健康探测失败：{exc}"


def request_verify_exit(port: int, token: str, *, transaction_id: str, target_version: str,
                        verify_pid: int) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api/internal/verify-exit",
        data=json.dumps({"transaction_id": transaction_id, "target_version": target_version,
                         "verify_pid": int(verify_pid)}).encode("utf-8"),
        method="POST",
        headers={"X-Coordinator-Token": token, "Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read()).get("ok") is True
    except Exception:
        return False


def request_worker_shutdown_result(port: int, token: str, *, transaction_id: str,
                                   target_version: str) -> tuple[str, str]:
    """区分明确接受、明确拒绝（HTTP 错误）与传输/响应丢失，避免响应丢失误解锁。"""
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api/internal/shutdown",
        data=json.dumps({"transaction_id": transaction_id,
                         "target_version": target_version}).encode("utf-8"),
        method="POST",
        headers={"X-Coordinator-Token": token, "Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            payload = json.loads(response.read())
            if payload.get("ok") is True:
                return "accepted", ""
            return "rejected", str(payload.get("detail") or payload.get("message") or "Worker 拒绝受控关闭")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096).decode("utf-8", errors="replace")
            parsed = json.loads(body)
            detail = str(parsed.get("detail") or parsed.get("message") or body)
        except Exception:  # noqa: BLE001
            detail = f"HTTP {exc.code}"
        return "rejected", detail
    except Exception as exc:  # noqa: BLE001 - 网络中断、超时或响应截断均属传输不确定
        return "transport_error", str(exc)


def request_worker_shutdown(port: int, token: str, *, transaction_id: str,
                            target_version: str) -> bool:
    outcome, _detail = request_worker_shutdown_result(
        port, token, transaction_id=transaction_id, target_version=target_version)
    return outcome == "accepted"


def request_release_business_gate(port: int, token: str, *, transaction_id: str,
                                  target_version: str) -> bool:
    """仅在事务 completed/rolled_back 后放行真实 Worker 的后台业务与同步服务。"""
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api/internal/release-business-gate",
        data=json.dumps({"transaction_id": transaction_id,
                         "target_version": target_version}).encode("utf-8"),
        method="POST",
        headers={"X-Coordinator-Token": token, "Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read()).get("ok") is True
    except Exception:
        return False


def request_recovery_shutdown(port: int, token: str, *, transaction_id: str,
                             target_version: str) -> bool:
    """请求隔离验证副本中的 Worker 受控退出；拒绝或通信失败均返回 False。"""
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api/internal/recovery-shutdown",
        data=json.dumps({"transaction_id": transaction_id,
                         "target_version": target_version}).encode("utf-8"),
        method="POST",
        headers={"X-Coordinator-Token": token, "Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read()).get("ok") is True
    except Exception:
        return False


def request_launcher_stop(port: int, token: str, *, nonce: str) -> bool:
    """受控停止 Worker：绑定协调令牌与每次启动随机 nonce，不接受浏览器调用。"""
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api/internal/launcher-stop",
        data=json.dumps({"nonce": nonce}).encode("utf-8"),
        method="POST",
        headers={"X-Coordinator-Token": token, "Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read()).get("ok") is True
    except Exception:
        return False


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, int(port))) == 0
