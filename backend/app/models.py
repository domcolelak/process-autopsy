"""SQLAlchemy models.

Every business table carries ``tenant_id`` and every query in the application
layer filters on it -- tenant isolation is enforced in code, not by convention.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import GUID, Base, JSONColumn, TimestampMixin, utcnow


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    api_key_hash: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    hourly_cost_eur: Mapped[float] = mapped_column(Float, default=35.0)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(32), default="analyst")

    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)


class DataSource(Base, TimestampMixin):
    __tablename__ = "data_sources"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    system: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default="csv")
    #: Connector secrets are stored through an encryption hook, never in clear text.
    secret_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    config: Mapped[dict] = mapped_column(JSONColumn, default=dict)


class Import(Base, TimestampMixin):
    __tablename__ = "imports"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("data_sources.id"), default=None
    )
    filename: Mapped[str] = mapped_column(String(400), default="")
    status: Mapped[str] = mapped_column(String(32), default="received")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    mapping: Mapped[dict] = mapped_column(JSONColumn, default=dict)
    detected_columns: Mapped[list] = mapped_column(JSONColumn, default=list)
    sample_rows: Mapped[list] = mapped_column(JSONColumn, default=list)
    errors: Mapped[list] = mapped_column(JSONColumn, default=list)


class RawEvent(Base):
    """Untouched source payload, kept for replay and mapping changes."""

    __tablename__ = "raw_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    import_id: Mapped[uuid.UUID | None] = mapped_column(GUID, ForeignKey("imports.id"), default=None)
    source_system: Mapped[str] = mapped_column(String(64), default="upload")
    payload: Mapped[dict] = mapped_column(JSONColumn, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NormalizedEvent(Base):
    """Canonical event -- the single shape every analytic function reads."""

    __tablename__ = "normalized_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    process_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("process_definitions.id"), default=None, index=True
    )
    source_system: Mapped[str] = mapped_column(String(64))
    source_event_id: Mapped[str | None] = mapped_column(String(200), default=None)
    case_id: Mapped[str] = mapped_column(String(200), index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="activity")
    activity_name: Mapped[str] = mapped_column(String(200), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(200), default=None)
    actor_type: Mapped[str | None] = mapped_column(String(32), default=None)
    team: Mapped[str | None] = mapped_column(String(120), default=None)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    object_type: Mapped[str | None] = mapped_column(String(64), default=None)
    object_id: Mapped[str | None] = mapped_column(String(200), default=None)
    monetary_value: Mapped[float | None] = mapped_column(Float, default=None)
    status_before: Mapped[str | None] = mapped_column(String(120), default=None)
    status_after: Mapped[str | None] = mapped_column(String(120), default=None)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    event_metadata: Mapped[dict] = mapped_column("metadata", JSONColumn, default=dict)

    __table_args__ = (
        Index("ix_events_tenant_case_time", "tenant_id", "case_id", "occurred_at"),
        Index("ix_events_tenant_activity", "tenant_id", "activity_name"),
        Index("ix_events_tenant_source", "tenant_id", "source_system"),
        UniqueConstraint(
            "tenant_id", "source_system", "source_event_id", name="uq_event_source_identity"
        ),
    )


class ProcessDefinition(Base, TimestampMixin):
    __tablename__ = "process_definitions"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    case_prefix: Mapped[str | None] = mapped_column(String(64), default=None)
    sla_hours: Mapped[float | None] = mapped_column(Float, default=None)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    variants: Mapped[list[ProcessVariant]] = relationship(
        back_populates="process", cascade="all, delete-orphan"
    )


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    process_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("process_definitions.id"), index=True
    )
    case_id: Mapped[str] = mapped_column(String(200), index=True)
    variant_key: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    throughput_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    handoff_count: Mapped[int] = mapped_column(Integer, default=0)
    rework_count: Mapped[int] = mapped_column(Integer, default=0)
    monetary_value: Mapped[float | None] = mapped_column(Float, default=None)
    sla_breached: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "process_id", "case_id", name="uq_case_identity"),
    )


class ProcessVariant(Base):
    __tablename__ = "process_variants"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    process_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("process_definitions.id"), index=True
    )
    variant_key: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[list] = mapped_column(JSONColumn, default=list)
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    share: Mapped[float] = mapped_column(Float, default=0.0)
    median_throughput_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    mean_throughput_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    sla_breach_rate: Mapped[float] = mapped_column(Float, default=0.0)
    total_monetary_value: Mapped[float] = mapped_column(Float, default=0.0)

    process: Mapped[ProcessDefinition] = relationship(back_populates="variants")


class ProcessEdge(Base):
    """Directly-follows relation between two activities."""

    __tablename__ = "process_edges"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    process_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("process_definitions.id"), index=True
    )
    source_activity: Mapped[str] = mapped_column(String(200))
    target_activity: Mapped[str] = mapped_column(String(200))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    median_wait_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    p90_wait_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    handoff_rate: Mapped[float] = mapped_column(Float, default=0.0)


class ProcessMetric(Base):
    __tablename__ = "process_metrics"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    process_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("process_definitions.id"), index=True
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    payload: Mapped[dict] = mapped_column(JSONColumn, default=dict)


class Finding(Base, TimestampMixin):
    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    process_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("process_definitions.id"), index=True
    )
    finding_type: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(16), default="medium", index=True)
    evidence: Mapped[dict] = mapped_column(JSONColumn, default=dict)
    affected_case_count: Mapped[int] = mapped_column(Integer, default=0)
    metric_value: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_value: Mapped[float | None] = mapped_column(Float, default=None)
    impact_hours_per_month: Mapped[float] = mapped_column(Float, default=0.0)
    impact_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="open")
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    narrative: Mapped[dict] = mapped_column(JSONColumn, default=dict)

    __table_args__ = (
        UniqueConstraint("tenant_id", "process_id", "fingerprint", name="uq_finding_fingerprint"),
        Index("ix_findings_tenant_severity", "tenant_id", "severity"),
    )


class OpportunityScore(Base, TimestampMixin):
    __tablename__ = "opportunity_scores"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    process_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("process_definitions.id"), index=True
    )
    activity_name: Mapped[str] = mapped_column(String(200))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    components: Mapped[dict] = mapped_column(JSONColumn, default=dict)
    estimated_hours_per_month: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_eur_per_month: Mapped[float] = mapped_column(Float, default=0.0)
    recommendation: Mapped[dict] = mapped_column(JSONColumn, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="proposed")


class Report(Base, TimestampMixin):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    process_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("process_definitions.id"), default=None
    )
    title: Mapped[str] = mapped_column(String(300))
    format: Mapped[str] = mapped_column(String(16), default="markdown")
    body: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSONColumn, default=dict)


class Benchmark(Base):
    """Snapshot used for before/after comparison of a process change."""

    __tablename__ = "benchmarks"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    process_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("process_definitions.id"), index=True
    )
    label: Mapped[str] = mapped_column(String(120))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONColumn, default=dict)


class AILogEntry(Base):
    __tablename__ = "ai_calls"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    object_type: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str | None] = mapped_column(String(200), default=None)
    actor: Mapped[str | None] = mapped_column(String(200), default=None)
    payload: Mapped[dict] = mapped_column(JSONColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
