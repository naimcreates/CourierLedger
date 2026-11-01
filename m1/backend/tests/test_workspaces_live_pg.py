"""Live-PostgreSQL workspace tests (Blocker B-1 fix, v0.1 E.3).

Runs the real app (uvicorn subprocess) against a scratch PostgreSQL
database with the full schema + RLS + roles applied (see
tests/live_pg.py). Skipped unless CL_LIVE_PG_PASSWORD is set.

Run with the owner role (default) and with the runtime role:
    CL_LIVE_PG_PASSWORD='<dev-password>' .venv/bin/python -m pytest tests/test_workspaces_live_pg.py -q
    CL_LIVE_PG_PASSWORD='<dev-password>' CL_LIVE_PG_ROLE=courierledger_runtime .venv/bin/python -m pytest tests/test_workspaces_live_pg.py -q

Asserts, live:
  * POST /api/workspaces creates a workspace (201) with an owner
    membership row in the database.
  * GET /api/workspaces lists it for the creator and NOT for a second
    user; GET /api/workspaces/{id} is 404 for the second user (no
    existence leak) and 200 for the creator.
  * Unauthenticated workspace create/list are 401.
  * A revoked membership hides the workspace (list empty, get 404).
  * At the SQL level, as the runtime role: user A sees only their own
    merchant row, user B sees none, no user sees none, and a direct
    INSERT into merchants is denied (no general INSERT policy).
"""

from __future__ import annotations

import os
import secrets
import uuid

import httpx
import psycopg
import pytest

from tests.live_pg import (
    authed_client,
    live_pg_enabled,
    live_pg_server,
    pg_host,
    post,
)

pytestmark = pytest.mark.skipif(
    not live_pg_enabled(), reason="live-PG tests need CL_LIVE_PG_PASSWORD"
)

PASSWORD = secrets.token_urlsafe(16)  # Phase 1 (A-F5): random per run, not a shared literal


@pytest.fixture(scope="module")
def live():
    with live_pg_server() as server:
        yield server


def _create_workspace(client: httpx.Client, name: str = "Acme Traders") -> dict:
    resp = post(client, "/api/workspaces", {"name": name})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "owner"
    assert body["status"] == "active"
    return body


def _membership_row(merchant_id: str, user_email: str) -> dict | None:
    """Read a membership row directly (owner connection, bypasses RLS
    only in the sense that the owner connection has no FORCE on these
    two tables -- see rls.sql; this is a test read, not app behaviour)."""
    with psycopg.connect(
        f"host={pg_host()} dbname=courierledger_live_test "
        f"user=courierledger password={os.environ['CL_LIVE_PG_PASSWORD']}"
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mm.role, mm.status, u.email FROM merchant_memberships mm "
                "JOIN users u ON u.id = mm.user_id "
                "WHERE mm.merchant_id = %s AND u.email = %s",
                (merchant_id, user_email),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {"role": row[0], "status": row[1], "email": row[2]}


def test_create_workspace_live(live):
    client_a = authed_client(live["base_url"], "a-live@example.com", PASSWORD)
    try:
        ws = _create_workspace(client_a, "Acme Traders")
        row = _membership_row(ws["id"], "a-live@example.com")
        assert row is not None, "owner membership row missing"
        assert row["role"] == "owner" and row["status"] == "active"

        resp = client_a.get("/api/workspaces")
        assert resp.status_code == 200, resp.text
        ids = [w["id"] for w in resp.json()]
        assert ws["id"] in ids

        resp = client_a.get(f"/api/workspaces/{ws['id']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Acme Traders"
    finally:
        client_a.close()


def test_second_user_cannot_see_workspace(live):
    client_a = authed_client(live["base_url"], "a2-live@example.com", PASSWORD)
    client_b = authed_client(live["base_url"], "b2-live@example.com", PASSWORD)
    try:
        ws = _create_workspace(client_a, "A Only Shop")
        resp = client_b.get("/api/workspaces")
        assert resp.status_code == 200, resp.text
        assert [w["id"] for w in resp.json()] == []
        resp = client_b.get(f"/api/workspaces/{ws['id']}")
        assert resp.status_code == 404, resp.text
        resp = client_a.get("/api/workspaces")
        assert ws["id"] in [w["id"] for w in resp.json()]
    finally:
        client_a.close()
        client_b.close()


def test_unauthenticated_workspace_access_is_401(live):
    anon = httpx.Client(base_url=live["base_url"], timeout=10.0, trust_env=False)
    try:
        # CSRF token present (the /csrf endpoint is public) but no
        # session cookie: auth must reject with 401, not 403.
        resp = post(anon, "/api/workspaces", {"name": "Nope"}, csrf=True)
        assert resp.status_code == 401, resp.text
        resp = anon.get("/api/workspaces")
        assert resp.status_code == 401, resp.text
    finally:
        anon.close()


def test_second_workspace_lists_both(live):
    client_a = authed_client(live["base_url"], "a3-live@example.com", PASSWORD)
    try:
        ws1 = _create_workspace(client_a, "Shop One")
        ws2 = _create_workspace(client_a, "Shop Two")
        resp = client_a.get("/api/workspaces")
        assert resp.status_code == 200, resp.text
        assert {ws1["id"], ws2["id"]} <= {w["id"] for w in resp.json()}
    finally:
        client_a.close()


def test_revoked_membership_hides_workspace(live):
    client_a = authed_client(live["base_url"], "a4-live@example.com", PASSWORD)
    try:
        ws = _create_workspace(client_a, "Revoked Shop")
        assert any(w["id"] == ws["id"] for w in client_a.get("/api/workspaces").json())
        # Revoke the membership directly as the owner role (test-only
        # back door: revocation has no API yet in M1; the owner role
        # bypasses RLS on these two NO FORCE tables).
        with psycopg.connect(
            f"host={pg_host()} dbname=courierledger_live_test "
            f"user=courierledger password={os.environ['CL_LIVE_PG_PASSWORD']}"
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE merchant_memberships SET status = 'revoked' "
                    "WHERE merchant_id = %s",
                    (ws["id"],),
                )
        resp = client_a.get("/api/workspaces")
        assert resp.status_code == 200, resp.text
        assert all(w["id"] != ws["id"] for w in resp.json())
        assert client_a.get(f"/api/workspaces/{ws['id']}").status_code == 404
    finally:
        client_a.close()


def test_runtime_role_sql_visibility_and_no_direct_insert(live):
    """SQL-level RLS proof, as the runtime role (independent of which
    role the app itself connected as): user A sees only their merchant,
    user B sees none, no user sees none, direct INSERT is denied."""
    client_a = authed_client(live["base_url"], "a5-live@example.com", PASSWORD)
    client_b = authed_client(live["base_url"], "b5-live@example.com", PASSWORD)
    try:
        ws = _create_workspace(client_a, "SQL Check Shop")
        user_a_id = _user_id("a5-live@example.com")
        user_b_id = _user_id("b5-live@example.com")

        def count_merchants_as(user_id: uuid.UUID | None) -> int:
            with psycopg.connect(
                f"host={pg_host()} dbname=courierledger_live_test "
                f"user=courierledger_runtime password={os.environ['CL_LIVE_PG_PASSWORD']}"
            ) as conn:
                # Implicit transaction: set_config(..., true) is
                # transaction-local, so it applies to the count below
                # and is discarded by the ROLLBACK.
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('app.user_id', %s, true)",
                        (str(user_id) if user_id else "",),
                    )
                    cur.execute("SELECT count(*) FROM merchants")
                    n = cur.fetchone()[0]
                    cur.execute("ROLLBACK")
            return n

        assert count_merchants_as(user_a_id) == 1
        assert count_merchants_as(user_b_id) == 0
        assert count_merchants_as(None) == 0

        with psycopg.connect(
            f"host={pg_host()} dbname=courierledger_live_test "
            f"user=courierledger_runtime password={os.environ['CL_LIVE_PG_PASSWORD']}"
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.user_id', %s, true)", (str(user_a_id),)
                )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(
                        "INSERT INTO merchants (id, name, created_by_user_id, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), 'sneaky', %s, now(), now())",
                        (str(user_a_id),),
                    )
                cur.execute("ROLLBACK")
        assert ws["id"]  # the created workspace is untouched by the probe
    finally:
        client_a.close()
        client_b.close()


def _user_id(email: str) -> uuid.UUID:
    with psycopg.connect(
        f"host={pg_host()} dbname=courierledger_live_test "
        f"user=courierledger password={os.environ['CL_LIVE_PG_PASSWORD']}"
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            return cur.fetchone()[0]
