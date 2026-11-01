"""Observability tests (Phase 2, Part 8).

Covers: the X-Request-ID header (generated + client-echoed),
request ids appearing in structured log lines, structured log
lines parsing as JSON, and GET /api/ready (200 with the database
up, 503 with it down — the 503 path is exercised by monkeypatching
check_connection, i.e. by simulating a down database, not by
stopping a real one).

These tests run against the SQLite test-only fallback, so the
/ready 200 case proves the endpoint logic, not PostgreSQL
readiness: a real readiness claim still needs the live cluster.
"""

import json
import logging
import re
import uuid

import pytest

import app.api.health as health_module
from app.logging_config import JsonFormatter, request_id_var

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def test_request_id_header_generated_and_valid_uuid(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    request_id = resp.headers.get("x-request-id")
    assert request_id is not None
    assert UUID_RE.match(request_id), request_id


def test_client_supplied_request_id_is_echoed(client):
    resp = client.get("/api/health", headers={"X-Request-ID": "demo-req-123"})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "demo-req-123"


def test_invalid_client_request_id_is_replaced_not_reflected(client):
    # Spaces / slashes fail the middleware's allow-list, so the
    # value must never reach the response header (or the logs).
    resp = client.get("/api/health", headers={"X-Request-ID": "bad id/with spaces"})
    assert resp.status_code == 200
    request_id = resp.headers["x-request-id"]
    assert request_id != "bad id/with spaces"
    assert UUID_RE.match(request_id), request_id


def test_request_id_appears_in_structured_log_line(client, caplog):
    caplog.set_level(logging.INFO, logger="app.main")
    resp = client.get("/api/health", headers={"X-Request-ID": "log-check-42"})
    assert resp.status_code == 200
    matches = [
        r for r in caplog.records
        if getattr(r, "request_id", None) == "log-check-42"
    ]
    assert matches, "no log record carried the request id"
    assert all(r.name == "app.main" for r in matches)


def test_json_formatter_line_parses_as_json_and_carries_request_id():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    token = request_id_var.set("fmt-check-1")
    try:
        line = formatter.format(record)
    finally:
        request_id_var.reset(token)
    parsed = json.loads(line)  # must parse: one JSON object per line
    assert parsed["message"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "app.test"
    assert parsed["request_id"] == "fmt-check-1"
    assert "ts" in parsed


def test_json_formatter_omits_request_id_outside_a_request():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="startup", args=(), exc_info=None,
    )
    assert request_id_var.get() is None
    parsed = json.loads(formatter.format(record))
    assert "request_id" not in parsed


def test_ready_returns_200_when_database_up(client):
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["database_ok"] is True


def test_ready_returns_503_when_database_down(client, monkeypatch):
    # Simulated down database: check_connection is stubbed to
    # False. No real database is stopped.
    monkeypatch.setattr(health_module, "check_connection", lambda: False)
    resp = client.get("/api/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["database_ok"] is False


def test_health_stays_200_when_database_down_but_ready_is_503(client, monkeypatch):
    # Liveness vs readiness are distinct: the service is up even
    # when the database is not.
    monkeypatch.setattr(health_module, "check_connection", lambda: False)
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["database_ok"] is False
    ready = client.get("/api/ready")
    assert ready.status_code == 503


def test_ready_also_carries_request_id(client):
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    assert UUID_RE.match(resp.headers["x-request-id"])
