from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point the app at a throwaway database before any application module is imported.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="process-autopsy-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{_TMP_DIR / 'test.db'}")
os.environ.setdefault("SEED_DEMO_ON_STARTUP", "false")
os.environ.setdefault("AI_PROVIDER", "offline")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.demo.seed import ensure_demo_tenant, generate_event_log, seed_demo  # noqa: E402
from app.main import app  # noqa: E402
from app.processes.traces import TraceEvent, build_traces  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture()
def tenant(db):
    tenant = ensure_demo_tenant(db)
    db.commit()
    return tenant


@pytest.fixture()
def seeded(db, tenant):
    result = seed_demo(db, case_count=120)
    db.commit()
    return result


@pytest.fixture()
def client(seeded):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def demo_traces():
    events = generate_event_log(case_count=200)
    return build_traces(
        [
            TraceEvent(
                case_id=event["case_id"],
                activity=event["activity_name"],
                occurred_at=event["occurred_at"],
                completed_at=event["completed_at"],
                actor=event["actor_id"],
                team=event["team"],
                source_system=event["source_system"],
                is_manual=event["is_manual"],
                duration_ms=event["duration_ms"],
                monetary_value=event["monetary_value"],
            )
            for event in events
        ]
    )
