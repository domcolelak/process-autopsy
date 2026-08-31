"""Automation opportunity scoring.

The score is a product of named components in ``[0, 1]``. Every component is
returned alongside the result so the UI can answer "why did this get 78?"
without the user having to trust a black box.

    score = frequency x time_cost x manuality x repeatability
            x stability x business_impact x confidence
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Sequence

from app.metrics.analytics import SECONDS_PER_HOUR, mean, median
from app.processes.traces import Trace


@dataclass
class OpportunityComponents:
    frequency: float
    time_cost: float
    manuality: float
    repeatability: float
    stability: float
    business_impact: float
    confidence: float

    def product(self) -> float:
        return (
            self.frequency
            * self.time_cost
            * self.manuality
            * self.repeatability
            * self.stability
            * self.business_impact
            * self.confidence
        )

    def as_dict(self) -> dict:
        return {k: round(v, 4) for k, v in asdict(self).items()}


@dataclass
class Opportunity:
    activity: str
    score: float
    components: OpportunityComponents
    estimated_hours_per_month: float
    estimated_eur_per_month: float
    evidence: dict = field(default_factory=dict)
    recommendation: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "activity": self.activity,
            "score": self.score,
            "components": self.components.as_dict(),
            "estimated_hours_per_month": round(self.estimated_hours_per_month, 2),
            "estimated_eur_per_month": round(self.estimated_eur_per_month, 2),
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


def score_opportunities(
    traces: Sequence[Trace],
    *,
    hourly_cost_eur: float = 35.0,
    window_days: float | None = None,
    min_occurrences: int = 20,
) -> list[Opportunity]:
    """Rank activities by how worthwhile automating them would be."""
    if not traces:
        return []

    days = window_days or _window_days(traces)
    total_cases = len(traces)

    occurrences: Counter[str] = Counter()
    cases: defaultdict[str, set[str]] = defaultdict(set)
    service: defaultdict[str, list[float]] = defaultdict(list)
    manual: Counter[str] = Counter()
    actors: defaultdict[str, set[str]] = defaultdict(set)
    systems: defaultdict[str, set[str]] = defaultdict(set)
    monetary: defaultdict[str, list[float]] = defaultdict(list)

    for trace in traces:
        for event in trace.events:
            key = event.activity
            occurrences[key] += 1
            cases[key].add(trace.case_id)
            service[key].append(event.service_seconds)
            actors[key].add(event.owner)
            systems[key].add(event.source_system)
            if event.is_manual:
                manual[key] += 1
            if trace.monetary_value is not None:
                monetary[key].append(trace.monetary_value)

    max_value = max(
        (max(values) for values in monetary.values() if values),
        default=0.0,
    )

    opportunities = []
    for activity, count in occurrences.items():
        if count < min_occurrences:
            continue

        durations = service[activity]
        total_seconds = float(sum(durations))
        hours_per_month = (total_seconds / days) * 30.0 / SECONDS_PER_HOUR
        manual_share = manual[activity] / count

        components = OpportunityComponents(
            frequency=_saturating(count / max(total_cases, 1), knee=1.0),
            time_cost=_saturating(hours_per_month / 40.0, knee=1.0),
            # A step nobody performs by hand is not an automation candidate.
            manuality=max(manual_share, 0.05),
            repeatability=_repeatability(durations),
            stability=_stability(traces, activity),
            business_impact=_business_impact(monetary[activity], max_value),
            confidence=min(0.5 + count / 500.0, 0.95),
        )

        score = round(100 * components.product(), 2)
        opportunities.append(
            Opportunity(
                activity=activity,
                score=score,
                components=components,
                estimated_hours_per_month=hours_per_month,
                estimated_eur_per_month=hours_per_month * hourly_cost_eur,
                evidence={
                    "occurrence_count": count,
                    "case_count": len(cases[activity]),
                    "median_service_minutes": round(median(durations) / 60, 2),
                    "manual_share": round(manual_share, 3),
                    "distinct_owners": len(actors[activity]),
                    "source_systems": sorted(systems[activity]),
                    "window_days": round(days, 1),
                },
                recommendation=_recommendation(
                    activity, manual_share, len(systems[activity]), components
                ),
            )
        )

    opportunities.sort(key=lambda o: (-o.score, o.activity))
    return opportunities


def _saturating(value: float, *, knee: float) -> float:
    """Map an unbounded positive quantity into ``(0, 1)`` with diminishing returns."""
    if value <= 0:
        return 0.0
    return float(1 - math.exp(-value / knee))


def _repeatability(durations: Sequence[float]) -> float:
    """How similar the executions are.

    Low variance means the step is mechanical and a deterministic automation is
    likely to cover it; high variance usually signals human judgement.
    """
    if len(durations) < 3:
        return 0.5
    average = mean(durations)
    if average <= 0:
        return 0.9
    variance = sum((d - average) ** 2 for d in durations) / len(durations)
    cv = math.sqrt(variance) / average
    return float(max(0.15, min(1.0, 1.0 / (1.0 + cv))))


def _stability(traces: Sequence[Trace], activity: str) -> float:
    """Whether the activity keeps appearing over time or is dying out."""
    ordered = sorted(traces, key=lambda t: t.started_at)
    middle = len(ordered) // 2 or 1
    first = sum(1 for t in ordered[:middle] for e in t.events if e.activity == activity)
    second = sum(1 for t in ordered[middle:] for e in t.events if e.activity == activity)
    if first == 0 and second == 0:
        return 0.0
    if first == 0 or second == 0:
        return 0.4
    ratio = min(first, second) / max(first, second)
    return float(0.4 + 0.6 * ratio)


def _business_impact(values: Sequence[float], max_value: float) -> float:
    """Cases carrying more money weigh more; absent amounts fall back to neutral."""
    if not values or max_value <= 0:
        return 0.6
    return float(min(1.0, 0.5 + 0.5 * (mean(values) / max_value)))


def _recommendation(
    activity: str, manual_share: float, system_count: int, components: OpportunityComponents
) -> dict:
    """Deterministic, rule-based next step. The AI layer only rephrases this."""
    if manual_share < 0.3:
        approach = "monitor"
        detail = "Mostly system-generated already; verify the remaining manual exceptions."
    elif system_count > 1:
        approach = "integration"
        detail = (
            "The step spans several systems, so an API integration or sync job removes "
            "the copying rather than scripting the UI."
        )
    elif components.repeatability >= 0.7:
        approach = "rule_based_automation"
        detail = (
            "Execution times are consistent, which suggests a deterministic rule or "
            "template can cover most executions."
        )
    else:
        approach = "assisted"
        detail = (
            "Execution times vary a lot, which usually means human judgement is "
            "involved; prepare the work instead of replacing the decision."
        )
    return {
        "approach": approach,
        "detail": detail,
        "activity": activity,
        "blockers": _blockers(components),
    }


def _blockers(components: OpportunityComponents) -> list[str]:
    blockers = []
    if components.stability < 0.6:
        blockers.append("Activity volume is unstable across the window")
    if components.repeatability < 0.4:
        blockers.append("High variance between executions")
    if components.confidence < 0.7:
        blockers.append("Limited sample size")
    return blockers


def _window_days(traces: Sequence[Trace]) -> float:
    start = min(t.started_at for t in traces)
    end = max(t.ended_at for t in traces)
    return max((end - start).total_seconds() / 86400.0, 1.0)
