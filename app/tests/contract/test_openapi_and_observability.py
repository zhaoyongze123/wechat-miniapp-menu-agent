"""OpenAPI 契约与运行时可观测性（慢请求/大响应体预警）契约测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from menu_capture.api.observability import ApiObservability
from menu_capture.api.server import create_app


def test_openapi_schema_exposes_typed_status_and_observability_contracts(tmp_path: Path) -> None:
    app = create_app(tmp_path / "hub", role="hub", admin_password="secret")
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})
    assert "StatusSummaryResponse" in components
    assert "StatusTasksResponse" in components
    assert "SlowRequestsResponse" in components

    paths = schema.get("paths", {})
    assert "/api/status/summary" in paths
    assert "/api/status/tasks" in paths
    assert "/api/diagnostics/slow-requests" in paths


def test_observability_middleware_adds_timing_header_and_records_alerts(tmp_path: Path) -> None:
    app = create_app(tmp_path / "hub", role="hub", admin_password="secret")
    auth = ("admin", "secret")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        resp = client.get("/api/diagnostics/slow-requests", auth=auth)
        assert resp.status_code == 200
        assert "x-response-time-ms" in resp.headers
        body = resp.json()
        assert body["slow_ms_threshold"] == 500.0
        assert body["large_bytes_threshold"] == 200 * 1024
        assert isinstance(body["alerts"], list)

        # 验证环形缓冲告警记录逻辑
        tracker: ApiObservability = app.state.observability
        tracker.record(method="GET", path="/api/status", status_code=200, duration_ms=1428.0, response_bytes=2_680_000)
        alerts = client.get("/api/diagnostics/slow-requests", auth=auth).json()["alerts"]
        assert len(alerts) >= 1
        latest = alerts[0]
        assert latest["path"] == "/api/status"
        assert set(latest["reasons"]) == {"slow", "large_payload"}
