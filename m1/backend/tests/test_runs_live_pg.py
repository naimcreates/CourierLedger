"""Live-PostgreSQL reconciliation run tests (M1 Milestone 6).

Runs the real app (uvicorn subprocess) against a scratch PostgreSQL
database with the full schema + RLS + roles applied (see
tests/live_pg.py). Skipped unless CL_LIVE_PG_PASSWORD is set.

Run with the runtime role (the production path):
CL_LIVE_PG_PASSWORD=... CL_LIVE_PG_ROLE=courierledger_runtime python -m pytest tests/test_runs_live_pg.py -q

Asserts, live:
  * POST /api/workspaces/{id}/imports/{batch_id}/runs creates a run
    from a real import under the runtime role (201): the run row is
    'completed' in PostgreSQL, results + fee lines + findings are
    stored, and the deferred CL-01 trigger does NOT reject the
    stored results (a rejection here is a trigger/engine disagreement
    and must be reported, not worked around).
  * A merchant-confirmed rule version run stores matched / open /
    mismatch results with exact variances.
  * A second user gets 404 on run detail / list for another user's
    workspace (no existence leak).
  * The run is immutable: PUT / PATCH / DELETE on a run are 405.

Shipments and the rule version are seeded by direct SQL as the owner
role (there is no shipments or rule-version API yet); everything the
user would do goes through HTTP. All data is synthetic.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
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
    resp = post(client, "/api/workspaces", {"name": "M6 Live Shop"})
    assert resp.status_code == 201, resp.text
    workspace_id = resp.json()["id"]
    resp = post(
        client,
        f"/api/workspaces/{workspace_id}/courier-accounts",
        {"courier_code": "steadfast", "display_label": "Steadfast test account"},
    )
    assert resp.status_code == 201, resp.text
    return workspace_id, resp.json()["id"]


def _seed_shipments(workspace_id: str, account_id: str) -> None:
    """Seed delivered orders/shipments matching the generic fixture,
    as the owner role (no shipments API exists yet). Tenant tables
    are FORCE RLS, so the owner connection must also assert the
    tenant context (set_config, transaction-local)."""
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_merchant_id', %s, true)",
                        (str(workspace_id),))
            for ref, tracking in [
                ("ORD-1001", "TRK-1001"),
                ("ORD-1002", "TRK-1002"),
                ("ORD-1008", "TRK-1008"),
                ("ORD-1009", "TRK-1009"),
            ]:
                # orders.id / shipments.id have no server default (the
                # UUID PK is generated app-side), and neither do the
                # currency/source/flag columns (their defaults live in
                # the ORM), so pass explicit values for every NOT NULL
                # column without a database default.
                order_id = str(uuid.uuid4())
                shipment_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO orders (id, merchant_id, merchant_order_ref, order_cod_poisha,"
                    " currency, source)"
                    " VALUES (%s, %s, %s, 100000, 'BDT', 'manual')",
                    (order_id, workspace_id, ref),
                )
                cur.execute(
                    "INSERT INTO shipments (id, merchant_id, order_id, merchant_courier_account_id,"
                    " tracking_id_raw, tracking_id_normalised, outcome,"
                    " expected_collectible_cod_poisha, is_cod_bearing, is_superseded, shipped_on)"
                    " VALUES (%s, %s, %s, %s, %s, %s, 'delivered', 100000, TRUE, FALSE, '2026-10-10')",
                    (shipment_id, workspace_id, order_id, account_id, tracking, tracking),
                )
        conn.commit()


def _seed_rule_version(workspace_id: str, account_id: str, user_email: str) -> str:
    """Seed a merchant-confirmed rule version (full_cod, 100bp,
    delivery 7000, return/other/tax 0) as the owner role. Returns its id."""
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_merchant_id', %s, true)",
                        (str(workspace_id),))
            cur.execute("SELECT id FROM users WHERE email = %s", (user_email,))
            user_id = cur.fetchone()[0]
            rule_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO courier_rule_versions (id, merchant_id,"
                " merchant_courier_account_id, version_no, status, cod_fee_base,"
                " cod_fee_rate_bps, delivery_fee_poisha, return_fee_poisha,"
                " vat_mode, rule_definition, content_hash, created_by_user_id,"
                " dc_confirmed, negative_net_payout_permitted)"
                " VALUES (%s, %s, %s, 1, 'draft', 'expected_collectible', 100,"
                " 7000, 0, 'unknown', %s::jsonb, %s, %s, FALSE, FALSE)",
                (
                    rule_id, workspace_id, account_id,
                    json.dumps({"other_fees_poisha": 0, "excluded_tax_poisha": 0}),
                    hashlib.sha256(b"m6-live-rule").hexdigest(),
                    user_id,
                ),
            )
        conn.commit()
    return rule_id


def _upload(client: httpx.Client, workspace_id: str, account_id: str, key: str):
    headers = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": key}
    return client.post(
        f"/api/workspaces/{workspace_id}/imports",
        data={"mapping": json.dumps(MAPPING), "merchant_courier_account_id": account_id},
        files={"file": ("statement.csv", FIXTURE.read_bytes(), "text/csv")},
        headers=headers,
    )


def _create_run(client: httpx.Client, workspace_id: str, batch_id: str, key: str, body: dict):
    headers = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": key}
    return client.post(
        f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs",
        json=body, headers=headers,
    )


def _db_run_rows(run_id: str, workspace_id: str):
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_merchant_id', %s, true)",
                        (str(workspace_id),))
            cur.execute(
                "SELECT status, tolerance_poisha, engine_version, completed_at IS NOT NULL"
                " FROM reconciliation_runs WHERE id = %s",
                (run_id,),
            )
            run_row = cur.fetchone()
            cur.execute(
                "SELECT overall_status, COUNT(*) FROM reconciliation_results"
                " WHERE run_id = %s GROUP BY overall_status ORDER BY overall_status",
                (run_id,),
            )
            result_counts = dict(cur.fetchall())
            cur.execute(
                "SELECT COUNT(*) FROM reconciliation_fee_lines"
                " WHERE result_id IN (SELECT id FROM reconciliation_results WHERE run_id = %s)",
                (run_id,),
            )
            fee_lines = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM reconciliation_findings WHERE run_id = %s",
                (run_id,),
            )
            findings = cur.fetchone()[0]
    return run_row, result_counts, fee_lines, findings


def test_fixture_alpha_run_live(live):
    client = authed_client(live["base_url"], "m6a-live@example.com", PASSWORD)
    try:
        workspace_id, account_id = _setup_workspace(client)
        _seed_shipments(workspace_id, account_id)
        resp = _upload(client, workspace_id, account_id, "m6-live-imp-1")
        assert resp.status_code == 201, resp.text
        batch_id = resp.json()["batch_id"]

        resp = _create_run(client, workspace_id, batch_id, "m6-live-run-1",
                           {"courier_fixture": "Fixture Alpha"})
        assert resp.status_code == 201, resp.text
        summary = resp.json()
        assert summary["status"] == "completed"
        # The fixture has 12 rows; only the four seeded strong matches
        # are eligible (the other fixture rows have no seeded shipment).
        assert summary["result_counts"] == {
            "matched": 0, "open": 0, "mismatch": 0,
            "needs_review": 4, "matched_with_tolerance": 0,
        }
        run_id = summary["run_id"]

        # The deferred CL-01 trigger fires at commit: the run was
        # created at all, so it did not reject these stored results.
        run_row, result_counts, fee_lines, findings = _db_run_rows(run_id, workspace_id)
        assert run_row == ("completed", 0, "V2.2.1", True)
        assert result_counts == {"needs_review": 4}
        assert fee_lines == 16  # 4 results x 4 fee lines
        assert findings == 4  # COLLECTION x2 + DQ_BLANK_VALUE x2

        # Replay: same key returns the same run with 200.
        resp = _create_run(client, workspace_id, batch_id, "m6-live-run-1",
                           {"courier_fixture": "Fixture Alpha"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["run_id"] == run_id

        detail = client.get(f"/api/workspaces/{workspace_id}/runs/{run_id}")
        assert detail.status_code == 200, detail.text
        assert len(detail.json()["results"]) == 4
    finally:
        client.close()


def test_rule_version_run_live(live):
    client = authed_client(live["base_url"], "m6b-live@example.com", PASSWORD)
    try:
        workspace_id, account_id = _setup_workspace(client)
        _seed_shipments(workspace_id, account_id)
        rule_id = _seed_rule_version(workspace_id, account_id, "m6b-live@example.com")
        resp = _upload(client, workspace_id, account_id, "m6-live-imp-2")
        assert resp.status_code == 201, resp.text
        batch_id = resp.json()["batch_id"]

        resp = _create_run(client, workspace_id, batch_id, "m6-live-run-2",
                           {"rule_version_id": rule_id})
        assert resp.status_code == 201, resp.text
        summary = resp.json()
        assert summary["result_counts"] == {
            "matched": 1, "open": 1, "mismatch": 1,
            "needs_review": 1, "matched_with_tolerance": 0,
        }
        run_id = summary["run_id"]

        run_row, result_counts, fee_lines, findings = _db_run_rows(run_id, workspace_id)
        assert run_row == ("completed", 0, "V2.2.1", True)
        assert result_counts == {"matched": 1, "mismatch": 1, "needs_review": 1, "open": 1}
        assert fee_lines == 16
        # Findings: TRK-1002 COLLECTION + SCENARIO, TRK-1009 four,
        # TRK-1008 two DQ_BLANK_VALUE.
        assert findings == 8

        # Exact variances for the open result, read back from PostgreSQL.
        with _owner_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.current_merchant_id', %s, true)",
                            (str(workspace_id),))
                cur.execute(
                    "SELECT r.c_poisha, r.delta_poisha, r.v_r_poisha, r.s_poisha,"
                    " r.v_total_poisha, r.v_cond_poisha, r.overall_status,"
                    " r.contractual_payout_status"
                    " FROM reconciliation_results r"
                    " JOIN statement_lines l ON l.id = r.statement_line_id"
                    " WHERE r.run_id = %s AND l.tracking_id_normalised = 'TRK-1002'",
                    (run_id,),
                )
                row = cur.fetchone()
        assert row == (-5000, -50, 0, 0, -4950, 0, "open", "matched")

        # Runs are immutable: no update/delete route exists.
        for method in ("put", "patch", "delete"):
            r = client.request(method, f"/api/workspaces/{workspace_id}/runs/{run_id}",
                               headers={"X-CSRF-Token": get_csrf(client)})
            assert r.status_code == 405, (method, r.status_code)
    finally:
        client.close()


def test_second_user_cannot_see_runs_live(live):
    client_a = authed_client(live["base_url"], "m6c-live@example.com", PASSWORD)
    client_b = authed_client(live["base_url"], "m6d-live@example.com", PASSWORD)
    try:
        workspace_id, account_id = _setup_workspace(client_a)
        _seed_shipments(workspace_id, account_id)
        resp = _upload(client_a, workspace_id, account_id, "m6-live-imp-3")
        assert resp.status_code == 201, resp.text
        batch_id = resp.json()["batch_id"]
        resp = _create_run(client_a, workspace_id, batch_id, "m6-live-run-3",
                           {"courier_fixture": "Fixture Alpha"})
        assert resp.status_code == 201, resp.text
        run_id = resp.json()["run_id"]
        for method, path in [
            ("GET", f"/api/workspaces/{workspace_id}/runs"),
            ("GET", f"/api/workspaces/{workspace_id}/runs/{run_id}"),
            ("GET", f"/api/workspaces/{workspace_id}/runs/{run_id}/results"),
            ("GET", f"/api/workspaces/{workspace_id}/runs/{run_id}/findings"),
            ("GET", f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs"),
        ]:
            r = client_b.request(method, path)
            assert r.status_code == 404, (method, path, r.text)
    finally:
        client_a.close()
        client_b.close()
