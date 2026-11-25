"""实时合格成品目录：把 Hub 当前有效的合格门店截图同步到宿主机可见目录。

业务状态（``effective_capture_tasks`` + ``store_state``）是唯一事实源，交付目录只是派生视图：

- 同一门店（优先 store_id，其次门店名）跨批次只保留一条最新有效轮次；同名不同 store_id 独立；
- 每家门店整轮目录替换，绝不合并多轮、追加文件或用历史截图补齐当前缺失；
- 只读取当前有效结果目录中的 ``page-*.png``，从不修改原始结果；
- 用户删改交付文件不会回写业务状态，周期核对发现差异后重新同步；
- 首次部署把 ``shared-results/``、旧版 ``output/``、``legacy-sources/`` 中可核实的
  历史截图统一纳入初始化；无任务可关联且存在多轮歧义的目录只报告、不猜测。

实现取舍：
- 首次与变更门店按门店增量复制；同门店未变化文件在交付文件系统内用硬链接复用，
  既不重复占用空间，也不与原始结果共享 inode（原始图与交付图始终隔离）。
- 替换采用 staging → 旧目录移出 → 新目录就位，失败时恢复旧目录，避免新旧混杂。
- 初始化按时间片分批推进，索引逐店落盘，进程中断后可继续。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from menu_capture.capture.capture_results import store_state

# 只认当前采集契约里的菜单截图，证据图、JSON、日志与临时文件不得进入交付目录。
PAGE_PATTERN = re.compile(r"^page-\d+\.png$", re.IGNORECASE)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
DEFAULT_POLL_SECONDS = 5.0
# 每轮只抽检一部分已同步门店，避免每几秒扫描几十 GB 文件。
VERIFY_BATCH = 64
MAX_RETRY_SECONDS = 60
# 首次初始化可能涉及上千家、几十 GB；按时间片推进，保持状态接口与其他业务可用。
INIT_TIME_BUDGET_SECONDS = 8.0
LEGACY_SCAN_DEPTH = 3


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _page_number(path: Path) -> int:
    match = re.search(r"(\d+)", path.stem)
    return int(match.group(1)) if match else 0


def _digits(value) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 0


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_png(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(8) == PNG_MAGIC
    except OSError:
        return False


def safe_directory_name(raw: str) -> str:
    """生成可读且 Windows 合法的门店目录名；非法字符替换为下划线。"""
    text = unicodedata.normalize("NFC", str(raw or "")).strip()
    text = INVALID_NAME.sub("_", text)
    text = re.sub(r"\s+", " ", text).rstrip(". ")
    # 隐藏目录会被文件管理器默认隐藏，统一去掉开头的点。
    text = text.lstrip(".")
    if not text:
        text = "未命名门店"
    if text.upper() in RESERVED_NAMES or text.split(".")[0].upper() in RESERVED_NAMES:
        text = f"_{text}"
    return text[:100].rstrip(". ")


def _normalized(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


class DeliveryService:
    """Hub 生命周期内的合格成品目录同步服务；未配置时只报告原因，不写任何文件。"""

    def __init__(
        self,
        data_root: Path,
        *,
        root: str = "",
        host_path: str = "",
        task_provider: Callable[[], list[dict]] | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.data_root = Path(data_root)
        self.home = self.data_root.parent
        self.output_root = self.home / "output"
        self.legacy_root = self.home / "legacy-sources"
        self.root = Path(root) if root else None
        self.host_path = host_path.strip()
        self.task_provider = task_provider
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.index_path = self.data_root / "delivery-index.json"
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._index = self._read_index()
        self._records: list[dict] = []
        self._names_dirty = False
        self._verify_cursor = 0
        self._snapshot = {
            "state": "initializing" if self.configured else "unconfigured", "message": "",
            "qualified": 0, "ready": 0, "pending": 0, "failed": 0, "images": 0, "bytes": 0,
            "last_sync_at": "", "generation": int(self._index.get("generation") or 0),
            "initialized": False, "initialized_at": str(self._index.get("initialized_at") or ""),
            "content_digest": str(self._index.get("content_digest") or ""),
            "inventory": dict(self._index.get("inventory") or {}),
        }

    # ---------- 配置与状态 ----------

    @property
    def configured(self) -> bool:
        return bool(self.root and self.host_path and self.root.is_absolute())

    @property
    def staging_root(self) -> Path:
        mount = self.root.parent if self.root else self.data_root
        return mount / ".menu-capture-staging"

    @property
    def trash_root(self) -> Path:
        mount = self.root.parent if self.root else self.data_root
        return mount / ".menu-capture-trash"

    def open_supported(self) -> bool:
        """只有 Hub 与交付目录同机且平台支持文件管理器时才提供一键打开。"""
        if not self.configured or not self.root or not self.root.is_dir():
            return False
        if sys.platform not in {"win32", "darwin"}:
            return False
        # 容器里的 Linux 进程无法打开宿主机资源管理器，不能给用户假按钮。
        return not Path("/.dockerenv").exists()

    def open_reason(self) -> str:
        if not self.configured:
            return "尚未配置宿主机交付目录"
        if not self.root or not self.root.is_dir():
            return "交付目录当前不可访问"
        if self.open_supported():
            return ""
        return "共享后台运行在容器中，无法直接打开宿主机目录；请复制路径后在文件管理器打开"

    def status(self) -> dict:
        with self._lock:
            snapshot = dict(self._snapshot)
            inventory = dict(snapshot.get("inventory") or {})
        configured = self.configured
        state, message = snapshot["state"], snapshot["message"]
        root_error = self._root_error() if configured else "尚未配置宿主机交付目录"
        if not configured:
            state, message = "unconfigured", "尚未配置宿主机交付目录，请由管理员完成挂载与路径设置"
        elif root_error:
            state, message = "error", root_error
        return {
            "supported": True,
            "configured": configured,
            "state": state,
            "message": message,
            "container_root": str(self.root) if self.root else "",
            "host_path": self.host_path,
            "open_supported": self.open_supported(),
            "open_reason": self.open_reason(),
            "counts": {
                "qualified": snapshot["qualified"],
                "ready": snapshot["ready"],
                "pending": snapshot["pending"],
                "failed": snapshot["failed"],
                "images": snapshot["images"],
                "bytes": snapshot["bytes"],
            },
            "inventory": inventory,
            "content_digest": snapshot["content_digest"],
            "last_sync_at": snapshot["last_sync_at"],
            "initialized_at": snapshot["initialized_at"],
            "generation": snapshot["generation"],
            "initializing": state == "initializing",
        }

    def reveal(self) -> dict:
        if not self.open_supported() or self.root is None:
            raise ValueError(self.open_reason())
        if sys.platform == "win32":
            os.startfile(self.root)  # noqa: S606 - 仅打开管理员配置的固定交付根
        else:
            import subprocess
            subprocess.run(["/usr/bin/open", str(self.root)], check=True)
        return {"ok": True}

    # ---------- 查询 ----------

    def stores(self, *, page: int = 1, size: int = 50, query: str = "", batch_id: str = "all") -> dict:
        with self._lock:
            records = list(self._records)
            batches = list(self._index.get("batch_options") or [])
        normalized = query.strip().casefold()
        rows = [
            row for row in records
            if (not normalized or normalized in (
                f'{row["store_name"]} {row["batch_name"]} {row["directory_name"]}'.casefold()))
            and (batch_id == "all" or row["batch_id"] == batch_id)
        ]
        total = len(rows)
        start = max(0, (page - 1) * size)
        return {"total": total, "page": page, "size": size, "items": rows[start:start + size],
                "batches": batches}

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.configured:
            self._clean_leftovers()
        self._thread = threading.Thread(target=self._loop, name="delivery-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def trigger(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception as error:  # noqa: BLE001 - 后台循环不能因单次异常退出
                with self._lock:
                    self._snapshot.update(state="error", message=f"同步异常：{error}")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    # ---------- 同步 ----------

    def reconcile(self) -> None:
        if not self.configured:
            with self._lock:
                self._snapshot.update(state="unconfigured", qualified=0, ready=0,
                                      pending=0, failed=0, images=0, bytes=0)
            return
        root = self.root
        assert root is not None
        root_error = self._root_error()
        if root_error:
            with self._lock:
                self._snapshot.update(state="error", message=root_error)
            return

        try:
            desired, inventory = self._collect_records()
        except Exception as error:  # noqa: BLE001 - 读取业务状态失败不能删除现有目录
            with self._lock:
                self._snapshot.update(state="error", message=f"读取合格门店失败：{error}")
            return

        with self._lock:
            initialized = self._snapshot["initialized"]
            self._snapshot.update(state="initializing" if not initialized else "syncing",
                                  message="正在初始化合格目录" if not initialized else "正在同步变更")

        deadline = time.monotonic() + INIT_TIME_BUDGET_SECONDS
        for record in desired:
            if self._stop.is_set():
                return
            key = record["key"]
            if not record["ready"]:
                # 业务上仍合格但截图缺失/未就绪：不能把旧目录继续冒充当前成品。
                self._remove_entry(key)
                continue
            if not self._needs_sync(record) or self._backoff_active(key):
                continue
            try:
                self._sync_store(record)
            except Exception as error:  # noqa: BLE001 - 单店失败不影响其他门店
                self._record_failure(key, str(error) or "同步失败")
            if time.monotonic() > deadline:
                break

        desired_keys = {record["key"] for record in desired}
        for key in list(self._index["stores"]):
            if key not in desired_keys:
                self._remove_entry(key)

        self._verify_slice(desired)
        self._finish(desired, inventory)

    # ---------- 收集当前有效结果与历史存量 ----------

    def _identity(self, task: dict) -> str:
        store_id = str(task.get("store_id") or "").strip()
        if store_id:
            return f"store:{store_id}"
        return f"name:{str(task.get('source_name') or '').strip()}"

    @staticmethod
    def _newer(left: dict, right: dict) -> bool:
        """只依据 Hub 业务版本与任务更新时间判断新轮次，绝不看图片文件时间。"""
        left_key = (_digits(left.get("batch_version")), _digits(left.get("material_version")),
                    str(left.get("updated_at") or ""))
        right_key = (_digits(right.get("batch_version")), _digits(right.get("material_version")),
                     str(right.get("updated_at") or ""))
        return left_key >= right_key

    def _collect_records(self) -> tuple[list[dict], dict]:
        tasks = list(self.task_provider() if self.task_provider is not None else [])
        blocked_names: set[str] = set()
        chosen: dict[str, dict] = {}
        for task in tasks:
            if store_state(task) != "captured":
                blocked_names.add(str(task.get("source_name") or "").strip())
                continue
            identity = self._identity(task)
            if identity not in chosen or self._newer(task, chosen[identity]):
                chosen[identity] = task

        records: list[dict] = []
        task_outputs: set[str] = set()
        discovered = {"shared_results": 0, "output": 0, "legacy_sources": 0}
        excluded: dict[str, int] = {}
        shared_seen: set[str] = set()
        names: set[str] = set()
        for identity, task in chosen.items():
            record = self._task_record(task, identity)
            records.append(record)
            names.add(record["store_name"])
            source_path = Path(record["output_dir"])
            task_outputs.add(str(source_path.resolve()))
            source = self._source_of(source_path)
            if source == "shared_results" and str(source_path.resolve()) not in shared_seen:
                shared_seen.add(str(source_path.resolve()))
                discovered["shared_results"] += 1
            elif source in discovered:
                discovered[source] += 1

        for source, base in (("output", self.output_root), ("legacy_sources", self.legacy_root)):
            groups: dict[str, list[tuple[Path, list[Path]]]] = {}
            for path, files in self._scan_legacy(base):
                discovered[source] += 1
                if not all(_valid_png(item) for item in files):
                    excluded["非有效菜单截图"] = excluded.get("非有效菜单截图", 0) + 1
                    continue
                if str(path.resolve()) in task_outputs:
                    continue
                name = path.name
                if name in names:
                    excluded["已有当前有效轮次"] = excluded.get("已有当前有效轮次", 0) + 1
                    continue
                if name in blocked_names:
                    excluded["业务状态不是合格"] = excluded.get("业务状态不是合格", 0) + 1
                    continue
                groups.setdefault(name, []).append((path, files))
            for name, group in groups.items():
                if len(group) > 1:
                    excluded["存在多轮历史目录，无法确认当前有效轮次"] = (
                        excluded.get("存在多轮历史目录，无法确认当前有效轮次", 0) + 1)
                    continue
                folder, files = group[0]
                records.append(self._legacy_record(folder, files, source))
                names.add(name)

        inventory = {
            "discovered": {**discovered, "total": sum(discovered.values())},
            "included": len(records),
            "legacy_included": len([record for record in records if record["legacy"]]),
            "excluded": {
                "count": sum(excluded.values()),
                "reasons": [{"reason": reason, "count": count} for reason, count in excluded.items()],
            },
        }
        return records, inventory

    def _source_of(self, path: Path) -> str:
        try:
            relative = path.resolve().relative_to(self.home.resolve())
        except (OSError, ValueError):
            return "other"
        first = relative.parts[0] if relative.parts else ""
        return {"data": "shared_results", "output": "output", "legacy-sources": "legacy_sources"}.get(first, "other")

    def _page_files(self, output: Path) -> tuple[list[Path], str]:
        if not output.is_absolute() or not _inside(output, self.home) or not output.is_dir():
            return [], "截图目录不可用或不在数据目录内"
        files = sorted(
            (path for path in output.iterdir()
             if path.is_file() and not path.is_symlink() and PAGE_PATTERN.match(path.name)),
            key=_page_number,
        )
        if not files:
            return [], "缺少菜单截图"
        return files, ""

    def _task_record(self, task: dict, identity: str) -> dict:
        files, reason = self._page_files(Path(str(task.get("output_dir") or "")))
        return {
            "key": identity,
            "task_id": str(task.get("id") or ""),
            "batch_id": str(task.get("batch_id") or ""),
            "batch_name": str(task.get("batch_name") or task.get("batch_id") or ""),
            "store_name": str(task.get("source_name") or ""),
            "store_id": str(task.get("store_id") or ""),
            "directory_name": self._allocate_name(identity, str(task.get("source_name") or ""),
                                                  str(task.get("store_id") or "")),
            "output_dir": str(task.get("output_dir") or ""),
            "task_updated_at": str(task.get("updated_at") or ""),
            "source": self._source_of(Path(str(task.get("output_dir") or ""))),
            "legacy": False,
            "files": files,
            "ready": not reason,
            "reason": reason,
        }

    def _legacy_record(self, folder: Path, files: list[Path], source: str) -> dict:
        name = folder.name
        identity = f"legacy:{source}:{name}"
        return {
            "key": identity,
            "task_id": "",
            "batch_id": f"legacy:{source}",
            "batch_name": "历史存量",
            "store_name": name,
            "store_id": "",
            "directory_name": self._allocate_name(identity, name, ""),
            "output_dir": str(folder),
            "task_updated_at": "legacy",
            "source": source,
            "legacy": True,
            "files": files,
            "ready": True,
            "reason": "",
        }

    def _scan_legacy(self, base: Path) -> list[tuple[Path, list[Path]]]:
        """扫描旧版目录：一、二级门店目录中的 page-*.png；多轮歧义留给上层报告。"""
        if not base.is_dir():
            return []
        found: list[tuple[Path, list[Path]]] = []
        base_depth = len(base.parts)
        for current, dirs, _files in os.walk(base, followlinks=False):
            path = Path(current)
            if len(path.parts) - base_depth > LEGACY_SCAN_DEPTH:
                dirs[:] = []
                continue
            dirs[:] = [name for name in dirs if not (path / name).is_symlink()]
            pages = sorted(
                (item for item in path.iterdir()
                 if item.is_file() and not item.is_symlink() and PAGE_PATTERN.match(item.name)),
                key=_page_number,
            )
            if not pages:
                continue
            found.append((path, pages))
            dirs[:] = []  # 已找到截图目录，不再深入其子目录
        return found

    def _allocate_name(self, key: str, store_name: str, store_id: str) -> str:
        base = safe_directory_name(store_name or store_id or "未命名门店")
        with self._lock:
            directories = self._index["directories"]
            existing = directories.get(key)
            if existing:
                return existing
            used = {_normalized(name) for other, name in directories.items() if other != key}
            name = base
            if _normalized(name) in used:
                suffix = safe_directory_name(store_id) if store_id else hashlib.sha1(key.encode()).hexdigest()[:6]
                name = safe_directory_name(f"{base}-{suffix}")[:100]
                index = 2
                while _normalized(name) in used:
                    name = safe_directory_name(f"{base}-{suffix}-{index}")[:100]
                    index += 1
            directories[key] = name
            self._names_dirty = True
            return name

    def _needs_sync(self, record: dict) -> bool:
        with self._lock:
            entry = self._index["stores"].get(record["key"])
        if not entry:
            return True
        if entry.get("status") != "ready":
            return True
        if entry.get("source_updated_at") != record["task_updated_at"]:
            return True
        return entry.get("output_dir") != record["output_dir"]

    def _backoff_active(self, key: str) -> bool:
        with self._lock:
            failure = self._index["failures"].get(key)
        return bool(failure and time.time() < float(failure.get("retry_at") or 0))

    def _target(self, directory: str) -> Path:
        assert self.root is not None
        return self.root / directory

    def _sync_store(self, record: dict) -> None:
        root = self.root
        assert root is not None
        target = self._target(record["directory_name"])
        hashed = hashlib.sha1(record["key"].encode()).hexdigest()[:16]
        staging = self.staging_root / hashed
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        old = target if target.is_dir() else None
        try:
            for source in record["files"]:
                destination = staging / source.name
                # 交付目录内部用硬链接复用未变化文件；绝不链接原始结果，保持写隔离。
                reused = False
                if old is not None:
                    previous = old / source.name
                    try:
                        if previous.is_file() and previous.stat().st_size == source.stat().st_size:
                            os.link(previous, destination)
                            reused = True
                    except OSError:
                        reused = False
                if not reused:
                    shutil.copy2(source, destination)
            self._assert_staged(staging, record["files"])
            digest = hashlib.sha256()
            sizes: list[int] = []
            for name in sorted(path.name for path in record["files"]):
                file = staging / name
                size = file.stat().st_size
                sizes.append(size)
                digest.update(f"{name}\0{size}\0{_file_sha256(file)}\n".encode())
            recycle = None
            if target.exists():
                self.trash_root.mkdir(parents=True, exist_ok=True)
                recycle = self.trash_root / f"{hashed}-{int(time.time() * 1000)}"
                os.replace(target, recycle)
            try:
                os.replace(staging, target)
            except OSError:
                if recycle is not None:
                    os.replace(recycle, target)
                raise
            if recycle is not None:
                shutil.rmtree(recycle, ignore_errors=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        with self._lock:
            self._index["stores"][record["key"]] = {
                "directory": record["directory_name"],
                "store_name": record["store_name"],
                "batch_id": record["batch_id"],
                "batch_name": record["batch_name"],
                "task_id": record["task_id"],
                "source": record["source"],
                "legacy": record["legacy"],
                "source_updated_at": record["task_updated_at"],
                "output_dir": record["output_dir"],
                "files": [path.name for path in record["files"]],
                "sizes": sizes,
                "bytes": sum(sizes),
                "content_digest": digest.hexdigest(),
                "status": "ready",
                "error": "",
                "synced_at": _stamp(),
            }
            self._index["failures"].pop(record["key"], None)
            self._index["generation"] = int(self._index.get("generation") or 0) + 1
            self._snapshot["generation"] = self._index["generation"]
            self._snapshot["last_sync_at"] = _stamp()
            _atomic_json(self.index_path, self._index)

    @staticmethod
    def _assert_staged(staging: Path, files: list[Path]) -> None:
        expected = {path.name: path.stat().st_size for path in files}
        actual = {path.name: path.stat().st_size for path in staging.iterdir() if path.is_file()}
        if actual != expected:
            raise ValueError("暂存文件与当前截图清单不一致")

    def _remove_entry(self, key: str) -> None:
        with self._lock:
            entry = self._index["stores"].get(key)
        if not entry or not entry.get("directory"):
            return
        target = self._target(str(entry.get("directory")))
        try:
            if target.is_dir():
                self.trash_root.mkdir(parents=True, exist_ok=True)
                recycle = self.trash_root / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}-{int(time.time() * 1000)}"
                os.replace(target, recycle)
                shutil.rmtree(recycle, ignore_errors=True)
        except OSError as error:
            self._record_failure(key, str(error) or "移出失败")
            return
        with self._lock:
            self._index["stores"].pop(key, None)
            self._index["failures"].pop(key, None)
            self._index["generation"] = int(self._index.get("generation") or 0) + 1
            self._snapshot["generation"] = self._index["generation"]
            self._snapshot["last_sync_at"] = _stamp()
            _atomic_json(self.index_path, self._index)

    def _record_failure(self, key: str, error: str) -> None:
        with self._lock:
            failure = dict(self._index["failures"].get(key) or {"count": 0})
            failure["count"] = int(failure.get("count") or 0) + 1
            failure["error"] = error
            failure["retry_at"] = time.time() + min(MAX_RETRY_SECONDS, 2 ** min(failure["count"], 6))
            self._index["failures"][key] = failure
            entry = self._index["stores"].get(key)
            if entry:
                entry["status"] = "failed"
                entry["error"] = error
            _atomic_json(self.index_path, self._index)

    def _verify_slice(self, desired: list[dict]) -> None:
        """每轮抽检一小批已同步目录；手工删改会在有限轮次内被发现并修复。"""
        ready = [record for record in desired if record["ready"]
                 and self._index["stores"].get(record["key"], {}).get("status") == "ready"]
        if not ready:
            return
        start = self._verify_cursor % len(ready)
        self._verify_cursor = (start + VERIFY_BATCH) % max(1, len(ready))
        for record in ready[start:start + VERIFY_BATCH]:
            entry = self._index["stores"].get(record["key"])
            if not entry:
                continue
            target = self._target(record["directory_name"])
            expected = {name: size for name, size in zip(entry.get("files", []), entry.get("sizes", []))}
            actual = {}
            if target.is_dir():
                actual = {path.name: path.stat().st_size for path in target.iterdir()
                          if path.is_file() and PAGE_PATTERN.match(path.name)}
            if actual != expected:
                # 文件侧被删改：移出索引后重新复制，不回写任何业务状态。
                with self._lock:
                    self._index["stores"].pop(record["key"], None)
                try:
                    self._sync_store(record)
                except Exception as error:  # noqa: BLE001
                    self._record_failure(record["key"], str(error) or "修复失败")

    def _finish(self, desired: list[dict], inventory: dict) -> None:
        with self._lock:
            stores = self._index["stores"]
            failures = self._index["failures"]
            ready_keys = [record["key"] for record in desired
                          if stores.get(record["key"], {}).get("status") == "ready"]
            images = sum(len(stores[key].get("files", [])) for key in ready_keys)
            size = sum(int(stores[key].get("bytes") or 0) for key in ready_keys)
            failed = len([record for record in desired if record["key"] in failures])
            pending = len([record for record in desired
                           if record["key"] not in ready_keys and record["key"] not in failures])
            state = "synced"
            message = "全部成品已同步"
            if failed:
                state, message = "partial", f"{failed} 家门店同步失败，目录可能不完整"
            elif pending:
                state, message = "syncing", f"{pending} 家门店待同步"
            elif not self._snapshot["initialized"]:
                message = "初始化完成"
            initialized = bool(self._snapshot["initialized"]) or (not pending and not failed)
            initialized_at = self._snapshot["initialized_at"]
            if initialized and not initialized_at:
                initialized_at = _stamp()
            digest = hashlib.sha256()
            for key in sorted(ready_keys):
                digest.update(f"{key}\0{stores[key].get('content_digest', '')}\n".encode())
            content_digest = digest.hexdigest() if ready_keys else ""
            self._snapshot.update(
                state=state, message=message, qualified=len(desired), ready=len(ready_keys),
                pending=pending, failed=failed, images=images, bytes=size, initialized=initialized,
                initialized_at=initialized_at, content_digest=content_digest,
                inventory={**inventory, "verified": {
                    "stores": len(ready_keys), "images": images, "bytes": size,
                    "content_digest": content_digest}},
            )
            self._records = [{
                **{key: record[key] for key in (
                    "task_id", "batch_id", "batch_name", "store_name", "directory_name",
                    "ready", "reason", "source", "legacy")},
                "status": stores.get(record["key"], {}).get("status", "pending"),
                "error": stores.get(record["key"], {}).get("error", ""),
                "images": len(stores.get(record["key"], {}).get("files", [])),
                "bytes": int(stores.get(record["key"], {}).get("bytes") or 0),
                "synced_at": stores.get(record["key"], {}).get("synced_at", ""),
            } for record in desired]
            batch_names = sorted({(record["batch_id"], record["batch_name"]) for record in desired})
            self._index["inventory"] = self._snapshot["inventory"]
            self._index["initialized_at"] = initialized_at
            self._index["content_digest"] = content_digest
            if self._names_dirty:
                self._names_dirty = False
                _atomic_json(self.index_path, self._index)
            self._index["batch_options"] = [{"batch_id": batch_id, "name": name}
                                            for batch_id, name in batch_names]
            _atomic_json(self.index_path, self._index)

    def _root_error(self) -> str:
        """校验交付根；宿主机 bind mount 存在时自动创建一级“合格”目录。"""
        if not self.root:
            return "尚未配置宿主机交付目录"
        if not self.root.parent.is_dir():
            return "交付目录所在挂载不存在或已失效，已暂停写入"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return f"无法创建交付目录：{error}"
        if not os.access(self.root, os.W_OK):
            return "交付目录不可写，请检查宿主机权限"
        return ""

    def _clean_leftovers(self) -> None:
        for folder in (self.staging_root, self.trash_root):
            try:
                if folder.is_dir():
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass

    def _read_index(self) -> dict:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return {
            "version": 2,
            "generation": int(data.get("generation") or 0),
            "directories": dict(data.get("directories") or {}),
            "stores": dict(data.get("stores") or {}),
            "failures": dict(data.get("failures") or {}),
            "inventory": dict(data.get("inventory") or {}),
            "initialized_at": str(data.get("initialized_at") or ""),
            "content_digest": str(data.get("content_digest") or ""),
            "batch_options": list(data.get("batch_options") or []),
        }
