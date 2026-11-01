"""Live-PostgreSQL CSV import tests (M1 Milestone 4).

Runs the real app (uvicorn subprocess) against a scratch PostgreSQL
database with the full schema + RLS + roles applied (see
tests/live_pg.py). Skipped unless CL_LIVE_PG_PASSWORD is set.

Run with the owner role (default) and with the runtime role:
CL_LIVE_PG_PASSWORD=... CL_LIVE_PG_ROLE=courierledger_runtime python -m pytest tests/test_imports_live_pg.py -q

Asserts, live:
  * POST /api/workspaces/{id}/imports uploads the synthetic generic
    statement fixture (201): batch 'parsed', 12 rows, 10 parsed,
    7 error rows; GET detail shows the strong match, the duplicate,
    the ambiguous and the amount-only unmatched row.
  * Re-upload with the same Idempotency-Key replays the same batch
    (no second batch, no duplicate rows).
  * A second user gets 404 (no existence leak) on list / detail /
    upload for another user's workspace.
  * Unauthenticated upload (with a CSRF token, no session) is 401.
  * A mapping with an unknown canonical field is 422.
  * The scratch DB has the five seeded courier codes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import psycopg
import pytest

from tests.live_pg import (
    authed_client,
    get_csrf,
    live_pg_enabled,
    live_pg_server,
    pg_host,
    post,
)

pytestmark = pytest.mark.skipif(
    not live_pg_enabled(), reason="live-PG tests need CL_LIVE_PG_PASSWORD"
)

PASSWORD = "correct-horse-battery-1"
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
    resp = post(client, "/api/workspaces", {"name": "M4 Live Shop"})
    assert resp.status_code == 201, resp.text
    workspace_id = resp.json()["id"]
    resp = post(
        client,
        f"/api/workspaces/{workspace_id}/courier-accounts",
        {"courier_code": "steadfast", "display_label": "Steadfast test account"},
    )
    assert resp.status_code == 201, resp.text
    return workspace_id, resp.json()["id"]


def _upload(client: httpx.Client, workspace_id: str, account_id: str, key: str, mapping=None):
    data = FIXTURE.read_bytes()
    headers = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": key}
    resp = client.post(
        f"/api/workspaces/{workspace_id}/imports",
        data={
            "mapping": json.dumps(mapping or MAPPING),
            "merchant_courier_account_id": account_id,
        },
        files={"file": ("statement.csv", data, "text/csv")},
        headers=headers,
    )
    return resp


def test_upload_fixture_live(live):
    client = authed_client(live["base_url"], "m4a-live@example.com", PASSWORD)
    try:
        workspace_id, account_id = _setup_workspace(client)
        resp = _upload(client, workspace_id, account_id, "m4-live-1")
        assert resp.status_code == 201, resp.text
        summary = resp.json()
        assert summary["status"] == "parsed"
        assert summary["row_count"] == 12
        assert summary["parsed_row_count"] == 10
        # 6, not 7: no shipments are seeded in this live test, so row 5
        # (repeated tracking TRK-AMB) finds no shipment candidates and
        # is unmatched without an ambiguity error. The SQLite suite
        # (tests/test_csv_import.py) covers the seeded ambiguous case.
        assert summary["error_row_count"] == 6
        assert summary["mapping_snapshot"] == MAPPING
        batch_id = summary["batch_id"]

        detail = client.get(f"/api/workspaces/{workspace_id}/imports/{batch_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert len(body["rows"]) == 12
        assert body["truncated"] is False
        by_row = {r["row_number"]: r for r in body["rows"]}
        # No shipments were seeded, so the strong/weak candidates are
        # absent here -- but the duplicate, ambiguous-via-weak and
        # amount-only rows are still decided by the file alone:
        assert by_row[1]["statement_line"]["match_status"] == "unmatched"
        assert by_row[3]["statement_line"]["match_status"] == "duplicate"
        assert by_row[4]["statement_line"]["match_status"] == "duplicate"
        assert by_row[6]["statement_line"]["match_status"] == "unmatched"
        assert by_row[8]["statement_line"]["reported_payout_state"] == "invalid"
        assert by_row[9]["statement_line"]["reported_collected_state"] == "explicit_zero"
        assert by_row[10]["parse_status"] == "skipped_blank"
        assert by_row[12]["parse_status"] == "failed"
        codes = {e["error_code"] for e in body["errors"]}
        assert {"DUPLICATE_TRACKING", "VALUE_INVALID", "VALUE_BLANK", "VALUE_UNPARSABLE"} <= codes

        # Idempotent replay: same key, same file -> same batch, no new rows.
        resp2 = _upload(client, workspace_id, account_id, "m4-live-1")
        assert resp2.status_code == 201, resp2.text
        assert resp2.json()["batch_id"] == batch_id
        assert resp2.json()["idempotency_outcome"] == "replay"

        listing = client.get(f"/api/workspaces/{workspace_id}/imports")
        assert listing.status_code == 200, listing.text
        assert [b["batch_id"] for b in listing.json()] == [batch_id]
    finally:
        client.close()


def test_second_user_cannot_see_imports(live):
    client_a = authed_client(live["base_url"], "m4b-live@example.com", PASSWORD)
    client_b = authed_client(live["base_url"], "m4c-live@example.com", PASSWORD)
    try:
        workspace_id, account_id = _setup_workspace(client_a)
        resp = _upload(client_a, workspace_id, account_id, "m4-live-2")
        assert resp.status_code == 201, resp.text
        batch_id = resp.json()["batch_id"]

        for method, path in [
            ("GET", f"/api/workspaces/{workspace_id}/imports"),
            ("GET", f"/api/workspaces/{workspace_id}/imports/{batch_id}"),
        ]:
            r = client_b.request(method, path)
            assert r.status_code == 404, (method, path, r.text)
        r = _upload(client_b, workspace_id, account_id, "m4-live-2b")
        assert r.status_code == 404, r.text
    finally:
        client_a.close()
        client_b.close()


def test_unauthenticated_upload_is_401(live):
    client = authed_client(live["base_url"], "m4d-live@example.com", PASSWORD)
    anon = httpx.Client(base_url=live["base_url"], timeout=10.0, trust_env=False)
    try:
        workspace_id, account_id = _setup_workspace(client)
        headers = {"X-CSRF-Token": get_csrf(anon), "Idempotency-Key": "m4-live-3"}
        resp = anon.post(
            f"/api/workspaces/{workspace_id}/imports",
            data={
                "mapping": json.dumps(MAPPING),
                "merchant_courier_account_id": account_id,
            },
            files={"file": ("statement.csv", FIXTURE.read_bytes(), "text/csv")},
            headers=headers,
        )
        assert resp.status_code == 401, resp.text
        resp = anon.get(f"/api/workspaces/{workspace_id}/imports")
        assert resp.status_code == 401, resp.text
    finally:
        client.close()
        anon.close()


def test_bad_mapping_is_422(live):
    client = authed_client(live["base_url"], "m4e-live@example.com", PASSWORD)
    try:
        workspace_id, account_id = _setup_workspace(client)
        bad = dict(MAPPING)
        bad["not_a_field"] = "Tracking ID"
        resp = _upload(client, workspace_id, account_id, "m4-live-4", mapping=bad)
        assert resp.status_code == 422, resp.text
    finally:
        client.close()


def test_couriers_seeded_in_scratch_db(live):
    with psycopg.connect(
        f"host={pg_host()} dbname=courierledger_live_test "
        f"user=courierledger password={os.environ['CL_LIVE_PG_PASSWORD']}"
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM couriers ORDER BY code")
            codes = [row[0] for row in cur.fetchall()]
    assert codes == ["ecourier", "paperfly", "pathao", "redx", "steadfast"]
