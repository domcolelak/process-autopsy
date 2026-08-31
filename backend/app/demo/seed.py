"""Deterministic demo dataset.

Simulates an order-to-delivery process containing problems the engine is
supposed to find:

* a slow approval handoff (finance queue) on high-value orders,
* an invoice correction loop that bounces between two teams,
* a manual ERP re-entry step performed by hand on every order,
* a rare exception path that takes far longer than the median,
* a deliberate degradation in the second half of the window.

The generator is seeded, so the demo produces the same findings on every run.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import DEMO_TENANT_SLUG, generate_api_key, hash_api_key
from app.models import NormalizedEvent, ProcessDefinition, Tenant, User
from app.processes.service import analyze_process

PROCESS_NAME = "Order to delivery"
DEMO_API_KEY = "pk_demo_process_autopsy"

TEAMS = {
    "Order received": ("Sales", "shop", False),
    "Order entered in ERP": ("Back office", "erp", True),
    "Stock checked": ("Warehouse", "erp", False),
    "Payment verified": ("Finance", "erp", False),
    "Approval requested": ("Back office", "mail", True),
    "Approval granted": ("Finance", "mail", True),
    "Invoice issued": ("Finance", "erp", False),
    "Invoice corrected": ("Back office", "erp", True),
    "Picked": ("Warehouse", "wms", False),
    "Shipped": ("Warehouse", "wms", False),
    "Delivered": ("Carrier", "carrier", False),
    "Customer query handled": ("Support", "helpdesk", True),
}


def ensure_demo_tenant(db: Session) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == DEMO_TENANT_SLUG))
    if tenant is None:
        tenant = Tenant(
            slug=DEMO_TENANT_SLUG,
            name="Demo workspace",
            api_key_hash=hash_api_key(DEMO_API_KEY),
            hourly_cost_eur=38.0,
        )
        db.add(tenant)
        db.flush()
        db.add(
            User(
                tenant_id=tenant.id,
                email="ops@demo.local",
                display_name="Demo operations lead",
                role="admin",
            )
        )
        db.flush()
    return tenant


def seed_demo(db: Session, *, case_count: int = 420, force: bool = False) -> dict:
    """Create the demo tenant, event log and a first analysis run."""
    tenant = ensure_demo_tenant(db)

    process = db.scalar(
        select(ProcessDefinition).where(
            ProcessDefinition.tenant_id == tenant.id,
            ProcessDefinition.name == PROCESS_NAME,
        )
    )
    if process is not None and not force:
        existing = db.scalar(
            select(func.count(NormalizedEvent.id)).where(
                NormalizedEvent.tenant_id == tenant.id,
                NormalizedEvent.process_id == process.id,
            )
        )
        if existing:
            return {
                "tenant_id": str(tenant.id),
                "process_id": str(process.id),
                "created": False,
                "event_count": existing,
            }

    if process is None:
        process = ProcessDefinition(
            tenant_id=tenant.id,
            name=PROCESS_NAME,
            description=(
                "Synthetic order process spanning shop, ERP, warehouse and carrier, "
                "generated for the demo workspace."
            ),
            sla_hours=72.0,
        )
        db.add(process)
        db.flush()

    events = generate_event_log(case_count=case_count, seed=settings.demo_random_seed)
    db.add_all(
        [
            NormalizedEvent(
                tenant_id=tenant.id,
                process_id=process.id,
                source_system=event["source_system"],
                source_event_id=event["source_event_id"],
                case_id=event["case_id"],
                activity_name=event["activity_name"],
                occurred_at=event["occurred_at"],
                completed_at=event["completed_at"],
                actor_id=event["actor_id"],
                actor_type="human" if event["is_manual"] else "system",
                team=event["team"],
                duration_ms=event["duration_ms"],
                object_type="order",
                object_id=event["case_id"],
                monetary_value=event["monetary_value"],
                is_manual=event["is_manual"],
                event_metadata={"segment": event["segment"]},
            )
            for event in events
        ]
    )
    db.flush()

    analysis = analyze_process(db, tenant.id, process.id)
    return {
        "tenant_id": str(tenant.id),
        "process_id": str(process.id),
        "created": True,
        "event_count": len(events),
        "analysis": analysis,
    }


def generate_event_log(*, case_count: int = 420, seed: int = 20260830) -> list[dict]:
    """Build the synthetic event log. Pure function -- no database involved."""
    rng = random.Random(seed)
    start = datetime(2026, 4, 1, 7, 0, tzinfo=timezone.utc)
    events: list[dict] = []

    for index in range(case_count):
        case_id = f"ORD-{10_000 + index}"
        # Cases arrive roughly every two hours across a ~35 day window.
        opened = start + timedelta(hours=2 * index, minutes=rng.randint(0, 90))
        # Second half of the window degrades: approvals queue up.
        degraded = index > case_count * 0.55
        segment = rng.choices(["retail", "b2b", "key_account"], weights=[6, 3, 1])[0]
        value = {
            "retail": rng.uniform(40, 400),
            "b2b": rng.uniform(400, 4_000),
            "key_account": rng.uniform(4_000, 25_000),
        }[segment]
        needs_approval = value > 2_000 or segment == "key_account"

        clock = opened
        sequence: list[tuple[str, float, float]] = []  # (activity, wait_h, service_min)

        sequence.append(("Order received", 0.0, 1.0))
        # Manual ERP re-entry: the automation candidate.
        sequence.append(("Order entered in ERP", rng.uniform(0.2, 2.5), rng.uniform(6, 11)))
        sequence.append(("Stock checked", rng.uniform(0.1, 1.5), rng.uniform(1, 4)))
        sequence.append(("Payment verified", rng.uniform(0.2, 3.0), rng.uniform(2, 6)))

        if needs_approval:
            # The bottleneck: a queue in front of the finance approval.
            base_wait = rng.uniform(6, 20)
            if degraded:
                base_wait *= rng.uniform(1.6, 2.4)
            sequence.append(("Approval requested", rng.uniform(0.3, 2.0), rng.uniform(4, 9)))
            sequence.append(("Approval granted", base_wait, rng.uniform(5, 12)))

        sequence.append(("Invoice issued", rng.uniform(0.3, 2.0), rng.uniform(2, 5)))

        # Invoice correction loop -- rework on roughly one in six cases.
        if rng.random() < 0.17:
            for _ in range(rng.choice([1, 1, 2])):
                sequence.append(("Invoice corrected", rng.uniform(1.0, 6.0), rng.uniform(8, 18)))
                sequence.append(("Invoice issued", rng.uniform(0.5, 3.0), rng.uniform(2, 5)))

        sequence.append(("Picked", rng.uniform(1.0, 8.0), rng.uniform(4, 12)))
        sequence.append(("Shipped", rng.uniform(0.5, 4.0), rng.uniform(2, 6)))

        # Rare, expensive exception path.
        if rng.random() < 0.05:
            sequence.append(("Customer query handled", rng.uniform(4, 26), rng.uniform(10, 30)))
            sequence.append(("Shipped", rng.uniform(6, 30), rng.uniform(2, 6)))

        sequence.append(("Delivered", rng.uniform(12, 48), rng.uniform(1, 3)))

        for step_index, (activity, wait_hours, service_minutes) in enumerate(sequence):
            clock = clock + timedelta(hours=wait_hours)
            team, system, manual = TEAMS[activity]
            duration_ms = int(service_minutes * 60_000)
            events.append(
                {
                    "case_id": case_id,
                    "activity_name": activity,
                    "occurred_at": clock,
                    "completed_at": clock + timedelta(milliseconds=duration_ms),
                    "actor_id": f"{team.lower().replace(' ', '_')}_{rng.randint(1, 4)}",
                    "team": team,
                    "source_system": system,
                    "source_event_id": f"{case_id}-{step_index}",
                    "duration_ms": duration_ms,
                    "monetary_value": round(value, 2),
                    "is_manual": manual,
                    "segment": segment,
                }
            )
            clock = clock + timedelta(milliseconds=duration_ms)

    return events


def demo_csv(*, case_count: int = 60, seed: int = 20260830) -> str:
    """Small CSV export used to exercise the import wizard end to end."""
    rows = generate_event_log(case_count=case_count, seed=seed)
    header = "order_id,step,timestamp,finished_at,handled_by,department,system,amount,channel"
    lines = [header]
    for row in rows:
        channel = "manual" if row["is_manual"] else "automated"
        lines.append(
            ",".join(
                [
                    row["case_id"],
                    row["activity_name"],
                    row["occurred_at"].isoformat(),
                    row["completed_at"].isoformat(),
                    row["actor_id"],
                    row["team"],
                    row["source_system"],
                    f"{row['monetary_value']:.2f}",
                    channel,
                ]
            )
        )
    return "\n".join(lines)


def reset_demo(db: Session, tenant_id: uuid.UUID) -> None:  # pragma: no cover - admin helper
    db.execute(
        NormalizedEvent.__table__.delete().where(NormalizedEvent.tenant_id == tenant_id)
    )
    db.flush()
