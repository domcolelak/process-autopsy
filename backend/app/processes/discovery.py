"""Process discovery.

Builds a directly-follows graph (DFG) from case traces, together with the
variant table and the start/end activities. The implementation is intentionally
self-contained: process discovery on an event log is a small amount of graph
bookkeeping, and owning it keeps the analytics deterministic and debuggable.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Sequence

from app.metrics.analytics import median, percentile, variant_frequencies
from app.processes.traces import Trace


@dataclass
class GraphNode:
    activity: str
    occurrence_count: int
    case_count: int
    median_service_seconds: float
    manual_share: float
    is_start: bool = False
    is_end: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class GraphEdge:
    source: str
    target: str
    occurrence_count: int
    case_count: int
    median_wait_seconds: float
    p90_wait_seconds: float
    total_wait_seconds: float
    handoff_rate: float
    is_loop_edge: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProcessGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    start_activities: dict[str, int] = field(default_factory=dict)
    end_activities: dict[str, int] = field(default_factory=dict)
    case_count: int = 0

    def as_dict(self) -> dict:
        return {
            "nodes": [n.as_dict() for n in self.nodes],
            "edges": [e.as_dict() for e in self.edges],
            "start_activities": self.start_activities,
            "end_activities": self.end_activities,
            "case_count": self.case_count,
        }

    def filter_by_frequency(self, min_edge_case_share: float) -> ProcessGraph:
        """Return a simplified graph keeping only sufficiently frequent edges.

        Real event logs produce a spaghetti graph; the UI needs a slider that
        removes rare paths without recomputing anything server side.
        """
        if self.case_count == 0:
            return self
        threshold = min_edge_case_share * self.case_count
        edges = [e for e in self.edges if e.case_count >= threshold]
        kept = {e.source for e in edges} | {e.target for e in edges}
        nodes = [n for n in self.nodes if n.activity in kept] or self.nodes
        return ProcessGraph(
            nodes=nodes,
            edges=edges,
            start_activities=self.start_activities,
            end_activities=self.end_activities,
            case_count=self.case_count,
        )


def discover_graph(traces: Sequence[Trace]) -> ProcessGraph:
    """Build the directly-follows graph of an event log."""
    if not traces:
        return ProcessGraph()

    node_occurrences: Counter[str] = Counter()
    node_cases: defaultdict[str, set[str]] = defaultdict(set)
    node_service: defaultdict[str, list[float]] = defaultdict(list)
    node_manual: Counter[str] = Counter()

    edge_waits: defaultdict[tuple[str, str], list[float]] = defaultdict(list)
    edge_cases: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    edge_handoffs: Counter[tuple[str, str]] = Counter()

    starts: Counter[str] = Counter()
    ends: Counter[str] = Counter()

    for trace in traces:
        starts[trace.events[0].activity] += 1
        ends[trace.events[-1].activity] += 1
        for event in trace.events:
            node_occurrences[event.activity] += 1
            node_cases[event.activity].add(trace.case_id)
            node_service[event.activity].append(event.service_seconds)
            if event.is_manual:
                node_manual[event.activity] += 1
        for source, target in trace.transitions():
            key = (source.activity, target.activity)
            edge_waits[key].append(
                max((target.occurred_at - source.end_time).total_seconds(), 0.0)
            )
            edge_cases[key].add(trace.case_id)
            if source.owner != target.owner:
                edge_handoffs[key] += 1

    revisited = activities_on_a_cycle(set(edge_waits.keys()))

    nodes = [
        GraphNode(
            activity=activity,
            occurrence_count=count,
            case_count=len(node_cases[activity]),
            median_service_seconds=median(node_service[activity]),
            manual_share=node_manual[activity] / count if count else 0.0,
            is_start=activity in starts,
            is_end=activity in ends,
        )
        for activity, count in node_occurrences.items()
    ]
    nodes.sort(key=lambda n: (-n.occurrence_count, n.activity))

    edges = []
    for (source, target), waits in edge_waits.items():
        count = len(waits)
        edges.append(
            GraphEdge(
                source=source,
                target=target,
                occurrence_count=count,
                case_count=len(edge_cases[(source, target)]),
                median_wait_seconds=median(waits),
                p90_wait_seconds=percentile(waits, 0.90),
                total_wait_seconds=float(sum(waits)),
                handoff_rate=edge_handoffs[(source, target)] / count if count else 0.0,
                is_loop_edge=source in revisited and target in revisited,
            )
        )
    edges.sort(key=lambda e: (-e.occurrence_count, e.source, e.target))

    return ProcessGraph(
        nodes=nodes,
        edges=edges,
        start_activities=dict(starts.most_common()),
        end_activities=dict(ends.most_common()),
        case_count=len(traces),
    )


def activities_on_a_cycle(edges: set[tuple[str, str]]) -> set[str]:
    """Activities that sit on a cycle in the directly-follows graph.

    Uses Tarjan's strongly connected components, iteratively: any component
    with more than one node, or a node with a self-loop, is on a cycle.
    """
    adjacency: defaultdict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()
    cyclic: set[str] = set()
    for source, target in edges:
        nodes.add(source)
        nodes.add(target)
        adjacency[source].append(target)
        if source == target:
            cyclic.add(source)

    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []

    for root in sorted(nodes):
        if root in indices:
            continue
        work: list[list] = [[root, 0]]
        while work:
            frame = work[-1]
            node, child_index = frame[0], frame[1]
            if child_index == 0:
                indices[node] = lowlink[node] = index_counter
                index_counter += 1
                stack.append(node)
                on_stack[node] = True

            recursed = False
            neighbours = adjacency[node]
            while child_index < len(neighbours):
                neighbour = neighbours[child_index]
                child_index += 1
                if neighbour not in indices:
                    frame[1] = child_index
                    work.append([neighbour, 0])
                    recursed = True
                    break
                if on_stack.get(neighbour):
                    lowlink[node] = min(lowlink[node], indices[neighbour])
            if recursed:
                continue
            frame[1] = child_index

            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == indices[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    cyclic.update(component)
    return cyclic


def discover_variants(traces: Sequence[Trace], *, sla_hours: float | None = None) -> list[dict]:
    """Variant table enriched with SLA breach rate and handoffs per variant."""
    from app.metrics.analytics import sla_breach_rate  # local import avoids a cycle

    by_key: defaultdict[str, list[Trace]] = defaultdict(list)
    for trace in traces:
        by_key[trace.variant_key].append(trace)

    variants = variant_frequencies(traces)
    for variant in variants:
        group = by_key[variant["variant_key"]]
        variant["sla_breach_rate"] = sla_breach_rate(group, sla_hours)
        variant["mean_handoffs"] = (
            sum(t.handoff_count for t in group) / len(group) if group else 0.0
        )
        variant["rework_case_ratio"] = (
            sum(1 for t in group if t.contains_loop()) / len(group) if group else 0.0
        )
    return variants


def case_timeline(trace: Trace) -> dict:
    """Per-case timeline used by the case drill-down screen."""
    steps = []
    previous_end = None
    for event in trace.events:
        wait = (
            max((event.occurred_at - previous_end).total_seconds(), 0.0)
            if previous_end is not None
            else 0.0
        )
        steps.append(
            {
                "activity": event.activity,
                "occurred_at": event.occurred_at.isoformat(),
                "completed_at": event.completed_at.isoformat() if event.completed_at else None,
                "owner": event.owner,
                "source_system": event.source_system,
                "is_manual": event.is_manual,
                "service_seconds": event.service_seconds,
                "wait_before_seconds": wait,
            }
        )
        previous_end = event.end_time

    return {
        "case_id": trace.case_id,
        "variant_key": trace.variant_key,
        "started_at": trace.started_at.isoformat(),
        "ended_at": trace.ended_at.isoformat(),
        "throughput_seconds": trace.throughput_seconds,
        "waiting_seconds": trace.waiting_seconds(),
        "handoff_count": trace.handoff_count,
        "rework_count": trace.rework_count,
        "monetary_value": trace.monetary_value,
        "steps": steps,
    }
