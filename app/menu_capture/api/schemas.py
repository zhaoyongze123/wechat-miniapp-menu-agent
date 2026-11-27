"""核心高频 API 的 Pydantic v2 响应契约模型。

目标：为高频轮询、工作台分页、素材/审核/分配等核心接口建立显式 OpenAPI Schema 契约，
防止字段漂移与隐性大包回归；统一采用 extra="allow" 保持前向扩展兼容。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _CompatModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class VersionResponse(_CompatModel):
    version: str = ""
    role: str = ""


class SessionResponse(_CompatModel):
    token: str


class StatusCountsModel(_CompatModel):
    rejected: int = 0
    returned: int = 0
    review: int = 0
    captured: int = 0
    running: int = 0
    pending: int = 0
    needs_review: int = 0
    attention: int = 0


class StatusSummaryResponse(_CompatModel):
    execution_engine: str
    launch_mode: str
    engine_ready: bool | None = None
    engine_message: str = ""
    running: bool = False
    stop_requested: bool = False
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    counts: StatusCountsModel
    material_rejected: int = 0
    material_rejected_all: int = 0
    events: list[dict[str, Any]] = Field(default_factory=list)


class StatusTasksResponse(_CompatModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    counts: StatusCountsModel
    selected: list[dict[str, Any]] = Field(default_factory=list)
    page: int
    pages: int
    total: int


class CaptureQueueResponse(_CompatModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    batches: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class SharedStatusResponse(_CompatModel):
    role: str


class SharedAssignmentsResponse(_CompatModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    page: int = 1
    pages: int = 1
    total: int = 0


class MaterialBatchesResponse(_CompatModel):
    batches: list[dict[str, Any]] = Field(default_factory=list)


class MaterialReworkResponse(_CompatModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class ReviewsPendingResponse(_CompatModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    batches: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class RejectionsListResponse(_CompatModel):
    records: list[dict[str, Any]] = Field(default_factory=list)


class ActiveTasksResponse(_CompatModel):
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class TaskHistoryResponse(_CompatModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0


class ProjectItemModel(_CompatModel):
    id: str
    code: str
    name: str


class SlowRequestAlert(_CompatModel):
    at: str
    method: str
    path: str
    status_code: int
    duration_ms: float
    response_bytes: int
    reasons: list[str]


class SlowRequestsResponse(_CompatModel):
    slow_ms_threshold: float
    large_bytes_threshold: int
    alerts: list[SlowRequestAlert] = Field(default_factory=list)
