"""Report generation.

The report body is rendered from computed values. The AI summary, when a
provider is configured, is added as a clearly separated section.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.metrics.analytics import SECONDS_PER_HOUR, summarize
from app.models import Finding, OpportunityScore, ProcessDefinition
from app.processes.discovery import discover_variants
from app.processes.service import load_traces


def build_report_payload(
    db: Session, tenant_id: uuid.UUID, process: ProcessDefinition
) -> dict:
    traces = load_traces(db, tenant_id, process.id)
    summary = summarize(traces, sla_hours=process.sla_hours)
    variants = discover_variants(traces, sla_hours=process.sla_hours)[:5]

    findings = list(
        db.scalars(
            select(Finding)
            .where(
                Finding.tenant_id == tenant_id,
                Finding.process_id == process.id,
                Finding.status != "dismissed",
            )
            .order_by(Finding.impact_score.desc())
            .limit(10)
        )
    )
    opportunities = list(
        db.scalars(
            select(OpportunityScore)
            .where(
                OpportunityScore.tenant_id == tenant_id,
                OpportunityScore.process_id == process.id,
            )
            .order_by(OpportunityScore.score.desc())
            .limit(10)
        )
    )

    return {
        "process": {
            "name": process.name,
            "description": process.description,
            "sla_hours": process.sla_hours,
        },
        "summary": {
            "case_count": summary.case_count,
            "event_count": summary.event_count,
            "variant_count": summary.variant_count,
            "median_throughput_hours": round(
                summary.throughput.median_seconds / SECONDS_PER_HOUR, 2
            ),
            "p90_throughput_hours": round(summary.throughput.p90_seconds / SECONDS_PER_HOUR, 2),
            "waiting_share": round(summary.waiting_share, 3),
            "mean_handoffs": round(summary.mean_handoffs, 2),
            "rework_case_ratio": round(summary.rework_case_ratio, 3),
            "sla_breach_rate": round(summary.sla_breach_rate, 3),
            "manual_event_share": round(summary.manual_event_share, 3),
        },
        "top_variants": [
            {
                "sequence": variant["sequence"],
                "case_count": variant["case_count"],
                "share": round(variant["share"], 4),
                "median_throughput_hours": round(
                    variant["median_throughput_seconds"] / SECONDS_PER_HOUR, 2
                ),
            }
            for variant in variants
        ],
        "findings": [
            {
                "title": finding.title,
                "type": finding.finding_type,
                "severity": finding.severity,
                "affected_cases": finding.affected_case_count,
                "hours_per_month": round(finding.impact_hours_per_month, 1),
                "confidence": finding.confidence,
                "evidence": finding.evidence,
            }
            for finding in findings
        ],
        "opportunities": [
            {
                "activity": opportunity.activity_name,
                "score": opportunity.score,
                "hours_per_month": round(opportunity.estimated_hours_per_month, 1),
                "eur_per_month": round(opportunity.estimated_eur_per_month, 2),
                "approach": opportunity.recommendation.get("approach"),
                "detail": opportunity.recommendation.get("detail"),
            }
            for opportunity in opportunities
        ],
        "totals": {
            "recoverable_hours_per_month": round(
                sum(o.estimated_hours_per_month for o in opportunities), 1
            ),
            "recoverable_eur_per_month": round(
                sum(o.estimated_eur_per_month for o in opportunities), 2
            ),
        },
    }


def render_markdown(process_name: str, payload: dict) -> str:
    """Render the payload as a Markdown report."""
    summary = payload["summary"]
    lines = [
        f"# {process_name}",
        "",
        "## Overview",
        "",
        f"- Cases analysed: **{summary['case_count']}**",
        f"- Events: **{summary['event_count']}**",
        f"- Distinct paths: **{summary['variant_count']}**",
        f"- Median cycle time: **{summary['median_throughput_hours']} h** "
        f"(p90 {summary['p90_throughput_hours']} h)",
        f"- Share of cycle time spent waiting: **{summary['waiting_share']:.0%}**",
        f"- Average handoffs per case: **{summary['mean_handoffs']}**",
        f"- Cases with rework: **{summary['rework_case_ratio']:.0%}**",
        f"- Manual events: **{summary['manual_event_share']:.0%}**",
        "",
        "## Findings",
        "",
    ]

    if payload["findings"]:
        for index, finding in enumerate(payload["findings"], start=1):
            lines += [
                f"### {index}. {finding['title']}",
                "",
                f"- Severity: **{finding['severity']}**",
                f"- Cases affected: **{finding['affected_cases']}**",
                f"- Estimated cost: **{finding['hours_per_month']} h / month**",
                f"- Confidence: **{finding['confidence']}**",
                "",
            ]
    else:
        lines += ["No findings crossed the detection thresholds.", ""]

    lines += ["## Automation opportunities", ""]
    if payload["opportunities"]:
        lines += ["| Activity | Score | h/month | EUR/month | Approach |", "|---|---|---|---|---|"]
        for opportunity in payload["opportunities"]:
            lines.append(
                f"| {opportunity['activity']} | {opportunity['score']} | "
                f"{opportunity['hours_per_month']} | {opportunity['eur_per_month']} | "
                f"{opportunity['approach']} |"
            )
        lines += [
            "",
            f"**Total potential: {payload['totals']['recoverable_hours_per_month']} h / month "
            f"({payload['totals']['recoverable_eur_per_month']} EUR / month)**",
            "",
        ]
    else:
        lines += ["No activity crossed the minimum volume threshold.", ""]

    ai_summary = payload.get("ai_summary")
    if ai_summary:
        lines += [
            "## Narrative summary",
            "",
            ai_summary.get("executive_summary", ""),
            "",
        ]
        if ai_summary.get("priorities"):
            lines += ["**Suggested order of work:**", ""]
            lines += [f"1. {item}" for item in ai_summary["priorities"]]
            lines.append("")

    lines += [
        "---",
        "",
        "All quantitative values in this report were computed by the analytics "
        "engine from the event log. Comparisons between time windows are "
        "observational and do not establish causation.",
    ]
    return "\n".join(lines)
