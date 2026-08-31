"""AI insight layer.

The model receives *only* computed evidence and returns a structured object.
It never sees raw event rows, it never produces a number the analytics did not
compute, and if it fails the caller still has the finding.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.provider import AICallResult, get_provider
from app.models import AILogEntry, Finding, OpportunityScore

PROMPT_VERSION = "finding-explainer-v1"

SYSTEM_FINDING = (
    "You are an operations analyst. You are given the result of a deterministic "
    "process-mining analysis. Explain what it means for the operations team in "
    "plain language. Use only the supplied numbers. If something is not in the "
    "evidence, say it is unknown rather than guessing. Avoid causal claims: the "
    "data is observational, so write 'is associated with' rather than 'causes'."
)

SYSTEM_OPPORTUNITY = (
    "You are an automation consultant. You are given a scored automation "
    "candidate with its component scores and evidence. Describe what automating "
    "the step would involve and what could go wrong. Use only the supplied "
    "numbers and never promise a saving larger than the estimate provided."
)

SYSTEM_REPORT = (
    "You are writing a short operations report for a COO. You are given a list "
    "of computed findings and opportunities. Summarise them honestly, keep every "
    "number exactly as supplied, and make the recommended order of work explicit."
)


class FindingNarrative(BaseModel):
    headline: str = Field(description="One sentence, no numbers invented")
    explanation: str
    likely_causes: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class OpportunityNarrative(BaseModel):
    summary: str
    implementation_steps: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ReportNarrative(BaseModel):
    executive_summary: str
    priorities: list[str] = Field(default_factory=list)
    watch_items: list[str] = Field(default_factory=list)


def explain_finding(db: Session, tenant_id: uuid.UUID, finding: Finding) -> FindingNarrative | None:
    """Attach a plain-language narrative to a finding; returns ``None`` on failure."""
    evidence = {
        "finding_type": finding.finding_type,
        "title": finding.title,
        "severity": finding.severity,
        "metric_value": finding.metric_value,
        "baseline_value": finding.baseline_value,
        "affected_case_count": finding.affected_case_count,
        "estimated_hours_per_month": round(finding.impact_hours_per_month, 1),
        "confidence": finding.confidence,
        "evidence": finding.evidence,
    }
    result = get_provider().structured(
        system=SYSTEM_FINDING,
        evidence=evidence,
        output_model=FindingNarrative,
        prompt_version=PROMPT_VERSION,
    )
    _log_call(db, tenant_id, "explain_finding", result)
    if not result.ok:
        return None
    narrative = result.output
    assert isinstance(narrative, FindingNarrative)
    finding.narrative = narrative.model_dump() | {"model": result.model}
    return narrative


def explain_opportunity(
    db: Session, tenant_id: uuid.UUID, opportunity: OpportunityScore
) -> OpportunityNarrative | None:
    evidence = {
        "activity": opportunity.activity_name,
        "score": opportunity.score,
        "components": opportunity.components,
        "estimated_hours_per_month": opportunity.estimated_hours_per_month,
        "estimated_eur_per_month": opportunity.estimated_eur_per_month,
        "deterministic_recommendation": opportunity.recommendation,
    }
    result = get_provider().structured(
        system=SYSTEM_OPPORTUNITY,
        evidence=evidence,
        output_model=OpportunityNarrative,
        prompt_version="opportunity-explainer-v1",
    )
    _log_call(db, tenant_id, "explain_opportunity", result)
    if not result.ok:
        return None
    narrative = result.output
    assert isinstance(narrative, OpportunityNarrative)
    return narrative


def summarize_report(db: Session, tenant_id: uuid.UUID, payload: dict) -> ReportNarrative | None:
    result = get_provider().structured(
        system=SYSTEM_REPORT,
        evidence=payload,
        output_model=ReportNarrative,
        prompt_version="report-writer-v1",
    )
    _log_call(db, tenant_id, "report", result)
    if not result.ok:
        return None
    narrative = result.output
    assert isinstance(narrative, ReportNarrative)
    return narrative


def _log_call(db: Session, tenant_id: uuid.UUID, purpose: str, result: AICallResult) -> None:
    db.add(
        AILogEntry(
            tenant_id=tenant_id,
            purpose=purpose,
            model=result.model,
            prompt_version=result.prompt_version,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            error=result.error,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.flush()
