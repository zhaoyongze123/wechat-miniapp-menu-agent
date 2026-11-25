"""实时合格成品目录：把 Hub 当前有效的合格门店截图同步到宿主机可见目录。

业务状态（``effective_capture_tasks`` + ``store_state``）是唯一事实源，交付目录只是派生视图：

- 门店身份沿用现有模型：批次 ID + 门店 ID；缺少 store_id 时使用同一批次内的门店名，
  绝不按门店名跨批次合并。目录名只是展示名称，不是业务主键；
- 每家门店整轮目录替换，绝不合并多轮、追加文件或用历史截图补齐当前缺失；
- 每张成品必须由当前有效轮次的源文件逐字节复制，并校验两者 SHA-256 一致；
- 只读取当前有效结果目录中的 ``page-*.png``，从不修改原始结果；
- 用户删改交付文件不会回写业务状态，周期核对用内容摘要发现差异后重新同步；
- 历史 ``output/``、``legacy-sources/`` 目录必须先通过权威业务记录验证资格；
  未关联或无法确认的目录只进入待核验清单，不直接纳入合格目录。

实现取舍：
- 替换采用 staging → 旧目录移出 → 新目录就位，替换前再次核对当前有效任务与代次，
  防止过期同步任务发布旧轮次。
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
# 每轮只抽检一小批已同步门店，但会逐文件核对内容摘要，避免只比大小漏掉同尺寸篡改。
VERIFY_BATCH = 8
MAX_RETRY_SECONDS = 60
# 首次初始化可能涉及上千家、几十 GB；按时间片推进，保持状态接口与其他业务可用。
INIT_TIME_BUDGET_SECONDS = 8.0
LEGACY_SCAN_DEPTH = 3
# 待核验清单只回传有限条目，避免状态接口过大；总数仍完整统计。
PENDING_ITEM_LIMIT = 50


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


class StaleResult(Exception):
    """同步期间业务轮次已变化：放弃发布，交给下一轮重新同步。"""


def _copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    """边复制边计算源文件摘要；返回值同时作为成品摘要，失败时不留下半成品。"""
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    shutil.copystat(source, destination)
    return size, digest.hexdigest()


def _inventory_view(raw: object) -> dict:
    """统一初始化核对结构，首次扫描完成前前端也能安全读取。"""
    data = raw if isinstance(raw, dict) else {}
    discovered = data.get("discovered") if isinstance(data.get("discovered"), dict) else {}
    excluded = data.get("excluded") if isinstance(data.get("excluded"), dict) else {}
    pending = data.get("pending_verification") if isinstance(data.get("pending_verification"), dict) else {}
    return {
        "discovered": {
            key: int(discovered.get(key) or 0)
            for key in ("shared_results", "output", "legacy_sources", "total")
        },
        "included": int(data.get("included") or 0),
        "legacy_included": int(data.get("legacy_included") or 0),
        "excluded": {
            "count": int(excluded.get("count") or 0),
            "reasons": [dict(item) for item in excluded.get("reasons") or [] if isinstance(item, dict)],
        },
        "pending_verification": {
            "count": int(pending.get("count") or 0),
            "stores": int(pending.get("stores") or 0),
            "items": [dict(item) for item in pending.get("items") or [] if isinstance(item, dict)],
        },
        "verified": data.get("verified") if isinstance(data.get("verified"), dict) else {},
    }


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
        # 发布前复核用的短缓存：避免为每家门店重复查询业务任务，同时保证发现的延迟足够小。
        self._fresh_chosen: dict[str, dict] = {}
        self._fresh_at = 0.0
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
            inventory = _inventory_view(snapshot.get("inventory"))
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
            except StaleResult:
                # 业务轮次在同步期间变化：放弃发布，立即唤醒下一轮重新计算。
                self._mark_stale(key)
                self._wake.set()
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
        """沿用现有门店模型：批次 ID + 门店 ID；无 store_id 时用同批次内门店名。"""
        batch = str(task.get("batch_id") or "").strip()
        store_id = str(task.get("store_id") or "").strip()
        if store_id:
            return f"batch:{batch}|store:{store_id}"
        return f"batch:{batch}|name:{str(task.get('source_name') or '').strip()}"

    @staticmethod
    def _newer(left: dict, right: dict) -> bool:
        """只依据 Hub 业务版本与任务更新时间判断新轮次，绝不看图片文件时间。"""
        left_key = (_digits(left.get("batch_version")), _digits(left.get("material_version")),
                    str(left.get("updated_at") or ""))
        right_key = (_digits(right.get("batch_version")), _digits(right.get("material_version")),
                     str(right.get("updated_at") or ""))
        return left_key >= right_key

    def _collect_tasks(self) -> tuple[dict[str, dict], set[str]]:
        """按批次+门店选当前有效轮次；返回选中任务与非合格门店名（用于历史目录排除）。"""
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
        return chosen, blocked_names

    def _collect_records(self) -> tuple[list[dict], dict]:
        chosen, blocked_names = self._collect_tasks()

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

        # 历史目录只做资格核实：权威记录已覆盖的跳过，其余进入待核验清单，绝不凭文件系统直接纳入。
        pending: list[dict] = []
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
                reason = "存在多轮历史目录，无法确认当前有效轮次" if len(group) > 1 else "缺少权威业务记录，待核验"
                for folder, files in group:
                    pending.append({
                        "name": name,
                        "directory": folder.name,
                        "source": source,
                        "images": len(files),
                        "reason": reason,
                    })

        inventory = {
            "discovered": {**discovered, "total": sum(discovered.values())},
            # 交付目录只包含通过权威记录验证的当前有效结果，历史目录不再直接纳入。
            "included": len(records),
            "legacy_included": 0,
            "excluded": {
                "count": sum(excluded.values()),
                "reasons": [{"reason": reason, "count": count} for reason, count in excluded.items()],
            },
            "pending_verification": {
                "count": len(pending),
                "stores": len({item["name"] for item in pending}),
                "items": pending[:PENDING_ITEM_LIMIT],
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
        if not entry.get("digests"):
            # 旧索引缺少内容摘要，无法证明成品与源一致，自动重建一次。
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
            # 整轮覆盖必须逐字节复制当前有效轮次的源文件；禁止按大小相同复用旧截图。
            sizes: dict[str, int] = {}
            digests: dict[str, str] = {}
            for source in record["files"]:
                size, sha = _copy_and_hash(source, staging / source.name)
                sizes[source.name] = size
                digests[source.name] = sha
            self._assert_staged(staging, digests)
            ordered = sorted(digests)
            digest = hashlib.sha256()
            for name in ordered:
                digest.update(f"{name}\0{digests[name]}\n".encode())
            # 发布前复核当前有效任务与代次，防止同步期间业务状态变化导致过期发布。
            if not self._still_current(record):
                raise StaleResult(record["key"])
            recycle = None
            if old is not None:
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
                "files": ordered,
                "sizes": [sizes[name] for name in ordered],
                "digests": digests,
                "bytes": sum(sizes.values()),
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

    def _still_current(self, record: dict) -> bool:
        """发布前复核：对应门店仍是同一批次/门店的当前有效轮次，且业务状态仍合格。"""
        now = time.monotonic()
        if not self._fresh_at or now - self._fresh_at > 2.0:
            self._fresh_chosen, _ = self._collect_tasks()
            self._fresh_at = now
        current = self._fresh_chosen.get(record["key"])
        return bool(
            current
            and str(current.get("id") or "") == record["task_id"]
            and str(current.get("output_dir") or "") == record["output_dir"]
            and str(current.get("updated_at") or "") == record["task_updated_at"]
        )

    def _mark_stale(self, key: str) -> None:
        """标记为待重同步而不是失败，下一轮会按最新有效轮次重建。"""
        with self._lock:
            entry = self._index["stores"].get(key)
            if entry:
                entry["status"] = "pending"
                entry["error"] = "业务轮次已变化，等待重新同步"
            self._fresh_at = 0.0
            _atomic_json(self.index_path, self._index)

    @staticmethod
    def _assert_staged(staging: Path, digests: dict[str, str]) -> None:
        actual = {path.name for path in staging.iterdir() if path.is_file()}
        if actual != set(digests):
            raise ValueError("暂存文件与当前截图清单不一致")
        for name, expected in digests.items():
            if _file_sha256(staging / name) != expected:
                raise ValueError(f"暂存文件内容校验失败：{name}")

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
        """每轮抽检一小批已同步目录，逐文件比对内容摘要：成品被同尺寸篡改或源已变化都能发现。"""
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
            expected = dict(entry.get("digests") or {})
            actual: dict[str, str] = {}
            if target.is_dir():
                actual = {path.name: _file_sha256(path) for path in target.iterdir()
                          if path.is_file() and PAGE_PATTERN.match(path.name)}
            mismatch = actual != expected
            if not mismatch:
                # 同时核对源文件摘要，防止源内容在相同 updated_at 下被替换而未被发现。
                for source in record["files"]:
                    if not source.is_file() or _file_sha256(source) != expected.get(source.name):
                        mismatch = True
                        break
            if not mismatch:
                continue
            # 文件侧被删改或源已变化：移出索引后按当前有效轮次重新复制，不回写业务状态。
            with self._lock:
                self._index["stores"].pop(record["key"], None)
            try:
                self._sync_store(record)
            except StaleResult:
                self._mark_stale(record["key"])
                self._wake.set()
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
