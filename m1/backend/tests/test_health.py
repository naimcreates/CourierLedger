"""Health endpoint tests.

The health endpoint must NOT claim the database is ok unless a real
connection succeeds. In this suite the (test-only) SQLite fallback is
configured, so a real round-trip succeeds and the endpoint reports
"sqlite-test-only" — never "postgresql".
"""

import app.api.health as health_module


def test_health_ok_with_test_database(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "courierledger-m1"
    assert body["database"] == "sqlite-test-only"
    assert body["database_ok"] is True


def test_health_reports_unavailable_when_connection_fails(client, monkeypatch):
    monkeypatch.setattr(health_module, "check_connection", lambda: False)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"  # the service is up; the DB is not
    assert body["database"] == "unavailable"
    assert body["database_ok"] is False


def test_health_never_claims_postgresql_without_postgres(client):
    resp = client.get("/api/health")
    assert resp.json()["database"] != "postgresql"
