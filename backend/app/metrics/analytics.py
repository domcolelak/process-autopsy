"""Deterministic process analytics.

Every number the product shows -- and every number the AI layer is allowed to
talk about -- is produced here. These functions are pure: traces in, statistics
out, no database and no model calls.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Sequence

from app.processes.traces import Trace, TraceEvent

SECONDS_PER_HOUR = 3600.0


# --------------------------------------------------------------------------
# Small statistics helpers (kept local so the analytics stay dependency-light)
# --------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile; ``q`` in ``[0, 1]``."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def median(values: Sequence[float]) -> float:
    return percentile(values, 0.5)


def mean(values: Sequence[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def iqr_outlier_bounds(values: Sequence[float], factor: float = 1.5) -> tuple[float, float]:
    """Tukey fences. Robust enough for skewed cycle-time distributions."""
    if len(values) < 4:
        return (float("-inf"), float("inf"))
    q1 = percentile(values, 0.25)
    q3 = percentile(values, 0.75)
    spread = q3 - q1
    return (q1 - factor * spread, q3 + factor * spread)


# --------------------------------------------------------------------------
# Metric result objects
# --------------------------------------------------------------------------


@dataclass
class ThroughputMetrics:
    case_count: int
    median_seconds: float
    mean_seconds: float
    p90_seconds: float
    p95_seconds: float
    min_seconds: float
    max_seconds: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActivityMetrics:
    activity: str
    occurrence_count: int
    case_count: int
    median_service_seconds: float
    total_service_seconds: float
    manual_share: float
    distinct_actors: int
    repeat_rate: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TransitionMetrics:
    source: str
    target: str
    occurrence_count: int
    case_count: int
    median_wait_seconds: float
    p90_wait_seconds: float
    total_wait_seconds: float
    handoff_rate: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class LoopMetrics:
    activity: str
    repeat_events: int
    affected_cases: int
    rework_ratio: float
    extra_seconds_total: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProcessSummary:
    case_count: int
    event_count: int
    activity_count: int
    variant_count: int
    throughput: ThroughputMetrics
    median_waiting_seconds: float
    waiting_share: float
    mean_handoffs: float
    rework_case_ratio: float
    sla_breach_rate: float
    manual_event_share: float
    window_start: datetime | None = None
    window_end: datetime | None = None
    activities: list[ActivityMetrics] = field(default_factory=list)
    transitions: list[TransitionMetrics] = field(default_factory=list)
    loops: list[LoopMetrics] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "case_count": self.case_count,
            "event_count": self.event_count,
            "activity_count": self.activity_count,
            "variant_count": self.variant_count,
            "throughput": self.throughput.as_dict(),
            "median_waiting_seconds": self.median_waiting_seconds,
            "waiting_share": self.waiting_share,
            "mean_handoffs": self.mean_handoffs,
            "rework_case_ratio": self.rework_case_ratio,
            "sla_breach_rate": self.sla_breach_rate,
            "manual_event_share": self.manual_event_share,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "activities": [a.as_dict() for a in self.activities],
            "transitions": [t.as_dict() for t in self.transitions],
            "loops": [loop.as_dict() for loop in self.loops],
        }


# --------------------------------------------------------------------------
# Metric functions
# --------------------------------------------------------------------------


def throughput_metrics(traces: Sequence[Trace]) -> ThroughputMetrics:
    values = [trace.throughput_seconds for trace in traces]
    return ThroughputMetrics(
        case_count=len(traces),
        median_seconds=median(values),
        mean_seconds=mean(values),
        p90_seconds=percentile(values, 0.90),
        p95_seconds=percentile(values, 0.95),
        min_seconds=min(values) if values else 0.0,
        max_seconds=max(values) if values else 0.0,
    )


def activity_metrics(traces: Sequence[Trace]) -> list[ActivityMetrics]:
    occurrences: Counter[str] = Counter()
    cases: defaultdict[str, set[str]] = defaultdict(set)
    services: defaultdict[str, list[float]] = defaultdict(list)
    manual: Counter[str] = Counter()
    actors: defaultdict[str, set[str]] = defaultdict(set)
    repeats: Counter[str] = Counter()

    for trace in traces:
        seen: set[str] = set()
        for event in trace.events:
            key = event.activity
            occurrences[key] += 1
            cases[key].add(trace.case_id)
            services[key].append(event.service_seconds)
            actors[key].add(event.owner)
            if event.is_manual:
                manual[key] += 1
            if key in seen:
                repeats[key] += 1
            seen.add(key)

    result = []
    for activity, count in occurrences.items():
        case_count = len(cases[activity])
        result.append(
            ActivityMetrics(
                activity=activity,
                occurrence_count=count,
                case_count=case_count,
                median_service_seconds=median(services[activity]),
                total_service_seconds=float(sum(services[activity])),
                manual_share=manual[activity] / count if count else 0.0,
                distinct_actors=len(actors[activity]),
                repeat_rate=repeats[activity] / case_count if case_count else 0.0,
            )
        )
    result.sort(key=lambda a: (-a.occurrence_count, a.activity))
    return result


def transition_metrics(traces: Sequence[Trace]) -> list[TransitionMetrics]:
    """Waiting time on every directly-follows edge.

    Waiting time is measured from the *end* of the source activity to the start
    of the target activity, so a long-running activity is not counted as a queue.
    """
    waits: defaultdict[tuple[str, str], list[float]] = defaultdict(list)
    cases: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    handoffs: Counter[tuple[str, str]] = Counter()

    for trace in traces:
        for source, target in trace.transitions():
            key = (source.activity, target.activity)
            wait = max((target.occurred_at - source.end_time).total_seconds(), 0.0)
            waits[key].append(wait)
            cases[key].add(trace.case_id)
            if source.owner != target.owner:
                handoffs[key] += 1

    result = []
    for (source, target), values in waits.items():
        count = len(values)
        result.append(
            TransitionMetrics(
                source=source,
                target=target,
                occurrence_count=count,
                case_count=len(cases[(source, target)]),
                median_wait_seconds=median(values),
                p90_wait_seconds=percentile(values, 0.90),
                total_wait_seconds=float(sum(values)),
                handoff_rate=handoffs[(source, target)] / count if count else 0.0,
            )
        )
    result.sort(key=lambda t: (-t.total_wait_seconds, t.source, t.target))
    return result


def loop_metrics(traces: Sequence[Trace]) -> list[LoopMetrics]:
    """Activities that repeat inside the same case, with their time cost."""
    repeat_events: Counter[str] = Counter()
    affected: defaultdict[str, set[str]] = defaultdict(set)
    extra_seconds: defaultdict[str, float] = defaultdict(float)

    for trace in traces:
        seen: set[str] = set()
        for event in trace.events:
            if event.activity in seen:
                repeat_events[event.activity] += 1
                affected[event.activity].add(trace.case_id)
                extra_seconds[event.activity] += event.service_seconds
            seen.add(event.activity)

    total_cases = len(traces) or 1
    result = [
        LoopMetrics(
            activity=activity,
            repeat_events=count,
            affected_cases=len(affected[activity]),
            rework_ratio=len(affected[activity]) / total_cases,
            extra_seconds_total=extra_seconds[activity],
        )
        for activity, count in repeat_events.items()
    ]
    result.sort(key=lambda loop: (-loop.repeat_events, loop.activity))
    return result


def variant_frequencies(traces: Sequence[Trace]) -> list[dict]:
    """Variant table with frequency, share and cycle-time statistics."""
    grouped: defaultdict[str, list[Trace]] = defaultdict(list)
    for trace in traces:
        grouped[trace.variant_key].append(trace)

    total = len(traces) or 1
    variants = []
    for key, group in grouped.items():
        durations = [t.throughput_seconds for t in group]
        values = [t.monetary_value for t in group if t.monetary_value is not None]
        variants.append(
            {
                "variant_key": key,
                "sequence": list(group[0].sequence),
                "case_count": len(group),
                "share": len(group) / total,
                "median_throughput_seconds": median(durations),
                "mean_throughput_seconds": mean(durations),
                "total_monetary_value": float(sum(values)) if values else 0.0,
                "example_case_ids": [t.case_id for t in group[:5]],
            }
        )
    variants.sort(key=lambda v: (-v["case_count"], v["variant_key"]))
    return variants


def sla_breach_rate(traces: Sequence[Trace], sla_hours: float | None) -> float:
    if not traces or not sla_hours:
        return 0.0
    limit = sla_hours * SECONDS_PER_HOUR
    return sum(1 for t in traces if t.throughput_seconds > limit) / len(traces)


def outlier_cases(traces: Sequence[Trace]) -> list[dict]:
    """Cases whose cycle time sits above the upper Tukey fence."""
    values = [t.throughput_seconds for t in traces]
    _, upper = iqr_outlier_bounds(values)
    if math.isinf(upper):
        return []
    flagged = [
        {
            "case_id": t.case_id,
            "throughput_seconds": t.throughput_seconds,
            "variant_key": t.variant_key,
            "threshold_seconds": upper,
        }
        for t in traces
        if t.throughput_seconds > upper
    ]
    flagged.sort(key=lambda c: -c["throughput_seconds"])
    return flagged


def manual_event_share(traces: Sequence[Trace]) -> float:
    total = sum(len(t.events) for t in traces)
    if not total:
        return 0.0
    return sum(t.manual_event_count for t in traces) / total


def summarize(
    traces: Sequence[Trace],
    *,
    sla_hours: float | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> ProcessSummary:
    """Full metric bundle for one process."""
    activities = activity_metrics(traces)
    transitions = transition_metrics(traces)
    loops = loop_metrics(traces)
    throughput = throughput_metrics(traces)

    waiting_values = [t.waiting_seconds() for t in traces]
    total_throughput = sum(t.throughput_seconds for t in traces)
    total_waiting = sum(waiting_values)

    return ProcessSummary(
        case_count=len(traces),
        event_count=sum(len(t.events) for t in traces),
        activity_count=len(activities),
        variant_count=len({t.variant_key for t in traces}),
        throughput=throughput,
        median_waiting_seconds=median(waiting_values),
        waiting_share=(total_waiting / total_throughput) if total_throughput else 0.0,
        mean_handoffs=mean([float(t.handoff_count) for t in traces]),
        rework_case_ratio=(
            sum(1 for t in traces if t.contains_loop()) / len(traces) if traces else 0.0
        ),
        sla_breach_rate=sla_breach_rate(traces, sla_hours),
        manual_event_share=manual_event_share(traces),
        window_start=window_start,
        window_end=window_end,
        activities=activities,
        transitions=transitions,
        loops=loops,
    )


def compare_windows(
    before: Sequence[Trace],
    after: Sequence[Trace],
    *,
    sla_hours: float | None = None,
) -> dict:
    """Before/after comparison used to prove (or disprove) an improvement."""
    before_summary = summarize(before, sla_hours=sla_hours)
    after_summary = summarize(after, sla_hours=sla_hours)

    def delta(field_name: str, before_value: float, after_value: float) -> dict:
        change = after_value - before_value
        pct = (change / before_value) if before_value else None
        return {
            "metric": field_name,
            "before": before_value,
            "after": after_value,
            "change": change,
            "change_pct": pct,
            "improved": _is_improvement(field_name, change),
        }

    return {
        "before": before_summary.as_dict(),
        "after": after_summary.as_dict(),
        "deltas": [
            delta(
                "median_throughput_seconds",
                before_summary.throughput.median_seconds,
                after_summary.throughput.median_seconds,
            ),
            delta(
                "p90_throughput_seconds",
                before_summary.throughput.p90_seconds,
                after_summary.throughput.p90_seconds,
            ),
            delta(
                "median_waiting_seconds",
                before_summary.median_waiting_seconds,
                after_summary.median_waiting_seconds,
            ),
            delta("mean_handoffs", before_summary.mean_handoffs, after_summary.mean_handoffs),
            delta(
                "rework_case_ratio",
                before_summary.rework_case_ratio,
                after_summary.rework_case_ratio,
            ),
            delta("sla_breach_rate", before_summary.sla_breach_rate, after_summary.sla_breach_rate),
            delta(
                "manual_event_share",
                before_summary.manual_event_share,
                after_summary.manual_event_share,
            ),
        ],
        "note": (
            "Observed change between two time windows. Other factors may differ "
            "between the windows; this is not a causal measurement."
        ),
    }


def _is_improvement(metric: str, change: float) -> bool | None:
    """All metrics compared here are 'lower is better'."""
    if change == 0:
        return None
    return change < 0


def events_to_hours(seconds: float) -> float:
    return seconds / SECONDS_PER_HOUR


def estimate_monthly_hours(
    total_seconds: float, *, window_days: float, cases_scale: float = 1.0
) -> float:
    """Project a measured time cost onto a 30-day month."""
    if window_days <= 0:
        return 0.0
    per_day = total_seconds / window_days
    return (per_day * 30.0 * cases_scale) / SECONDS_PER_HOUR


def trace_event_from_row(row: dict) -> TraceEvent:  # pragma: no cover - convenience helper
    return TraceEvent(
        case_id=row["case_id"],
        activity=row["activity_name"],
        occurred_at=row["occurred_at"],
        completed_at=row.get("completed_at"),
        actor=row.get("actor_id"),
        team=row.get("team"),
        source_system=row.get("source_system", "unknown"),
        is_manual=bool(row.get("is_manual", False)),
        duration_ms=row.get("duration_ms"),
        monetary_value=row.get("monetary_value"),
    )
