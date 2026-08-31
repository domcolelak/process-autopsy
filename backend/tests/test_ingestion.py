"""Tests for CSV profiling, sanitisation and canonical normalisation."""
from __future__ import annotations

from datetime import datetime, timezone

from app.ingestion.mapping import (
    EventMapping,
    normalize_rows,
    parse_datetime,
    profile_rows,
    read_csv,
    sanitize_cell,
)


class TestSanitisation:
    def test_formula_prefixes_are_neutralised(self):
        for dangerous in ("=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(A1)"):
            assert sanitize_cell(dangerous).startswith("'")

    def test_ordinary_values_are_untouched(self):
        assert sanitize_cell("ORD-1001") == "ORD-1001"
        assert sanitize_cell(" Approval granted ") == "Approval granted"

    def test_oversized_cells_are_clamped(self):
        assert len(sanitize_cell("x" * 10_000)) == 4096

    def test_non_strings_pass_through(self):
        assert sanitize_cell(42) == 42
        assert sanitize_cell(None) is None

    def test_csv_upload_is_sanitised(self):
        rows = read_csv("id,note\n1,=HYPERLINK(\"http://x\")\n")
        assert rows[0]["note"].startswith("'=")


class TestDateParsing:
    def test_iso_with_zone(self):
        parsed = parse_datetime("2026-05-01T08:00:00+02:00")
        assert parsed.utcoffset().total_seconds() == 7200

    def test_zulu_suffix(self):
        assert parse_datetime("2026-05-01T08:00:00Z").tzinfo is not None

    def test_naive_is_assumed_utc(self):
        assert parse_datetime("2026-05-01 08:00:00").tzinfo == timezone.utc

    def test_european_format(self):
        assert parse_datetime("01.05.2026 08:00") == datetime(
            2026, 5, 1, 8, 0, tzinfo=timezone.utc
        )

    def test_garbage_returns_none(self):
        assert parse_datetime("not a date") is None
        assert parse_datetime("") is None


class TestProfiling:
    def test_delimiter_is_detected(self):
        rows = read_csv("a;b\n1;2\n")
        assert rows == [{"a": "1", "b": "2"}]

    def test_bom_is_stripped(self):
        rows = read_csv("﻿case_id,activity\nA,Start\n".encode("utf-8"))
        assert "case_id" in rows[0]

    def test_columns_are_matched_to_canonical_fields(self):
        rows = read_csv(
            "order_id,step,timestamp,department\n"
            "ORD-1,Received,2026-05-01T08:00:00Z,Sales\n"
            "ORD-1,Shipped,2026-05-02T08:00:00Z,Warehouse\n"
        )
        profile = profile_rows(rows)
        assert profile.suggested_mapping["case_id"] == "order_id"
        assert profile.suggested_mapping["activity_name"] == "step"
        assert profile.suggested_mapping["occurred_at"] == "timestamp"
        assert profile.suggested_mapping["team"] == "department"
        assert profile.warnings == []

    def test_missing_required_field_produces_a_warning(self):
        profile = profile_rows(read_csv("foo,bar\n1,2\n"))
        assert any("case_id" in warning for warning in profile.warnings)

    def test_empty_file(self):
        profile = profile_rows([])
        assert profile.row_count == 0
        assert profile.warnings

    def test_a_column_is_never_mapped_twice(self):
        rows = read_csv("id,activity,timestamp\nA,Start,2026-05-01T08:00:00Z\n")
        mapping = profile_rows(rows).suggested_mapping
        assert len(set(mapping.values())) == len(mapping.values())


class TestNormalisation:
    def _rows(self):
        return read_csv(
            "order_id,step,timestamp,finished_at,department,channel,amount\n"
            "ORD-1,Received,2026-05-01T08:00:00Z,2026-05-01T08:05:00Z,Sales,manual,120.50\n"
            "ORD-1,Shipped,2026-05-02T08:00:00Z,,Warehouse,automated,120.50\n"
        )

    def _mapping(self):
        return EventMapping(
            case_id="order_id",
            activity_name="step",
            occurred_at="timestamp",
            completed_at="finished_at",
            team="department",
            is_manual="channel",
            monetary_value="amount",
        )

    def test_rows_become_canonical_events(self):
        result = normalize_rows(self._rows(), self._mapping())
        assert result.accepted == 2
        assert result.rejected == 0
        first = result.events[0]
        assert first["case_id"] == "ORD-1"
        assert first["is_manual"] is True
        assert first["monetary_value"] == 120.50
        assert first["team"] == "Sales"

    def test_missing_completed_at_is_none(self):
        result = normalize_rows(self._rows(), self._mapping())
        assert result.events[1]["completed_at"] is None

    def test_source_event_id_is_generated_when_absent(self):
        result = normalize_rows(self._rows(), self._mapping())
        ids = {event["source_event_id"] for event in result.events}
        assert len(ids) == 2
        assert all(identifier.startswith("ORD-1:") for identifier in ids)

    def test_invalid_rows_are_reported_not_dropped_silently(self):
        rows = read_csv("order_id,step,timestamp\n,Received,2026-05-01T08:00:00Z\n")
        result = normalize_rows(
            rows,
            EventMapping(case_id="order_id", activity_name="step", occurred_at="timestamp"),
        )
        assert result.accepted == 0
        assert result.errors == [{"row": 1, "problems": ["missing case_id"]}]

    def test_completed_before_started_is_discarded(self):
        rows = read_csv(
            "order_id,step,timestamp,finished_at\n"
            "ORD-1,Received,2026-05-02T08:00:00Z,2026-05-01T08:00:00Z\n"
        )
        result = normalize_rows(
            rows,
            EventMapping(
                case_id="order_id",
                activity_name="step",
                occurred_at="timestamp",
                completed_at="finished_at",
            ),
        )
        assert result.events[0]["completed_at"] is None
