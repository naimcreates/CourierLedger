"""M1 core database -- model tests (test_models.py).

What these tests prove (all offline, no PostgreSQL server needed):
- the model registry holds exactly the 32 tables named in the brief
  (31 in the spec + password_reset_tokens; see README.md "Count
  discrepancy" -- the brief's list names 32, the spec counts 31);
- every money column is BIGINT poisha, none is FLOAT/NUMERIC, and no
  money column carries a default (one documented exception:
  workspace_settings.tolerance_poisha, DEFAULT 0 per v0.1 C.7);
- composite tenant foreign keys and UNIQUE (merchant_id, id) pairs exist
  where the spec requires them (spot-checks + a uniform sweep);
- the CL-01..CL-26 amendments are visible in the schema (named checks);
- all 32 tables can be created on SQLite (SMOKE ONLY -- this exercises
  DDL shape, not PostgreSQL semantics; PostgreSQL verification is
  UNVERIFIED until a server exists, see README.md).

Synthetic data only. No secrets. No production deployment.
"""
from sqlalchemy import BigInteger, Float, Numeric, create_engine
from sqlalchemy.dialects import postgresql

from app.db.models import ALL_TABLES, APPEND_ONLY_TABLES, Base, TENANT_TABLES

TABLES = Base.metadata.tables


def _check_sql(table_name: str) -> list[str]:
    table = TABLES[table_name]
    return [
        str(c.sqltext.compile(dialect=postgresql.dialect()))
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    ]


# ---------------------------------------------------------------------------
# Registry / count
# ---------------------------------------------------------------------------


def test_table_count_is_32_and_matches_registry():
    # The brief says "31 total (29 + 2 variance tables)" but its own list
    # names 32 tables (it includes password_reset_tokens, which is not in
    # v0.1 section C). This module implements all 32 named tables; the
    # discrepancy is recorded in README.md, not hidden by dropping one.
    assert len(TABLES) == 32, sorted(TABLES)
    assert set(TABLES) == set(ALL_TABLES)
    assert len(ALL_TABLES) == 32
    assert len(TENANT_TABLES) == 25
    assert len(APPEND_ONLY_TABLES) == 15


def test_every_table_has_uuid_pk():
    from sqlalchemy import Uuid

    for name, table in TABLES.items():
        pks = [c for c in table.columns if c.primary_key]
        assert len(pks) == 1, (name, [c.name for c in pks])
        assert isinstance(pks[0].type, Uuid), (name, pks[0].type)
        assert pks[0].name == "id", (name, pks[0].name)


# ---------------------------------------------------------------------------
# Money rules: BIGINT poisha, no float, no defaults
# ---------------------------------------------------------------------------


def test_all_money_columns_are_bigint_poisha():
    for name, table in TABLES.items():
        for col in table.columns:
            assert not isinstance(col.type, (Float, Numeric)), (name, col.name)
            if col.name.endswith("_poisha"):
                assert isinstance(col.type, BigInteger), (name, col.name, col.type)


def test_no_money_column_has_a_default_except_documented_tolerance():
    offenders = []
    for name, table in TABLES.items():
        for col in table.columns:
            if not col.name.endswith("_poisha"):
                continue
            if col.default is not None or col.server_default is not None:
                offenders.append((name, col.name))
    # Exactly one documented exception: v0.1 C.7 mandates DEFAULT 0 for
    # workspace_settings.tolerance_poisha (a typed configuration control,
    # not a calculated amount). Every other *_poisha column must be here.
    assert offenders == [("workspace_settings", "tolerance_poisha")], offenders


def test_all_timestamps_are_timestamptz():
    from sqlalchemy import DateTime

    for name, table in TABLES.items():
        for col in table.columns:
            if isinstance(col.type, DateTime):
                assert col.type.timezone is True, (name, col.name)


# ---------------------------------------------------------------------------
# Tenant keys: UNIQUE (merchant_id, id) + composite foreign keys
# ---------------------------------------------------------------------------


def _unique_columns(table) -> list[tuple[str, ...]]:
    return [
        tuple(c.name for c in con.columns)
        for con in table.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    ]


def test_unique_merchant_id_id_on_every_tenant_owned_parent():
    # Every table carrying a NOT NULL merchant_id must expose
    # UNIQUE (merchant_id, id) so children can use composite tenant FKs
    # (v0.1 A.7/R2). audit_logs is excluded: its merchant_id is nullable
    # (NULL only for pre-workspace identity events) and it is never a
    # parent. merchants has no merchant_id column of its own (it IS the
    # root). Identity/global tables carry no merchant_id.
    for name, table in TABLES.items():
        if name == "audit_logs":
            continue
        merchant_col = table.columns.get("merchant_id")
        if merchant_col is None or merchant_col.nullable:
            continue
        assert ("merchant_id", "id") in _unique_columns(table), name


def _fk_columns(table_name: str) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    table = TABLES[table_name]
    out = []
    for con in table.constraints:
        if con.__class__.__name__ == "ForeignKeyConstraint":
            out.append((
                tuple(e.parent.name for e in con.elements),
                tuple(f"{e.column.table.name}.{e.column.name}" for e in con.elements),
            ))
    return out


def test_composite_tenant_fk_spot_checks():
    assert (("merchant_id", "order_id"), ("orders.merchant_id", "orders.id")) in _fk_columns("shipments")
    assert (("merchant_id", "import_row_id"), ("import_rows.merchant_id", "import_rows.id")) in _fk_columns("statement_lines")
    assert (("merchant_id", "merchant_courier_account_id"),
            ("merchant_courier_accounts.merchant_id", "merchant_courier_accounts.id")) in _fk_columns("courier_rule_versions")
    assert (("merchant_id", "rule_version_id"),
            ("courier_rule_versions.merchant_id", "courier_rule_versions.id")) in _fk_columns("courier_rule_evidence")
    assert (("merchant_id", "run_id"),
            ("reconciliation_runs.merchant_id", "reconciliation_runs.id")) in _fk_columns("reconciliation_results")
    assert (("merchant_id", "result_id"),
            ("reconciliation_results.merchant_id", "reconciliation_results.id")) in _fk_columns("reconciliation_fee_lines")
    assert (("merchant_id", "settlement_batch_id"),
            ("settlement_batches.merchant_id", "settlement_batches.id")) in _fk_columns("bank_receipts")
    # CL-13: supersession stays inside one candidate and one tenant.
    assert (("merchant_id", "candidate_id", "supersedes_decision_id"),
            ("shipment_match_decisions.merchant_id",
             "shipment_match_decisions.candidate_id",
             "shipment_match_decisions.id")) in _fk_columns("shipment_match_decisions")
    # CL-21: variance inputs are batch-scoped composite FKs.
    assert (("merchant_id", "settlement_batch_id", "calculation_id"),
            ("settlement_variance_calculations.merchant_id",
             "settlement_variance_calculations.settlement_batch_id",
             "settlement_variance_calculations.id")) in _fk_columns("settlement_variance_calculation_inputs")
    assert (("merchant_id", "settlement_batch_id", "bank_receipt_id"),
            ("bank_receipts.merchant_id", "bank_receipts.settlement_batch_id",
             "bank_receipts.id")) in _fk_columns("settlement_variance_calculation_inputs")
    # CL-04: typed idempotency result references, composite + tenant-checked.
    assert (("merchant_id", "result_import_batch_id"),
            ("import_batches.merchant_id", "import_batches.id")) in _fk_columns("idempotency_keys")
    assert (("merchant_id", "result_reconciliation_run_id"),
            ("reconciliation_runs.merchant_id", "reconciliation_runs.id")) in _fk_columns("idempotency_keys")


def test_all_foreign_keys_are_on_delete_restrict():
    for name, table in TABLES.items():
        for con in table.constraints:
            if con.__class__.__name__ == "ForeignKeyConstraint":
                assert con.ondelete == "RESTRICT", (name, con.name)


# ---------------------------------------------------------------------------
# Amendment traceability in the schema
# ---------------------------------------------------------------------------


def test_cl01_five_value_overall_status():
    checks = " ".join(_check_sql("reconciliation_results"))
    for value in ("matched", "open", "mismatch", "needs_review", "matched_with_tolerance"):
        assert f"'{value}'" in checks, value
    assert "v_total_poisha" in " ".join(
        str(c.sqltext) for c in TABLES["reconciliation_results"].constraints
        if c.__class__.__name__ == "CheckConstraint"
    )


def test_cl02_negative_payout_model():
    cols = TABLES["statement_lines"].columns
    assert "negative_payout_permitted" in cols
    assert "negative_net_payout_permitted" in TABLES["courier_rule_versions"].columns
    checks = " ".join(_check_sql("statement_lines"))
    assert "negative_payout_permitted" in checks


def test_cl03_candidates_have_no_decision_columns():
    cols = TABLES["shipment_match_candidates"].columns
    assert "decision" not in cols
    assert "decided_by_user_id" not in cols
    assert "prior_candidate_id" in cols  # CL-13
    assert "shipment_match_decisions" in TABLES


def test_cl04_idempotency_is_the_sole_owner():
    assert "idempotency_key" not in TABLES["import_batches"].columns
    assert "idempotency_key" not in TABLES["reconciliation_runs"].columns
    cols = TABLES["idempotency_keys"].columns
    assert "result_import_batch_id" in cols
    assert "result_reconciliation_run_id" in cols
    checks = " ".join(_check_sql("idempotency_keys"))
    assert "'import_create'" in checks and "'run_create'" in checks


def test_cl08_cl09_fee_lines_per_basis_states_and_component_seq():
    cols = TABLES["reconciliation_fee_lines"].columns
    assert "data_quality_state" not in cols  # CL-08: replaced
    for state_col in ("expected_basis_state", "reported_basis_state", "reported_actual_state"):
        assert state_col in cols
    assert "component_seq" in cols  # CL-09
    assert "source_statement_field" in cols
    assert ("merchant_id", "result_id", "fee_code", "component_seq") in _unique_columns(
        TABLES["reconciliation_fee_lines"]
    )


def test_cl10_allocations_have_kind_not_is_active():
    cols = TABLES["settlement_allocations"].columns
    assert "is_active" not in cols
    assert "kind" in cols
    assert "reverses_allocation_id" in cols


def test_cl13_decision_chain_columns():
    cols = TABLES["shipment_match_decisions"].columns
    assert "supersedes_decision_id" in cols
    assert "candidate_id" in cols


def test_cl14_idempotency_lease_columns():
    cols = TABLES["idempotency_keys"].columns
    for col in ("lease_expires_at", "claim_token", "attempt_count", "claimed_by_instance"):
        assert col in cols


def test_cl15_tombstone_merchant_column():
    assert "anonymised_at" in TABLES["merchants"].columns


def test_cl16_variance_tables_and_no_stored_bank_variance():
    assert "bank_variance_poisha" not in TABLES["settlement_batches"].columns
    assert "settlement_variance_calculations" in TABLES
    assert "settlement_variance_calculation_inputs" in TABLES
    calc = TABLES["settlement_variance_calculations"].columns
    for col in ("calculation_seq", "expected_batch_payout_poisha",
               "allocated_bank_confirmed_total_poisha", "bank_variance_poisha"):
        assert col in calc


def test_cl21_variance_inputs_carry_settlement_batch_id():
    assert "settlement_batch_id" in TABLES["settlement_variance_calculation_inputs"].columns


def test_cl05_run_hash_deduplication():
    assert ("merchant_id", "run_hash") in _unique_columns(TABLES["reconciliation_runs"])


def test_one_active_cod_bearing_shipment_per_order_partial_index():
    indexes = TABLES["shipments"].indexes
    partial = [i for i in indexes if i.name == "uq_shipments_one_active_cod_bearing_per_order"]
    assert len(partial) == 1 and partial[0].unique is True


def test_enum_check_presence_spot_checks():
    assert any("active" in c and "invited" in c for c in _check_sql("users"))
    assert any("delivered" in c for c in _check_sql("shipments"))
    assert any("strong_tracking" in c for c in _check_sql("shipment_match_candidates"))
    assert any("allocation" in c and "reversal" in c for c in _check_sql("settlement_allocations"))


def test_sqlite_create_all_smoke_only():
    """DDL-shape smoke test on SQLite. NOT PostgreSQL verification:
    PostgreSQL-only constructs (server_default now(), JSONB, partial
    indexes) are exercised for shape only here; live-PG verification
    is UNVERIFIED until a server exists (see README.md)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Base.metadata.drop_all(engine)
