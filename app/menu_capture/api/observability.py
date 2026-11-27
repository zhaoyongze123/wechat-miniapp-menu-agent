"""轻量级 API 运行时可观测性中间件（慢请求与大响应体自动预警）。

设计约束：
- 零外部依赖（不搭 Prometheus/ELK），仅使用标准库 logging 与有界内存环形队列；
- 为每个 /api/* 响应附加 X-Response-Time-Ms 诊断头；
- 超过阈值（默认耗时 >= 500ms 或响应体 >= 200KB）自动输出结构化 WARNING 并记入最近告警。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request

logger = logging.getLogger("menu_capture.api.observability")


class ApiObservability:
    """记录慢请求与大响应体告警的有界环形缓冲，线程安全。"""

    def __init__(self, *, slow_ms: float = 500.0, large_bytes: int = 200 * 1024, max_entries: int = 50) -> None:
        self.slow_ms = float(slow_ms)
        self.large_bytes = int(large_bytes)
        self._lock = threading.Lock()
        self._alerts: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_entries)))

    def record(self, *, method: str, path: str, status_code: int, duration_ms: float, response_bytes: int) -> None:
        reasons: list[str] = []
        if duration_ms >= self.slow_ms:
            reasons.append("slow")
        if response_bytes >= self.large_bytes:
            reasons.append("large_payload")
        if not reasons:
            return
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 1),
            "response_bytes": response_bytes,
            "reasons": reasons,
        }
        with self._lock:
            self._alerts.append(entry)
        logger.warning(
            "api_perf_alert reasons=%s method=%s path=%s status=%d duration_ms=%.1f bytes=%d",
            ",".join(reasons), method, path, status_code, duration_ms, response_bytes,
        )

    def recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._alerts)
        return items[-max(1, min(limit, len(items) or 1)):][::-1]


def install_observability(
    app: FastAPI,
    *,
    slow_ms: float = 500.0,
    large_bytes: int = 200 * 1024,
) -> ApiObservability:
    tracker = ApiObservability(slow_ms=slow_ms, large_bytes=large_bytes)
    app.state.observability = tracker

    @app.middleware("http")
    async def _observe_api_requests(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        started = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - started) * 1000.0
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
        raw_len = response.headers.get("content-length")
        response_bytes = int(raw_len) if raw_len and raw_len.isdigit() else 0
        tracker.record(
            method=request.method,
            path=path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            response_bytes=response_bytes,
        )
        return response

    return tracker
