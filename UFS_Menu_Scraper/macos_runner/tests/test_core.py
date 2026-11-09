from __future__ import annotations

import base64
import json
import stat
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from menu_capture.config import ConfigStore, ModelConfig
from menu_capture.model import ModelError, VisionModel
from menu_capture.store import TaskStore


def settings_payload(input_dir: Path, output_dir: Path, *, api_key: str = "") -> dict:
    return {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "model": {
            "provider": "openai_compatible",
            "base_url": "https://vision.example/v1",
            "model": "vision-test",
            "api_key": api_key,
        },
    }


def test_config_empty_key_preserves_saved_secret_and_clear_removes_it(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "settings")
    payload = settings_payload(tmp_path / "inbox", tmp_path / "output", api_key="  secret-value  ")

    first = store.save(payload)
    assert store.key() == "secret-value"
    assert first["model"]["api_key_configured"] is True
    assert "api_key" not in first["model"]
    assert "secret-value" not in store.path.read_text(encoding="utf-8")

    payload["model"]["api_key"] = ""
    second = store.save(payload)
    assert store.key() == "secret-value"
    assert second["model"]["api_key_configured"] is True

    payload["model"]["clear_api_key"] = True
    cleared = store.save(payload)
    assert store.key() == ""
    assert cleared["model"]["api_key_configured"] is False


def test_config_files_are_private_and_nested_input_output_paths_are_rejected(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "settings")
    store.save(settings_payload(tmp_path / "inbox", tmp_path / "output", api_key="private"))

    for path in (store.path, store.secret_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with pytest.raises(ValueError, match="不能相同或互相包含"):
        store.save(settings_payload(tmp_path / "root", tmp_path / "root" / "output"))

    with pytest.raises(ValueError, match="不能相同或互相包含"):
        store.save(settings_payload(tmp_path / "root" / "input", tmp_path / "root"))


def test_saved_legacy_navigation_prompt_does_not_block_config_load(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    payload = settings_payload(tmp_path / "inbox", tmp_path / "output")
    payload["model"].pop("api_key")
    payload["model"]["prompt"] = "旧导航提示词"
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.load().model.model == "vision-test"
    assert "prompt" not in store.public()["model"]


def write_image(path: Path, color: str = "red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), color).save(path)


def test_scan_is_idempotent_for_the_same_source_file(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    write_image(source / "Cafe" / "menu.png")
    store = TaskStore(tmp_path / "tasks.sqlite3")

    assert store.scan(source) == {"added": 1, "total": 1}
    assert store.scan(source) == {"added": 0, "total": 1}
    assert len(store.tasks()) == 1


def test_same_named_sources_keep_distinct_task_identity(tmp_path: Path) -> None:
    source_a, source_b = tmp_path / "batch-a", tmp_path / "batch-b"
    write_image(source_a / "Same Shop" / "menu.png")
    write_image(source_b / "Same Shop" / "menu.png")
    store = TaskStore(tmp_path / "tasks.sqlite3")

    assert store.scan(source_a) == {"added": 1, "total": 1}
    assert store.scan(source_b) == {"added": 1, "total": 2}
    tasks = store.tasks()
    assert len({task["id"] for task in tasks}) == 2
    assert {task["source_name"] for task in tasks} == {"Same Shop"}
    assert {task["source_path"] for task in tasks} == {
        str(source_a / "Same Shop" / "menu.png"),
        str(source_b / "Same Shop" / "menu.png"),
    }


def test_recover_marks_interrupted_running_tasks_for_review(tmp_path: Path) -> None:
    source = tmp_path / "inbox"
    write_image(source / "Cafe" / "menu.png")
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.scan(source)
    task = store.tasks()[0]
    store.update(task["id"], status="running", stage="capturing", message="in progress")

    store.recover()

    recovered = store.get(task["id"])
    assert recovered["status"] == "needs_review"
    assert recovered["message"] == "上次执行中断，请检查现有截图后重试"


def make_model(provider: str, handler, *, enable_thinking: bool | None = None) -> tuple[VisionModel, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(capture))
    model = VisionModel(
        ModelConfig(
            provider=provider,
            base_url="https://vision.example/v1",
            model="vision-test",
            enable_thinking=enable_thinking,
        ),
        "test-secret",
        client=client,
    )
    return model, requests


def decoded_image(request_json: dict, provider: str) -> Image.Image:
    if provider == "anthropic":
        encoded = request_json["messages"][0]["content"][0]["source"]["data"]
    else:
        image_url = request_json["messages"][0]["content"][1]["image_url"]["url"]
        prefix = "data:image/jpeg;base64,"
        assert image_url.startswith(prefix)
        encoded = image_url[len(prefix) :]
    image_bytes = base64.b64decode(encoded, validate=True)
    image = Image.open(BytesIO(image_bytes))
    image.load()
    return image


@pytest.mark.parametrize("provider", ["openai_compatible", "anthropic"])
def test_model_sends_provider_specific_image_as_base64(provider: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if provider == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]}, request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )

    model, requests = make_model(provider, respond)
    try:
        result = model._request(Image.new("RGB", (37, 19), "#148b78"), "inspect this image")
    finally:
        model.close()

    assert result == "ok"
    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.content)
    if provider == "anthropic":
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert body["messages"][0]["content"][1] == {"type": "text", "text": "inspect this image"}
    else:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert body["messages"][0]["content"][0] == {"type": "text", "text": "inspect this image"}
    assert "enable_thinking" not in body
    assert body["model"] == "vision-test"
    assert decoded_image(body, provider).size == (37, 19)


@pytest.mark.parametrize("enabled", [True, False])
def test_model_passes_explicit_thinking_setting_only_to_openai_compatible(enabled: bool) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=request)

    model, requests = make_model("openai_compatible", respond, enable_thinking=enabled)
    try:
        model._request(Image.new("RGB", (8, 8), "white"), "test")
    finally:
        model.close()

    body = json.loads(requests[0].content)
    assert body["enable_thinking"] is enabled


def test_anthropic_never_receives_openai_thinking_parameter() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]}, request=request)

    model, requests = make_model("anthropic", respond, enable_thinking=False)
    try:
        model._request(Image.new("RGB", (8, 8), "white"), "test")
    finally:
        model.close()

    assert "enable_thinking" not in json.loads(requests[0].content)


def test_model_thinking_option_defaults_to_none() -> None:
    assert ModelConfig().enable_thinking is None


def test_model_http_auth_error_does_not_echo_secret() -> None:
    secret = "sensitive-token-value"

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"gateway rejected {secret}", request=request)

    model, _ = make_model("openai_compatible", unauthorized)
    model.key = secret
    try:
        with pytest.raises(ModelError) as exc_info:
            model._request(Image.new("RGB", (8, 8), "white"), "test")
    finally:
        model.close()

    assert "HTTP 401" in str(exc_info.value)
    assert secret not in str(exc_info.value)


