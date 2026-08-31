"""Finding engine.

A *finding* is a statement about the process that is fully backed by computed
evidence: which metric, measured on how many cases, against which baseline, and
how much time it costs per month. The AI layer may later phrase a finding in
plain language, but it may never create one and may never change its numbers.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from app.metrics.analytics import (
    SECONDS_PER_HOUR,
    ProcessSummary,
    median,
    outlier_cases,
    summarize,
)
from app.processes.traces import Trace


@dataclass
class FindingCandidate:
    finding_type: str
    title: str
    severity: str
    evidence: dict
    affected_case_count: int
    metric_value: float
    baseline_value: float | None
    impact_hours_per_month: float
    impact_score: float
    confidence: float
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def fingerprint(self) -> str:
        """Stable identity so re-running analysis updates instead of duplicating."""
        subject = self.evidence.get("subject", self.title)
        raw = f"{self.finding_type}|{subject}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["detected_at"] = self.detected_at.isoformat()
        payload["fingerprint"] = self.fingerprint
        return payload


# --------------------------------------------------------------------------
# Detection thresholds. Deliberately explicit and configurable -- a finding
# engine that cannot be tuned gets ignored after the first week.
# --------------------------------------------------------------------------


@dataclass
class FindingConfig:
    min_cases: int = 15
    #: An edge is "slow" when its median wait exceeds this multiple of the
    #: median wait of the other edges in the process.
    wait_baseline_multiple: float = 2.5
    min_wait_hours: float = 2.0
    #: Share of cases in which an activity must repeat to count as rework.
    rework_case_share: float = 0.10
    #: Mean handoffs above this are flagged as fragmented ownership.
    handoff_threshold: float = 4.0
    #: A variant is "rare but expensive" below this share ...
    rare_variant_share: float = 0.10
    #: ... and above this multiple of the overall median cycle time.
    expensive_variant_multiple: float = 1.75
    #: Relative degradation between the two halves of the window.
    trend_worsening_pct: float = 0.20
    manual_repetition_share: float = 0.60
    hourly_cost_eur: float = 35.0


def detect_findings(
    traces: Sequence[Trace],
    *,
    sla_hours: float | None = None,
    config: FindingConfig | None = None,
) -> list[FindingCandidate]:
    """Run every detector over one process and return ranked findings."""
    cfg = config or FindingConfig()
    if len(traces) < max(cfg.min_cases, 2):
        return []

    summary = summarize(traces, sla_hours=sla_hours)
    window_days = _window_days(traces)

    findings: list[FindingCandidate] = []
    findings += _excessive_waiting(summary, cfg, window_days)
    findings += _repeated_activity(summary, cfg, window_days)
    findings += _high_handoffs(summary, cfg)
    findings += _rare_expensive_variant(traces, summary, cfg)
    findings += _worsening_cycle_time(traces, cfg, summary)
    findings += _manual_repetition(summary, cfg, window_days)
    findings += _cycle_time_outliers(traces, summary)

    findings.sort(key=lambda f: -f.impact_score)
    return findings


# --------------------------------------------------------------------------
# Detectors
# --------------------------------------------------------------------------


def _excessive_waiting(
    summary: ProcessSummary, cfg: FindingConfig, window_days: float
) -> list[FindingCandidate]:
    """Hand-off edges whose queue time dominates the rest of the process.

    The baseline for an edge is the median wait of *the other* edges. Comparing
    against a baseline that includes the candidate would let a single dominant
    queue hide itself -- with few edges it drags the median up to its own level.
    """
    if not summary.transitions:
        return []

    waits = [t.median_wait_seconds for t in summary.transitions]
    findings = []
    for index, transition in enumerate(summary.transitions):
        if transition.case_count < cfg.min_cases:
            continue
        if transition.median_wait_seconds < cfg.min_wait_hours * SECONDS_PER_HOUR:
            continue
        others = waits[:index] + waits[index + 1 :]
        baseline = median(others) if others else 0.0
        if baseline > 0 and transition.median_wait_seconds < baseline * cfg.wait_baseline_multiple:
            continue
        hours_per_month = _project_hours(transition.total_wait_seconds, window_days)
        findings.append(
            FindingCandidate(
                finding_type="excessive_waiting",
                title=(
                    f"Cases wait {_h(transition.median_wait_seconds)} between "
                    f"'{transition.source}' and '{transition.target}'"
                ),
                severity=_severity(hours_per_month),
                evidence={
                    "subject": f"{transition.source}->{transition.target}",
                    "source_activity": transition.source,
                    "target_activity": transition.target,
                    "median_wait_hours": round(
                        transition.median_wait_seconds / SECONDS_PER_HOUR, 2
                    ),
                    "p90_wait_hours": round(transition.p90_wait_seconds / SECONDS_PER_HOUR, 2),
                    "baseline_median_wait_hours": round(baseline / SECONDS_PER_HOUR, 2),
                    "baseline_scope": "median wait of the other transitions",
                    "handoff_rate": round(transition.handoff_rate, 3),
                    "cases_affected": transition.case_count,
                    "waiting_hours_per_month": round(hours_per_month, 1),
                },
                affected_case_count=transition.case_count,
                metric_value=transition.median_wait_seconds,
                baseline_value=baseline,
                impact_hours_per_month=hours_per_month,
                impact_score=_impact_score(hours_per_month, transition.case_count, summary),
                confidence=_confidence(transition.case_count),
            )
        )
    return findings


def _repeated_activity(
    summary: ProcessSummary, cfg: FindingConfig, window_days: float
) -> list[FindingCandidate]:
    """Activities executed more than once inside the same case."""
    findings = []
    for loop in summary.loops:
        if loop.affected_cases < cfg.min_cases:
            continue
        if loop.rework_ratio < cfg.rework_case_share:
            continue
        hours_per_month = _project_hours(loop.extra_seconds_total, window_days)
        findings.append(
            FindingCandidate(
                finding_type="repeated_activity",
                title=(
                    f"'{loop.activity}' repeats in {loop.rework_ratio:.0%} of cases "
                    f"({loop.repeat_events} extra executions)"
                ),
                severity=_severity(hours_per_month),
                evidence={
                    "subject": loop.activity,
                    "activity": loop.activity,
                    "repeat_events": loop.repeat_events,
                    "cases_affected": loop.affected_cases,
                    "rework_case_ratio": round(loop.rework_ratio, 3),
                    "rework_hours_per_month": round(hours_per_month, 1),
                },
                affected_case_count=loop.affected_cases,
                metric_value=loop.rework_ratio,
                baseline_value=0.0,
                impact_hours_per_month=hours_per_month,
                impact_score=_impact_score(hours_per_month, loop.affected_cases, summary),
                confidence=_confidence(loop.affected_cases),
            )
        )
    return findings


def _high_handoffs(summary: ProcessSummary, cfg: FindingConfig) -> list[FindingCandidate]:
    if summary.mean_handoffs < cfg.handoff_threshold:
        return []
    # Handoffs cost coordination time; attribute a conservative 10 minutes each.
    coordination_hours = summary.mean_handoffs * summary.case_count * (10 / 60)
    return [
        FindingCandidate(
            finding_type="high_handoff_count",
            title=(
                f"Cases change owner {summary.mean_handoffs:.1f} times on average "
                f"across {summary.case_count} cases"
            ),
            severity=_severity(coordination_hours),
            evidence={
                "subject": "process_handoffs",
                "mean_handoffs": round(summary.mean_handoffs, 2),
                "threshold": cfg.handoff_threshold,
                "cases_affected": summary.case_count,
                "assumption": "10 minutes of coordination cost per handoff",
                "coordination_hours_in_window": round(coordination_hours, 1),
            },
            affected_case_count=summary.case_count,
            metric_value=summary.mean_handoffs,
            baseline_value=cfg.handoff_threshold,
            impact_hours_per_month=coordination_hours,
            impact_score=_impact_score(coordination_hours, summary.case_count, summary),
            confidence=_confidence(summary.case_count),
        )
    ]


def _rare_expensive_variant(
    traces: Sequence[Trace], summary: ProcessSummary, cfg: FindingConfig
) -> list[FindingCandidate]:
    """Uncommon paths that consume disproportionate cycle time."""
    from app.processes.discovery import discover_variants

    overall_median = summary.throughput.median_seconds
    if overall_median <= 0:
        return []

    findings = []
    for variant in discover_variants(traces):
        if variant["share"] > cfg.rare_variant_share:
            continue
        if variant["case_count"] < max(5, cfg.min_cases // 3):
            continue
        ratio = variant["median_throughput_seconds"] / overall_median
        if ratio < cfg.expensive_variant_multiple:
            continue
        extra_seconds = (
            variant["median_throughput_seconds"] - overall_median
        ) * variant["case_count"]
        hours = extra_seconds / SECONDS_PER_HOUR
        findings.append(
            FindingCandidate(
                finding_type="rare_expensive_variant",
                title=(
                    f"A path used by {variant['share']:.0%} of cases takes "
                    f"{ratio:.1f}x the median cycle time"
                ),
                severity=_severity(hours),
                evidence={
                    "subject": f"variant:{variant['variant_key']}",
                    "variant_key": variant["variant_key"],
                    "sequence": variant["sequence"],
                    "case_count": variant["case_count"],
                    "share": round(variant["share"], 4),
                    "median_throughput_hours": round(
                        variant["median_throughput_seconds"] / SECONDS_PER_HOUR, 2
                    ),
                    "process_median_throughput_hours": round(
                        overall_median / SECONDS_PER_HOUR, 2
                    ),
                    "ratio": round(ratio, 2),
                    "example_case_ids": variant["example_case_ids"],
                },
                affected_case_count=variant["case_count"],
                metric_value=variant["median_throughput_seconds"],
                baseline_value=overall_median,
                impact_hours_per_month=hours,
                impact_score=_impact_score(hours, variant["case_count"], summary),
                confidence=_confidence(variant["case_count"]),
            )
        )
    return findings


def _worsening_cycle_time(
    traces: Sequence[Trace], cfg: FindingConfig, summary: ProcessSummary
) -> list[FindingCandidate]:
    """Split the window in half and compare median cycle time."""
    if len(traces) < max(cfg.min_cases * 2, 20):
        return []
    ordered = sorted(traces, key=lambda t: t.started_at)
    middle = len(ordered) // 2
    first = [t.throughput_seconds for t in ordered[:middle]]
    second = [t.throughput_seconds for t in ordered[middle:]]
    before, after = median(first), median(second)
    if before <= 0:
        return []
    change = (after - before) / before
    if change < cfg.trend_worsening_pct:
        return []

    extra_hours = ((after - before) * len(second)) / SECONDS_PER_HOUR
    return [
        FindingCandidate(
            finding_type="worsening_cycle_time",
            title=f"Median cycle time increased by {change:.0%} in the recent half of the window",
            severity=_severity(extra_hours),
            evidence={
                "subject": "cycle_time_trend",
                "earlier_median_hours": round(before / SECONDS_PER_HOUR, 2),
                "recent_median_hours": round(after / SECONDS_PER_HOUR, 2),
                "change_pct": round(change, 3),
                "earlier_cases": len(first),
                "recent_cases": len(second),
                "split_at": ordered[middle].started_at.isoformat(),
                "note": "Observed trend between two windows, not a causal claim.",
            },
            affected_case_count=len(second),
            metric_value=after,
            baseline_value=before,
            impact_hours_per_month=extra_hours,
            impact_score=_impact_score(extra_hours, len(second), summary),
            confidence=_confidence(len(ordered)),
        )
    ]


def _manual_repetition(
    summary: ProcessSummary, cfg: FindingConfig, window_days: float
) -> list[FindingCandidate]:
    """Frequent, highly manual activities -- the raw material for automation."""
    findings = []
    for activity in summary.activities:
        if activity.manual_share < cfg.manual_repetition_share:
            continue
        if activity.case_count < cfg.min_cases:
            continue
        hours_per_month = _project_hours(activity.total_service_seconds, window_days)
        if hours_per_month < 1.0:
            continue
        findings.append(
            FindingCandidate(
                finding_type="high_manual_repetition",
                title=(
                    f"'{activity.activity}' runs manually {activity.occurrence_count} times "
                    f"({_h(activity.median_service_seconds)} each)"
                ),
                severity=_severity(hours_per_month),
                evidence={
                    "subject": f"manual:{activity.activity}",
                    "activity": activity.activity,
                    "occurrence_count": activity.occurrence_count,
                    "cases_affected": activity.case_count,
                    "manual_share": round(activity.manual_share, 3),
                    "median_service_minutes": round(activity.median_service_seconds / 60, 1),
                    "hours_per_month": round(hours_per_month, 1),
                    "distinct_actors": activity.distinct_actors,
                },
                affected_case_count=activity.case_count,
                metric_value=activity.manual_share,
                baseline_value=cfg.manual_repetition_share,
                impact_hours_per_month=hours_per_month,
                impact_score=_impact_score(hours_per_month, activity.case_count, summary),
                confidence=_confidence(activity.case_count),
            )
        )
    return findings


def _cycle_time_outliers(
    traces: Sequence[Trace], summary: ProcessSummary
) -> list[FindingCandidate]:
    outliers = outlier_cases(traces)
    if len(outliers) < 3:
        return []
    threshold = outliers[0]["threshold_seconds"]
    extra_hours = (
        sum(o["throughput_seconds"] - threshold for o in outliers) / SECONDS_PER_HOUR
    )
    return [
        FindingCandidate(
            finding_type="cycle_time_outliers",
            title=f"{len(outliers)} cases exceed the statistical cycle-time fence",
            severity=_severity(extra_hours),
            evidence={
                "subject": "cycle_time_outliers",
                "outlier_case_count": len(outliers),
                "threshold_hours": round(threshold / SECONDS_PER_HOUR, 2),
                "method": "Tukey upper fence (Q3 + 1.5 * IQR) on case cycle time",
                "worst_cases": outliers[:10],
            },
            affected_case_count=len(outliers),
            metric_value=float(len(outliers)),
            baseline_value=threshold,
            impact_hours_per_month=extra_hours,
            impact_score=_impact_score(extra_hours, len(outliers), summary),
            confidence=0.75,
        )
    ]


# --------------------------------------------------------------------------
# Scoring helpers
# --------------------------------------------------------------------------


def _window_days(traces: Sequence[Trace]) -> float:
    if not traces:
        return 1.0
    start = min(t.started_at for t in traces)
    end = max(t.ended_at for t in traces)
    days = (end - start).total_seconds() / 86400.0
    return max(days, 1.0)


def _project_hours(total_seconds: float, window_days: float) -> float:
    """Scale a measured total onto a 30-day month."""
    if window_days <= 0:
        return 0.0
    return (total_seconds / window_days) * 30.0 / SECONDS_PER_HOUR


def _severity(hours_per_month: float) -> str:
    if hours_per_month >= 80:
        return "critical"
    if hours_per_month >= 20:
        return "high"
    if hours_per_month >= 5:
        return "medium"
    return "low"


#: Monthly hour cost treated as "as bad as it gets" when normalising impact.
IMPACT_REFERENCE_HOURS = 2_000.0


def _impact_score(
    hours_per_month: float, cases: int, summary: ProcessSummary
) -> float:
    """0-100 score combining time cost and how much of the process is affected.

    The time term is logarithmic. A linear term with a cap makes every large
    finding score identically -- a queue costing 10,000 hours a month and one
    costing 400 both saturate, and the ranking between them becomes arbitrary.
    Logarithmic scaling keeps bigger strictly higher while still applying
    diminishing returns, so a huge finding cannot bury everything else.
    """
    time_component = 0.0
    if hours_per_month > 0:
        time_component = min(
            math.log1p(hours_per_month) / math.log1p(IMPACT_REFERENCE_HOURS), 1.0
        )
    coverage = min(cases / summary.case_count, 1.0) if summary.case_count else 1.0
    return round(100 * (0.7 * time_component + 0.3 * coverage), 2)


def _confidence(sample_size: int) -> float:
    """Sample-size driven confidence, capped so nothing ever reads as certain."""
    if sample_size <= 0:
        return 0.0
    return round(min(0.5 + sample_size / 400.0, 0.95), 3)


def _h(seconds: float) -> str:
    hours = seconds / SECONDS_PER_HOUR
    if hours >= 1:
        return f"{hours:.1f} h"
    return f"{seconds / 60:.0f} min"
