"""M1 core database -- migration / SQL-file tests (test_migration_sql.py).

Runs the real Alembic CLI in OFFLINE mode (`alembic upgrade head
--sql`): SQL generation only, no live server, no connection attempt.
Asserts the generated DDL creates every M1 table, enables + forces RLS,
and carries the key constraints. Also asserts the separate SQL files
(rls.sql / triggers.sql / roles.sql) contain the mechanisms the README
claims they contain. Nothing here executes DDL against PostgreSQL --
live-migration verification is UNVERIFIED until a server exists.
"""
import os
import re
import subprocess
from pathlib import Path

from app.db.models import ALL_TABLES, TENANT_TABLES

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BACKEND_DIR / "app" / "db"


def _offline_sql() -> str:
    env = dict(os.environ)
    # Offline generation needs a URL syntactically, never connects.
    env["DATABASE_URL"] = "postgresql+psycopg://courierledger:change-me@localhost:5432/courierledger"
    proc = subprocess.run(
        [".venv/bin/alembic", "upgrade", "head", "--sql"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_offline_migration_creates_every_table():
    sql = _offline_sql()
    created = re.findall(r"^CREATE TABLE (\w+)", sql, flags=re.MULTILINE)
    # 32 M1 tables + alembic_version.
    assert len(created) == 33, created
    for table in ALL_TABLES:
        assert table in created, table
    assert "alembic_version" in created


def test_offline_migration_enables_and_forces_rls():
    sql = _offline_sql()
    assert sql.count("ENABLE ROW LEVEL SECURITY") == 26  # merchants + 25 tenant tables
    assert sql.count("FORCE ROW LEVEL SECURITY") == 26
    for table in ["merchants", *TENANT_TABLES]:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql, table


def test_offline_migration_carries_key_constraints():
    sql = _offline_sql()
    # Five-value overall status (CL-01 / P0 patch).
    assert "overall_status" in sql
    for value in ("matched", "open", "mismatch", "needs_review", "matched_with_tolerance"):
        assert f"'{value}'" in sql, value
    # One active COD-bearing shipment per order (partial unique index).
    assert "uq_shipments_one_active_cod_bearing_per_order" in sql
    # CL-13: at most one successor per decision (partial unique index).
    assert "uq_match_decisions_one_successor" in sql
    # CL-10: at most one reversal per allocation (partial unique index).
    assert "uq_allocations_one_reversal" in sql
    # CL-16: no stored bank variance on settlement_batches.
    batch_block = re.search(
        r"CREATE TABLE settlement_batches \((.*?)\n\);", sql, flags=re.DOTALL
    )
    assert batch_block is not None
    assert "bank_variance_poisha" not in batch_block.group(1)
    # CL-04: no idempotency columns on batches/runs.
    for table in ("import_batches", "reconciliation_runs"):
        block = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, flags=re.DOTALL)
        assert block is not None
        assert "idempotency_key" not in block.group(1), table
    # Composite tenant FKs render with both columns.
    assert "FOREIGN KEY(merchant_id, order_id) REFERENCES orders (merchant_id, id)" in sql
    assert "FOREIGN KEY(merchant_id, candidate_id, supersedes_decision_id)" in sql


def test_rls_sql_contains_tenant_context_and_policies():
    text = (DB_DIR / "rls.sql").read_text()
    assert "app_current_merchant_id" in text
    assert "app.current_merchant_id" in text
    assert "merchant_id = public.app_current_merchant_id()" in text
    for table in TENANT_TABLES:
        assert f"ON public.{table}" in text, table
    # Append-only tables have SELECT + INSERT policies and no ALL policy.
    assert "statement_lines_tenant_select" in text
    assert "statement_lines_tenant_insert" in text
    assert "statement_lines_tenant_all" not in text


def test_rls_sql_contains_b1_membership_policy_and_create_function():
    """Blocker B-1 fix (v0.1 E.3): membership-based merchants SELECT,
    user context, controlled creation function, no general INSERT
    policy on merchants, NO FORCE on the two identity-root tables."""
    text = (DB_DIR / "rls.sql").read_text()
    assert "app_current_user_id" in text
    assert "app.user_id" in text
    # Membership-based merchants SELECT (EXISTS over memberships).
    assert "merchants_tenant_select" in text
    assert "mm.user_id = public.app_current_user_id()" in text
    assert "mm.status = 'active'" in text
    # No general INSERT policy on merchants (creation is the function).
    assert "CREATE POLICY merchants_tenant_insert" not in text
    # Split memberships policies (no FOR ALL); INSERT stays context-gated.
    assert "merchant_memberships_tenant_select" in text
    assert "merchant_memberships_tenant_insert" in text
    assert "merchant_memberships_tenant_update" in text
    assert "CREATE POLICY merchant_memberships_tenant_all" not in text  # (the old name survives only in a DROP POLICY IF EXISTS line)
    # NO FORCE on the two identity-root tables only.
    assert "ALTER TABLE public.merchants NO FORCE ROW LEVEL SECURITY" in text
    assert "ALTER TABLE public.merchant_memberships NO FORCE ROW LEVEL SECURITY" in text
    assert text.count("NO FORCE ROW LEVEL SECURITY") == 2
    # Controlled creation function: SECURITY DEFINER, hardened search_path,
    # EXECUTE revoked from PUBLIC and granted to the runtime role only.
    assert "CREATE OR REPLACE FUNCTION public.create_workspace(" in text
    assert "SECURITY DEFINER" in text
    assert "REVOKE ALL ON FUNCTION public.create_workspace(uuid, text, text) FROM PUBLIC" in text
    assert "GRANT EXECUTE ON FUNCTION public.create_workspace(uuid, text, text) TO courierledger_runtime" in text
    # Index backing the membership-based policy subquery.
    assert "ix_merchant_memberships_user_active" in text


def test_triggers_sql_contains_cl_mechanisms():
    text = (DB_DIR / "triggers.sql").read_text()
    for fn in (
        "fn_append_only_guard",
        "fn_reconciliation_run_guard",
        "fn_rule_version_guard",
        "fn_result_status_check",          # CL-01
        "fn_current_match_decision",       # CL-13 derivation
        "fn_match_decision_chain_check",   # CL-13 / CL-18 delta-scoped
        "fn_confirmed_candidate_line_check",  # CL-03 line level
        "fn_variance_calculation_check",   # CL-16 / CL-21
        "fn_allocation_overage_check",     # CL-10
    ):
        assert fn in text, fn
    assert "DEFERRABLE INITIALLY DEFERRED" in text
    assert "clock_timestamp()" in text  # lease-clock rule stated
    assert "SECURITY DEFINER" in text
    assert "SET search_path" in text  # CL-26 rule 7


def test_roles_sql_contains_three_roles_and_narrow_grants():
    text = (DB_DIR / "roles.sql").read_text()
    for role in ("courierledger_migrator", "courierledger_runtime", "courierledger_maintenance"):
        assert role in text, role
    assert "NOBYPASSRLS" in text
    assert "UPDATE (status, completed_at)" in text  # runs: narrow grant only
    assert "DELETE ON public.idempotency_keys TO courierledger_maintenance" in text
    assert "clock_timestamp()" in text  # CL-19/CL-24 predicates use it
