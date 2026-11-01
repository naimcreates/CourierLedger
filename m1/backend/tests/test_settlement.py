"""Settlement batch / bank receipt / allocation / variance API tests
(M1 Milestone 7, part 2) -- SQLite fallback suite. Live-PostgreSQL
coverage (RLS + runtime role + the deferred CL-10 / CL-16 triggers)
lives in tests/test_settlement_live_pg.py.

Covers: batch creation + validation, receipt lifecycle (including
idempotent completion and bank-account-number rejection), allocation
to fully_reconciled, over-allocation 422, allocation without an
expected payout capping at partially_allocated, reversal rules,
variance preconditions (422 cases), variance superseded by a new
allocation / receipt, amount-only never matching anything, tenant
isolation, unauthenticated 401, CSRF 403, pagination.

All data is synthetic. Money is integer poisha.
"""

import pytest

from tests.conftest import get_csrf, post, register, login


def _setup(client, email="m7-user@example.com"):
    """Register + log in, create a workspace + Steadfast account.
    Returns (workspace_id, account_id)."""
    # The couriers reference list is seeded by SQL in live PG; on the
    # SQLite fallback the row must exist for the account endpoint
    # (same pattern as tests/test_run_store.py::_api_setup).
    from app.db.models import Courier
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        session.add(Courier(code="steadfast", name="Steadfast (synthetic test row)"))
        session.commit()
    finally:
        session.close()
    register(client, email)
    login(client, email)
    resp = post(client, "/api/workspaces", {"name": "M7 Shop"})
    assert resp.status_code == 201, resp.text
    workspace_id = resp.json()["id"]
    resp = post(
        client,
        f"/api/workspaces/{workspace_id}/courier-accounts",
        {"courier_code": "steadfast", "display_label": "Steadfast test account"},
    )
    assert resp.status_code == 201, resp.text
    return workspace_id, resp.json()["id"]


def _batch(client, ws, account, expected=100000, **extra):
    body = {"merchant_courier_account_id": account, "expected_batch_payout_poisha": expected}
    body.update(extra)
    resp = post(client, f"/api/workspaces/{ws}/settlement-batches", body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _receipt(client, ws, batch_id, amount=100000, **extra):
    body = {"amount_poisha": amount}
    body.update(extra)
    resp = post(
        client, f"/api/workspaces/{ws}/settlement-batches/{batch_id}/receipts", body
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _complete(client, ws, batch_id, receipt_id):
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch_id}/receipts/{receipt_id}/complete",
        {},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _allocate(client, ws, batch_id, receipt_id, amount):
    return post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch_id}/allocations",
        {"bank_receipt_id": receipt_id, "allocated_poisha": amount},
    )


def _detail(client, ws, batch_id):
    resp = client.get(f"/api/workspaces/{ws}/settlement-batches/{batch_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_batch_defaults_and_validation(client):
    ws, account = _setup(client)
    batch = _batch(
        client, ws, account, expected=100000,
        period_start="2026-10-01", period_end="2026-10-31",
    )
    assert batch["allocation_status"] == "unallocated"
    assert batch["bank_status"] == "no_receipt"
    assert batch["allocated_total_poisha"] is None
    assert batch["current_bank_variance_poisha"] is None
    assert batch["expected_batch_payout_poisha"] == 100000
    assert batch["period_start"] == "2026-10-01"
    assert batch["period_end"] == "2026-10-31"

    # Unknown courier account in this workspace -> 404.
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches",
        {"merchant_courier_account_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 404, resp.text
    # Unknown run -> 404.
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches",
        {
            "merchant_courier_account_id": account,
            "run_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert resp.status_code == 404, resp.text
    # Inverted period -> 422.
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches",
        {
            "merchant_courier_account_id": account,
            "period_start": "2026-10-31",
            "period_end": "2026-10-01",
        },
    )
    assert resp.status_code == 422, resp.text
    # Negative expected payout -> 422; NULL expected is allowed.
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches",
        {"merchant_courier_account_id": account, "expected_batch_payout_poisha": -1},
    )
    assert resp.status_code == 422, resp.text
    batch2 = _batch(client, ws, account, expected=None)
    assert batch2["expected_batch_payout_poisha"] is None


def test_receipt_lifecycle_and_idempotent_complete(client):
    ws, account = _setup(client)
    batch = _batch(client, ws, account)
    receipt = _receipt(client, ws, batch["batch_id"], 100000,
                       receipt_ref_label="Oct bank stmt")
    assert receipt["is_complete"] is False
    assert receipt["settlement_batch_id"] == batch["batch_id"]
    assert receipt["marked_complete_by_user_id"] is None
    assert receipt["marked_complete_at"] is None
    assert _detail(client, ws, batch["batch_id"])["bank_status"] == "partial_receipt"

    completed = _complete(client, ws, batch["batch_id"], receipt["receipt_id"])
    assert completed["is_complete"] is True
    assert completed["marked_complete_by_user_id"] is not None
    assert completed["marked_complete_at"] is not None
    assert (
        _detail(client, ws, batch["batch_id"])["bank_status"]
        == "receipt_complete_unallocated"
    )

    # Idempotent replay: same receipt, no duplicate row, no error.
    again = _complete(client, ws, batch["batch_id"], receipt["receipt_id"])
    assert again["receipt_id"] == receipt["receipt_id"]
    assert again["is_complete"] is True
    assert again["marked_complete_at"] == completed["marked_complete_at"]
    assert len(_detail(client, ws, batch["batch_id"])["receipts"]) == 1

    # Amount must be > 0.
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/receipts",
        {"amount_poisha": 0},
    )
    assert resp.status_code == 422, resp.text


def test_bank_account_number_is_rejected(client):
    ws, account = _setup(client)
    batch = _batch(client, ws, account)
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/receipts",
        {"amount_poisha": 100000, "bank_account_number": "1234567890"},
    )
    assert resp.status_code == 422, resp.text
    assert "bank_account_number" in resp.text.lower()
    assert _detail(client, ws, batch["batch_id"])["receipts"] == []


def test_allocation_flow_to_fully_reconciled(client):
    ws, account = _setup(client)
    batch = _batch(client, ws, account, expected=100000)
    receipt = _receipt(client, ws, batch["batch_id"], 100000)
    _complete(client, ws, batch["batch_id"], receipt["receipt_id"])

    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 60000)
    assert resp.status_code == 201, resp.text
    alloc = resp.json()
    assert alloc["kind"] == "allocation"
    detail = _detail(client, ws, batch["batch_id"])
    assert detail["allocation_status"] == "partially_allocated"
    assert detail["allocated_total_poisha"] == 60000
    assert detail["net_allocated_total_poisha"] == 60000
    assert detail["bank_status"] == "receipt_complete_unallocated"

    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 40000)
    assert resp.status_code == 201, resp.text
    detail = _detail(client, ws, batch["batch_id"])
    assert detail["allocation_status"] == "fully_allocated"
    assert detail["allocated_total_poisha"] == 100000
    assert detail["bank_status"] == "fully_reconciled"


def test_over_allocation_rejected_and_receipt_must_match_batch(client):
    ws, account = _setup(client)
    batch = _batch(client, ws, account, expected=100000)
    other = _batch(client, ws, account, expected=50000)
    receipt = _receipt(client, ws, batch["batch_id"], 100000)
    _complete(client, ws, batch["batch_id"], receipt["receipt_id"])

    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 100001)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "over_allocation"

    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 100000)
    assert resp.status_code == 201, resp.text
    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 1)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "over_allocation"

    # A receipt linked to another batch cannot be allocated here.
    resp = _allocate(client, ws, other["batch_id"], receipt["receipt_id"], 1)
    assert resp.status_code == 404, resp.text
    # Unknown receipt -> 404.
    resp = _allocate(
        client, ws, batch["batch_id"],
        "00000000-0000-0000-0000-000000000000", 1,
    )
    assert resp.status_code == 404, resp.text


def test_allocation_without_expected_reaches_fully_allocated_but_no_variance(client):
    # Fully allocated is about the receipts being fully allocated, not
    # about the net equalling the expected payout (v0.1 T-17): with no
    # expected payout a batch whose receipts are fully allocated is
    # fully_allocated -- but variance calculation still needs an
    # expected payout, so it 422s.
    ws, account = _setup(client)
    batch = _batch(client, ws, account, expected=None)
    receipt = _receipt(client, ws, batch["batch_id"], 50000)
    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 50000)
    assert resp.status_code == 201, resp.text
    detail = _detail(client, ws, batch["batch_id"])
    assert detail["allocation_status"] == "fully_allocated"
    assert detail["bank_status"] == "partial_receipt"
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "expected_payout_null"


def test_calculate_variance_nonzero_shortfall_and_surplus(client):
    # The bank variance is informative exactly when receipts differ
    # from the expectation: 60000 received against 100000 expected
    # gives -40000; 120000 received gives +20000. (The M6 CL-16 live
    # test recorded -40000 the same way.)
    ws, account = _setup(client)
    batch = _batch(client, ws, account, expected=100000)
    receipt = _receipt(client, ws, batch["batch_id"], 60000)
    _complete(client, ws, batch["batch_id"], receipt["receipt_id"])
    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 60000)
    assert resp.status_code == 201, resp.text
    assert _detail(client, ws, batch["batch_id"])["allocation_status"] == "fully_allocated"
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["bank_variance_poisha"] == -40000
    assert resp.json()["allocated_bank_confirmed_total_poisha"] == 60000
    assert _detail(client, ws, batch["batch_id"])["current_bank_variance_poisha"] == -40000

    batch2 = _batch(client, ws, account, expected=100000)
    receipt2 = _receipt(client, ws, batch2["batch_id"], 120000)
    _complete(client, ws, batch2["batch_id"], receipt2["receipt_id"])
    resp = _allocate(client, ws, batch2["batch_id"], receipt2["receipt_id"], 120000)
    assert resp.status_code == 201, resp.text
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch2['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["bank_variance_poisha"] == 20000


def test_reversal_rules(client):
    ws, account = _setup(client)
    batch = _batch(client, ws, account, expected=100000)
    receipt = _receipt(client, ws, batch["batch_id"], 100000)
    _complete(client, ws, batch["batch_id"], receipt["receipt_id"])
    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 100000)
    assert resp.status_code == 201, resp.text
    alloc_id = resp.json()["allocation_id"]
    assert _detail(client, ws, batch["batch_id"])["bank_status"] == "fully_reconciled"

    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}"
        f"/allocations/{alloc_id}/reverse",
        {},
    )
    assert resp.status_code == 201, resp.text
    reversal = resp.json()
    assert reversal["kind"] == "reversal"
    assert reversal["reverses_allocation_id"] == alloc_id
    assert reversal["allocated_poisha"] == 100000
    assert reversal["allocation_id"] != alloc_id
    detail = _detail(client, ws, batch["batch_id"])
    assert detail["net_allocated_total_poisha"] == 0
    assert detail["allocated_total_poisha"] == 0
    assert detail["allocation_status"] == "unallocated"
    # The original row is still there: append-only, never edited.
    assert len(detail["allocations"]) == 2

    # Duplicate reversal -> 422.
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}"
        f"/allocations/{alloc_id}/reverse",
        {},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "already_reversed"
    # Reversing the reversal itself -> 422.
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}"
        f"/allocations/{reversal['allocation_id']}/reverse",
        {},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "cannot_reverse_reversal"
    # Unknown allocation -> 404.
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}"
        "/allocations/00000000-0000-0000-0000-000000000000/reverse",
        {},
    )
    assert resp.status_code == 404, resp.text


def test_calculate_variance_preconditions(client):
    ws, account = _setup(client)
    # No expected payout -> 422 expected_payout_null.
    batch = _batch(client, ws, account, expected=None)
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "expected_payout_null"

    # Not fully allocated -> 422 not_fully_allocated.
    batch = _batch(client, ws, account, expected=100000)
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "not_fully_allocated"

    # Fully allocated but receipt incomplete -> 422 receipt_incomplete.
    receipt = _receipt(client, ws, batch["batch_id"], 100000)
    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 100000)
    assert resp.status_code == 201, resp.text
    assert _detail(client, ws, batch["batch_id"])["allocation_status"] == "fully_allocated"
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "receipt_incomplete"
    # Nothing was written.
    assert _detail(client, ws, batch["batch_id"])["variance_calculations"] == []


def test_calculate_variance_happy_path_and_superseded(client):
    ws, account = _setup(client)
    batch = _batch(client, ws, account, expected=100000)
    receipt = _receipt(client, ws, batch["batch_id"], 100000)
    _complete(client, ws, batch["batch_id"], receipt["receipt_id"])
    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 100000)
    assert resp.status_code == 201, resp.text

    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 201, resp.text
    calc = resp.json()
    # Receipts (100000) equal the expectation (100000) here, so the
    # variance is 0; see test_calculate_variance_nonzero_shortfall_and_
    # surplus for the informative non-zero cases.
    assert calc == {
        "calculation_seq": 1,
        "expected_batch_payout_poisha": 100000,
        "allocated_bank_confirmed_total_poisha": 100000,
        "bank_variance_poisha": 0,
    }
    resp = client.get(
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/current-variance"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "bank_variance_poisha": 0,
        "calculation_seq": 1,
        "variance_status": "ok",
    }
    detail = _detail(client, ws, batch["batch_id"])
    assert detail["current_bank_variance_poisha"] == 0
    assert detail["variance_status"] == "ok"
    assert len(detail["variance_calculations"]) == 1

    # A new allocation supersedes the old calculation: the old row
    # stays, but the current variance becomes NULL.
    alloc_id = detail["allocations"][0]["allocation_id"]
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}"
        f"/allocations/{alloc_id}/reverse",
        {},
    )
    assert resp.status_code == 201, resp.text
    resp = client.get(
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/current-variance"
    )
    assert resp.json()["bank_variance_poisha"] is None
    assert resp.json()["variance_status"] == "not_fully_allocated"
    detail = _detail(client, ws, batch["batch_id"])
    assert len(detail["variance_calculations"]) == 1  # never deleted

    # Re-allocate and recalculate: seq 2, current again.
    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 100000)
    assert resp.status_code == 201, resp.text
    resp = client.get(
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/current-variance"
    )
    assert resp.json()["variance_status"] == "input_set_changed"
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["calculation_seq"] == 2
    assert resp.json()["bank_variance_poisha"] == 0
    resp = client.get(
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/current-variance"
    )
    assert resp.json() == {
        "bank_variance_poisha": 0,
        "calculation_seq": 2,
        "variance_status": "ok",
    }


def test_new_receipt_invalidates_variance_with_reasons(client):
    ws, account = _setup(client)
    batch = _batch(client, ws, account, expected=80000)
    receipt = _receipt(client, ws, batch["batch_id"], 80000)
    _complete(client, ws, batch["batch_id"], receipt["receipt_id"])
    resp = _allocate(client, ws, batch["batch_id"], receipt["receipt_id"], 80000)
    assert resp.status_code == 201, resp.text
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 201, resp.text

    # A new, incomplete receipt: the batch is no longer fully
    # allocated (the new receipt's 5000 is unallocated), so the
    # variance is NULL with not_fully_allocated. Fully allocated is
    # about the receipts being fully allocated (v0.1 T-17), so adding
    # an unallocated receipt drops the status -- it is not merely an
    # input-set change while the batch stays fully allocated.
    receipt2 = _receipt(client, ws, batch["batch_id"], 5000)
    resp = client.get(
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/current-variance"
    )
    assert resp.json()["bank_variance_poisha"] is None
    assert resp.json()["variance_status"] == "not_fully_allocated"
    # Completing it still leaves it unallocated: still NULL.
    _complete(client, ws, batch["batch_id"], receipt2["receipt_id"])
    resp = client.get(
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/current-variance"
    )
    assert resp.json()["bank_variance_poisha"] is None
    assert resp.json()["variance_status"] == "not_fully_allocated"
    # Allocating it restores fully_allocated -- but the recorded
    # calculation's input set does not include the new receipt or its
    # allocation, so the current variance is still NULL, now with
    # input_set_changed.
    resp = _allocate(client, ws, batch["batch_id"], receipt2["receipt_id"], 5000)
    assert resp.status_code == 201, resp.text
    assert _detail(client, ws, batch["batch_id"])["allocation_status"] == "fully_allocated"
    resp = client.get(
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/current-variance"
    )
    assert resp.json()["bank_variance_poisha"] is None
    assert resp.json()["variance_status"] == "input_set_changed"
    # Recalculation records seq 2: 85000 allocated against 80000
    # expected, variance +5000.
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/calculate-variance",
        {},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["calculation_seq"] == 2
    assert resp.json()["bank_variance_poisha"] == 5000


def test_current_variance_no_calculation(client):
    ws, account = _setup(client)
    batch = _batch(client, ws, account, expected=100000)
    resp = client.get(
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/current-variance"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "bank_variance_poisha": None,
        "calculation_seq": None,
        "variance_status": "no_calculation",
    }


def test_amount_only_never_matches_anything(client):
    """Two receipts with the same amount stay linked only to the
    batch they were created under; no endpoint links by amount."""
    ws, account = _setup(client)
    batch_a = _batch(client, ws, account, expected=50000)
    batch_b = _batch(client, ws, account, expected=50000)
    receipt_a = _receipt(client, ws, batch_a["batch_id"], 50000)
    receipt_b = _receipt(client, ws, batch_b["batch_id"], 50000)
    assert receipt_a["settlement_batch_id"] == batch_a["batch_id"]
    assert receipt_b["settlement_batch_id"] == batch_b["batch_id"]
    assert _detail(client, ws, batch_a["batch_id"])["receipts"][0]["receipt_id"] == (
        receipt_a["receipt_id"]
    )
    assert _detail(client, ws, batch_b["batch_id"])["receipts"][0]["receipt_id"] == (
        receipt_b["receipt_id"]
    )
    # Listing receipts shows both, each with its own batch link.
    resp = client.get(f"/api/workspaces/{ws}/bank-receipts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    by_id = {r["receipt_id"]: r for r in body["receipts"]}
    assert by_id[receipt_a["receipt_id"]]["settlement_batch_id"] == batch_a["batch_id"]
    assert by_id[receipt_b["receipt_id"]]["settlement_batch_id"] == batch_b["batch_id"]
    assert all(r["is_complete"] is False for r in body["receipts"])


def test_list_batches_pagination(client):
    ws, account = _setup(client)
    ids = [_batch(client, ws, account, expected=1000 + i)["batch_id"] for i in range(3)]
    resp = client.get(f"/api/workspaces/{ws}/settlement-batches?limit=2&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["settlement_batches"]) == 2
    # Newest first: created_at is non-increasing down the page. (The
    # tie-break is the random UUID, and SQLite's CURRENT_TIMESTAMP
    # has 1-second resolution, so equal timestamps are routine here;
    # on PostgreSQL the timestamps separate the rows. What must hold
    # on both is that paging covers every batch exactly once.)
    created = [b["created_at"] for b in body["settlement_batches"]]
    assert created == sorted(created, reverse=True)
    resp = client.get(f"/api/workspaces/{ws}/settlement-batches?limit=2&offset=2")
    page2 = resp.json()["settlement_batches"]
    assert len(page2) == 1
    assert {b["batch_id"] for b in body["settlement_batches"]} | {
        b["batch_id"] for b in page2
    } == set(ids)


def test_tenant_isolation_auth_and_csrf(client):
    ws, account = _setup(client, "m7-a@example.com")
    batch = _batch(client, ws, account)
    # Second user: 404 everywhere (no existence leak).
    register(client, "m7-b@example.com")
    login(client, "m7-b@example.com")
    for path in [
        f"/api/workspaces/{ws}/settlement-batches",
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}",
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/current-variance",
        f"/api/workspaces/{ws}/bank-receipts",
    ]:
        resp = client.get(path)
        assert resp.status_code == 404, (path, resp.text)
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch['batch_id']}/receipts",
        {"amount_poisha": 1000},
    )
    assert resp.status_code == 404, resp.text
    resp = post(
        client, f"/api/workspaces/{ws}/settlement-batches",
        {"merchant_courier_account_id": account},
    )
    assert resp.status_code == 404, resp.text

    # Unauthenticated: GET -> 401; POST without a CSRF token -> 403
    # (the CSRF dependency runs before auth, exactly as on the runs
    # router -- a POST with no token is refused before auth is even
    # checked).
    from fastapi.testclient import TestClient
    from app.main import app

    anon = TestClient(app)
    resp = anon.get(f"/api/workspaces/{ws}/settlement-batches")
    assert resp.status_code == 401, resp.text
    resp = anon.post(
        f"/api/workspaces/{ws}/settlement-batches",
        json={"merchant_courier_account_id": account},
    )
    assert resp.status_code == 403, resp.text

    # Authenticated but no CSRF token: 403.
    login(client, "m7-a@example.com")
    resp = client.post(
        f"/api/workspaces/{ws}/settlement-batches",
        json={"merchant_courier_account_id": account},
    )
    assert resp.status_code == 403, resp.text
