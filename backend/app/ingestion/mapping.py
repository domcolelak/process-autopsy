"""CSV/XLSX profiling and mapping into the canonical event model.

Two things matter here beyond plumbing:

* **CSV formula injection** -- values starting with ``=``, ``+``, ``-`` or ``@``
  execute when the exported file is later opened in a spreadsheet. Every string
  we store is neutralised.
* **Explicit mapping** -- source schemas are never assumed to match. The wizard
  profiles the columns, proposes a mapping, and the user confirms it.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from pydantic import BaseModel, Field

MAX_ROWS = 500_000
MAX_CELL_LENGTH = 4096
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_TRUE_VALUES = {"1", "true", "yes", "y", "manual", "human"}
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)

#: Column names we recognise automatically, per canonical field.
_HEURISTICS: dict[str, tuple[str, ...]] = {
    "case_id": ("case_id", "case", "order_id", "ticket_id", "invoice_id", "process_id", "id"),
    "activity_name": ("activity", "activity_name", "step", "task", "event", "action", "status"),
    "occurred_at": ("occurred_at", "timestamp", "time", "started_at", "start", "created_at", "date"),
    "completed_at": ("completed_at", "end", "ended_at", "finished_at", "closed_at"),
    "actor_id": (
        "actor",
        "actor_id",
        "user",
        "user_id",
        "assignee",
        "owner",
        "agent",
        "handled_by",
        "processed_by",
        "performed_by",
    ),
    "team": ("team", "group", "department", "queue", "unit"),
    "source_system": ("source", "source_system", "system", "application"),
    "duration_ms": ("duration_ms", "duration", "elapsed_ms"),
    "monetary_value": ("amount", "value", "monetary_value", "total", "revenue", "price"),
    "object_type": ("object_type", "entity_type", "type"),
    "object_id": ("object_id", "entity_id", "reference"),
    "status_before": ("status_before", "from_status", "old_status"),
    "status_after": ("status_after", "to_status", "new_status"),
    "is_manual": ("is_manual", "manual", "channel", "automated"),
}

REQUIRED_FIELDS = ("case_id", "activity_name", "occurred_at")


class ColumnProfile(BaseModel):
    name: str
    non_empty: int
    distinct: int
    inferred_type: str
    samples: list[str] = Field(default_factory=list)
    suggested_field: str | None = None


class ImportProfile(BaseModel):
    row_count: int
    columns: list[ColumnProfile]
    suggested_mapping: dict[str, str]
    sample_rows: list[dict[str, str]]
    warnings: list[str] = Field(default_factory=list)


class EventMapping(BaseModel):
    """User-confirmed mapping from source column names to canonical fields."""

    case_id: str
    activity_name: str
    occurred_at: str
    completed_at: str | None = None
    actor_id: str | None = None
    actor_type: str | None = None
    team: str | None = None
    source_system: str | None = None
    source_event_id: str | None = None
    duration_ms: str | None = None
    monetary_value: str | None = None
    object_type: str | None = None
    object_id: str | None = None
    status_before: str | None = None
    status_after: str | None = None
    is_manual: str | None = None
    default_source_system: str = "upload"
    #: Values of ``is_manual`` that mean "a person did this".
    manual_true_values: list[str] = Field(default_factory=lambda: sorted(_TRUE_VALUES))


@dataclass
class NormalizationResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accepted(self) -> int:
        return len(self.events)

    @property
    def rejected(self) -> int:
        return len(self.errors)


def sanitize_cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection and clamp oversized cells."""
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    if len(cleaned) > MAX_CELL_LENGTH:
        cleaned = cleaned[:MAX_CELL_LENGTH]
    if cleaned.startswith(_FORMULA_PREFIXES):
        return "'" + cleaned
    return cleaned


def read_csv(content: bytes | str, *, max_rows: int = MAX_ROWS) -> list[dict[str, str]]:
    """Parse CSV/TSV content, tolerating BOMs and both common delimiters."""
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows: list[dict[str, str]] = []
    for index, row in enumerate(reader):
        if index >= max_rows:
            break
        rows.append({(k or "").strip(): sanitize_cell(v) for k, v in row.items() if k})
    return rows


def profile_rows(rows: list[dict[str, str]]) -> ImportProfile:
    """Describe the uploaded columns and propose a canonical mapping."""
    if not rows:
        return ImportProfile(
            row_count=0,
            columns=[],
            suggested_mapping={},
            sample_rows=[],
            warnings=["The uploaded file contains no data rows."],
        )

    column_names = list(rows[0].keys())
    profiles: list[ColumnProfile] = []
    for name in column_names:
        values = [str(row.get(name, "")) for row in rows]
        non_empty = [v for v in values if v not in ("", "None")]
        profiles.append(
            ColumnProfile(
                name=name,
                non_empty=len(non_empty),
                distinct=len(set(non_empty)),
                inferred_type=_infer_type(non_empty),
                samples=list(dict.fromkeys(non_empty))[:5],
            )
        )

    mapping = _suggest_mapping(profiles)
    for profile in profiles:
        for target, column in mapping.items():
            if column == profile.name:
                profile.suggested_field = target

    warnings = [
        f"No column could be matched to the required field '{field_name}'."
        for field_name in REQUIRED_FIELDS
        if field_name not in mapping
    ]

    return ImportProfile(
        row_count=len(rows),
        columns=profiles,
        suggested_mapping=mapping,
        sample_rows=rows[:5],
        warnings=warnings,
    )


def _infer_type(values: list[str]) -> str:
    if not values:
        return "empty"
    sample = values[:200]
    if all(parse_datetime(v) is not None for v in sample):
        return "datetime"
    if all(_is_number(v) for v in sample):
        return "number"
    if len(set(v.lower() for v in sample)) <= 2:
        return "boolean"
    if len(set(sample)) <= max(2, len(sample) // 10):
        return "categorical"
    return "string"


def _is_number(value: str) -> bool:
    try:
        float(value.replace(",", "."))
    except ValueError:
        return False
    return True


def _suggest_mapping(profiles: list[ColumnProfile]) -> dict[str, str]:
    """Match columns to canonical fields by normalised name, best score wins."""
    available = {p.name: _normalise(p.name) for p in profiles}
    by_type = {p.name: p.inferred_type for p in profiles}
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for target, keywords in _HEURISTICS.items():
        best: tuple[int, str] | None = None
        for name, normalised in available.items():
            if name in used:
                continue
            score = _match_score(normalised, keywords)
            if score == 0:
                continue
            if target in ("occurred_at", "completed_at") and by_type[name] != "datetime":
                continue
            if best is None or score > best[0]:
                best = (score, name)
        if best is not None:
            mapping[target] = best[1]
            used.add(best[1])

    # Last resort: any datetime column can serve as the timestamp.
    if "occurred_at" not in mapping:
        for name, inferred in by_type.items():
            if inferred == "datetime" and name not in used:
                mapping["occurred_at"] = name
                used.add(name)
                break
    return mapping


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _match_score(normalised: str, keywords: tuple[str, ...]) -> int:
    for index, keyword in enumerate(keywords):
        if normalised == keyword:
            return 100 - index
        if normalised.endswith("_" + keyword) or normalised.startswith(keyword + "_"):
            return 60 - index
        if keyword in normalised:
            return 30 - index
    return 0


def parse_datetime(value: Any) -> datetime | None:
    """Parse the timestamp formats that appear in exported operational data."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip().strip("'")
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(str(value).replace("'", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def normalize_rows(
    rows: Iterable[dict[str, Any]], mapping: EventMapping
) -> NormalizationResult:
    """Turn mapped source rows into canonical event dictionaries."""
    result = NormalizationResult()
    manual_values = {v.lower() for v in mapping.manual_true_values}

    for index, row in enumerate(rows):
        case_id = str(row.get(mapping.case_id, "")).strip().strip("'")
        activity = str(row.get(mapping.activity_name, "")).strip().strip("'")
        occurred_at = parse_datetime(row.get(mapping.occurred_at))

        problems = []
        if not case_id:
            problems.append("missing case_id")
        if not activity:
            problems.append("missing activity_name")
        if occurred_at is None:
            problems.append("unparseable occurred_at")
        if problems:
            result.errors.append({"row": index + 1, "problems": problems})
            continue

        completed_at = (
            parse_datetime(row.get(mapping.completed_at)) if mapping.completed_at else None
        )
        if completed_at is not None and completed_at < occurred_at:
            completed_at = None

        duration_value = _parse_float(row.get(mapping.duration_ms)) if mapping.duration_ms else None
        manual_raw = str(row.get(mapping.is_manual, "")).strip().lower() if mapping.is_manual else ""

        result.events.append(
            {
                "case_id": case_id,
                "activity_name": activity,
                "occurred_at": occurred_at,
                "completed_at": completed_at,
                "actor_id": _opt(row, mapping.actor_id),
                "actor_type": _opt(row, mapping.actor_type),
                "team": _opt(row, mapping.team),
                "source_system": _opt(row, mapping.source_system) or mapping.default_source_system,
                "source_event_id": _opt(row, mapping.source_event_id)
                or f"{case_id}:{activity}:{occurred_at.isoformat()}",
                "duration_ms": int(duration_value) if duration_value is not None else None,
                "monetary_value": _parse_float(row.get(mapping.monetary_value))
                if mapping.monetary_value
                else None,
                "object_type": _opt(row, mapping.object_type),
                "object_id": _opt(row, mapping.object_id),
                "status_before": _opt(row, mapping.status_before),
                "status_after": _opt(row, mapping.status_after),
                "is_manual": manual_raw in manual_values if manual_raw else False,
                "metadata": {},
            }
        )
    return result


def _opt(row: dict[str, Any], column: str | None) -> str | None:
    if not column:
        return None
    value = row.get(column)
    if value in (None, ""):
        return None
    return str(value).strip().strip("'")
