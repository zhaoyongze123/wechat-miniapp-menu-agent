from __future__ import annotations

import base64
import io
import json
import logging
import re
import secrets
import time
import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from .config import ModelConfig
from .model_usage import usage_context


_logger = logging.getLogger(__name__)


class ModelError(RuntimeError):
    pass


class Region(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class VisionModel:
    def __init__(self, config: ModelConfig, key: str, client: httpx.Client | None = None, *, usage_ledger=None):
        if not config.base_url or not config.model or not key:
            raise ModelError("请先在配置页填写模型接口、模型名称和密钥")
        self.config, self.key = config, key
        self.usage_ledger = usage_ledger
        # 模型接口固定直连，避免继承终端/启动器中的代理导致 TLS 握手失败。
        self.client = client or httpx.Client(timeout=config.timeout_seconds, follow_redirects=False, trust_env=False)

    @property
    def coordinate_space(self) -> str:
        """返回已知模型族的坐标协议；未知模型保持既有像素协议。"""
        model_name = self.config.model.strip()
        if re.search(r"^qwen3-vl(?![a-z0-9])", model_name, re.IGNORECASE) or re.search(
            r"^qwen3\.[5-8](?![a-z0-9])", model_name, re.IGNORECASE
        ):
            return "normalized_999"
        return "image_pixels"

    def close(self):
        self.client.close()

    def _request(self, image: Image.Image, prompt: str) -> str:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=88)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        base = self.config.base_url
        if self.config.provider == "anthropic":
            url = base if base.endswith("/messages") else base + "/messages"
            headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
            payload = {"model": self.config.model, "max_tokens": 1400, "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": encoded}},
                {"type": "text", "text": prompt},
            ]}]}
        else:
            url = base if base.endswith("/chat/completions") else base + "/chat/completions"
            headers = {"Authorization": "Bearer " + self.key}
            payload = {"model": self.config.model, "max_tokens": 1400, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}},
            ]}]}
            if self.config.enable_thinking is not None:
                payload["enable_thinking"] = self.config.enable_thinking
        # 只在真正发出 HTTP 请求的入口计次；OCR、规则判断和读统计都不会产生调用。
        call_id = None
        if self.usage_ledger is not None:
            try:
                call_id = self.usage_ledger.start_call(provider=self.config.provider, model=self.config.model, endpoint=url)
            except Exception as exc:
                _logger.warning("模型用量记录未开始：%s", type(exc).__name__)
        started = time.monotonic()
        body, http_status, request_id, error_type = None, None, "", ""
        status = "error"
        try:
            try:
                response = self.client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                error_type = type(exc).__name__
                # 不保存请求、认证头或原始错误响应，避免网关回显密钥。
                raise ModelError(f"模型网络请求失败：{type(exc).__name__}") from None
            http_status = response.status_code
            request_id = response.headers.get("x-request-id", "") or response.headers.get("request-id", "")
            try:
                body = response.json()
            except ValueError:
                pass
            if http_status != 200:
                error_type = "HTTPError"
                raise ModelError(f"模型接口 HTTP {http_status}；请检查地址、额度和鉴权")
            try:
                if self.config.provider == "anthropic":
                    content = "\n".join(block["text"] for block in body["content"] if block.get("type") == "text")
                else:
                    content = body["choices"][0]["message"]["content"]
                    if not isinstance(content, str):
                        content = "\n".join(block["text"] for block in content if block.get("type") == "text")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                error_type = "ResponseFormatError"
                raise ModelError("模型响应不是支持的视觉消息格式；请确认接口兼容性") from None
            status = "success"
            return content
        except BaseException as exc:
            error_type = error_type or type(exc).__name__
            raise
        finally:
            if call_id is not None:
                try:
                    self.usage_ledger.finish_call(
                        call_id, status=status, elapsed_ms=round((time.monotonic() - started) * 1000),
                        http_status=http_status, response=body, request_id=request_id, error_type=error_type,
                    )
                except Exception as exc:
                    # 计量失败不重试模型，也不改变自动化执行结果；保留可诊断的错误类型。
                    _logger.warning("模型用量记录未完成：%s", type(exc).__name__)

    def test(self) -> dict:
        # 测试只发送程序生成的色块，不上传用户桌面、聊天或素材。
        palette = {"red": (240, 30, 30), "blue": (20, 50, 240), "green": (20, 190, 40), "yellow": (250, 220, 20)}
        expected = secrets.choice(list(palette))
        image = Image.new("RGB", (300, 200), "white")
        image.paste(palette[expected], (100, 50, 200, 150))
        with usage_context(source="connection_test", stage="model_connection_test"):
            text = self._request(image, '看图回答中心色块颜色。只返回 JSON 对象，color字段写英文颜色名，看不清写unknown。')
        try:
            parsed = parse_json(text)
            ok = parsed.get("color", "").lower() == expected
        except (ValueError, TypeError, AttributeError):
            ok = False
        return {"ok": ok, "message": "视觉接口测试通过：识别出测试色块" if ok else "接口有响应，但视觉格式/识别测试未通过；不能据此开始采集"}


def parse_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    elif stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3].strip()
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("响应必须为JSON对象")
    return value
