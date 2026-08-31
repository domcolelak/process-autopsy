"""Tests for the finding engine and the opportunity scorer."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.findings.engine import FindingConfig, detect_findings
from app.opportunities.scoring import score_opportunities
from app.processes.traces import TraceEvent, build_traces

BASE = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)


def log(sequences):
    """Build traces from ``[(case, [(activity, hour_offset, minutes, team, manual)])]``."""
    events = []
    for case_id, steps in sequences:
        for activity, offset, minutes, team, manual in steps:
            start = BASE + timedelta(hours=offset)
            events.append(
                TraceEvent(
                    case_id=case_id,
                    activity=activity,
                    occurred_at=start,
                    completed_at=start + timedelta(minutes=minutes),
                    team=team,
                    is_manual=manual,
                    duration_ms=minutes * 60_000,
                )
            )
    return build_traces(events)


class TestFindingEngine:
    def test_small_logs_produce_nothing(self):
        traces = log([(f"c{i}", [("a", i * 24, 5, "ops", False)]) for i in range(3)])
        assert detect_findings(traces) == []

    def test_slow_handoff_is_detected(self):
        sequences = []
        for index in range(60):
            day = index * 24
            sequences.append(
                (
                    f"c{index}",
                    [
                        ("request", day, 5, "sales", False),
                        # 20 hours of queue before the approval.
                        ("approve", day + 20, 5, "finance", False),
                        ("ship", day + 21, 5, "warehouse", False),
                    ],
                )
            )
        findings = detect_findings(log(sequences))
        waiting = [f for f in findings if f.finding_type == "excessive_waiting"]
        assert waiting, "expected the slow approval handoff to be reported"
        top = waiting[0]
        assert top.evidence["source_activity"] == "request"
        assert top.evidence["target_activity"] == "approve"
        assert top.evidence["median_wait_hours"] == pytest.approx(19.9, abs=0.3)
        assert top.affected_case_count == 60

    def test_repeated_activity_is_detected(self):
        sequences = []
        for index in range(60):
            day = index * 24
            steps = [("issue", day, 5, "finance", False)]
            if index % 2 == 0:
                steps.append(("fix", day + 1, 10, "back office", True))
                steps.append(("issue", day + 2, 5, "finance", False))
            steps.append(("send", day + 3, 5, "finance", False))
            sequences.append((f"c{index}", steps))
        findings = detect_findings(log(sequences))
        rework = [f for f in findings if f.finding_type == "repeated_activity"]
        assert rework
        assert rework[0].evidence["activity"] == "issue"
        assert rework[0].evidence["rework_case_ratio"] == pytest.approx(0.5, abs=0.02)

    def test_findings_are_ranked_by_impact(self, demo_traces):
        findings = detect_findings(demo_traces, sla_hours=72)
        scores = [f.impact_score for f in findings]
        assert scores == sorted(scores, reverse=True)

    def test_fingerprint_is_stable_across_runs(self, demo_traces):
        first = {f.fingerprint for f in detect_findings(demo_traces, sla_hours=72)}
        second = {f.fingerprint for f in detect_findings(demo_traces, sla_hours=72)}
        assert first == second

    def test_confidence_never_reaches_certainty(self, demo_traces):
        for finding in detect_findings(demo_traces, sla_hours=72):
            assert 0.0 < finding.confidence <= 0.95

    def test_demo_log_surfaces_the_planted_problems(self, demo_traces):
        types = {f.finding_type for f in detect_findings(demo_traces, sla_hours=72)}
        assert "excessive_waiting" in types
        assert "repeated_activity" in types
        assert "high_manual_repetition" in types

    def test_thresholds_are_configurable(self, demo_traces):
        strict = FindingConfig(min_cases=10_000)
        assert detect_findings(demo_traces, config=strict) == []


class TestOpportunityScoring:
    def test_manual_repeated_step_outranks_automated_one(self):
        sequences = []
        for index in range(80):
            day = index * 24
            sequences.append(
                (
                    f"c{index}",
                    [
                        ("manual retype", day, 9, "back office", True),
                        ("auto sync", day + 1, 9, "system", False),
                    ],
                )
            )
        opportunities = score_opportunities(log(sequences), min_occurrences=10)
        by_activity = {o.activity: o for o in opportunities}
        assert by_activity["manual retype"].score > by_activity["auto sync"].score

    def test_components_multiply_to_the_score(self, demo_traces):
        for opportunity in score_opportunities(demo_traces):
            expected = round(100 * opportunity.components.product(), 2)
            assert opportunity.score == pytest.approx(expected, abs=0.01)

    def test_every_component_is_exposed(self, demo_traces):
        opportunity = score_opportunities(demo_traces)[0]
        assert set(opportunity.components.as_dict()) == {
            "frequency",
            "time_cost",
            "manuality",
            "repeatability",
            "stability",
            "business_impact",
            "confidence",
        }

    def test_low_volume_activities_are_skipped(self, demo_traces):
        opportunities = score_opportunities(demo_traces, min_occurrences=10_000)
        assert opportunities == []

    def test_manual_erp_entry_is_the_top_demo_candidate(self, demo_traces):
        top = score_opportunities(demo_traces)[0]
        assert top.activity == "Order entered in ERP"
        assert top.estimated_hours_per_month > 0
        assert top.recommendation["approach"] in {
            "rule_based_automation",
            "integration",
            "assisted",
            "monitor",
        }

    def test_euro_estimate_follows_the_hourly_rate(self, demo_traces):
        cheap = score_opportunities(demo_traces, hourly_cost_eur=10.0)[0]
        pricey = score_opportunities(demo_traces, hourly_cost_eur=100.0)[0]
        assert pricey.estimated_eur_per_month == pytest.approx(
            cheap.estimated_eur_per_month * 10, rel=1e-6
        )
