"""二维码素材清洗、人工决策和不可覆盖的输入快照管理。"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from PIL import Image

from .config import ConfigStore, atomic_json
from .preprocess import CROP_MARGIN_RATIO, SUPPORTED_EXTENSIONS, _apple_vision_inspector, _crop_candidate, _enumerate_images, _inspect_image, build_materials
from .image_tools import image_ocr
from .material_versions import MaterialVersions
from .store import TaskStore

REJECTION_REASONS = {
    "blur": "图片模糊 / 无法识别",
    "invalid_qr": "二维码无效",
    "store_mismatch": "二维码门店不一致",
    "store_closed": "门店已关店 / 停业",
    "miniapp_unavailable": "小程序到期或未绑定",
    "scan_order_unsupported": "门店不支持扫码点餐",
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
MAX_BATCH_BYTES = 2 * 1024 * 1024 * 1024
MAX_STORE_UPLOAD_FILES = 100
MAX_STORE_UPLOAD_BYTES = 500 * 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FORMAT_BY_EXTENSION = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (ValueError, OSError):
        return False


def _store_id(source_folder: str) -> str:
    return hashlib.sha256(source_folder.encode("utf-8")).hexdigest()[:24]


_ORDERING_TERMS = (
    "扫码点餐", "扫码点单", "扫码点菜", "扫码下单", "点餐", "点单", "点菜",
    "订餐", "餐桌码", "桌码", "点餐码", "在线点餐", "自助点餐", "立即点餐",
    "立即下单", "小程序点餐", "扫码菜单",
)
_NON_ORDERING_TERMS = (
    "企业微信", "企业号", "关注公众号", "微信公众号", "添加好友", "加好友",
    "进群", "加群", "收款码", "付款码", "微信支付", "核销",
)
_LOCAL_OCR_REASON_LABELS = {
    *(f"周边 OCR 识别到“{term}”" for term in _ORDERING_TERMS),
    *(f"周边 OCR 识别到“{term}”；二维码未能由本地解码器读出，需人工确认" for term in _ORDERING_TERMS),
    "周边 OCR 识别到非点餐用途标识",
    "二维码周边没有识别到明确点餐标识，需人工确认",
    "本地 OCR 未能完成，需人工确认",
}


def _nearby_ocr_text(box: dict, items: list[dict], image_size: tuple[int, int]) -> str:
    """只取二维码周边的 OCR 行，避免把同一张海报上的远处文字算给它。"""
    image_width, image_height = image_size
    x, y, width, height = (float(box[key]) for key in ("x", "y", "width", "height"))
    margin_x, margin_y = max(width * 1.25, 48), max(height * 1.25, 48)
    left, top = max(0, x - margin_x), max(0, y - margin_y)
    right, bottom = min(image_width, x + width + margin_x), min(image_height, y + height + margin_y)
    nearby = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            text_x, text_y, text_width, text_height = (
                float(item[key]) for key in ("x", "y", "width", "height")
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (text_x, text_y, text_width, text_height)):
            continue
        if text_width <= 0 or text_height <= 0:
            continue
        if text_x <= right and text_x + text_width >= left and text_y <= bottom and text_y + text_height >= top:
            nearby.append((text_y, text_x, item["text"]))
    nearby.sort(key=lambda item: (item[0], item[1]))
    return " ".join(text for _y, _x, text in nearby)


def _classify_local_purpose(text: str) -> tuple[str, str]:
    """用二维码附近的本地 OCR 文本判断用途；没有明确点餐字样时不猜。"""
    normalized = re.sub(r"[\W_]+", "", text.casefold())
    for term in _ORDERING_TERMS:
        if re.sub(r"[\W_]+", "", term.casefold()) in normalized:
            return "ordering", f"周边 OCR 识别到“{term}”"
    for term in _NON_ORDERING_TERMS:
        if re.sub(r"[\W_]+", "", term.casefold()) in normalized:
            return "other", "周边 OCR 识别到非点餐用途标识"
    return "unknown", "二维码周边没有识别到明确点餐标识，需人工确认"


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
    ):
        self.root = Path(root).expanduser().resolve()
        self.configs, self.task_store = configs, task_store
        self.inspector = inspector
        self.ocr_reader = ocr_reader or image_ocr
        self.control_lock = control_lock  # 发布并发/runner idle 由 API 外层负责。
        self.material_root = self.root / "materials"
        self.batch_root = self.material_root / "batches"
        self.upload_root = self.material_root / "uploads"
        self.published_root = self.material_root / "published"
        for directory in (self.batch_root, self.upload_root, self.published_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._init_schema()
        self.versions = MaterialVersions(self.task_store, self.published_root)
        # 崩溃后不恢复半截分析或上传，避免悄悄重跑/发布不完整素材。
        with self.task_store.connect() as db:
            db.execute(
                "UPDATE material_batches SET status='interrupted',error='进程中断；需显式重新创建批次',updated_at=? WHERE status IN ('uploading','queued','analyzing')",
                (_now(),),
            )
        # 服务接收请求前只恢复一次清理事务，避免并发首请求重复处理同一暂存目录。
        from .batch_management import BatchManager
        self._batch_management = BatchManager(self)

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
            for name in ("description", "deleted_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE material_batches ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(material_decisions)")}
            for name in ("reject_reason", "reject_detail", "rejected_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE material_decisions ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            # 管理列表按批次查询引用运行；索引避免每个批次都扫描完整 runs 表。
            db.execute("CREATE INDEX IF NOT EXISTS runs_material_batch ON runs(material_batch_id)")

    @staticmethod
    def rejection(row: dict) -> dict | None:
        code = row.get("reject_reason", "")
        if row["decision"] != "skipped" or not code:
            return None
        return {"code": code, "label": REJECTION_REASONS[code], "detail": row.get("reject_detail", ""), "marked_at": row.get("rejected_at", "")}

    def create_local_batch(self, input_root: Path, *, name: str | None = None, description: str = "") -> dict:
        name, description = self._batch_metadata(name, description)
        source = self._validate_local_root(Path(input_root))
        images = _enumerate_images(source)
        self._validate_local_limits(images)
        batch_id = uuid.uuid4().hex
        return self._insert_batch(batch_id, "local", source, "ready_to_analyze", total=len(images), name=name, description=description)

    def begin_upload(self, *, name: str | None = None, description: str = "") -> dict:
        name, description = self._batch_metadata(name, description)
        batch_id = uuid.uuid4().hex
        source = self.upload_root / batch_id / "source"
        source.mkdir(parents=True, mode=0o700, exist_ok=False)
        with self.task_store.connect() as db:
            stamp = _now()
            db.execute(
                "INSERT INTO material_batches(id,source_kind,source_root,status,created_at,updated_at,name,description) VALUES(?,?,?,?,?,?,?,?)",
                (batch_id, "upload", str(source), "uploading", stamp, stamp, name, description),
            )
        return {"batch_id": batch_id, "status": "uploading", "file_count": 0, "total_bytes": 0}

    def add_upload_file(self, batch_id: str, relative_path: str, content: bytes) -> dict:
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
                raise ValueError("单批总大小超过 2 GB 限制")
            source_root = Path(row["source_root"]).resolve(strict=True)
            target = source_root.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not _inside(target.parent, source_root) or any(parent.is_symlink() for parent in [target.parent, *target.parent.parents] if parent != source_root.parent):
                raise ValueError("上传路径解析后超出私有素材目录")
            if target.exists() or target.is_symlink():
                raise ValueError("上传批次内出现重复相对路径")
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
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

    def abort_upload(self, batch_id: str) -> dict:
        """上传失败后退出上传态，保留已收到的文件，允许用户在管理页回收。"""
        with self._lock:
            row = self._batch_row(batch_id)
            if row["status"] != "uploading":
                raise ValueError("该批次当前不在上传中")
            self._set_batch_error(batch_id, "failed", "上传未完成，可在批次管理中删除后重新导入")
        return self.get_batch(batch_id)

    def finish_upload(self, batch_id: str) -> dict:
        with self._lock, self.task_store.connect() as db:
            row = self._batch_row(batch_id)
            if row["status"] != "uploading":
                raise ValueError("上传批次不在 uploading 状态")
            if row["upload_file_count"] == 0:
                raise ValueError("上传批次没有可处理的图片")
            db.execute("UPDATE material_batches SET status='ready_to_analyze',updated_at=? WHERE id=?", (_now(), batch_id))
        return self.get_batch(batch_id)

    def begin_store_upload(self, batch_id: str, store_id: str) -> dict:
        """为当前门店创建临时补图目录，不触碰用户的原始素材目录。"""
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

    def add_store_upload_file(self, batch_id: str, store_id: str, upload_id: str, filename: str, content: bytes) -> dict:
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

    def commit_store_upload(self, batch_id: str, store_id: str, upload_id: str, mode: str, files: list[dict]) -> dict:
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
        return {"store": self.get_store(batch_id, store_id), "uploaded": len(prepared), "mode": mode}

    def abort_store_upload(self, batch_id: str, store_id: str, upload_id: str) -> None:
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

    def reanalyze_store(self, batch_id: str, store_id: str) -> dict:
        """只重跑当前门店的本地二维码检测与 OCR；批次其他门店和决定不变。"""
        with self._lock:
            batch = self._batch_row(batch_id)
            if batch["status"] != "completed":
                raise ValueError("只有分析完成的批次可以重新分析单店")
            row = self._decision_row(batch_id, store_id)
            if row["decision"] == "confirmed":
                raise ValueError("该门店已确认；请先撤销确认再重新分析")
            store = json.loads(row["store_json"])
            if not store.get("images"):
                raise ValueError("当前门店没有图片，请先补充素材")
            inspect = self.inspector or _apple_vision_inspector
            refreshed_images = []
            for image in store["images"]:
                source = self._checked_source(batch, image)
                refreshed = _inspect_image(source, image["path"], inspect)
                for key in ("display_name", "origin", "supplement_store_id"):
                    if key in image:
                        refreshed[key] = image[key]
                refreshed_images.append(refreshed)
            store["images"] = refreshed_images
            store["selected_source"] = None
            store["selected_artifact"] = None
            analysis_dir = self.batch_root / batch_id / "repair-analysis"
            analysis_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            manifest = {"stores": [store]}
            source_root = Path(batch["source_root"])
            self._classify_and_select(
                manifest, source_root, analysis_dir,
                source_resolver=lambda image: self._checked_source(batch, image),
            )
            store = manifest["stores"][0]
            # 检测后再核一次源图 hash，避免分析期间文件被替换。
            for image in store["images"]:
                self._checked_source(batch, image)
            recommendation = any(
                candidate.get("selected") and candidate.get("purpose") == "ordering"
                for image in store["images"] for candidate in image.get("candidates", [])
            )
            with self.task_store.connect() as db:
                db.execute(
                    "UPDATE material_decisions SET analysis_status=?,recommendation=?,selected_json='{}',selected_source_hash='',store_json=?,updated_at=? WHERE batch_id=? AND store_id=?",
                    (store["status"], int(recommendation), json.dumps(store, ensure_ascii=False), _now(), batch_id, store_id),
                )
                self._refresh_batch_summary(db, batch_id)
        return self.get_store(batch_id, store_id)

    def _supplement_dir(self, batch_id: str, store_id: str, upload_id: str) -> Path:
        if not _UPLOAD_ID_RE.fullmatch(batch_id) or not re.fullmatch(r"[0-9a-f]{24}", store_id) or not _UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("补图路径编号无效")
        batch_root = self.batch_root / batch_id
        store_root = batch_root / "supplements" / store_id
        if batch_root.is_symlink() or (batch_root / "supplements").is_symlink() or store_root.is_symlink():
            raise ValueError("批次素材目录不安全")
        folder = batch_root / "supplements" / store_id / upload_id
        return folder

    @staticmethod
    def _refresh_batch_summary(db, batch_id: str) -> None:
        batch = db.execute("SELECT summary_json FROM material_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise ValueError("素材批次不存在")
        try:
            summary = json.loads(batch["summary_json"])
        except (TypeError, ValueError):
            summary = {}
        rows = db.execute("SELECT recommendation,store_json FROM material_decisions WHERE batch_id=?", (batch_id,)).fetchall()
        summary["total_images"] = sum(len(json.loads(row["store_json"]).get("images", [])) for row in rows)
        summary["total_stores"] = len(rows)
        summary["selected_stores"] = sum(bool(row["recommendation"]) for row in rows)
        summary["review_stores"] = len(rows) - summary["selected_stores"]
        db.execute(
            "UPDATE material_batches SET summary_json=?,progress_done=?,progress_total=?,updated_at=? WHERE id=?",
            (json.dumps(summary, ensure_ascii=False), summary["total_images"] * 2, summary["total_images"] * 2, _now(), batch_id),
        )

    def create_upload_batch(self, files: Iterable[dict]) -> dict:
        """小批量/测试便捷接口；生产 API 应使用逐文件三步上传。"""
        batch = self.begin_upload()
        try:
            for item in files:
                self.add_upload_file(batch["batch_id"], item["path"], item["content"])
            return self.finish_upload(batch["batch_id"])
        except Exception:
            self._set_batch_error(batch["batch_id"], "upload_error", "上传失败；未开始分析")
            shutil.rmtree(self.upload_root / batch["batch_id"], ignore_errors=True)
            raise

    def start_analysis(self, batch_id: str) -> dict:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                raise RuntimeError("已有素材批次正在分析")
            row = self._batch_row(batch_id)
            if row["status"] != "ready_to_analyze":
                raise ValueError("只有 ready_to_analyze 批次可以开始分析")
            with self.task_store.connect() as db:
                db.execute(
                    "UPDATE material_batches SET status='queued',progress_done=0,error='',updated_at=? WHERE id=?",
                    (_now(), batch_id),
                )
            worker = threading.Thread(target=self._analyze_worker, args=(batch_id,), name=f"materials-{batch_id[:8]}", daemon=True)
            self._worker = worker
            try:
                worker.start()
            except Exception:
                self._set_batch_error(batch_id, "failed", "无法启动素材分析线程")
                raise
        return self.get_batch(batch_id)

    def _analyze_worker(self, batch_id: str) -> None:
        row = self._batch_row(batch_id)
        source_root = Path(row["source_root"])
        analysis_dir = self.batch_root / batch_id / "analysis"
        analysis_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with self.task_store.connect() as db:
                db.execute("UPDATE material_batches SET status='analyzing',updated_at=? WHERE id=?", (_now(), batch_id))
            current_files = _enumerate_images(source_root)
            self._validate_local_limits(current_files)
            if row["source_kind"] == "upload" and len(current_files) != row["upload_file_count"]:
                raise ValueError("上传素材文件集合发生变化，拒绝分析")
            with self.task_store.connect() as db:
                db.execute("UPDATE material_batches SET progress_total=?,updated_at=? WHERE id=?", (len(current_files) * 2, _now(), batch_id))

            def on_progress(value: dict) -> None:
                with self.task_store.connect() as db:
                    db.execute(
                        "UPDATE material_batches SET progress_done=?,progress_total=?,updated_at=? WHERE id=? AND status='analyzing'",
                        (value["done"], value["total"] * 2, _now(), batch_id),
                    )

            manifest = build_materials(source_root, analysis_dir, inspector=self.inspector, progress=on_progress)
            def on_classified(done: int) -> None:
                with self.task_store.connect() as db:
                    db.execute("UPDATE material_batches SET progress_done=?,updated_at=? WHERE id=? AND status='analyzing'",
                               (len(current_files) + done, _now(), batch_id))

            self._classify_and_select(manifest, source_root, analysis_dir, on_classified)
            after_files = _enumerate_images(source_root)
            self._validate_local_limits(after_files)
            if len(after_files) != len(current_files) or (row["source_kind"] == "upload" and len(after_files) != row["upload_file_count"]):
                raise ValueError("分析期间素材文件集合发生变化，拒绝保存结果")
            safe_manifest = self._sanitize_manifest(manifest)
            atomic_json(analysis_dir / "manifest.json", safe_manifest)
            with self.task_store.connect() as db:
                for store in safe_manifest["stores"]:
                    store_id = _store_id(store["source_folder"])
                    recommendation = any(candidate.get("selected") for image in store["images"] for candidate in image["candidates"])
                    db.execute(
                        "INSERT INTO material_decisions(batch_id,store_id,source_folder,source_name,analysis_status,recommendation,store_json,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (batch_id, store_id, store["source_folder"], store["source_name"], store["status"], int(recommendation), json.dumps(store, ensure_ascii=False), _now()),
                    )
                db.execute(
                    "UPDATE material_batches SET status='completed',progress_done=?,progress_total=?,summary_json=?,manifest_json=?,updated_at=? WHERE id=?",
                    (safe_manifest["summary"]["total_images"] * 2, safe_manifest["summary"]["total_images"] * 2, json.dumps(safe_manifest["summary"], ensure_ascii=False), json.dumps(safe_manifest, ensure_ascii=False), _now(), batch_id),
                )
        except Exception as exc:
            # 不把路径、图片内容或条码解码错误原文写进批次状态。
            self._set_batch_error(batch_id, "failed", f"素材分析失败（{type(exc).__name__}）")

    def adopt_manifest(self, manifest_path: Path) -> dict:
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
            stamp = _now()
            db.execute(
                "INSERT INTO material_batches(id,source_kind,source_root,status,progress_done,progress_total,summary_json,manifest_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_id, "adopted_manifest", str(source), "completed", safe_manifest["summary"]["total_images"], safe_manifest["summary"]["total_images"], json.dumps(safe_manifest["summary"], ensure_ascii=False), json.dumps(safe_manifest, ensure_ascii=False), stamp, stamp),
            )
            for store in safe_manifest["stores"]:
                store_id = _store_id(store["source_folder"])
                recommendation = any(candidate.get("selected") for image in store["images"] for candidate in image["candidates"])
                db.execute(
                    "INSERT INTO material_decisions(batch_id,store_id,source_folder,source_name,analysis_status,recommendation,store_json,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (batch_id, store_id, store["source_folder"], store["source_name"], store["status"], int(recommendation), json.dumps(store, ensure_ascii=False), stamp),
                )
        return self.get_batch(batch_id)

    def _classify_and_select(self, manifest: dict, source_root: Path, analysis_dir: Path, progress=None, source_resolver=None) -> None:
        """本地 OCR 关联二维码周边文字，再按可解码和清晰度为每店选一张。"""
        resolve_source = source_resolver or (lambda image: source_root / image["path"])
        duplicate_names: set[str] = set()
        names: dict[str, set[str]] = {}
        for store in manifest["stores"]:
            names.setdefault(store["source_name"], set()).add(store["source_folder"])
        duplicate_names = {name for name, folders in names.items() if len(folders) > 1}
        processed = 0
        for store in manifest["stores"]:
            for image in store["images"]:
                source = resolve_source(image)
                ocr_items = None
                image_size = None
                if any(candidate.get("purpose_hint") != "enterprise_wechat" for candidate in image["candidates"]):
                    try:
                        ocr_items = self.ocr_reader(source)
                        if not isinstance(ocr_items, list):
                            raise TypeError("OCR 结果格式无效")
                        full_text = " ".join(
                            item.get("text", "") for item in ocr_items
                            if isinstance(item, dict) and isinstance(item.get("text"), str)
                        )
                        if _classify_local_purpose(full_text)[0] == "ordering":
                            image["ordering_text_seen"] = True
                        with Image.open(source) as source_image:
                            image_size = source_image.size
                    except Exception:
                        ocr_items = None
                for candidate in image["candidates"]:
                    if candidate.get("purpose_hint") == "enterprise_wechat":
                        candidate.update(
                            purpose="enterprise_wechat",
                            purpose_source="payload_domain",
                            purpose_reason="本地解码识别为企业微信域名",
                        )
                        continue
                    if ocr_items is None or image_size is None:
                        candidate.update(
                            purpose="unknown",
                            purpose_source="local_ocr",
                            purpose_reason="本地 OCR 未能完成，需人工确认",
                        )
                    else:
                        nearby_text = _nearby_ocr_text(candidate["box"], ocr_items, image_size)
                        purpose, reason = _classify_local_purpose(nearby_text)
                        if purpose == "ordering" and not candidate.get("payload_sha256"):
                            reason += "；二维码未能由本地解码器读出，需人工确认"
                        candidate.update(
                            purpose=purpose,
                            purpose_source="local_ocr",
                            purpose_reason=reason,
                        )

            ordering = [
                candidate
                for image in store["images"]
                for candidate in image["candidates"]
                if candidate.get("purpose") == "ordering" and candidate.get("payload_sha256")
            ]
            # 店内还没有可解码的点餐码时，再看没有二维码框出的照片；
            # OCR 只用于提示人工找码，不会凭一行文字虚构二维码位置。
            if not ordering:
                for image in store["images"]:
                    if image["candidates"]:
                        continue
                    try:
                        ocr_items = self.ocr_reader(resolve_source(image))
                        if not isinstance(ocr_items, list):
                            continue
                        full_text = " ".join(
                            item.get("text", "") for item in ocr_items
                            if isinstance(item, dict) and isinstance(item.get("text"), str)
                        )
                        if _classify_local_purpose(full_text)[0] == "ordering":
                            image["ordering_text_seen"] = True
                    except Exception:
                        continue

            for _image in store["images"]:
                processed += 1
                if progress:
                    progress(processed)

            # 清除 build_materials 的几何启发式预选；由语义分类单独决定建议。
            for image in store["images"]:
                for candidate in image["candidates"]:
                    candidate["selected"] = False
            store["selected_source"] = None
            old_artifact = store.get("selected_artifact")
            if old_artifact:
                (analysis_dir / old_artifact).unlink(missing_ok=True)
            store["selected_artifact"] = None
            if store["source_name"] in duplicate_names:
                store["status"] = "needs_review"
                store["reason"] = "不同父路径存在同名店铺目录，拒绝归档以防混店"
                continue

            ordering = [
                (image_index, candidate_index, image, candidate)
                for image_index, image in enumerate(store["images"])
                for candidate_index, candidate in enumerate(image["candidates"])
                if candidate.get("purpose") == "ordering"
            ]
            # 解码成功就是本地可读的直接证据，不再用固定像素阈值排除较小的码。
            eligible = [item for item in ordering if item[3].get("payload_sha256")]
            if not eligible:
                store["status"] = "needs_review"
                if ordering:
                    store["reason"] = "识别到点餐标识，但二维码未能由本地解码器读出，需人工确认"
                elif any(image.get("ordering_text_seen") for image in store["images"]):
                    store["reason"] = "原图 OCR 发现点餐标识，但本地没有定位到对应二维码，需人工确认"
                elif not any(image["candidates"] for image in store["images"]):
                    store["reason"] = "没有检测到可用二维码候选，需人工确认"
                elif any("本地 OCR 未能完成" in c.get("purpose_reason", "") for image in store["images"] for c in image["candidates"]):
                    store["reason"] = "本地 OCR 未能完成，需人工确认"
                else:
                    store["reason"] = "没有在二维码周边识别到明确点餐标识，需人工确认"
                continue
            image_index, candidate_index, image, candidate = max(
                eligible,
                key=lambda item: (item[3].get("quality_score", item[3].get("short_edge_px", 0)), item[3].get("short_edge_px", 0), bool(item[3].get("payload_sha256")), item[2]["path"]),
            )
            artifact_relative = PurePosixPath("可采集") / store["source_name"] / "二维码.png"
            artifact = analysis_dir.joinpath(*artifact_relative.parts)
            try:
                artifact.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                _crop_candidate(resolve_source(image), candidate["box"], artifact)
            except Exception:
                artifact.unlink(missing_ok=True)
                store["status"] = "needs_review"
                store["reason"] = "点餐候选裁剪失败，需人工复核"
                continue
            candidate["selected"] = True
            store["status"] = "selected"
            store["reason"] = "周边 OCR 识别到点餐标识且二维码本地解码成功；已按尺寸和清晰度选取，待微信验证"
            store["selected_source"] = image["path"]
            store["selected_artifact"] = artifact_relative.as_posix()

    def active_release(self, active_root: Path) -> dict | None:
        return self.versions.active(active_root)

    def _version_summary(self, batch: dict) -> dict:
        root = Path(self.configs.load().input_dir).expanduser().resolve()
        versions = self.versions.list(batch["id"], root, include_deleted=bool(batch.get("deleted_at")))
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

    def list_batches(self, include_deleted: bool = False) -> list[dict]:
        with self.task_store.connect() as db:
            predicate = "" if include_deleted else "WHERE deleted_at=''"
            rows = [dict(row) for row in db.execute(f"SELECT * FROM material_batches {predicate} ORDER BY created_at DESC,id DESC")]
        return [self._batch_summary(row) for row in rows]

    def rejections(self) -> list[dict]:
        """返回仍处于不合格状态的清洗门店，直接以人工审核决策为事实源。"""
        with self.task_store.connect() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT d.*, b.name AS batch_name
                FROM material_decisions d
                JOIN material_batches b ON b.id=d.batch_id
                WHERE d.decision='skipped' AND b.deleted_at=''
                ORDER BY d.updated_at DESC,d.source_folder
            """)]
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
                "marked_at": row.get("rejected_at") or row["updated_at"],
                "status": "不合格",
                "stage": "素材清洗",
                "batch_id": row["batch_id"],
                "batch_name": row.get("batch_name", ""),
                "task_id": "",
                "run_id": "",
            })
        return result

    def get_batch(self, batch_id: str) -> dict:
        row = self._batch_row(batch_id)
        with self.task_store.connect() as db:
            stores = [dict(item) for item in db.execute("SELECT * FROM material_decisions WHERE batch_id=? ORDER BY source_folder,store_id", (batch_id,))]
        summary = self._summary_counts(row, stores)
        return {
            "batch_id": batch_id,
            "source_kind": row["source_kind"],
            "source_root": row["source_root"],
            "status": row["status"],
            "progress": {"done": row["progress_done"], "total": row["progress_total"]},
            "summary": summary,
            "error": row["error"],
            "snapshot_id": row["snapshot_id"] or None,
            **self._version_summary(row),
            "stores": [self._store_summary(item) for item in stores],
        }

    def get_store(self, batch_id: str, store_id: str) -> dict:
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
                    "recommendation": bool(candidate.get("selected") and candidate.get("purpose") == "ordering"),
                })
            images.append({**image, "image_index": image_index, "candidates": candidates})
        return {
            "batch_id": batch_id,
            "store_id": store_id,
            "source_name": row["source_name"],
            "source_folder": row["source_folder"],
            "analysis_status": summary["analysis_status"],
            "reason": summary["reason"],
            "decision": row["decision"],
            "rejection": self.rejection(row),
            "recommendation": summary["recommendation"],
            "selected": selected,
            "images": images,
        }

    def preview_original(self, batch_id: str, store_id: str, image_index: int) -> dict:
        batch = self._batch_row(batch_id)
        store_row = self._decision_row(batch_id, store_id)
        store = json.loads(store_row["store_json"])
        image = self._image_record(store, image_index)
        path = self._checked_source(batch, image)
        return {"bytes": path.read_bytes(), "media_type": self._media_type(path), "filename": path.name}

    def preview_candidate(self, batch_id: str, store_id: str, image_index: int, candidate_index: int) -> dict:
        batch = self._batch_row(batch_id)
        store_row = self._decision_row(batch_id, store_id)
        store = json.loads(store_row["store_json"])
        image = self._image_record(store, image_index)
        candidate = self._candidate_record(image, candidate_index)
        source = self._checked_source(batch, image)
        buffer = io.BytesIO()
        with Image.open(source) as original:
            box = candidate["box"]
            left, top, right, bottom = self._crop_bounds(original.size, box)
            crop = original.crop((left, top, right, bottom))
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
    ) -> dict:
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
                    with Image.open(source) as original:
                        normalized_box = self._validate_box(box, original.size)
                else:
                    candidate = self._candidate_record(image, candidate_index)
                    if box is not None:
                        raise ValueError("选择现有候选时不允许覆盖其 box")
                    normalized_box = candidate["box"]
                selected = {"image_index": image_index, "candidate_index": candidate_index, "box": normalized_box}
                source_hash = image["sha256"]
            rejected_at = _now() if decision == "skip" else ""
            if decision == "skip" and row["decision"] == "skipped" and not reject_reason:
                rejected_at = row.get("rejected_at", "") or _now()
            with self.task_store.connect() as db:
                db.execute(
                    "UPDATE material_decisions SET decision=?,selected_json=?,selected_source_hash=?,reject_reason=?,reject_detail=?,rejected_at=?,updated_at=? WHERE batch_id=? AND store_id=?",
                    ("confirmed" if decision == "confirm" else "skipped" if decision == "skip" else "pending",
                     json.dumps(selected, ensure_ascii=False), source_hash, reason_code, reason_detail, rejected_at, _now(), batch_id, store_id),
                )
        return self.get_store(batch_id, store_id)

    def accept_recommendations(self, batch_id: str, store_ids: list[str] | None = None) -> dict:
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
                    if candidate.get("selected") and candidate.get("purpose") == "ordering"
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

    def publish_snapshot(self, batch_id: str, description: str = "", *, name: str | None = None, version: str | None = None, activate: bool = True) -> dict:
        if not isinstance(description, str) or len(description.strip()) > 200:
            raise ValueError("发布说明最多 200 字")
        description = description.strip()
        with self._publish_lock, self._lock:
            batch = self._batch_row(batch_id)
            if batch["status"] != "completed":
                raise ValueError("只有分析完成的批次可以发布")
            with self.task_store.connect() as db:
                rows = [dict(row) for row in db.execute("SELECT * FROM material_decisions WHERE batch_id=? AND decision='confirmed' ORDER BY source_folder,store_id", (batch_id,))]
            if not rows:
                raise ValueError("没有已人工确认的店铺；未发布任何素材")
            stores = [(row, json.loads(row["store_json"]), json.loads(row["selected_json"])) for row in rows]
            names: dict[str, str] = {}
            fingerprint_items = []
            for row, store, selected in stores:
                if not selected:
                    raise ValueError("已确认决策缺少候选坐标")
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
                    box = self._validate_box(selected["box"], original.size)
                fingerprint_items.append({
                    "store_id": row["store_id"], "source_folder": row["source_folder"],
                    "source_name": row["source_name"], "source_hash": row["selected_source_hash"],
                    "image_index": selected["image_index"], "candidate_index": selected["candidate_index"], "box": box,
                })
            content = json.dumps(fingerprint_items, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            # 对相同版本的重试必须先验证内容，防止覆盖已经发布的版本。
            existing = self.versions.records(batch_id)
            legacy_fingerprint = hashlib.sha256(content).hexdigest()
            # 同内容在不同批次内仍需不同任务。已发布的本批次快照继续沿用旧 ID。
            if any(row["snapshot_id"] == legacy_fingerprint for row in existing):
                fingerprint = legacy_fingerprint
            else:
                fingerprint = hashlib.sha256(batch_id.encode() + b"\0" + content).hexdigest()
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
            expected_artifact_paths = {f"{row['source_name']}/二维码.png" for row, _store, _selected in stores}
            if already_published:
                self._validate_snapshot_marker(snapshot, fingerprint, expected_artifact_paths)
            else:
                temporary = self.published_root / ("." + fingerprint + ".tmp-" + uuid.uuid4().hex)
                temporary.mkdir(mode=0o700)
                try:
                    artifact_hashes = []
                    for row, _store, selected in stores:
                        image = self._image_record(_store, selected["image_index"])
                        source = self._checked_source(batch, image)
                        artifact = temporary / row["source_name"] / "二维码.png"
                        artifact.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                        _crop_candidate(source, selected["box"], artifact)
                        artifact_hashes.append({"path": artifact.relative_to(temporary).as_posix(), "sha256": _sha256(artifact)})
                    atomic_json(temporary / ".materials-snapshot.json", {"snapshot_id": fingerprint, "stores": fingerprint_items, "artifacts": artifact_hashes})
                    temporary.rename(snapshot)
                except Exception:
                    shutil.rmtree(temporary, ignore_errors=True)
                    raise

            self._validate_snapshot_marker(snapshot, fingerprint, expected_artifact_paths)

            previous = self.configs.load().model_dump()
            previous_input = previous["input_dir"]
            try:
                updated = dict(previous)
                updated["input_dir"] = str(snapshot)
                if activate:
                    self.configs.save(updated)
                reuse_roots = [self.published_root / row["snapshot_id"] for row in existing]
                task_scan = self.task_store.scan(snapshot, reuse_sources=reuse_roots)
                with self.task_store.connect() as db:
                    self.versions.remember(db, batch_id, name.strip(), version, fingerprint, description, len(stores), activate=activate)
            except Exception:
                try:
                    previous["input_dir"] = previous_input
                    self.configs.save(previous)
                except Exception:
                    pass
                raise
            return {
                "snapshot_id": fingerprint,
                "release": next(item for item in self.versions.list(batch_id, Path(self.configs.load().input_dir).expanduser().resolve()) if item["version"] == version),
                "input_dir": str(snapshot),
                "task_scan": task_scan,
                "already_published": already_published or task_scan.get("added", 0) == 0,
            }

    def activate_version(self, batch_id: str, version: str) -> dict:
        with self._publish_lock, self._lock:
            release = self.versions.find(batch_id, version)
            if release is None:
                raise ValueError("发布版本不存在")
            snapshot = self.published_root / release["snapshot_id"]
            manifest = json.loads((snapshot / ".materials-snapshot.json").read_text())
            self._validate_snapshot_marker(snapshot, release["snapshot_id"], {row["path"] for row in manifest["artifacts"]})
            previous = self.configs.load().model_dump()
            updated = {**previous, "input_dir": str(snapshot)}
            try:
                self.configs.save(updated)
                scan = self.task_store.scan(snapshot, reuse_sources=[self.published_root / row["snapshot_id"]
                                                                   for row in self.versions.records(batch_id)])
                with self.task_store.connect() as db:
                    db.execute("INSERT INTO material_selection VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET version_id=excluded.version_id",
                               (release["id"],))
            except Exception:
                self.configs.save(previous)
                raise
            return {"release": self.active_release(snapshot), "task_scan": scan}

    def _insert_batch(self, batch_id: str, kind: str, source: Path, status: str, *, total: int = 0,
                      name: str, description: str) -> dict:
        source = source.resolve(strict=True)
        if self.material_root == source or self.material_root in source.parents or source in self.material_root.parents:
            raise ValueError("素材源不能与素材管理输出目录重叠")
        with self.task_store.connect() as db:
            stamp = _now()
            db.execute(
                "INSERT INTO material_batches(id,source_kind,source_root,status,progress_total,created_at,updated_at,name,description) VALUES(?,?,?,?,?,?,?,?,?)",
                (batch_id, kind, str(source), status, total, stamp, stamp, name, description),
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
                raise ValueError("单批总大小超过 2 GB 限制")

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
        return {
            "total_stores": summary.get("total_stores", len(decisions)),
            "total_images": summary.get("total_images", batch["progress_total"]),
            "selected_stores": summary.get("selected_stores", sum(row["recommendation"] for row in decisions)),
            "review_stores": summary.get("review_stores", sum(not row["recommendation"] for row in decisions)),
            "confirmed_stores": sum(row["decision"] == "confirmed" for row in decisions),
            "skipped_stores": sum(row["decision"] == "skipped" for row in decisions),
            "pending_stores": sum(row["decision"] == "pending" for row in decisions),
        }

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
        valid_recommendation = bool(row["recommendation"]) and any(
            candidate.get("selected") and candidate.get("purpose") == "ordering"
            for image in data.get("images", []) for candidate in image.get("candidates", [])
        )
        stale_pending = bool(row["recommendation"]) and not valid_recommendation and row["decision"] == "pending"
        return {
            "store_id": row["store_id"], "source_name": row["source_name"], "source_folder": row["source_folder"],
            "analysis_status": "needs_review" if stale_pending else row["analysis_status"],
            "reason": "旧批次未判断二维码用途，请重新导入分析或人工选择" if stale_pending else data.get("reason", ""),
            "recommendation": valid_recommendation, "decision": row["decision"],
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
            "enterprise_wechat": "本地解码识别为企业微信用途",
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
    def _validate_box(box: dict, image_size: tuple[int, int]) -> dict:
        if not isinstance(box, dict):
            raise ValueError("二维码 box 必须是对象")
        values = [box.get(key) for key in ("x", "y", "width", "height")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise ValueError("二维码 box 坐标必须是有限数值")
        x, y, width, height = map(float, values)
        image_width, image_height = image_size
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > image_width or y + height > image_height:
            raise ValueError("二维码 box 超出原图边界")
        return {"x": x, "y": y, "width": width, "height": height}

    @staticmethod
    def _crop_bounds(image_size: tuple[int, int], box: dict) -> tuple[int, int, int, int]:
        width, height = image_size
        x, y, qr_width, qr_height = (box[key] for key in ("x", "y", "width", "height"))
        return (
            max(0, math.floor(x - qr_width * CROP_MARGIN_RATIO)),
            max(0, math.floor(y - qr_height * CROP_MARGIN_RATIO)),
            min(width, math.ceil(x + qr_width * (1 + CROP_MARGIN_RATIO))),
            min(height, math.ceil(y + qr_height * (1 + CROP_MARGIN_RATIO))),
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
                    purpose = candidate.get("purpose", "unknown")
                    if purpose not in {"ordering", "enterprise_wechat", "other", "unknown"}:
                        purpose = "unknown"
                    purpose_source = candidate.get("purpose_source")
                    safe_reason = cls._purpose_reason(purpose)
                    if purpose_source == "local_ocr" and candidate.get("purpose_reason") in _LOCAL_OCR_REASON_LABELS:
                        safe_reason = candidate["purpose_reason"]
                    safe_candidate = {
                        "box": box, "payload_sha256": payload_hash, "short_edge_px": edge,
                        # 旧 manifest 没有用途分类；不能沿用旧几何启发式的推荐状态。
                        "selected": bool(candidate.get("selected")) and purpose == "ordering", "purpose": purpose,
                        # OCR 只保存受控判断标签，不把整段图片文字写入清单。
                        "purpose_reason": ("本地解码识别为企业微信域名" if purpose == "enterprise_wechat" and purpose_source == "payload_domain" else safe_reason),
                        "source": "vision_fallback" if candidate.get("source") == "vision_fallback" else "local_detector",
                        "purpose_source": (
                            "payload_domain"
                            if purpose == "enterprise_wechat" and purpose_source == "payload_domain"
                            else "local_ocr"
                            if purpose_source == "local_ocr"
                            else "vision_model"
                        ),
                    }
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
                    **({"error": str(raw_image["error"])[:200]} if raw_image.get("error") else {}),
                })
                total_images += 1
            status = raw_store.get("status") if raw_store.get("status") in {"selected", "needs_review"} else "needs_review"
            has_recommendation = any(candidate["selected"] for image in images for candidate in image["candidates"])
            if status == "selected" and not has_recommendation:
                status = "needs_review"
            safe_stores.append({
                "source_name": source_name, "source_folder": folder_key, "status": status,
                "reason": ("旧清单缺少二维码用途分类，需人工复核" if raw_store.get("status") == "selected" and not has_recommendation else str(raw_store.get("reason", ""))[:300]),
                "selected_source": raw_store.get("selected_source") if has_recommendation and isinstance(raw_store.get("selected_source"), str) else None,
                "selected_artifact": raw_store.get("selected_artifact") if has_recommendation and isinstance(raw_store.get("selected_artifact"), str) else None,
                "images": images,
            })
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

