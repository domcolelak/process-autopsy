"""Case trace construction.

A *trace* is the ordered sequence of events belonging to one case. Every
analytic function in the product operates on traces, never on raw rows, so the
same code path serves CSV imports, the batch event API and native connectors.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One canonical event, reduced to the fields the analytics need."""

    case_id: str
    activity: str
    occurred_at: datetime
    completed_at: datetime | None = None
    actor: str | None = None
    team: str | None = None
    source_system: str = "unknown"
    is_manual: bool = False
    duration_ms: int | None = None
    monetary_value: float | None = None

    @property
    def end_time(self) -> datetime:
        return self.completed_at or self.occurred_at

    @property
    def service_seconds(self) -> float:
        """Time actively spent inside the activity, when it is known."""
        if self.duration_ms is not None:
            return self.duration_ms / 1000.0
        if self.completed_at is not None:
            return max((self.completed_at - self.occurred_at).total_seconds(), 0.0)
        return 0.0

    @property
    def owner(self) -> str:
        """Unit responsible for the step -- team first, actor as a fallback."""
        return self.team or self.actor or "unassigned"


@dataclass(slots=True)
class Trace:
    """All events of a single case, ordered in time."""

    case_id: str
    events: list[TraceEvent] = field(default_factory=list)

    @property
    def sequence(self) -> tuple[str, ...]:
        return tuple(event.activity for event in self.events)

    @property
    def variant_key(self) -> str:
        return variant_key_for(self.sequence)

    @property
    def started_at(self) -> datetime:
        return self.events[0].occurred_at

    @property
    def ended_at(self) -> datetime:
        return max(event.end_time for event in self.events)

    @property
    def throughput_seconds(self) -> float:
        return max((self.ended_at - self.started_at).total_seconds(), 0.0)

    @property
    def monetary_value(self) -> float | None:
        values = [e.monetary_value for e in self.events if e.monetary_value is not None]
        return max(values) if values else None

    @property
    def handoff_count(self) -> int:
        """Number of times responsibility moves to a different team/actor."""
        owners = [event.owner for event in self.events]
        return sum(1 for a, b in zip(owners, owners[1:]) if a != b)

    @property
    def rework_count(self) -> int:
        """Repetitions of an activity that already happened in the same case."""
        seen: set[str] = set()
        repeats = 0
        for event in self.events:
            if event.activity in seen:
                repeats += 1
            seen.add(event.activity)
        return repeats

    @property
    def manual_event_count(self) -> int:
        return sum(1 for event in self.events if event.is_manual)

    def transitions(self) -> list[tuple[TraceEvent, TraceEvent]]:
        return list(zip(self.events, self.events[1:]))

    def waiting_seconds(self) -> float:
        """Throughput minus time actually spent working on the case."""
        service = sum(event.service_seconds for event in self.events)
        return max(self.throughput_seconds - service, 0.0)

    def contains_loop(self) -> bool:
        return self.rework_count > 0


def variant_key_for(sequence: Sequence[str]) -> str:
    """Stable short identifier for an activity sequence."""
    digest = hashlib.sha1("␟".join(sequence).encode("utf-8"))
    return digest.hexdigest()[:16]


def build_traces(events: Iterable[TraceEvent]) -> list[Trace]:
    """Group events into per-case traces ordered by time.

    Ties on ``occurred_at`` are broken by activity name so that a given event
    log always produces exactly the same variants -- determinism matters more
    than being clever here, because variants are user-visible identities.
    """
    grouped: dict[str, list[TraceEvent]] = {}
    for event in events:
        grouped.setdefault(event.case_id, []).append(event)

    traces: list[Trace] = []
    for case_id, case_events in grouped.items():
        case_events.sort(key=lambda e: (e.occurred_at, e.activity))
        traces.append(Trace(case_id=case_id, events=case_events))
    traces.sort(key=lambda t: (t.started_at, t.case_id))
    return traces


def filter_window(
    traces: Sequence[Trace], start: datetime | None, end: datetime | None
) -> list[Trace]:
    """Return traces that *started* inside the half-open window ``[start, end)``."""
    result = []
    for trace in traces:
        if start is not None and trace.started_at < start:
            continue
        if end is not None and trace.started_at >= end:
            continue
        result.append(trace)
    return result
