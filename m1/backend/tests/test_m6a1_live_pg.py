"""Live-PostgreSQL tests for M6-A1 (relaxed result identity CHECKs).

M6-A1 dropped the NULL-propagation arm of
``ck_results_v_cond_identity`` / ``ck_results_v_total_identity``: the
identity is now enforced only when all terms are non-NULL, and no
constraint is imposed on the dependent when any term is NULL. The
V2.2.1 engine legitimately emits non-NULL dependents from partial
identities, and runs containing them must store.

Proves, against real PostgreSQL (scratch DB built by
tests/live_pg.py:setup_scratch_db, which runs ``alembic upgrade head``
-- so this file only passes with migration 0002 applied):

  (i)   example (a) stores: delivered shipment, blank collected,
        payout 90000, expected payout 92000 -> V_total -2000 with
        C / delta / V_R / S all NULL, overall needs_review.
  (ii)  example (b) stores: blank COD-fee line, collected 95000,
        payout 87050 -> V_cond 0 with S / V_R NULL, overall
        needs_review.
  (iii) a genuinely violated identity with ALL terms non-NULL is
        still rejected (V_cond != S - V_R, and V_total !=
        C - delta - V_R + S).

The engine values are asserted from a real ``reconcile_shipment``
call first, so the inserted rows are grounded in the engine, not in
hand-written numbers. The stored rows are then read back and compared
field by field. The deferred CL-01 trigger also runs at commit (the
insert helper commits), so these rows pass the overall-status check
too -- both are needs_review with required NULLs present.

Skipped unless CL_LIVE_PG_PASSWORD is set. All data is synthetic.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from app.domain.engine import reconcile_shipment
from tests.live_pg import (
    DB_NAME,
    drop_scratch_db,
    live_pg_enabled,
    pg_host,
    setup_scratch_db,
)

pytestmark = pytest.mark.skipif(
    not live_pg_enabled(), reason="live-PG tests need CL_LIVE_PG_PASSWORD"
)

PASSWORD = os.environ.get("CL_LIVE_PG_PASSWORD", "")

# Result value tuples, in RESULT_COLS order:
#   E, R, C, X_E, X_R, XU_R, delta, V_R, P_e, P_cond, P_r, S, V_cond, V_total
RESULT_COLS = (
    "id, merchant_id, run_id, shipment_id, expected_collectible_poisha, "
    "reported_collected_poisha, c_poisha, x_e_poisha, x_r_poisha, x_upper_r_poisha, "
    "delta_poisha, v_r_poisha, p_e_poisha, p_cond_poisha, p_r_poisha, s_poisha, "
    "v_cond_poisha, v_total_poisha, contractual_payout_status, data_quality_status, overall_status"
)

# The two M6-A1 engine inputs (verbatim from the amendment request).
SHIPMENT_A = {
    "merchant_order_id": "ORD-M6A1-A", "tracking_id": "TRK-M6A1-A",
    "courier_fixture": "Fixture Alpha", "outcome": "delivered",
    "amount_to_collect_poisha": 100000, "reported_collected_poisha": None,
    "expected_delivery_fee_poisha": 7000, "reported_delivery_fee_poisha": 7000,
    "cod_fee_base": "full_cod", "cod_fee_rate_bp": 100,
    "other_fees_poisha": 0, "excluded_tax_poisha": 0,
    "reported_deductions_total_poisha": 8000, "reported_payout_poisha": 90000,
}
SHIPMENT_B = {
    "merchant_order_id": "ORD-M6A1-B", "tracking_id": "TRK-M6A1-B",
    "courier_fixture": "Fixture Alpha", "outcome": "delivered",
    "amount_to_collect_poisha": 100000, "reported_collected_poisha": 95000,
    "expected_delivery_fee_poisha": 7000, "reported_delivery_fee_poisha": 7000,
    "cod_fee_base": "full_cod", "cod_fee_rate_bp": 100,
    "other_fees_poisha": 0, "excluded_tax_poisha": 0,
    "reported_deductions_total_poisha": None, "reported_payout_poisha": 87050,
    "fee_lines": [
        {"fee_type": "delivery", "raw": 7000, "column_present": True},
        {"fee_type": "cod_fee", "raw": "", "column_present": True},
        {"fee_type": "other", "raw": 0, "column_present": True},
    ],
}

# Engine "unknown" contractual status is stored as "not_calculable"
# (same mapping as app/services/run_service.py:_persist_results).
_CONTRACTUAL_MAP = {"matched": "matched", "mismatch": "mismatch", "unknown": "not_calculable"}


def _engine_values(shipment: dict) -> tuple[tuple, str, str, str]:
    """Run the real engine and return (values tuple, contractual, dq, overall)."""
    r = reconcile_shipment(shipment)
    vals = (
        r["E"], r["R"], r["C"], r["X_e"], r["X_r"], r["X_R"],
        r["delta"], r["V_R"], r["P_e"], r["P_cond"], r["P_r"],
        r["S"], r["V_cond"], r["V_total"],
    )
    return vals, _CONTRACTUAL_MAP[r["contractual_payout_status"]], r["data_quality_status"], r["overall_status"]


def _owner_conn():
    return psycopg.connect(
        f"host={pg_host()} dbname={DB_NAME} user=courierledger password={PASSWORD}"
    )


def _set_ctx(cur, merchant_id) -> None:
    cur.execute("SELECT set_config('app.current_merchant_id', %s, true)", (str(merchant_id),))


def _attempt(merchant_id, build):
    """Run build(cur) in one transaction, then commit.

    Returns (outcome, message): 'committed', 'insert-raised' (a
    statement-time check fired) or 'commit-raised' (a deferred trigger
    fired at commit)."""
    conn = _owner_conn()
    try:
        with conn.cursor() as cur:
            _set_ctx(cur, merchant_id)
            try:
                build(cur)
            except Exception as exc:  # noqa: BLE001 - probe reports the error
                conn.rollback()
                return "insert-raised", str(exc)
        try:
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - probe reports the error
            conn.rollback()
            return "commit-raised", str(exc)
        return "committed", None
    finally:
        conn.close()


def _query(merchant_id, sql, params=()):
    conn = _owner_conn()
    try:
        with conn.cursor() as cur:
            _set_ctx(cur, merchant_id)
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _insert_result(cur, merchant_id, run_id, shipment_id, vals, contractual, dq, overall):
    rid = uuid.uuid4()
    cur.execute(
        f"INSERT INTO reconciliation_results ({RESULT_COLS}) VALUES (%s, %s, %s, %s, "
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (str(rid), str(merchant_id), str(run_id), str(shipment_id),
         *vals, contractual, dq, overall),
    )
    return rid


def _new_shipment(merchant_id, tag: str) -> uuid.UUID:
    """A fresh order + delivered COD-bearing shipment, committed."""
    ship_id, order_id = uuid.uuid4(), uuid.uuid4()
    conn = _owner_conn()
    try:
        with conn.cursor() as cur:
            _set_ctx(cur, merchant_id)
            cur.execute(
                "INSERT INTO orders (id, merchant_id, merchant_order_ref, order_cod_poisha, "
                "currency, source) VALUES (%s, %s, %s, 100000, 'BDT', 'manual')",
                (str(order_id), str(merchant_id), f"M6A1-{tag}"),
            )
            cur.execute(
                "INSERT INTO shipments (id, merchant_id, order_id, outcome, "
                "expected_collectible_cod_poisha, is_cod_bearing, is_superseded) "
                "VALUES (%s, %s, %s, 'delivered', 100000, true, false)",
                (str(ship_id), str(merchant_id), str(order_id)),
            )
        conn.commit()
    finally:
        conn.close()
    return ship_id


@pytest.fixture(scope="module")
def env():
    setup_scratch_db()
    try:
        user_id = uuid.uuid4()
        with _owner_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (id, email, display_name, password_hash, status, "
                    "created_at, updated_at) "
                    "VALUES (%s, 'm6a1@example.com', 'M6-A1', 'x', 'active', now(), now())",
                    (str(user_id),),
                )
        with psycopg.connect(
            f"host={pg_host()} dbname={DB_NAME} user=courierledger_runtime password={PASSWORD}"
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT public.create_workspace(%s, 'M6-A1 Shop', NULL)",
                    (str(user_id),),
                )
                merchant_id = cur.fetchone()[0]
        account_id, file_id, batch_id, run_id = (uuid.uuid4() for _ in range(4))
        with _owner_conn() as conn:
            with conn.cursor() as cur:
                _set_ctx(cur, merchant_id)
                cur.execute("SELECT id FROM couriers WHERE code = 'steadfast'")
                courier_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO merchant_courier_accounts (id, merchant_id, courier_id, status) "
                    "VALUES (%s, %s, %s, 'active')",
                    (str(account_id), str(merchant_id), str(courier_id)),
                )
                cur.execute(
                    "INSERT INTO file_objects (id, merchant_id, storage_key, original_filename, "
                    "sha256, size_bytes, content_type, uploaded_by_user_id) "
                    "VALUES (%s, %s, 'local://m6a1/file.csv', 'file.csv', %s, 10, "
                    "'text/csv', %s)",
                    (str(file_id), str(merchant_id), "d" * 64, str(user_id)),
                )
                cur.execute(
                    "INSERT INTO import_batches (id, merchant_id, file_object_id, "
                    "merchant_courier_account_id, created_by_user_id, status, "
                    "row_count, parsed_row_count, error_row_count) "
                    "VALUES (%s, %s, %s, %s, %s, 'uploaded', 0, 0, 0)",
                    (str(batch_id), str(merchant_id), str(file_id), str(account_id), str(user_id)),
                )
                cur.execute(
                    "INSERT INTO reconciliation_runs (id, merchant_id, import_batch_id, "
                    "merchant_courier_account_id, rule_snapshot, input_snapshot, "
                    "mapping_snapshot, tolerance_poisha, engine_version, status, run_hash) "
                    "VALUES (%s, %s, %s, %s, '{}', '{}', '{}', 0, 'm6a1-tests', "
                    "'running', %s)",
                    (str(run_id), str(merchant_id), str(batch_id), str(account_id), "m6a1-run"),
                )
            conn.commit()
        yield {"merchant_id": merchant_id, "run_id": run_id}
    finally:
        drop_scratch_db()


# --- Engine grounding -------------------------------------------------------
# The values below are asserted from the real engine call, not hand-written:
# (a) V_total -2000 with C/delta/V_R/S all NULL; (b) V_cond 0 with S/V_R NULL.

def test_engine_example_a_emits_partial_v_total():
    vals, contractual, dq, overall = _engine_values(SHIPMENT_A)
    assert vals == (
        100000, None, None, 8000, 8000, None,
        None, None, 92000, None, 90000, None, None, -2000,
    ), vals
    assert (contractual, dq, overall) == ("not_calculable", "needs_review", "needs_review")


def test_engine_example_b_emits_partial_v_cond():
    vals, contractual, dq, overall = _engine_values(SHIPMENT_B)
    assert vals == (
        100000, 95000, -5000, 8000, None, 7950,
        -50, None, 92000, 87050, 87050, None, 0, -4950,
    ), vals
    assert (contractual, dq, overall) == ("matched", "needs_review", "needs_review")


# --- (i) example (a) stores --------------------------------------------------

def test_m6a1_a_partial_v_total_stores(env):
    merchant_id, run_id = env["merchant_id"], env["run_id"]
    vals, contractual, dq, overall = _engine_values(SHIPMENT_A)
    ship_id = _new_shipment(merchant_id, "a")
    outcome, msg = _attempt(
        merchant_id,
        lambda cur: _insert_result(cur, merchant_id, run_id, ship_id, vals, contractual, dq, overall),
    )
    assert outcome == "committed", msg
    rows = _query(
        merchant_id,
        "SELECT c_poisha, delta_poisha, v_r_poisha, s_poisha, v_cond_poisha, "
        "v_total_poisha, overall_status FROM reconciliation_results "
        "WHERE merchant_id = %s AND run_id = %s AND shipment_id = %s",
        (str(merchant_id), str(run_id), str(ship_id)),
    )
    assert rows == [(None, None, None, None, None, -2000, "needs_review")], rows


# --- (ii) example (b) stores -------------------------------------------------

def test_m6a1_b_partial_v_cond_stores(env):
    merchant_id, run_id = env["merchant_id"], env["run_id"]
    vals, contractual, dq, overall = _engine_values(SHIPMENT_B)
    ship_id = _new_shipment(merchant_id, "b")
    outcome, msg = _attempt(
        merchant_id,
        lambda cur: _insert_result(cur, merchant_id, run_id, ship_id, vals, contractual, dq, overall),
    )
    assert outcome == "committed", msg
    rows = _query(
        merchant_id,
        "SELECT s_poisha, v_r_poisha, v_cond_poisha, v_total_poisha, overall_status "
        "FROM reconciliation_results "
        "WHERE merchant_id = %s AND run_id = %s AND shipment_id = %s",
        (str(merchant_id), str(run_id), str(ship_id)),
    )
    assert rows == [(None, None, 0, -4950, "needs_review")], rows


# --- (iii) genuinely violated identities still rejected ----------------------

def test_m6a1_violated_v_cond_still_rejected(env):
    """All terms non-NULL, V_cond 999 != S - V_R = 0 - 0: CHECK must fire."""
    merchant_id, run_id = env["merchant_id"], env["run_id"]
    ship_id = _new_shipment(merchant_id, "bad-cond")
    clean = (100000, 100000, 0, 5000, 5000, 5000, 0, 0, 95000, 95000, 95000, 0, 0, 0)
    bad = clean[:12] + (999, 0)  # S=0, V_R=0, V_cond=999, V_total=0
    outcome, msg = _attempt(
        merchant_id,
        lambda cur: _insert_result(cur, merchant_id, run_id, ship_id, bad, "matched", "ok", "matched"),
    )
    assert outcome == "insert-raised", (outcome, msg)
    assert "ck_results_v_cond_identity" in msg, msg


def test_m6a1_violated_v_total_still_rejected(env):
    """All terms non-NULL, V_total 1 != C - delta - V_R + S = 0: CHECK must fire."""
    merchant_id, run_id = env["merchant_id"], env["run_id"]
    ship_id = _new_shipment(merchant_id, "bad-total")
    clean = (100000, 100000, 0, 5000, 5000, 5000, 0, 0, 95000, 95000, 95000, 0, 0, 0)
    bad = clean[:13] + (1,)  # V_total=1, everything else clean
    outcome, msg = _attempt(
        merchant_id,
        lambda cur: _insert_result(cur, merchant_id, run_id, ship_id, bad, "matched", "ok", "matched"),
    )
    assert outcome == "insert-raised", (outcome, msg)
    assert "ck_results_v_total_identity" in msg, msg
