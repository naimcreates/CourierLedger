"""Orders + shipments creation API tests (M1 Milestone 8, part 1) --
SQLite fallback suite.

Covers: create/list/get orders and shipments, strict integer-poisha
validation (floats / strings / bools are 422), the V2.1
outcome -> expected-collectible derivation (including NULL vs
verified 0 and the split-amount quarantine flag), the MVP one
COD-bearing shipment per order rule (422), tracking normalisation,
cross-workspace 404s, unauthenticated 401s and missing-CSRF 403s.

Live-PostgreSQL coverage (RLS + runtime role + the import -> run
proof that an API-created shipment strong-matches) lives in
tests/test_shipments_live_pg.py.

NOTE: app/main.py is wired by the coordinator, so these tests mount
the shipments router themselves (guarded: skipped when the route
already exists). The live tests cannot do that (uvicorn subprocess)
and need the wiring in place.

SQLite limitation (test-only fallback, NOT a production rule): the
partial unique index uq_shipments_one_active_cod_bearing_per_order
uses postgresql_where, which SQLite ignores, so on SQLite it is a
plain UNIQUE index on (merchant_id, order_id) and ANY second
shipment for one order fails at the database. The cases that need
two shipments for one order -- a second non-COD shipment, and a
superseded shipment naming a successor of the same order -- are
therefore covered only in the live-PG suite, where the partial
index applies. The pure derivation for superseded (verified 0) is
unit-tested here via derive_expected_collectible.
"""

import pytest

from app.api import shipments as shipments_api
from app.main import app
from tests.conftest import authed_client, get_csrf, post
from app.services.shipment_service import derive_expected_collectible

# app/main.py is wired by the coordinator, so these tests mount the
# shipments router themselves (guarded: skipped when the route
# already exists, e.g. after the coordinator wires it).
if not any(
    getattr(r, "path", "") == "/api/workspaces/{workspace_id}/orders"
    for r in app.routes
):
    app.include_router(shipments_api.router, prefix="/api")


def _workspace(client, name="M8 Test Shop"):
    resp = post(client, "/api/workspaces", {"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _order(client, ws, ref="ORD-1", cod=100000):
    resp = post(client, f"/api/workspaces/{ws}/orders",
                {"merchant_order_ref": ref, "order_cod_poisha": cod})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _shipment(client, ws, order_id, **body):
    payload = {"outcome": "delivered", "is_cod_bearing": True}
    payload.update(body)
    return post(client, f"/api/workspaces/{ws}/orders/{order_id}/shipments", payload)


# ---------------------------------------------------------------------------
# Derivation (pure function)
# ---------------------------------------------------------------------------

def test_derive_expected_collectible_matrix():
    # delivered: order COD when no amount stated; stated amount wins.
    assert derive_expected_collectible(
        outcome="delivered", order_cod_poisha=100000,
        amount_to_collect_poisha=None, partial_amount_evidence_ref=None,
        is_superseded=False) == (100000, False, None)
    assert derive_expected_collectible(
        outcome="delivered", order_cod_poisha=100000,
        amount_to_collect_poisha=100000, partial_amount_evidence_ref=None,
        is_superseded=False) == (100000, False, None)
    # delivered: split amount is recorded, flagged for review.
    assert derive_expected_collectible(
        outcome="delivered", order_cod_poisha=100000,
        amount_to_collect_poisha=60000, partial_amount_evidence_ref=None,
        is_superseded=False) == (60000, True, "split_amount_differs_from_order_total")
    # delivered: nothing known -> NULL, never an invented amount.
    assert derive_expected_collectible(
        outcome="delivered", order_cod_poisha=None,
        amount_to_collect_poisha=None, partial_amount_evidence_ref=None,
        is_superseded=False) == (None, True, "order_cod_unknown")
    # partial: documented only, else NULL (never the full order COD).
    assert derive_expected_collectible(
        outcome="partially_delivered", order_cod_poisha=100000,
        amount_to_collect_poisha=40000, partial_amount_evidence_ref="photo-1",
        is_superseded=False) == (40000, False, None)
    assert derive_expected_collectible(
        outcome="partially_delivered", order_cod_poisha=100000,
        amount_to_collect_poisha=40000, partial_amount_evidence_ref=None,
        is_superseded=False) == (None, True, "partial_amount_not_documented")
    assert derive_expected_collectible(
        outcome="partially_delivered", order_cod_poisha=100000,
        amount_to_collect_poisha=None, partial_amount_evidence_ref="photo-1",
        is_superseded=False) == (None, True, "partial_amount_not_documented")
    # returned / cancelled / superseded: verified 0.
    for outcome in ("returned", "cancelled"):
        assert derive_expected_collectible(
            outcome=outcome, order_cod_poisha=100000,
            amount_to_collect_poisha=None, partial_amount_evidence_ref=None,
            is_superseded=False) == (0, False, None)
    assert derive_expected_collectible(
        outcome="delivered", order_cod_poisha=100000,
        amount_to_collect_poisha=None, partial_amount_evidence_ref=None,
        is_superseded=True) == (0, False, None)
    # unknown / in_transit: NULL, flagged.
    for outcome in ("unknown", "in_transit"):
        assert derive_expected_collectible(
            outcome=outcome, order_cod_poisha=100000,
            amount_to_collect_poisha=None, partial_amount_evidence_ref=None,
            is_superseded=False) == (None, True, "outcome_not_confirmed")


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def test_create_order_201(client):
    authed_client(client, "m8a@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1001", cod=100000)
    assert order["merchant_order_ref"] == "ORD-1001"
    assert order["order_cod_poisha"] == 100000
    assert order["currency"] == "BDT"
    assert order["source"] == "manual"
    assert order["merchant_id"] == ws
    assert order["order_id"]


def test_create_order_null_cod_allowed(client):
    authed_client(client, "m8b@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-NULL", cod=None)
    assert order["order_cod_poisha"] is None


def test_create_order_validation_422(client):
    authed_client(client, "m8c@example.com")
    ws = _workspace(client)
    base = f"/api/workspaces/{ws}/orders"
    bad_payloads = [
        {"merchant_order_ref": "ORD-F", "order_cod_poisha": 100000.5},  # float
        {"merchant_order_ref": "ORD-S", "order_cod_poisha": "100000"},  # string
        {"merchant_order_ref": "ORD-B", "order_cod_poisha": True},  # bool
        {"merchant_order_ref": "ORD-N", "order_cod_poisha": -1},  # negative
        {"merchant_order_ref": "   "},  # blank ref
        {"merchant_order_ref": "ORD-M", "merchant_id": ws},  # never trusted
        {"merchant_order_ref": "ORD-U", "currency": "USD"},  # BDT only
        {"merchant_order_ref": "ORD-X", "expected_collectible_cod_poisha": 100},
        {"merchant_order_ref": "ORD-SRC", "source": "csv_import"},
    ]
    for payload in bad_payloads:
        resp = post(client, base, payload)
        assert resp.status_code == 422, (payload, resp.text)


def test_list_and_get_orders(client):
    authed_client(client, "m8d@example.com")
    ws = _workspace(client)
    o1 = _order(client, ws, ref="ORD-A", cod=100000)
    o2 = _order(client, ws, ref="ORD-B", cod=50000)
    resp = client.get(f"/api/workspaces/{ws}/orders")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert {o["order_id"] for o in body["orders"]} == {o1["order_id"], o2["order_id"]}
    page = client.get(f"/api/workspaces/{ws}/orders?limit=1&offset=0")
    assert page.json()["total"] == 2
    assert len(page.json()["orders"]) == 1
    got = client.get(f"/api/workspaces/{ws}/orders/{o1['order_id']}")
    assert got.status_code == 200, got.text
    assert got.json()["merchant_order_ref"] == "ORD-A"
    assert client.get(f"/api/workspaces/{ws}/orders/00000000-0000-0000-0000-000000000000").status_code == 404


def test_order_cross_workspace_404_and_unauthenticated_401(client):
    authed_client(client, "m8e@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1", cod=100000)
    other = authed_client(client, "m8f@example.com")
    assert other.get(f"/api/workspaces/{ws}/orders").status_code == 404
    assert other.get(f"/api/workspaces/{ws}/orders/{order['order_id']}").status_code == 404
    # Fresh unauthenticated client for the 401 checks (the shared
    # fixture client now carries the second user's session).
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    assert anon.get(f"/api/workspaces/{ws}/orders").status_code == 401
    resp = anon.post(f"/api/workspaces/{ws}/orders",
                     json={"merchant_order_ref": "ORD-9"},
                     headers={"X-CSRF-Token": get_csrf(anon)})
    assert resp.status_code == 401, resp.text


def test_order_post_without_csrf_403(client):
    authed_client(client, "m8g@example.com")
    ws = _workspace(client)
    resp = client.post(f"/api/workspaces/{ws}/orders",
                       json={"merchant_order_ref": "ORD-1"})
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Shipments
# ---------------------------------------------------------------------------

def test_create_shipment_delivered_derives_order_cod(client):
    authed_client(client, "m8h@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1", cod=100000)
    resp = _shipment(client, ws, order["order_id"],
                     tracking_id=" trk-1001 ", outcome="delivered",
                     outcome_source="merchant_confirmed")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tracking_id"] == "trk-1001"
    assert body["tracking_id_normalised"] == "TRK-1001"
    assert body["outcome"] == "delivered"
    assert body["expected_collectible_cod_poisha"] == 100000
    assert body["expected_collectible_derived_poisha"] == 100000
    assert body["needs_review"] is False
    assert body["needs_review_reason"] is None
    assert body["merchant_id"] == ws
    assert body["order_id"] == order["order_id"]


def test_create_shipment_split_amount_flagged_for_review(client):
    authed_client(client, "m8i@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1", cod=100000)
    resp = _shipment(client, ws, order["order_id"],
                     tracking_id="TRK-SPLIT", outcome="delivered",
                     amount_to_collect_poisha=60000)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["expected_collectible_cod_poisha"] == 60000
    assert body["needs_review"] is True
    assert body["needs_review_reason"] == "split_amount_differs_from_order_total"


def test_create_shipment_delivered_order_cod_null(client):
    authed_client(client, "m8j@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1", cod=None)
    resp = _shipment(client, ws, order["order_id"], tracking_id="TRK-NULL")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["expected_collectible_cod_poisha"] is None
    assert body["needs_review"] is True
    assert body["needs_review_reason"] == "order_cod_unknown"


def test_create_shipment_partial_documented_and_undocumented(client):
    authed_client(client, "m8k@example.com")
    ws = _workspace(client)
    o1 = _order(client, ws, ref="ORD-P1", cod=100000)
    resp = _shipment(client, ws, o1["order_id"], tracking_id="TRK-P1",
                     outcome="partially_delivered",
                     amount_to_collect_poisha=40000,
                     partial_amount_evidence_ref="photo-1")
    assert resp.status_code == 201, resp.text
    assert resp.json()["expected_collectible_cod_poisha"] == 40000
    assert resp.json()["needs_review"] is False

    o2 = _order(client, ws, ref="ORD-P2", cod=100000)
    resp = _shipment(client, ws, o2["order_id"], tracking_id="TRK-P2",
                     outcome="partially_delivered",
                     amount_to_collect_poisha=40000)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["expected_collectible_cod_poisha"] is None
    assert body["needs_review"] is True
    assert body["needs_review_reason"] == "partial_amount_not_documented"

    o3 = _order(client, ws, ref="ORD-P3", cod=100000)
    resp = _shipment(client, ws, o3["order_id"], tracking_id="TRK-P3",
                     outcome="partially_delivered")
    assert resp.status_code == 201, resp.text
    assert resp.json()["expected_collectible_cod_poisha"] is None
    assert resp.json()["needs_review_reason"] == "partial_amount_not_documented"


def test_create_shipment_returned_cancelled_verified_zero(client):
    authed_client(client, "m8l@example.com")
    ws = _workspace(client)
    for outcome in ("returned", "cancelled"):
        order = _order(client, ws, ref=f"ORD-{outcome}", cod=100000)
        resp = _shipment(client, ws, order["order_id"],
                         tracking_id=f"TRK-{outcome}", outcome=outcome)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["expected_collectible_cod_poisha"] == 0, resp.text
        assert body["needs_review"] is False


def test_create_shipment_unknown_and_in_transit_null(client):
    authed_client(client, "m8m@example.com")
    ws = _workspace(client)
    for outcome in ("unknown", "in_transit"):
        order = _order(client, ws, ref=f"ORD-{outcome}", cod=100000)
        resp = _shipment(client, ws, order["order_id"],
                         tracking_id=f"TRK-{outcome}", outcome=outcome,
                         is_cod_bearing=False)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["expected_collectible_cod_poisha"] is None, resp.text
        assert body["needs_review"] is True
        assert body["needs_review_reason"] == "outcome_not_confirmed"


def test_create_shipment_validation_422(client):
    authed_client(client, "m8n@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1", cod=100000)
    base = f"/api/workspaces/{ws}/orders/{order['order_id']}/shipments"
    bad = [
        {"outcome": "lost"},  # not a known outcome
        {"outcome": "delivered", "outcome_source": "guessed"},
        {"outcome": "delivered", "amount_to_collect_poisha": 10.5},  # float
        {"outcome": "delivered", "amount_to_collect_poisha": "1000"},  # string
        {"outcome": "delivered", "amount_to_collect_poisha": -5},  # negative
        {"outcome": "returned", "amount_to_collect_poisha": 1000},  # contradicts outcome
        {"outcome": "unknown", "amount_to_collect_poisha": 1000},
        {"outcome": "delivered", "is_superseded": True},  # needs successor
        {"outcome": "delivered", "superseded_by_shipment_id": "00000000-0000-0000-0000-000000000000"},
        {"outcome": "delivered", "merchant_id": ws},  # never trusted
        {"outcome": "delivered", "expected_collectible_cod_poisha": 100},
        {"outcome": "delivered", "merchant_courier_account_id": "00000000-0000-0000-0000-000000000000"},
    ]
    for payload in bad:
        resp = post(client, base, payload)
        assert resp.status_code == 422, (payload, resp.text)


def test_second_cod_bearing_shipment_422(client):
    authed_client(client, "m8o@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1", cod=100000)
    first = _shipment(client, ws, order["order_id"], tracking_id="TRK-1")
    assert first.status_code == 201, first.text
    second = _shipment(client, ws, order["order_id"], tracking_id="TRK-2")
    assert second.status_code == 422, second.text
    assert second.json()["detail"]["reason"] == "second_cod_bearing_shipment"


def test_shipment_for_other_workspace_order_404(client):
    authed_client(client, "m8p@example.com")
    ws_a = _workspace(client, "Shop A")
    ws_b = _workspace(client, "Shop B")
    order_a = _order(client, ws_a, ref="ORD-A", cod=100000)
    resp = _shipment(client, ws_b, order_a["order_id"], tracking_id="TRK-X")
    assert resp.status_code == 404, resp.text


def test_list_and_get_shipments_with_filters(client):
    authed_client(client, "m8q@example.com")
    ws = _workspace(client)
    o1 = _order(client, ws, ref="ORD-1", cod=100000)
    o2 = _order(client, ws, ref="ORD-2", cod=50000)
    s1 = _shipment(client, ws, o1["order_id"], tracking_id="TRK-1001")
    assert s1.status_code == 201, s1.text
    s2 = _shipment(client, ws, o2["order_id"], tracking_id="TRK-2002",
                   outcome="returned", is_cod_bearing=False)
    assert s2.status_code == 201, s2.text

    listing = client.get(f"/api/workspaces/{ws}/shipments")
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 2
    by_order = client.get(f"/api/workspaces/{ws}/shipments?order_id={o1['order_id']}")
    assert by_order.json()["total"] == 1
    assert by_order.json()["shipments"][0]["shipment_id"] == s1.json()["shipment_id"]
    by_tracking = client.get("/api/workspaces/%s/shipments?tracking_id= trk-2002 " % ws)
    assert by_tracking.json()["total"] == 1
    assert by_tracking.json()["shipments"][0]["shipment_id"] == s2.json()["shipment_id"]
    assert client.get(f"/api/workspaces/{ws}/shipments?tracking_id=TRK-NOPE").json()["total"] == 0
    got = client.get(f"/api/workspaces/{ws}/shipments/{s1.json()['shipment_id']}")
    assert got.status_code == 200, got.text
    assert got.json()["tracking_id_normalised"] == "TRK-1001"


def test_shipment_cross_workspace_404_and_unauthenticated_401(client):
    authed_client(client, "m8r@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1", cod=100000)
    shipment = _shipment(client, ws, order["order_id"], tracking_id="TRK-1")
    assert shipment.status_code == 201, shipment.text
    sid = shipment.json()["shipment_id"]

    other = authed_client(client, "m8s@example.com")
    assert other.get(f"/api/workspaces/{ws}/shipments").status_code == 404
    assert other.get(f"/api/workspaces/{ws}/shipments/{sid}").status_code == 404

    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    assert anon.get(f"/api/workspaces/{ws}/shipments").status_code == 401
    assert anon.get(f"/api/workspaces/{ws}/shipments/{sid}").status_code == 401
    resp = anon.post(
        f"/api/workspaces/{ws}/orders/{order['order_id']}/shipments",
        json={"outcome": "delivered"},
        headers={"X-CSRF-Token": get_csrf(anon)},
    )
    assert resp.status_code == 401, resp.text


def test_shipment_post_without_csrf_403(client):
    authed_client(client, "m8t@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1", cod=100000)
    resp = client.post(
        f"/api/workspaces/{ws}/orders/{order['order_id']}/shipments",
        json={"outcome": "delivered"},
    )
    assert resp.status_code == 403, resp.text


def test_no_update_or_delete_routes_for_orders_shipments(client):
    authed_client(client, "m8u@example.com")
    ws = _workspace(client)
    order = _order(client, ws, ref="ORD-1", cod=100000)
    shipment = _shipment(client, ws, order["order_id"], tracking_id="TRK-1")
    assert shipment.status_code == 201, shipment.text
    sid = shipment.json()["shipment_id"]
    csrf = {"X-CSRF-Token": get_csrf(client)}
    for method, path in [
        ("PUT", f"/api/workspaces/{ws}/orders/{order['order_id']}"),
        ("PATCH", f"/api/workspaces/{ws}/orders/{order['order_id']}"),
        ("DELETE", f"/api/workspaces/{ws}/orders/{order['order_id']}"),
        ("PUT", f"/api/workspaces/{ws}/shipments/{sid}"),
        ("PATCH", f"/api/workspaces/{ws}/shipments/{sid}"),
        ("DELETE", f"/api/workspaces/{ws}/shipments/{sid}"),
    ]:
        resp = client.request(method, path, json={}, headers=csrf)
        assert resp.status_code == 405, (method, path, resp.status_code)
