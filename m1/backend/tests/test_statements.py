"""Courier-statement line API tests (M1 Milestone 8, part 2) --
SQLite fallback suite. Live-PostgreSQL coverage (RLS + runtime
role) is the coordinator's integration pass; these tests prove the
endpoint contract against real import rows written by the M4
service.

Covers: list (newest first, exact stored values, raw values
verbatim, linked shipment, courier account, import batch ref),
pagination (page/page_size), import_batch_id + match_status
filters, invalid match_status 422, detail 200/404, cross-workspace
404, unauthenticated 401, read-only (no POST/PUT/DELETE routes).

NOTE: app/main.py is wired by the coordinator, so these tests mount
the statements router themselves (guarded: skipped when the route
already exists). All data is synthetic. Money is integer poisha.
"""

import json
import uuid
from pathlib import Path

import pytest

from app.db.models import Courier, Order, Shipment
from app.db.session import SessionLocal
from app.main import app
from app.api import statements as statements_api
from tests.conftest import get_csrf, login, post, register

if not any(
    getattr(r, "path", "") == "/api/workspaces/{workspace_id}/statements"
    for r in app.routes
):
    app.include_router(statements_api.router, prefix="/api")

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


def _setup(client, email="m8-statements@example.com"):
    """Register + log in, create a workspace + Steadfast account."""
    session = SessionLocal()
    try:
        session.add(Courier(code="steadfast", name="Steadfast (synthetic test row)"))
        session.commit()
    finally:
        session.close()
    register(client, email)
    login(client, email)
    resp = post(client, "/api/workspaces", {"name": "M8 Statements Shop"})
    assert resp.status_code == 201, resp.text
    workspace_id = resp.json()["id"]
    resp = post(
        client,
        f"/api/workspaces/{workspace_id}/courier-accounts",
        {"courier_code": "steadfast", "display_label": "Steadfast test account"},
    )
    assert resp.status_code == 201, resp.text
    return workspace_id, resp.json()["id"]


def _seed_shipment(workspace_id, account_id):
    """One delivered COD shipment for TRK-1001 / ORD-1001, so that
    fixture row strong-matches it at import time."""
    session = SessionLocal()
    try:
        order = Order(
            merchant_id=uuid.UUID(workspace_id),
            merchant_order_ref="ORD-1001",
            order_cod_poisha=100000,
        )
        session.add(order)
        session.flush()
        session.add(
            Shipment(
                merchant_id=uuid.UUID(workspace_id),
                order_id=order.id,
                merchant_courier_account_id=uuid.UUID(account_id),
                tracking_id_raw="TRK-1001",
                tracking_id_normalised="TRK-1001",
                outcome="delivered",
                outcome_source="merchant_confirmed",
                expected_collectible_cod_poisha=100000,
                is_cod_bearing=True,
            )
        )
        session.commit()
    finally:
        session.close()


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


@pytest.fixture()
def imported(client):
    ws, account = _setup(client)
    _seed_shipment(ws, account)
    batch = _upload(client, ws, account)
    assert batch["status"] == "parsed"
    assert batch["row_count"] == 12
    assert batch["parsed_row_count"] == 10
    return ws, account, batch


def _list(client, ws, **params):
    resp = client.get(f"/api/workspaces/{ws}/statements", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_list_exact_rows_and_verbatim_values(client, imported):
    ws, _account, batch = imported
    body = _list(client, ws)
    # 10 parsed fixture rows -> 10 statement lines.
    assert body["total"] == 10
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert len(body["statements"]) == 10

    by_tracking = {s["tracking_id_raw"]: s for s in body["statements"]}
    row1001 = by_tracking["TRK-1001"]
    # Raw CSV values verbatim, exactly as stored on the import row.
    assert row1001["raw_values"] == {
        "Tracking ID": "TRK-1001",
        "Order Ref": "ORD-1001",
        "Collected (poisha)": "100000",
        "Payout (poisha)": "92000",
        "Delivery Fee (poisha)": "7000",
        "COD Fee (poisha)": "1000",
        "Return Fee (poisha)": "0",
        "Other Fee (poisha)": "0",
        "Status": "delivered",
        "Statement Date": "2026-10-15",
    }
    assert row1001["parse_status"] == "parsed"
    assert row1001["row_number"] == 1
    assert row1001["match_status"] == "strong_matched"
    assert row1001["reported_collected_poisha"] == 100000
    assert row1001["reported_collected_state"] == "present_valid"
    assert row1001["reported_payout_poisha"] == 92000
    assert row1001["reported_return_fee_poisha"] == 0
    assert row1001["reported_return_fee_state"] == "explicit_zero"
    assert row1001["statement_date"] == "2026-10-15"
    # Linked shipment where matched.
    assert row1001["shipment"] is not None
    assert row1001["shipment"]["tracking_id_normalised"] == "TRK-1001"
    assert row1001["shipment"]["outcome"] == "delivered"
    assert row1001["shipment"]["expected_collectible_cod_poisha"] == 100000
    assert row1001["shipment_id"] == row1001["shipment"]["shipment_id"]
    # Courier account + import batch reference.
    assert row1001["courier_account"]["courier_code"] == "steadfast"
    assert row1001["courier_account"]["display_label"] == "Steadfast test account"
    assert row1001["import_batch"]["batch_id"] == batch["batch_id"]
    assert row1001["import_batch"]["status"] == "parsed"

    # A blank-collected / invalid-payout row keeps its DQ states and
    # NULL values (never an invented zero).
    row1008 = by_tracking["TRK-1008"]
    assert row1008["match_status"] == "unmatched"
    assert row1008["shipment"] is None
    assert row1008["shipment_id"] is None
    assert row1008["reported_collected_poisha"] is None
    assert row1008["reported_collected_state"] == "blank"
    assert row1008["reported_payout_poisha"] is None
    assert row1008["reported_payout_state"] == "invalid"

    # Match-status tallies over the 10 lines: 1 strong, 2 duplicate,
    # 7 unmatched (no other shipments were seeded).
    statuses = [s["match_status"] for s in body["statements"]]
    assert statuses.count("strong_matched") == 1
    assert statuses.count("duplicate") == 2
    assert statuses.count("unmatched") == 7


def test_list_pagination(client, imported):
    ws, _account, _batch = imported
    page1 = _list(client, ws, page=1, page_size=3)
    assert page1["total"] == 10
    assert len(page1["statements"]) == 3
    page2 = _list(client, ws, page=2, page_size=3)
    page4 = _list(client, ws, page=4, page_size=3)
    assert len(page4["statements"]) == 1
    page5 = _list(client, ws, page=5, page_size=3)
    assert page5["statements"] == []
    assert page5["total"] == 10
    ids1 = {s["statement_line_id"] for s in page1["statements"]}
    ids2 = {s["statement_line_id"] for s in page2["statements"]}
    ids4 = {s["statement_line_id"] for s in page4["statements"]}
    assert not (ids1 & ids2)
    assert not (ids1 & ids4)
    all_ids = ids1 | ids2 | ids4 | {
        s["statement_line_id"] for s in _list(client, ws, page=3, page_size=3)["statements"]
    }
    assert len(all_ids) == 10


def test_list_filters(client, imported):
    ws, _account, batch = imported
    by_batch = _list(client, ws, import_batch_id=batch["batch_id"])
    assert by_batch["total"] == 10
    matched = _list(client, ws, match_status="strong_matched")
    assert matched["total"] == 1
    assert matched["statements"][0]["tracking_id_raw"] == "TRK-1001"
    duplicates = _list(client, ws, match_status="duplicate")
    assert duplicates["total"] == 2
    unmatched = _list(client, ws, match_status="unmatched")
    assert unmatched["total"] == 7
    # Both filters combine.
    both = _list(
        client, ws, import_batch_id=batch["batch_id"], match_status="duplicate"
    )
    assert both["total"] == 2
    # Unknown batch in this workspace -> 404.
    resp = client.get(
        f"/api/workspaces/{ws}/statements",
        params={"import_batch_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 404, resp.text
    # Invalid match_status -> 422.
    resp = client.get(f"/api/workspaces/{ws}/statements", params={"match_status": "bogus"})
    assert resp.status_code == 422, resp.text


def test_detail_and_404s(client, imported):
    ws, _account, batch = imported
    body = _list(client, ws)
    line_id = body["statements"][0]["statement_line_id"]
    resp = client.get(f"/api/workspaces/{ws}/statements/{line_id}")
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["statement_line_id"] == line_id
    assert detail["import_batch"]["batch_id"] == batch["batch_id"]
    assert detail["raw_values"] is not None

    # Unknown line -> 404.
    resp = client.get(
        f"/api/workspaces/{ws}/statements/00000000-0000-0000-0000-000000000000"
    )
    assert resp.status_code == 404, resp.text
    # Cross-workspace: a second user gets 404 on the first user's line.
    register(client, "m8-statements-b@example.com")
    login(client, "m8-statements-b@example.com")
    resp = client.get(f"/api/workspaces/{ws}/statements/{line_id}")
    assert resp.status_code == 404, resp.text
    resp = client.get(f"/api/workspaces/{ws}/statements")
    assert resp.status_code == 404, resp.text


def test_unauthenticated_and_read_only(client, imported):
    ws, _account, _batch = imported
    anon = client.__class__(app)
    resp = anon.get(f"/api/workspaces/{ws}/statements")
    assert resp.status_code == 401, resp.text
    body = _list(client, ws)
    line_id = body["statements"][0]["statement_line_id"]
    resp = anon.get(f"/api/workspaces/{ws}/statements/{line_id}")
    assert resp.status_code == 401, resp.text
    # No write routes exist: POST / PUT / DELETE -> 405.
    resp = client.post(f"/api/workspaces/{ws}/statements", json={})
    assert resp.status_code == 405, resp.text
    resp = client.put(f"/api/workspaces/{ws}/statements/{line_id}", json={})
    assert resp.status_code == 405, resp.text
    resp = client.delete(f"/api/workspaces/{ws}/statements/{line_id}")
    assert resp.status_code == 405, resp.text
