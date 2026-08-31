"""Analysis service -- the bridge between storage and the pure analytics.

Loads canonical events for one process, converts them to traces, runs discovery,
metrics, findings and opportunity scoring, and persists the results. All queries
are tenant scoped.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.findings.engine import FindingConfig, detect_findings
from app.metrics.analytics import compare_windows, summarize
from app.models import (
    Case,
    Finding,
    NormalizedEvent,
    OpportunityScore,
    ProcessDefinition,
    ProcessEdge,
    ProcessMetric,
    ProcessVariant,
    Tenant,
)
from app.opportunities.scoring import score_opportunities
from app.processes.discovery import ProcessGraph, discover_graph, discover_variants
from app.processes.traces import Trace, TraceEvent, build_traces, filter_window


def load_traces(
    db: Session,
    tenant_id: uuid.UUID,
    process_id: uuid.UUID,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Trace]:
    """Read canonical events for one process and build case traces."""
    stmt = (
        select(NormalizedEvent)
        .where(
            NormalizedEvent.tenant_id == tenant_id,
            NormalizedEvent.process_id == process_id,
        )
        .order_by(NormalizedEvent.case_id, NormalizedEvent.occurred_at)
    )
    if start is not None:
        stmt = stmt.where(NormalizedEvent.occurred_at >= start)
    if end is not None:
        stmt = stmt.where(NormalizedEvent.occurred_at < end)

    events = [
        TraceEvent(
            case_id=row.case_id,
            activity=row.activity_name,
            occurred_at=_aware(row.occurred_at),
            completed_at=_aware(row.completed_at),
            actor=row.actor_id,
            team=row.team,
            source_system=row.source_system,
            is_manual=bool(row.is_manual),
            duration_ms=row.duration_ms,
            monetary_value=row.monetary_value,
        )
        for row in db.scalars(stmt)
    ]
    return build_traces(events)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; normalise everything to UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def get_process(db: Session, tenant_id: uuid.UUID, process_id: uuid.UUID) -> ProcessDefinition | None:
    return db.scalar(
        select(ProcessDefinition).where(
            ProcessDefinition.tenant_id == tenant_id, ProcessDefinition.id == process_id
        )
    )


def analyze_process(
    db: Session,
    tenant_id: uuid.UUID,
    process_id: uuid.UUID,
    *,
    config: FindingConfig | None = None,
) -> dict:
    """Full analysis run: cases, variants, edges, metrics, findings, opportunities."""
    process = get_process(db, tenant_id, process_id)
    if process is None:
        raise LookupError("process not found for this tenant")

    tenant = db.get(Tenant, tenant_id)
    hourly_cost = tenant.hourly_cost_eur if tenant else 35.0

    traces = load_traces(db, tenant_id, process_id)
    if not traces:
        return {"process_id": str(process_id), "case_count": 0, "findings": 0, "opportunities": 0}

    summary = summarize(traces, sla_hours=process.sla_hours)
    graph = discover_graph(traces)
    variants = discover_variants(traces, sla_hours=process.sla_hours)

    _persist_cases(db, tenant_id, process_id, traces, process.sla_hours)
    _persist_variants(db, tenant_id, process_id, variants)
    _persist_edges(db, tenant_id, process_id, graph)

    db.add(
        ProcessMetric(
            tenant_id=tenant_id,
            process_id=process_id,
            computed_at=datetime.now(timezone.utc),
            window_start=min(t.started_at for t in traces),
            window_end=max(t.ended_at for t in traces),
            payload=summary.as_dict(),
        )
    )

    findings = detect_findings(
        traces,
        sla_hours=process.sla_hours,
        config=config or FindingConfig(hourly_cost_eur=hourly_cost),
    )
    _persist_findings(db, tenant_id, process_id, findings)

    opportunities = score_opportunities(traces, hourly_cost_eur=hourly_cost)
    _persist_opportunities(db, tenant_id, process_id, opportunities)

    process.last_analyzed_at = datetime.now(timezone.utc)
    db.flush()

    return {
        "process_id": str(process_id),
        "case_count": len(traces),
        "event_count": summary.event_count,
        "variant_count": summary.variant_count,
        "findings": len(findings),
        "opportunities": len(opportunities),
        "analyzed_at": process.last_analyzed_at.isoformat(),
    }


def _persist_cases(
    db: Session,
    tenant_id: uuid.UUID,
    process_id: uuid.UUID,
    traces: list[Trace],
    sla_hours: float | None,
) -> None:
    db.execute(
        delete(Case).where(Case.tenant_id == tenant_id, Case.process_id == process_id)
    )
    limit = (sla_hours or 0) * 3600
    db.add_all(
        [
            Case(
                tenant_id=tenant_id,
                process_id=process_id,
                case_id=trace.case_id,
                variant_key=trace.variant_key,
                started_at=trace.started_at,
                ended_at=trace.ended_at,
                throughput_seconds=trace.throughput_seconds,
                event_count=len(trace.events),
                handoff_count=trace.handoff_count,
                rework_count=trace.rework_count,
                monetary_value=trace.monetary_value,
                sla_breached=bool(limit and trace.throughput_seconds > limit),
            )
            for trace in traces
        ]
    )
    db.flush()


def _persist_variants(
    db: Session, tenant_id: uuid.UUID, process_id: uuid.UUID, variants: list[dict]
) -> None:
    db.execute(
        delete(ProcessVariant).where(
            ProcessVariant.tenant_id == tenant_id, ProcessVariant.process_id == process_id
        )
    )
    db.add_all(
        [
            ProcessVariant(
                tenant_id=tenant_id,
                process_id=process_id,
                variant_key=variant["variant_key"],
                sequence=variant["sequence"],
                case_count=variant["case_count"],
                share=variant["share"],
                median_throughput_seconds=variant["median_throughput_seconds"],
                mean_throughput_seconds=variant["mean_throughput_seconds"],
                sla_breach_rate=variant.get("sla_breach_rate", 0.0),
                total_monetary_value=variant.get("total_monetary_value", 0.0),
            )
            for variant in variants
        ]
    )
    db.flush()


def _persist_edges(
    db: Session, tenant_id: uuid.UUID, process_id: uuid.UUID, graph: ProcessGraph
) -> None:
    db.execute(
        delete(ProcessEdge).where(
            ProcessEdge.tenant_id == tenant_id, ProcessEdge.process_id == process_id
        )
    )
    db.add_all(
        [
            ProcessEdge(
                tenant_id=tenant_id,
                process_id=process_id,
                source_activity=edge.source,
                target_activity=edge.target,
                occurrence_count=edge.occurrence_count,
                case_count=edge.case_count,
                median_wait_seconds=edge.median_wait_seconds,
                p90_wait_seconds=edge.p90_wait_seconds,
                handoff_rate=edge.handoff_rate,
            )
            for edge in graph.edges
        ]
    )
    db.flush()


def _persist_findings(
    db: Session, tenant_id: uuid.UUID, process_id: uuid.UUID, findings: list
) -> None:
    """Upsert by fingerprint so re-analysis refreshes instead of duplicating,
    and user-set statuses survive a new run."""
    existing = {
        row.fingerprint: row
        for row in db.scalars(
            select(Finding).where(
                Finding.tenant_id == tenant_id, Finding.process_id == process_id
            )
        )
    }
    seen: set[str] = set()

    for candidate in findings:
        seen.add(candidate.fingerprint)
        row = existing.get(candidate.fingerprint)
        if row is None:
            row = Finding(
                tenant_id=tenant_id,
                process_id=process_id,
                fingerprint=candidate.fingerprint,
                status="open",
            )
            db.add(row)
        row.finding_type = candidate.finding_type
        row.title = candidate.title
        row.severity = candidate.severity
        row.evidence = candidate.evidence
        row.affected_case_count = candidate.affected_case_count
        row.metric_value = candidate.metric_value
        row.baseline_value = candidate.baseline_value
        row.impact_hours_per_month = candidate.impact_hours_per_month
        row.impact_score = candidate.impact_score
        row.confidence = candidate.confidence
        row.detected_at = candidate.detected_at

    for fingerprint, row in existing.items():
        if fingerprint not in seen and row.status == "open":
            row.status = "resolved"
    db.flush()


def _persist_opportunities(
    db: Session, tenant_id: uuid.UUID, process_id: uuid.UUID, opportunities: list
) -> None:
    previous = {
        row.activity_name: row.status
        for row in db.scalars(
            select(OpportunityScore).where(
                OpportunityScore.tenant_id == tenant_id,
                OpportunityScore.process_id == process_id,
            )
        )
    }
    db.execute(
        delete(OpportunityScore).where(
            OpportunityScore.tenant_id == tenant_id,
            OpportunityScore.process_id == process_id,
        )
    )
    db.add_all(
        [
            OpportunityScore(
                tenant_id=tenant_id,
                process_id=process_id,
                activity_name=opportunity.activity,
                score=opportunity.score,
                components=opportunity.components.as_dict() | {"evidence": opportunity.evidence},
                estimated_hours_per_month=opportunity.estimated_hours_per_month,
                estimated_eur_per_month=opportunity.estimated_eur_per_month,
                recommendation=opportunity.recommendation,
                status=previous.get(opportunity.activity, "proposed"),
            )
            for opportunity in opportunities
        ]
    )
    db.flush()


def before_after(
    db: Session,
    tenant_id: uuid.UUID,
    process_id: uuid.UUID,
    *,
    split_at: datetime,
) -> dict:
    """Compare the process before and after a change date."""
    process = get_process(db, tenant_id, process_id)
    if process is None:
        raise LookupError("process not found for this tenant")
    traces = load_traces(db, tenant_id, process_id)
    before = filter_window(traces, None, split_at)
    after = filter_window(traces, split_at, None)
    result = compare_windows(before, after, sla_hours=process.sla_hours)
    result["split_at"] = split_at.isoformat()
    result["before_case_count"] = len(before)
    result["after_case_count"] = len(after)
    return result
