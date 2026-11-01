"""Live-PostgreSQL deferred-trigger tests (M6, CL-01 / CL-13 / CL-16).

Runs against a scratch PostgreSQL database (schema + RLS + triggers +
roles applied by tests/live_pg.py:setup_scratch_db; no HTTP server --
these tests drive raw SQL directly, like test_idempotency_lease_live_pg).
Skipped unless CL_LIVE_PG_PASSWORD is set.

Proves, against real PostgreSQL:
  * CL-01: fn_result_status_check enforces the v0.2 precedence
    (needs_review first, then mismatch, then open, then
    matched_with_tolerance / matched) at commit, including the M6
    correction that the "must be mismatch" branches do NOT fire when a
    data-quality finding or a required NULL forces needs_review
    (T-16(d): financial variance + missing data -> needs_review).
  * CL-13: fn_match_decision_chain_check (delta-scoped) + the partial
    unique index keep each candidate's decision history linear, and
    fn_current_match_decision derives the head; CL-03 keeps at most
    one currently-confirmed candidate per statement line.
  * CL-16: fn_variance_calculation_check admits only calculations
    whose snapshots agree with the batch's current receipt/allocation
    sets, and the CURRENT variance (CL-16/CL-21 derivation) becomes
    NULL when the input set changes while the old row stays untouched.
  * Append-only: UPDATE/DELETE on reconciliation_results raises.

All data is synthetic. Money is integer poisha; every inserted result
satisfies the row-local identities V_cond = S - V_R and
V_total = C - delta - V_R + S, and x_upper_r is X_R, the deductions
recalculated on the reported basis (NOT the reported total).

Two structural limits, found live and recorded here rather than
papered over (see test_cl13_e_unanimous_rejection and
test_cl13_f_current_decision_undecided): a forked decision history
(two heads) cannot be created through the guarded insert path in a
triggers-applied database -- the partial unique index rejects a second
successor at statement time and the chain trigger rejects a second
root at commit -- so the multi-head unanimous-rejection and
conflicted branches of fn_current_match_decision only fire for
pre-existing malformed histories loaded before the triggers
(CL-23 load-before-trigger fixtures), which this harness does not do.
"""

from __future__ import annotations

import os
import subprocess
import uuid

import itertools

import psycopg
import pytest

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

# Monotonic row numbers for seeded import rows (unique per batch).
_row_counter = itertools.count(1)

# Result value tuples, in column order:
#   E, R, C, X_E, X_R, XU_R, delta, V_R, P_e, P_cond, P_r, S, V_cond, V_total
CLEAN = (100000, 100000, 0, 5000, 5000, 5000, 0, 0, 95000, 95000, 95000, 0, 0, 0)
# Statement variance only: S = V_cond = V_total = -1000 (P_r 94000 vs 95000 due).
STMT_VAR = (100000, 100000, 0, 5000, 5000, 5000, 0, 0, 95000, 95000, 94000, -1000, -1000, -1000)
# Fee variance: V_R = 1000, S = 1000, V_cond = 0, V_total = -1000.
FEE_VAR = (100000, 100000, 0, 5000, 6000, 6000, 1000, 1000, 95000, 94000, 95000, 1000, 0, -1000)
# Collection only: C = V_total = -5000, everything contractual zero.
OPEN_VAR = (100000, 95000, -5000, 5000, 5000, 5000, 0, 0, 95000, 90000, 90000, 0, 0, -5000)
# Scenario only, within a 500 tolerance: V_total = 400, V_cond = 0, C = 0.
TOL_VAR = (100000, 100000, 0, 5000, 5000, 5000, -400, 0, 95000, 95000, 95000, 0, 0, 400)
# Required NULL: x_upper_r NULL, everything else clean.
NULL_XU = (100000, 100000, 0, 5000, 5000, None, 0, 0, 95000, 95000, 95000, 0, 0, 0)

RESULT_COLS = (
    "id, merchant_id, run_id, shipment_id, expected_collectible_poisha, "
    "reported_collected_poisha, c_poisha, x_e_poisha, x_r_poisha, x_upper_r_poisha, "
    "delta_poisha, v_r_poisha, p_e_poisha, p_cond_poisha, p_r_poisha, s_poisha, "
    "v_cond_poisha, v_total_poisha, contractual_payout_status, data_quality_status, overall_status"
)

# CL-16/CL-21 current-variance derivation, as a SQL query: the
# highest-seq calculation whose listed input set exactly equals the
# batch's current input set and whose preconditions still hold; else
# NULL. (Preconditions: batch expected payout NOT NULL and equal to the
# snapshot; every current receipt complete; listed receipt/allocation
# sets equal the current sets; net from listed allocations equals the
# snapshot; batch fully allocated.)
CURRENT_VARIANCE_SQL = """
SELECT c.bank_variance_poisha
  FROM settlement_variance_calculations c
  JOIN settlement_batches b
    ON b.merchant_id = c.merchant_id AND b.id = c.settlement_batch_id
 WHERE c.merchant_id = %s AND c.settlement_batch_id = %s
   AND b.allocation_status = 'fully_allocated'
   AND b.expected_batch_payout_poisha IS NOT NULL
   AND c.expected_batch_payout_poisha = b.expected_batch_payout_poisha
   AND NOT EXISTS (
     SELECT 1 FROM bank_receipts r
      WHERE r.merchant_id = c.merchant_id
        AND r.settlement_batch_id = c.settlement_batch_id
        AND r.is_complete IS NOT TRUE)
   AND NOT EXISTS (
     SELECT 1 FROM settlement_variance_calculation_inputs i
      WHERE i.merchant_id = c.merchant_id AND i.calculation_id = c.id
        AND i.input_kind = 'receipt'
        AND NOT EXISTS (
          SELECT 1 FROM bank_receipts r
           WHERE r.merchant_id = i.merchant_id
             AND r.settlement_batch_id = i.settlement_batch_id
             AND r.id = i.bank_receipt_id))
   AND NOT EXISTS (
     SELECT 1 FROM bank_receipts r
      WHERE r.merchant_id = c.merchant_id
        AND r.settlement_batch_id = c.settlement_batch_id
        AND NOT EXISTS (
          SELECT 1 FROM settlement_variance_calculation_inputs i
           WHERE i.merchant_id = r.merchant_id AND i.calculation_id = c.id
             AND i.input_kind = 'receipt' AND i.bank_receipt_id = r.id))
   AND NOT EXISTS (
     SELECT 1 FROM settlement_variance_calculation_inputs i
      WHERE i.merchant_id = c.merchant_id AND i.calculation_id = c.id
        AND i.input_kind = 'allocation'
        AND NOT EXISTS (
          SELECT 1 FROM settlement_allocations a
           WHERE a.merchant_id = i.merchant_id
             AND a.settlement_batch_id = i.settlement_batch_id
             AND a.id = i.settlement_allocation_id))
   AND NOT EXISTS (
     SELECT 1 FROM settlement_allocations a
      WHERE a.merchant_id = c.merchant_id
        AND a.settlement_batch_id = c.settlement_batch_id
        AND NOT EXISTS (
          SELECT 1 FROM settlement_variance_calculation_inputs i
           WHERE i.merchant_id = a.merchant_id AND i.calculation_id = c.id
             AND i.input_kind = 'allocation'
             AND i.settlement_allocation_id = a.id))
   AND (SELECT COALESCE(sum(CASE WHEN a.kind = 'reversal'
                               THEN -a.allocated_poisha ELSE a.allocated_poisha END), 0)
          FROM settlement_allocations a
         WHERE a.merchant_id = c.merchant_id
           AND a.id IN (
             SELECT i.settlement_allocation_id
               FROM settlement_variance_calculation_inputs i
              WHERE i.merchant_id = c.merchant_id AND i.calculation_id = c.id
                AND i.input_kind = 'allocation'))
       = c.allocated_bank_confirmed_total_poisha
 ORDER BY c.calculation_seq DESC
 LIMIT 1
"""


def _owner_conn():
    return psycopg.connect(
        f"host={pg_host()} dbname={DB_NAME} user=courierledger password={PASSWORD}"
    )


def _set_ctx(cur, merchant_id) -> None:
    cur.execute("SELECT set_config('app.current_merchant_id', %s, true)", (str(merchant_id),))


def _attempt(merchant_id, build):
    """Run build(cur) in one transaction, then commit.

    Returns (outcome, message): outcome is 'committed' (deferred
    triggers passed), 'insert-raised' (a statement-time check fired --
    e.g. the partial unique index) or 'commit-raised' (a deferred
    constraint trigger fired at commit)."""
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


def _insert_finding(cur, merchant_id, run_id, result_id, category, ftype, code, amount):
    cur.execute(
        "INSERT INTO reconciliation_findings (id, merchant_id, run_id, result_id, category, "
        "finding_type, finding_code, amount_poisha, is_within_tolerance, explanation_en) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false, 'M6 trigger test finding')",
        (str(uuid.uuid4()), str(merchant_id), str(run_id), str(result_id),
         category, ftype, code, amount),
    )


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
                (str(order_id), str(merchant_id), f"M6-{tag}"),
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


def _new_line_with_candidates(merchant_id, batch_id, n_candidates: int, tag: str):
    """One import row + statement line (all amounts column_absent) +
    n_candidates, each on its own shipment. Returns (line_id, [candidate_ids])."""
    row_id, line_id = uuid.uuid4(), uuid.uuid4()
    candidate_ids = [uuid.uuid4() for _ in range(n_candidates)]
    conn = _owner_conn()
    try:
        with conn.cursor() as cur:
            _set_ctx(cur, merchant_id)
            cur.execute(
                "INSERT INTO import_rows (id, merchant_id, import_batch_id, row_number, "
                "raw_values, raw_line_hash, parse_status) "
                "VALUES (%s, %s, %s, %s, '{}', %s, 'parsed')",
                (str(row_id), str(merchant_id), str(batch_id), next(_row_counter),
                 f"m6-{tag}"),
            )
            cur.execute(
                "INSERT INTO statement_lines (id, merchant_id, import_row_id, import_batch_id, "
                "match_status, reported_collected_state, reported_payout_state, "
                "reported_delivery_fee_state, reported_cod_fee_state, reported_return_fee_state, "
                "reported_other_fee_state, negative_payout_permitted) "
                "VALUES (%s, %s, %s, %s, 'unmatched', 'column_absent', 'column_absent', "
                "'column_absent', 'column_absent', 'column_absent', 'column_absent', false)",
                (str(line_id), str(merchant_id), str(row_id), str(batch_id)),
            )
        conn.commit()
    finally:
        conn.close()
    # Shipments + candidates in a second transaction.
    conn = _owner_conn()
    try:
        with conn.cursor() as cur:
            _set_ctx(cur, merchant_id)
            for i, cid in enumerate(candidate_ids):
                oid = uuid.uuid4()
                cur.execute(
                    "INSERT INTO orders (id, merchant_id, merchant_order_ref, "
                    "order_cod_poisha, currency, source) "
                    "VALUES (%s, %s, %s, 100000, 'BDT', 'manual')",
                    (str(oid), str(merchant_id), f"M6-{tag}-c{i}-{oid.hex[:8]}"),
                )
                ship_id = uuid.uuid4()
                cur.execute(
                    "INSERT INTO shipments (id, merchant_id, order_id, outcome, "
                    "expected_collectible_cod_poisha, is_cod_bearing, is_superseded) "
                    "VALUES (%s, %s, %s, 'delivered', 100000, true, false)",
                    (str(ship_id), str(merchant_id), str(oid)),
                )
                cur.execute(
                    "INSERT INTO shipment_match_candidates (id, merchant_id, statement_line_id, "
                    "shipment_id, match_basis, rank) VALUES (%s, %s, %s, %s, 'strong_tracking', %s)",
                    (str(cid), str(merchant_id), str(line_id), str(ship_id), i + 1),
                )
        conn.commit()
    finally:
        conn.close()
    return line_id, candidate_ids


def _insert_decision(cur, merchant_id, candidate_id, decision, supersedes=None):
    did = uuid.uuid4()
    cur.execute(
        "INSERT INTO shipment_match_decisions (id, merchant_id, candidate_id, decision, "
        "supersedes_decision_id) VALUES (%s, %s, %s, %s, %s)",
        (str(did), str(merchant_id), str(candidate_id), decision,
         str(supersedes) if supersedes else None),
    )
    return did


def _current_decision(merchant_id, candidate_id):
    rows = _query(
        merchant_id,
        "SELECT public.fn_current_match_decision(%s, %s)",
        (str(merchant_id), str(candidate_id)),
    )
    return rows[0][0]


def _insert_calculation(cur, merchant_id, batch_id, seq, expected, allocated, receipt_ids, alloc_ids):
    calc_id = uuid.uuid4()
    cur.execute(
        "INSERT INTO settlement_variance_calculations (id, merchant_id, settlement_batch_id, "
        "calculation_seq, expected_batch_payout_poisha, "
        "allocated_bank_confirmed_total_poisha, bank_variance_poisha, actor_type) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'system')",
        (str(calc_id), str(merchant_id), str(batch_id), seq, expected, allocated,
         allocated - expected),
    )
    for rid in receipt_ids:
        cur.execute(
            "INSERT INTO settlement_variance_calculation_inputs (id, merchant_id, "
            "settlement_batch_id, calculation_id, input_kind, bank_receipt_id) "
            "VALUES (%s, %s, %s, %s, 'receipt', %s)",
            (str(uuid.uuid4()), str(merchant_id), str(batch_id), str(calc_id), str(rid)),
        )
    for aid in alloc_ids:
        cur.execute(
            "INSERT INTO settlement_variance_calculation_inputs (id, merchant_id, "
            "settlement_batch_id, calculation_id, input_kind, settlement_allocation_id) "
            "VALUES (%s, %s, %s, %s, 'allocation', %s)",
            (str(uuid.uuid4()), str(merchant_id), str(batch_id), str(calc_id), str(aid)),
        )
    return calc_id


def _seed_cl16_batch(merchant_id, user_id, account_id, expected, alloc_status, bank_status,
                     receipts, allocs):
    """One settlement batch + receipts + allocations, committed.

    receipts: list of (amount, is_complete). allocs: list of
    (receipt_index, amount, kind, reverses_index_or_None).
    Returns (batch_id, [receipt_ids], [alloc_ids])."""
    batch_id = uuid.uuid4()
    receipt_ids, alloc_ids = [], []
    conn = _owner_conn()
    try:
        with conn.cursor() as cur:
            _set_ctx(cur, merchant_id)
            cur.execute(
                "INSERT INTO settlement_batches (id, merchant_id, merchant_courier_account_id, "
                "expected_batch_payout_poisha, allocation_status, bank_status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (str(batch_id), str(merchant_id), str(account_id), expected,
                 alloc_status, bank_status),
            )
            for amount, complete in receipts:
                rid = uuid.uuid4()
                receipt_ids.append(rid)
                cur.execute(
                    "INSERT INTO bank_receipts (id, merchant_id, settlement_batch_id, "
                    "amount_poisha, is_complete, marked_complete_by_user_id, marked_complete_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (str(rid), str(merchant_id), str(batch_id), amount, complete,
                     str(user_id) if complete else None,
                     "now()" if complete else None),
                )
            for ridx, amount, kind, rev_idx in allocs:
                aid = uuid.uuid4()
                alloc_ids.append(aid)
                cur.execute(
                    "INSERT INTO settlement_allocations (id, merchant_id, settlement_batch_id, "
                    "bank_receipt_id, allocated_poisha, kind, reverses_allocation_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (str(aid), str(merchant_id), str(batch_id), str(receipt_ids[ridx]),
                     amount, kind,
                     str(alloc_ids[rev_idx]) if rev_idx is not None else None),
                )
        conn.commit()
    finally:
        conn.close()
    return batch_id, receipt_ids, alloc_ids


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
                    "VALUES (%s, 'm6-triggers@example.com', 'M6 Triggers', 'x', 'active', "
                    "now(), now())",
                    (str(user_id),),
                )
        with psycopg.connect(
            f"host={pg_host()} dbname={DB_NAME} user=courierledger_runtime password={PASSWORD}"
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT public.create_workspace(%s, 'M6 Triggers Shop', NULL)",
                    (str(user_id),),
                )
                merchant_id = cur.fetchone()[0]
        account_id, file_id, batch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        run0_id, run500_id = uuid.uuid4(), uuid.uuid4()
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
                    "VALUES (%s, %s, 'local://m6-triggers/file.csv', 'file.csv', %s, 10, "
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
                for rid, tol, rhash in ((run0_id, 0, "m6-run-tol0"), (run500_id, 500, "m6-run-tol500")):
                    cur.execute(
                        "INSERT INTO reconciliation_runs (id, merchant_id, import_batch_id, "
                        "merchant_courier_account_id, rule_snapshot, input_snapshot, "
                        "mapping_snapshot, tolerance_poisha, engine_version, status, run_hash) "
                        "VALUES (%s, %s, %s, %s, '{}', '{}', '{}', %s, 'm6-trigger-tests', "
                        "'running', %s)",
                        (str(rid), str(merchant_id), str(batch_id), str(account_id), tol, rhash),
                    )
            conn.commit()
        # CL-16 batches: (a/b) shared valid batch, (c) its own valid batch,
        # (d) not fully allocated, (e) incomplete receipt.
        # Valid shape: expected 100000; R1 60000 + R2 40000 complete;
        # A1 60000 from R1; A2 40000 from R2; A3 reversal of A2 (40000);
        # net allocated 60000, variance -40000.
        b_ab, r_ab, a_ab = _seed_cl16_batch(
            merchant_id, user_id, account_id, 100000, "fully_allocated", "fully_reconciled",
            [(60000, True), (40000, True)],
            [(0, 60000, "allocation", None), (1, 40000, "allocation", None),
             (1, 40000, "reversal", 1)],
        )
        b_c, r_c, a_c = _seed_cl16_batch(
            merchant_id, user_id, account_id, 100000, "fully_allocated", "fully_reconciled",
            [(60000, True), (40000, True)],
            [(0, 60000, "allocation", None), (1, 40000, "allocation", None),
             (1, 40000, "reversal", 1)],
        )
        b_d, _r_d, _a_d = _seed_cl16_batch(
            merchant_id, user_id, account_id, 50000, "unallocated", "no_receipt", [], [],
        )
        b_e, r_e, _a_e = _seed_cl16_batch(
            merchant_id, user_id, account_id, 50000, "fully_allocated", "partial_receipt",
            [(50000, False)], [],
        )
        yield {
            "merchant_id": merchant_id, "user_id": user_id, "account_id": account_id,
            "batch_id": batch_id, "run0_id": run0_id, "run500_id": run500_id,
            "b_ab": b_ab, "r_ab": r_ab, "a_ab": a_ab,
            "b_c": b_c, "r_c": r_c, "a_c": a_c,
            "b_d": b_d, "b_e": b_e, "r_e": r_e,
        }
    finally:
        drop_scratch_db()


# ---------------------------------------------------------------------------
# CL-01
# ---------------------------------------------------------------------------

def test_cl01_valid_matched_commits(env):
    ship_id = _new_shipment(env["merchant_id"], "cl01-a")
    outcome, msg = _attempt(
        env["merchant_id"],
        lambda cur: _insert_result(
            cur, env["merchant_id"], env["run0_id"], ship_id, CLEAN, "matched", "ok", "matched"),
    )
    assert outcome == "committed", msg


def test_cl01_valid_mismatch_open_needs_review_tolerance_commit(env):
    mid = env["merchant_id"]
    # mismatch: fee variance, no DQ/NULL.
    ship = _new_shipment(mid, "cl01-b1")
    outcome, msg = _attempt(
        mid,
        lambda cur: (
            _insert_finding(
                cur, mid, env["run0_id"],
                _insert_result(cur, mid, env["run0_id"], ship, FEE_VAR,
                               "matched", "ok", "mismatch"),
                "financial", "fee_tax", "FEE_VAR", 1000),
        ),
    )
    assert outcome == "committed", msg
    # open: collection-only variance.
    ship = _new_shipment(mid, "cl01-b2")
    outcome, msg = _attempt(
        mid,
        lambda cur: (
            _insert_finding(
                cur, mid, env["run0_id"],
                _insert_result(cur, mid, env["run0_id"], ship, OPEN_VAR,
                               "matched", "ok", "open"),
                "financial", "collection", "COLL_DIFF", -5000),
        ),
    )
    assert outcome == "committed", msg
    # needs_review: statement variance + a data-quality finding (T-16(d)).
    ship = _new_shipment(mid, "cl01-b3")
    outcome, msg = _attempt(
        mid,
        lambda cur: (
            _insert_finding(
                cur, mid, env["run0_id"],
                _insert_result(cur, mid, env["run0_id"], ship, STMT_VAR,
                               "mismatch", "needs_review", "needs_review"),
                "data_quality", "data_quality", "DQ_MISSING_CONTRACT", None),
        ),
    )
    assert outcome == "committed", msg
    # matched_with_tolerance: scenario-only V_total 400 within the 500 run
    # tolerance (T-16(e)).
    ship = _new_shipment(mid, "cl01-b4")
    outcome, msg = _attempt(
        mid,
        lambda cur: (
            _insert_finding(
                cur, mid, env["run500_id"],
                _insert_result(cur, mid, env["run500_id"], ship, TOL_VAR,
                               "matched", "ok", "matched_with_tolerance"),
                "financial", "scenario", "SCENARIO_VAR", 400),
        ),
    )
    assert outcome == "committed", msg


def test_cl01_fee_variance_stored_as_open_raises(env):
    ship_id = _new_shipment(env["merchant_id"], "cl01-c")
    outcome, msg = _attempt(
        env["merchant_id"],
        lambda cur: _insert_result(
            cur, env["merchant_id"], env["run0_id"], ship_id, FEE_VAR,
            "matched", "ok", "open"),
    )
    assert outcome == "commit-raised", f"expected a commit-time CL-01 raise, got {outcome}: {msg}"
    assert "CL-01" in msg and "must be mismatch" in msg


def test_cl01_t16d_dq_plus_statement_variance_is_needs_review(env):
    """T-16(d): a financial variance plus missing data -> needs_review.

    Before the M6 correction this exact row was rejected at commit with
    'must be mismatch' (and stored as mismatch it was rejected by the
    needs_review-first branch), so the legitimate result was
    unrepresentable either way. It must commit now."""
    mid = env["merchant_id"]
    ship_id = _new_shipment(mid, "cl01-d")

    def build(cur):
        rid = _insert_result(cur, mid, env["run0_id"], ship_id, STMT_VAR,
                             "mismatch", "needs_review", "needs_review")
        _insert_finding(cur, mid, env["run0_id"], rid,
                        "data_quality", "data_quality", "DQ_MISSING_CONTRACT", None)
        _insert_finding(cur, mid, env["run0_id"], rid,
                        "financial", "statement_arithmetic", "STMT_VAR", -1000)

    outcome, msg = _attempt(mid, build)
    assert outcome == "committed", msg
    rows = _query(
        mid,
        "SELECT overall_status FROM reconciliation_results WHERE shipment_id = %s",
        (str(ship_id),),
    )
    assert rows and rows[0][0] == "needs_review"


def test_cl01_matched_with_nonzero_finding_raises(env):
    mid = env["merchant_id"]
    ship_id = _new_shipment(mid, "cl01-e")

    def build(cur):
        rid = _insert_result(cur, mid, env["run0_id"], ship_id, CLEAN,
                             "matched", "ok", "matched")
        _insert_finding(cur, mid, env["run0_id"], rid,
                        "financial", "collection", "COLL_DIFF", 5000)

    outcome, msg = _attempt(mid, build)
    assert outcome == "commit-raised", f"expected a commit-time CL-01 raise, got {outcome}: {msg}"
    assert "CL-01" in msg and "matched while a finding is non-zero" in msg


def test_cl01_required_null_with_matched_raises(env):
    ship_id = _new_shipment(env["merchant_id"], "cl01-f")
    outcome, msg = _attempt(
        env["merchant_id"],
        lambda cur: _insert_result(
            cur, env["merchant_id"], env["run0_id"], ship_id, NULL_XU,
            "matched", "needs_review", "matched"),
    )
    assert outcome == "commit-raised", f"expected a commit-time CL-01 raise, got {outcome}: {msg}"
    assert "CL-01" in msg and "must be needs_review" in msg


# ---------------------------------------------------------------------------
# CL-13 / CL-03
# ---------------------------------------------------------------------------

def _seed_confirmed_then_rejected(env):
    """One candidate whose chain is root 'confirmed' + successor
    'rejected' (committed). Returns (candidate_id, root_decision_id)."""
    mid = env["merchant_id"]
    _, (cand,) = _new_line_with_candidates(mid, env["batch_id"], 1, "cl13-a")
    root_id_holder: dict = {}

    def build(cur):
        root_id_holder["root"] = _insert_decision(cur, mid, cand, "confirmed")
        _insert_decision(cur, mid, cand, "rejected", supersedes=root_id_holder["root"])

    outcome, msg = _attempt(mid, build)
    assert outcome == "committed", msg
    assert _current_decision(mid, cand) == "rejected"
    return cand, root_id_holder["root"]


def test_cl13_root_then_successor_commits_and_head_is_derived(env):
    mid = env["merchant_id"]
    cand, _root = _seed_confirmed_then_rejected(env)
    assert _current_decision(mid, cand) == "rejected"


def test_cl13_second_successor_fails_at_the_unique_index(env):
    """A second successor to the same decision: the partial unique index
    (uq_match_decisions_one_successor) fires at statement time, before
    the deferred chain trigger ever runs. Assert the failure and name
    the layer that caught it."""
    mid = env["merchant_id"]
    cand, root_id = _seed_confirmed_then_rejected(env)

    def build(cur):
        _insert_decision(cur, mid, cand, "confirmed", supersedes=root_id)

    outcome, msg = _attempt(mid, build)
    assert outcome == "insert-raised", f"expected a statement-time failure, got {outcome}: {msg}"
    assert "uq_match_decisions_one_successor" in msg


def test_cl13_second_root_fails_at_commit(env):
    mid = env["merchant_id"]
    _, (cand,) = _new_line_with_candidates(mid, env["batch_id"], 1, "cl13-c")

    def build_root(cur):
        _insert_decision(cur, mid, cand, "confirmed")

    outcome, msg = _attempt(mid, build_root)
    assert outcome == "committed", msg

    def build_second_root(cur):
        _insert_decision(cur, mid, cand, "rejected")

    outcome, msg = _attempt(mid, build_second_root)
    assert outcome == "commit-raised", f"expected a commit-time CL-13 raise, got {outcome}: {msg}"
    assert "CL-13" in msg and "already has a root" in msg


def test_cl13_successor_naming_a_non_head_fails(env):
    """A successor naming the root after the root already has a successor
    (i.e. naming a non-head): in a well-formed chain every non-head
    already has exactly one successor, so the partial unique index is
    the layer that catches this at statement time; the deferred chain
    trigger's non-head check is the backstop for histories the index
    cannot see (pre-existing malformed history)."""
    mid = env["merchant_id"]
    cand, root_id = _seed_confirmed_then_rejected(env)

    def build(cur):
        _insert_decision(cur, mid, cand, "confirmed", supersedes=root_id)

    outcome, msg = _attempt(mid, build)
    assert outcome == "insert-raised", f"expected a statement-time failure, got {outcome}: {msg}"
    assert "uq_match_decisions_one_successor" in msg
    # The head is unchanged: still the rejected successor.
    assert _current_decision(mid, cand) == "rejected"


def test_cl13_e_unanimous_rejection(env):
    """Unanimous rejection (two heads, both 'rejected') cannot be built
    through the guarded insert path: a second successor is rejected by
    the partial unique index and a second root by the chain trigger, so
    a forked history is unrepresentable in a triggers-applied database.
    The multi-head branch of fn_current_match_decision therefore only
    fires for pre-existing malformed histories loaded before the
    triggers (CL-23 load-before-trigger fixtures), which this harness
    does not do. This test proves both halves of that: the fork attempt
    fails, and a well-formed chain ending in rejection derives
    'rejected'."""
    mid = env["merchant_id"]
    _, (cand,) = _new_line_with_candidates(mid, env["batch_id"], 1, "cl13-e")

    def build_fork(cur):
        _insert_decision(cur, mid, cand, "rejected")
        _insert_decision(cur, mid, cand, "rejected")

    outcome, msg = _attempt(mid, build_fork)
    assert outcome == "commit-raised", f"expected a commit-time CL-13 raise, got {outcome}: {msg}"
    assert "CL-13" in msg and "exactly one root" in msg

    _, (cand2,) = _new_line_with_candidates(mid, env["batch_id"], 1, "cl13-e2")

    def build_rejected_chain(cur):
        root = _insert_decision(cur, mid, cand2, "confirmed")
        _insert_decision(cur, mid, cand2, "rejected", supersedes=root)

    outcome, msg = _attempt(mid, build_rejected_chain)
    assert outcome == "committed", msg
    assert _current_decision(mid, cand2) == "rejected"


def test_cl13_f_current_decision_undecided_is_null(env):
    mid = env["merchant_id"]
    _, (cand,) = _new_line_with_candidates(mid, env["batch_id"], 1, "cl13-f")
    assert _current_decision(mid, cand) is None
    # A conflicted state (fn returns NULL with rows present) is likewise
    # unconstructible through the guarded insert path: every malformed
    # shape the function treats as conflicted -- a second root, a fork,
    # a cycle -- is rejected by the unique index or the chain trigger
    # before it can commit. See test_cl13_e_unanimous_rejection.


def test_cl03_two_confirmed_candidates_for_one_line_raise(env):
    mid = env["merchant_id"]
    _, (c1, c2) = _new_line_with_candidates(mid, env["batch_id"], 2, "cl03-g")

    def build_first(cur):
        _insert_decision(cur, mid, c1, "confirmed")

    outcome, msg = _attempt(mid, build_first)
    assert outcome == "committed", msg
    assert _current_decision(mid, c1) == "confirmed"

    def build_second(cur):
        _insert_decision(cur, mid, c2, "confirmed")

    outcome, msg = _attempt(mid, build_second)
    assert outcome == "commit-raised", f"expected a commit-time CL-03 raise, got {outcome}: {msg}"
    assert "CL-03" in msg and "currently-confirmed candidates" in msg


# ---------------------------------------------------------------------------
# CL-16
# ---------------------------------------------------------------------------

def test_cl16_matching_calculation_commits(env):
    mid = env["merchant_id"]

    def build(cur):
        _insert_calculation(cur, mid, env["b_ab"], 1, 100000, 60000,
                            env["r_ab"], env["a_ab"])

    outcome, msg = _attempt(mid, build)
    assert outcome == "committed", msg
    rows = _query(
        mid,
        "SELECT bank_variance_poisha FROM settlement_variance_calculations "
        "WHERE settlement_batch_id = %s AND calculation_seq = 1",
        (str(env["b_ab"]),),
    )
    assert rows and rows[0][0] == -40000


def test_cl16_snapshot_disagreement_raises(env):
    mid = env["merchant_id"]

    def build(cur):
        # Same input set, but the snapshot claims 70000 allocated while
        # the listed allocations net to 60000.
        _insert_calculation(cur, mid, env["b_ab"], 2, 100000, 70000,
                            env["r_ab"], env["a_ab"])

    outcome, msg = _attempt(mid, build)
    assert outcome == "commit-raised", f"expected a commit-time CL-16 raise, got {outcome}: {msg}"
    assert "CL-16" in msg and "snapshots allocated total 70000" in msg


def test_cl16_new_receipt_makes_current_variance_null(env):
    mid = env["merchant_id"]
    # A valid calculation first (seq 1 on the (c) batch).
    outcome, msg = _attempt(
        mid,
        lambda cur: _insert_calculation(cur, mid, env["b_c"], 1, 100000, 60000,
                                        env["r_c"], env["a_c"]),
    )
    assert outcome == "committed", msg
    rows = _query(mid, CURRENT_VARIANCE_SQL, (str(mid), str(env["b_c"])))
    assert rows and rows[0][0] == -40000, f"current variance before change: {rows}"
    # Add a new complete receipt to the batch: the old calculation's
    # listed input set no longer equals the batch's current set.
    new_receipt = uuid.uuid4()
    conn = _owner_conn()
    try:
        with conn.cursor() as cur:
            _set_ctx(cur, mid)
            cur.execute(
                "INSERT INTO bank_receipts (id, merchant_id, settlement_batch_id, "
                "amount_poisha, is_complete, marked_complete_by_user_id, marked_complete_at) "
                "VALUES (%s, %s, %s, 10000, true, %s, now())",
                (str(new_receipt), str(mid), str(env["b_c"]), str(env["user_id"])),
            )
        conn.commit()
    finally:
        conn.close()
    rows = _query(mid, CURRENT_VARIANCE_SQL, (str(mid), str(env["b_c"])))
    assert rows == [] or rows[0][0] is None, f"current variance after change: {rows}"
    # Append-only: the old calculation row is still there, unchanged.
    rows = _query(
        mid,
        "SELECT calculation_seq, expected_batch_payout_poisha, "
        "allocated_bank_confirmed_total_poisha, bank_variance_poisha "
        "FROM settlement_variance_calculations WHERE settlement_batch_id = %s",
        (str(env["b_c"]),),
    )
    assert rows == [(1, 100000, 60000, -40000)], rows


def test_cl16_batch_not_fully_allocated_raises(env):
    mid = env["merchant_id"]

    def build(cur):
        _insert_calculation(cur, mid, env["b_d"], 1, 50000, 0, [], [])

    outcome, msg = _attempt(mid, build)
    assert outcome == "commit-raised", f"expected a commit-time CL-16 raise, got {outcome}: {msg}"
    assert "CL-16" in msg and "not fully allocated" in msg


def test_cl16_incomplete_receipt_raises(env):
    mid = env["merchant_id"]

    def build(cur):
        _insert_calculation(cur, mid, env["b_e"], 1, 50000, 0, env["r_e"], [])

    outcome, msg = _attempt(mid, build)
    assert outcome == "commit-raised", f"expected a commit-time CL-16 raise, got {outcome}: {msg}"
    assert "CL-16" in msg and "incomplete receipt" in msg


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------

def _psql_as_postgres(sql: str):
    """Run one SQL statement as the postgres superuser (bypasses RLS, so
    the append-only trigger itself is what fires -- as the owner or
    runtime role the missing UPDATE/DELETE policies would silently
    affect zero rows instead). Returns (returncode, combined output)."""
    proc = subprocess.run(
        ["su", "postgres", "-c",
         f"psql -d {DB_NAME} -v ON_ERROR_STOP=1 -q -c \"{sql}\""],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_append_only_update_and_delete_on_results_raise(env):
    mid = env["merchant_id"]
    ship_id = _new_shipment(mid, "append-only")
    result_id_holder: dict = {}

    def build(cur):
        result_id_holder["id"] = _insert_result(
            cur, mid, env["run0_id"], ship_id, CLEAN, "matched", "ok", "matched")

    outcome, msg = _attempt(mid, build)
    assert outcome == "committed", msg
    rid = result_id_holder["id"]
    rc, out = _psql_as_postgres(
        f"UPDATE reconciliation_results SET overall_status = 'open' WHERE id = '{rid}';"
    )
    assert rc != 0, f"UPDATE should have failed, output: {out}"
    assert "append-only table reconciliation_results: UPDATE is not permitted" in out
    rc, out = _psql_as_postgres(
        f"DELETE FROM reconciliation_results WHERE id = '{rid}';"
    )
    assert rc != 0, f"DELETE should have failed, output: {out}"
    assert "append-only table reconciliation_results: DELETE is not permitted" in out
    rows = _query(
        mid,
        "SELECT overall_status FROM reconciliation_results WHERE id = %s",
        (str(rid),),
    )
    assert rows and rows[0][0] == "matched"
