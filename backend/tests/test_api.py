"""End-to-end API tests, including the import wizard and tenant isolation."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from app.core.security import hash_api_key
from app.demo.seed import DEMO_API_KEY, demo_csv
from app.models import Finding, NormalizedEvent, ProcessDefinition, Tenant


class TestHealthAndOverview:
    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_overview_reports_recoverable_time(self, client):
        body = client.get("/v1/overview").json()
        assert body["process_count"] >= 1
        assert body["case_count"] > 0
        assert body["recoverable_hours_per_month"] > 0
        assert body["top_opportunity"] is not None

    def test_openapi_is_served(self, client):
        assert client.get("/openapi.json").status_code == 200


class TestProcesses:
    def test_list_and_analyze(self, client):
        processes = client.get("/v1/processes").json()
        assert processes, "the demo process should be present"
        process = processes[0]
        assert process["case_count"] > 0

        result = client.post(f"/v1/processes/{process['id']}/analyze").json()
        assert result["case_count"] == process["case_count"]
        assert result["findings"] >= 1

    def test_map_returns_a_connected_graph(self, client):
        process_id = client.get("/v1/processes").json()[0]["id"]
        graph = client.get(f"/v1/processes/{process_id}/map").json()
        activities = {node["activity"] for node in graph["nodes"]}
        assert "Order received" in activities
        for edge in graph["edges"]:
            assert edge["source"] in activities
            assert edge["target"] in activities

    def test_map_frequency_filter(self, client):
        process_id = client.get("/v1/processes").json()[0]["id"]
        full = client.get(f"/v1/processes/{process_id}/map").json()
        filtered = client.get(
            f"/v1/processes/{process_id}/map", params={"min_edge_case_share": 0.5}
        ).json()
        assert len(filtered["edges"]) <= len(full["edges"])

    def test_variants_and_metrics(self, client):
        process_id = client.get("/v1/processes").json()[0]["id"]
        variants = client.get(f"/v1/processes/{process_id}/variants").json()
        assert variants
        assert sum(v["share"] for v in variants) <= 1.0000001

        metrics = client.get(f"/v1/processes/{process_id}/metrics").json()
        assert metrics["case_count"] > 0
        assert metrics["throughput"]["median_seconds"] > 0

    def test_case_timeline(self, client):
        process_id = client.get("/v1/processes").json()[0]["id"]
        variants = client.get(f"/v1/processes/{process_id}/variants").json()
        case_id = variants[0]["example_case_ids"][0]
        timeline = client.get(f"/v1/processes/{process_id}/cases/{case_id}").json()
        assert timeline["case_id"] == case_id
        assert timeline["steps"]
        assert timeline["steps"][0]["wait_before_seconds"] == 0

    def test_unknown_case_is_404(self, client):
        process_id = client.get("/v1/processes").json()[0]["id"]
        assert client.get(f"/v1/processes/{process_id}/cases/nope").status_code == 404

    def test_unknown_process_is_404(self, client):
        assert client.get(f"/v1/processes/{uuid.uuid4()}/map").status_code == 404

    def test_before_after(self, client):
        process_id = client.get("/v1/processes").json()[0]["id"]
        metrics = client.get(f"/v1/processes/{process_id}/metrics").json()
        start = datetime.fromisoformat(metrics["window_start"])
        end = datetime.fromisoformat(metrics["window_end"])
        midpoint = start + (end - start) / 2

        response = client.post(
            f"/v1/processes/{process_id}/before-after",
            json={"split_at": midpoint.isoformat()},
        )
        body = response.json()
        assert body["before_case_count"] > 0
        assert body["after_case_count"] > 0
        assert {d["metric"] for d in body["deltas"]} >= {"median_throughput_seconds"}


class TestFindingsApi:
    def test_findings_are_ranked_and_filterable(self, client):
        findings = client.get("/v1/findings").json()
        assert findings
        scores = [f["impact_score"] for f in findings]
        assert scores == sorted(scores, reverse=True)

        critical = client.get("/v1/findings", params={"severity": "critical"}).json()
        assert all(f["severity"] == "critical" for f in critical)

    def test_status_transition_is_persisted(self, client):
        finding_id = client.get("/v1/findings").json()[0]["id"]
        updated = client.post(
            f"/v1/findings/{finding_id}/status", json={"status": "acknowledged"}
        ).json()
        assert updated["status"] == "acknowledged"
        assert client.get(f"/v1/findings/{finding_id}").json()["status"] == "acknowledged"

    def test_invalid_status_is_rejected(self, client):
        finding_id = client.get("/v1/findings").json()[0]["id"]
        response = client.post(f"/v1/findings/{finding_id}/status", json={"status": "whatever"})
        assert response.status_code == 422

    def test_explain_uses_the_offline_provider(self, client):
        finding_id = client.get("/v1/findings").json()[0]["id"]
        body = client.post(f"/v1/findings/{finding_id}/explain").json()
        assert body["narrative"]["model"] == "offline-deterministic"
        assert body["narrative"]["headline"]

    def test_findings_evidence_carries_numbers(self, client):
        for finding in client.get("/v1/findings").json():
            assert finding["evidence"]
            assert finding["affected_case_count"] > 0
            assert 0 < finding["confidence"] <= 0.95


class TestOpportunitiesApi:
    def test_ranked_with_components(self, client):
        opportunities = client.get("/v1/opportunities").json()
        assert opportunities
        assert opportunities[0]["components"]["manuality"] > 0
        assert opportunities[0]["estimated_eur_per_month"] > 0

    def test_status_update(self, client):
        opportunity_id = client.get("/v1/opportunities").json()[0]["id"]
        body = client.post(
            f"/v1/opportunities/{opportunity_id}/status", json={"status": "planned"}
        ).json()
        assert body["status"] == "planned"

    def test_explain(self, client):
        opportunity_id = client.get("/v1/opportunities").json()[0]["id"]
        body = client.post(f"/v1/opportunities/{opportunity_id}/explain").json()
        assert body["narrative"]["summary"]


class TestImportWizard:
    def test_upload_profile_map_and_analyze(self, client):
        csv_bytes = demo_csv(case_count=40).encode("utf-8")
        upload = client.post(
            "/v1/imports", files={"file": ("orders.csv", csv_bytes, "text/csv")}
        )
        assert upload.status_code == 201
        profile = upload.json()
        assert profile["row_count"] > 0
        assert profile["suggested_mapping"]["case_id"] == "order_id"
        assert profile["suggested_mapping"]["activity_name"] == "step"
        assert profile["suggested_mapping"]["occurred_at"] == "timestamp"
        assert not profile["warnings"]

        mapping = {
            "case_id": "order_id",
            "activity_name": "step",
            "occurred_at": "timestamp",
            "completed_at": "finished_at",
            "actor_id": "handled_by",
            "team": "department",
            "source_system": "system",
            "monetary_value": "amount",
            "is_manual": "channel",
        }
        applied = client.post(
            f"/v1/imports/{profile['import_id']}/mapping",
            json={
                "process_name": "Imported orders",
                "sla_hours": 72,
                "mapping": mapping,
                "analyze": True,
            },
        )
        assert applied.status_code == 200
        body = applied.json()
        assert body["accepted"] > 0
        assert body["rejected"] == 0
        assert body["analysis"]["case_count"] == 40

    def test_reapplying_the_same_import_is_idempotent(self, client):
        csv_bytes = demo_csv(case_count=10).encode("utf-8")
        mapping = {"case_id": "order_id", "activity_name": "step", "occurred_at": "timestamp"}

        first = client.post("/v1/imports", files={"file": ("a.csv", csv_bytes, "text/csv")}).json()
        applied = client.post(
            f"/v1/imports/{first['import_id']}/mapping",
            json={"process_name": "Idempotency check", "mapping": mapping, "analyze": False},
        ).json()

        second = client.post("/v1/imports", files={"file": ("a.csv", csv_bytes, "text/csv")}).json()
        again = client.post(
            f"/v1/imports/{second['import_id']}/mapping",
            json={
                "process_id": applied["process_id"],
                "mapping": mapping,
                "analyze": False,
            },
        ).json()
        assert applied["accepted"] > 0
        assert again["accepted"] == 0, "already stored events must not be duplicated"

    def test_bad_mapping_reports_rejected_rows(self, client):
        csv_bytes = b"order_id,step,timestamp\n,,\nORD-1,Received,not-a-date\n"
        profile = client.post(
            "/v1/imports", files={"file": ("bad.csv", csv_bytes, "text/csv")}
        ).json()
        body = client.post(
            f"/v1/imports/{profile['import_id']}/mapping",
            json={
                "process_name": "Broken import",
                "mapping": {
                    "case_id": "order_id",
                    "activity_name": "step",
                    "occurred_at": "timestamp",
                },
                "analyze": False,
            },
        ).json()
        assert body["accepted"] == 0
        assert body["rejected"] == 2
        assert body["errors"][0]["problems"]


class TestEventApi:
    def test_batch_ingestion_is_idempotent(self, client):
        events = [
            {
                "case_id": "API-1",
                "activity_name": "Created",
                "occurred_at": "2026-06-01T08:00:00+00:00",
                "source_event_id": "api-1-created",
            },
            {
                "case_id": "API-1",
                "activity_name": "Closed",
                "occurred_at": "2026-06-01T12:00:00+00:00",
                "source_event_id": "api-1-closed",
            },
        ]
        first = client.post(
            "/v1/events/batch", json={"process_name": "API process", "events": events}
        ).json()
        assert first["accepted"] == 2

        repeat = client.post(
            "/v1/events/batch",
            json={"process_id": first["process_id"], "events": events},
        ).json()
        assert repeat["accepted"] == 0
        assert repeat["duplicates"] == 2

    def test_empty_batch_is_rejected(self, client):
        response = client.post("/v1/events/batch", json={"events": []})
        assert response.status_code == 422


class TestReports:
    def test_report_contains_computed_numbers(self, client):
        process_id = client.get("/v1/processes").json()[0]["id"]
        report = client.post(
            "/v1/reports", json={"process_id": process_id, "include_ai_summary": True}
        ).json()
        assert "Automation opportunities" in report["body"]
        assert "Cases analysed" in report["body"]
        assert report["payload"]["summary"]["case_count"] > 0
        assert client.get(f"/v1/reports/{report['id']}").status_code == 200


class TestTenantIsolation:
    def test_other_tenant_sees_no_data(self, client, db):
        other = Tenant(
            slug="other-co",
            name="Other company",
            api_key_hash=hash_api_key("pk_other_tenant_key"),
        )
        db.add(other)
        db.commit()

        headers = {"X-API-Key": "pk_other_tenant_key"}
        assert client.get("/v1/processes", headers=headers).json() == []
        assert client.get("/v1/findings", headers=headers).json() == []
        assert client.get("/v1/overview", headers=headers).json()["case_count"] == 0

    def test_cross_tenant_object_access_is_404(self, client, db):
        other = Tenant(
            slug="snooper",
            name="Snooper",
            api_key_hash=hash_api_key("pk_snooper_key"),
        )
        db.add(other)
        db.commit()

        demo_process = db.scalar(select(ProcessDefinition))
        demo_finding = db.scalar(select(Finding))
        headers = {"X-API-Key": "pk_snooper_key"}

        assert client.get(f"/v1/processes/{demo_process.id}/map", headers=headers).status_code == 404
        assert client.get(f"/v1/findings/{demo_finding.id}", headers=headers).status_code == 404

    def test_invalid_api_key_is_401(self, client):
        response = client.get("/v1/processes", headers={"X-API-Key": "pk_not_a_real_key"})
        assert response.status_code == 401

    def test_demo_key_works(self, client):
        response = client.get("/v1/processes", headers={"X-API-Key": DEMO_API_KEY})
        assert response.status_code == 200
        assert response.json()

    def test_events_are_scoped_to_the_demo_tenant(self, db):
        demo = db.scalar(select(Tenant).where(Tenant.slug == "demo"))
        events = db.scalars(select(NormalizedEvent)).all()
        assert events
        assert all(event.tenant_id == demo.id for event in events)
