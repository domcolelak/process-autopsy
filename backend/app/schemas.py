"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.ingestion.mapping import ColumnProfile, EventMapping


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- processes -------------------------------------------------------------


class ProcessCreate(BaseModel):
    name: str
    description: str = ""
    sla_hours: float | None = None


class ProcessOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str
    sla_hours: float | None
    last_analyzed_at: datetime | None


class ProcessListItem(ProcessOut):
    case_count: int = 0
    open_findings: int = 0
    median_throughput_hours: float = 0.0


# --- imports ---------------------------------------------------------------


class ImportOut(ORMModel):
    id: uuid.UUID
    filename: str
    status: str
    row_count: int
    accepted_count: int
    rejected_count: int
    detected_columns: list[Any] = Field(default_factory=list)
    sample_rows: list[Any] = Field(default_factory=list)
    errors: list[Any] = Field(default_factory=list)
    mapping: dict[str, Any] = Field(default_factory=dict)


class ImportProfileOut(BaseModel):
    import_id: uuid.UUID
    row_count: int
    columns: list[ColumnProfile]
    suggested_mapping: dict[str, str]
    sample_rows: list[dict[str, str]]
    warnings: list[str]


class ApplyMappingRequest(BaseModel):
    process_id: uuid.UUID | None = None
    process_name: str | None = None
    sla_hours: float | None = None
    mapping: EventMapping
    analyze: bool = True


class ApplyMappingResponse(BaseModel):
    import_id: uuid.UUID
    process_id: uuid.UUID
    accepted: int
    rejected: int
    errors: list[dict[str, Any]] = Field(default_factory=list)
    analysis: dict[str, Any] | None = None


# --- events ----------------------------------------------------------------


class EventIn(BaseModel):
    case_id: str
    activity_name: str
    occurred_at: datetime
    completed_at: datetime | None = None
    actor_id: str | None = None
    actor_type: str | None = None
    team: str | None = None
    source_system: str = "api"
    source_event_id: str | None = None
    duration_ms: int | None = None
    object_type: str | None = None
    object_id: str | None = None
    monetary_value: float | None = None
    status_before: str | None = None
    status_after: str | None = None
    is_manual: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventBatchRequest(BaseModel):
    process_id: uuid.UUID | None = None
    process_name: str | None = None
    events: list[EventIn] = Field(min_length=1, max_length=10_000)


class EventBatchResponse(BaseModel):
    process_id: uuid.UUID
    accepted: int
    duplicates: int


# --- analysis output -------------------------------------------------------


class GraphResponse(BaseModel):
    process_id: uuid.UUID
    case_count: int
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    start_activities: dict[str, int]
    end_activities: dict[str, int]


class VariantOut(BaseModel):
    variant_key: str
    sequence: list[str]
    case_count: int
    share: float
    median_throughput_seconds: float
    mean_throughput_seconds: float
    sla_breach_rate: float = 0.0
    mean_handoffs: float = 0.0
    rework_case_ratio: float = 0.0
    total_monetary_value: float = 0.0
    example_case_ids: list[str] = Field(default_factory=list)


class FindingOut(ORMModel):
    id: uuid.UUID
    process_id: uuid.UUID
    finding_type: str
    title: str
    severity: str
    evidence: dict[str, Any]
    affected_case_count: int
    metric_value: float
    baseline_value: float | None
    impact_hours_per_month: float
    impact_score: float
    confidence: float
    detected_at: datetime
    status: str
    narrative: dict[str, Any] = Field(default_factory=dict)


class FindingStatusUpdate(BaseModel):
    status: str = Field(pattern="^(open|acknowledged|in_progress|resolved|dismissed)$")


class OpportunityOut(ORMModel):
    id: uuid.UUID
    process_id: uuid.UUID
    activity_name: str
    score: float
    components: dict[str, Any]
    estimated_hours_per_month: float
    estimated_eur_per_month: float
    recommendation: dict[str, Any]
    status: str


class OpportunityStatusUpdate(BaseModel):
    status: str = Field(pattern="^(proposed|planned|in_progress|done|rejected)$")


class WorkspaceOverview(BaseModel):
    process_count: int
    case_count: int
    event_count: int
    open_findings: int
    recoverable_hours_per_month: float
    recoverable_eur_per_month: float
    top_finding: FindingOut | None = None
    top_opportunity: OpportunityOut | None = None
    worsening_processes: list[dict[str, Any]] = Field(default_factory=list)


class BeforeAfterRequest(BaseModel):
    split_at: datetime


class ReportRequest(BaseModel):
    process_id: uuid.UUID
    title: str | None = None
    include_ai_summary: bool = True


class ReportOut(ORMModel):
    id: uuid.UUID
    title: str
    format: str
    body: str
    payload: dict[str, Any]
    created_at: datetime
