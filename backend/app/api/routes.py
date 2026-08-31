"""HTTP API.

Every endpoint resolves a :class:`TenantContext` first and passes its
``tenant_id`` into each query -- there is no code path that reads data without
a tenant filter.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.insights import explain_finding, explain_opportunity, summarize_report
from app.audit.log import record_audit
from app.core.db import get_db
from app.core.security import TenantContext, current_tenant
from app.ingestion.mapping import normalize_rows, profile_rows, read_csv
from app.models import (
    Case,
    Finding,
    Import,
    NormalizedEvent,
    OpportunityScore,
    ProcessDefinition,
    RawEvent,
    Report,
)
from app.processes.discovery import case_timeline, discover_graph, discover_variants
from app.processes.service import analyze_process, before_after, get_process, load_traces
from app.reports.builder import build_report_payload, render_markdown
from app.schemas import (
    ApplyMappingRequest,
    ApplyMappingResponse,
    BeforeAfterRequest,
    EventBatchRequest,
    EventBatchResponse,
    FindingOut,
    FindingStatusUpdate,
    GraphResponse,
    ImportOut,
    ImportProfileOut,
    OpportunityOut,
    OpportunityStatusUpdate,
    ProcessCreate,
    ProcessListItem,
    ProcessOut,
    ReportOut,
    ReportRequest,
    VariantOut,
    WorkspaceOverview,
)

router = APIRouter()

MAX_UPLOAD_BYTES = 32 * 1024 * 1024


# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------


@router.get("/processes", response_model=list[ProcessListItem])
def list_processes(
    ctx: TenantContext = Depends(current_tenant), db: Session = Depends(get_db)
) -> list[ProcessListItem]:
    processes = db.scalars(
        select(ProcessDefinition)
        .where(ProcessDefinition.tenant_id == ctx.tenant_id)
        .order_by(ProcessDefinition.name)
    ).all()

    items = []
    for process in processes:
        case_count = db.scalar(
            select(func.count(Case.id)).where(
                Case.tenant_id == ctx.tenant_id, Case.process_id == process.id
            )
        )
        open_findings = db.scalar(
            select(func.count(Finding.id)).where(
                Finding.tenant_id == ctx.tenant_id,
                Finding.process_id == process.id,
                Finding.status == "open",
            )
        )
        median_hours = db.scalar(
            select(func.avg(Case.throughput_seconds)).where(
                Case.tenant_id == ctx.tenant_id, Case.process_id == process.id
            )
        )
        items.append(
            ProcessListItem(
                id=process.id,
                name=process.name,
                description=process.description,
                sla_hours=process.sla_hours,
                last_analyzed_at=process.last_analyzed_at,
                case_count=case_count or 0,
                open_findings=open_findings or 0,
                median_throughput_hours=round((median_hours or 0) / 3600, 2),
            )
        )
    return items


@router.post("/processes", response_model=ProcessOut, status_code=status.HTTP_201_CREATED)
def create_process(
    body: ProcessCreate,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> ProcessDefinition:
    process = ProcessDefinition(
        tenant_id=ctx.tenant_id,
        name=body.name,
        description=body.description,
        sla_hours=body.sla_hours,
    )
    db.add(process)
    db.flush()
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="process.created",
        object_type="process",
        object_id=process.id,
    )
    return process


@router.post("/processes/{process_id}/analyze")
def run_analysis(
    process_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = analyze_process(db, ctx.tenant_id, process_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="process.analyzed",
        object_type="process",
        object_id=process_id,
        payload=result,
    )
    return result


@router.get("/processes/{process_id}/map", response_model=GraphResponse)
def process_map(
    process_id: uuid.UUID,
    min_edge_case_share: float = Query(0.0, ge=0.0, le=1.0),
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> GraphResponse:
    _require_process(db, ctx, process_id)
    traces = load_traces(db, ctx.tenant_id, process_id)
    graph = discover_graph(traces)
    if min_edge_case_share > 0:
        graph = graph.filter_by_frequency(min_edge_case_share)
    payload = graph.as_dict()
    return GraphResponse(process_id=process_id, **payload)


@router.get("/processes/{process_id}/variants", response_model=list[VariantOut])
def process_variants(
    process_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=500),
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> list[VariantOut]:
    process = _require_process(db, ctx, process_id)
    traces = load_traces(db, ctx.tenant_id, process_id)
    variants = discover_variants(traces, sla_hours=process.sla_hours)
    return [VariantOut(**variant) for variant in variants[:limit]]


@router.get("/processes/{process_id}/metrics")
def process_metrics(
    process_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> dict:
    from app.metrics.analytics import summarize

    process = _require_process(db, ctx, process_id)
    traces = load_traces(db, ctx.tenant_id, process_id)
    return summarize(
        traces,
        sla_hours=process.sla_hours,
        window_start=min((t.started_at for t in traces), default=None),
        window_end=max((t.ended_at for t in traces), default=None),
    ).as_dict()


@router.get("/processes/{process_id}/cases/{case_id}")
def process_case(
    process_id: uuid.UUID,
    case_id: str,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> dict:
    _require_process(db, ctx, process_id)
    traces = load_traces(db, ctx.tenant_id, process_id)
    for trace in traces:
        if trace.case_id == case_id:
            return case_timeline(trace)
    raise HTTPException(status_code=404, detail="case not found")


@router.post("/processes/{process_id}/before-after")
def process_before_after(
    process_id: uuid.UUID,
    body: BeforeAfterRequest,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return before_after(db, ctx.tenant_id, process_id, split_at=body.split_at)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Imports
# --------------------------------------------------------------------------


@router.post("/imports", response_model=ImportProfileOut, status_code=status.HTTP_201_CREATED)
async def upload_import(
    file: UploadFile = File(...),
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> ImportProfileOut:
    """Upload a CSV export and get back a column profile plus a proposed mapping."""
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds the 32 MB upload limit")

    rows = read_csv(content)
    profile = profile_rows(rows)

    record = Import(
        tenant_id=ctx.tenant_id,
        filename=file.filename or "upload.csv",
        status="profiled",
        row_count=profile.row_count,
        detected_columns=[column.model_dump() for column in profile.columns],
        sample_rows=profile.sample_rows,
        mapping=profile.suggested_mapping,
    )
    db.add(record)
    db.flush()

    now = datetime.now(timezone.utc)
    db.add_all(
        [
            RawEvent(
                tenant_id=ctx.tenant_id,
                import_id=record.id,
                source_system="upload",
                payload=row,
                received_at=now,
            )
            for row in rows
        ]
    )
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="import.uploaded",
        object_type="import",
        object_id=record.id,
        payload={"rows": profile.row_count, "filename": record.filename},
    )
    return ImportProfileOut(
        import_id=record.id,
        row_count=profile.row_count,
        columns=profile.columns,
        suggested_mapping=profile.suggested_mapping,
        sample_rows=profile.sample_rows,
        warnings=profile.warnings,
    )


@router.get("/imports/{import_id}", response_model=ImportOut)
def get_import(
    import_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> Import:
    record = db.scalar(
        select(Import).where(Import.tenant_id == ctx.tenant_id, Import.id == import_id)
    )
    if record is None:
        raise HTTPException(status_code=404, detail="import not found")
    return record


@router.post("/imports/{import_id}/mapping", response_model=ApplyMappingResponse)
def apply_mapping(
    import_id: uuid.UUID,
    body: ApplyMappingRequest,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> ApplyMappingResponse:
    """Confirm the mapping, materialise canonical events and analyse the process."""
    record = db.scalar(
        select(Import).where(Import.tenant_id == ctx.tenant_id, Import.id == import_id)
    )
    if record is None:
        raise HTTPException(status_code=404, detail="import not found")

    raw_rows = [
        row.payload
        for row in db.scalars(
            select(RawEvent).where(
                RawEvent.tenant_id == ctx.tenant_id, RawEvent.import_id == import_id
            )
        )
    ]
    result = normalize_rows(raw_rows, body.mapping)

    process = _resolve_process(db, ctx, body.process_id, body.process_name or record.filename)
    if body.sla_hours is not None:
        process.sla_hours = body.sla_hours

    inserted = _insert_events(db, ctx.tenant_id, process.id, result.events, "upload")

    record.status = "applied"
    record.mapping = body.mapping.model_dump()
    record.accepted_count = inserted
    record.rejected_count = result.rejected
    record.errors = result.errors[:200]
    db.flush()

    analysis = None
    if body.analyze and inserted:
        analysis = analyze_process(db, ctx.tenant_id, process.id)

    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="import.mapped",
        object_type="import",
        object_id=import_id,
        payload={"accepted": inserted, "rejected": result.rejected},
    )
    return ApplyMappingResponse(
        import_id=import_id,
        process_id=process.id,
        accepted=inserted,
        rejected=result.rejected,
        errors=result.errors[:50],
        analysis=analysis,
    )


@router.post("/events/batch", response_model=EventBatchResponse)
def ingest_events(
    body: EventBatchRequest,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> EventBatchResponse:
    """Idempotent batch ingestion for connectors and custom integrations."""
    process = _resolve_process(db, ctx, body.process_id, body.process_name or "API events")
    payloads = [event.model_dump() for event in body.events]
    inserted = _insert_events(db, ctx.tenant_id, process.id, payloads, "api")
    return EventBatchResponse(
        process_id=process.id,
        accepted=inserted,
        duplicates=len(payloads) - inserted,
    )


# --------------------------------------------------------------------------
# Findings and opportunities
# --------------------------------------------------------------------------


@router.get("/findings", response_model=list[FindingOut])
def list_findings(
    process_id: uuid.UUID | None = None,
    finding_status: str | None = Query(None, alias="status"),
    severity: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> list[Finding]:
    stmt = select(Finding).where(Finding.tenant_id == ctx.tenant_id)
    if process_id:
        stmt = stmt.where(Finding.process_id == process_id)
    if finding_status:
        stmt = stmt.where(Finding.status == finding_status)
    if severity:
        stmt = stmt.where(Finding.severity == severity)
    stmt = stmt.order_by(Finding.impact_score.desc()).limit(limit)
    return list(db.scalars(stmt))


@router.get("/findings/{finding_id}", response_model=FindingOut)
def get_finding(
    finding_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> Finding:
    return _require_finding(db, ctx, finding_id)


@router.post("/findings/{finding_id}/explain", response_model=FindingOut)
def explain(
    finding_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> Finding:
    finding = _require_finding(db, ctx, finding_id)
    explain_finding(db, ctx.tenant_id, finding)
    db.flush()
    return finding


@router.post("/findings/{finding_id}/status", response_model=FindingOut)
def set_finding_status(
    finding_id: uuid.UUID,
    body: FindingStatusUpdate,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> Finding:
    finding = _require_finding(db, ctx, finding_id)
    finding.status = body.status
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="finding.status_changed",
        object_type="finding",
        object_id=finding_id,
        payload={"status": body.status},
    )
    return finding


@router.get("/opportunities", response_model=list[OpportunityOut])
def list_opportunities(
    process_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> list[OpportunityScore]:
    stmt = select(OpportunityScore).where(OpportunityScore.tenant_id == ctx.tenant_id)
    if process_id:
        stmt = stmt.where(OpportunityScore.process_id == process_id)
    stmt = stmt.order_by(OpportunityScore.score.desc()).limit(limit)
    return list(db.scalars(stmt))


@router.post("/opportunities/{opportunity_id}/explain")
def explain_opportunity_endpoint(
    opportunity_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> dict:
    opportunity = db.scalar(
        select(OpportunityScore).where(
            OpportunityScore.tenant_id == ctx.tenant_id,
            OpportunityScore.id == opportunity_id,
        )
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="opportunity not found")
    narrative = explain_opportunity(db, ctx.tenant_id, opportunity)
    return {
        "opportunity_id": str(opportunity_id),
        "narrative": narrative.model_dump() if narrative else None,
    }


@router.post("/opportunities/{opportunity_id}/status", response_model=OpportunityOut)
def set_opportunity_status(
    opportunity_id: uuid.UUID,
    body: OpportunityStatusUpdate,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> OpportunityScore:
    opportunity = db.scalar(
        select(OpportunityScore).where(
            OpportunityScore.tenant_id == ctx.tenant_id,
            OpportunityScore.id == opportunity_id,
        )
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="opportunity not found")
    opportunity.status = body.status
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="opportunity.status_changed",
        object_type="opportunity",
        object_id=opportunity_id,
        payload={"status": body.status},
    )
    return opportunity


# --------------------------------------------------------------------------
# Overview and reports
# --------------------------------------------------------------------------


@router.get("/overview", response_model=WorkspaceOverview)
def overview(
    ctx: TenantContext = Depends(current_tenant), db: Session = Depends(get_db)
) -> WorkspaceOverview:
    """Home screen: recoverable time first, vanity metrics never."""
    process_count = db.scalar(
        select(func.count(ProcessDefinition.id)).where(
            ProcessDefinition.tenant_id == ctx.tenant_id
        )
    )
    case_count = db.scalar(
        select(func.count(Case.id)).where(Case.tenant_id == ctx.tenant_id)
    )
    event_count = db.scalar(
        select(func.count(NormalizedEvent.id)).where(NormalizedEvent.tenant_id == ctx.tenant_id)
    )
    open_findings = db.scalar(
        select(func.count(Finding.id)).where(
            Finding.tenant_id == ctx.tenant_id, Finding.status == "open"
        )
    )
    hours = db.scalar(
        select(func.sum(OpportunityScore.estimated_hours_per_month)).where(
            OpportunityScore.tenant_id == ctx.tenant_id,
            OpportunityScore.status.in_(("proposed", "planned")),
        )
    )
    euros = db.scalar(
        select(func.sum(OpportunityScore.estimated_eur_per_month)).where(
            OpportunityScore.tenant_id == ctx.tenant_id,
            OpportunityScore.status.in_(("proposed", "planned")),
        )
    )
    top_finding = db.scalar(
        select(Finding)
        .where(Finding.tenant_id == ctx.tenant_id, Finding.status == "open")
        .order_by(Finding.impact_score.desc())
        .limit(1)
    )
    top_opportunity = db.scalar(
        select(OpportunityScore)
        .where(OpportunityScore.tenant_id == ctx.tenant_id)
        .order_by(OpportunityScore.score.desc())
        .limit(1)
    )
    worsening = [
        {
            "process_id": str(finding.process_id),
            "title": finding.title,
            "change_pct": finding.evidence.get("change_pct"),
        }
        for finding in db.scalars(
            select(Finding).where(
                Finding.tenant_id == ctx.tenant_id,
                Finding.finding_type == "worsening_cycle_time",
                Finding.status == "open",
            )
        )
    ]

    return WorkspaceOverview(
        process_count=process_count or 0,
        case_count=case_count or 0,
        event_count=event_count or 0,
        open_findings=open_findings or 0,
        recoverable_hours_per_month=round(hours or 0.0, 1),
        recoverable_eur_per_month=round(euros or 0.0, 2),
        top_finding=FindingOut.model_validate(top_finding) if top_finding else None,
        top_opportunity=OpportunityOut.model_validate(top_opportunity)
        if top_opportunity
        else None,
        worsening_processes=worsening,
    )


@router.post("/reports", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
def create_report(
    body: ReportRequest,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> Report:
    process = _require_process(db, ctx, body.process_id)
    payload = build_report_payload(db, ctx.tenant_id, process)
    narrative = None
    if body.include_ai_summary:
        result = summarize_report(db, ctx.tenant_id, payload)
        narrative = result.model_dump() if result else None
    payload["ai_summary"] = narrative

    report = Report(
        tenant_id=ctx.tenant_id,
        process_id=process.id,
        title=body.title or f"{process.name} - process autopsy",
        format="markdown",
        body=render_markdown(process.name, payload),
        payload=payload,
    )
    db.add(report)
    db.flush()
    return report


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(
    report_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> Report:
    report = db.scalar(
        select(Report).where(Report.tenant_id == ctx.tenant_id, Report.id == report_id)
    )
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    return report


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _require_process(
    db: Session, ctx: TenantContext, process_id: uuid.UUID
) -> ProcessDefinition:
    process = get_process(db, ctx.tenant_id, process_id)
    if process is None:
        raise HTTPException(status_code=404, detail="process not found")
    return process


def _require_finding(db: Session, ctx: TenantContext, finding_id: uuid.UUID) -> Finding:
    finding = db.scalar(
        select(Finding).where(Finding.tenant_id == ctx.tenant_id, Finding.id == finding_id)
    )
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    return finding


def _resolve_process(
    db: Session, ctx: TenantContext, process_id: uuid.UUID | None, fallback_name: str
) -> ProcessDefinition:
    if process_id is not None:
        return _require_process(db, ctx, process_id)
    existing = db.scalar(
        select(ProcessDefinition).where(
            ProcessDefinition.tenant_id == ctx.tenant_id,
            ProcessDefinition.name == fallback_name,
        )
    )
    if existing is not None:
        return existing
    process = ProcessDefinition(tenant_id=ctx.tenant_id, name=fallback_name)
    db.add(process)
    db.flush()
    return process


def _insert_events(
    db: Session,
    tenant_id: uuid.UUID,
    process_id: uuid.UUID,
    events: list[dict],
    default_source: str,
) -> int:
    """Insert canonical events, skipping ones already stored (idempotency)."""
    if not events:
        return 0

    keys = {
        (event.get("source_system") or default_source, event.get("source_event_id"))
        for event in events
    }
    existing = {
        (row.source_system, row.source_event_id)
        for row in db.scalars(
            select(NormalizedEvent).where(
                NormalizedEvent.tenant_id == tenant_id,
                NormalizedEvent.source_event_id.in_(
                    [key[1] for key in keys if key[1] is not None]
                ),
            )
        )
    }

    inserted = 0
    seen_in_batch: set[tuple[str, str | None]] = set()
    for event in events:
        source_system = event.get("source_system") or default_source
        source_event_id = event.get("source_event_id") or (
            f"{event['case_id']}:{event['activity_name']}:{event['occurred_at']}"
        )
        key = (source_system, source_event_id)
        if key in existing or key in seen_in_batch:
            continue
        seen_in_batch.add(key)
        db.add(
            NormalizedEvent(
                tenant_id=tenant_id,
                process_id=process_id,
                source_system=source_system,
                source_event_id=source_event_id,
                case_id=event["case_id"],
                activity_name=event["activity_name"],
                occurred_at=event["occurred_at"],
                completed_at=event.get("completed_at"),
                actor_id=event.get("actor_id"),
                actor_type=event.get("actor_type"),
                team=event.get("team"),
                duration_ms=event.get("duration_ms"),
                object_type=event.get("object_type"),
                object_id=event.get("object_id"),
                monetary_value=event.get("monetary_value"),
                status_before=event.get("status_before"),
                status_after=event.get("status_after"),
                is_manual=bool(event.get("is_manual", False)),
                event_metadata=event.get("metadata", {}),
            )
        )
        inserted += 1
    db.flush()
    return inserted
