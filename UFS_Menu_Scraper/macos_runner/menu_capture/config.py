from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = ROOT.parent.parent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(StrictModel):
    provider: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    base_url: str = ""
    model: str = ""
    timeout_seconds: int = Field(default=90, ge=10, le=300)
    # Qwen 等兼容端可按请求控制思考模式；None 表示不改变提供商原有默认行为。
    enable_thinking: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def discard_retired_navigation_prompt(cls, value):
        # 已保存的旧配置仍可加载；新版导航使用 Agent 自己的提示词和业务经验。
        if isinstance(value, dict) and "prompt" in value:
            return {key: item for key, item in value.items() if key != "prompt"}
        return value

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("接口地址应为 http(s) URL，不能包含用户名或密钥")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程模型接口需要 HTTPS；HTTP 仅供本机服务使用")
        return value


class CaptureConfig(StrictModel):
    window_width: int = Field(default=430, ge=320, le=1000)
    window_height: int = Field(default=900, ge=480, le=1800)
    overlap_ratio: float = Field(default=0.35, ge=0.2, le=0.7)
    max_pages: int = Field(default=120, ge=2, le=500)
    max_agent_steps: int = Field(default=18, ge=2, le=60)
    max_task_seconds: int = Field(default=900, ge=60, le=7200)
    settle_seconds: float = Field(default=1.2, ge=0.3, le=10)


class DebugLoggingConfig(StrictModel):
    max_storage_mb: int = Field(default=512, ge=64, le=4096)


class MaterialsConfig(StrictModel):
    # off: 保持原有方形二维码链路；assist: 圆形码只作辅助候选，不跳过第二轮；
    # priority: 圆形码命中后整店跳过第二轮（需独立标注集验收通过后再开启）。
    circle_detection: Literal["off", "assist", "priority"] = "off"


class Settings(StrictModel):
    input_dir: str = str(WORKSPACE_ROOT / "素材" / "原图")
    output_dir: str = str(WORKSPACE_ROOT / "输出" / "截图输出")
    model: ModelConfig = Field(default_factory=ModelConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    debug_logging: DebugLoggingConfig = Field(default_factory=DebugLoggingConfig)
    materials: MaterialsConfig = Field(default_factory=MaterialsConfig)
    people_count: int = Field(default=1, ge=1, le=12)
    auto_authorize: bool = False


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # 本地密钥和配置只允许当前用户读取，原子替换避免中断留下半份 JSON。
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    path.chmod(0o600)


class ConfigStore:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.path = root / "config.local.json"
        self.secret_path = root / "secrets.local.json"

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        return Settings.model_validate_json(self.path.read_text(encoding="utf-8"))

    def key(self) -> str:
        if not self.secret_path.exists():
            return ""
        return json.loads(self.secret_path.read_text(encoding="utf-8")).get("api_key", "")

    def public(self) -> dict:
        result = self.load().model_dump()
        result["model"]["api_key_configured"] = bool(self.key())
        return result

    def save(self, payload: dict) -> dict:
        payload = json.loads(json.dumps(payload))
        model = payload.setdefault("model", {})
        secret = model.pop("api_key", "")
        clear = model.pop("clear_api_key", False)
        model.pop("api_key_configured", None)
        settings = Settings.model_validate(payload)
        source = Path(settings.input_dir).expanduser().resolve() if settings.input_dir.strip() else None
        output = Path(settings.output_dir).expanduser().resolve()
        if source and (source == output or source in output.parents or output in source.parents):
            raise ValueError("素材目录和保存目录不能相同或互相包含，避免把结果当成新素材")
        # 清除采集版本后保留明确的未选择状态，不能把空字符串解析为进程工作目录。
        settings.input_dir, settings.output_dir = str(source) if source else "", str(output)
        atomic_json(self.path, settings.model_dump())
        if clear or secret:
            atomic_json(self.secret_path, {"api_key": "" if clear else secret.strip()})
        return self.public()
