"""Tests for the deterministic analytics layer."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.metrics.analytics import (
    compare_windows,
    iqr_outlier_bounds,
    median,
    outlier_cases,
    percentile,
    sla_breach_rate,
    summarize,
    transition_metrics,
)
from app.processes.discovery import activities_on_a_cycle, discover_graph, discover_variants
from app.processes.traces import TraceEvent, build_traces, variant_key_for

BASE = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)


def event(case, activity, hours, *, team="ops", minutes=5, manual=False):
    start = BASE + timedelta(hours=hours)
    return TraceEvent(
        case_id=case,
        activity=activity,
        occurred_at=start,
        completed_at=start + timedelta(minutes=minutes),
        team=team,
        is_manual=manual,
        duration_ms=minutes * 60_000,
    )


class TestPercentile:
    def test_median_of_odd_sample(self):
        assert median([3, 1, 2]) == 2

    def test_median_of_even_sample_interpolates(self):
        assert median([1, 2, 3, 4]) == 2.5

    def test_bounds(self):
        values = [1, 2, 3, 4, 5]
        assert percentile(values, 0.0) == 1
        assert percentile(values, 1.0) == 5

    def test_empty_is_zero(self):
        assert percentile([], 0.5) == 0.0

    def test_tukey_fences_need_four_points(self):
        low, high = iqr_outlier_bounds([1, 2])
        assert low == float("-inf") and high == float("inf")


class TestTraces:
    def test_events_are_grouped_and_ordered(self):
        traces = build_traces(
            [
                event("A", "second", 2),
                event("A", "first", 1),
                event("B", "only", 1),
            ]
        )
        assert [t.case_id for t in traces] == ["A", "B"]
        assert traces[0].sequence == ("first", "second")

    def test_variant_key_is_stable_and_order_sensitive(self):
        assert variant_key_for(["a", "b"]) == variant_key_for(["a", "b"])
        assert variant_key_for(["a", "b"]) != variant_key_for(["b", "a"])

    def test_handoffs_count_owner_changes(self):
        trace = build_traces(
            [
                event("A", "one", 1, team="sales"),
                event("A", "two", 2, team="finance"),
                event("A", "three", 3, team="finance"),
                event("A", "four", 4, team="warehouse"),
            ]
        )[0]
        assert trace.handoff_count == 2

    def test_rework_counts_repeats_only(self):
        trace = build_traces(
            [
                event("A", "issue", 1),
                event("A", "fix", 2),
                event("A", "issue", 3),
                event("A", "issue", 4),
            ]
        )[0]
        assert trace.rework_count == 2
        assert trace.contains_loop()

    def test_waiting_is_throughput_minus_service(self):
        trace = build_traces(
            [event("A", "one", 0, minutes=30), event("A", "two", 10, minutes=30)]
        )[0]
        # 10h30m span, 1h of service time.
        assert trace.throughput_seconds == pytest.approx(10.5 * 3600)
        assert trace.waiting_seconds() == pytest.approx(9.5 * 3600)


class TestTransitions:
    def test_wait_is_measured_from_end_of_source(self):
        traces = build_traces(
            [event("A", "one", 0, minutes=60), event("A", "two", 3, minutes=5)]
        )
        transitions = transition_metrics(traces)
        assert len(transitions) == 1
        # Source ends at 01:00, target starts at 03:00 -> two hours of queue.
        assert transitions[0].median_wait_seconds == pytest.approx(2 * 3600)

    def test_negative_waits_are_clamped(self):
        overlapping = [
            TraceEvent("A", "one", BASE, BASE + timedelta(hours=5)),
            TraceEvent("A", "two", BASE + timedelta(hours=1), BASE + timedelta(hours=2)),
        ]
        transitions = transition_metrics(build_traces(overlapping))
        assert transitions[0].median_wait_seconds == 0.0


class TestGraph:
    def test_self_loop_is_detected(self):
        assert activities_on_a_cycle({("a", "a")}) == {"a"}

    def test_two_node_cycle_is_detected(self):
        assert activities_on_a_cycle({("a", "b"), ("b", "a")}) == {"a", "b"}

    def test_acyclic_graph_has_no_cycles(self):
        assert activities_on_a_cycle({("a", "b"), ("b", "c")}) == set()

    def test_graph_nodes_and_edges(self, demo_traces):
        graph = discover_graph(demo_traces)
        activities = {node.activity for node in graph.nodes}
        assert "Order received" in activities
        assert "Delivered" in activities
        assert graph.case_count == len(demo_traces)
        assert graph.start_activities.get("Order received") == len(demo_traces)

    def test_frequency_filter_removes_rare_edges(self, demo_traces):
        graph = discover_graph(demo_traces)
        simplified = graph.filter_by_frequency(0.5)
        assert len(simplified.edges) < len(graph.edges)

    def test_empty_log_gives_empty_graph(self):
        graph = discover_graph([])
        assert graph.nodes == [] and graph.edges == []


class TestVariants:
    def test_shares_sum_to_one(self, demo_traces):
        variants = discover_variants(demo_traces, sla_hours=72)
        assert sum(v["share"] for v in variants) == pytest.approx(1.0)

    def test_variants_are_sorted_by_frequency(self, demo_traces):
        variants = discover_variants(demo_traces)
        counts = [v["case_count"] for v in variants]
        assert counts == sorted(counts, reverse=True)


class TestSummary:
    def test_demo_log_summary_is_coherent(self, demo_traces):
        summary = summarize(demo_traces, sla_hours=72)
        assert summary.case_count == len(demo_traces)
        assert summary.event_count > summary.case_count
        assert 0.0 <= summary.waiting_share <= 1.0
        assert 0.0 <= summary.rework_case_ratio <= 1.0
        assert summary.throughput.median_seconds > 0

    def test_sla_breach_rate_bounds(self, demo_traces):
        assert sla_breach_rate(demo_traces, None) == 0.0
        assert sla_breach_rate(demo_traces, 0.0001) == 1.0

    def test_outliers_are_above_the_fence(self, demo_traces):
        outliers = outlier_cases(demo_traces)
        for outlier in outliers:
            assert outlier["throughput_seconds"] > outlier["threshold_seconds"]


class TestBeforeAfter:
    def test_improvement_is_flagged(self):
        def make(case_prefix, span_hours, count):
            events = []
            for index in range(count):
                offset = index * 24
                events.append(event(f"{case_prefix}{index}", "start", offset))
                events.append(event(f"{case_prefix}{index}", "end", offset + span_hours))
            return build_traces(events)

        before = make("b", 20, 10)
        after = make("a", 10, 10)
        result = compare_windows(before, after)
        cycle_delta = next(
            d for d in result["deltas"] if d["metric"] == "median_throughput_seconds"
        )
        assert cycle_delta["change"] < 0
        assert cycle_delta["improved"] is True
        assert "not a causal measurement" in result["note"]
