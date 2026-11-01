"""Reports summary API tests (M1 Milestone 8, part 2) -- SQLite
fallback suite.

Covers: aggregates match seeded rows exactly (run counts by
status, result counts by overall status, the five variances as
SEPARATE totals with NULLs excluded and counted, settlement batch
counts by allocation/bank status, bank receipt counts by
completeness, import batch counts by status), empty workspace
returns honest zeros, cross-workspace 404, unauthenticated 401.

Runs/results are seeded directly (the run store's own API tests
cover the create path); imports, settlement batches and receipts
are seeded through the real API. All data is synthetic. Money is
integer poisha.

NOTE: app/main.py is wired by the coordinator, so these tests mount
the reports router themselves (guarded: skipped when the route
already exists).
"""

import json
import uuid
from pathlib import Path

from app.db.models import (
    Courier,
    Order,
    ReconciliationResult,
    ReconciliationRun,
    Shipment,
)
from app.db.session import SessionLocal
from app.main import app
from app.api import reports as reports_api
from tests.conftest import get_csrf, login, post, register

if not any(
    getattr(r, "path", "") == "/api/workspaces/{workspace_id}/reports/summary"
    for r in app.routes
):
    app.include_router(reports_api.router, prefix="/api")

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


def _setup(client, email="m8-reports@example.com"):
    """Register + log in, create a workspace + Steadfast account."""
    session = SessionLocal()
    try:
        session.add(Courier(code="steadfast", name="Steadfast (synthetic test row)"))
        session.commit()
    finally:
        session.close()
    register(client, email)
    login(client, email)
    resp = post(client, "/api/workspaces", {"name": "M8 Reports Shop"})
    assert resp.status_code == 201, resp.text
    workspace_id = resp.json()["id"]
    resp = post(
        client,
        f"/api/workspaces/{workspace_id}/courier-accounts",
        {"courier_code": "steadfast", "display_label": "Steadfast test account"},
    )
    assert resp.status_code == 201, resp.text
    return workspace_id, resp.json()["id"]


def _upload(client, ws, account_id):
    resp = client.post(
        f"/api/workspaces/{ws}/imports",
        files={"file": ("statement.csv", FIXTURE.read_bytes(), "text/csv")},
        data={
            "mapping": json.dumps(MAPPING),
            "merchant_courier_account_id": account_id,
        },
        headers={"X-CSRF-Token": get_csrf(client)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _batch(client, ws, account, expected=100000):
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches",
        {"merchant_courier_account_id": account,
         "expected_batch_payout_poisha": expected},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _receipt(client, ws, batch_id, amount):
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch_id}/receipts",
        {"amount_poisha": amount},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _seed_run_and_results(workspace_id, account_id, batch_id):
    """One completed run with three results whose variances exercise
    NULL handling exactly (row 3 is the M6-A1 shape: V_total -2000
    with C/delta/V_R/S all NULL, V_cond 0 with S/V_R NULL)."""
    ws = uuid.UUID(workspace_id)
    session = SessionLocal()
    try:
        shipment_ids = []
        for ref, cod in (("ORD-R1", 100000), ("ORD-R2", 95000), ("ORD-R3", 90000)):
            order = Order(
                merchant_id=ws, merchant_order_ref=ref, order_cod_poisha=cod
            )
            session.add(order)
            session.flush()
            shipment = Shipment(
                merchant_id=ws,
                order_id=order.id,
                merchant_courier_account_id=uuid.UUID(account_id),
                tracking_id_raw=f"TRK-{ref}",
                tracking_id_normalised=f"TRK-{ref}",
                outcome="delivered",
                outcome_source="merchant_confirmed",
                expected_collectible_cod_poisha=cod,
                is_cod_bearing=True,
            )
            session.add(shipment)
            session.flush()
            shipment_ids.append(shipment.id)
        run = ReconciliationRun(
            merchant_id=ws,
            import_batch_id=uuid.UUID(batch_id),
            merchant_courier_account_id=uuid.UUID(account_id),
            rule_snapshot={"fixture": "test-seed"},
            input_snapshot={"seeded": True},
            mapping_snapshot={},
            tolerance_poisha=0,
            engine_version="test-1",
            status="completed",
            run_hash="m8-reports-run-hash-1",
        )
        session.add(run)
        session.flush()
        # Row 1: matched, every variance 0.
        session.add(ReconciliationResult(
            merchant_id=ws, run_id=run.id, shipment_id=shipment_ids[0],
            c_poisha=0, delta_poisha=0, v_r_poisha=0, s_poisha=0,
            v_cond_poisha=0, v_total_poisha=0,
            contractual_payout_status="matched", data_quality_status="ok",
            overall_status="matched",
        ))
        # Row 2: open; C -5000, delta -50, V_R 0, S 0, V_total -4950,
        # V_cond 0 (identities hold: 0 - 0 = 0; -5000 + 50 - 0 + 0).
        session.add(ReconciliationResult(
            merchant_id=ws, run_id=run.id, shipment_id=shipment_ids[1],
            c_poisha=-5000, delta_poisha=-50, v_r_poisha=0, s_poisha=0,
            v_cond_poisha=0, v_total_poisha=-4950,
            contractual_payout_status="matched", data_quality_status="ok",
            overall_status="open",
        ))
        # Row 3: needs_review; V_total -2000 with C/delta/V_R/S NULL,
        # V_cond 0 with S/V_R NULL.
        session.add(ReconciliationResult(
            merchant_id=ws, run_id=run.id, shipment_id=shipment_ids[2],
            c_poisha=None, delta_poisha=None, v_r_poisha=None, s_poisha=None,
            v_cond_poisha=0, v_total_poisha=-2000,
            contractual_payout_status="not_calculable",
            data_quality_status="needs_review", overall_status="needs_review",
        ))
        session.commit()
    finally:
        session.close()


def _summary(client, ws):
    resp = client.get(f"/api/workspaces/{ws}/reports/summary")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_summary_matches_seeded_rows_exactly(client):
    ws, account = _setup(client)
    batch = _upload(client, ws, account)
    _seed_run_and_results(ws, account, batch["batch_id"])

    # Settlement batch 1: receipt 60000 complete + allocated in full
    # -> fully_allocated / fully_reconciled. Batch 2: no receipt.
    sb1 = _batch(client, ws, account, expected=100000)
    r1 = _receipt(client, ws, sb1["batch_id"], 60000)
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{sb1['batch_id']}"
        f"/receipts/{r1['receipt_id']}/complete",
        {},
    )
    assert resp.status_code == 200, resp.text
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{sb1['batch_id']}/allocations",
        {"bank_receipt_id": r1["receipt_id"], "allocated_poisha": 60000},
    )
    assert resp.status_code == 201, resp.text
    sb2 = _batch(client, ws, account, expected=50000)
    # Second receipt on batch 1, left incomplete.
    _receipt(client, ws, sb1["batch_id"], 5000)

    summary = _summary(client, ws)
    assert summary["workspace_id"] == ws

    assert summary["reconciliation_runs"] == {
        "total": 1,
        "by_status": {"running": 0, "completed": 1, "failed": 0},
    }
    assert summary["reconciliation_results"]["total"] == 3
    assert summary["reconciliation_results"]["by_overall_status"] == {
        "matched": 1, "open": 1, "mismatch": 0,
        "needs_review": 1, "matched_with_tolerance": 0,
    }
    # Five variances, each totalled separately over its own non-NULL
    # values; NULLs excluded from the total and counted beside it.
    # The totals are NOT added together anywhere.
    variances = summary["reconciliation_results"]["variances_poisha"]
    assert variances["collection_c"] == {
        "total_poisha": -5000, "non_null_count": 2, "null_count": 1,
    }
    assert variances["fee_v_r"] == {
        "total_poisha": 0, "non_null_count": 2, "null_count": 1,
    }
    assert variances["statement_s"] == {
        "total_poisha": 0, "non_null_count": 2, "null_count": 1,
    }
    assert variances["conditional_payout_v_cond"] == {
        "total_poisha": 0, "non_null_count": 3, "null_count": 0,
    }
    assert variances["scenario_v_total"] == {
        "total_poisha": -6950, "non_null_count": 3, "null_count": 0,
    }

    assert summary["settlement_batches"]["total"] == 2
    assert summary["settlement_batches"]["by_allocation_status"] == {
        "unallocated": 1, "partially_allocated": 1, "fully_allocated": 0,
    }
    assert summary["settlement_batches"]["by_bank_status"] == {
        "no_receipt": 1, "partial_receipt": 0,
        "receipt_complete_unallocated": 1, "fully_reconciled": 0,
    }
    # Batch 1 gained a second, incomplete receipt after full
    # allocation, so it is partially_allocated, and (one receipt
    # complete, not fully allocated) receipt_complete_unallocated.
    assert summary["bank_receipts"] == {
        "total": 2, "complete": 1, "incomplete": 1,
    }
    assert summary["import_batches"]["total"] == 1
    assert summary["import_batches"]["by_status"] == {
        "uploaded": 0, "mapped": 0, "parsed": 1,
        "parse_failed": 0, "ready": 0, "superseded": 0,
    }


def test_summary_empty_workspace_is_honest_zeros(client):
    ws, _account = _setup(client)
    summary = _summary(client, ws)
    assert summary["reconciliation_runs"] == {
        "total": 0,
        "by_status": {"running": 0, "completed": 0, "failed": 0},
    }
    assert summary["reconciliation_results"]["total"] == 0
    assert summary["reconciliation_results"]["by_overall_status"] == {
        "matched": 0, "open": 0, "mismatch": 0,
        "needs_review": 0, "matched_with_tolerance": 0,
    }
    for key, entry in summary["reconciliation_results"]["variances_poisha"].items():
        assert entry == {"total_poisha": 0, "non_null_count": 0, "null_count": 0}, key
    assert summary["settlement_batches"]["total"] == 0
    assert summary["bank_receipts"] == {"total": 0, "complete": 0, "incomplete": 0}
    assert summary["import_batches"]["total"] == 0


def test_summary_tenancy_and_auth(client):
    ws, _account = _setup(client)
    anon = client.__class__(app)
    resp = anon.get(f"/api/workspaces/{ws}/reports/summary")
    assert resp.status_code == 401, resp.text
    register(client, "m8-reports-b@example.com")
    login(client, "m8-reports-b@example.com")
    resp = client.get(f"/api/workspaces/{ws}/reports/summary")
    assert resp.status_code == 404, resp.text
