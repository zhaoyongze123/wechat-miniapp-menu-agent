from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from menu_capture.config import ModelConfig
from menu_capture.model import ModelError, VisionModel
from menu_capture.model_usage import usage_context
from menu_capture.server import create_app


def _response(content='{"action":"wait","reason":"ok"}', *, usage=None, status=200):
    body = {"id": "req-123", "model": "returned-vl", "choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(status, headers={"x-request-id": "hdr-123"}, json=body)


def _model(handler, ledger=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return VisionModel(ModelConfig(base_url="https://example.test/v1", model="configured-vl"),
                       "test-secret", client=client, usage_ledger=ledger)


def test_request_persists_openai_usage_and_response_metadata_once(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return _response(usage={"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29,
                                "prompt_tokens_details": {"cached_tokens": 5, "image_tokens": 7},
                                "completion_tokens_details": {"reasoning_tokens": 3}})

    app = create_app(tmp_path)
    model = _model(handler, app.state.model_usage)
    try:
        assert model._request(Image.new("RGB", (20, 20), "red"), "synthetic prompt") == '{"action":"wait","reason":"ok"}'
        result = app.state.model_usage.query()
    finally:
        model.close()
    assert len(requests) == 1
    row = result["calls"][0]
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (21, 8, 29)
    assert (row["cached_tokens"], row["image_tokens"], row["reasoning_tokens"]) == (5, 7, 3)
    assert (row["returned_model"], row["request_id"], row["status"]) == ("returned-vl", "hdr-123", "success")


def test_http_error_usage_and_timeout_are_counted_without_sensitive_error_text(tmp_path):
    calls = {"count": 0}
    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return _response(status=429, usage={"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13})
        if calls["count"] == 2:
            return _response(usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        raise httpx.ReadTimeout("test-secret echoed with private prompt")
    app = create_app(tmp_path)
    model = _model(handler, app.state.model_usage)
    try:
        with pytest.raises(ModelError) as error:
            model._request(Image.new("RGB", (8, 8)), "private prompt")
        assert "test-secret" not in str(error.value) and "private prompt" not in str(error.value)
        assert model._request(Image.new("RGB", (8, 8)), "private prompt").startswith("{")
        with pytest.raises(ModelError):
            model._request(Image.new("RGB", (8, 8)), "private prompt")
        result = app.state.model_usage.query()
    finally:
        model.close()
    assert result["summary"]["calls"] == 3 and result["summary"]["error_calls"] == 2
    failed = next(row for row in result["calls"] if row["http_status"] == 429)
    timeout = next(row for row in result["calls"] if row["http_status"] is None)
    assert failed["http_status"] == 429 and failed["input_tokens"] == 11
    assert timeout["http_status"] is None and timeout["usage_state"] == "missing"
    assert result["summary"]["success_calls"] == 1 and result["summary"]["input_tokens"] == 18
    serialized = json.dumps(result)
    assert "test-secret" not in serialized and "private prompt" not in serialized


def test_usage_survives_response_format_failure(tmp_path):
    response = httpx.Response(
        200, headers={"x-request-id": "format-1"}, json={"id": "format-1", "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}, "choices": []})
    app = create_app(tmp_path)
    model = _model(lambda request: response, app.state.model_usage)
    try:
        with pytest.raises(ModelError, match="视觉消息格式"):
            model._request(Image.new("RGB", (8, 8)), "safe")
        row = app.state.model_usage.query()["calls"][0]
    finally:
        model.close()
    assert row["input_tokens"] == 4 and row["output_tokens"] == 6
    # 服务端 HTTP 成功、应用层消息格式失败；计量仍记录模型请求为失败。
    assert row["status"] == "error"
    assert row["error_type"] == "ResponseFormatError"


def test_connection_test_source_is_recorded_with_synthetic_color_image(tmp_path):
    def handler(request):
        # 用请求中生成的图片验证 test() 走的是独立色块，不依赖桌面截图。
        data_url = request.read().decode("utf-8")
        assert "data:image/jpeg;base64," in data_url
        return _response('{"color":"red"}', usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})

    app = create_app(tmp_path)
    model = _model(handler, app.state.model_usage)
    try:
        model.test()
        row = app.state.model_usage.query()["calls"][0]
    finally:
        model.close()
    assert row["source"] == "connection_test" and row["stage"] == "model_connection_test"


def test_trace_correlates_calls_per_action_and_survives_action_history_trim(tmp_path):
    app = create_app(tmp_path)
    runner = app.state.runner
    runner.run_id = "run-alpha"
    runner.current_task = {"id": "task-one", "source_name": "Shop A", "stage": "capture"}
    runner._trace_attempt, runner._trace_step = "attempt-1", 7
    runner._trace_material = {"material_batch_id": "batch-1", "material_version": "v1"}
    model = _model(lambda request: _response(usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}), app.state.model_usage)
    try:
        with runner._trace("model"):
            model._request(Image.new("RGB", (8, 8)), "safe")
        action_one = app.state.model_usage.query(run_id="run-alpha", task_id="task-one")["calls"][0]
        runner.current_task = {"id": "task-two", "source_name": "Shop B", "stage": "capture"}
        runner._trace_attempt, runner._trace_step = "attempt-2", 8
        with runner._trace("model"):
            model._request(Image.new("RGB", (8, 8)), "safe")
        action_two = app.state.model_usage.query(run_id="run-alpha", task_id="task-two")["calls"][0]
        assert action_one["action_id"] != action_two["action_id"]
        assert (action_one["run_id"], action_one["task_id"], action_one["attempt_id"], int(action_one["step"])) == ("run-alpha", "task-one", "attempt-1", 7)
        assert (action_two["run_id"], action_two["task_id"], action_two["attempt_id"], int(action_two["step"])) == ("run-alpha", "task-two", "attempt-2", 8)
        with app.state.store.connect() as db:
            db.execute("DELETE FROM run_actions")
        assert app.state.model_usage.query()["summary"]["calls"] == 2
    finally:
        model.close()


def test_usage_api_filters_pages_and_is_read_only(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    ledger = app.state.model_usage
    for run, task, source in (("r1", "t1", "capture"), ("r1", "t2", "capture"), ("r2", "t3", "connection_test")):
        cid = ledger.start_call(provider="fake", model="unit", context={"run_id": run, "task_id": task, "source": source})
        ledger.finish_call(cid, status="success", elapsed_ms=1, http_status=200,
                           response={"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    def forbidden(*args, **kwargs):
        raise AssertionError("统计 API 不应触发模型或环境检测")
    monkeypatch.setattr(app.state.runner, "start", forbidden)
    monkeypatch.setattr(app.state.environment, "diagnose", forbidden, raising=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        page = client.get("/api/model-usage", params={"source": "capture", "group_by": "task", "run_id": "r1", "page_size": 1})
        assert page.status_code == 200
        data = page.json()
        assert data["summary"]["calls"] == 2 and data["pagination"] == {"page": 1, "page_size": 1, "total": 2, "pages": 2}
        assert len(data["calls"]) == 1 and data["calls"][0]["task_id"] in {"t1", "t2"}
        assert all("api_key" not in json.dumps(row) and "prompt" not in json.dumps(row) for row in data["calls"])
        assert client.get("/api/model-usage", params={"page": 0}).status_code == 400
        assert client.get("/api/model-usage", params={"source": "invalid"}).status_code == 400
    assert ledger.query()["summary"]["calls"] == 3
