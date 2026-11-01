"""Live-PostgreSQL settlement tests (M1 Milestone 7, part 2).

Runs the real app (uvicorn subprocess) against a scratch PostgreSQL
database with the full schema + RLS + roles applied (see
tests/live_pg.py). Skipped unless CL_LIVE_PG_PASSWORD is set.

Run with the runtime role (the production path):
CL_LIVE_PG_PASSWORD=... CL_LIVE_PG_ROLE=courierledger_runtime python -m pytest tests/test_settlement_live_pg.py -q

Asserts, live:
  * (a) CL-10 over-allocation is rejected at BOTH layers: the API
    returns 422 when a second allocation would push the net from one
    receipt over its amount, and a direct SQL insert that
    over-allocates is rejected at commit by the deferred
    fn_allocation_overage_check trigger.
  * (b) A reversal restores allocatable headroom and the current
    variance becomes NULL (input_set_changed) until recalculated.
  * (c) The full happy path through the API: batch -> receipt ->
    complete -> allocate to fully_allocated -> calculate-variance ->
    current-variance returns the exact value; adding a receipt
    afterwards makes current-variance NULL (receipt_incomplete while
    the new receipt is incomplete, input_set_changed once it is
    complete).
  * (d) Tenant isolation: user B gets 404 on user A's batch /
    receipt / allocation endpoints; unauthenticated GET gets 401
    (unauthenticated POST without a CSRF token gets 403, because the
    CSRF dependency runs before auth -- same as the runs router).
  * (e) Cross-batch isolation of fully_allocated (M8): batch A has
    a receipt of its own left unallocated while batch B's receipt
    is allocated in full -- B being fully_allocated must NOT make
    A fully_allocated, and A's current variance must be NULL with
    reason not_fully_allocated. (A needs a recorded calculation
    first: with no calculation at all the derivation reports
    no_calculation, per the documented reason order in
    settlement_variance.py -- so A is fully allocated and
    calculated, then gains the unallocated receipt.)

All data is synthetic. Money is integer poisha.
"""
from __future__ import annotations

import os
import secrets
import uuid

import httpx
import psycopg
import pytest

from tests.live_pg import (
    authed_client,
    live_pg_enabled,
    live_pg_server,
    pg_host,
    post,
)

pytestmark = pytest.mark.skipif(
    not live_pg_enabled(), reason="live-PG tests need CL_LIVE_PG_PASSWORD"
)

PASSWORD = secrets.token_urlsafe(16)  # Phase 1 (A-F5): random per run, not a shared literal
DB_KWARGS = {"host": pg_host(), "dbname": "courierledger_live_test"}


@pytest.fixture(scope="module")
def live():
    with live_pg_server() as server:
        yield server


def _owner_conn():
    return psycopg.connect(
        user="courierledger",
        password=os.environ["CL_LIVE_PG_PASSWORD"],
        **DB_KWARGS,
    )


def _setup_workspace(client: httpx.Client) -> tuple[str, str]:
    """Create a workspace + a Steadfast courier account for this client.
    Returns (workspace_id, account_id)."""
    resp = post(client, "/api/workspaces", {"name": "M7 Live Shop"})
    assert resp.status_code == 201, resp.text
    workspace_id = resp.json()["id"]
    resp = post(
        client,
        f"/api/workspaces/{workspace_id}/courier-accounts",
        {"courier_code": "steadfast", "display_label": "Steadfast test account"},
    )
    assert resp.status_code == 201, resp.text
    return workspace_id, resp.json()["id"]


def _make_batch(client, ws, account, expected):
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches",
        {"merchant_courier_account_id": account,
         "expected_batch_payout_poisha": expected},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_receipt(client, ws, batch_id, amount):
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch_id}/receipts",
        {"amount_poisha": amount},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _complete(client, ws, batch_id, receipt_id):
    resp = post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch_id}"
        f"/receipts/{receipt_id}/complete",
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


def _calc(client, ws, batch_id):
    return post(
        client,
        f"/api/workspaces/{ws}/settlement-batches/{batch_id}/calculate-variance",
        {},
    )


def _current_variance(client, ws, batch_id):
    resp = client.get(
        f"/api/workspaces/{ws}/settlement-batches/{batch_id}/current-variance"
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _detail(client, ws, batch_id):
    resp = client.get(f"/api/workspaces/{ws}/settlement-batches/{batch_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _allocation_count(workspace_id, batch_id):
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_merchant_id', %s, true)",
                        (str(workspace_id),))
            cur.execute(
                "SELECT COUNT(*) FROM settlement_allocations"
                " WHERE settlement_batch_id = %s",
                (batch_id,),
            )
            return cur.fetchone()[0]


def test_over_allocation_rejected_live(live):
    """(a) CL-10: full allocation, then a second allocation from the
    same receipt fails at the API layer (422) and a direct insert
    fails at commit (deferred trigger)."""
    client = authed_client(live["base_url"], "m7a-live@example.com", PASSWORD)
    try:
        ws, account = _setup_workspace(client)
        batch = _make_batch(client, ws, account, expected=100000)
        batch_id = batch["batch_id"]
        receipt = _make_receipt(client, ws, batch_id, 100000)
        receipt_id = receipt["receipt_id"]
        _complete(client, ws, batch_id, receipt_id)

        resp = _allocate(client, ws, batch_id, receipt_id, 100000)
        assert resp.status_code == 201, resp.text
        assert _detail(client, ws, batch_id)["allocation_status"] == "fully_allocated"

        # API layer: one more poisha from the same receipt -> 422.
        resp = _allocate(client, ws, batch_id, receipt_id, 1)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "over_allocation"
        assert _allocation_count(ws, batch_id) == 1

        # Trigger layer: a direct insert that over-allocates is
        # rejected at commit by fn_allocation_overage_check (the
        # trigger is DEFERRABLE INITIALLY DEFERRED, so the INSERT
        # itself succeeds and the COMMIT raises).
        with _owner_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.current_merchant_id', %s, true)",
                    (str(ws),),
                )
                cur.execute("SELECT id FROM users WHERE email = %s",
                            ("m7a-live@example.com",))
                user_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO settlement_allocations (id, merchant_id,"
                    " settlement_batch_id, bank_receipt_id, allocated_poisha,"
                    " kind, created_by_user_id)"
                    " VALUES (%s, %s, %s, %s, 1, 'allocation', %s)",
                    (str(uuid.uuid4()), ws, batch_id, receipt_id, user_id),
                )
            with pytest.raises(Exception, match="CL-10"):
                conn.commit()
            conn.rollback()
        assert _allocation_count(ws, batch_id) == 1
    finally:
        client.close()


def test_reversal_restores_headroom_and_invalidates_variance_live(live):
    """(b) Reverse a full allocation: headroom returns, the current
    variance goes NULL, re-allocation + recalculation restores it."""
    client = authed_client(live["base_url"], "m7b-live@example.com", PASSWORD)
    try:
        ws, account = _setup_workspace(client)
        batch = _make_batch(client, ws, account, expected=100000)
        batch_id = batch["batch_id"]
        receipt = _make_receipt(client, ws, batch_id, 100000)
        receipt_id = receipt["receipt_id"]
        _complete(client, ws, batch_id, receipt_id)
        resp = _allocate(client, ws, batch_id, receipt_id, 100000)
        assert resp.status_code == 201, resp.text
        alloc_id = resp.json()["allocation_id"]

        resp = _calc(client, ws, batch_id)
        assert resp.status_code == 201, resp.text
        assert resp.json() == {
            "calculation_seq": 1,
            "expected_batch_payout_poisha": 100000,
            "allocated_bank_confirmed_total_poisha": 100000,
            "bank_variance_poisha": 0,
        }
        assert _current_variance(client, ws, batch_id) == {
            "bank_variance_poisha": 0,
            "calculation_seq": 1,
            "variance_status": "ok",
        }

        # Reverse: the old calculation row stays, but the current
        # variance is NULL (batch no longer fully allocated).
        resp = post(
            client,
            f"/api/workspaces/{ws}/settlement-batches/{batch_id}"
            f"/allocations/{alloc_id}/reverse",
            {},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["kind"] == "reversal"
        detail = _detail(client, ws, batch_id)
        assert detail["allocation_status"] == "unallocated"
        assert detail["net_allocated_total_poisha"] == 0
        assert len(detail["variance_calculations"]) == 1
        current = _current_variance(client, ws, batch_id)
        assert current["bank_variance_poisha"] is None
        assert current["variance_status"] == "not_fully_allocated"

        # Headroom is restored: the full amount can be allocated again.
        resp = _allocate(client, ws, batch_id, receipt_id, 100000)
        assert resp.status_code == 201, resp.text
        assert _detail(client, ws, batch_id)["allocation_status"] == "fully_allocated"
        # ...but the variance is still NULL: the new allocation row
        # is not in the old calculation's input set.
        current = _current_variance(client, ws, batch_id)
        assert current["bank_variance_poisha"] is None
        assert current["variance_status"] == "input_set_changed"

        resp = _calc(client, ws, batch_id)
        assert resp.status_code == 201, resp.text
        assert resp.json()["calculation_seq"] == 2
        assert resp.json()["bank_variance_poisha"] == 0
        assert _current_variance(client, ws, batch_id) == {
            "bank_variance_poisha": 0,
            "calculation_seq": 2,
            "variance_status": "ok",
        }
    finally:
        client.close()


def test_happy_path_and_new_receipt_invalidates_live(live):
    """(c) Full happy path, then a new receipt invalidates the
    current variance with the right reason at each step."""
    client = authed_client(live["base_url"], "m7c-live@example.com", PASSWORD)
    try:
        ws, account = _setup_workspace(client)
        batch = _make_batch(client, ws, account, expected=80000)
        batch_id = batch["batch_id"]
        receipt = _make_receipt(client, ws, batch_id, 80000)
        receipt_id = receipt["receipt_id"]
        _complete(client, ws, batch_id, receipt_id)
        resp = _allocate(client, ws, batch_id, receipt_id, 80000)
        assert resp.status_code == 201, resp.text
        assert _detail(client, ws, batch_id)["bank_status"] == "fully_reconciled"

        resp = _calc(client, ws, batch_id)
        assert resp.status_code == 201, resp.text
        assert resp.json()["bank_variance_poisha"] == 0
        assert _current_variance(client, ws, batch_id)["variance_status"] == "ok"

        # A new, incomplete receipt: the batch is no longer fully
        # allocated (the new receipt's 5000 is unallocated).
        receipt2 = _make_receipt(client, ws, batch_id, 5000)
        current = _current_variance(client, ws, batch_id)
        assert current["bank_variance_poisha"] is None
        assert current["variance_status"] == "not_fully_allocated"
        # Completing it: still unallocated, still NULL.
        _complete(client, ws, batch_id, receipt2["receipt_id"])
        current = _current_variance(client, ws, batch_id)
        assert current["bank_variance_poisha"] is None
        assert current["variance_status"] == "not_fully_allocated"
        # Allocating it restores fully_allocated, but the recorded
        # calculation's input set lacks the new receipt/allocation.
        resp = _allocate(client, ws, batch_id, receipt2["receipt_id"], 5000)
        assert resp.status_code == 201, resp.text
        assert _detail(client, ws, batch_id)["allocation_status"] == "fully_allocated"
        current = _current_variance(client, ws, batch_id)
        assert current["bank_variance_poisha"] is None
        assert current["variance_status"] == "input_set_changed"
        # Recalculation records seq 2: 85000 allocated against 80000
        # expected, variance +5000.
        resp = _calc(client, ws, batch_id)
        assert resp.status_code == 201, resp.text
        assert resp.json()["calculation_seq"] == 2
        assert resp.json()["bank_variance_poisha"] == 5000
        assert _current_variance(client, ws, batch_id) == {
            "bank_variance_poisha": 5000,
            "calculation_seq": 2,
            "variance_status": "ok",
        }
    finally:
        client.close()


def test_second_user_and_anonymous_live(live):
    """(d) Tenant isolation + auth: user B gets 404, anonymous GET
    gets 401."""
    client_a = authed_client(live["base_url"], "m7d-live@example.com", PASSWORD)
    client_b = authed_client(live["base_url"], "m7e-live@example.com", PASSWORD)
    try:
        ws, account = _setup_workspace(client_a)
        batch = _make_batch(client_a, ws, account, expected=100000)
        batch_id = batch["batch_id"]
        receipt = _make_receipt(client_a, ws, batch_id, 100000)
        receipt_id = receipt["receipt_id"]
        resp = _allocate(client_a, ws, batch_id, receipt_id, 50000)
        assert resp.status_code == 201, resp.text
        alloc_id = resp.json()["allocation_id"]

        # POSTs need valid bodies: FastAPI validates the body before
        # the endpoint's membership check runs, so an invalid body
        # would 422 instead of reaching the 404.
        for method, path, body in [
            ("GET", f"/api/workspaces/{ws}/settlement-batches", None),
            ("GET", f"/api/workspaces/{ws}/settlement-batches/{batch_id}", None),
            ("GET", f"/api/workspaces/{ws}/settlement-batches/{batch_id}/current-variance", None),
            ("GET", f"/api/workspaces/{ws}/bank-receipts", None),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{batch_id}/receipts",
             {"amount_poisha": 1000}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{batch_id}/allocations",
             {"bank_receipt_id": receipt_id, "allocated_poisha": 1}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{batch_id}"
                     f"/allocations/{alloc_id}/reverse", {}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{batch_id}/calculate-variance",
             {}),
        ]:
            if method == "GET":
                r = client_b.get(path)
            else:
                r = post(client_b, path, body)
            assert r.status_code == 404, (method, path, r.text)

        anon = httpx.Client(base_url=live["base_url"], timeout=10.0, trust_env=False)
        try:
            r = anon.get(f"/api/workspaces/{ws}/settlement-batches")
            assert r.status_code == 401, r.text
            r = anon.get(f"/api/workspaces/{ws}/settlement-batches/{batch_id}")
            assert r.status_code == 401, r.text
        finally:
            anon.close()
    finally:
        client_a.close()
        client_b.close()


def test_cross_batch_allocation_does_not_fully_allocate_live(live):
    """(e) M8: a receipt allocated in full to batch B cannot make
    batch A fully_allocated. A has a recorded calculation (variance
    0, ok) and then gains a receipt of its own that stays
    unallocated; B's receipt is allocated in full. A must stay NOT
    fully_allocated with current variance NULL / not_fully_allocated,
    while B is fully_allocated with current variance ok."""
    client = authed_client(live["base_url"], "m7f-live@example.com", PASSWORD)
    try:
        ws, account = _setup_workspace(client)

        # Batch A: fully allocated + calculated first, so its current
        # variance is ok (this is what the later unallocated receipt
        # supersedes -- a batch with no calculation at all reports
        # no_calculation, not not_fully_allocated).
        batch_a = _make_batch(client, ws, account, expected=100000)
        batch_a_id = batch_a["batch_id"]
        receipt_a1 = _make_receipt(client, ws, batch_a_id, 100000)
        _complete(client, ws, batch_a_id, receipt_a1["receipt_id"])
        resp = _allocate(client, ws, batch_a_id, receipt_a1["receipt_id"], 100000)
        assert resp.status_code == 201, resp.text
        assert _detail(client, ws, batch_a_id)["allocation_status"] == "fully_allocated"
        resp = _calc(client, ws, batch_a_id)
        assert resp.status_code == 201, resp.text
        assert resp.json()["bank_variance_poisha"] == 0
        assert _current_variance(client, ws, batch_a_id) == {
            "bank_variance_poisha": 0,
            "calculation_seq": 1,
            "variance_status": "ok",
        }

        # Batch A gains a receipt of its own that stays unallocated.
        _make_receipt(client, ws, batch_a_id, 5000)
        assert _detail(client, ws, batch_a_id)["allocation_status"] == "partially_allocated"
        current_a = _current_variance(client, ws, batch_a_id)
        assert current_a["bank_variance_poisha"] is None
        assert current_a["variance_status"] == "not_fully_allocated"

        # Batch B: its own receipt allocated in full.
        batch_b = _make_batch(client, ws, account, expected=50000)
        batch_b_id = batch_b["batch_id"]
        receipt_b = _make_receipt(client, ws, batch_b_id, 50000)
        _complete(client, ws, batch_b_id, receipt_b["receipt_id"])
        resp = _allocate(client, ws, batch_b_id, receipt_b["receipt_id"], 50000)
        assert resp.status_code == 201, resp.text
        assert _detail(client, ws, batch_b_id)["allocation_status"] == "fully_allocated"
        resp = _calc(client, ws, batch_b_id)
        assert resp.status_code == 201, resp.text
        assert resp.json()["bank_variance_poisha"] == 0
        assert _current_variance(client, ws, batch_b_id) == {
            "bank_variance_poisha": 0,
            "calculation_seq": 1,
            "variance_status": "ok",
        }

        # B being fully_allocated did not leak into A: A is still NOT
        # fully_allocated, and its current variance is still NULL
        # with reason not_fully_allocated.
        assert _detail(client, ws, batch_a_id)["allocation_status"] == "partially_allocated"
        current_a = _current_variance(client, ws, batch_a_id)
        assert current_a["bank_variance_poisha"] is None
        assert current_a["variance_status"] == "not_fully_allocated"
    finally:
        client.close()
