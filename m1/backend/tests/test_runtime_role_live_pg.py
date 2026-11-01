"""Live-PostgreSQL runtime-role tests (Blocker B-4 fix).

The app must connect as courierledger_runtime (never the owner role)
in the local dev configuration. Run ONLY with the runtime role:
    CL_LIVE_PG_PASSWORD='<dev-password>' CL_LIVE_PG_ROLE=courierledger_runtime \
      .venv/bin/python -m pytest tests/test_runtime_role_live_pg.py -q

Asserts, live:
  * The app's database session is courierledger_runtime, NOBYPASSRLS.
  * The runtime role cannot UPDATE append-only tables, DELETE
    idempotency_keys, or UPDATE merchants (grants deny it).
  * A pooled connection never carries a previous request's tenant/user
    context into the next checkout (transaction-local settings +
    pool reset on return).
  * Health / register / login / /me / workspace create+list /
    reconcile (V2.2 Ex A) all work through the app under the runtime
    role.
  * Alembic honours MIGRATION_DATABASE_URL (owner role) even when
    DATABASE_URL names the runtime role.
"""

from __future__ import annotations

import os
import secrets
import subprocess

import httpx
import psycopg
import pytest
from sqlalchemy import create_engine, text

from tests.live_pg import (
    BACKEND_DIR,
    authed_client,
    live_pg_enabled,
    live_pg_server,
    owner_dsn,
    pg_host,
    post,
)

pytestmark = pytest.mark.skipif(
    not (live_pg_enabled() and os.environ.get("CL_LIVE_PG_ROLE") == "courierledger_runtime"),
    reason="runtime-role tests need CL_LIVE_PG_PASSWORD + CL_LIVE_PG_ROLE=courierledger_runtime",
)

PASSWORD = secrets.token_urlsafe(16)  # Phase 1 (A-F5): random per run, not a shared literal


@pytest.fixture(scope="module")
def live():
    with live_pg_server() as server:
        yield server


def _runtime_conn():
    return psycopg.connect(
        f"host={pg_host()} dbname=courierledger_live_test "
        f"user=courierledger_runtime password={os.environ['CL_LIVE_PG_PASSWORD']}"
    )


def test_app_session_is_runtime_role_nobypassrls(live):
    with _runtime_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_user, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
            user, bypass = cur.fetchone()
    assert user == "courierledger_runtime"
    assert bypass is False


def test_runtime_role_cannot_write_append_only_or_merchants(live):
    with _runtime_conn() as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE audit_logs SET action = 'x' WHERE false")
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM idempotency_keys WHERE false")
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE merchants SET name = 'x' WHERE false")
            conn.rollback()
            # Sanctioned narrow write still works: reconciliation_runs
            # status/completed_at is the one column-level UPDATE grant.
            # (No row exists; the privilege check passes and 0 rows
            # are affected -- that is the point: no privilege error.)
            cur.execute(
                "UPDATE reconciliation_runs SET status = 'completed' WHERE false"
            )
            assert cur.rowcount == 0
            conn.rollback()


def test_pooled_connection_context_does_not_leak(live):
    engine = create_engine(live["app_dsn"], pool_size=1, max_overflow=0)
    try:
        with engine.connect() as conn:
            conn.execute(
                text("SELECT set_config('app.current_merchant_id', "
                     "'11111111-1111-1111-1111-111111111111', true)")
            )
            conn.execute(
                text("SELECT set_config('app.user_id', "
                     "'22222222-2222-2222-2222-222222222222', true)")
            )
            assert conn.execute(
                text("SELECT current_setting('app.current_merchant_id', true)")
            ).scalar_one() == "11111111-1111-1111-1111-111111111111"
            conn.commit()  # ends the transaction; connection returns to pool
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT current_setting('app.current_merchant_id', true), "
                     "current_setting('app.user_id', true)")
            ).fetchone()
        assert row == ("", ""), row
    finally:
        engine.dispose()


def test_auth_workspace_reconcile_smoke_under_runtime(live):
    client = authed_client(live["base_url"], "runtime-smoke@example.com", PASSWORD)
    try:
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200, resp.text
        resp = post(client, "/api/workspaces", {"name": "Runtime Shop"})
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["id"]
        resp = client.get("/api/workspaces")
        assert resp.status_code == 200, resp.text
        assert ws_id in [w["id"] for w in resp.json()]

        # V2.2 Example A, Fixture Gamma (reported_collected),
        # under-collection: E=100000, R=95000, deductions 7950
        # (7000 delivery + 950 fee), payout 87050 -> C -5000,
        # V_total -4950, V_cond 0, contractual matched, overall open.
        resp = post(
            client,
            "/api/reconcile",
            {"shipments": [{
                "merchant_order_id": "ORD-RT-01",
                "tracking_id": "TRK-RT-01",
                "courier_fixture": "Fixture Gamma",
                "outcome": "delivered",
                "amount_to_collect_poisha": 100000,
                "reported_collected_poisha": 95000,
                "expected_delivery_fee_poisha": 7000,
                "reported_delivery_fee_poisha": 7000,
                "cod_fee_base": "reported_collected",
                "cod_fee_rate_bp": 100,
                "other_fees_poisha": 0,
                "excluded_tax_poisha": 0,
                "reported_deductions_total_poisha": 7950,
                "reported_payout_poisha": 87050,
            }], "tolerance_poisha": 0},
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()["results"][0]
        assert result["C"] == -5000, result
        assert result["V_total"] == -4950, result
        assert result["V_cond"] == 0, result
        assert result["contractual_payout_status"] == "matched", result
        assert result["overall_status"] == "open", result
    finally:
        client.close()


def test_alembic_uses_migration_database_url(live):
    """Alembic must read MIGRATION_DATABASE_URL (owner) even when
    DATABASE_URL names the runtime role (which cannot read
    alembic_version). `alembic current` only reads the version table,
    so success proves the owner URL was used."""
    env = dict(os.environ)
    env["MIGRATION_DATABASE_URL"] = owner_dsn()
    env["DATABASE_URL"] = live["app_dsn"]  # runtime role -- must NOT be used
    proc = subprocess.run(
        [".venv/bin/alembic", "current"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    # Phase 2: the head revision is 0006 (missing FK indexes), not
    # 0005 -- this assertion names the current head, and must move
    # with the next migration. The behaviour under test (Alembic
    # reads MIGRATION_DATABASE_URL, not DATABASE_URL) is unchanged.
    assert "0006 (head)" in proc.stdout, proc.stdout
