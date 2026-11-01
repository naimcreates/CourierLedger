"""Reconciliation run store tests (M1 Milestone 6) -- SQLite fallback
suite. Live-PostgreSQL coverage (RLS + runtime role + the deferred
CL-01 trigger) lives in tests/test_runs_live_pg.py.

Covers: run creation from a seeded import batch (generic fixture +
seeded shipments, as in tests/test_csv_import.py); stored statuses
equal engine statuses (recomputed from the stored input snapshot);
all five variances + NULL vs explicit zero; a returned shipment with
verified 0; a D_c-missing Beta run producing needs_review; findings
never netted (two findings on one result both stored, including two
same-code DQ findings); run immutability at API level (no update
routes); idempotency replay, hash conflict and same run_hash replay;
cross-workspace 404s; zero eligible lines -> 422 with reasons and no
run row; import history unchanged after run creation.

All fixtures are SYNTHETIC EXAMPLES of a generic statement layout --
no courier's real CSV template or rate is claimed anywhere here.
"""
import hashlib
import json
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.auth_models import Merchant, MerchantMembership, User
from app.db.models import (
    Courier,
    CourierRuleVersion,
    FileObject,
    IdempotencyKey,
    ImportBatch,
    ImportRow,
    MerchantCourierAccount,
    Order,
    ReconciliationFeeLine,
    ReconciliationFinding,
    ReconciliationResult,
    ReconciliationRun,
    Shipment,
    StatementLine,
    WorkspaceSettings,
)
from app.db.session import SessionLocal
from app.domain.engine import reconcile_batch, reconcile_shipment
from app.services import csv_import, run_service
from app.services.idempotency_service import IdempotencyConflictError

DATA = Path(__file__).parent / "data"
FIXTURE = DATA / "generic_statement_fixture.csv"

MAPPING = {
    "tracking_id": "Tracking ID",
    "merchant_order_ref": "Order Ref",
    "reported_collected": "Collected (poisha)",
    "reported_payout": "Payout (poisha)",
    "delivery_fee": "Delivery Fee (poisha)",
    "cod_fee": "COD Fee (poisha)",
    "return_fee": "Return Fee (poisha)",
    "other_fee": "Other Fee (poisha)",
    "shipment_status": "Status",
    "statement_date": "Statement Date",
}

RETURNED_CSV = (
    "Tracking ID,Order Ref,Collected (poisha),Payout (poisha),Delivery Fee (poisha),"
    "COD Fee (poisha),Return Fee (poisha),Other Fee (poisha),Status,Statement Date\n"
    "TRK-RET,ORD-RET,0,0,0,0,0,0,returned,2026-10-15\n"
).encode()


@pytest.fixture()
def db(monkeypatch, tmp_path):
    # Keep test file writes out of the real backend/var/ tree.
    monkeypatch.setattr(csv_import, "VAR_IMPORTS_DIR", tmp_path / "imports")
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def env(db):
    user = User(email="m6-user@example.com", password_hash="x", status="active")
    db.add(user)
    db.flush()
    merchant = Merchant(name="M6 Shop", created_by_user_id=user.id)
    db.add(merchant)
    db.flush()
    db.add(MerchantMembership(merchant_id=merchant.id, user_id=user.id, role="owner", status="active"))
    courier = Courier(code="steadfast", name="Steadfast (synthetic test row)")
    db.add(courier)
    db.flush()
    account = MerchantCourierAccount(merchant_id=merchant.id, courier_id=courier.id)
    db.add(account)
    db.flush()

    def _order(ref, tracking=None, shipped_on=None, outcome="delivered",
               order_cod=100000, expected_collectible=100000):
        order = Order(merchant_id=merchant.id, merchant_order_ref=ref, order_cod_poisha=order_cod)
        db.add(order)
        db.flush()
        shipment = Shipment(
            merchant_id=merchant.id,
            order_id=order.id,
            merchant_courier_account_id=account.id,
            tracking_id_raw=tracking,
            tracking_id_normalised=tracking,
            outcome=outcome,
            expected_collectible_cod_poisha=expected_collectible,
            shipped_on=shipped_on,
        )
        db.add(shipment)
        db.flush()
        return order, shipment

    shipments = {}
    for ref, tracking in [
        ("ORD-1001", "TRK-1001"),
        ("ORD-1002", "TRK-1002"),
        ("ORD-1003", "TRK-DUP"),
        ("ORD-1007", "TRK-1007X"),
        ("ORD-1008", "TRK-1008"),
        ("ORD-1009", "TRK-1009"),
        ("ORD-1010", "TRK-1010X"),
    ]:
        _, s = _order(ref, tracking, shipped_on=date(2026, 10, 10))
        shipments[ref] = s
    # Ambiguous: two active shipments share one tracking id.
    _order("ORD-1005a", "TRK-AMB", shipped_on=date(2026, 10, 10))
    _order("ORD-1005b", "TRK-AMB", shipped_on=date(2026, 10, 10))
    db.commit()
    return {
        "user": user, "merchant": merchant, "account": account,
        "shipments": shipments, "make_order": _order,
    }


def _import(db, env, data=None, key=None):
    return csv_import.run_import(
        db,
        user=env["user"],
        merchant_id=env["merchant"].id,
        merchant_courier_account_id=env["account"].id,
        filename="statement.csv",
        data=FIXTURE.read_bytes() if data is None else data,
        mapping=dict(MAPPING),
        idempotency_key=key or f"imp-{uuid.uuid4().hex}",
    )


def _run(db, env, batch_id, key=None, **kwargs):
    return run_service.create_run_from_import(
        db,
        user=env["user"],
        merchant_id=env["merchant"].id,
        import_batch_id=batch_id,
        idempotency_key=key or f"run-{uuid.uuid4().hex}",
        **kwargs,
    )


def _rule_version(db, env, version_no=1, delivery_fee=7000, return_fee=0,
                  other_fees=0, excluded_tax=0, base="expected_collectible",
                  rate=100, vat_mode="unknown", vat_rate=None):
    version = CourierRuleVersion(
        merchant_id=env["merchant"].id,
        merchant_courier_account_id=env["account"].id,
        version_no=version_no,
        status="draft",
        cod_fee_base=base,
        cod_fee_rate_bps=rate,
        delivery_fee_poisha=delivery_fee,
        return_fee_poisha=return_fee,
        vat_mode=vat_mode,
        vat_rate_bps=vat_rate,
        rule_definition={"other_fees_poisha": other_fees, "excluded_tax_poisha": excluded_tax},
        content_hash=hashlib.sha256(f"m6-rule-{version_no}".encode()).hexdigest(),
        created_by_user_id=env["user"].id,
    )
    db.add(version)
    db.flush()
    db.commit()
    return version


def _results_by_tracking(db, run_id):
    """Stored results keyed by statement tracking id."""
    run_id = uuid.UUID(str(run_id))
    out = {}
    for result in db.execute(
        select(ReconciliationResult).where(ReconciliationResult.run_id == run_id)
    ).scalars().all():
        line = db.get(StatementLine, result.statement_line_id)
        out[line.tracking_id_normalised] = result
    return out


def _findings(db, result_id):
    return db.execute(
        select(ReconciliationFinding)
        .where(ReconciliationFinding.result_id == result_id)
        .order_by(ReconciliationFinding.finding_code)
    ).scalars().all()


def _fee_lines(db, result_id):
    return {
        fl.fee_code: fl
        for fl in db.execute(
            select(ReconciliationFeeLine)
            .where(ReconciliationFeeLine.result_id == result_id)
        ).scalars().all()
    }


# ---------------------------------------------------------------------------
# Fixture Alpha run from the generic fixture
# ---------------------------------------------------------------------------

def test_fixture_alpha_run_from_generic_fixture(db, env):
    batch = _import(db, env, key="m6-alpha-imp")
    summary = _run(db, env, batch["batch_id"], key="m6-alpha-run", courier_fixture="Fixture Alpha")
    assert summary["status"] == "completed"
    assert summary["tolerance_poisha"] == 0
    assert summary["idempotency_outcome"] == "claimed"
    # Only the four strong-matched rows are eligible.
    assert summary["result_counts"] == {
        "matched": 0, "open": 0, "mismatch": 0, "needs_review": 4, "matched_with_tolerance": 0,
    }
    assert summary["excluded_counts"] == {
        "duplicate tracking in batch": 2,
        "ambiguous match — needs human confirmation": 2,
        "no shipment matched": 2,
        "row failed to parse / skipped": 2,
    }
    run = db.get(ReconciliationRun, uuid.UUID(summary["run_id"]))
    assert run is not None
    assert run.rule_version_id is None
    assert run.rule_content_hash is None
    assert run.rule_snapshot["kind"] == "synthetic_fixture"
    assert run.rule_snapshot["courier_fixture"] == "Fixture Alpha"
    assert run.rule_snapshot["label"] == "SYNTHETIC FIXTURE — not a real courier contract"
    assert run.engine_version == "V2.2.1"
    assert run.mapping_snapshot == MAPPING
    assert run.completed_at is not None
    # Fixture Alpha states no expected delivery fee, so no exact payout
    # is calculable: every result is needs_review.
    results = _results_by_tracking(db, run.id)
    assert set(results) == {"TRK-1001", "TRK-1002", "TRK-1008", "TRK-1009"}
    assert all(r.overall_status == "needs_review" for r in results.values())
    assert all(r.data_quality_status == "needs_review" for r in results.values())
    assert all(r.contractual_payout_status == "not_calculable" for r in results.values())


def test_stored_results_equal_engine_recomputed_from_snapshot(db, env):
    batch = _import(db, env, key="m6-eq-imp")
    summary = _run(db, env, batch["batch_id"], key="m6-eq-run", courier_fixture="Fixture Alpha")
    run = db.get(ReconciliationRun, uuid.UUID(summary["run_id"]))
    inputs = run.input_snapshot["shipments"]
    assert len(inputs) == 4
    assert run.input_snapshot["engine"] == "app.domain.engine"
    assert len(run.input_snapshot["excluded"]) == 8
    recomputed = {r["tracking_id"]: r for r in reconcile_batch(inputs, 0)["results"]}
    stored = _results_by_tracking(db, run.id)
    assert set(recomputed) == set(stored)
    for tracking, engine_r in recomputed.items():
        s = stored[tracking]
        assert s.expected_collectible_poisha == engine_r["E"]
        assert s.reported_collected_poisha == engine_r["R"]
        assert s.c_poisha == engine_r["C"]
        assert s.x_e_poisha == engine_r["X_e"]
        assert s.x_r_poisha == engine_r["X_r"]
        assert s.x_upper_r_poisha == engine_r["X_R"]
        assert s.delta_poisha == engine_r["delta"]
        assert s.v_r_poisha == engine_r["V_R"]
        assert s.p_e_poisha == engine_r["P_e"]
        assert s.p_cond_poisha == engine_r["P_cond"]
        assert s.p_r_poisha == engine_r["P_r"]
        assert s.s_poisha == engine_r["S"]
        assert s.v_cond_poisha == engine_r["V_cond"]
        assert s.v_total_poisha == engine_r["V_total"]
        assert s.overall_status == engine_r["overall_status"]
        assert s.data_quality_status == (
            "needs_review" if engine_r["data_quality_status"] == "needs_review" else "ok"
        )
        assert s.contractual_payout_status == {
            "matched": "matched", "mismatch": "mismatch", "unknown": "not_calculable",
        }[engine_r["contractual_payout_status"]]


def test_rule_version_run_statuses_and_variances(db, env):
    batch = _import(db, env, key="m6-rule-imp")
    version = _rule_version(db, env)
    summary = _run(db, env, batch["batch_id"], key="m6-rule-run", rule_version_id=version.id)
    assert summary["result_counts"] == {
        "matched": 1, "open": 1, "mismatch": 1, "needs_review": 1, "matched_with_tolerance": 0,
    }
    results = _results_by_tracking(db, summary["run_id"])
    # TRK-1001: exact match on every variance.
    r1 = results["TRK-1001"]
    assert r1.overall_status == "matched"
    assert (r1.c_poisha, r1.delta_poisha, r1.v_r_poisha, r1.s_poisha,
            r1.v_total_poisha, r1.v_cond_poisha) == (0, 0, 0, 0, 0, 0)
    assert r1.p_e_poisha == r1.p_cond_poisha == r1.p_r_poisha == 92000
    assert r1.contractual_payout_status == "matched"
    assert _findings(db, r1.id) == []
    # TRK-1002: collection-only shortfall -> open, contractual matched.
    # Hand-checked: C -5000, delta -50, V_R 0, S 0, V_total -4950, V_cond 0.
    r2 = results["TRK-1002"]
    assert r2.overall_status == "open"
    assert r2.contractual_payout_status == "matched"
    assert (r2.c_poisha, r2.delta_poisha, r2.v_r_poisha, r2.s_poisha,
            r2.v_total_poisha, r2.v_cond_poisha) == (-5000, -50, 0, 0, -4950, 0)
    assert r2.p_e_poisha == 92000 and r2.p_cond_poisha == 87050 and r2.p_r_poisha == 87050
    # TRK-1009: all explicit zeros reported against a 100000 COD order.
    # R is verified 0, not NULL; fee mismatch -> mismatch.
    r9 = results["TRK-1009"]
    assert r9.overall_status == "mismatch"
    assert r9.reported_collected_poisha == 0  # explicit zero, never NULL
    assert r9.expected_collectible_poisha == 100000
    assert (r9.c_poisha, r9.delta_poisha, r9.v_r_poisha, r9.s_poisha,
            r9.v_total_poisha, r9.v_cond_poisha) == (-100000, -1000, -7000, 0, -92000, 7000)
    assert r9.contractual_payout_status == "mismatch"
    fees9 = _fee_lines(db, r9.id)
    assert fees9["delivery_fee"].reported_actual_poisha == 0
    assert fees9["delivery_fee"].reported_actual_state == "explicit_zero"
    assert fees9["cod_fee"].reported_actual_poisha == 0
    # TRK-1008: blank collected + invalid payout -> NULLs, needs_review.
    r8 = results["TRK-1008"]
    assert r8.overall_status == "needs_review"
    assert r8.reported_collected_poisha is None  # blank, never zero
    assert r8.p_r_poisha is None  # invalid "1,5x0", never zero
    assert r8.c_poisha is None and r8.v_cond_poisha is None
    assert r8.contractual_payout_status == "not_calculable"
    fees8 = _fee_lines(db, r8.id)
    assert fees8["delivery_fee"].expected_basis_poisha == 7000
    assert fees8["delivery_fee"].expected_basis_state == "present_valid"
    assert fees8["cod_fee"].reported_actual_state == "blank"
    assert fees8["cod_fee"].reported_actual_poisha is None
    assert fees8["cod_fee"].expected_basis_poisha == 1000
    assert fees8["other_fee"].reported_actual_state == "blank"


def test_findings_never_netted(db, env):
    batch = _import(db, env, key="m6-net-imp")
    version = _rule_version(db, env)
    summary = _run(db, env, batch["batch_id"], key="m6-net-run", rule_version_id=version.id)
    results = _results_by_tracking(db, summary["run_id"])
    # TRK-1002 keeps both its COLLECTION and its SCENARIO finding.
    codes2 = {f.finding_code: f.amount_poisha for f in _findings(db, results["TRK-1002"].id)}
    assert codes2 == {
        "COLLECTION:reported_vs_expected": -5000,
        "SCENARIO:expectation": -4950,
    }
    # TRK-1009 keeps all four findings.
    codes9 = {f.finding_code: f.amount_poisha for f in _findings(db, results["TRK-1009"].id)}
    assert codes9 == {
        "COLLECTION:reported_vs_expected": -100000,
        "FEE_TAX:reported_basis": -7000,
        "PAYOUT_COND:contractual": 7000,
        "SCENARIO:expectation": -92000,
    }
    # TRK-1008 keeps BOTH blank-value findings (one per fee line),
    # disambiguated by the occurrence suffix -- neither is dropped.
    codes8 = [f.finding_code for f in _findings(db, results["TRK-1008"].id)]
    assert codes8 == ["DQ_BLANK_VALUE", "DQ_BLANK_VALUE:2"]
    for f in _findings(db, results["TRK-1008"].id):
        assert f.category == "data_quality" and f.finding_type == "data_quality"
        assert f.amount_poisha is None and f.is_within_tolerance is False


def test_returned_shipment_verified_zero(db, env):
    env["make_order"]("ORD-RET", "TRK-RET", shipped_on=date(2026, 10, 10),
                      outcome="returned", order_cod=50000, expected_collectible=0)
    db.commit()
    batch = _import(db, env, data=RETURNED_CSV, key="m6-ret-imp")
    version = _rule_version(db, env, version_no=1, delivery_fee=0, return_fee=0)
    summary = _run(db, env, batch["batch_id"], key="m6-ret-run", rule_version_id=version.id)
    assert summary["result_counts"]["matched"] == 1
    results = _results_by_tracking(db, summary["run_id"])
    r = results["TRK-RET"]
    assert r.overall_status == "matched"
    assert r.expected_collectible_poisha == 0  # verified 0 for a confirmed return, never NULL
    assert r.reported_collected_poisha == 0
    assert r.c_poisha == 0 and r.p_e_poisha == 0 and r.p_cond_poisha == 0
    assert r.contractual_payout_status == "matched"


def test_fixture_beta_dc_missing_needs_review(db, env):
    batch = _import(db, env, key="m6-beta-imp")
    summary = _run(db, env, batch["batch_id"], key="m6-beta-run", courier_fixture="Fixture Beta")
    assert summary["result_counts"]["needs_review"] == 4
    results = _results_by_tracking(db, summary["run_id"])
    r1 = results["TRK-1001"]
    assert r1.overall_status == "needs_review"
    codes = {f.finding_code for f in _findings(db, r1.id)}
    assert "DQ_MISSING_CONTRACT" in codes
    fees = _fee_lines(db, r1.id)
    assert fees["cod_fee"].expected_basis_state == "contract_missing"
    assert fees["cod_fee"].expected_basis_poisha is None
    assert fees["cod_fee"].reported_basis_state == "contract_missing"
    assert fees["cod_fee"].reported_basis_poisha is None
    run = db.get(ReconciliationRun, uuid.UUID(summary["run_id"]))
    assert run.rule_snapshot["cod_fee_base"] == "cod_minus_delivery_fee"


def test_matched_with_tolerance_and_finding_tolerance_flag(db, env):
    batch = _import(db, env, key="m6-tol-imp")
    version = _rule_version(db, env)
    summary = _run(db, env, batch["batch_id"], key="m6-tol-run",
                   rule_version_id=version.id, tolerance_poisha=5000)
    assert summary["tolerance_poisha"] == 5000
    assert summary["result_counts"]["matched_with_tolerance"] == 1
    results = _results_by_tracking(db, summary["run_id"])
    r2 = results["TRK-1002"]
    assert r2.overall_status == "matched_with_tolerance"
    findings = _findings(db, r2.id)
    assert all(f.is_within_tolerance for f in findings)
    # The gross amounts are still stored and shown, not netted away.
    assert {f.finding_code: f.amount_poisha for f in findings} == {
        "COLLECTION:reported_vs_expected": -5000,
        "SCENARIO:expectation": -4950,
    }


def test_tolerance_from_workspace_settings(db, env):
    db.add(WorkspaceSettings(merchant_id=env["merchant"].id, tolerance_poisha=500))
    db.commit()
    batch = _import(db, env, key="m6-ws-imp")
    summary = _run(db, env, batch["batch_id"], key="m6-ws-run", courier_fixture="Fixture Alpha")
    assert summary["tolerance_poisha"] == 500
    summary2 = _run(db, env, batch["batch_id"], key="m6-ws-run2",
                    courier_fixture="Fixture Alpha", tolerance_poisha=0)
    assert summary2["tolerance_poisha"] == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_idempotency_replay_same_key(db, env):
    batch = _import(db, env, key="m6-rep-imp")
    first = _run(db, env, batch["batch_id"], key="m6-rep-key", courier_fixture="Fixture Alpha")
    second = _run(db, env, batch["batch_id"], key="m6-rep-key", courier_fixture="Fixture Alpha")
    assert second["run_id"] == first["run_id"]
    assert second["idempotency_outcome"] == "replay"
    assert len(db.execute(select(ReconciliationRun)).scalars().all()) == 1


def test_idempotency_hash_conflict_same_key_different_batch(db, env):
    batch1 = _import(db, env, key="m6-con-imp1")
    batch2 = _import(db, env, key="m6-con-imp2")
    _run(db, env, batch1["batch_id"], key="m6-con-key", courier_fixture="Fixture Alpha")
    db.rollback()
    with pytest.raises(IdempotencyConflictError):
        _run(db, env, batch2["batch_id"], key="m6-con-key", courier_fixture="Fixture Alpha")


def test_same_run_hash_returns_existing_run(db, env):
    batch = _import(db, env, key="m6-hash-imp")
    first = _run(db, env, batch["batch_id"], key="m6-hash-key1", courier_fixture="Fixture Alpha")
    second = _run(db, env, batch["batch_id"], key="m6-hash-key2", courier_fixture="Fixture Alpha")
    assert second["run_id"] == first["run_id"]
    assert second["idempotency_outcome"] == "replay"
    assert len(db.execute(select(ReconciliationRun)).scalars().all()) == 1


# ---------------------------------------------------------------------------
# 422 paths
# ---------------------------------------------------------------------------

def test_zero_eligible_lines_422_no_shipments(db):
    user = User(email="m6-none@example.com", password_hash="x", status="active")
    db.add(user)
    db.flush()
    merchant = Merchant(name="M6 Empty Shop", created_by_user_id=user.id)
    db.add(merchant)
    db.flush()
    db.add(MerchantMembership(merchant_id=merchant.id, user_id=user.id, role="owner", status="active"))
    courier = Courier(code="steadfast", name="Steadfast (synthetic test row)")
    db.add(courier)
    db.flush()
    account = MerchantCourierAccount(merchant_id=merchant.id, courier_id=courier.id)
    db.add(account)
    db.flush()
    db.commit()
    batch = csv_import.run_import(
        db, user=user, merchant_id=merchant.id,
        merchant_courier_account_id=account.id, filename="statement.csv",
        data=FIXTURE.read_bytes(), mapping=dict(MAPPING), idempotency_key="m6-none-imp",
    )
    with pytest.raises(run_service.RunCreationError) as excinfo:
        run_service.create_run_from_import(
            db, user=user, merchant_id=merchant.id,
            import_batch_id=batch["batch_id"], courier_fixture="Fixture Alpha",
            idempotency_key="m6-none-run",
        )
    assert excinfo.value.reason == "no_eligible_lines"
    assert excinfo.value.counts == {
        "duplicate tracking in batch": 2,
        "no shipment matched": 8,
        "row failed to parse / skipped": 2,
    }
    db.rollback()
    assert db.execute(select(ReconciliationRun)).scalars().all() == []
    # The import's own completed import_create key exists; what must
    # NOT exist is any run_create key (eligibility precedes claiming).
    assert db.execute(
        select(IdempotencyKey).where(IdempotencyKey.operation == "run_create")
    ).scalars().all() == []


def test_parse_failed_batch_is_422(db, env):
    batch = csv_import.run_import(
        db, user=env["user"], merchant_id=env["merchant"].id,
        merchant_courier_account_id=env["account"].id, filename="bad.csv",
        data=b"\xff\xfe not utf-8", mapping=dict(MAPPING), idempotency_key="m6-bad-imp",
    )
    assert batch["status"] == "parse_failed"
    with pytest.raises(run_service.RunCreationError) as excinfo:
        _run(db, env, batch["batch_id"], key="m6-bad-run", courier_fixture="Fixture Alpha")
    assert excinfo.value.reason == "batch_parse_failed"


def test_rule_validation_errors(db, env):
    batch = _import(db, env, key="m6-val-imp")
    version = _rule_version(db, env)
    with pytest.raises(run_service.RunCreationError) as excinfo:
        _run(db, env, batch["batch_id"], key="m6-val-1",
             courier_fixture="Fixture Alpha", rule_version_id=version.id)
    assert excinfo.value.reason == "ambiguous_rule"
    with pytest.raises(run_service.RunCreationError) as excinfo:
        _run(db, env, batch["batch_id"], key="m6-val-2", courier_fixture="Fixture Zeta")
    assert excinfo.value.reason == "unknown_fixture"
    with pytest.raises(run_service.RunCreationError) as excinfo:
        _run(db, env, batch["batch_id"], key="m6-val-3",
             rule_version_id=version.id, tolerance_poisha=-1)
    assert excinfo.value.reason == "bad_tolerance"
    with pytest.raises(run_service.RunCreationError) as excinfo:
        _run(db, env, batch["batch_id"], key="m6-val-4",
             rule_version_id=version.id, tolerance_poisha=True)
    assert excinfo.value.reason == "bad_tolerance"
    with pytest.raises(run_service.RunNotFoundError):
        _run(db, env, uuid.uuid4(), key="m6-val-5", courier_fixture="Fixture Alpha")


def test_rule_version_of_another_merchant_is_422(db, env):
    batch = _import(db, env, key="m6-xm-imp")
    other_user = User(email="m6-other@example.com", password_hash="x", status="active")
    db.add(other_user)
    db.flush()
    other_merchant = Merchant(name="M6 Other Shop", created_by_user_id=other_user.id)
    db.add(other_merchant)
    db.flush()
    other_account = MerchantCourierAccount(
        merchant_id=other_merchant.id, courier_id=env["account"].courier_id)
    db.add(other_account)
    db.flush()
    other_version = CourierRuleVersion(
        merchant_id=other_merchant.id,
        merchant_courier_account_id=other_account.id,
        version_no=1, status="draft", cod_fee_base="expected_collectible",
        cod_fee_rate_bps=100, rule_definition={},
        content_hash=hashlib.sha256(b"m6-other-rule").hexdigest(),
        created_by_user_id=other_user.id,
    )
    db.add(other_version)
    db.flush()
    db.commit()
    with pytest.raises(run_service.RunCreationError) as excinfo:
        _run(db, env, batch["batch_id"], key="m6-xm-run", rule_version_id=other_version.id)
    assert excinfo.value.reason == "rule_version_not_found"


# ---------------------------------------------------------------------------
# Import history unchanged + readers + isolation
# ---------------------------------------------------------------------------

def _import_snapshot(db, batch_id):
    batch_id = uuid.UUID(str(batch_id))
    batch = db.get(ImportBatch, batch_id)
    rows = db.execute(
        select(ImportRow).where(ImportRow.import_batch_id == batch_id)
        .order_by(ImportRow.row_number)
    ).scalars().all()
    lines = db.execute(
        select(StatementLine).where(StatementLine.import_batch_id == batch_id)
    ).scalars().all()
    return (
        batch.status, batch.row_count, batch.parsed_row_count, batch.error_row_count,
        [(r.row_number, r.parse_status, r.raw_values) for r in rows],
        sorted((l.match_status, str(l.shipment_id)) for l in lines),
    )


def test_import_history_unchanged_after_run_creation(db, env):
    batch = _import(db, env, key="m6-hist-imp")
    before = _import_snapshot(db, batch["batch_id"])
    _run(db, env, batch["batch_id"], key="m6-hist-run", courier_fixture="Fixture Alpha")
    assert _import_snapshot(db, batch["batch_id"]) == before


def test_readers_and_batch_listing(db, env):
    batch = _import(db, env, key="m6-read-imp")
    alpha = _run(db, env, batch["batch_id"], key="m6-read-alpha", courier_fixture="Fixture Alpha")
    beta = _run(db, env, batch["batch_id"], key="m6-read-beta", courier_fixture="Fixture Beta")
    items, total = run_service.list_runs(db, env["merchant"].id)
    assert total == 2
    # Newest first by created_at; the two runs are created within one
    # SQLite second, so their created_at ties and the tie-break (id
    # desc, random UUIDs) is arbitrary here -- assert the set, not
    # the order. On PostgreSQL created_at has microsecond resolution
    # and same-second ties do not occur for user-driven creates.
    assert {i["run_id"] for i in items} == {beta["run_id"], alpha["run_id"]}
    assert items[0]["result_counts"]["needs_review"] == 4
    items2, total2 = run_service.list_runs(db, env["merchant"].id, limit=1, offset=1)
    assert total2 == 2 and len(items2) == 1
    assert items2[0]["run_id"] in {beta["run_id"], alpha["run_id"]}

    detail = run_service.get_run_detail(db, env["merchant"].id, alpha["run_id"])
    assert detail is not None
    assert len(detail["results"]) == 4
    assert detail["result_counts"]["needs_review"] == 4
    assert all(detail["results"][i]["fee_lines"] for i in range(4))
    assert detail["input_snapshot"]["shipments"][0]["tracking_id"] == "TRK-1001"

    results, rtotal = run_service.list_run_results(db, env["merchant"].id, alpha["run_id"], limit=2)
    assert rtotal == 4 and len(results) == 2
    results2, _ = run_service.list_run_results(db, env["merchant"].id, alpha["run_id"], limit=2, offset=2)
    assert len(results2) == 2
    assert {r["result_id"] for r in results} != {r["result_id"] for r in results2}

    findings, ftotal = run_service.list_run_findings(db, env["merchant"].id, alpha["run_id"])
    assert ftotal == 4
    assert {f["finding_code"] for f in findings} == {
        "COLLECTION:reported_vs_expected", "DQ_BLANK_VALUE", "DQ_BLANK_VALUE:2",
    }

    batch_runs = run_service.list_runs_for_batch(db, env["merchant"].id, batch["batch_id"])
    assert {r["run_id"] for r in batch_runs} == {beta["run_id"], alpha["run_id"]}
    assert run_service.get_run_detail(db, env["merchant"].id, uuid.uuid4()) is None
    with pytest.raises(run_service.RunNotFoundError):
        run_service.list_run_results(db, env["merchant"].id, uuid.uuid4())
    with pytest.raises(run_service.RunNotFoundError):
        run_service.list_runs_for_batch(db, env["merchant"].id, uuid.uuid4())


def test_cross_workspace_isolation_service(db, env):
    batch = _import(db, env, key="m6-iso-imp")
    summary = _run(db, env, batch["batch_id"], key="m6-iso-run", courier_fixture="Fixture Alpha")
    other = uuid.uuid4()
    assert run_service.get_run_detail(db, other, summary["run_id"]) is None
    items, total = run_service.list_runs(db, other)
    assert (items, total) == ([], 0)
    with pytest.raises(run_service.RunNotFoundError):
        run_service.list_run_results(db, other, summary["run_id"])
    with pytest.raises(run_service.RunNotFoundError):
        run_service.create_run_from_import(
            db, user=env["user"], merchant_id=other,
            import_batch_id=batch["batch_id"], courier_fixture="Fixture Alpha",
            idempotency_key="m6-iso-run2",
        )


# ---------------------------------------------------------------------------
# API (TestClient, SQLite)
# ---------------------------------------------------------------------------

def _api_setup(client, db, email):
    from tests.conftest import authed_client, get_csrf, post
    # The couriers reference list is seeded by SQL in live PG; on the
    # SQLite fallback the row must exist for the account endpoint.
    db.add(Courier(code="steadfast", name="Steadfast (synthetic test row)"))
    db.commit()
    authed_client(client, email)
    resp = post(client, "/api/workspaces", {"name": "M6 API Shop"})
    assert resp.status_code == 201, resp.text
    workspace_id = resp.json()["id"]
    resp = post(client, f"/api/workspaces/{workspace_id}/courier-accounts",
                {"courier_code": "steadfast", "display_label": "Steadfast test account"})
    assert resp.status_code == 201, resp.text
    return workspace_id, resp.json()["id"]


def _api_seed_shipments(db, merchant_id, account_id):
    def _order(ref, tracking):
        order = Order(merchant_id=merchant_id, merchant_order_ref=ref, order_cod_poisha=100000)
        db.add(order)
        db.flush()
        db.add(Shipment(
            merchant_id=merchant_id, order_id=order.id,
            merchant_courier_account_id=account_id,
            tracking_id_raw=tracking, tracking_id_normalised=tracking,
            outcome="delivered", expected_collectible_cod_poisha=100000,
            shipped_on=date(2026, 10, 10),
        ))
        db.flush()
    for ref, trk in [("ORD-1001", "TRK-1001"), ("ORD-1002", "TRK-1002"),
                     ("ORD-1003", "TRK-DUP"), ("ORD-1007", "TRK-1007X"),
                     ("ORD-1008", "TRK-1008"), ("ORD-1009", "TRK-1009"),
                     ("ORD-1010", "TRK-1010X")]:
        _order(ref, trk)
    _order("ORD-1005a", "TRK-AMB")
    _order("ORD-1005b", "TRK-AMB")
    db.commit()


def _api_upload(client, workspace_id, account_id, key):
    from tests.conftest import get_csrf
    headers = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": key}
    return client.post(
        f"/api/workspaces/{workspace_id}/imports",
        data={"mapping": json.dumps(MAPPING), "merchant_courier_account_id": account_id},
        files={"file": ("statement.csv", FIXTURE.read_bytes(), "text/csv")},
        headers=headers,
    )


def test_api_create_run_201_replay_200_and_readers(client, db, monkeypatch, tmp_path):
    from tests.conftest import get_csrf, post
    monkeypatch.setattr(csv_import, "VAR_IMPORTS_DIR", tmp_path / "imports")
    workspace_id, account_id = _api_setup(client, db, "m6api-a@example.com")
    _api_seed_shipments(db, uuid.UUID(workspace_id), uuid.UUID(account_id))
    resp = _api_upload(client, workspace_id, account_id, "m6api-imp-1")
    assert resp.status_code == 201, resp.text
    batch_id = resp.json()["batch_id"]

    headers = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": "m6api-run-1"}
    resp = client.post(f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs",
                       json={"courier_fixture": "Fixture Alpha"}, headers=headers)
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    assert resp.json()["result_counts"]["needs_review"] == 4

    # Same key replays the same run with 200, not a second run.
    resp = client.post(f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs",
                       json={"courier_fixture": "Fixture Alpha"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["run_id"] == run_id
    assert resp.json()["idempotency_outcome"] == "replay"

    resp = client.get(f"/api/workspaces/{workspace_id}/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) == 4
    resp = client.get(f"/api/workspaces/{workspace_id}/runs/{run_id}/results?limit=2&offset=2")
    assert resp.status_code == 200 and resp.json()["total"] == 4 and len(resp.json()["results"]) == 2
    resp = client.get(f"/api/workspaces/{workspace_id}/runs/{run_id}/findings")
    assert resp.status_code == 200 and resp.json()["total"] == 4
    resp = client.get(f"/api/workspaces/{workspace_id}/runs")
    assert resp.status_code == 200 and resp.json()["total"] == 1
    resp = client.get(f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs")
    assert resp.status_code == 200 and resp.json()["total"] == 1
    resp = client.get(f"/api/workspaces/{workspace_id}/runs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_api_run_422_zero_eligible_and_409_conflict(client, db, monkeypatch, tmp_path):
    from tests.conftest import get_csrf
    monkeypatch.setattr(csv_import, "VAR_IMPORTS_DIR", tmp_path / "imports")
    workspace_id, account_id = _api_setup(client, db, "m6api-b@example.com")
    # No shipments seeded: nothing is eligible.
    resp = _api_upload(client, workspace_id, account_id, "m6api-imp-2")
    assert resp.status_code == 201, resp.text
    batch_id = resp.json()["batch_id"]
    headers = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": "m6api-run-2"}
    resp = client.post(f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs",
                       json={"courier_fixture": "Fixture Alpha"}, headers=headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "no_eligible_lines"
    assert resp.json()["detail"]["counts"]["no shipment matched"] == 8

    # 409: same key, different (eligible) batch. Shipments are seeded
    # first so both new batches have eligible lines; the first batch
    # stays ineligible because its match decisions were made at import
    # time and are stored, not re-decided.
    _api_seed_shipments(db, uuid.UUID(workspace_id), uuid.UUID(account_id))
    resp = _api_upload(client, workspace_id, account_id, "m6api-imp-3")
    assert resp.status_code == 201, resp.text
    batch2 = resp.json()["batch_id"]
    resp = _api_upload(client, workspace_id, account_id, "m6api-imp-4")
    assert resp.status_code == 201, resp.text
    batch3 = resp.json()["batch_id"]
    headers2 = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": "m6api-run-3"}
    resp = client.post(f"/api/workspaces/{workspace_id}/imports/{batch2}/runs",
                       json={"courier_fixture": "Fixture Alpha"}, headers=headers2)
    assert resp.status_code == 201, resp.text
    resp = client.post(f"/api/workspaces/{workspace_id}/imports/{batch3}/runs",
                       json={"courier_fixture": "Fixture Alpha"}, headers=headers2)
    assert resp.status_code == 409, resp.text


def test_api_cross_workspace_404_and_no_update_routes(client, db, monkeypatch, tmp_path):
    from tests.conftest import authed_client, get_csrf
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(csv_import, "VAR_IMPORTS_DIR", tmp_path / "imports")
    workspace_id, account_id = _api_setup(client, db, "m6api-c@example.com")
    _api_seed_shipments(db, uuid.UUID(workspace_id), uuid.UUID(account_id))
    resp = _api_upload(client, workspace_id, account_id, "m6api-imp-4")
    batch_id = resp.json()["batch_id"]
    headers = {"X-CSRF-Token": get_csrf(client)}
    resp = client.post(f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs",
                       json={"courier_fixture": "Fixture Alpha"}, headers=headers)
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]

    # Run immutability at API level: no update/delete routes exist
    # (checked as the owning user, before switching clients).
    for method in ("put", "patch", "delete"):
        r = getattr(client, method)(f"/api/workspaces/{workspace_id}/runs/{run_id}",
                                    headers={"X-CSRF-Token": get_csrf(client)})
        assert r.status_code == 405, (method, r.status_code)

    # A second user (fresh client, own session) sees nothing.
    other = TestClient(app)
    authed_client(other, "m6api-d@example.com")
    assert other.get(f"/api/workspaces/{workspace_id}/runs/{run_id}").status_code == 404
    assert other.get(f"/api/workspaces/{workspace_id}/runs").status_code == 404
    assert other.get(f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs").status_code == 404
    assert other.post(f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs",
                      json={"courier_fixture": "Fixture Alpha"},
                      headers={"X-CSRF-Token": get_csrf(other)}).status_code == 404


def test_api_reconcile_stays_compute_only(client, db, monkeypatch, tmp_path):
    from tests.conftest import post
    monkeypatch.setattr(csv_import, "VAR_IMPORTS_DIR", tmp_path / "imports")
    workspace_id, _ = _api_setup(client, db, "m6api-e@example.com")
    resp = post(client, "/api/reconcile", {
        "shipments": [{
            "merchant_order_id": "ORD-X", "tracking_id": "TRK-X",
            "courier_fixture": "Fixture Alpha", "outcome": "delivered",
            "amount_to_collect_poisha": 100000,
            "reported_collected_poisha": 100000,
            "reported_payout_poisha": 92000,
            "expected_delivery_fee_poisha": 7000,
            "other_fees_poisha": 0, "excluded_tax_poisha": 0,
            "fee_lines": [
                {"fee_type": "delivery", "raw": 7000, "column_present": True},
                {"fee_type": "cod", "raw": 1000, "column_present": True},
                {"fee_type": "return", "raw": 0, "column_present": True},
                {"fee_type": "other", "raw": 0, "column_present": True},
            ],
        }],
        "tolerance_poisha": 0,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["overall_status"] == "matched"
    items, total = run_service.list_runs(db, uuid.UUID(workspace_id))
    assert total == 0  # compute-only: no run was persisted


def test_api_run_requires_auth_and_csrf(client, db, monkeypatch, tmp_path):
    from tests.conftest import get_csrf
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(csv_import, "VAR_IMPORTS_DIR", tmp_path / "imports")
    workspace_id, account_id = _api_setup(client, db, "m6api-f@example.com")
    _api_seed_shipments(db, uuid.UUID(workspace_id), uuid.UUID(account_id))
    resp = _api_upload(client, workspace_id, account_id, "m6api-imp-5")
    batch_id = resp.json()["batch_id"]
    # Unauthenticated POST (fresh client, no session cookie) -> 401.
    anon_client = TestClient(app)
    r = anon_client.post(f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs",
                         json={"courier_fixture": "Fixture Alpha"},
                         headers={"X-CSRF-Token": get_csrf(anon_client)})
    assert r.status_code == 401, r.text
    # Authenticated but no CSRF header -> 403.
    r = client.post(f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs",
                    json={"courier_fixture": "Fixture Alpha"})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# M6-A1: relaxed result identity CHECKs (SQLite run-store path)
# ---------------------------------------------------------------------------
# The V2.2.1 engine legitimately emits a non-NULL V_total / V_cond from a
# partial identity (example (a): blank collected -> V_total -2000 with
# C/delta/V_R/S all NULL). The run store must persist such results. The
# SQLite fallback enforces the models' CHECKs, so this exercises the
# relaxed CHECK through the real create_run_from_import path, and the
# direct-ORM test below proves the CHECK still rejects a genuinely
# violated identity when all terms are non-NULL.

M6A1_CSV = (
    "Tracking ID,Order Ref,Collected (poisha),Payout (poisha),Delivery Fee (poisha),"
    "COD Fee (poisha),Return Fee (poisha),Other Fee (poisha),Status,Statement Date\n"
    "TRK-M6A1,ORD-1011,,90000,7000,1000,0,0,delivered,2026-10-15\n"
).encode()


def test_m6a1_blank_collected_run_stores_partial_v_total(db, env):
    env["make_order"]("ORD-1011", "TRK-M6A1", shipped_on=date(2026, 10, 10))
    db.commit()
    batch = _import(db, env, data=M6A1_CSV, key="m6a1-sqlite-imp")
    version = _rule_version(db, env)
    summary = _run(db, env, batch["batch_id"], key="m6a1-sqlite-run",
                   rule_version_id=version.id)
    assert summary["status"] == "completed"
    assert summary["result_counts"] == {
        "matched": 0, "open": 0, "mismatch": 0, "needs_review": 1,
        "matched_with_tolerance": 0,
    }
    results = _results_by_tracking(db, summary["run_id"])
    assert set(results) == {"TRK-M6A1"}
    r = results["TRK-M6A1"]
    # Engine-grounded: the stored row equals the engine's own output for
    # the exact input the run persisted in its snapshot.
    run = db.get(ReconciliationRun, uuid.UUID(summary["run_id"]))
    engine_r = reconcile_shipment(run.input_snapshot["shipments"][0])
    assert engine_r["V_total"] == -2000
    assert engine_r["C"] is None and engine_r["delta"] is None
    assert engine_r["V_R"] is None and engine_r["S"] is None
    assert engine_r["overall_status"] == "needs_review"
    assert r.expected_collectible_poisha == engine_r["E"] == 100000
    assert r.reported_collected_poisha is None  # blank, never zero
    assert r.c_poisha is None and r.delta_poisha is None
    assert r.v_r_poisha is None and r.s_poisha is None
    assert r.v_total_poisha == -2000 and r.v_cond_poisha is None
    assert r.p_e_poisha == 92000 and r.p_r_poisha == 90000
    assert r.overall_status == "needs_review"
    assert r.contractual_payout_status == "not_calculable"


def test_m6a1_sqlite_identity_check_still_rejects_all_defined_violation(db, env):
    """Direct ORM inserts: a partial identity stores, a violated
    all-defined identity raises IntegrityError. Proves the SQLite
    fallback enforces the relaxed CHECKs (not just the live PG tests)."""
    batch = _import(db, env, key="m6a1-check-imp")
    summary = _run(db, env, batch["batch_id"], key="m6a1-check-run",
                   courier_fixture="Fixture Alpha")
    run_id = uuid.UUID(summary["run_id"])
    merchant_id = env["merchant"].id
    _, ship_ok = env["make_order"]("ORD-M6A1-OK", "TRK-M6A1-OK",
                                   shipped_on=date(2026, 10, 10))
    _, ship_bad = env["make_order"]("ORD-M6A1-BAD", "TRK-M6A1-BAD",
                                    shipped_on=date(2026, 10, 10))
    db.commit()

    def _result(shipment_id, vals, contractual="matched", dq="ok", overall="matched"):
        return ReconciliationResult(
            merchant_id=merchant_id, run_id=run_id, shipment_id=shipment_id,
            expected_collectible_poisha=vals[0], reported_collected_poisha=vals[1],
            c_poisha=vals[2], x_e_poisha=vals[3], x_r_poisha=vals[4],
            x_upper_r_poisha=vals[5], delta_poisha=vals[6], v_r_poisha=vals[7],
            p_e_poisha=vals[8], p_cond_poisha=vals[9], p_r_poisha=vals[10],
            s_poisha=vals[11], v_cond_poisha=vals[12], v_total_poisha=vals[13],
            contractual_payout_status=contractual, data_quality_status=dq,
            overall_status=overall,
        )

    # Example (a) shape: V_total -2000 with C/delta/V_R/S NULL -- stores.
    db.add(_result(ship_ok.id,
                   (100000, None, None, 8000, 8000, None, None, None,
                    92000, None, 90000, None, None, -2000),
                   contractual="not_calculable", dq="needs_review",
                   overall="needs_review"))
    db.flush()
    stored = db.execute(
        select(ReconciliationResult).where(
            ReconciliationResult.run_id == run_id,
            ReconciliationResult.shipment_id == ship_ok.id,
        )
    ).scalar_one()
    assert stored.v_total_poisha == -2000 and stored.c_poisha is None

    # All terms non-NULL, V_cond 999 != S - V_R = 0 -- must be rejected.
    db.add(_result(ship_bad.id,
                   (100000, 100000, 0, 5000, 5000, 5000, 0, 0,
                    95000, 95000, 95000, 0, 999, 0)))
    with pytest.raises(IntegrityError, match="ck_results_v_cond_identity"):
        db.flush()
    db.rollback()
