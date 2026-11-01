"""Phase 2 infrastructure tests — live PostgreSQL, runtime role.

Run ONLY with the runtime role:
  CL_LIVE_PG_PASSWORD=<password from gitignored backend/.env>
  CL_LIVE_PG_ROLE=courierledger_runtime
  .venv/bin/python -m pytest tests/test_infrastructure_phase2_live_pg.py -q

Proves, live (against the scratch DB the fixture provisions with
`alembic upgrade head` + rls.sql + triggers.sql + roles.sql + seed):
- Migration 0007 applied: alembic head is 0007 and the three
  Phase 2 FK indexes exist (user_sessions.user_id,
  password_reset_tokens.user_id, import_batches.template_id).
- Per-role timeouts are set (Phase 2): the runtime role carries
  statement_timeout 30s, idle_in_transaction_session_timeout 60s,
  lock_timeout 5s in pg_roles.rolconfig.
- The readiness endpoint is live: GET /api/ready returns 200 with
  the database up (the scratch DB the server runs against).

Do NOT run these against SQLite: the checks are PostgreSQL catalog
queries and a live-server HTTP probe.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from tests.live_pg import (
    DB_NAME,
    live_pg_enabled,
    live_pg_server,
    pg_host,
)

pytestmark = pytest.mark.skipif(
    not (live_pg_enabled() and os.environ.get("CL_LIVE_PG_ROLE") == "courierledger_runtime"),
    reason="Phase 2 infrastructure live tests need CL_LIVE_PG_PASSWORD + CL_LIVE_PG_ROLE=courierledger_runtime",
)


@pytest.fixture(scope="module")
def live():
    with live_pg_server() as server:
        yield server


def _owner_conn():
    # Keyword format, not the SQLAlchemy URL: psycopg.connect()
    # rejects "postgresql+psycopg://..." strings, and passing a DSN
    # around also risks the password landing in an error message.
    return psycopg.connect(
        f"host={pg_host()} dbname={DB_NAME} "
        f"user=courierledger password={os.environ['CL_LIVE_PG_PASSWORD']}"
    )


def test_migration_head_is_0007(live):
    with _owner_conn() as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == "0007"


def test_phase2_fk_indexes_exist(live):
    expected = {
        "ix_user_sessions_user_id",
        "ix_password_reset_tokens_user_id",
        "ix_import_batches_template_id",
    }
    with _owner_conn() as conn:
        found = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = ANY(%s)",
                (sorted(expected),),
            )
        }
    assert found == expected


def test_runtime_role_timeouts_set(live):
    with _owner_conn() as conn:
        (config,) = conn.execute(
            "SELECT rolconfig FROM pg_roles WHERE rolname = 'courierledger_runtime'"
        ).fetchone()
    assert config is not None
    settings = dict(item.split("=", 1) for item in config)
    assert settings["statement_timeout"] == "30s"
    assert settings["idle_in_transaction_session_timeout"] == "60s"
    assert settings["lock_timeout"] == "5s"


def test_ready_endpoint_reports_database_up(live):
    resp = live["client"].get("/api/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["database_ok"] is True
    assert "X-Request-ID" in resp.headers
