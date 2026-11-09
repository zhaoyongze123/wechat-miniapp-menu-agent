"""清洗批次的管理生命周期：元数据、回收站与受控清理。"""
from __future__ import annotations

import shutil
import uuid
import json
from pathlib import Path
import re

from .config import atomic_json
from .materials import _now


_ACTIVE_BATCH_STATES = {"uploading", "queued", "analyzing"}
_TERMINAL_RUN_STATES = {"completed", "terminated"}


class BatchManager:
    def __init__(self, service):
        self.service = service
        self._recover_purge_staging()

    def list(self, *, deleted: bool = False) -> list[dict]:
        predicate = "deleted_at!=''" if deleted else "deleted_at=''"
        with self.service.task_store.connect() as db:
            batches = [dict(row) for row in db.execute(
                f"SELECT * FROM material_batches WHERE {predicate} ORDER BY created_at DESC,id DESC"
            )]
        return [self.service._batch_summary(batch) for batch in batches]

    def update(self, batch_id: str, *, name: str, description: str = "") -> dict:
        name, description = self.service._batch_metadata(name, description)
        with self.service._publish_lock, self.service._lock:
            batch = self.service._batch_row(batch_id)
            with self.service.task_store.connect() as db:
                db.execute("UPDATE material_batches SET name=?,description=?,updated_at=? WHERE id=? AND deleted_at=''",
                           (name, description, _now(), batch_id))
            return self._readable(batch_id)

    def delete(self, batch_id: str) -> dict:
        # 活跃任务先快速拒绝，避免等 worker 完成后才意外删除刚结束的批次。
        initial = self.service._batch_row(batch_id)
        if initial["status"] in _ACTIVE_BATCH_STATES:
            raise ValueError("批次正在分析或上传")
        with self.service._publish_lock, self.service._worker_lock, self.service._lock:
            batch = self.service._batch_row(batch_id)
            summary = self.summary(batch)
            if not summary["can_delete"]:
                raise ValueError(summary["delete_block_reason"])
            with self.service.task_store.connect() as db:
                db.execute("UPDATE material_batches SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at=''",
                           (_now(), _now(), batch_id))
            return self._readable(batch_id, include_deleted=True)

    def restore(self, batch_id: str) -> dict:
        with self.service._publish_lock, self.service._worker_lock, self.service._lock:
            batch = self.service._batch_row(batch_id, include_deleted=True)
            if not batch["deleted_at"]:
                raise ValueError("该批次不在回收站")
            with self.service.task_store.connect() as db:
                db.execute("UPDATE material_batches SET deleted_at='',updated_at=? WHERE id=?", (_now(), batch_id))
            return self._readable(batch_id)

    def clear_selection(self, *, batch_id: str | None = None) -> dict:
        """清空当前素材版本选择及输入目录；保留发布快照、版本和采集任务。"""
        with self.service._publish_lock, self.service._worker_lock, self.service._lock:
            config = self.service.configs.load()
            input_dir = config.input_dir
            current_root = Path(input_dir).expanduser().resolve() if input_dir else None
            with self.service.task_store.connect() as db:
                selection = db.execute(
                    "SELECT v.id AS version_id,v.batch_id,v.snapshot_id FROM material_selection s "
                    "JOIN material_versions v ON v.id=s.version_id WHERE s.singleton=1"
                ).fetchone()
                active = None
                if selection and current_root == (self.service.published_root / selection["snapshot_id"]).resolve():
                    active = dict(selection)
            if active is None:
                if batch_id:
                    raise ValueError("指定批次不是当前选用版本")
                return {"ok": True, "cleared": False, "batch_id": None}
            if batch_id and batch_id != active["batch_id"]:
                raise ValueError("指定批次不是当前选用版本")
            with self.service.task_store.connect() as db:
                runs = db.execute("SELECT status FROM runs WHERE material_batch_id=?", (active["batch_id"],)).fetchall()
            if any(row["status"] not in _TERMINAL_RUN_STATES for row in runs):
                raise ValueError("仍有运行中或可恢复的采集任务引用此批次，不能清除当前选用状态")

            previous = config.model_dump()
            payload = {**previous, "input_dir": ""}
            # 配置与选用记录跨文件存储；数据库提交失败时回退配置，避免留下半次清除。
            atomic_json(self.service.configs.path, payload)
            try:
                with self.service.task_store.connect() as db:
                    db.execute("DELETE FROM material_selection WHERE singleton=1 AND version_id=?",
                               (active["version_id"],))
            except Exception:
                atomic_json(self.service.configs.path, previous)
                raise
            return {"ok": True, "cleared": True, "batch_id": active["batch_id"]}

    def purge(self, batch_id: str) -> dict:
        """仅清除无发布、无任何运行引用的回收站批次及程序托管目录。"""
        with self.service._publish_lock, self.service._worker_lock, self.service._lock:
            batch = self.service._batch_row(batch_id, include_deleted=True)
            if not batch["deleted_at"]:
                raise ValueError("只能彻底删除回收站中的批次")
            if batch["status"] in _ACTIVE_BATCH_STATES:
                raise ValueError("批次正在分析或上传")
            state = self._state(batch)
            if state["versions_count"] or batch["snapshot_id"]:
                raise ValueError("已发布批次及其快照必须保留")
            if state["any_run_reference"]:
                raise ValueError("仍有运行记录引用此批次，不能彻底删除")
            # 只触碰以批次 ID 命名的程序托管目录；本地来源目录和发布快照永不参与清理。
            managed = [self.service.upload_root / batch_id, self.service.batch_root / batch_id]
            for path in managed:
                self._validate_managed_path(path, path.parent)
            staging_root = self.service.material_root / ".purge"
            if staging_root.exists() and (staging_root.is_symlink() or not staging_root.is_dir()):
                raise ValueError("清理暂存目录不是安全目录")
            staging_root.mkdir(mode=0o700, exist_ok=True)
            staging = staging_root / (batch_id + "-" + uuid.uuid4().hex)
            staging.mkdir(mode=0o700)
            moved: list[tuple[Path, Path]] = []
            committed = False
            journal = staging / "journal.json"
            journal_rows = [{"original": str(source), "staged": str(staging / source.parent.name)} for source in managed]
            self._write_journal(journal, {"batch_id": batch_id, "phase": "moving", "paths": journal_rows})
            try:
                for source in managed:
                    if source.exists():
                        target = staging / source.parent.name
                        source.rename(target)
                        moved.append((target, source))
                self._write_journal(journal, {"batch_id": batch_id, "phase": "moved", "paths": journal_rows})
                with self.service.task_store.connect() as db:
                    # purge 前再次在同一数据库事务中核对引用，避免并发创建新的引用。
                    if db.execute("SELECT 1 FROM runs WHERE material_batch_id=? LIMIT 1", (batch_id,)).fetchone():
                        raise ValueError("运行记录仍引用此批次，不能彻底删除")
                    if db.execute("SELECT 1 FROM material_versions WHERE batch_id=? LIMIT 1", (batch_id,)).fetchone():
                        raise ValueError("批次已关联发布版本，不能彻底删除")
                    db.execute("DELETE FROM material_decisions WHERE batch_id=?", (batch_id,))
                    db.execute("DELETE FROM material_batches WHERE id=? AND deleted_at!=''", (batch_id,))
                committed = True
                try:
                    self._write_journal(journal, {"batch_id": batch_id, "phase": "db_committed", "paths": journal_rows})
                    self._remove_staging_contents(staging)
                except Exception as exc:
                    raise ValueError("批次记录已删除，托管文件清理待恢复处理") from exc
                return {"batch_id": batch_id, "purged": True}
            except Exception:
                # DB 未提交时恢复原目录；恢复失败应显式抛出，不能吞掉数据不一致。
                if not committed:
                    for staged, original in reversed(moved):
                        if staged.exists() and not original.exists():
                            staged.rename(original)
                    if staging.exists():
                        self._remove_staging_contents(staging)
                raise

    def summary(self, batch: dict) -> dict:
        state = self._state(batch)
        reasons = []
        if batch["status"] in _ACTIVE_BATCH_STATES:
            reasons.append("批次正在分析或上传")
        if state["recoverable_run"]:
            reasons.append("仍有运行中或可恢复的采集任务引用此批次")
        if state["current_active"]:
            reasons.append("此批次当前正在使用；请先切换版本或清除当前选用状态")
        deleted = bool(batch["deleted_at"])
        can_delete = not deleted and not reasons
        can_purge = (deleted and batch["status"] not in _ACTIVE_BATCH_STATES and not state["versions_count"]
                     and not batch["snapshot_id"] and not state["any_run_reference"])
        clear_reason = ""
        if not state["current_active"]:
            clear_reason = "此批次不是当前选用版本"
        elif state["recoverable_run"]:
            clear_reason = "仍有运行中或可恢复的采集任务引用此批次"
        purge_reason = ""
        if not deleted:
            purge_reason = "请先将批次移入回收站"
        elif batch["status"] in _ACTIVE_BATCH_STATES:
            purge_reason = "批次正在分析或上传"
        elif state["versions_count"] or batch["snapshot_id"]:
            purge_reason = "已发布批次及其快照必须保留"
        elif state["any_run_reference"]:
            purge_reason = "仍有运行记录引用此批次"
        return {
            "name": batch["name"], "description": batch["description"], "deleted_at": batch["deleted_at"],
            "versions_count": state["versions_count"], "current_active": state["current_active"],
            "can_delete": can_delete, "delete_block_reason": "; ".join(reasons), "can_purge": can_purge,
            "purge_block_reason": purge_reason,
            "can_clear_selection": state["current_active"] and not state["recoverable_run"],
            "clear_selection_block_reason": clear_reason,
        }

    def _state(self, batch: dict) -> dict:
        batch_id = batch["id"]
        with self.service.task_store.connect() as db:
            versions_count = db.execute("SELECT COUNT(*) FROM material_versions WHERE batch_id=?", (batch_id,)).fetchone()[0]
            run_rows = db.execute("SELECT status FROM runs WHERE material_batch_id=?", (batch_id,)).fetchall()
            selected = db.execute("SELECT v.snapshot_id FROM material_selection s JOIN material_versions v "
                                  "ON v.id=s.version_id WHERE s.singleton=1 AND v.batch_id=?",
                                  (batch_id,)).fetchone()
        config_root = Path(self.service.configs.load().input_dir).expanduser().resolve()
        selected_snapshot = selected["snapshot_id"] if selected else ""
        expected_root = (self.service.published_root / selected_snapshot).resolve() if selected_snapshot else None
        current_active = bool(expected_root and config_root == expected_root)
        return {
            "versions_count": versions_count,
            "recoverable_run": any(row["status"] not in _TERMINAL_RUN_STATES for row in run_rows),
            "any_run_reference": bool(run_rows),
            "current_active": current_active,
        }

    def _readable(self, batch_id: str, *, include_deleted: bool = False) -> dict:
        batch = self.service._batch_row(batch_id, include_deleted=include_deleted)
        return self.service._batch_summary(batch)

    @staticmethod
    def _write_journal(path: Path, journal: dict) -> None:
        atomic_json(path, journal)

    def _recover_purge_staging(self) -> None:
        """只按 purge journal 恢复崩溃现场；没有明确记录时不猜目录归属。"""
        root = self.service.material_root / ".purge"
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            raise ValueError("清理暂存根目录异常，拒绝自动恢复")
        for staging in root.iterdir():
            if staging.is_symlink() or not staging.is_dir():
                raise ValueError("清理暂存目录包含非预期对象")
            journal_path = staging / "journal.json"
            if not journal_path.exists():
                if {child.name for child in staging.iterdir()} <= {"journal.json.tmp"}:
                    shutil.rmtree(staging)
                    continue
                raise ValueError("清理暂存目录缺少 journal，拒绝自动处理")
            if journal_path.is_symlink() or not journal_path.is_file():
                raise ValueError("清理 journal 类型异常")
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("清理 journal 无法读取，拒绝自动处理") from exc
            batch_id = journal.get("batch_id") if isinstance(journal, dict) else None
            if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
                raise ValueError("清理 journal 批次 ID 无效")
            if journal.get("phase") not in {"moving", "moved", "db_committed"}:
                raise ValueError("清理 journal 阶段无效")
            if not staging.name.startswith(batch_id + "-"):
                raise ValueError("清理 journal 与暂存目录不匹配")
            expected = {
                str(self.service.upload_root / batch_id): str(staging / "uploads"),
                str(self.service.batch_root / batch_id): str(staging / "batches"),
            }
            paths = journal.get("paths")
            if (not isinstance(paths, list) or len(paths) != 2
                    or any(not isinstance(item, dict) or not isinstance(item.get("original"), str)
                           or not isinstance(item.get("staged"), str) for item in paths)
                    or {item["original"]: item["staged"] for item in paths} != expected):
                raise ValueError("清理 journal 路径不在允许范围内")
            entries = {child.name for child in staging.iterdir()}
            if entries - {"journal.json", "journal.json.tmp", "uploads", "batches"}:
                raise ValueError("清理暂存目录包含未登记对象")
            for original, staged in expected.items():
                original_path, staged_path = Path(original), Path(staged)
                self._validate_managed_path(original_path, original_path.parent)
                self._validate_managed_path(staged_path, staging)
            with self.service.task_store.connect() as db:
                exists = db.execute("SELECT 1 FROM material_batches WHERE id=?", (batch_id,)).fetchone() is not None
            if exists:
                # DB仍有批次行表示提交未完成，将所有已移动目录还原。
                for original, staged in expected.items():
                    original_path, staged_path = Path(original), Path(staged)
                    if staged_path.exists():
                        if original_path.exists():
                            raise ValueError("恢复时原目录和暂存目录同时存在，拒绝覆盖")
                        staged_path.rename(original_path)
            # DB行已删除时提交完成；暂存目录只包含登记的托管路径，可安全清理。
            self._remove_staging_contents(staging)

    @staticmethod
    def _remove_staging_contents(staging: Path) -> None:
        """先删登记的数据目录，最后删 journal，确保中断后仍有恢复依据。"""
        for name in ("uploads", "batches"):
            child = staging / name
            if child.exists():
                if child.is_symlink() or not child.is_dir():
                    raise ValueError("清理暂存对象类型异常")
                shutil.rmtree(child)
        temporary = staging / "journal.json.tmp"
        if temporary.exists():
            if temporary.is_symlink() or not temporary.is_file():
                raise ValueError("清理 journal 临时文件类型异常")
            temporary.unlink()
        journal = staging / "journal.json"
        if journal.exists():
            if journal.is_symlink() or not journal.is_file():
                raise ValueError("清理 journal 类型异常")
            journal.unlink()
        staging.rmdir()

    @staticmethod
    def _validate_managed_path(path: Path, expected_parent: Path) -> None:
        # 校验目录本身和父级，拒绝 symlink 与解析后越界路径。
        if path.is_symlink() or expected_parent.is_symlink():
            raise ValueError("拒绝清理符号链接目录")
        if path.exists():
            if not path.is_dir() or path.resolve(strict=True).parent != expected_parent.resolve(strict=True):
                raise ValueError("托管目录类型或解析位置异常，拒绝清理")
            # 拒绝树内任何链接，避免清理过程触碰目标外的目录或文件。
            for child in path.rglob("*"):
                if child.is_symlink():
                    raise ValueError("托管目录包含符号链接，拒绝清理")

