from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import threading
import time
import uuid
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any


_MEDIA_NAME = re.compile(r"^[0-9a-f]{32}\.png$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@"),
)


class DebugLog:
    """采集调试记录的有界异步落盘器；队列或磁盘故障不能影响桌面动作。"""

    SEGMENT_BYTES = 8 * 1024 * 1024
    QUEUE_SIZE = 128
    MAX_RECORD_BYTES = 512 * 1024
    CLI_TAIL_BYTES = 128 * 1024
    SERVICE_LOG_RESERVE = 4 * 1024 * 1024

    def __init__(self, data_dir: Path, max_storage_mb: int = 512):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "debug-logs"
        self.segments_dir = self.root / "segments"
        self.cli_dir = self.root / "cli"
        self.media_dir = self.data_dir / "action-evidence"
        for path in (self.root, self.segments_dir, self.cli_dir, self.media_dir):
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.chmod(0o700)
            except OSError:
                pass

        self._lock = threading.RLock()
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._max_bytes = max(64, int(max_storage_mb)) * 1024 * 1024
        self._used_bytes = 0
        self._reserved_bytes = 0
        self._dropped_records = 0
        self._dropped_evidence = 0
        self._current_path: Path | None = None
        self._current_stream = None
        self._current_size = 0
        self._segment_seq = 0
        self._action_index: dict[int, tuple[Path, int, int]] = {}
        self._pending_actions: set[int] = set()
        self._dropped_actions: set[int] = set()
        self._segment_actions: dict[Path, set[int]] = {}
        self._segment_media: dict[Path, set[str]] = {}
        self._media_ref_counts: dict[str, int] = {}
        self._pending_media: set[str] = set()
        self._cli_buffers: dict[str, tuple[deque[bytes], int]] = {}
        self._service_handler: logging.handlers.RotatingFileHandler | None = None
        self._closed = False
        # 服务中断留下的复制临时文件不属于实时 IPC，启动时回收，避免绕过空间统计。
        for directory in (self.segments_dir, self.cli_dir, self.media_dir):
            for temporary in directory.glob("*.tmp"):
                try:
                    temporary.unlink()
                except OSError:
                    pass
        self._scan_existing()
        self._prune_to_limit(0)
        self._worker = threading.Thread(target=self._write_loop, name="debug-log-writer", daemon=True)
        self._worker.start()

    @staticmethod
    def _safe_text(value: Any, limit: int = 6000) -> str:
        text = str(value or "")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(r"\1[REDACTED]", text)
        text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", text)
        text = re.sub(r"(验证码\s*[:：]?\s*)\d{4,8}", r"\1[已隐藏]", text)
        return text[:limit]

    def configure(self, max_storage_mb: int) -> None:
        with self._lock:
            self._max_bytes = max(64, int(max_storage_mb)) * 1024 * 1024
            self._resize_service_log_limit_locked()
            self._prune_to_limit(0)

    def emit_action(self, record: dict) -> bool:
        """只入有界内存队列；序列化、轮转、清理和落盘由后台线程完成。"""
        if self._closed:
            return False
        payload = self._bound_value(record)
        if not isinstance(payload, dict):
            return False
        payload.setdefault("record_id", uuid.uuid4().hex)
        payload.setdefault("time", datetime.now().astimezone().isoformat(timespec="milliseconds"))
        payload["kind"] = "action"
        media_refs = self._media_refs(payload.get("details", {}))
        try:
            with self._lock:
                self._pending_media.update(media_refs)
                if payload.get("action_id") is not None:
                    self._pending_actions.add(int(payload["action_id"]))
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            with self._lock:
                self._pending_media.difference_update(media_refs)
                if payload.get("action_id") is not None:
                    action_id = int(payload["action_id"])
                    self._pending_actions.discard(action_id)
                    self._dropped_actions.add(action_id)
                self._dropped_records += 1
            return False
        except Exception:
            with self._lock:
                self._pending_media.difference_update(media_refs)
                if payload.get("action_id") is not None:
                    action_id = int(payload["action_id"])
                    self._pending_actions.discard(action_id)
                    self._dropped_actions.add(action_id)
                self._dropped_records += 1
            return False

    def emit(self, kind: str, **fields) -> bool:
        if self._closed:
            return False
        record = self._bound_value({"kind": kind, "time": datetime.now().astimezone().isoformat(timespec="milliseconds"), **fields})
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            with self._lock:
                self._dropped_records += 1
            return False

    def save_image(self, source: Path) -> str | None:
        """打开源帧后排入后台复制；文件句柄可跨任务目录重命名继续读取。"""
        stream = None
        try:
            source = Path(source)
            stream = source.open("rb")
            size = os.fstat(stream.fileno()).st_size
            if size <= 0:
                stream.close()
                with self._lock:
                    self._dropped_evidence += 1
                return None
            name = uuid.uuid4().hex + ".png"
            with self._lock:
                if not self._make_room_locked(size):
                    stream.close()
                    self._dropped_evidence += 1
                    return None
                self._reserved_bytes += size
                self._pending_media.add(name)
            try:
                self._queue.put_nowait({"_op": "save_image", "name": name, "size": size, "stream": stream})
            except queue.Full:
                with self._lock:
                    self._reserved_bytes = max(0, self._reserved_bytes - size)
                    self._pending_media.discard(name)
                    self._dropped_evidence += 1
                stream.close()
                return None
            return name
        except (OSError, ValueError):
            if stream is not None:
                stream.close()
            with self._lock:
                self._dropped_evidence += 1
            return None

    def action_record(self, action_id: int) -> dict | None:
        with self._lock:
            location = self._action_index.get(int(action_id))
        if not location:
            return None
        path, offset, length = location
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                payload = json.loads(stream.read(length))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def action_state(self, action_id: int) -> str:
        with self._lock:
            if int(action_id) in self._action_index:
                return "ready"
            if int(action_id) in self._pending_actions:
                return "pending"
            if int(action_id) in self._dropped_actions:
                return "dropped"
            return "expired"

    def summary(self, max_storage_mb: int | None = None) -> dict:
        # 仅在用户打开日志面板或手动刷新时扫描一次，动作热路径不遍历目录。
        with self._lock:
            used = self._scan_size()
            managed = self._scan_managed_size()
            file_count = sum(1 for base in (self.root, self.media_dir) if base.exists()
                             for path in base.rglob("*") if path.is_file() and not path.name.endswith(".tmp"))
            return {
                "max_storage_mb": max_storage_mb if max_storage_mb is not None else self._max_bytes // (1024 * 1024),
                "used_bytes": used,
                "managed_bytes": managed,
                "reserved_bytes": self._reserved_bytes,
                "total_bytes": used + self._reserved_bytes,
                "file_count": file_count,
                "dropped_records": self._dropped_records,
                "dropped_evidence": self._dropped_evidence,
            }

    def clear(self) -> dict:
        """清理已落盘调试日志与证据；不触碰任务库、采集输出、来源素材或实时 IPC。"""
        self.flush(timeout=5)
        with self._lock:
            self._close_segment()
            for path in list(self.segments_dir.glob("*.jsonl")) + list(self.cli_dir.glob("*.log")):
                self._delete_file(path)
            for path in self.media_dir.iterdir():
                if path.is_file() and not path.name.endswith(".tmp"):
                    self._delete_file(path)
            self._action_index.clear()
            self._pending_actions.clear()
            self._dropped_actions.clear()
            self._segment_actions.clear()
            self._segment_media.clear()
            self._media_ref_counts.clear()
            self._pending_media.clear()
            self._used_bytes = self._scan_managed_size()
        return self.summary()

    def add_cli_output(self, run_key: str, chunk: bytes) -> None:
        """UiPath CLI 输出由独立读线程排空，仅保留有界尾部，避免 PIPE 阻塞或无限日志。"""
        if not chunk:
            return
        with self._lock:
            chunks, length = self._cli_buffers.setdefault(str(run_key), (deque(), 0))
            chunks.append(bytes(chunk))
            length += len(chunk)
            while length > self.CLI_TAIL_BYTES and chunks:
                excess = length - self.CLI_TAIL_BYTES
                first = chunks[0]
                if len(first) <= excess:
                    length -= len(chunks.popleft())
                else:
                    chunks[0] = first[excess:]
                    length -= excess
            self._cli_buffers[str(run_key)] = (chunks, length)

    def finish_cli_output(self, run_key: str) -> str | None:
        with self._lock:
            value = self._cli_buffers.pop(str(run_key), None)
        if not value:
            return None
        chunks, _ = value
        text = b"".join(chunks).decode("utf-8", errors="replace")
        safe = self._safe_text(text, self.CLI_TAIL_BYTES).encode("utf-8")
        path = self.cli_log_path(run_key)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            if not self._make_room_locked(len(safe)):
                self._dropped_records += 1
                return None
            try:
                temporary.write_bytes(safe)
                os.chmod(temporary, 0o600)
                temporary.replace(path)
                self._used_bytes += path.stat().st_size
            except OSError:
                temporary.unlink(missing_ok=True)
                self._dropped_records += 1
                return None
        self.emit("uipath_cli_output_saved", run_id=str(run_key), file=path.name, bytes=len(safe))
        return str(path)

    def export_run(self, run_id: str, destination: Path) -> Path:
        """按需导出单轮动作、UiPath CLI 尾部和已关联截图，不复制来源素材或采集结果。"""
        destination = Path(destination)
        self.flush(timeout=1)
        image_names: set[str] = set()
        action_lines: list[bytes] = []
        for path in sorted(self.segments_dir.glob("*.jsonl")):
            try:
                with path.open("rb") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if row.get("run_id") == run_id:
                            action_lines.append(line)
                            image_names.update(self._media_refs(row.get("details", {})))
            except OSError:
                continue
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if action_lines:
                archive.writestr("actions.jsonl", b"".join(action_lines))
            cli_path = self.cli_log_path(run_id)
            if cli_path.is_file():
                try:
                    archive.write(cli_path, "uipath-cli.log")
                except FileNotFoundError:
                    pass
            for name in sorted(image_names):
                image = self.media_dir / name
                if image.is_file():
                    try:
                        archive.write(image, "evidence/" + name)
                    except FileNotFoundError:
                        pass
        return destination

    def flush(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + max(0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        with self._lock:
            if self._current_stream:
                try:
                    self._current_stream.flush()
                except OSError:
                    pass

    def close(self) -> None:
        if self._closed:
            return
        self.flush(timeout=5)
        self._closed = True
        try:
            self._queue.put(None, timeout=5)
        except queue.Full:
            self._closed = False
            return
        self._worker.join(timeout=5)
        if not self._worker.is_alive():
            with self._lock:
                self._close_segment()

    def _write_loop(self) -> None:
        while True:
            try:
                record = self._queue.get(timeout=0.25)
            except queue.Empty:
                with self._lock:
                    if self._current_stream:
                        try:
                            self._current_stream.flush()
                        except OSError:
                            self._dropped_records += 1
                continue
            if record is None:
                self._queue.task_done()
                break
            try:
                if record.get("_op") == "save_image":
                    self._write_image(record)
                else:
                    self._write_record(record)
            except Exception:
                # 诊断日志始终是旁路能力，不能让记录失败变成采集失败。
                with self._lock:
                    self._dropped_records += 1
                    if record.get("action_id") is not None:
                        self._dropped_actions.add(int(record["action_id"]))
                        self._pending_actions.discard(int(record["action_id"]))
                    if record.get("_op") == "save_image":
                        self._reserved_bytes = max(0, self._reserved_bytes - int(record.get("size", 0)))
                        self._pending_media.discard(str(record.get("name", "")))
                        try:
                            record.get("stream").close()
                        except Exception:
                            pass
            finally:
                with self._lock:
                    self._pending_media.difference_update(self._media_refs(record.get("details", {})))
                    if record.get("action_id") is not None:
                        self._pending_actions.discard(int(record["action_id"]))
                self._queue.task_done()

    def _write_image(self, record: dict) -> None:
        name, size, source = record["name"], int(record["size"]), record["stream"]
        temporary = self.media_dir / (name + ".tmp")
        destination = self.media_dir / name
        try:
            with source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, 0o600)
            actual_size = temporary.stat().st_size
            temporary.replace(destination)
            with self._lock:
                self._reserved_bytes = max(0, self._reserved_bytes - size)
                self._used_bytes += actual_size
                self._prune_to_limit(0)
        except Exception:
            source.close()
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            with self._lock:
                self._reserved_bytes = max(0, self._reserved_bytes - size)
                self._pending_media.discard(name)
                self._dropped_evidence += 1

    def _write_record(self, record: dict) -> None:
        safe = self._sanitize_record(record)
        line = self._encode_record(safe)
        if len(line) > self.MAX_RECORD_BYTES:
            safe = self._shrink_record(safe)
            line = self._encode_record(safe)
        action_id = safe.get("action_id")
        media_refs = self._media_refs(safe.get("details", {}))
        with self._lock:
            if self._current_stream and self._current_size + len(line) > self.SEGMENT_BYTES:
                self._close_segment()
            if not self._make_room_locked(len(line)):
                self._dropped_records += 1
                if action_id is not None:
                    self._dropped_actions.add(int(action_id))
                return
            if self._current_stream is None:
                self._open_segment()
            assert self._current_stream is not None and self._current_path is not None
            offset = self._current_size
            self._current_stream.write(line)
            self._current_size += len(line)
            self._used_bytes += len(line)
            if action_id is not None:
                try:
                    action_id = int(action_id)
                    self._action_index[action_id] = (self._current_path, offset, len(line))
                    self._segment_actions.setdefault(self._current_path, set()).add(action_id)
                except (TypeError, ValueError):
                    pass
            if media_refs:
                refs = self._segment_media.setdefault(self._current_path, set())
                for name in media_refs - refs:
                    refs.add(name)
                    self._media_ref_counts[name] = self._media_ref_counts.get(name, 0) + 1
            if self._current_size >= self.SEGMENT_BYTES:
                self._current_stream.flush()
                self._close_segment()

    def _sanitize_record(self, record: dict) -> dict:
        safe = dict(record)
        details = safe.get("details")
        if isinstance(details, dict):
            safe["details"] = self._sanitize_value(details)
        for key in ("error_message", "error_stack", "output"):
            if key in safe:
                safe[key] = self._safe_text(safe[key], 16000 if key == "error_stack" else 6000)
        return self._sanitize_value(safe)

    def _sanitize_value(self, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "[省略：嵌套过深]"
        if isinstance(value, dict):
            safe = {}
            for key, item in list(value.items())[:500]:
                name = str(key)[:80]
                if re.fullmatch(r"(?i)(?:api[_-]?key|authorization|cookie|password|secret|access_token|refresh_token|base64|image_base64|captcha|otp)", name):
                    safe[name] = "[REDACTED]"
                elif name == "error_stack" and isinstance(item, str):
                    safe[name] = self._safe_text(item, 16000)
                else:
                    safe[name] = self._sanitize_value(item, depth + 1)
            return safe
        if isinstance(value, (list, tuple)):
            rows = [self._sanitize_value(item, depth + 1) for item in value[:500]]
            if len(value) > 500:
                rows.append({"omitted_items": len(value) - 500})
            return rows
        if isinstance(value, str):
            return self._safe_text(value, 2000)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._safe_text(value, 500)

    @classmethod
    def _bound_value(cls, value: Any, depth: int = 0) -> Any:
        """入队前限制对象规模，避免有界队列仍被少数超大 OCR 结果占满内存。"""
        if depth > 8:
            return "[省略：嵌套过深]"
        if isinstance(value, dict):
            return {str(key)[:80]: (item[:16000] if str(key) == "error_stack" and isinstance(item, str)
                                  else cls._bound_value(item, depth + 1))
                    for key, item in list(value.items())[:500]}
        if isinstance(value, (list, tuple)):
            rows = [cls._bound_value(item, depth + 1) for item in value[:500]]
            if len(value) > 500:
                rows.append({"omitted_items": len(value) - 500})
            return rows
        if isinstance(value, str):
            limit = 16_000 if depth and len(value) > 6_000 else 2_000
            return value[:limit]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:500]

    def _shrink_record(self, record: dict) -> dict:
        reduced = dict(record)
        details = dict(reduced.get("details") or {})
        rows = details.get("ocr_rows")
        if isinstance(rows, list) and len(rows) > 100:
            details["ocr_rows"] = rows[:100]
            details["ocr_rows_omitted"] = len(rows) - 100
        reduced["details"] = details
        reduced["record_truncated"] = True
        if len(self._encode_record(reduced)) > self.MAX_RECORD_BYTES:
            for key in ("ocr_rows", "matched_ocr", "error_stack"):
                details.pop(key, None)
                reduced["details"] = details
                if len(self._encode_record(reduced)) <= self.MAX_RECORD_BYTES:
                    break
        if len(self._encode_record(reduced)) > self.MAX_RECORD_BYTES:
            # 无论未知详情来自哪层，都保证单条记录有硬上限，并保留排错入口和截图引用。
            reduced["details"] = {
                "detail_truncated": True,
                "error_type": details.get("error_type"),
                "error_message": str(details.get("error_message", ""))[:2000],
                "error_stack": str(details.get("error_stack", ""))[:16000],
                "evidence": sorted(self._media_refs(details)),
            }
            for key, value in list(reduced.items()):
                if key != "details" and isinstance(value, (dict, list, tuple)):
                    reduced[key] = "[省略：记录过大]"
        return reduced

    @staticmethod
    def _encode_record(record: dict) -> bytes:
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8") + b"\n"

    @staticmethod
    def _media_refs(value: Any) -> set[str]:
        found: set[str] = set()
        def visit(item):
            if isinstance(item, dict):
                for child in item.values():
                    visit(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    visit(child)
            elif isinstance(item, str) and _MEDIA_NAME.fullmatch(item):
                found.add(item)
        visit(value)
        return found

    def _scan_existing(self) -> None:
        with self._lock:
            self._used_bytes = self._scan_managed_size()
            for path in sorted(self.segments_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
                self._index_segment(path)

    def _scan_size(self) -> int:
        total = 0
        for base in (self.root, self.media_dir):
            if base.exists():
                for path in base.rglob("*"):
                    try:
                        if path.is_file() and not path.name.endswith(".tmp"):
                            total += path.stat().st_size
                    except OSError:
                        pass
        return total

    def _scan_managed_size(self) -> int:
        """调试日志自有文件；service.log 由固定 4 MiB 配额单独预留。"""
        total = 0
        for base in (self.root, self.media_dir):
            if not base.exists():
                continue
            for path in base.rglob("*"):
                try:
                    if not path.is_file() or path.name.endswith(".tmp"):
                        continue
                    if path.parent == self.root and path.name in {"service.log", "service.log.1"}:
                        continue
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def _index_segment(self, path: Path) -> None:
        actions: set[int] = set()
        refs: set[str] = set()
        try:
            with path.open("rb") as stream:
                while True:
                    offset = stream.tell()
                    line = stream.readline(self.MAX_RECORD_BYTES + 1)
                    if not line:
                        break
                    if not line.endswith(b"\n") or len(line) > self.MAX_RECORD_BYTES:
                        continue
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if record.get("action_id") is not None:
                        action_id = int(record["action_id"])
                        self._action_index[action_id] = (path, offset, len(line))
                        actions.add(action_id)
                    refs.update(self._media_refs(record.get("details", {})))
        except OSError:
            return
        self._segment_actions[path] = actions
        self._segment_media[path] = refs
        for name in refs:
            self._media_ref_counts[name] = self._media_ref_counts.get(name, 0) + 1

    def _open_segment(self) -> None:
        self._segment_seq += 1
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        path = self.segments_dir / f"{stamp}-{self._segment_seq:04d}.jsonl"
        self._current_stream = path.open("ab", buffering=0)
        os.chmod(path, 0o600)
        self._current_path = path
        self._current_size = path.stat().st_size
        self._segment_actions.setdefault(path, set())
        self._segment_media.setdefault(path, set())

    def _close_segment(self) -> None:
        if self._current_stream:
            try:
                self._current_stream.flush()
                self._current_stream.close()
            except OSError:
                pass
        self._current_stream = None
        self._current_path = None
        self._current_size = 0

    def _make_room(self, size: int) -> bool:
        with self._lock:
            return self._make_room_locked(size)

    def _make_room_locked(self, size: int) -> bool:
        capacity = max(0, self._max_bytes - (self.SERVICE_LOG_RESERVE if self._service_handler else 0))
        if size < 0 or size > capacity:
            return False
        self._prune_to_limit(size)
        return self._used_bytes + self._reserved_bytes + size <= capacity

    def _prune_to_limit(self, additional: int) -> None:
        service_reserve = self.SERVICE_LOG_RESERVE if self._service_handler else 0
        target = max(0, self._max_bytes - service_reserve - additional)
        if self._used_bytes + self._reserved_bytes <= target:
            return
        segments = sorted(self.segments_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
        for path in segments:
            if self._used_bytes + self._reserved_bytes <= target:
                break
            if path == self._current_path:
                continue
            self._delete_file(path)
            for action_id in self._segment_actions.pop(path, set()):
                self._action_index.pop(action_id, None)
            for name in self._segment_media.pop(path, set()):
                count = max(0, self._media_ref_counts.get(name, 0) - 1)
                if count:
                    self._media_ref_counts[name] = count
                else:
                    self._media_ref_counts.pop(name, None)
                    media = self.media_dir / name
                    if name not in self._pending_media:
                        self._delete_file(media)

        media_files = sorted((p for p in self.media_dir.iterdir() if p.is_file() and not p.name.endswith(".tmp")),
                             key=lambda p: p.stat().st_mtime if p.exists() else 0)
        for path in media_files:
            if self._used_bytes + self._reserved_bytes <= target:
                break
            if path.name not in self._pending_media and self._media_ref_counts.get(path.name, 0) == 0:
                self._delete_file(path)
        cli_files = sorted((p for p in self.cli_dir.glob("*.log") if p.is_file()),
                           key=lambda p: p.stat().st_mtime if p.exists() else 0)
        for path in cli_files:
            if self._used_bytes + self._reserved_bytes <= target:
                break
            self._delete_file(path)
        # 日志文件仍未腾出空间时，关闭当前分段并允许淘汰它，保证总量有硬上限。
        if self._used_bytes + self._reserved_bytes > target and self._current_path is not None:
            self._close_segment()
            self._prune_to_limit(additional)

    def _delete_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            self._used_bytes = max(0, self._used_bytes - size)
        except OSError:
            pass

    def cli_log_path(self, run_key: str) -> Path:
        return self.cli_dir / (re.sub(r"[^a-zA-Z0-9._-]", "_", str(run_key))[:100] + ".log")

    def install_service_logging(self) -> None:
        """LaunchAgent 不再维护无限增长的 stdout/stderr 文件，服务日志使用受限轮转文件。"""
        path = self.root / "service.log"
        max_bytes = min(2 * 1024 * 1024, max(256 * 1024, self._max_bytes // 16))
        try:
            handler = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=1, encoding="utf-8")
        except OSError:
            self._dropped_records += 1
            return
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        self._service_handler = handler
        for name in ("uvicorn", "uvicorn.error"):
            logging.getLogger(name).propagate = True
        try:
            path.chmod(0o600)
        except OSError:
            pass
        with self._lock:
            self._resize_service_log_limit_locked()
            self._prune_to_limit(0)

    def _resize_service_log_limit_locked(self) -> None:
        handler = self._service_handler
        if handler is None:
            return
        handler.maxBytes = min(2 * 1024 * 1024, max(256 * 1024, self._max_bytes // 16))
        try:
            if handler.stream is not None and os.fstat(handler.stream.fileno()).st_size > handler.maxBytes:
                handler.doRollover()
        except OSError:
            pass

