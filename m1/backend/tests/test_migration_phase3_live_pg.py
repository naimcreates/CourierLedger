"""Phase 3 (Workstreams 2 + 3) — migration-role model and drift
allow-list, live PostgreSQL only.

Run ONLY with a live password:
  CL_LIVE_PG_PASSWORD=<password from gitignored backend/.env>
  .venv/bin/python -m pytest tests/test_migration_phase3_live_pg.py -q

Proves, live (against the scratch DB the fixture provisions with
`alembic upgrade head` + rls.sql + triggers.sql + roles.sql + seed,
exactly as m1/scripts/provision_db.sh does):
- Workstream 2 (migrator role, Option 2 decided 2026-11-02):
  `courierledger` is the live migrator (LOGIN owner, not superuser,
  no BYPASSRLS, owns all 33 tables); `courierledger_migrator` is
  reserved (NOLOGIN, 0 grants, 0 objects, NO password verifier in
  pg_authid); `courierledger_maintenance` is likewise NOLOGIN with
  no verifier; the runtime role owns nothing, cannot BYPASSRLS,
  holds no DELETE privilege and cannot run DDL (negative probes).
- Workstream 3 (drift): migration 0007 applied (head 0007, the
  genuinely missing `uq_memberships_merchant_id` now exists); the
  residual `alembic check` output is EXACTLY the documented
  allow-list — 6 remove_fk + 6 add_fk (NO ACTION vs RESTRICT
  representation on the six auth-table foreign keys) + 1
  remove_index (the RLS-layer ix_merchant_memberships_user_active).
  If that signature ever changes, this test fails loudly and the
  inventory in MIGRATION_VERIFICATION.md §3 must be re-derived —
  do not "fix" the test by widening the allow-list.

Do NOT run these against SQLite: the checks are PostgreSQL catalog
queries, superuser pg_authid reads, live privilege probes, and a
real `alembic check` subprocess.
"""

from __future__ import annotations

import os
import re
import subprocess

import psycopg
import pytest

from tests.live_pg import (
    DB_NAME,
    live_pg_enabled,
    live_pg_server,
    owner_dsn,
    pg_host,
)

pytestmark = pytest.mark.skipif(
    not live_pg_enabled(),
    reason="Phase 3 migration live tests need CL_LIVE_PG_PASSWORD",
)

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The documented residual drift signature (MIGRATION_VERIFICATION.md
# §3): 6 remove_fk + 6 add_fk + 1 remove_index. Any other operation
# kind, or any other count, is a NEW drift event, not the allow-list.
ALLOW_LIST = {"remove_fk": 6, "add_fk": 6, "remove_index": 1}
ALLOW_LIST_INDEX = "ix_merchant_memberships_user_active"


@pytest.fixture(scope="module")
def live():
    with live_pg_server() as server:
        yield server


def _conn(user: str):
    return psycopg.connect(
        f"host={pg_host()} dbname={DB_NAME} "
        f"user={user} password={os.environ['CL_LIVE_PG_PASSWORD']}"
    )


def _superuser_psql(sql: str) -> str:
    """Run one read-only query as the postgres superuser (needed for
    pg_authid, which non-superusers cannot read). Returns stdout.
    Skips the test if `su postgres` is unavailable in this
    environment (e.g. PostgreSQL not installed)."""
    proc = subprocess.run(
        ["su", "postgres", "-c", f'psql -d {DB_NAME} -tAc "{sql}"'],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        pytest.skip(f"superuser psql unavailable: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def test_migration_head_is_0007(live):
    with _conn("courierledger") as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == "0007"


def test_phase3_unique_constraint_exists(live):
    with _conn("courierledger") as conn:
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'uq_memberships_merchant_id'"
        ).fetchone()
    assert row is not None, "0007 constraint missing"
    assert row[0] == "UNIQUE (merchant_id, id)"


def test_live_migrator_is_courierledger_owner(live):
    """Option 2: the live migrator is `courierledger` — LOGIN owner,
    not superuser, cannot bypass RLS, owns all 33 tables."""
    with _conn("courierledger") as conn:
        row = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname = 'courierledger'"
        ).fetchone()
        assert row == (True, False, False), row
        owned = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
            "AND tableowner = 'courierledger'"
        ).fetchone()[0]
        assert owned == 33, owned


def test_reserved_migrator_role_has_no_grants_or_objects(live):
    with _conn("courierledger") as conn:
        row = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname = 'courierledger_migrator'"
        ).fetchone()
        assert row == (False, False, False), row
        owned = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
            "AND tableowner = 'courierledger_migrator'"
        ).fetchone()[0]
        assert owned == 0
        privs = conn.execute(
            "SELECT count(*) FROM information_schema.table_privileges "
            "WHERE grantee = 'courierledger_migrator'"
        ).fetchone()[0]
        assert privs == 0


def test_nologin_roles_have_no_password_verifier(live):
    """pg_authid (superuser-only) is the correct source: pg_roles
    masks rolpassword as a constant '********' for every role and
    cannot distinguish "has a verifier" from "has none"."""
    out = _superuser_psql(
        "SELECT rolname, (rolpassword IS NULL) FROM pg_authid "
        "WHERE rolname IN ('courierledger_migrator', "
        "'courierledger_maintenance', 'courierledger', "
        "'courierledger_runtime') ORDER BY rolname"
    )
    states = dict(
        (parts[0], parts[1] == "t") for parts in (line.split("|") for line in out.splitlines())
    )
    assert states["courierledger_migrator"] is True
    assert states["courierledger_maintenance"] is True
    assert states["courierledger"] is False  # LOGIN owner: has a password
    assert states["courierledger_runtime"] is False  # LOGIN app role: has one


def test_runtime_role_owns_nothing_and_cannot_bypass_rls(live):
    with _conn("courierledger") as conn:
        row = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname = 'courierledger_runtime'"
        ).fetchone()
        assert row == (True, False, False), row
        owned = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
            "AND tableowner = 'courierledger_runtime'"
        ).fetchone()[0]
        assert owned == 0
        deletes = conn.execute(
            "SELECT count(*) FROM information_schema.table_privileges "
            "WHERE grantee = 'courierledger_runtime' AND privilege_type = 'DELETE'"
        ).fetchone()[0]
        assert deletes == 0


def test_runtime_cannot_run_ddl_or_privileged_dml(live):
    """Negative probes as the runtime role: every DDL / privileged
    statement must fail with 42501 (insufficient_privilege)."""
    probes = [
        "CREATE TABLE probe_phase3 (id int)",
        "ALTER TABLE merchants ADD COLUMN probe_col int",
        "DROP TABLE merchants",
        "DELETE FROM idempotency_keys",
        "ALTER TABLE merchants DISABLE ROW LEVEL SECURITY",
    ]
    with _conn("courierledger_runtime") as conn:
        conn.autocommit = True
        for sql in probes:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
        # Fail-closed read without tenant context: 0 rows.
        n = conn.execute("SELECT count(*) FROM merchants").fetchone()[0]
        assert n == 0


def test_runtime_grant_revoke_changes_nothing(live):
    """PostgreSQL answers GRANT/REVOKE by a non-owner without grant
    option with a WARNING and no change (not an error) — so the
    assertion is the unchanged ACL, not an exception."""
    with _conn("courierledger_runtime") as conn:
        conn.autocommit = True
        before = conn.execute(
            "SELECT count(*) FROM information_schema.table_privileges "
            "WHERE grantee = 'courierledger_runtime'"
        ).fetchone()[0]
        conn.execute("GRANT DELETE ON merchants TO courierledger_runtime")
        conn.execute("REVOKE SELECT ON merchants FROM courierledger_runtime")
        after = conn.execute(
            "SELECT count(*) FROM information_schema.table_privileges "
            "WHERE grantee = 'courierledger_runtime'"
        ).fetchone()[0]
    assert before == after == 74


def test_maintenance_privileges_stay_restricted(live):
    with _conn("courierledger") as conn:
        deletes = conn.execute(
            "SELECT table_name FROM information_schema.table_privileges "
            "WHERE grantee = 'courierledger_maintenance' "
            "AND privilege_type = 'DELETE'"
        ).fetchall()
        assert [row[0] for row in deletes] == ["idempotency_keys"]


def test_force_rls_still_enabled(live):
    with _conn("courierledger") as conn:
        enabled = conn.execute(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND relrowsecurity"
        ).fetchone()[0]
        forced = conn.execute(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND relforcerowsecurity"
        ).fetchone()[0]
    assert enabled == 26
    assert forced == 24


def test_fk_ondelete_representation_unchanged(live):
    """Pins the factual basis of the allow-list: the six auth-table
    FKs are NO ACTION ('a') in the live database while the models
    declare RESTRICT; the 79 named FKs are RESTRICT ('r'). If this
    changes, the drift inventory must be re-derived."""
    with _conn("courierledger") as conn:
        no_action = conn.execute(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND confdeltype = 'a' AND conrelid::regclass::text IN "
            "('merchant_memberships', 'merchants', "
            "'password_reset_tokens', 'user_sessions')"
        ).fetchone()[0]
        restrict = conn.execute(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND confdeltype = 'r'"
        ).fetchone()[0]
    assert no_action == 6
    assert restrict == 79


def test_drift_is_exactly_the_documented_allow_list(live):
    """`alembic check` on the provisioned scratch DB — AFTER the
    fixture server has served real traffic (health + ready + a
    register/login through the live client) — must report exactly
    the allow-list and nothing else. This is both the drift
    regression test and the "no schema change from app startup"
    proof: any new operation kind, count, or a clean pass (0 ops)
    fails this test until the inventory is re-derived."""
    resp = live["client"].get("/api/ready")
    assert resp.status_code == 200
    from tests.live_pg import authed_client

    authed_client(live["base_url"], "phase3-drift@example.com").close()

    env = dict(os.environ)
    env["MIGRATION_DATABASE_URL"] = owner_dsn()
    proc = subprocess.run(
        [".venv/bin/alembic", "check"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # 255 = "new upgrade operations detected". The op list is
    # printed in stderr (the ERROR log line); count there only, or
    # every op is counted twice (stdout repeats the same list).
    assert proc.returncode == 255, proc.stdout + proc.stderr
    kinds = re.findall(
        r"\('(add_constraint|remove_constraint|add_fk|remove_fk|remove_index|add_index)'",
        proc.stderr,
    )
    counts = {kind: kinds.count(kind) for kind in sorted(set(kinds))}
    assert counts == ALLOW_LIST, counts
    assert ALLOW_LIST_INDEX in proc.stderr
    assert "uq_memberships_merchant_id" not in proc.stderr
    assert "uq_templates_id" not in proc.stderr
