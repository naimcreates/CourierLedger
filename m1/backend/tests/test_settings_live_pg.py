"""Live-PostgreSQL workspace settings tests (M1 Milestone 9, part 2).

Runs the real app (uvicorn subprocess) against a scratch PostgreSQL
database with the full schema + RLS + roles applied (see
tests/live_pg.py). Skipped unless CL_LIVE_PG_PASSWORD is set.

Run with the runtime role (the production path):
CL_LIVE_PG_PASSWORD=<your-local-db-password> \
CL_LIVE_PG_ROLE=courierledger_runtime \
CL_LIVE_PG_PORT=8011 python -m pytest tests/test_settings_live_pg.py -q

Asserts, live:
  * GET /api/workspaces/{id}/settings returns the effective
    defaults (tolerance 0) when no row exists, and PATCH creates
    the row under the runtime role (RLS tenant policy) -- the
    write really lands in PostgreSQL.
  * A member added by direct SQL gets 403 on PATCH but 200 on GET.
  * Tolerance flows into runs over HTTP: a run created before the
    PATCH records tolerance 0 and a 200-poisha payout variance as
    `mismatch`; after PATCHing tolerance to 500, a new run from
    the same import records tolerance 500 and the same variance
    as `matched_with_tolerance` (gross difference still stored).

All data is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
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

PASSWORD = secrets.token_urlsafe(16)  # Phase 1 (A-F5): random per run, not a shared literal
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
# Same 200-poisha-short payout as the SQLite flow test: expected
# payout 92000 (full_cod, 100bp, delivery 7000), reported 91800.
TOL_CSV = (
    "Tracking ID,Order Ref,Collected (poisha),Payout (poisha),Delivery Fee (poisha),"
    "COD Fee (poisha),Return Fee (poisha),Other Fee (poisha),Status,Statement Date\n"
    "TRK-TOL,ORD-TOL,100000,91800,7000,1000,0,0,delivered,2026-10-15\n"
).encode()
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


def _patch(client: httpx.Client, path: str, payload: dict):
    return client.patch(
        path, json=payload, headers={"X-CSRF-Token": get_csrf(client)}
    )


def _setup_workspace(client: httpx.Client) -> tuple[str, str]:
    """Create a workspace + a Steadfast courier account for this client.
    Returns (workspace_id, account_id)."""
    resp = post(client, "/api/workspaces", {"name": "Settings Live Shop"})
    assert resp.status_code == 201, resp.text
    workspace_id = resp.json()["id"]
    resp = post(
        client,
        f"/api/workspaces/{workspace_id}/courier-accounts",
        {"courier_code": "steadfast", "display_label": "Steadfast test account"},
    )
    assert resp.status_code == 201, resp.text
    return workspace_id, resp.json()["id"]


def _seed_shipment(workspace_id: str, account_id: str) -> None:
    """Seed one delivered order/shipment (ORD-TOL / TRK-TOL, COD
    100000) as the owner role."""
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_merchant_id', %s, true)",
                        (str(workspace_id),))
            order_id = str(uuid.uuid4())
            shipment_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO orders (id, merchant_id, merchant_order_ref, order_cod_poisha,"
                " currency, source) VALUES (%s, %s, %s, 100000, 'BDT', 'manual')",
                (order_id, workspace_id, "ORD-TOL"),
            )
            cur.execute(
                "INSERT INTO shipments (id, merchant_id, order_id, merchant_courier_account_id,"
                " tracking_id_raw, tracking_id_normalised, outcome,"
                " expected_collectible_cod_poisha, is_cod_bearing, is_superseded, shipped_on)"
                " VALUES (%s, %s, %s, %s, %s, %s, 'delivered', 100000, TRUE, FALSE, '2026-10-10')",
                (shipment_id, workspace_id, order_id, account_id, "TRK-TOL", "TRK-TOL"),
            )
        conn.commit()


def _seed_rule_version(workspace_id: str, account_id: str, user_email: str) -> str:
    """Seed a merchant-confirmed rule version (expected_collectible,
    100bp, delivery 7000, return/other/tax 0) as the owner role."""
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
                    hashlib.sha256(b"settings-live-rule").hexdigest(),
                    user_id,
                ),
            )
        conn.commit()
    return rule_id


def _add_member(workspace_id: str, owner_email: str, member_email: str) -> None:
    """Give an already-registered user an active 'member' role by
    direct SQL (there is no member-invite API yet)."""
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_merchant_id', %s, true)",
                        (str(workspace_id),))
            cur.execute("SELECT id FROM users WHERE email = %s", (owner_email,))
            owner_id = cur.fetchone()[0]
            cur.execute("SELECT id FROM users WHERE email = %s", (member_email,))
            member_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO merchant_memberships (id, merchant_id, user_id, role,"
                " status, invited_by_user_id, created_at, updated_at)"
                " VALUES (%s, %s, %s, 'member', 'active', %s, now(), now())",
                (str(uuid.uuid4()), workspace_id, member_id, owner_id),
            )
        conn.commit()


def _upload(client: httpx.Client, workspace_id: str, account_id: str, key: str):
    headers = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": key}
    return client.post(
        f"/api/workspaces/{workspace_id}/imports",
        data={"mapping": json.dumps(MAPPING), "merchant_courier_account_id": account_id},
        files={"file": ("statement.csv", TOL_CSV, "text/csv")},
        headers=headers,
    )


def _create_run(client: httpx.Client, workspace_id: str, batch_id: str, key: str, body: dict):
    headers = {"X-CSRF-Token": get_csrf(client), "Idempotency-Key": key}
    return client.post(
        f"/api/workspaces/{workspace_id}/imports/{batch_id}/runs",
        json=body, headers=headers,
    )


def _db_settings_row(workspace_id: str):
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_merchant_id', %s, true)",
                        (str(workspace_id),))
            cur.execute(
                "SELECT tolerance_poisha, locale FROM workspace_settings"
                " WHERE merchant_id = %s",
                (workspace_id,),
            )
            return cur.fetchone()


def _db_run_result(workspace_id: str, run_id: str):
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_merchant_id', %s, true)",
                        (str(workspace_id),))
            cur.execute(
                "SELECT tolerance_poisha FROM reconciliation_runs WHERE id = %s",
                (run_id,),
            )
            run_tolerance = cur.fetchone()[0]
            cur.execute(
                "SELECT overall_status, v_cond_poisha, s_poisha FROM reconciliation_results"
                " WHERE run_id = %s",
                (run_id,),
            )
            result = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE is_within_tolerance)"
                " FROM reconciliation_findings WHERE run_id = %s",
                (run_id,),
            )
            finding_total, finding_within = cur.fetchone()
    return run_tolerance, result, (finding_total, finding_within)


def test_settings_defaults_patch_and_member_readonly_live(live):
    owner = authed_client(live["base_url"], "set-live-owner@example.com", PASSWORD)
    member = authed_client(live["base_url"], "set-live-member@example.com", PASSWORD)
    try:
        workspace_id, _account_id = _setup_workspace(owner)
        path = f"/api/workspaces/{workspace_id}/settings"

        # No row yet: effective defaults, and the row is not created.
        resp = owner.get(path)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tolerance_poisha"] == 0
        assert resp.json()["locale"] == "en_bn"
        assert resp.json()["is_default"] is True
        assert _db_settings_row(workspace_id) is None

        # PATCH as owner creates the row under the runtime role.
        resp = _patch(owner, path, {"tolerance_poisha": 500})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tolerance_poisha"] == 500
        assert resp.json()["is_default"] is False
        assert _db_settings_row(workspace_id) == (500, "en_bn")

        # A member may read but not write.
        _add_member(workspace_id, "set-live-owner@example.com", "set-live-member@example.com")
        resp = member.get(path)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tolerance_poisha"] == 500
        resp = _patch(member, path, {"tolerance_poisha": 1})
        assert resp.status_code == 403, resp.text
        assert _db_settings_row(workspace_id) == (500, "en_bn")
    finally:
        owner.close()
        member.close()


def test_tolerance_flows_into_runs_live(live):
    client = authed_client(live["base_url"], "set-live-tol@example.com", PASSWORD)
    try:
        workspace_id, account_id = _setup_workspace(client)
        _seed_shipment(workspace_id, account_id)
        rule_id = _seed_rule_version(workspace_id, account_id, "set-live-tol@example.com")
        resp = _upload(client, workspace_id, account_id, "set-live-tol-imp")
        assert resp.status_code == 201, resp.text
        batch_id = resp.json()["batch_id"]

        # Run 1: no settings row, no explicit tolerance -> effective
        # default 0; the 200-poisha variance is a mismatch.
        resp = _create_run(client, workspace_id, batch_id, "set-live-tol-run-1",
                           {"rule_version_id": rule_id})
        assert resp.status_code == 201, resp.text
        summary = resp.json()
        assert summary["tolerance_poisha"] == 0
        assert summary["result_counts"] == {
            "matched": 0, "open": 0, "mismatch": 1,
            "needs_review": 0, "matched_with_tolerance": 0,
        }
        run_tolerance, result, _findings = _db_run_result(workspace_id, summary["run_id"])
        assert run_tolerance == 0
        assert result == ("mismatch", -200, -200)

        # PATCH the workspace tolerance to 500 over HTTP.
        resp = _patch(client, f"/api/workspaces/{workspace_id}/settings",
                      {"tolerance_poisha": 500})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tolerance_poisha"] == 500

        # Run 2 from the same batch, no explicit tolerance: the run
        # picks up the workspace setting, stores it, and the same
        # 200-poisha variance is now within tolerance.
        resp = _create_run(client, workspace_id, batch_id, "set-live-tol-run-2",
                           {"rule_version_id": rule_id})
        assert resp.status_code == 201, resp.text
        summary = resp.json()
        assert summary["tolerance_poisha"] == 500
        assert summary["result_counts"] == {
            "matched": 0, "open": 0, "mismatch": 0,
            "needs_review": 0, "matched_with_tolerance": 1,
        }
        run_tolerance, result, findings = _db_run_result(workspace_id, summary["run_id"])
        assert run_tolerance == 500
        assert result == ("matched_with_tolerance", -200, -200)
        assert findings[0] > 0  # findings are preserved within tolerance
        assert findings[0] == findings[1]  # ... and all flagged within tolerance
    finally:
        client.close()
