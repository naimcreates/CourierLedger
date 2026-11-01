"""Live-PostgreSQL orders + shipments tests (M1 Milestone 8, part 1).

Runs the real app (uvicorn subprocess) against a scratch PostgreSQL
database with the full schema + RLS + roles applied (see
tests/live_pg.py). Skipped unless CL_LIVE_PG_PASSWORD is set.

Run with the owner role (default) and with the runtime role:
CL_LIVE_PG_PASSWORD=<your-local-db-password>
CL_LIVE_PG_PASSWORD=<your-local-db-password>

Asserts, live:
  * An API-created order + shipment (TRK-1001 / ORD-1001) strong-
    matches row 1 of the synthetic generic statement fixture at
    import time, and a run created from that import contains exactly
    one result -- for the API-created shipment, with expected
    collectible 100000.
  * A second active COD-bearing shipment for one order is 422, while
    a second non-COD shipment and a superseded COD-bearing shipment
    are allowed (the partial unique index applies on PostgreSQL;
    SQLite cannot express it -- see tests/test_shipments.py).
  * A second user gets 404 on another user's orders / shipments.

NOTE: these tests need the shipments router wired in app/main.py
(one line -- see backend/README.md "Milestone 8" and the
coordinator handoff); without it the order/shipment endpoints 404.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import httpx
import pytest

from tests.live_pg import (
    authed_client,
    get_csrf,
    live_pg_enabled,
    live_pg_server,
    post,
)

pytestmark = pytest.mark.skipif(
    not live_pg_enabled(), reason="live-PG tests need CL_LIVE_PG_PASSWORD"
)

PASSWORD = secrets.token_urlsafe(16)  # Phase 1 (A-F5): random per run, not a shared literal
FIXTURE = Path(__file__).parent / "data" / "generic_statement_fixture.csv"
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


@pytest.fixture(scope="module")
def live():
    with live_pg_server() as server:
        yield server


def _setup_workspace(client: httpx.Client) -> tuple[str, str]:
    """Create a workspace + a Steadfast courier account for this client.
    Returns (workspace_id, account_id)."""
    resp = post(client, "/api/workspaces", {"name": "M8 Live Shop"})
    assert resp.status_code == 201, resp.text
    workspace_id = resp.json()["id"]
    resp = post(
        client,
        f"/api/workspaces/{workspace_id}/courier-accounts",
        {"courier_code": "steadfast", "display_label": "Steadfast test account"},
    )
    assert resp.status_code == 201, resp.text
    return workspace_id, resp.json()["id"]


def _order(client, ws, ref, cod):
    resp = post(client, f"/api/workspaces/{ws}/orders",
                {"merchant_order_ref": ref, "order_cod_poisha": cod})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _shipment(client, ws, order_id, **body):
    payload = {"outcome": "delivered", "is_cod_bearing": True}
    payload.update(body)
    return post(client, f"/api/workspaces/{ws}/orders/{order_id}/shipments", payload)


def _upload(client, ws, account_id, key):
    headers = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": key}
    return client.post(
        f"/api/workspaces/{ws}/imports",
        data={
            "mapping": json.dumps(MAPPING),
            "merchant_courier_account_id": account_id,
        },
        files={"file": ("statement.csv", FIXTURE.read_bytes(), "text/csv")},
        headers=headers,
    )


def test_api_created_shipment_reconciles_through_import_run(live):
    """End-to-end proof: order + shipment created via the API,
    fixture uploaded, run created from the import -> the run contains
    a result for the API-created shipment."""
    client = authed_client(live["base_url"], "m8-live-a@example.com", PASSWORD)
    try:
        ws, account_id = _setup_workspace(client)
        order = _order(client, ws, "ORD-1001", 100000)
        resp = _shipment(client, ws, order["order_id"],
                         tracking_id="TRK-1001", outcome="delivered",
                         outcome_source="merchant_confirmed")
        assert resp.status_code == 201, resp.text
        shipment = resp.json()
        assert shipment["expected_collectible_cod_poisha"] == 100000
        assert shipment["needs_review"] is False

        up = _upload(client, ws, account_id, "m8-live-1")
        assert up.status_code == 201, up.text
        summary = up.json()
        assert summary["status"] == "parsed"
        assert summary["row_count"] == 12
        batch_id = summary["batch_id"]

        detail = client.get(f"/api/workspaces/{ws}/imports/{batch_id}")
        assert detail.status_code == 200, detail.text
        by_row = {r["row_number"]: r for r in detail.json()["rows"]}
        # Row 1 (TRK-1001) strong-matched the API-created shipment.
        assert by_row[1]["statement_line"]["match_status"] == "strong_matched"
        assert by_row[1]["statement_line"]["shipment_id"] == shipment["shipment_id"]

        run_resp = post(client, f"/api/workspaces/{ws}/imports/{batch_id}/runs", {})
        assert run_resp.status_code == 201, run_resp.text
        run = run_resp.json()
        assert run["status"] == "completed"
        assert run["result_counts"]["needs_review"] == 1
        assert sum(run["result_counts"].values()) == 1

        results = client.get(
            f"/api/workspaces/{ws}/runs/{run['run_id']}/results")
        assert results.status_code == 200, results.text
        body = results.json()
        assert body["total"] == 1
        result = body["results"][0]
        assert result["shipment_id"] == shipment["shipment_id"]
        assert result["expected_collectible_poisha"] == 100000
        assert result["reported_collected_poisha"] == 100000
    finally:
        client.close()


def test_second_cod_bearing_422_but_non_cod_and_superseded_allowed(live):
    """PostgreSQL partial-index behaviour the SQLite fallback cannot
    express: a second ACTIVE COD-bearing shipment is 422, while a
    second non-COD shipment and a superseded COD-bearing shipment
    (recorded after its successor exists) are allowed."""
    client = authed_client(live["base_url"], "m8-live-b@example.com", PASSWORD)
    try:
        ws, _account_id = _setup_workspace(client)
        order = _order(client, ws, "ORD-1", 100000)
        first = _shipment(client, ws, order["order_id"], tracking_id="TRK-A")
        assert first.status_code == 201, first.text

        second_cod = _shipment(client, ws, order["order_id"], tracking_id="TRK-B")
        assert second_cod.status_code == 422, second_cod.text
        assert second_cod.json()["detail"]["reason"] == "second_cod_bearing_shipment"

        # A second non-COD shipment for the same order is allowed.
        non_cod = _shipment(client, ws, order["order_id"], tracking_id="TRK-C",
                            outcome="returned", is_cod_bearing=False)
        assert non_cod.status_code == 201, non_cod.text
        assert non_cod.json()["expected_collectible_cod_poisha"] == 0

        # A superseded COD-bearing shipment recorded after its
        # successor exists is allowed and collects verified 0.
        superseded = _shipment(
            client, ws, order["order_id"], tracking_id="TRK-D",
            outcome="delivered", is_cod_bearing=True, is_superseded=True,
            superseded_by_shipment_id=first.json()["shipment_id"],
            superseded_reason="re-shipped as TRK-A",
        )
        assert superseded.status_code == 201, superseded.text
        assert superseded.json()["expected_collectible_cod_poisha"] == 0
        assert superseded.json()["needs_review"] is False

        # The active COD-bearing shipment still blocks a new one.
        third_cod = _shipment(client, ws, order["order_id"], tracking_id="TRK-E")
        assert third_cod.status_code == 422, third_cod.text
    finally:
        client.close()


def test_second_user_cannot_see_orders_or_shipments(live):
    client_a = authed_client(live["base_url"], "m8-live-c@example.com", PASSWORD)
    client_b = authed_client(live["base_url"], "m8-live-d@example.com", PASSWORD)
    try:
        ws, _account_id = _setup_workspace(client_a)
        order = _order(client_a, ws, "ORD-1", 100000)
        shipment = _shipment(client_a, ws, order["order_id"], tracking_id="TRK-1")
        assert shipment.status_code == 201, shipment.text
        sid = shipment.json()["shipment_id"]
        for method, path in [
            ("GET", f"/api/workspaces/{ws}/orders"),
            ("GET", f"/api/workspaces/{ws}/orders/{order['order_id']}"),
            ("GET", f"/api/workspaces/{ws}/shipments"),
            ("GET", f"/api/workspaces/{ws}/shipments/{sid}"),
        ]:
            r = client_b.request(method, path)
            assert r.status_code == 404, (method, path, r.text)
        r = _shipment(client_b, ws, order["order_id"], tracking_id="TRK-2")
        assert r.status_code == 404, r.text
    finally:
        client_a.close()
        client_b.close()


def test_unauthenticated_orders_and_shipments_401(live):
    client = authed_client(live["base_url"], "m8-live-e@example.com", PASSWORD)
    anon = httpx.Client(base_url=live["base_url"], timeout=10.0, trust_env=False)
    try:
        ws, _account_id = _setup_workspace(client)
        order = _order(client, ws, "ORD-1", 100000)
        assert anon.get(f"/api/workspaces/{ws}/orders").status_code == 401
        assert anon.get(f"/api/workspaces/{ws}/shipments").status_code == 401
        resp = anon.post(
            f"/api/workspaces/{ws}/orders/{order['order_id']}/shipments",
            json={"outcome": "delivered"},
            headers={"X-CSRF-Token": get_csrf(anon)},
        )
        assert resp.status_code == 401, resp.text
    finally:
        client.close()
        anon.close()
