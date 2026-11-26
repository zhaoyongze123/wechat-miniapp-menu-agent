"""二维码素材清洗、人工决策和不可覆盖的输入快照管理。

关键流程：创建批次 → 分析 job → 人工确认/跳过 → 发布快照 → 激活给采集。
边界：发布快照不可变；二维码只保存载荷哈希，不保留原文；批次阶段状态以本模块为准。
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from PIL import Image

from menu_capture.core.config import ConfigStore, atomic_json
from menu_capture.materials.preprocess import (
    CROP_MARGIN_RATIO, SUPPORTED_EXTENSIONS,
    _apple_vision_inspector, _apply_orientation, _crop_candidate, _enumerate_images,
    _inspect_image, analyze_store, discover_stores,
)
from menu_capture.desktop.image_tools import image_ocr
from menu_capture.materials.material_jobs import JobRunner, JobStore, OPEN_STATUSES
from menu_capture.materials.material_versions import MaterialVersions
from menu_capture.materials.store_material_versions import StoreMaterialVersions
from menu_capture.materials.util import (
    _STRONG_ORDERING_TERMS, _WEAK_ORDERING_TERMS,
    _assigned_ocr_texts, _atomic_crop, _classify_local_purpose, _inside, _mkdir_ok,
    _now, _selected_margin_ratio, _sha256, _store_id,
)
from menu_capture.storage.store import TaskStore
from menu_capture.projects.service import (
    LEGACY_ID, ProjectConflict, assert_batch_scope, assert_task_scope, assert_writable, ensure_legacy_project,
    get_project, migrate_projects, resolve_batch_project, select_version,
)

logger = logging.getLogger(__name__)

from menu_capture.capture.capture_results import waiting_for_materials

REJECTION_REASONS = {
    "blur": "图片模糊 / 无法识别",
    "invalid_qr": "二维码无效",
    "store_mismatch": "二维码门店不一致",
    "store_closed": "门店已关店 / 停业",
    "miniapp_unavailable": "小程序到期或未绑定",
    "scan_order_unsupported": "门店不支持扫码点餐",
    "not_ordering": "非点餐码",
    "qr_external_web": "二维码指向外部网页（非小程序）",
    "menu_unavailable": "门店未配置菜单 / 无可用菜品",
    "phone_only": "需在手机上打开（桌面无法采集）",
    "authorization_unavailable": "需要验证码/扫码授权（自动化无法进入）",
    "network_unavailable": "网络异常，重试后仍无法进入",
    "duplicate": "重复素材",
    "other": "其他原因",
}

MAX_FILES = 10_000
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BATCH_BYTES = 5 * 1024 * 1024 * 1024
MAX_STORE_UPLOAD_FILES = 100
MAX_STORE_UPLOAD_BYTES = 500 * 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FORMAT_BY_EXTENSION = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}


# OCR 关键词分三级：明确点餐 > 明确非点餐 > 泛化的“下单”，避免团购文案被当成点餐。
_ORDERING_TERMS = (*_STRONG_ORDERING_TERMS, *_WEAK_ORDERING_TERMS)
_LOCAL_OCR_REASON_LABELS = {
    *(f"周边 OCR 识别到“{term}”" for term in _ORDERING_TERMS),
    *(f"周边 OCR 识别到“{term}”；二维码未能由本地解码器读出，需人工确认" for term in _ORDERING_TERMS),
    "周边 OCR 识别到非点餐用途标识",
    "二维码周边没有识别到明确点餐标识，需人工确认",
    "本地 OCR 未能完成，需人工确认",
}
# URL 判定只保存受控标签，不把二维码原文写入清单。
_PAYLOAD_URL_REASON_LABELS = {
    "二维码链接命中点餐关键词",
    "二维码链接命中非点餐关键词",
    "二维码链接同时命中点餐与非点餐证据，需人工确认",
}
# 餐饮域名只作候选，必须与周边 OCR 同时命中点餐才落这个标签。
_PAYLOAD_URL_OCR_REASON_LABELS = {
    "餐饮点餐域名且周边 OCR 识别到点餐标识",
}
# 候选质量门禁只保存受控原因，便于人工核对时区分结构误检、偏移和松框。
_QUALITY_REASON_LABELS = {
    "候选框未通过二维码结构校验",
    "二维码超出候选框",
    "二维码面积占候选框过低",
    "二维码明显偏离候选框中心",
}
# 用途枚举：not_ordering/conflict 是本轮新增；历史数据里的 other 在清洗时映射为 not_ordering。
_PURPOSES = {"ordering", "not_ordering", "enterprise_wechat", "unknown", "conflict"}
_LEGACY_PURPOSES = {"other": "not_ordering"}
_RECOMMENDATION_LEVELS = {"high", "ocr", "low", "none"}
_WECHAT_VALIDATION_STATUSES = {"unverified", "not_applicable"}


class MaterialService:
    """素材分析与确认；二维码用途由本地 OCR 判断，不调用视觉模型。"""

    def __init__(
        self,
        root: Path,
        configs: ConfigStore,
        task_store: TaskStore,
        inspector: Callable[[Path], list[dict]] | None = None,
        control_lock=None,
        ocr_reader: Callable[[Path], list[dict]] | None = None,
        role: str = "standalone",
    ):
        self.root = Path(root).expanduser().resolve()
        self.configs, self.task_store = configs, task_store
        self.role = role
        self.inspector = inspector
        self.ocr_reader = ocr_reader or image_ocr
        self.control_lock = control_lock  # 发布并发/runner idle 由 API 外层负责。
        self.material_root = self.root / "materials"
        self.batch_root = self.material_root / "batches"
        self.upload_root = self.material_root / "uploads"
        self.published_root = self.material_root / "published"
        for directory in (self.batch_root, self.upload_root, self.published_root):
            _mkdir_ok(directory)
            directory.chmod(0o700)
        self._lock = threading.RLock()
        # 批次管理（删除/恢复/清空选择）与上传写入的互斥锁；分析运行态已归 job，不再由它承载 worker 单例。
        self._worker_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        # OCR 引擎的线程安全性未知；只对 OCR 调用加锁，二维码检测仍可并发。
        self._ocr_lock = threading.Lock()
        self._ocr_local = threading.local()
        self._init_schema()
        self.jobs = JobStore(self.task_store)
        self.job_runner = JobRunner(
            self.jobs, self._run_job_item,
            prepare=self._prepare_job, on_finish=self._job_finished,
        )
        self.versions = MaterialVersions(self.task_store, self.published_root, role=role)
        self.store_versions = StoreMaterialVersions(self.task_store, self.published_root)
        # 迁移与入口启用同事务完成。不能确认历史归属时让启动失败，避免旧全局写路径继续混入新数据。
        with self.task_store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            migrate_projects(db)
            if role in {"standalone", "worker"}:
                ensure_legacy_project(db)
        # 旧数据只做一次状态收敛：运行态按“是否所有门店都有决策”判定，不再无条件判死。
        self._migrate_legacy_batches()
        # 服务接收请求前只恢复一次清理事务，避免并发首请求重复处理同一暂存目录。
        from menu_capture.capture.batch_management import BatchManager
        self._batch_management = BatchManager(self)

    def _analysis_concurrency(self) -> int:
        return max(1, min(4, int(self.configs.load().materials.analysis_concurrency)))

    def start_job_runner(self) -> None:
        self.job_runner.start()

    def shutdown(self) -> None:
        self.job_runner.stop()

    def _migrate_legacy_batches(self) -> None:
        """收敛历史批次状态与进度口径；新代码只使用 uploading/ready_to_analyze/completed。"""
        with self.task_store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT id,source_root FROM material_batches "
                "WHERE status IN ('queued','analyzing','interrupted','failed','error')"
            )]
        for row in rows:
            try:
                stores, _duplicates = discover_stores(Path(row["source_root"]))
                total = len(stores)
            except Exception:
                total = 0
            with self.task_store.connect() as db:
                done = int(db.execute(
                    "SELECT COUNT(*) FROM material_decisions WHERE batch_id=?", (row["id"],)
                ).fetchone()[0])
                # 只有所有门店都有决策才算 completed；否则回到可分析状态，避免“完成了一半”。
                status = "completed" if total > 0 and done >= total else "ready_to_analyze"
                db.execute(
                    "UPDATE material_batches SET status=?,progress_done=?,progress_total=?,error='',updated_at=? WHERE id=?",
                    (status, done if status == "completed" else 0, total, _now(), row["id"]),
                )
        # 历史进度按“图片 × 单元”写入，这里统一重算为门店数，避免 5030/1122 两种口径并存。
        with self.task_store.connect() as db:
            db.execute(
                "UPDATE material_batches SET progress_total=("
                "SELECT COUNT(*) FROM material_decisions WHERE batch_id=material_batches.id"
                ") WHERE progress_total<>("
                "SELECT COUNT(*) FROM material_decisions WHERE batch_id=material_batches.id"
                ") AND status='completed'"
            )
            db.execute(
                "UPDATE material_batches SET progress_done=progress_total "
                "WHERE status='completed' AND progress_done<>progress_total"
            )

    def _init_schema(self) -> None:
        with self.task_store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS material_releases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL, published_at TEXT NOT NULL, store_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS material_batches (
                    id TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL,
                    source_root TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress_done INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    upload_file_count INTEGER NOT NULL DEFAULT 0,
                    upload_bytes INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    snapshot_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    deleted_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS material_decisions (
                    batch_id TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    source_folder TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    analysis_status TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'pending',
                    recommendation INTEGER NOT NULL DEFAULT 0,
                    selected_json TEXT NOT NULL DEFAULT '{}',
                    selected_source_hash TEXT NOT NULL DEFAULT '',
                    store_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, store_id),
                    FOREIGN KEY(batch_id) REFERENCES material_batches(id) ON DELETE CASCADE
                );
            """)

        with self.task_store.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(material_batches)")}
            for name in ("description", "deleted_at", "approved_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE material_batches ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            # 手工指定的执行阶段优先于自动推断；空字符串表示恢复“按运行轮次自动判断”。
            if "manual_stage" not in columns:
                db.execute("ALTER TABLE material_batches ADD COLUMN manual_stage TEXT NOT NULL DEFAULT ''")
            db.execute(
                "UPDATE material_batches SET manual_stage='approved' "
                "WHERE approved_at<>'' AND manual_stage=''")
            # 采集轮次只在实际启动采集时累加，用来区分“首轮 / 已重跑”，不参与任何业务判定。
            if "capture_rounds" not in columns:
                db.execute("ALTER TABLE material_batches ADD COLUMN capture_rounds INTEGER NOT NULL DEFAULT 0")
            # 历史批次没有轮次记录：按已发生过的运行补一次，避免老批次全部显示“未采集”。
            db.execute(
                "UPDATE material_batches SET capture_rounds=("
                "SELECT COUNT(*) FROM runs r WHERE r.material_batch_id=material_batches.id) "
                "WHERE capture_rounds=0 AND EXISTS("
                "SELECT 1 FROM runs r2 WHERE r2.material_batch_id=material_batches.id)")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(material_decisions)")}
            for name in ("reject_reason", "reject_detail", "rejected_at", "reject_reviewed_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE material_decisions ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            # 管理列表按批次查询引用运行；索引避免每个批次都扫描完整 runs 表。
            db.execute("CREATE INDEX IF NOT EXISTS runs_material_batch ON runs(material_batch_id)")

    def _read_ocr(self, path: Path) -> list[dict] | None:
        """按线程缓存本地 OCR 结果，避免方形判定与用途分类重复调用。"""
        key = Path(path)
        cache = getattr(self._ocr_local, "cache", None)
        if cache is None:
            cache = self._ocr_local.cache = {}
        if key not in cache:
            try:
                # 注入的 OCR 引擎线程安全性未知，用独立锁串行化调用。
                with self._ocr_lock:
                    items = self.ocr_reader(key)
                cache[key] = items if isinstance(items, list) else None
            except Exception:
                cache[key] = None
        return cache[key]

    @staticmethod
    def rejection(row: dict) -> dict | None:
        code = row.get("reject_reason", "")
        if row["decision"] != "skipped" or not code:
            return None
        return {"code": code, "label": REJECTION_REASONS[code], "detail": row.get("reject_detail", ""),
                "marked_at": row.get("rejected_at", ""), "reviewed_at": row.get("reject_reviewed_at", "")}

    def create_local_batch(self, input_root: Path, *, name: str | None = None, description: str = "",
                           project_id: str | None = None) -> dict:
        name, description = self._batch_metadata(name, description)
        source = self._validate_local_root(Path(input_root))
        images = _enumerate_images(source)
        self._validate_local_limits(images)
        batch_id = uuid.uuid4().hex
        return self._insert_batch(batch_id, "local", source, "ready_to_analyze", total=len(images),
                                  name=name, description=description, project_id=project_id)

    def begin_upload(self, *, name: str | None = None, description: str = "",
                     project_id: str | None = None) -> dict:
        name, description = self._batch_metadata(name, description)
        batch_id = uuid.uuid4().hex
        source = self.upload_root / batch_id / "source"
        source.mkdir(parents=True, mode=0o700, exist_ok=False)
        try:
            with self.task_store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                stamp = _now()
                project_id = self.project_for_new_batch(db, project_id)
                db.execute(
                    "INSERT INTO material_batches(id,source_kind,source_root,status,created_at,updated_at,name,description,project_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (batch_id, "upload", str(source), "uploading", stamp, stamp, name, description, project_id),
                )
        except Exception:
            # Only remove the newly created empty directory, never a source tree.
            source.rmdir()
            source.parent.rmdir()
            raise
        return {"batch_id": batch_id, "status": "uploading", "file_count": 0, "total_bytes": 0}

    def add_upload_file(self, batch_id: str, relative_path: str, content: bytes, *, project_id: str | None = None) -> dict:
        self._check_batch_scope(batch_id, project_id, write=True)
        rel = self._safe_upload_path(relative_path)
        if not isinstance(content, bytes) or not content:
            raise ValueError("上传文件必须是非空原始 bytes")
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("单文件超过 50 MB 限制")
        self._validate_uploaded_image(rel, content)
        with self._lock:
            row = self._batch_row(batch_id)
            if row["status"] != "uploading":
                raise ValueError("上传批次不在 uploading 状态")
            next_count = row["upload_file_count"] + 1
            next_size = row["upload_bytes"] + len(content)
            if next_count > MAX_FILES:
                raise ValueError("单批文件数超过 10000 限制")
            if next_size > MAX_BATCH_BYTES:
                raise ValueError("单批总大小超过 5 GB 限制")
            source_root = Path(row["source_root"]).resolve(strict=True)
            target = source_root.joinpath(*rel.parts)
            _mkdir_ok(target.parent)
            if not _inside(target.parent, source_root) or any(parent.is_symlink() for parent in [target.parent, *target.parent.parents] if parent != source_root.parent):
                raise ValueError("上传路径解析后超出私有素材目录")
            if target.exists() or target.is_symlink():
                raise ValueError("上传批次内出现重复相对路径")
            # 临时文件用短名：门店目录名较长时，原名再追加 uuid 会让完整路径超过
            # Windows 260 字符上限，写入抛 OSError，整批上传中断。
            temporary = target.with_name("." + uuid.uuid4().hex + ".tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(target)
                with self.task_store.connect() as db:
                    db.execute(
                        "UPDATE material_batches SET upload_file_count=?,upload_bytes=?,progress_total=?,updated_at=? WHERE id=? AND status='uploading'",
                        (next_count, next_size, next_count, _now(), batch_id),
                    )
            except Exception:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise
            return {"batch_id": batch_id, "file_count": next_count, "total_bytes": next_size}

    def abort_upload(self, batch_id: str, *, project_id: str | None = None) -> dict:
        """上传失败后退出上传态，保留已收到的文件，允许用户在管理页回收。"""
        self._check_batch_scope(batch_id, project_id, write=True)
        with self._lock:
            row = self._batch_row(batch_id)
            if row["status"] != "uploading":
                raise ValueError("该批次当前不在上传中")
            self._set_batch_error(batch_id, "failed", "上传未完成，可在批次管理中删除后重新导入")
        return self.get_batch(batch_id, project_id=project_id)

    def finish_upload(self, batch_id: str, *, project_id: str | None = None) -> dict:
        self._check_batch_scope(batch_id, project_id, write=True)
        with self._lock, self.task_store.connect() as db:
            row = self._batch_row(batch_id)
            if row["status"] != "uploading":
                raise ValueError("上传批次不在 uploading 状态")
            if row["upload_file_count"] == 0:
                raise ValueError("上传批次没有可处理的图片")
            db.execute("UPDATE material_batches SET status='ready_to_analyze',updated_at=? WHERE id=?", (_now(), batch_id))
        return self.get_batch(batch_id, project_id=project_id)

    def begin_store_upload(self, batch_id: str, store_id: str, *, project_id: str | None = None) -> dict:
        """为当前门店创建临时补图目录，不触碰用户的原始素材目录。"""
        self._check_batch_scope(batch_id, project_id, write=True)
        batch = self._batch_row(batch_id)
        if batch["status"] != "completed":
            raise ValueError("只有分析完成的批次可以补充门店素材")
        row = self._decision_row(batch_id, store_id)
        if row["decision"] == "confirmed":
            raise ValueError("该门店已确认；请先撤销确认再修改素材")
        upload_id = uuid.uuid4().hex
        folder = self._supplement_dir(batch_id, store_id, upload_id)
        folder.mkdir(parents=True, mode=0o700, exist_ok=False)
        return {"upload_id": upload_id, "status": "uploading"}

    def add_store_upload_file(self, batch_id: str, store_id: str, upload_id: str, filename: str, content: bytes,
                              *, project_id: str | None = None) -> dict:
        self._check_batch_scope(batch_id, project_id, write=True)
        if not _UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("补图上传编号无效")
        batch = self._batch_row(batch_id)
        if batch["status"] != "completed":
            raise ValueError("只有分析完成的批次可以补充门店素材")
        row = self._decision_row(batch_id, store_id)
        if row["decision"] == "confirmed":
            raise ValueError("该门店已确认；请先撤销确认再修改素材")
        relative = self._safe_upload_path(filename)
        if len(relative.parts) != 1:
            raise ValueError("单店补图只接受图片文件，不接受目录路径")
        if not isinstance(content, bytes) or not content:
            raise ValueError("上传图片不能为空")
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("单文件超过 50 MB 限制")
        self._validate_uploaded_image(relative, content)
        folder = self._supplement_dir(batch_id, store_id, upload_id)
        batch_dir = self.batch_root / batch_id
        if any(path.is_symlink() for path in (batch_dir, batch_dir / "supplements", batch_dir / "supplements" / store_id, folder)) or not folder.is_dir():
            raise ValueError("补图上传已失效，请重新选择图片")
        images = _enumerate_images(folder)
        if len(images) >= MAX_STORE_UPLOAD_FILES:
            raise ValueError(f"单店一次最多上传 {MAX_STORE_UPLOAD_FILES} 张图片")
        used = sum(path.stat().st_size for path in images)
        if used + len(content) > MAX_STORE_UPLOAD_BYTES:
            raise ValueError("单店一次上传总量不能超过 500 MB")
        target = folder / f"{uuid.uuid4().hex}{relative.suffix.lower()}"
        temporary = target.with_name(target.name + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return {
            "path": PurePosixPath("__supplement__", store_id, upload_id, target.name).as_posix(),
            "display_name": relative.name,
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def commit_store_upload(self, batch_id: str, store_id: str, upload_id: str, mode: str, files: list[dict],
                            *, project_id: str | None = None) -> dict:
        self._check_batch_scope(batch_id, project_id, write=True)
        if not _UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("补图上传编号无效")
        if mode not in {"append", "replace"}:
            raise ValueError("补图方式必须是 append 或 replace")
        if not isinstance(files, list) or not files or len(files) > MAX_STORE_UPLOAD_FILES:
            raise ValueError(f"请选择 1 至 {MAX_STORE_UPLOAD_FILES} 张有效图片")
        with self._lock:
            batch = self._batch_row(batch_id)
            if batch["status"] != "completed":
                raise ValueError("只有分析完成的批次可以补充门店素材")
            row = self._decision_row(batch_id, store_id)
            if row["decision"] == "confirmed":
                raise ValueError("该门店已确认；请先撤销确认再修改素材")
            session_prefix = PurePosixPath("__supplement__", store_id, upload_id)
            prepared = []
            total_bytes = 0
            for item in files:
                if not isinstance(item, dict):
                    raise ValueError("补图文件记录无效")
                path_value, display_name, digest = item.get("path"), item.get("display_name"), item.get("sha256")
                path = PurePosixPath(str(path_value or "").replace("\\", "/"))
                if path.is_absolute() or path.parts[:3] != session_prefix.parts or len(path.parts) != 4:
                    raise ValueError("补图文件不属于当前上传批次")
                if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
                    raise ValueError("补图文件 hash 无效")
                safe_name = self._safe_upload_path(str(display_name or ""))
                if len(safe_name.parts) != 1:
                    raise ValueError("补图文件名无效")
                source = self._checked_source(batch, {"path": path.as_posix(), "sha256": digest, "supplement_store_id": store_id})
                total_bytes += source.stat().st_size
                prepared.append({
                    "path": path.as_posix(), "sha256": digest, "display_name": safe_name.name,
                    "origin": "supplement", "supplement_store_id": store_id,
                    "detection_count": 0, "candidates": [],
                })
            if total_bytes > MAX_STORE_UPLOAD_BYTES:
                raise ValueError("单店一次上传总量不能超过 500 MB")

            store = json.loads(row["store_json"])
            if mode == "replace":
                store["images"] = prepared
                store["reason"] = f"已替换为 {len(prepared)} 张图片，等待重新分析"
            else:
                store["images"].extend(prepared)
                store["reason"] = f"已补充 {len(prepared)} 张图片，等待重新分析"
            store["status"] = "needs_review"
            store["selected_source"] = None
            store["selected_artifact"] = None
            for image in store["images"]:
                for candidate in image.get("candidates", []):
                    candidate["selected"] = False
            decision = "skipped" if row["decision"] == "skipped" else "pending"
            with self.task_store.connect() as db:
                db.execute(
                    "UPDATE material_decisions SET decision=?,analysis_status='needs_review',recommendation=0,selected_json='{}',selected_source_hash='',store_json=?,updated_at=? WHERE batch_id=? AND store_id=?",
                    (decision, json.dumps(store, ensure_ascii=False), _now(), batch_id, store_id),
                )
                self._refresh_batch_summary(db, batch_id)
        return {"store": self.get_store(batch_id, store_id, project_id=project_id),
                "uploaded": len(prepared), "mode": mode}

    def abort_store_upload(self, batch_id: str, store_id: str, upload_id: str, *, project_id: str | None = None) -> None:
        self._check_batch_scope(batch_id, project_id, write=True)
        if not _UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("补图上传编号无效")
        batch = self._batch_row(batch_id)
        self._decision_row(batch_id, store_id)
        prefix = PurePosixPath("__supplement__", store_id, upload_id).as_posix() + "/"
        with self.task_store.connect() as db:
            rows = db.execute("SELECT store_json FROM material_decisions WHERE batch_id=?", (batch_id,)).fetchall()
        if any(
            image.get("path", "").startswith(prefix)
            for row in rows for image in json.loads(row["store_json"]).get("images", [])
        ):
            raise ValueError("补图已提交，不能作为临时上传清理")
        folder = self._supplement_dir(batch["id"], store_id, upload_id)
        if folder.is_symlink():
            raise ValueError("补图目录不安全，拒绝删除")
        shutil.rmtree(folder, ignore_errors=True)

    def reanalyze_store(self, batch_id: str, store_id: str, *, project_id: str | None = None) -> dict:
        """只重跑当前门店的本地二维码检测与 OCR；批次其他门店和决定不变。"""
        self._check_batch_scope(batch_id, project_id, write=True)
        with self._lock:
            batch = self._batch_row(batch_id)
            if batch["status"] != "completed":
                raise ValueError("只有分析完成的批次可以重新分析单店")
            self._reanalyze_store_locked(batch, store_id)
        return self.get_store(batch_id, store_id, project_id=project_id)

    def _reanalyze_store_locked(self, batch: dict, store_id: str) -> None:
        batch_id = batch["id"]
        row = self._decision_row(batch_id, store_id)
        if row["decision"] == "confirmed":
            raise ValueError("该门店已确认；请先撤销确认再重新分析")
        store = json.loads(row["store_json"])
        if not store.get("images"):
            raise ValueError("当前门店没有图片，请先补充素材")
        inspect = self.inspector or _apple_vision_inspector
        analysis_dir = self.batch_root / batch_id / "repair-analysis"
        _mkdir_ok(analysis_dir)
        self._ocr_local.cache = {}
        analyze_store(
            store, Path(batch["source_root"]),
            inspector=inspect,
            resolve_path=lambda image: self._checked_source(batch, image),
        )
        manifest = {"stores": [store]}
        source_root = Path(batch["source_root"])
        self._classify_and_select(
            manifest, source_root, analysis_dir,
            duplicate_names=self._decision_duplicate_names(batch_id),
            source_resolver=lambda image: self._checked_source(batch, image),
        )
        store = manifest["stores"][0]
        # 检测后再核一次源图 hash，避免分析期间文件被替换。
        for image in store["images"]:
            self._checked_source(batch, image)
        store = self._sanitize_manifest(
            {"input_root": batch["source_root"], "stores": [store]}
        )["stores"][0]
        recommendation = any(
            candidate.get("selected")
            for image in store["images"] for candidate in image.get("candidates", [])
        )
        with self.task_store.connect() as db:
            db.execute(
                "UPDATE material_decisions SET analysis_status=?,recommendation=?,selected_json='{}',selected_source_hash='',store_json=?,updated_at=? WHERE batch_id=? AND store_id=?",
                (store["status"], int(recommendation), json.dumps(store, ensure_ascii=False), _now(), batch_id, store_id),
            )
            self._refresh_batch_summary(db, batch_id)

    def _decision_duplicate_names(self, batch_id: str) -> set[str]:
        """跨父目录同名门店：只有同名目录分属不同父路径时才算冲突。"""
        with self.task_store.connect() as db:
            rows = db.execute(
                "SELECT source_name FROM material_decisions WHERE batch_id=? "
                "GROUP BY source_name HAVING COUNT(DISTINCT source_folder)>1",
                (batch_id,),
            ).fetchall()
        return {row["source_name"] for row in rows}

    # ------------------------------------------------------------ 任务入口

    def _store_items(self, stores: list[dict]) -> list[dict]:
        return [{"store_id": _store_id(store["source_folder"]), "source_folder": store["source_folder"]}
                for store in stores]

    def start_analysis(self, batch_id: str, *, project_id: str | None = None) -> dict:
        """把整批分析转成持久化 job；立即返回，前端通过任务中心观察进度。"""
        self._check_batch_scope(batch_id, project_id, write=True)
        batch = self._batch_row(batch_id)
        if batch["status"] != "ready_to_analyze":
            raise ValueError("只有 ready_to_analyze 批次可以开始分析")
        source_root = Path(batch["source_root"])
        files = _enumerate_images(source_root)
        self._validate_local_limits(files)
        if batch["source_kind"] == "upload" and len(files) != batch["upload_file_count"]:
            raise ValueError("上传素材文件集合发生变化，拒绝分析")
        stores, _duplicates = discover_stores(source_root)
        if not stores:
            raise ValueError("素材目录中没有可分析的图片")
        items = self._store_items(stores)
        concurrency = self._analysis_concurrency()
        job = self.jobs.create(batch_id, "analyze", items, concurrency)
        self.jobs.event(job["id"], "info", f"任务已创建：{len(items)} 家门店，并发 {concurrency}")
        self.job_runner.wake()
        return {**self.get_batch(batch_id, project_id=project_id), "job": self.jobs.get(job["id"])}

    def start_reanalysis(self, batch_id: str, store_ids: list[str], *, project_id: str | None = None) -> dict:
        """对已完成批次里选中的门店重跑检测；已确认门店必须先撤销确认。"""
        self._check_batch_scope(batch_id, project_id, write=True)
        batch = self._batch_row(batch_id)
        if batch["status"] != "completed":
            raise ValueError("只有分析完成的批次可以重新分析")
        ids = list(dict.fromkeys(store_ids))
        if not ids:
            raise ValueError("请选择要重新分析的门店")
        with self.task_store.connect() as db:
            rows = {row["store_id"]: dict(row) for row in db.execute(
                "SELECT store_id,decision,source_folder FROM material_decisions WHERE batch_id=?", (batch_id,),
            )}
        if not set(ids) <= set(rows):
            raise ValueError("重新分析列表含有未知门店")
        pending = [store_id for store_id in ids if rows[store_id]["decision"] == "pending"]
        if not pending:
            raise ValueError("选中的门店没有待审核的；已确认和不合格的门店不在重新分析范围内")
        items = [{"store_id": store_id, "source_folder": rows[store_id]["source_folder"]} for store_id in pending]
        concurrency = self._analysis_concurrency()
        job = self.jobs.create(batch_id, "reanalyze", items, concurrency)
        self.jobs.event(job["id"], "info", f"重新分析任务已创建：{len(items)} 家门店，并发 {concurrency}")
        self.job_runner.wake()
        return {**self.get_batch(batch_id, project_id=project_id), "job": self.jobs.get(job["id"])}

    def retry_failed(self, batch_id: str, *, project_id: str | None = None) -> dict:
        """只重跑上一次任务里失败且未确认的门店；会先取消旧任务再建新任务。"""
        self._check_batch_scope(batch_id, project_id, write=True)
        batch = self._batch_row(batch_id)
        latest = self.jobs.latest(batch_id)
        if latest is None:
            raise ValueError("该批次还没有分析任务")
        failed = self.jobs.failed_items(latest["id"])
        with self.task_store.connect() as db:
            confirmed = {row["store_id"] for row in db.execute(
                "SELECT store_id FROM material_decisions WHERE batch_id=? AND decision='confirmed'", (batch_id,),
            )}
        items = [item for item in failed if item["store_id"] not in confirmed]
        if not items:
            raise ValueError("没有可重试的失败门店")
        concurrency = self._analysis_concurrency()
        job = self.jobs.create_replacing(batch_id, "retry_failed", items, concurrency)
        self.jobs.event(job["id"], "info", f"重试任务已创建：{len(items)} 家失败门店")
        self.job_runner.wake()
        return {**self.get_batch(batch_id, project_id=project_id), "job": self.jobs.get(job["id"])}

    def pause_job(self, job_id: str, *, project_id: str | None = None) -> dict:
        self._check_job_scope(job_id, project_id, write=True)
        if not self.jobs.request_pause(job_id):
            raise ValueError("当前任务状态不支持暂停")
        self.jobs.event(job_id, "info", "已请求暂停，将在当前门店处理完后停止")
        return self.jobs.get(job_id) or {}

    def resume_job(self, job_id: str, *, project_id: str | None = None) -> dict:
        self._check_job_scope(job_id, project_id, write=True)
        if not self.jobs.resume(job_id):
            raise ValueError("只有已暂停或已中断的任务可以恢复")
        self.jobs.event(job_id, "info", "任务已恢复并重新排队")
        self.job_runner.wake()
        return self.jobs.get(job_id) or {}

    def cancel_job(self, job_id: str, *, project_id: str | None = None) -> dict:
        self._check_job_scope(job_id, project_id, write=True)
        if not self.jobs.cancel(job_id):
            raise ValueError("当前任务状态无法取消")
        self.jobs.event(job_id, "warn", "任务已取消，未完成门店保持待处理")
        return self.jobs.get(job_id) or {}

    def list_jobs(self, *, active: bool = True, batch_id: str = "", limit: int = 20,
                  project_id: str | None = None) -> list[dict]:
        if self.role == "hub" and project_id is None:
            raise ProjectConflict("项目范围缺失")
        if project_id is None:
            project_id = LEGACY_ID
        if project_id is not None:
            return self.jobs.for_project(project_id, active=active, limit=limit, batch_id=batch_id)
        if batch_id:
            return self.jobs.for_batch(batch_id, limit)
        if active:
            return self.jobs.active(limit)
        return self.jobs.for_batch(batch_id, limit) if batch_id else self.jobs.active(limit)

    def job_detail(self, job_id: str, events: bool = True, *, project_id: str | None = None) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise ValueError("任务不存在")
        if project_id is not None or self.role == "hub":
            self._check_batch_scope(job["batch_id"], project_id)
        job["events"] = self.jobs.events(job_id, 50) if events else []
        return job

    def _check_job_scope(self, job_id: str, project_id: str | None, *, write: bool) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise ValueError("任务不存在")
        self._check_batch_scope(job["batch_id"], project_id, write=write)
        return job

    # ------------------------------------------------------------ job 执行

    def _prepare_job(self, job: dict) -> dict:
        """每个 job 只枚举一次源目录；单店 worker 复用骨架与重名集合。"""
        batch = self._batch_row(job["batch_id"])
        source_root = Path(batch["source_root"])
        stores, duplicates = discover_stores(source_root)
        analysis_dir = self.batch_root / job["batch_id"] / "analysis"
        # Windows 上并发创建门店目录会偶发 WinError 5（拒绝访问）；
        # 目录属于准备工作，放到单线程阶段串行建好，worker 只负责写文件。
        artifacts_root = analysis_dir / "可采集"
        try:
            _mkdir_ok(artifacts_root)
        except OSError:
            logger.exception("prepare artifacts root failed")
        for store in stores:
            try:
                _mkdir_ok(artifacts_root / store["source_name"])
            except OSError:
                logger.warning("prepare store dir failed store=%s", store["source_name"])
        return {
            "batch": batch,
            "source_root": source_root,
            "analysis_dir": analysis_dir,
            "stores": {_store_id(store["source_folder"]): store for store in stores},
            "duplicate_names": duplicates,
        }

    def _run_job_item(self, job: dict, item: dict, context: dict | None) -> None:
        """处理一家门店：检测 + 语义分类 + 写决策；任何异常由 JobRunner 记短码并重试。"""
        context = context or self._prepare_job(job)
        batch = context["batch"]
        batch_id = job["batch_id"]
        store_id = item["store_id"]
        analysis_dir = context["analysis_dir"]
        _mkdir_ok(analysis_dir)
        try:
            row = self._decision_row(batch_id, store_id)
        except ValueError:
            row = None
        if row is not None:
            if row["decision"] != "pending":
                # 运行期间门店可能被人工确认或判为不合格：这是受保护状态，跳过而不是判失败。
                self.jobs.event(
                    job["id"], "warn",
                    f"{row['source_folder'].rstrip('/').split('/')[-1]}：已人工处理，跳过重新分析",
                    store_id=store_id,
                )
                return
            store = json.loads(row["store_json"])
            if not store.get("images"):
                raise ValueError("当前门店没有图片，请先补充素材")
        else:
            store = context["stores"].get(store_id)
            if store is None:
                raise ValueError("素材目录中找不到该门店")
        self._ocr_local.cache = {}
        # 检测阶段按相对路径定位源图；此时骨架还没有 sha256，校验放到检测完成之后。
        resolve = lambda image: context["source_root"] / image["path"]  # noqa: E731 - 与整批 build_materials 同口径
        analyze_store(
            store, context["source_root"],
            inspector=self.inspector or _apple_vision_inspector,
            resolve_path=resolve,
        )
        manifest = {"stores": [store]}
        self._classify_and_select(
            manifest, context["source_root"], analysis_dir,
            duplicate_names=context["duplicate_names"],
            source_resolver=lambda image: self._checked_source(batch, image),
        )
        store = manifest["stores"][0]
        # 检测后再核一次源图 hash，避免分析期间文件被替换。
        for image in store["images"]:
            self._checked_source(batch, image)
        # 与整批路径一致：落库前统一清洗。
        store = self._sanitize_manifest(
            {"input_root": str(context["source_root"]), "stores": [store]}
        )["stores"][0]
        recommendation = any(
            candidate.get("selected")
            for image in store["images"] for candidate in image.get("candidates", [])
        )
        with self.task_store.connect() as db:
            # 重跑只覆盖分析结果，保留人工决策与不合格原因。
            db.execute(
                "INSERT INTO material_decisions(batch_id,store_id,source_folder,source_name,analysis_status,recommendation,store_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(batch_id,store_id) DO UPDATE SET "
                "analysis_status=excluded.analysis_status,recommendation=excluded.recommendation,"
                "store_json=excluded.store_json,selected_json='{}',selected_source_hash='',updated_at=excluded.updated_at",
                (batch_id, store_id, store["source_folder"], store["source_name"], store["status"],
                 int(recommendation), json.dumps(store, ensure_ascii=False), _now()),
            )

    def _job_finished(self, job: dict | None, context: dict | None) -> None:
        if job is None:
            return
        batch_id = job["batch_id"]
        counts = job.get("counts") or self.jobs.counts(job["id"])
        with self.task_store.connect() as db:
            if job["status"] == "completed":
                self._refresh_batch_summary(db, batch_id)
                error = (f"部分门店未能分析（{counts['failed']} 家），可在任务详情重试"
                         if counts["failed"] else "")
                db.execute(
                    "UPDATE material_batches SET status='completed',error=?,progress_done=?,progress_total=?,updated_at=? WHERE id=?",
                    (error, counts["total"], counts["total"], _now(), batch_id),
                )
            else:
                # 暂停/中断/取消不改变批次可用性，只镜像进度；已完成的门店决策照常保留。
                message = job.get("message") or job.get("error") or ""
                db.execute(
                    "UPDATE material_batches SET progress_done=?,progress_total=?,error=?,updated_at=? WHERE id=?",
                    (counts["done"] + counts["failed"], counts["total"], message, _now(), batch_id),
                )
        if job["status"] == "completed" and context:
            self._write_analysis_artifacts(batch_id, context)

    def _write_analysis_artifacts(self, batch_id: str, context: dict) -> None:
        """任务结束后一次性落 manifest/metrics，供排查与历史兼容；发布仍以决策表为准。"""
        row = self._batch_row(batch_id)
        with self.task_store.connect() as db:
            stores = [json.loads(item["store_json"]) for item in db.execute(
                "SELECT store_json FROM material_decisions WHERE batch_id=? ORDER BY source_folder,store_id",
                (batch_id,),
            )]
        try:
            summary = json.loads(row["summary_json"])
        except (TypeError, ValueError):
            summary = {}
        manifest = {
            "input_root": row["source_root"],
            "created_at": _now(),
            "stores": stores,
            "summary": summary,
        }
        analysis_dir = context["analysis_dir"]
        _mkdir_ok(analysis_dir)
        atomic_json(analysis_dir / "manifest.json", self._sanitize_manifest(manifest))
        if isinstance(summary.get("detection_stats"), dict):
            atomic_json(analysis_dir / "analysis-metrics.json", summary["detection_stats"])


    def _supplement_dir(self, batch_id: str, store_id: str, upload_id: str) -> Path:
        if not _UPLOAD_ID_RE.fullmatch(batch_id) or not re.fullmatch(r"[0-9a-f]{24}", store_id) or not _UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("补图路径编号无效")
        batch_root = self.batch_root / batch_id
        store_root = batch_root / "supplements" / store_id
        if batch_root.is_symlink() or (batch_root / "supplements").is_symlink() or store_root.is_symlink():
            raise ValueError("批次素材目录不安全")
        folder = batch_root / "supplements" / store_id / upload_id
        return folder

    def _refresh_batch_summary(self, db, batch_id: str, extra: dict | None = None) -> None:
        """按决策表一次性重算 summary 与门店口径进度，替换原先的全表逐店扫描。"""
        batch = db.execute("SELECT summary_json FROM material_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise ValueError("素材批次不存在")
        try:
            summary = json.loads(batch["summary_json"])
        except (TypeError, ValueError):
            summary = {}
        rows = db.execute("SELECT recommendation,store_json FROM material_decisions WHERE batch_id=?", (batch_id,)).fetchall()
        stores = [json.loads(row["store_json"]) for row in rows]
        summary["total_images"] = sum(len(store.get("images", [])) for store in stores)
        summary["total_stores"] = len(rows)
        summary["selected_stores"] = sum(bool(row["recommendation"]) for row in rows)
        summary["review_stores"] = len(rows) - summary["selected_stores"]
        total_ms = round(sum(float(store.get("square_ms", 0.0)) for store in stores), 1)
        summary["detection_stats"] = self._detection_stats(stores, total_ms)
        for key, value in (extra or {}).items():
            if value:
                summary[key] = value
        db.execute(
            "UPDATE material_batches SET summary_json=?,progress_done=?,progress_total=?,updated_at=? WHERE id=?",
            (json.dumps(summary, ensure_ascii=False), len(rows), len(rows), _now(), batch_id),
        )

    def create_upload_batch(self, files: Iterable[dict], *, project_id: str | None = None) -> dict:
        """小批量/测试便捷接口；生产 API 应使用逐文件三步上传。"""
        batch = self.begin_upload(project_id=project_id)
        try:
            for item in files:
                self.add_upload_file(batch["batch_id"], item["path"], item["content"], project_id=project_id)
            return self.finish_upload(batch["batch_id"], project_id=project_id)
        except Exception:
            self._set_batch_error(batch["batch_id"], "upload_error", "上传失败；未开始分析")
            shutil.rmtree(self.upload_root / batch["batch_id"], ignore_errors=True)
            raise

    @staticmethod
    def _detection_stats(stores: list[dict], total_ms: float) -> dict:
        """方形检测的聚合指标；只含计数与耗时，不含路径或二维码内容。"""
        return {
            "square": {
                "scanned_stores": sum(bool(store.get("square_checked")) for store in stores),
                "hit_stores": sum(
                    bool(store.get("square_checked"))
                    and any(
                        candidate.get("payload_sha256")
                        for image in store.get("images", []) for candidate in image.get("candidates", [])
                    )
                    for store in stores
                ),
                "ms": round(sum(float(store.get("square_ms", 0.0)) for store in stores), 1),
            },
            "total_ms": total_ms,
        }

    def adopt_manifest(self, manifest_path: Path, *, project_id: str | None = None) -> dict:
        path = Path(manifest_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("manifest 路径必须是文件")
        if path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("manifest 超过 100 MB 限制")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("manifest 不是有效 JSON") from None
        safe_manifest = self._sanitize_manifest(manifest)
        source = self._validate_local_root(Path(safe_manifest["input_root"]))
        batch_id = uuid.uuid4().hex
        with self.task_store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            project_id = self.project_for_new_batch(db, project_id)
            stamp = _now()
            db.execute(
                "INSERT INTO material_batches(id,source_kind,source_root,status,progress_done,progress_total,summary_json,manifest_json,created_at,updated_at,project_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (batch_id, "adopted_manifest", str(source), "completed", safe_manifest["summary"]["total_images"], safe_manifest["summary"]["total_images"], json.dumps(safe_manifest["summary"], ensure_ascii=False), json.dumps(safe_manifest, ensure_ascii=False), stamp, stamp, project_id),
            )
            for store in safe_manifest["stores"]:
                store_id = _store_id(store["source_folder"])
                recommendation = any(candidate.get("selected") for image in store["images"] for candidate in image["candidates"])
                db.execute(
                    "INSERT INTO material_decisions(batch_id,store_id,source_folder,source_name,analysis_status,recommendation,store_json,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (batch_id, store_id, store["source_folder"], store["source_name"], store["status"], int(recommendation), json.dumps(store, ensure_ascii=False), stamp),
                )
        return self.get_batch(batch_id, project_id=project_id)

    def _classify_and_select(self, manifest: dict, source_root: Path, analysis_dir: Path, progress=None,
                             source_resolver=None, duplicate_names: set[str] | None = None) -> None:
        """图片级判断用途，门店级再按决策树产出唯一建议，两层职责分开。"""
        resolve_source = source_resolver or (lambda image: source_root / image["path"])
        if duplicate_names is None:
            # 整批调用时自行判定；逐店 job 必须由批次上下文传入，否则漏掉跨父目录同名冲突。
            duplicate_names = set()
            names: dict[str, set[str]] = {}
            for store in manifest["stores"]:
                names.setdefault(store["source_name"], set()).add(store["source_folder"])
            duplicate_names = {name for name, folders in names.items() if len(folders) > 1}
        processed = 0
        for store in manifest["stores"]:
            # 清除 build_materials 的几何启发式预选；由语义分类单独决定建议。
            self._reset_store_selection(store, analysis_dir)
            for image in store["images"]:
                self._classify_image(image, resolve_source(image))
            for _image in store["images"]:
                processed += 1
                if progress:
                    progress(processed)

            if store["source_name"] in duplicate_names:
                store["status"] = "needs_review"
                store["reason"] = "不同父路径存在同名店铺目录，拒绝归档以防混店"
                store["recommendation_level"] = "none"
                store["wechat_validation_status"] = "not_applicable"
                continue

            picked, level, reason = self._recommend_store(store, resolve_source)
            store["recommendation_level"] = level
            store["wechat_validation_status"] = "unverified" if picked else "not_applicable"
            if picked is None:
                store["status"] = "needs_review"
                store["reason"] = reason
                continue
            image_index, candidate_index, image, candidate = picked
            artifact_relative = PurePosixPath("可采集") / store["source_name"] / "二维码.png"
            artifact = analysis_dir.joinpath(*artifact_relative.parts)
            try:
                # 批量分析时 Windows 偶发对新建门店目录返回 WinError 5；
                # 同一路径单店重跑即可成功，属于瞬时拒绝访问，按退避重试而非直接降级人工。
                for attempt in range(4):
                    try:
                        _mkdir_ok(artifact.parent)
                        _atomic_crop(resolve_source(image), candidate["box"], artifact)
                        break
                    except OSError as exc:
                        logger.warning(
                            "crop attempt=%s failed winerror=%s filename=%s",
                            attempt, getattr(exc, "winerror", None),
                            Path(getattr(exc, "filename", "") or "").name,
                        )
                        if attempt == 3:
                            raise
                        time.sleep(0.25 * (2 ** attempt))
            except Exception as exc:
                artifact.unlink(missing_ok=True)
                # 本机日志保留完整堆栈用于定位；业务字段只写异常类型。
                logger.exception("crop failed store=%s image=%s", store["source_name"], image["path"])
                store["status"] = "needs_review"
                # 只记异常类型，避免把本机路径或图片内容写进业务字段。
                store["reason"] = f"候选裁剪失败（{type(exc).__name__}），需人工复核"
                store["recommendation_level"] = "none"
                store["wechat_validation_status"] = "not_applicable"
                continue
            candidate["selected"] = True
            store["status"] = "selected"
            store["reason"] = reason
            store["selected_source"] = image["path"]
            store["selected_artifact"] = artifact_relative.as_posix()

    def _classify_image(self, image: dict, source: Path) -> None:
        """图片级：只判断本图每个候选的用途，并记录漏检提示，不影响其他图片。"""
        candidates = image.get("candidates", [])
        image.pop("ordering_text_seen", None)
        image.pop("possible_missed_code", None)
        if not candidates:
            image["is_multi_qr_image"] = False
            # 全图 OCR 明确看到点餐文字却一个码都没定位到：强制再扫一次，
            # 避免整图检测偶发漏检把“有码门店”直接判成无码。
            try:
                ocr_items = self._read_ocr(source)
                if not isinstance(ocr_items, list):
                    ocr_items = None
            except Exception:
                ocr_items = None
            if ocr_items is None or _classify_local_purpose(self._ocr_text(ocr_items))[0] != "ordering":
                return
            if self._retry_candidates(image, source):
                self._classify_all(image["candidates"], ocr_items)
                image["is_multi_qr_image"] = self._valid_candidate_count(image["candidates"]) >= 2
            if not any(
                candidate.get("purpose") == "ordering" and candidate.get("quality") != "rejected"
                for candidate in image["candidates"]
            ):
                image["ordering_text_seen"] = True
                image["possible_missed_code"] = True
            return
        # 全部候选都能由链接直接定性时（如企业微信码），不需要跑 OCR。
        url_decided = not any(
            (candidate.get("purpose_hint") or "unknown") not in {"enterprise_wechat", "ordering", "not_ordering", "conflict"}
            for candidate in candidates
        )
        ocr_items = None
        if not url_decided:
            try:
                ocr_items = self._read_ocr(source)
                if not isinstance(ocr_items, list):
                    ocr_items = None
            except Exception:
                ocr_items = None
        self._classify_all(candidates, ocr_items)
        # 一图多码只统计通过质量门禁的候选；纹理误检不能把单码图变成多码图。
        image["is_multi_qr_image"] = self._valid_candidate_count(candidates) >= 2

        full_text = self._ocr_text(ocr_items)
        valid_ordering = any(
            candidate.get("purpose") == "ordering" and candidate.get("quality") != "rejected"
            for candidate in candidates
        )
        # 漏检重试：OCR 明显看到点餐文字却没有有效点餐候选，或多码图强制再扫一次。
        needs_retry = (
            (ocr_items is not None and _classify_local_purpose(full_text)[0] == "ordering" and not valid_ordering)
            or self._valid_candidate_count(candidates) >= 2
        )
        if needs_retry and self._retry_candidates(image, source):
            candidates = image["candidates"]
            if ocr_items is None:
                try:
                    ocr_items = self._read_ocr(source)
                    if not isinstance(ocr_items, list):
                        ocr_items = None
                except Exception:
                    ocr_items = None
            self._classify_all(candidates, ocr_items)
            image["is_multi_qr_image"] = self._valid_candidate_count(candidates) >= 2
            full_text = self._ocr_text(ocr_items)

        # 全图 OCR 只提示可能存在漏检：本图出现点餐文字，但没有任何候选被判定为点餐。
        # 它不能单独给候选定罪，也不能单独触发推荐。
        if ocr_items is not None and _classify_local_purpose(full_text)[0] == "ordering" and not any(
            candidate.get("purpose") == "ordering" and candidate.get("quality") != "rejected"
            for candidate in candidates
        ):
            image["ordering_text_seen"] = True
            image["possible_missed_code"] = True

    @staticmethod
    def _ocr_text(ocr_items: list[dict] | None) -> str:
        if not ocr_items:
            return ""
        return " ".join(
            item.get("text", "") for item in ocr_items
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )

    def _classify_all(self, candidates: list[dict], ocr_items: list[dict] | None) -> None:
        assigned = _assigned_ocr_texts(candidates, ocr_items) if ocr_items is not None else {}
        for index, candidate in enumerate(candidates):
            self._classify_candidate(candidate, index, assigned, ocr_items is not None)

    @staticmethod
    def _valid_candidate_count(candidates: list[dict]) -> int:
        return sum(1 for candidate in candidates if candidate.get("quality") != "rejected")

    @staticmethod
    def _same_candidate(first: dict, second: dict) -> bool:
        try:
            ax, ay, aw, ah = (float(first["box"][key]) for key in ("x", "y", "width", "height"))
            bx, by, bw, bh = (float(second["box"][key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            return False
        center_distance = math.hypot(ax + aw / 2 - bx - bw / 2, ay + ah / 2 - by - bh / 2)
        return center_distance < max(aw, ah, bw, bh) * 0.3

    def _retry_candidates(self, image: dict, source: Path) -> bool:
        """对漏检/多码图再扫一次；新框与旧框合并，优先保留质量更好的那个。"""
        if self.inspector is None:
            return False
        try:
            record = _inspect_image(source, image["path"], self.inspector)
        except Exception:
            return False
        incoming = record.get("candidates") or []
        existing = image["candidates"]
        added = False
        for candidate in incoming:
            if not isinstance(candidate, dict):
                continue
            candidate["selected"] = False
            candidate["retry"] = True
            for index, old in enumerate(existing):
                if self._same_candidate(candidate, old):
                    if self._candidate_better(candidate, old):
                        existing[index] = candidate
                        added = True
                    break
            else:
                existing.append(candidate)
                added = True
        if added:
            image["detection_count"] = max(int(image.get("detection_count", 0)), len(existing))
        return added

    @staticmethod
    def _candidate_better(first: dict, second: dict) -> bool:
        def score(candidate):
            return (
                1 if candidate.get("quality") != "rejected" else 0,
                1 if candidate.get("payload_sha256") else 0,
                candidate.get("qr_fill") if isinstance(candidate.get("qr_fill"), (int, float)) else 0,
                candidate.get("quality_score", 0) or 0,
            )
        return score(first) > score(second)

    @staticmethod
    def _classify_candidate(candidate: dict, index: int, assigned: dict[int, str], ocr_done: bool) -> None:
        """二维码级：URL 明确证据优先，只用离该码最近的文字做辅助判断。"""
        # 质量门禁在用途判断之前：未通过结构/包含度校验的框不能被 OCR 提升为点餐候选。
        if candidate.get("quality") == "rejected":
            candidate.update(
                purpose="unknown",
                purpose_source="local_ocr",
                purpose_reason=candidate.get("quality_reason") or "候选框未通过二维码质量校验，需人工确认",
            )
            return
        hint = candidate.get("purpose_hint") or "unknown"
        if hint == "enterprise_wechat":
            candidate.update(
                purpose="enterprise_wechat",
                purpose_source="payload_domain",
                purpose_reason="本地解码识别为企业微信域名",
            )
            return
        if hint == "ordering":
            candidate.update(
                purpose="ordering",
                purpose_source="payload_url",
                purpose_reason="二维码链接命中点餐关键词",
            )
            return
        if hint == "not_ordering":
            candidate.update(
                purpose="not_ordering",
                purpose_source="payload_url",
                purpose_reason="二维码链接命中非点餐关键词",
            )
            return
        if hint == "conflict":
            candidate.update(
                purpose="conflict",
                purpose_source="payload_url",
                purpose_reason="二维码链接同时命中点餐与非点餐证据，需人工确认",
            )
            return
        if not ocr_done:
            candidate.update(
                purpose="unknown",
                purpose_source="local_ocr",
                purpose_reason="本地 OCR 未能完成，需人工确认",
            )
            return
        purpose, reason = _classify_local_purpose(assigned.get(index, ""))
        if purpose == "ordering" and hint == "ordering_candidate":
            # 餐饮 SaaS 域名本身不足以定性，必须由周边 OCR 复核后才算点餐。
            candidate.update(
                purpose="ordering",
                purpose_source="payload_url_ocr",
                purpose_reason="餐饮点餐域名且周边 OCR 识别到点餐标识",
            )
            return
        if purpose == "ordering" and not candidate.get("payload_sha256"):
            reason += "；二维码未能由本地解码器读出，需人工确认"
        candidate.update(
            purpose=purpose,
            purpose_source="local_ocr",
            purpose_reason=reason,
        )

    def _mark_missed_code_hints(self, store: dict, resolve_source: Callable[[dict], Path]) -> bool:
        """只在门店没有明确点餐候选时补查无码图片的全图文字，作为漏检线索。"""
        found = False
        for image in store["images"]:
            if image.get("possible_missed_code"):
                found = True
                continue
            # 有候选的图片已在图片级完成 OCR；这里只补查完全没有定位到二维码的图片。
            if image.get("candidates"):
                continue
            try:
                ocr_items = self._read_ocr(resolve_source(image))
                if not isinstance(ocr_items, list):
                    continue
                full_text = " ".join(
                    item.get("text", "") for item in ocr_items
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
            except Exception:
                continue
            if _classify_local_purpose(full_text)[0] == "ordering":
                image["ordering_text_seen"] = True
                image["possible_missed_code"] = True
                found = True
        return found

    @staticmethod
    def _candidate_rank(item: tuple) -> tuple:
        """跨图片选优：先看证据强度，再看图片质量；同图多码歧义不进入这里。"""
        candidate = item[3]
        source = candidate.get("purpose_source")
        evidence = 2 if source in {"payload_url", "payload_domain"} else 1 if source in {"payload_url_ocr", "local_ocr"} else 0
        return (
            evidence,
            candidate.get("quality_score", candidate.get("short_edge_px", 0)),
            candidate.get("short_edge_px", 0),
            bool(candidate.get("payload_sha256")),
            item[2]["path"],
        )

    def _recommend_store(self, store: dict, resolve_source: Callable[[dict], Path]) -> tuple:
        """门店级决策树：同图歧义不选优，跨图明确点餐可选优，未知候选只低可信推荐。"""
        all_items = [
            (image_index, candidate_index, image, candidate)
            for image_index, image in enumerate(store["images"])
            for candidate_index, candidate in enumerate(image["candidates"])
        ]
        # 质量门禁：未通过结构/包含度校验的框不参与任何门店级决策，
        # 否则纹理误检或超大松框会把“无码门店”推进推荐或冲突分支。
        candidate_items = [item for item in all_items if item[3].get("quality") != "rejected"]
        # 有框但全部被质量门禁拒绝时直接交人工；不能落到后面的漏检/未知分支误报原因。
        if all_items and not candidate_items:
            return None, "none", "候选框均未通过二维码结构/包含度校验，需人工确认"
        # 同一图片出现两个及以上点餐候选时，整张图不参与自动选择；
        # 其他图片的独立明确候选不受影响。
        ordering_per_image: dict[int, int] = {}
        for image_index, _candidate_index, _image, candidate in candidate_items:
            if candidate.get("purpose") == "ordering":
                ordering_per_image[image_index] = ordering_per_image.get(image_index, 0) + 1
        ambiguous_images = {index for index, count in ordering_per_image.items() if count >= 2}
        selectable = [
            item for item in candidate_items
            if item[3].get("purpose") == "ordering" and item[0] not in ambiguous_images
        ]
        image_error = any(image.get("error") for image in store["images"])

        if selectable:
            # 图片异常时只允许“唯一明确点餐候选”直推；多个候选无法排除异常图里的干扰。
            if image_error and len(selectable) > 1:
                return None, "none", "部分图片读取或二维码检查失败，且存在多个明确点餐候选，需人工确认"
            picked = max(selectable, key=self._candidate_rank)
            candidate = picked[3]
            source = candidate.get("purpose_source")
            level = "high" if source in {"payload_url", "payload_domain"} else "ocr"
            if source in {"payload_url", "payload_domain"}:
                base = "二维码链接明确指向点餐"
            elif source == "payload_url_ocr":
                base = "餐饮点餐域名且周边 OCR 识别到点餐标识"
            else:
                base = "周边 OCR 识别到点餐标识"
            if candidate.get("payload_sha256"):
                reason = f"{base}且二维码本地解码成功；已按证据和图片质量选取，待微信验证"
            else:
                reason = f"{base}；二维码本地未解码，按点餐候选推荐，待微信验证"
            if image_error:
                reason += "；部分图片读取或检查失败，已记录异常"
            return picked, level, reason

        if ambiguous_images:
            return None, "none", "同一图片定位到多个点餐二维码，需人工确认"
        if any(item[3].get("purpose") == "conflict" for item in candidate_items):
            return None, "none", "存在用途证据冲突的二维码，需人工确认"
        if self._mark_missed_code_hints(store, resolve_source):
            return None, "none", "原图 OCR 发现点餐标识，但本地没有定位到对应二维码，需人工确认"
        if image_error:
            return None, "none", "至少一张原图读取或二维码检查失败，需人工复核"
        if any(
            "本地 OCR 未能完成" in (candidate.get("purpose_reason") or "")
            for image in store["images"] for candidate in image["candidates"]
        ):
            return None, "none", "本地 OCR 未能完成，需人工确认"

        unknown = [
            item for item in candidate_items
            if (item[3].get("purpose") or "unknown") == "unknown"
        ]
        # 唯一未知候选只在“图片正常、无冲突、无漏检线索”时低可信推荐，不能当作点餐码认定。
        if len(unknown) == 1:
            return unknown[0], "low", "店内仅剩一个用途未知二维码，图片正常且无冲突或漏检线索；按低可信候选推荐，待微信验证"
        if not candidate_items:
            return None, "none", "没有检测到可用二维码候选，需人工确认"
        if all(item[3].get("purpose") == "enterprise_wechat" for item in candidate_items):
            return None, "none", "仅识别到企业微信码，需人工确认"
        if unknown:
            return None, "none", "存在多个用途未知二维码，需人工确认"
        return None, "none", "仅识别到非点餐二维码，需人工确认"

    @staticmethod
    def _reset_store_selection(store: dict, analysis_dir: Path) -> None:
        for image in store["images"]:
            for candidate in image["candidates"]:
                candidate["selected"] = False
        store["recommendation_level"] = "none"
        store["wechat_validation_status"] = "not_applicable"
        store["selected_source"] = None
        old_artifact = store.get("selected_artifact")
        if old_artifact:
            (analysis_dir / old_artifact).unlink(missing_ok=True)
        store["selected_artifact"] = None

    def active_release(self, active_root: Path, *, project_id: str | None = None) -> dict | None:
        if self.role == "hub":
            release = self.versions.active_project(project_id) if project_id is not None else None
        else:
            release = self.versions.active_project(project_id or LEGACY_ID)
        if release:
            try:
                batch = self._batch_row(release["batch_id"])
            except ValueError:
                # 批次在两次读取之间进入回收站时，不再展示其发布状态。
                return None
            release["workflow"] = self.workflow_stage(batch)
        return release

    def _version_summary(self, batch: dict) -> dict:
        project_id = batch.get("project_id")
        root = self.published_root if project_id is not None else Path(self.configs.load().input_dir).expanduser().resolve()
        versions = self.versions.list(batch["id"], root, include_deleted=bool(batch.get("deleted_at")),
                                      project_id=project_id)
        changes = self.versions.changes(batch["id"], versions[0]["snapshot_id"] if versions else "")
        return {
            "name": self.versions.batch_name(batch), "release": versions[0] if versions else None,
            "releases": versions, "suggested_version": self.versions.suggested(batch["id"]),
            "has_changes": bool(sum(changes.values())), "draft_changes": changes,
        }

    @property
    def batch_management(self):
        """管理器按需创建，避免素材服务与管理服务互相导入。"""
        return self._batch_management

    @staticmethod
    def _batch_metadata(name: str | None, description: str) -> tuple[str, str]:
        if name is None:
            name = "清洗 " + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("请填写批次名称（最多 80 字）")
        if not isinstance(description, str) or len(description.strip()) > 500:
            raise ValueError("批次描述最多 500 字")
        return name.strip(), description.strip()

    def project_for_new_batch(self, db, project_id: str | None) -> str:
        """Hub 新建批次必须指定项目；旧本机模式仅兼容固定 legacy 项目。"""
        if project_id is None:
            if self.role == "hub":
                raise ProjectConflict("新建批次必须指定项目")
            project_id = LEGACY_ID
        assert_writable(db, project_id)
        return project_id

    @staticmethod
    def _assert_batch_write(db, batch_id: str, project_id: str | None = None) -> str:
        owner = resolve_batch_project(db, batch_id)
        if project_id is not None and owner["id"] != project_id:
            raise ProjectConflict("素材批次不属于当前项目")
        assert_writable(db, owner["id"])
        return owner["id"]

    def _check_batch_scope(self, batch_id: str, project_id: str | None = None, *, write: bool = False,
                           include_deleted: bool = False) -> str:
        with self.task_store.connect() as db:
            owner = resolve_batch_project(db, batch_id, include_deleted=include_deleted)
            if self.role == "hub" and project_id is None:
                raise ProjectConflict("项目范围缺失")
            if project_id is not None and owner["id"] != project_id:
                raise ProjectConflict("素材批次不属于当前项目")
            if write:
                if self.role == "hub" and project_id is None:
                    raise ProjectConflict("批次写入必须指定项目")
                assert_writable(db, owner["id"])
            return owner["id"]

    def _write_project(self, db, batch_id: str, project_id: str | None) -> str:
        owner = resolve_batch_project(db, batch_id)
        if project_id is None:
            if self.role == "hub":
                raise ProjectConflict("批次写入必须指定项目")
            project_id = owner["id"]
        if project_id != owner["id"]:
            raise ProjectConflict("素材批次不属于当前项目")
        assert_writable(db, project_id)
        return project_id

    def list_batches(self, include_deleted: bool = False, *, project_id: str | None = None) -> list[dict]:
        with self.task_store.connect() as db:
            project_id = project_id or LEGACY_ID
            if db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                return []
            filters = ["project_id=?"]
            params = [project_id]
            if not include_deleted:
                filters.append("deleted_at=''")
            predicate = "WHERE " + " AND ".join(filters) if filters else ""
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM material_batches {predicate} ORDER BY created_at DESC,id DESC", params)]
        return [self._batch_summary(row) for row in rows]

    def batch_options(self, *, project_id: str | None = None) -> list[dict]:
        """批次下拉框只读名称和阶段，避免加载识别结果、发布差异及历史运行。"""
        with self.task_store.connect() as db:
            project_id = project_id or LEGACY_ID
            if db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                return []
            rows = [dict(row) for row in db.execute(
                "SELECT b.id,b.name,b.created_at,b.capture_rounds,b.manual_stage,b.approved_at,v.snapshot_id "
                "FROM material_batches b LEFT JOIN material_versions v ON v.batch_id=b.id "
                "AND v.id=(SELECT version_id FROM project_material_selection WHERE project_id=?) "
                "WHERE b.deleted_at='' AND b.project_id=? ORDER BY b.created_at DESC,b.id DESC",
                (project_id, project_id))]
        selected = next((row for row in rows if row["snapshot_id"]), None)
        active_id = None
        if selected:
            active_id = selected["id"]
        return [{"batch_id": row["id"], "name": self.versions.batch_name(row),
                 "created_at": row["created_at"], "workflow": self.workflow_stage(row),
                 "current_active": row["id"] == active_id} for row in rows]

    # 批次执行阶段：事实源是采集轮次和人工通过标记，避免再维护一套会与运行记录打架的状态机。
    _WORKFLOW_LABELS = {
        "not_started": "未采集",
        "first_run": "首轮已跑",
        "rerun": "已重跑",
        "approved": "人工审核通过（可建档）",
    }

    def workflow_stage(self, batch: dict) -> dict:
        rounds = int(batch.get("capture_rounds") or 0)
        manual = str(batch.get("manual_stage") or "")
        if manual in self._WORKFLOW_LABELS:
            stage, source = manual, "manual"
        elif str(batch.get("approved_at") or ""):
            # 兼容旧数据：approved_at 曾经是唯一的人工标记。
            stage, source = "approved", "manual"
        elif rounds >= 2:
            stage, source = "rerun", "auto"
        elif rounds == 1:
            stage, source = "first_run", "auto"
        else:
            stage, source = "not_started", "auto"
        return {"stage": stage, "label": self._WORKFLOW_LABELS[stage],
                "capture_rounds": rounds, "source": source, "manual_stage": manual}

    def set_stage(self, batch_id: str, stage: str, *, project_id: str | None = None) -> dict:
        """人工指定批次阶段；传空字符串恢复自动判断。"""
        self._check_batch_scope(batch_id, project_id, write=True)
        stage = str(stage or "").strip()
        if stage and stage not in self._WORKFLOW_LABELS:
            raise ValueError("批次阶段无效")
        self._batch_row(batch_id)
        with self.task_store.connect() as db:
            db.execute(
                "UPDATE material_batches SET manual_stage=?,approved_at=?,updated_at=? "
                "WHERE id=? AND deleted_at=''",
                (stage, _now() if stage == "approved" else "", _now(), batch_id),
            )
        return self.get_batch(batch_id, project_id=project_id)

    def register_capture_round(self, task_ids: list[str], *, project_id: str | None = None) -> list[str]:
        """采集启动时按任务归属批次累加轮次；同一批次一次启动只加一次。"""
        if self.role == "hub" and project_id is None:
            raise ProjectConflict("项目范围缺失")
        if project_id is not None:
            allowed = {row["id"] for row in self.effective_capture_tasks(project_id=project_id)}
            if not set(task_ids).issubset(allowed):
                raise ProjectConflict("存在不属于当前项目的采集任务")
        batch_ids: set[str] = set()
        for task_id in task_ids:
            task = self.task_store.get(task_id)
            snapshot = Path(task["source_path"]).expanduser().resolve().parent.parent
            binding = self.task_store.material_binding(str(snapshot))
            if binding and binding.get("batch_id"):
                if project_id is not None:
                    self._check_batch_scope(binding["batch_id"], project_id, write=True)
                batch_ids.add(binding["batch_id"])
        if not batch_ids:
            return []
        stamp = _now()
        with self.task_store.connect() as db:
            for batch_id in batch_ids:
                db.execute(
                    "UPDATE material_batches SET capture_rounds=capture_rounds+1,updated_at=? "
                    "WHERE id=? AND deleted_at=''", (stamp, batch_id),
                )
                # 已通过的批次重新采集后必须回到待审核，等新结果复核。
                db.execute(
                    "UPDATE material_batches SET manual_stage='',approved_at='' "
                    "WHERE id=? AND (manual_stage='approved' OR approved_at<>'')", (batch_id,),
                )
        return sorted(batch_ids)

    def _latest_versions(self, *, project_id: str | None = None) -> dict[str, dict]:
        """显式项目范围用于业务查询；未指定时保留全局同步视图。"""
        with self.task_store.connect() as db:
            sql = ("SELECT b.id,b.name,b.created_at,b.project_id,COALESCE(p.code,'') AS project_code,"
                   "COALESCE(p.name,'') AS project_name FROM material_batches b "
                   "LEFT JOIN projects p ON p.id=b.project_id WHERE b.deleted_at=''")
            params = ()
            if project_id is not None:
                get_project(db, project_id)
                sql += " AND b.project_id=?"
                params = (project_id,)
            batches = {row["id"]: dict(row) for row in db.execute(sql + " ORDER BY b.created_at,b.id", params)}
            latest: dict[str, dict] = {}
            for row in db.execute("SELECT * FROM material_versions ORDER BY rowid").fetchall():
                batch = batches.get(row["batch_id"])
                if batch is None:
                    continue
                latest[row["batch_id"]] = {"version": dict(row), "batch": batch}
        return latest

    def latest_snapshot_map(self, *, project_id: str | None = None) -> dict[str, str]:
        """每个未删除批次的最新发布快照；同步协调必须使用全局视图。"""
        return {batch_id: item["version"]["snapshot_id"]
                for batch_id, item in self._latest_versions(project_id=project_id).items()}

    def effective_capture_tasks(self, *, project_id: str | None = None) -> list[dict]:
        """有门店发布记录的批次按门店选取任务，旧快照按原有方式兼容。"""
        latest = self._latest_versions(project_id=project_id)
        publications, blocked = self.store_versions.current({item["version"]["id"] for item in latest.values()})
        tasks = self.task_store.tasks()
        by_id = {task["id"]: task for task in tasks if not task["archived_at"]}
        by_snapshot: dict[str, list[dict]] = {}
        for task in by_id.values():
            by_snapshot.setdefault(Path(task["source_path"]).parent.parent.name, []).append(task)
        result: list[dict] = []
        for batch_id, item in latest.items():
            release, batch = item["version"], item["batch"]
            raw = publications.get(release["id"])
            if raw is None:
                legacy_stores = {}
                marker = self.published_root / release["snapshot_id"] / ".materials-snapshot.json"
                if marker.is_file():
                    legacy_stores = {entry["source_name"]: entry["store_id"]
                                     for entry in json.loads(marker.read_text(encoding="utf-8"))["stores"]}
                raw = [{"task_id": task["id"], "store_id": legacy_stores.get(task["source_name"], ""), "revision": None}
                       for task in by_snapshot.get(release["snapshot_id"], [])]
            seen = set()
            for mapping in raw:
                task = by_id.get(mapping["task_id"])
                if not task or task["source_name"] in seen:
                    continue
                seen.add(task["source_name"])
                revision = mapping["revision"]
                awaiting_publish = (batch_id, mapping["store_id"]) in blocked
                state = {**task}
                if awaiting_publish:
                    state.update(status="blocked", stage="returned_to_materials", message="待重新选码并发布")
                result.append({
                    **state, "batch_id": batch_id, "batch_name": batch["name"] or batch_id,
                    "project_id": batch["project_id"] or "", "project_code": batch["project_code"],
                    "project_name": batch["project_name"],
                    "version": f"v{revision}" if revision else release["version"],
                    "material_version": revision, "batch_version": release["version"],
                    "store_id": mapping["store_id"],
                    "snapshot_id": Path(task["source_path"]).parent.parent.name,
                    "batch_snapshot_id": release["snapshot_id"],
                    "reject_label": REJECTION_REASONS.get(task.get("reject_reason", ""), ""),
                    "runnable": (not waiting_for_materials(state)
                                 and state["status"] in {"pending", "needs_review", "needs_retry", "error", "blocked"}),
                })
        return sorted(result, key=lambda row: (row["batch_name"], row["source_name"]))

    def rework_records(self, batch_ids: list[str] | None = None, *, project_id: str | None = None) -> list[dict]:
        """跨批次返工清单：待处理 / 不合格 / 已选码待发布，按原批次归属展示。"""
        wanted = set(batch_ids) if batch_ids else None
        latest = self._latest_versions(project_id=project_id)
        manifests: dict[str, dict[str, tuple]] = {}
        _, blocked = self.store_versions.current(set())
        with self.task_store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM material_decisions ORDER BY batch_id,source_folder,store_id")]
            names = {row["id"]: row["name"] for row in db.execute(
                "SELECT id,name FROM material_batches WHERE deleted_at=''" +
                (" AND project_id=?" if project_id is not None else ""),
                ((project_id,) if project_id is not None else ())) }
        records: list[dict] = []
        for row in rows:
            batch_id = row["batch_id"]
            if batch_id not in names or (wanted and batch_id not in wanted):
                continue
            decision = row["decision"]
            if decision == "confirmed":
                if batch_id not in manifests:
                    entries: dict[str, tuple] = {}
                    item = latest.get(batch_id)
                    if item:
                        marker = self.published_root / item["version"]["snapshot_id"] / ".materials-snapshot.json"
                        if marker.is_file():
                            manifest = json.loads(marker.read_text(encoding="utf-8"))
                            entries = {entry["store_id"]: (entry["source_hash"], entry.get("box"))
                                       for entry in manifest.get("stores", [])}
                    manifests[batch_id] = entries
                try:
                    current = (row["selected_source_hash"], json.loads(row["selected_json"] or "{}").get("box"))
                except ValueError:
                    current = (row["selected_source_hash"], None)
                # 与最新快照一致的门店没有待发布内容，不进返工表。
                if manifests[batch_id].get(row["store_id"]) == current and (batch_id, row["store_id"]) not in blocked:
                    continue
                category = "unpublished"
            else:
                category = {"pending": "pending", "skipped": "rejected"}.get(decision, "pending")
            code = row.get("reject_reason", "")
            version = (latest.get(batch_id) or {}).get("version") or {}
            records.append({
                "batch_id": batch_id, "batch_name": names[batch_id] or batch_id,
                "store_id": row["store_id"], "store_name": row["source_name"],
                "source_folder": row["source_folder"], "decision": decision, "category": category,
                "reason_code": code, "reason_label": REJECTION_REASONS.get(code, "") if code else "",
                "reason_detail": row.get("reject_detail", ""), "updated_at": row["updated_at"],
                "version": version.get("version", ""), "publishable": category == "unpublished",
            })
        return records

    def return_rework_store(self, batch_id: str, store_id: str, *, project_id: str | None = None) -> dict:
        """先留存旧决策和采集证据，再锁定本店，重新发布才解除。"""
        self._check_batch_scope(batch_id, project_id, write=True)
        with self._lock:
            batch = self._batch_row(batch_id)
            if batch["status"] != "completed":
                raise ValueError("分析未完成，不能打回门店")
            decision = self._decision_row(batch_id, store_id)
            task_ids = [row["id"] for row in self.effective_capture_tasks()
                        if row["batch_id"] == batch_id and row["store_id"] == store_id]
            with self.task_store.connect() as db:
                self.store_versions.return_store(db, decision, task_ids)
                db.execute("""
                    UPDATE material_decisions SET decision='pending',selected_json='{}',selected_source_hash='',
                        reject_reason='',reject_detail='',rejected_at='',reject_reviewed_at='',updated_at=?
                    WHERE batch_id=? AND store_id=?
                """, (_now(), batch_id, store_id))
        return self.get_store(batch_id, store_id, project_id=project_id)

    def set_approved(self, batch_id: str, approved: bool, *, project_id: str | None = None) -> dict:
        """兼容旧入口：审核通过/撤销等价于手工阶段 approved/自动。"""
        return self.set_stage(batch_id, "approved" if approved else "", project_id=project_id)

    def rejections(self, *, project_id: str | None = None) -> list[dict]:
        """返回仍处于不合格状态的清洗门店，直接以人工审核决策为事实源。"""
        if self.role == "hub" and project_id is None:
            raise ProjectConflict("项目范围缺失")
        project_id = project_id or LEGACY_ID
        with self.task_store.connect() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT d.*, b.name AS batch_name
                FROM material_decisions d
                JOIN material_batches b ON b.id=d.batch_id
                WHERE d.decision='skipped' AND b.deleted_at='' AND b.project_id=?
                ORDER BY d.updated_at DESC,d.source_folder
            """, (project_id,))]
        result = []
        for row in rows:
            store = json.loads(row["store_json"])
            image_names = [
                image.get("display_name") or Path(image.get("path", "")).name
                for image in store.get("images", [])
                if isinstance(image, dict) and image.get("path")
            ]
            code = row.get("reject_reason", "")
            result.append({
                "source_type": "materials",
                "source_label": "素材清洗",
                "record_id": f"{row['batch_id']}:{row['store_id']}",
                "store_id": row["store_id"],
                "store_key": row["source_folder"],
                "store_name": row["source_name"],
                "source_folder": row["source_folder"],
                "source_item": image_names[0] if len(image_names) == 1 else f"{len(image_names)} 张原图",
                "reason_code": code,
                "reason_label": REJECTION_REASONS.get(code, "未记录原因"),
                "detail": row.get("reject_detail", ""),
                "evidence": "",
                "marked_at": row.get("rejected_at") or row["updated_at"],
                "reviewed_at": row.get("reject_reviewed_at", ""),
                "status": "不合格",
                "stage": "素材清洗",
                "batch_id": row["batch_id"],
                "batch_name": row.get("batch_name", ""),
                "task_id": "",
                "run_id": "",
            })
        return result

    def get_batch(self, batch_id: str, *, project_id: str | None = None) -> dict:
        if self.role == "hub" or project_id is not None:
            self._check_batch_scope(batch_id, project_id)
        row = self._batch_row(batch_id)
        with self.task_store.connect() as db:
            stores = [dict(item) for item in db.execute("SELECT * FROM material_decisions WHERE batch_id=? ORDER BY source_folder,store_id", (batch_id,))]
        summary = self._summary_counts(row, stores)
        job = self.jobs.latest(batch_id)
        progress = {"done": row["progress_done"], "total": row["progress_total"]}
        if job is not None and job["status"] in OPEN_STATUSES:
            # 运行期间以 job 的门店计数为准，批次行只做结束后的镜像，避免两套进度互相覆盖。
            counts = job.get("counts") or {}
            progress = {"done": counts.get("done", 0) + counts.get("failed", 0),
                        "total": counts.get("total", 0)}
        return {
            "batch_id": batch_id,
            "source_kind": row["source_kind"],
            "source_root": row["source_root"],
            "status": row["status"],
            "progress": progress,
            "summary": summary,
            "error": row["error"],
            "snapshot_id": row["snapshot_id"] or None,
            "job": job,
            "workflow": self.workflow_stage(row),
            **self._version_summary(row),
            "stores": [self._store_summary(item) for item in stores],
        }

    def get_store(self, batch_id: str, store_id: str, *, project_id: str | None = None) -> dict:
        if self.role == "hub" or project_id is not None:
            self._check_batch_scope(batch_id, project_id)
        row = self._decision_row(batch_id, store_id)
        data = json.loads(row["store_json"])
        summary = self._store_summary(row)
        selected = json.loads(row["selected_json"])
        if not selected:
            selected = None
        images = []
        for image_index, image in enumerate(data["images"]):
            candidates = []
            for candidate_index, candidate in enumerate(image["candidates"]):
                candidates.append({
                    **candidate,
                    "candidate_index": candidate_index,
                    "recommendation": bool(candidate.get("selected")),
                })
            images.append({**image, "image_index": image_index, "candidates": candidates})
        return {
            "batch_id": batch_id,
            "store_id": store_id,
            "source_name": row["source_name"],
            "source_folder": row["source_folder"],
            **self.store_versions.state(batch_id, store_id),
            "analysis_status": summary["analysis_status"],
            "reason": summary["reason"],
            "decision": row["decision"],
            "rejection": self.rejection(row),
            "recommendation": summary["recommendation"],
            "recommendation_level": summary["recommendation_level"],
            "wechat_validation_status": summary["wechat_validation_status"],
            "selected": selected,
            "images": images,
        }

    def preview_original(self, batch_id: str, store_id: str, image_index: int, *, project_id: str | None = None) -> dict:
        if self.role == "hub" or project_id is not None:
            self._check_batch_scope(batch_id, project_id)
        batch = self._batch_row(batch_id)
        store_row = self._decision_row(batch_id, store_id)
        store = json.loads(store_row["store_json"])
        image = self._image_record(store, image_index)
        path = self._checked_source(batch, image)
        return {"bytes": path.read_bytes(), "media_type": self._media_type(path), "filename": path.name}

    def preview_candidate(self, batch_id: str, store_id: str, image_index: int, candidate_index: int,
                          *, project_id: str | None = None) -> dict:
        if self.role == "hub" or project_id is not None:
            self._check_batch_scope(batch_id, project_id)
        batch = self._batch_row(batch_id)
        store_row = self._decision_row(batch_id, store_id)
        store = json.loads(store_row["store_json"])
        image = self._image_record(store, image_index)
        candidate = self._candidate_record(image, candidate_index)
        source = self._checked_source(batch, image)
        buffer = io.BytesIO()
        with Image.open(source) as original:
            orientation = int(original.getexif().get(274, 1) or 1)
            box = candidate["box"]
            left, top, right, bottom = self._crop_bounds(original.size, box)
            crop = original.crop((left, top, right, bottom))
            crop = _apply_orientation(crop, orientation)
            if crop.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
                crop = crop.convert("RGB")
            crop.save(buffer, format="PNG")
        return {"bytes": buffer.getvalue(), "media_type": "image/png", "filename": "candidate.png"}

    def decide_store(
        self,
        batch_id: str,
        store_id: str,
        decision: str,
        image_index: int | None = None,
        candidate_index: int | None = None,
        box: dict | None = None,
        reject_reason: str | None = None,
        reject_detail: str = "",
        *, project_id: str | None = None,
    ) -> dict:
        self._check_batch_scope(batch_id, project_id, write=True)
        if not isinstance(decision, str) or decision not in {"confirm", "skip", "pending"}:
            raise ValueError("decision 只能是 confirm/skip/pending")
        if not isinstance(reject_detail, str) or len(reject_detail.strip()) > 200:
            raise ValueError("不合格说明最多 200 字")
        if reject_reason is not None:
            if decision != "skip" or not isinstance(reject_reason, str) or reject_reason not in REJECTION_REASONS:
                raise ValueError("请选择有效的不合格原因")
            if reject_reason == "other" and not reject_detail.strip():
                raise ValueError("请填写其他原因")
        with self._lock:
            batch = self._batch_row(batch_id)
            if batch["status"] != "completed":
                raise ValueError("分析未完成，不能修改店铺决策")
            row = self._decision_row(batch_id, store_id)
            store = json.loads(row["store_json"])
            # 旧接口没有原因字段，保留旧记录；撤销或重新确认时清除不合格原因。
            reason_code = (reject_reason if reject_reason is not None else row["reject_reason"]) if decision == "skip" else ""
            reason_detail = (reject_detail.strip() if reject_reason is not None else row["reject_detail"]) if decision == "skip" else ""
            selected, source_hash = {}, ""
            if decision == "confirm":
                self._verify_store_hashes(batch, store)
                if image_index is None:
                    raise ValueError("确认候选时必须指定 image_index")
                image = self._image_record(store, image_index)
                source = self._checked_source(batch, image)
                if candidate_index is None:
                    if box is None:
                        raise ValueError("手选二维码框必须提供 box")
                    # 浏览器按 EXIF 方向显示原图，人工框选坐标是显示方向；先换算回原图像素坐标再校验。
                    normalized_box = self._display_box_to_source(box, source)
                    with Image.open(source) as original:
                        normalized_box = self._validate_box(normalized_box, original.size)
                    # 人工框选以框本身为最终裁剪范围，不再自动扩边。
                    crop_margin_ratio = 0.0
                else:
                    candidate = self._candidate_record(image, candidate_index)
                    if box is not None:
                        raise ValueError("选择现有候选时不允许覆盖其 box")
                    normalized_box = candidate["box"]
                    crop_margin_ratio = CROP_MARGIN_RATIO
                selected = {"image_index": image_index, "candidate_index": candidate_index,
                            "box": normalized_box, "crop_margin_ratio": crop_margin_ratio,
                            "box_space": "source"}
                source_hash = image["sha256"]
            rejected_at = _now() if decision == "skip" else ""
            if decision == "skip" and row["decision"] == "skipped" and not reject_reason:
                rejected_at = row.get("rejected_at", "") or _now()
            # 重新选择不合格原因等于一次新的结论，此前的人工确认标记必须清空。
            reviewed_at = row.get("reject_reviewed_at", "") if decision == "skip" and not reject_reason else ""
            with self.task_store.connect() as db:
                db.execute(
                    "UPDATE material_decisions SET decision=?,selected_json=?,selected_source_hash=?,reject_reason=?,reject_detail=?,rejected_at=?,reject_reviewed_at=?,updated_at=? WHERE batch_id=? AND store_id=?",
                    ("confirmed" if decision == "confirm" else "skipped" if decision == "skip" else "pending",
                     json.dumps(selected, ensure_ascii=False), source_hash, reason_code, reason_detail, rejected_at,
                     reviewed_at, _now(), batch_id, store_id),
                )
        return self.get_store(batch_id, store_id, project_id=project_id)

    def mark_rejection_reviewed(self, batch_id: str, store_id: str, reviewed: bool,
                                *, project_id: str | None = None) -> dict:
        """人工复核不合格门店：标记/取消“确认确实不合格”，供清单与导出使用。"""
        self._check_batch_scope(batch_id, project_id, write=True)
        with self.task_store.connect() as db:
            cursor = db.execute(
                "UPDATE material_decisions SET reject_reviewed_at=?,updated_at=? "
                "WHERE batch_id=? AND store_id=? AND decision='skipped'",
                (_now() if reviewed else "", _now(), batch_id, store_id),
            )
            if not cursor.rowcount:
                raise ValueError("该门店当前没有不合格记录")
        return self.get_store(batch_id, store_id, project_id=project_id)

    def accept_recommendations(self, batch_id: str, store_ids: list[str] | None = None,
                               *, project_id: str | None = None) -> dict:
        self._check_batch_scope(batch_id, project_id, write=True)
        with self._lock:
            batch = self._batch_row(batch_id)
            if batch["status"] != "completed":
                raise ValueError("分析未完成，不能批量确认")
            with self.task_store.connect() as db:
                rows = [dict(row) for row in db.execute(
                    "SELECT * FROM material_decisions WHERE batch_id=? AND recommendation=1 AND decision='pending'",
                    (batch_id,),
                )]
            if store_ids is not None:
                requested = set(store_ids)
                known = {row["store_id"] for row in rows}
                if not requested <= known:
                    raise ValueError("批量确认列表含有未推荐或未知店铺")
                rows = [row for row in rows if row["store_id"] in requested]
            confirmations = []
            for row in rows:
                store = json.loads(row["store_json"])
                self._verify_store_hashes(batch, store)
                match = next((
                    (image_index, candidate_index, candidate)
                    for image_index, image in enumerate(store["images"])
                    for candidate_index, candidate in enumerate(image["candidates"])
                    if candidate.get("selected")
                ), None)
                if match is None:
                    continue
                image_index, candidate_index, candidate = match
                image = self._image_record(store, image_index)
                confirmations.append((row["store_id"], image["sha256"], {
                    "image_index": image_index, "candidate_index": candidate_index, "box": candidate["box"],
                }))
            with self.task_store.connect() as db:
                for store_id, source_hash, selected in confirmations:
                    db.execute(
                        "UPDATE material_decisions SET decision='confirmed',selected_json=?,selected_source_hash=?,updated_at=? WHERE batch_id=? AND store_id=? AND decision='pending'",
                        (json.dumps(selected, ensure_ascii=False), source_hash, _now(), batch_id, store_id),
                    )
            return {"confirmed": len(confirmations)}

    def publish_snapshot(self, batch_id: str, description: str = "", *, name: str | None = None,
                         version: str | None = None, activate: bool = True,
                         store_ids: list[str] | None = None, project_id: str | None = None) -> dict:
        if not isinstance(description, str) or len(description.strip()) > 200:
            raise ValueError("发布说明最多 200 字")
        description = description.strip()
        with self._publish_lock, self._lock:
            batch = self._batch_row(batch_id)
            if batch["status"] != "completed":
                raise ValueError("只有分析完成的批次可以发布")
            with self.task_store.connect() as db:
                project_id = self._write_project(db, batch_id, project_id)
                rows = [dict(row) for row in db.execute("SELECT * FROM material_decisions WHERE batch_id=? AND decision='confirmed' ORDER BY source_folder,store_id", (batch_id,))]
                decision_names = {row["store_id"]: row["source_name"] for row in db.execute(
                    "SELECT store_id,source_name FROM material_decisions WHERE batch_id=?", (batch_id,))}
            selected_ids = None if store_ids is None else list(dict.fromkeys(store_ids))
            if selected_ids is not None:
                by_id = {row["store_id"]: row for row in rows}
                unconfirmed = [store_id for store_id in selected_ids if store_id not in by_id]
                if unconfirmed:
                    sample = "、".join(decision_names.get(store_id, store_id) for store_id in unconfirmed[:5])
                    more = f" 等 {len(unconfirmed)} 家" if len(unconfirmed) > 5 else ""
                    raise ValueError(f"以下门店尚未确认二维码：{sample}{more}")
                rows = [by_id[store_id] for store_id in selected_ids]
            if not rows:
                raise ValueError("没有已人工确认的店铺；未发布任何素材")
            stores = [(row, json.loads(row["store_json"]), json.loads(row["selected_json"])) for row in rows]
            # 已确认却没有候选坐标属于历史脏数据：直接列出门店名，便于撤销确认后重分析。
            missing = [row["source_name"] for row, _store, selected in stores if not selected]
            if missing:
                sample = "、".join(missing[:5])
                more = f" 等 {len(missing)} 家" if len(missing) > 5 else ""
                raise ValueError(f"已确认但没有候选坐标：{sample}{more}；请撤销确认后重新分析")
            existing = self.versions.records(batch_id)
            # 增量发布：本次未勾选的门店沿用上一版快照条目，不重新裁剪也不丢门店。
            previous_entries: list[dict] = []
            if selected_ids is not None and existing:
                marker = self.published_root / existing[0]["snapshot_id"] / ".materials-snapshot.json"
                if marker.is_file():
                    manifest = json.loads(marker.read_text(encoding="utf-8"))
                    previous_entries = [entry for entry in manifest.get("stores", [])
                                        if entry.get("store_id") not in set(selected_ids)]
            names: dict[str, str] = {}
            for entry in previous_entries:
                names[entry["source_name"]] = entry["source_folder"]
            selected_items = []
            for row, store, selected in stores:
                if row["source_name"] in names and names[row["source_name"]] != row["source_folder"]:
                    raise ValueError("已确认店铺中存在不同父路径的同名目录，拒绝发布")
                names[row["source_name"]] = row["source_folder"]
                image = self._image_record(store, selected["image_index"])
                if image["sha256"] != row["selected_source_hash"]:
                    raise ValueError("确认后原图 hash 已变化，拒绝发布")
                source = self._checked_source(batch, image)
                if _sha256(source) != row["selected_source_hash"]:
                    raise ValueError("确认后原图 hash 已变化，拒绝发布")
                self._verify_store_hashes(batch, store)
                with Image.open(source) as original:
                    box = self._validate_box(self._source_box(selected, source), original.size)
                selected_items.append({
                    "store_id": row["store_id"], "source_folder": row["source_folder"],
                    "source_name": row["source_name"], "source_hash": row["selected_source_hash"],
                    "image_index": selected["image_index"], "candidate_index": selected["candidate_index"],
                    "box": box, "crop_margin_ratio": _selected_margin_ratio(selected),
                })
            fingerprint_items = selected_items + previous_entries
            fingerprint_items.sort(key=lambda item: (item.get("source_folder", ""), item.get("store_id", "")))
            content = json.dumps(fingerprint_items, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            # 对相同版本的重试必须先验证内容，防止覆盖已经发布的版本。
            legacy_fingerprint = hashlib.sha256(content).hexdigest()
            # 同内容在不同批次内仍需不同任务。已发布的本批次快照继续沿用旧 ID。
            if any(row["snapshot_id"] == legacy_fingerprint for row in existing):
                fingerprint = legacy_fingerprint
            else:
                fingerprint = hashlib.sha256(batch_id.encode() + b"\0" + content).hexdigest()
            if existing:
                previous_root = self.published_root / existing[0]["snapshot_id"]
                marker = previous_root / ".materials-snapshot.json"
                last_stores = json.loads(marker.read_text(encoding="utf-8"))["stores"]
                if last_stores == fingerprint_items:
                    fingerprint = existing[0]["snapshot_id"]
                elif any(row["snapshot_id"] == fingerprint for row in existing):
                    # 内容回到旧图时，新发布仍有独立身份，旧采集结果不重新成为有效结果。
                    fingerprint = hashlib.sha256(f"{fingerprint}:{existing[0]['snapshot_id']}".encode()).hexdigest()
            if version is None:
                previous_version = next((row for row in existing if row["snapshot_id"] == fingerprint), None)
                version = previous_version["version"] if previous_version else self.versions.suggested(batch_id)
                if previous_version:
                    description = previous_version["description"]
            name = name if name is not None else self.versions.batch_name(batch)
            if version != "历史版本":
                self.versions.validate(name, version, description)
            conflict = self.versions.find(batch_id, version)
            if conflict and (conflict["snapshot_id"] != fingerprint or conflict["description"] != description):
                raise ValueError("该版本已经发布，请填写新的版本号")
            snapshot = self.published_root / fingerprint
            already_published = snapshot.exists()
            expected_artifact_paths = {f"{entry['source_name']}/二维码.png" for entry in fingerprint_items}
            if already_published:
                self._validate_snapshot_marker(snapshot, fingerprint, expected_artifact_paths)
            else:
                # 临时目录用短名：fingerprint（64 字符）叠加超长门店名会让 Windows
                # 路径超过 260 字符，mkdir 直接抛 WinError 206；发布完成后再重命名。
                temporary = self.published_root / (".tmp-" + uuid.uuid4().hex[:12])
                temporary.mkdir(mode=0o700)
                try:
                    artifact_hashes = []
                    for row, _store, selected in stores:
                        image = self._image_record(_store, selected["image_index"])
                        source = self._checked_source(batch, image)
                        artifact = temporary / row["source_name"] / "二维码.png"
                        _mkdir_ok(artifact.parent)
                        _crop_candidate(source, self._source_box(selected, source), artifact, _selected_margin_ratio(selected))
                        artifact_hashes.append({"path": artifact.relative_to(temporary).as_posix(), "sha256": _sha256(artifact)})
                    if previous_entries:
                        previous_root = self.published_root / existing[0]["snapshot_id"]
                        for entry in previous_entries:
                            artifact = temporary / entry["source_name"] / "二维码.png"
                            _mkdir_ok(artifact.parent)
                            shutil.copyfile(previous_root / entry["source_name"] / "二维码.png", artifact)
                            artifact_hashes.append({"path": artifact.relative_to(temporary).as_posix(), "sha256": _sha256(artifact)})
                    atomic_json(temporary / ".materials-snapshot.json", {"snapshot_id": fingerprint, "stores": fingerprint_items, "artifacts": artifact_hashes})
                    temporary.rename(snapshot)
                except Exception:
                    shutil.rmtree(temporary, ignore_errors=True)
                    raise

            self._validate_snapshot_marker(snapshot, fingerprint, expected_artifact_paths)

            previous = self.configs.load().model_dump() if self.role != "hub" else None
            try:
                if previous is not None and activate:
                    self.configs.save({**previous, "input_dir": str(snapshot)})
                reuse_roots = [self.published_root / row["snapshot_id"] for row in existing]
                task_scan = self.task_store.scan(snapshot, reuse_sources=reuse_roots)
                store_records = self.store_versions.prepare(batch_id, snapshot, fingerprint_items,
                                                            existing[0] if existing else None)
                with self.task_store.connect() as db:
                    if project_id is not None:
                        db.execute("BEGIN IMMEDIATE")
                    release_id = self.versions.remember(db, batch_id, name.strip(), version, fingerprint, description,
                                                        len(fingerprint_items), activate=activate, project_id=project_id)
                    store_updates = self.store_versions.remember(db, batch_id, release_id, store_records,
                                                                  {row["store_id"] for row in selected_items})
                    if any(row["changed"] or row["unblocked"] for row in store_updates):
                        db.execute("UPDATE material_batches SET manual_stage='',approved_at='' "
                                   "WHERE id=? AND (manual_stage='approved' OR approved_at<>'')", (batch_id,))
            except Exception:
                if previous is not None:
                    try:
                        self.configs.save(previous)
                    except Exception:
                        pass
                raise
            root = snapshot if self.role == "hub" else Path(self.configs.load().input_dir).expanduser().resolve()
            return {
                "snapshot_id": fingerprint,
                "release": next(item for item in self.versions.list(batch_id, root, project_id=project_id)
                                if item["version"] == version),
                "input_dir": str(snapshot),
                "task_scan": task_scan,
                "store_updates": store_updates,
                "already_published": already_published or task_scan.get("added", 0) == 0,
            }

    def activate_version(self, batch_id: str, version: str, *, project_id: str | None = None) -> dict:
        with self._publish_lock, self._lock:
            with self.task_store.connect() as db:
                project_id = self._write_project(db, batch_id, project_id)
            release = self.versions.find(batch_id, version)
            if release is None:
                raise ValueError("发布版本不存在")
            snapshot = self.published_root / release["snapshot_id"]
            manifest = json.loads((snapshot / ".materials-snapshot.json").read_text(encoding="utf-8"))
            self._validate_snapshot_marker(snapshot, release["snapshot_id"], {row["path"] for row in manifest["artifacts"]})
            previous = self.configs.load().model_dump() if self.role != "hub" else None
            try:
                if previous is not None:
                    self.configs.save({**previous, "input_dir": str(snapshot)})
                scan = self.task_store.scan(snapshot, reuse_sources=[self.published_root / row["snapshot_id"]
                                                                   for row in self.versions.records(batch_id)])
                with self.task_store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self._write_project(db, batch_id, project_id)
                    select_version(db, project_id, release["id"])
            except Exception:
                if previous is not None:
                    self.configs.save(previous)
                raise
            return {"release": self.active_release(snapshot, project_id=project_id), "task_scan": scan}

    def _insert_batch(self, batch_id: str, kind: str, source: Path, status: str, *, total: int = 0,
                      name: str, description: str, project_id: str | None = None) -> dict:
        source = source.resolve(strict=True)
        if self.material_root == source or self.material_root in source.parents or source in self.material_root.parents:
            raise ValueError("素材源不能与素材管理输出目录重叠")
        with self.task_store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            project_id = self.project_for_new_batch(db, project_id)
            stamp = _now()
            db.execute(
                "INSERT INTO material_batches(id,source_kind,source_root,status,progress_total,created_at,updated_at,name,description,project_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_id, kind, str(source), status, total, stamp, stamp, name, description, project_id),
            )
        return {"batch_id": batch_id, "status": status, "progress": {"done": 0, "total": total}}

    def _validate_local_root(self, path: Path) -> Path:
        source = path.expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ValueError("素材输入路径必须是文件夹")
        if self.material_root == source or self.material_root in source.parents or source in self.material_root.parents:
            raise ValueError("素材源不能与素材管理输出目录重叠")
        return source

    @staticmethod
    def _validate_local_limits(images: list[Path]) -> None:
        if len(images) > MAX_FILES:
            raise ValueError("单批文件数超过 10000 限制")
        total = 0
        for image in images:
            size = image.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError("单文件超过 50 MB 限制")
            total += size
            if total > MAX_BATCH_BYTES:
                raise ValueError("单批总大小超过 5 GB 限制")

    @staticmethod
    def _safe_upload_path(value: str) -> PurePosixPath:
        if not isinstance(value, str) or not value or "\x00" in value or len(value) > 2048:
            raise ValueError("上传相对路径无效")
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or normalized.startswith("//") or re.match(r"^[a-zA-Z]:", normalized):
            raise ValueError("上传路径不能是绝对路径")
        parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("上传路径不能包含空段、. 或 ..")
        rel = PurePosixPath(*parts)
        if rel.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError("只允许上传 jpg/jpeg/png/webp 图片")
        if any(len(part.encode("utf-8")) > 240 for part in rel.parts):
            raise ValueError("上传路径单段名称过长")
        return rel

    @staticmethod
    def _validate_uploaded_image(relative_path: PurePosixPath, content: bytes) -> None:
        try:
            with Image.open(io.BytesIO(content)) as image:
                actual_format = image.format
                if image.width <= 0 or image.height <= 0 or image.width * image.height > 100_000_000:
                    raise ValueError("图片尺寸无效或超过上限")
                image.verify()
        except Exception:
            raise ValueError("上传内容不是有效图片") from None
        expected = _FORMAT_BY_EXTENSION[relative_path.suffix.lower()]
        if actual_format != expected:
            raise ValueError("图片内容格式与文件扩展名不匹配")

    def _set_batch_error(self, batch_id: str, status: str, message: str) -> None:
        with self.task_store.connect() as db:
            db.execute("UPDATE material_batches SET status=?,error=?,updated_at=? WHERE id=?", (status, message, _now(), batch_id))

    def _batch_row(self, batch_id: str, *, include_deleted: bool = False) -> dict:
        with self.task_store.connect() as db:
            row = db.execute("SELECT * FROM material_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None or (row["deleted_at"] and not include_deleted):
            raise ValueError("素材批次不存在")
        return dict(row)

    def batch_name(self, batch_id: str) -> str:
        try:
            return str(self._batch_row(batch_id).get("name") or "")
        except ValueError:
            return ""

    def _decision_row(self, batch_id: str, store_id: str) -> dict:
        with self.task_store.connect() as db:
            row = db.execute("SELECT * FROM material_decisions WHERE batch_id=? AND store_id=?", (batch_id, store_id)).fetchone()
        if row is None:
            raise ValueError("素材店铺不存在")
        return dict(row)

    @staticmethod
    def _summary_counts(batch: dict, decisions: list[dict]) -> dict:
        try:
            summary = json.loads(batch["summary_json"])
        except (ValueError, TypeError):
            summary = {}
        counts = {
            "total_stores": summary.get("total_stores", len(decisions)),
            "total_images": summary.get("total_images", 0),
            "selected_stores": summary.get("selected_stores", sum(row["recommendation"] for row in decisions)),
            "review_stores": summary.get("review_stores", sum(not row["recommendation"] for row in decisions)),
            "confirmed_stores": sum(row["decision"] == "confirmed" for row in decisions),
            "skipped_stores": sum(row["decision"] == "skipped" for row in decisions),
            "pending_stores": sum(row["decision"] == "pending" for row in decisions),
        }
        if isinstance(summary.get("detection_stats"), dict):
            counts["detection_stats"] = summary["detection_stats"]
        return counts

    def _batch_summary(self, row: dict) -> dict:
        with self.task_store.connect() as db:
            decisions = [dict(item) for item in db.execute("SELECT * FROM material_decisions WHERE batch_id=?", (row["id"],))]
        return {
            "batch_id": row["id"], "source_kind": row["source_kind"], "status": row["status"],
            "progress": {"done": row["progress_done"], "total": row["progress_total"]},
            "summary": self._summary_counts(row, decisions), "error": row["error"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            **self.batch_management.summary(row),
            **self._version_summary(row),
        }

    def _store_summary(self, row: dict) -> dict:
        data = json.loads(row["store_json"])
        selected = json.loads(row["selected_json"])
        selected_candidates = [
            candidate for image in data.get("images", [])
            for candidate in image.get("candidates", []) if candidate.get("selected")
        ]
        valid_recommendation = bool(row["recommendation"]) and bool(selected_candidates)
        level = data.get("recommendation_level")
        if level not in _RECOMMENDATION_LEVELS:
            sources = [candidate.get("purpose_source") for candidate in selected_candidates]
            level = (
                "high" if any(source in {"payload_url", "payload_domain"} for source in sources)
                else "ocr" if sources else "none"
            )
        validation_status = data.get("wechat_validation_status")
        if validation_status not in _WECHAT_VALIDATION_STATUSES:
            validation_status = "unverified" if valid_recommendation else "not_applicable"
        return {
            "store_id": row["store_id"], "source_name": row["source_name"], "source_folder": row["source_folder"],
            "analysis_status": row["analysis_status"],
            "reason": data.get("reason", ""),
            "recommendation": valid_recommendation, "decision": row["decision"],
            "recommendation_level": level,
            "wechat_validation_status": validation_status,
            "rejection": self.rejection(row),
            "selected": selected or None,
        }

    @staticmethod
    def _image_record(store: dict, image_index: int) -> dict:
        if isinstance(image_index, bool) or not isinstance(image_index, int) or not 0 <= image_index < len(store["images"]):
            raise ValueError("image_index 超出范围")
        return store["images"][image_index]

    @staticmethod
    def _candidate_record(image: dict, candidate_index: int) -> dict:
        candidates = image.get("candidates", [])
        if isinstance(candidate_index, bool) or not isinstance(candidate_index, int) or not 0 <= candidate_index < len(candidates):
            raise ValueError("candidate_index 超出范围")
        return candidates[candidate_index]

    @staticmethod
    def _purpose_reason(purpose: str) -> str:
        return {
            "ordering": "本地 OCR 识别到点餐标识，尚未微信扫码验证",
            "not_ordering": "本地 OCR 识别到非点餐用途标识",
            "enterprise_wechat": "本地解码识别为企业微信用途",
            "conflict": "二维码用途证据冲突，需人工确认",
            "other": "本地 OCR 识别到非点餐用途标识",
            "unknown": "二维码周边没有明确点餐标识，需人工确认",
        }.get(purpose, "画面用途证据不足，需人工复核")

    def _checked_source(self, batch: dict, image: dict) -> Path:
        normalized = image["path"].replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized) or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("素材清单含有不安全相对路径")
        relative = Path(*normalized.split("/"))
        if relative.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError("素材清单中的路径不是支持的图片")
        if image.get("origin") == "supplement":
            if len(relative.parts) != 4 or relative.parts[0] != "__supplement__" or not re.fullmatch(r"[0-9a-f]{24}", relative.parts[1]) or not _UPLOAD_ID_RE.fullmatch(relative.parts[2]):
                raise ValueError("补充素材路径无效")
            if image.get("supplement_store_id") != relative.parts[1]:
                raise ValueError("补充素材不属于当前门店")
            batch_dir = self.batch_root / batch["id"]
            expected = batch_dir / "supplements" / relative.parts[1] / relative.parts[2] / relative.parts[3]
            path = expected
            if any(component.is_symlink() for component in (batch_dir, batch_dir / "supplements", batch_dir / "supplements" / relative.parts[1], batch_dir / "supplements" / relative.parts[1] / relative.parts[2], path)):
                raise ValueError("补充素材路径不安全")
            if not _inside(path, batch_dir) or not path.is_file():
                raise ValueError("补充素材文件不存在或不安全")
        else:
            source_root = Path(batch["source_root"]).resolve(strict=True)
            path = source_root / relative
            if path.is_symlink() or not _inside(path, source_root) or not path.is_file():
                raise ValueError("原图路径不存在或不安全")
        if _sha256(path) != image["sha256"]:
            raise ValueError("原图 hash 已变化，需重新分析后再操作")
        return path

    def _verify_store_hashes(self, batch: dict, store: dict) -> None:
        for image in store["images"]:
            self._checked_source(batch, image)

    @staticmethod
    def _display_box_to_source(box: dict, source: Path) -> dict:
        """浏览器按 EXIF 方向显示图片，人工框选坐标属于显示方向；这里换算回原图像素坐标。"""
        with Image.open(source) as image:
            width, height = image.size
            orientation = int(image.getexif().get(274, 1) or 1)
        x, y, w, h = (float(box[key]) for key in ("x", "y", "width", "height"))
        if orientation == 6:
            return {"x": y, "y": height - (x + w), "width": h, "height": w}
        if orientation == 8:
            return {"x": width - (y + h), "y": x, "width": h, "height": w}
        if orientation == 3:
            return {"x": width - (x + w), "y": height - (y + h), "width": w, "height": h}
        return {"x": x, "y": y, "width": w, "height": h}

    @classmethod
    def _source_box(cls, selected: dict, source: Path) -> dict:
        """兼容旧记录：新记录已标记 source；旧记录越界时按显示方向换算。"""
        box = selected["box"]
        if selected.get("box_space") == "source":
            return box
        with Image.open(source) as image:
            size = image.size
        try:
            return cls._validate_box(box, size)
        except ValueError:
            return cls._display_box_to_source(box, source)

    @staticmethod
    def _validate_box(box: dict, image_size: tuple[int, int]) -> dict:
        if not isinstance(box, dict):
            raise ValueError("二维码 box 必须是对象")
        values = [box.get(key) for key in ("x", "y", "width", "height")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise ValueError("二维码 box 坐标必须是有限数值")
        x, y, width, height = map(float, values)
        image_width, image_height = image_size
        # 前端 canvas 取整可能带来 1~2px 误差；容差内钳制到边界，避免合法框选被拦截。
        tolerance = 2.0
        if x < -tolerance or y < -tolerance or width <= 0 or height <= 0 or x + width > image_width + tolerance or y + height > image_height + tolerance:
            raise ValueError("二维码 box 超出原图边界")
        x = min(max(0.0, x), max(0.0, image_width - 1.0))
        y = min(max(0.0, y), max(0.0, image_height - 1.0))
        width = min(width, image_width - x)
        height = min(height, image_height - y)
        return {"x": x, "y": y, "width": width, "height": height}

    @staticmethod
    def _crop_bounds(image_size: tuple[int, int], box: dict,
                     margin_ratio: float = CROP_MARGIN_RATIO) -> tuple[int, int, int, int]:
        width, height = image_size
        x, y, qr_width, qr_height = (box[key] for key in ("x", "y", "width", "height"))
        return (
            max(0, math.floor(x - qr_width * margin_ratio)),
            max(0, math.floor(y - qr_height * margin_ratio)),
            min(width, math.ceil(x + qr_width * (1 + margin_ratio))),
            min(height, math.ceil(y + qr_height * (1 + margin_ratio))),
        )

    @staticmethod
    def _media_type(path: Path) -> str:
        return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[path.suffix.lower()]

    @classmethod
    def _sanitize_manifest(cls, manifest: dict) -> dict:
        if not isinstance(manifest, dict) or not isinstance(manifest.get("stores"), list):
            raise ValueError("manifest 格式无效")
        input_root = manifest.get("input_root")
        if not isinstance(input_root, str) or not input_root:
            raise ValueError("manifest 缺少 input_root")
        safe_stores, seen = [], set()
        total_images = 0
        for raw_store in manifest["stores"]:
            if not isinstance(raw_store, dict):
                raise ValueError("manifest store 格式无效")
            source_name = raw_store.get("source_name")
            source_folder = raw_store.get("source_folder")
            if not isinstance(source_name, str) or not source_name or Path(source_name).name != source_name or "/" in source_name or "\\" in source_name or source_name in {".", ".."}:
                raise ValueError("manifest source_name 无效")
            if not isinstance(source_folder, str) or not source_folder:
                selected_source = raw_store.get("selected_source") or ""
                source_folder = Path(selected_source).parent.as_posix() if selected_source else source_name
            normalized_folder = source_folder.replace("\\", "/")
            folder = PurePosixPath(normalized_folder)
            if folder.is_absolute() or re.match(r"^[a-zA-Z]:", normalized_folder) or ".." in folder.parts or ":" in normalized_folder:
                raise ValueError("manifest source_folder 路径无效")
            folder_key = folder.as_posix()
            if folder_key in seen:
                raise ValueError("manifest 中存在重复 source_folder")
            seen.add(folder_key)
            images = []
            raw_images = raw_store.get("images", [])
            if not isinstance(raw_images, list):
                raise ValueError("manifest images 格式无效")
            for raw_image in raw_images:
                if not isinstance(raw_image, dict):
                    raise ValueError("manifest image 格式无效")
                image_path = raw_image.get("path")
                digest = raw_image.get("sha256")
                normalized_image_path = image_path.replace("\\", "/") if isinstance(image_path, str) else ""
                if not isinstance(image_path, str) or normalized_image_path.startswith("/") or re.match(r"^[a-zA-Z]:", normalized_image_path) or any(part in {"", ".", ".."} for part in normalized_image_path.split("/")):
                    raise ValueError("manifest image path 无效")
                if Path(normalized_image_path).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    raise ValueError("manifest image 扩展名无效")
                if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
                    raise ValueError("manifest image hash 无效")
                candidates = []
                for candidate in raw_image.get("candidates", []):
                    if not isinstance(candidate, dict):
                        raise ValueError("manifest candidate 格式无效")
                    box = cls._validate_box(candidate.get("box"), (10**9, 10**9))
                    payload_hash = candidate.get("payload_sha256")
                    if payload_hash is not None and (not isinstance(payload_hash, str) or not _HASH_RE.fullmatch(payload_hash)):
                        raise ValueError("manifest payload hash 无效")
                    edge = candidate.get("short_edge_px")
                    if isinstance(edge, bool) or not isinstance(edge, int) or edge < 0:
                        raise ValueError("manifest 二维码尺寸无效")
                    raw_purpose = candidate.get("purpose")
                    raw_purpose = _LEGACY_PURPOSES.get(raw_purpose, raw_purpose)
                    purpose = raw_purpose if raw_purpose in _PURPOSES else "unknown"
                    purpose_source = candidate.get("purpose_source")
                    safe_reason = cls._purpose_reason(purpose)
                    if purpose_source == "local_ocr" and candidate.get("purpose_reason") in _LOCAL_OCR_REASON_LABELS:
                        safe_reason = candidate["purpose_reason"]
                    # 质量门禁原因也走受控标签，人工核对时能区分结构误检、偏移和松框。
                    if candidate.get("purpose_reason") in _QUALITY_REASON_LABELS:
                        safe_reason = candidate["purpose_reason"]
                    if purpose_source == "payload_url" and candidate.get("purpose_reason") in _PAYLOAD_URL_REASON_LABELS:
                        safe_reason = candidate["purpose_reason"]
                    if purpose_source == "payload_url_ocr" and candidate.get("purpose_reason") in _PAYLOAD_URL_OCR_REASON_LABELS:
                        safe_reason = candidate["purpose_reason"]
                    safe_candidate = {
                        "box": box, "payload_sha256": payload_hash, "short_edge_px": edge,
                        # 选中标记由本服务的推荐规则维护，重新分析时不能再按用途二次收紧。
                        "selected": bool(candidate.get("selected")), "purpose": purpose,
                        # OCR 只保存受控判断标签，不把整段图片文字写入清单。
                        "purpose_reason": ("本地解码识别为企业微信域名" if purpose == "enterprise_wechat" and purpose_source == "payload_domain" else safe_reason),
                        "source": (
                            "vision_fallback" if candidate.get("source") == "vision_fallback"
                            else "local_detector"
                        ),
                        "purpose_source": (
                            "payload_domain"
                            if purpose == "enterprise_wechat" and purpose_source == "payload_domain"
                            else "payload_url"
                            if purpose_source == "payload_url"
                            else "payload_url_ocr"
                            if purpose_source == "payload_url_ocr"
                            else "local_ocr"
                            if purpose_source == "local_ocr"
                            else "vision_model"
                        ),
                    }
                    if candidate.get("kind") == "wechat_mini_program":
                        safe_candidate["kind"] = "wechat_mini_program"
                    # 质量门禁字段：重新分析/交付清单时必须保留，否则被拒候选会重新变成可用。
                    quality = candidate.get("quality")
                    quality_reason = candidate.get("quality_reason")
                    if quality in {"ok", "rejected"}:
                        safe_candidate["quality"] = quality
                        if quality == "rejected" and quality_reason in _QUALITY_REASON_LABELS:
                            safe_candidate["quality_reason"] = quality_reason
                    if isinstance(candidate.get("qr_likeness"), bool):
                        safe_candidate["qr_likeness"] = candidate["qr_likeness"]
                    if isinstance(candidate.get("qr_contained"), bool):
                        safe_candidate["qr_contained"] = candidate["qr_contained"]
                    if isinstance(candidate.get("qr_centered"), bool):
                        safe_candidate["qr_centered"] = candidate["qr_centered"]
                    fill = candidate.get("qr_fill")
                    if isinstance(fill, (int, float)) and not isinstance(fill, bool) and math.isfinite(fill) and 0 <= fill <= 1:
                        safe_candidate["qr_fill"] = round(float(fill), 4)
                    if candidate.get("retry") is True:
                        safe_candidate["retry"] = True
                    for score_name in ("sharpness_score", "quality_score"):
                        score = candidate.get(score_name)
                        if score is not None:
                            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0:
                                raise ValueError(f"manifest {score_name} 无效")
                            safe_candidate[score_name] = float(score)
                    candidates.append(safe_candidate)
                images.append({
                    "path": normalized_image_path, "sha256": digest,
                    "detection_count": max(0, int(raw_image.get("detection_count", len(candidates)))),
                    "candidates": candidates,
                    **({"ordering_text_seen": True} if raw_image.get("ordering_text_seen") is True else {}),
                    **({"possible_missed_code": True} if raw_image.get("possible_missed_code") is True else {}),
                    **({"is_multi_qr_image": True} if raw_image.get("is_multi_qr_image") is True else {}),
                    **({"error": str(raw_image["error"])[:200]} if raw_image.get("error") else {}),
                })
                total_images += 1
            status = raw_store.get("status") if raw_store.get("status") in {"selected", "needs_review"} else "needs_review"
            has_recommendation = any(candidate["selected"] for image in images for candidate in image["candidates"])
            if status == "selected" and not has_recommendation:
                status = "needs_review"
            level = raw_store.get("recommendation_level")
            if level not in _RECOMMENDATION_LEVELS:
                # 旧清单没有等级字段：按选中候选的证据来源补算，避免历史数据被当成高可信。
                selected_sources = [
                    candidate.get("purpose_source")
                    for image in images for candidate in image["candidates"] if candidate["selected"]
                ]
                level = (
                    "high" if any(source in {"payload_url", "payload_domain"} for source in selected_sources)
                    else "ocr" if selected_sources else "none"
                )
            validation_status = raw_store.get("wechat_validation_status")
            if validation_status not in _WECHAT_VALIDATION_STATUSES:
                validation_status = "unverified" if has_recommendation else "not_applicable"
            safe_store = {
                "source_name": source_name, "source_folder": folder_key, "status": status,
                "reason": str(raw_store.get("reason", ""))[:300],
                "recommendation_level": level,
                "wechat_validation_status": validation_status,
                "selected_source": raw_store.get("selected_source") if has_recommendation and isinstance(raw_store.get("selected_source"), str) else None,
                "selected_artifact": raw_store.get("selected_artifact") if has_recommendation and isinstance(raw_store.get("selected_artifact"), str) else None,
                "images": images,
            }
            safe_stores.append(safe_store)
        summary = {
            "total_stores": len(safe_stores), "total_images": total_images,
            "selected_stores": sum(store["status"] == "selected" for store in safe_stores),
            "review_stores": sum(store["status"] != "selected" for store in safe_stores),
        }
        return {"input_root": input_root, "stores": safe_stores, "summary": summary}

    @staticmethod
    def _validate_snapshot_marker(snapshot: Path, fingerprint: str, expected_paths: set[str]) -> None:
        if not snapshot.is_dir() or snapshot.is_symlink():
            raise ValueError("已存在的快照路径不是安全目录")
        marker = snapshot / ".materials-snapshot.json"
        if marker.is_symlink():
            raise ValueError("快照发布标记不能是 symlink")
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("同名快照目录缺少有效发布标记，拒绝覆盖") from None
        if data.get("snapshot_id") != fingerprint:
            raise ValueError("已存在快照标记与当前决策不一致")
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("快照缺少图片 hash 清单，拒绝复用")
        actual_paths: set[str] = set()
        for item in artifacts:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str) or not _HASH_RE.fullmatch(item["sha256"]):
                raise ValueError("快照图片 hash 清单无效，拒绝复用")
            relative = PurePosixPath(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("快照图片路径无效，拒绝复用")
            path = snapshot.joinpath(*relative.parts)
            if path.is_symlink() or not _inside(path, snapshot) or not path.is_file() or _sha256(path) != item["sha256"]:
                raise ValueError("已发布快照图片缺失或 hash 已变化，拒绝复用")
            actual_paths.add(relative.as_posix())
        image_files = {
            path.relative_to(snapshot).as_posix()
            for path in snapshot.rglob("*")
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        }
        if any(path.is_symlink() for path in snapshot.rglob("*")):
            raise ValueError("快照目录含有 symlink，拒绝复用")
        if actual_paths != expected_paths or image_files != expected_paths:
            raise ValueError("已发布快照图片集合与已确认店铺不一致，拒绝复用")

