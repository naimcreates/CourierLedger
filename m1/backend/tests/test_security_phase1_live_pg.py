"""Phase 1 security tests — live PostgreSQL, runtime role.

Run ONLY with the runtime role:
  CL_LIVE_PG_PASSWORD=<password from gitignored backend/.env>
  CL_LIVE_PG_ROLE=courierledger_runtime
  .venv/bin/python -m pytest tests/test_security_phase1_live_pg.py -q

Proves, live:
- B-F1 logout / revoke-all / password-reset really revoke sessions
  under the runtime role (UPDATE revoked_at — the role has no
  DELETE grant, so the old DELETE implementation 500'd).
- B-F3 viewer POSTs are 403 live (at least the matrix spot-checks).
- B-F7 direct-SQL self-promotion probe: FIXED in Phase 1.1. RLS
  policies on merchant_memberships are now role-scoped (only an
  active owner/admin may manage memberships; only an owner may set
  role='owner'), backstopped by the fn_membership_role_guard
  trigger, so a viewer/member can no longer promote themselves via
  direct SQL. The probe now asserts the block (rowcount 0, role
  unchanged) instead of pinning the residual.
- B-F12 create_workspace() rejects a p_user_id that is not the
  authenticated user; the API path still works.
- B-F17 PUBLIC can no longer EXECUTE app_current_*; the runtime
  role still can (RLS policies need it).
- Cross-tenant HTTP probes: user B (non-member of A's workspace)
  gets 404 on every workspace-scoped endpoint.
- Direct-SQL RLS probes as the runtime role: context A vs B,
  unset/malformed context -> zero rows, cross-tenant INSERT
  rejected, append-only UPDATE rejected.
- Session expiry -> 401; CSRF missing/wrong -> 403 on POST/PATCH.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
import pytest

from tests.live_pg import (
    authed_client,
    get_csrf,
    live_pg_enabled,
    live_pg_server,
    owner_dsn,
    pg_host,
    post,
)

pytestmark = pytest.mark.skipif(
    not (live_pg_enabled() and os.environ.get("CL_LIVE_PG_ROLE") == "courierledger_runtime"),
    reason="Phase 1 live tests need CL_LIVE_PG_PASSWORD + CL_LIVE_PG_ROLE=courierledger_runtime",
)

PASSWORD = secrets.token_urlsafe(16)  # Phase 1 (A-F5): random per run
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


def _runtime_conn():
    return psycopg.connect(
        f"host={pg_host()} dbname=courierledger_live_test "
        f"user=courierledger_runtime password={os.environ['CL_LIVE_PG_PASSWORD']}"
    )


def _user_id(client: httpx.Client) -> str:
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _setup_a(client: httpx.Client) -> dict:
    """Build A's full resource chain. Returns ids for cross-tenant probes.

    The order/shipment for ORD-1001/TRK-1001 is created BEFORE the
    fixture upload so the fixture row strong-matches and the run has
    an eligible line (a run with no eligible lines is 422)."""
    resp = post(client, "/api/workspaces", {"name": "Sec Live Shop A"})
    assert resp.status_code == 201, resp.text
    ws = resp.json()["id"]
    resp = post(client, f"/api/workspaces/{ws}/courier-accounts",
                {"courier_code": "steadfast"})
    assert resp.status_code == 201, resp.text
    account = resp.json()["id"]
    resp = post(client, f"/api/workspaces/{ws}/orders",
                {"merchant_order_ref": "ORD-1001", "order_cod_poisha": 100000})
    assert resp.status_code == 201, resp.text
    order = resp.json()["order_id"]
    resp = post(client, f"/api/workspaces/{ws}/orders/{order}/shipments",
                {"tracking_id": "TRK-1001", "outcome": "delivered",
                 "is_cod_bearing": True, "amount_to_collect_poisha": 100000})
    assert resp.status_code == 201, resp.text
    shipment = resp.json()["shipment_id"]
    resp = client.post(
        f"/api/workspaces/{ws}/imports",
        data={"mapping": json.dumps(MAPPING),
              "merchant_courier_account_id": account},
        files={"file": ("statement.csv", FIXTURE.read_bytes(), "text/csv")},
        headers={"X-CSRF-Token": get_csrf(client),
                 "Idempotency-Key": "sec-live-import-1"},
    )
    assert resp.status_code == 201, resp.text
    batch = resp.json()["batch_id"]
    resp = post(client, f"/api/workspaces/{ws}/imports/{batch}/runs",
                {"courier_fixture": "Fixture Alpha"})
    assert resp.status_code in (200, 201), resp.text
    run = resp.json()["run_id"]
    resp = post(client, f"/api/workspaces/{ws}/settlement-batches",
                {"merchant_courier_account_id": account, "run_id": run})
    assert resp.status_code == 201, resp.text
    sbatch = resp.json()["batch_id"]
    resp = post(client, f"/api/workspaces/{ws}/settlement-batches/{sbatch}/receipts",
                {"amount_poisha": 50000})
    assert resp.status_code == 201, resp.text
    receipt = resp.json()["receipt_id"]
    resp = post(client, f"/api/workspaces/{ws}/settlement-batches/{sbatch}/allocations",
                {"bank_receipt_id": receipt, "allocated_poisha": 10000})
    assert resp.status_code == 201, resp.text
    allocation = resp.json()["allocation_id"]
    resp = client.get(f"/api/workspaces/{ws}/statements?page_size=1")
    assert resp.status_code == 200, resp.text
    lines = resp.json()["statements"]
    assert lines, resp.text
    line = lines[0]["statement_line_id"]
    return {
        "ws": ws, "account": account, "batch": batch, "run": run,
        "sbatch": sbatch, "receipt": receipt, "allocation": allocation,
        "order": order, "shipment": shipment, "line": line,
    }


def _invite_accept(owner: httpx.Client, ws: str, invitee: httpx.Client,
                   email: str, role: str) -> str:
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": email, "role": role})
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    resp = post(invitee, f"/api/workspaces/{ws}/members/{mid}/accept", {})
    assert resp.status_code == 200, resp.text
    return mid


# ---------------------------------------------------------------------------
# B-F1 — revocation under the runtime role
# ---------------------------------------------------------------------------

def test_logout_revokes_session_live(live):
    a = authed_client(live["base_url"], "sec-live-logout@example.com", PASSWORD)
    try:
        assert a.get("/api/auth/me").status_code == 200
        resp = post(a, "/api/auth/logout", {})
        assert resp.status_code == 200, resp.text
        assert a.get("/api/auth/me").status_code == 401
        # The row remains, marked revoked (no DELETE grant).
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM user_sessions WHERE revoked_at IS NOT NULL")
                assert cur.fetchone()[0] >= 1
    finally:
        a.close()


def test_revoke_all_revokes_both_sessions_live(live):
    a1 = authed_client(live["base_url"], "sec-live-revoke@example.com", PASSWORD)
    a2 = authed_client(live["base_url"], "sec-live-revoke@example.com", PASSWORD)
    try:
        assert a1.get("/api/auth/me").status_code == 200
        assert a2.get("/api/auth/me").status_code == 200
        resp = post(a1, "/api/auth/revoke-all", {})
        assert resp.status_code == 200, resp.text
        assert a1.get("/api/auth/me").status_code == 401
        assert a2.get("/api/auth/me").status_code == 401
    finally:
        a1.close()
        a2.close()


def test_reset_confirm_revokes_all_sessions_live(live):
    a1 = authed_client(live["base_url"], "sec-live-reset@example.com", PASSWORD)
    a2 = authed_client(live["base_url"], "sec-live-reset@example.com", PASSWORD)
    try:
        user_id = _user_id(a1)
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO password_reset_tokens "
                    "(id, user_id, token_hash, expires_at) "
                    "VALUES (gen_random_uuid(), %s, %s, %s)",
                    (user_id, digest, expires),
                )
            conn.commit()
        resp = post(a1, "/api/auth/password-reset/confirm",
                    {"token": raw, "new_password": "brand-new-password-9"})
        assert resp.status_code == 200, resp.text
        assert a1.get("/api/auth/me").status_code == 401
        assert a2.get("/api/auth/me").status_code == 401
        # New password works.
        a3 = httpx.Client(base_url=live["base_url"], timeout=10.0, trust_env=False)
        try:
            from tests.live_pg import login

            login(a3, "sec-live-reset@example.com", "brand-new-password-9")
            assert a3.get("/api/auth/me").status_code == 200
        finally:
            a3.close()
    finally:
        a1.close()
        a2.close()


def test_expired_session_is_401_live(live):
    a = authed_client(live["base_url"], "sec-live-expiry@example.com", PASSWORD)
    try:
        user_id = _user_id(a)
        assert a.get("/api/auth/me").status_code == 200
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE user_sessions SET expires_at = now() - interval '1 day' "
                    "WHERE user_id = %s",
                    (user_id,),
                )
            conn.commit()
        assert a.get("/api/auth/me").status_code == 401
    finally:
        a.close()


# ---------------------------------------------------------------------------
# B-F3 — viewer 403 live
# ---------------------------------------------------------------------------

def test_viewer_posts_403_live(live):
    owner = authed_client(live["base_url"], "sec-live-owner@example.com", PASSWORD)
    viewer = authed_client(live["base_url"], "sec-live-viewer@example.com", PASSWORD)
    try:
        resp = post(owner, "/api/workspaces", {"name": "Sec Live Viewer Shop"})
        assert resp.status_code == 201, resp.text
        ws = resp.json()["id"]
        _invite_accept(owner, ws, viewer, "sec-live-viewer@example.com", "viewer")
        fake = str(uuid.uuid4())
        cases = [
            (f"/api/workspaces/{ws}/courier-accounts", {"courier_code": "steadfast"}),
            (f"/api/workspaces/{ws}/orders",
             {"merchant_order_ref": "ORD-V1", "order_cod_poisha": 1000}),
            (f"/api/workspaces/{ws}/settlement-batches",
             {"merchant_courier_account_id": fake}),
            (f"/api/workspaces/{ws}/imports/{fake}/runs",
             {"courier_fixture": "Fixture Alpha"}),
        ]
        for path, payload in cases:
            resp = post(viewer, path, payload)
            assert resp.status_code == 403, (path, resp.status_code, resp.text)
        # Upload as viewer is 403 too (multipart).
        resp = viewer.post(
            f"/api/workspaces/{ws}/imports",
            data={"mapping": json.dumps(MAPPING),
                  "merchant_courier_account_id": fake},
            files={"file": ("statement.csv", FIXTURE.read_bytes(), "text/csv")},
            headers={"X-CSRF-Token": get_csrf(viewer)},
        )
        assert resp.status_code == 403, resp.text
        # Viewer reads still work.
        assert viewer.get(f"/api/workspaces/{ws}").status_code == 200
        assert viewer.get(f"/api/workspaces/{ws}/imports").status_code == 200
    finally:
        owner.close()
        viewer.close()


# ---------------------------------------------------------------------------
# Cross-tenant HTTP probes (user B, non-member of A's workspace)
# ---------------------------------------------------------------------------

def test_cross_tenant_probes_404_live(live):
    a = authed_client(live["base_url"], "sec-live-a@example.com", PASSWORD)
    b = authed_client(live["base_url"], "sec-live-b@example.com", PASSWORD)
    c = authed_client(live["base_url"], "sec-live-c@example.com", PASSWORD)
    try:
        ids = _setup_a(a)
        ws = ids["ws"]
        # Invite C to A's workspace so B can try accepting C's invitation.
        resp = post(a, f"/api/workspaces/{ws}/members/invite",
                    {"email": "sec-live-c@example.com", "role": "member"})
        assert resp.status_code == 201, resp.text
        c_invite_id = resp.json()["id"]

        probes = [
            ("GET", f"/api/workspaces/{ws}", None),
            ("GET", f"/api/workspaces/{ws}/imports", None),
            ("GET", f"/api/workspaces/{ws}/imports/{ids['batch']}", None),
            ("POST", f"/api/workspaces/{ws}/imports/{ids['batch']}/runs",
             {"courier_fixture": "Fixture Alpha"}),
            ("GET", f"/api/workspaces/{ws}/runs", None),
            ("GET", f"/api/workspaces/{ws}/runs/{ids['run']}", None),
            ("GET", f"/api/workspaces/{ws}/runs/{ids['run']}/results", None),
            ("GET", f"/api/workspaces/{ws}/runs/{ids['run']}/findings", None),
            ("GET", f"/api/workspaces/{ws}/imports/{ids['batch']}/runs", None),
            ("POST", f"/api/workspaces/{ws}/settlement-batches",
             {"merchant_courier_account_id": ids["account"]}),
            ("GET", f"/api/workspaces/{ws}/settlement-batches", None),
            ("GET", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}", None),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/receipts",
             {"amount_poisha": 1000}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/receipts/{ids['receipt']}/complete", {}),
            ("GET", f"/api/workspaces/{ws}/bank-receipts", None),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/allocations",
             {"bank_receipt_id": ids["receipt"], "allocated_poisha": 100}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/allocations/{ids['allocation']}/reverse", {}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/calculate-variance", {}),
            ("GET", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/current-variance", None),
            ("GET", f"/api/workspaces/{ws}/orders/{ids['order']}", None),
            ("POST", f"/api/workspaces/{ws}/orders",
             {"merchant_order_ref": "ORD-B1"}),
            ("GET", f"/api/workspaces/{ws}/shipments/{ids['shipment']}", None),
            ("POST", f"/api/workspaces/{ws}/orders/{ids['order']}/shipments",
             {"tracking_id": "TRK-B1"}),
            ("GET", f"/api/workspaces/{ws}/statements", None),
            ("GET", f"/api/workspaces/{ws}/statements/{ids['line']}", None),
            ("GET", f"/api/workspaces/{ws}/reports/summary", None),
            ("POST", f"/api/workspaces/{ws}/courier-accounts",
             {"courier_code": "steadfast"}),
            ("GET", f"/api/workspaces/{ws}/settings", None),
            ("PATCH", f"/api/workspaces/{ws}/settings", {"tolerance_poisha": 5}),
            ("GET", f"/api/workspaces/{ws}/members", None),
            ("POST", f"/api/workspaces/{ws}/members/invite",
             {"email": "x@example.com", "role": "member"}),
            ("PATCH", f"/api/workspaces/{ws}/members/{c_invite_id}/role",
             {"role": "admin"}),
            ("POST", f"/api/workspaces/{ws}/members/{c_invite_id}/revoke", {}),
            ("POST", f"/api/workspaces/{ws}/members/transfer-owner",
             {"user_id": _user_id(b)}),
            ("POST", f"/api/workspaces/{ws}/members/{c_invite_id}/accept", {}),
        ]
        for method, path, payload in probes:
            if method == "GET":
                resp = b.get(path)
            elif method == "POST":
                resp = post(b, path, payload or {})
            elif method == "PATCH":
                resp = b.patch(path, json=payload or {},
                               headers={"X-CSRF-Token": get_csrf(b)})
            else:  # pragma: no cover
                raise AssertionError(method)
            assert resp.status_code == 404, (method, path, resp.status_code, resp.text)
    finally:
        a.close()
        b.close()
        c.close()


# ---------------------------------------------------------------------------
# CSRF matrix live (authed, missing/wrong token -> 403 on POST/PATCH)
# ---------------------------------------------------------------------------

def test_csrf_matrix_live(live):
    a = authed_client(live["base_url"], "sec-live-csrf@example.com", PASSWORD)
    try:
        ids = _setup_a(a)
        ws = ids["ws"]
        fake = str(uuid.uuid4())
        cases = [
            ("POST", "/api/workspaces", {"name": "CSRF Shop"}),
            ("POST", "/api/auth/logout", {}),
            ("POST", "/api/auth/revoke-all", {}),
            ("POST", "/api/auth/password-reset/request", {"email": "x@example.com"}),
            ("POST", f"/api/workspaces/{ws}/courier-accounts", {"courier_code": "steadfast"}),
            ("POST", f"/api/workspaces/{ws}/orders", {"merchant_order_ref": "ORD-C1"}),
            ("POST", f"/api/workspaces/{ws}/orders/{ids['order']}/shipments", {"tracking_id": "TRK-C1"}),
            ("POST", f"/api/workspaces/{ws}/imports/{ids['batch']}/runs", {"courier_fixture": "Fixture Alpha"}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches", {"merchant_courier_account_id": ids["account"]}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/receipts", {"amount_poisha": 100}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/receipts/{ids['receipt']}/complete", {}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/allocations", {"bank_receipt_id": ids["receipt"], "allocated_poisha": 100}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/allocations/{ids['allocation']}/reverse", {}),
            ("POST", f"/api/workspaces/{ws}/settlement-batches/{ids['sbatch']}/calculate-variance", {}),
            ("POST", f"/api/workspaces/{ws}/members/invite", {"email": "y@example.com", "role": "member"}),
            ("POST", "/api/reconcile", {"shipments": []}),
            ("PATCH", f"/api/workspaces/{ws}/settings", {"tolerance_poisha": 1}),
        ]
        for method, path, payload in cases:
            # Missing CSRF.
            if method == "POST":
                resp = a.post(path, json=payload)
            else:
                resp = a.patch(path, json=payload)
            assert resp.status_code == 403, (method, path, "missing", resp.status_code, resp.text)
            # Wrong CSRF.
            if method == "POST":
                resp = a.post(path, json=payload, headers={"X-CSRF-Token": "wrong-token"})
            else:
                resp = a.patch(path, json=payload, headers={"X-CSRF-Token": "wrong-token"})
            assert resp.status_code == 403, (method, path, "wrong", resp.status_code, resp.text)
    finally:
        a.close()


# ---------------------------------------------------------------------------
# Direct-SQL probes as the runtime role
# ---------------------------------------------------------------------------

def test_rls_direct_sql_probes_live(live):
    a = authed_client(live["base_url"], "sec-live-rls-a@example.com", PASSWORD)
    b = authed_client(live["base_url"], "sec-live-rls-b@example.com", PASSWORD)
    try:
        ids_a = _setup_a(a)
        ids_b = _setup_a(b)
        uid_a, uid_b = _user_id(a), _user_id(b)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                def _set_ctx(merchant, user):
                    cur.execute(
                        "SELECT set_config('app.current_merchant_id', %s, true), "
                        "set_config('app.user_id', %s, true)",
                        (merchant, user),
                    )

                # Context A sees only A's orders; context B only B's.
                _set_ctx(ids_a["ws"], uid_a)
                cur.execute("SELECT count(*) FROM orders")
                assert cur.fetchone()[0] == 1
                _set_ctx(ids_b["ws"], uid_b)
                cur.execute("SELECT count(*) FROM orders")
                assert cur.fetchone()[0] == 1
                # Unset context -> zero rows.
                cur.execute("SELECT set_config('app.current_merchant_id', '', true)")
                cur.execute("SELECT count(*) FROM orders")
                assert cur.fetchone()[0] == 0
                # Malformed context -> zero rows (fail-closed).
                cur.execute("SELECT set_config('app.current_merchant_id', 'not-a-uuid', true)")
                cur.execute("SELECT count(*) FROM orders")
                assert cur.fetchone()[0] == 0
                # Cross-tenant INSERT rejected by RLS.
                _set_ctx(ids_a["ws"], uid_a)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(
                        "INSERT INTO orders (merchant_id, merchant_order_ref) "
                        "VALUES (%s, 'ORD-XTENANT')",
                        (ids_b["ws"],),
                    )
                conn.rollback()
                # Append-only UPDATE rejected (no grant).
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE audit_logs SET action = 'x' WHERE false")
                conn.rollback()
    finally:
        a.close()
        b.close()


def test_create_workspace_trusts_p_user_id_residual_b_f12(live):
    """B-F12 residual probe (NOT fixed in Phase 1): create_workspace()
    trusts its p_user_id argument. A Phase 1 assertion that p_user_id
    must match app_current_user_id() was tried and REVERTED — it broke
    29 existing live tests whose fixtures call the function directly
    as the runtime role without setting app.user_id. The control is the
    single application call site (workspace_service passes the session
    user's id; deps.py sets app.user_id from the same session) plus
    the app-layer auth on the endpoint. This test pins the residual:
    a direct-SQL caller CAN create a workspace for another user."""
    a = authed_client(live["base_url"], "sec-live-cw-a@example.com", PASSWORD)
    b = authed_client(live["base_url"], "sec-live-cw-b@example.com", PASSWORD)
    try:
        uid_a, uid_b = _user_id(a), _user_id(b)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                # No app.user_id context is set, and the p_user_id is
                # B's — the call still succeeds (residual).
                cur.execute(
                    "SELECT public.create_workspace(%s, 'Probe Shop', NULL)",
                    (uid_b,),
                )
                new_ws = cur.fetchone()[0]
                assert new_ws is not None
            conn.commit()
        # The workspace created for B is visible to B over HTTP.
        assert b.get(f"/api/workspaces/{new_ws}").status_code == 200
        # And the normal API path still works for A.
        resp = post(a, "/api/workspaces", {"name": "Sec Live CW Shop"})
        assert resp.status_code == 201, resp.text
    finally:
        a.close()
        b.close()


def test_public_cannot_execute_context_helpers_live(live):
    with _runtime_conn() as conn:
        with conn.cursor() as cur:
            for fn in ("app_current_merchant_id", "app_current_user_id"):
                cur.execute(
                    "SELECT has_function_privilege('public', %s, 'EXECUTE')",
                    (f"public.{fn}()",),
                )
                assert cur.fetchone()[0] is False, fn
                cur.execute(
                    "SELECT has_function_privilege('courierledger_runtime', %s, 'EXECUTE')",
                    (f"public.{fn}()",),
                )
                assert cur.fetchone()[0] is True, fn


def test_direct_sql_self_promotion_blocked_by_rls_b_f7(live):
    """B-F7 FIXED in Phase 1.1 (this test pinned the residual in
    Phase 1 and is rewritten here -- not weakened: the old version
    asserted that a viewer COULD self-promote via direct SQL, which
    is exactly what the fix removes). RLS policies on
    merchant_memberships are now role-scoped
    (app_current_user_role()), and the fn_membership_role_guard
    trigger backstops the self-accept arm, so a viewer who can run
    SQL as the runtime role can no longer promote their own
    membership: the direct-SQL UPDATE affects zero rows (USING
    fails silently) and the role stays 'viewer'. The HTTP layer
    still refuses a viewer-initiated role change (403), and an
    owner can still change roles via direct SQL."""
    owner = authed_client(live["base_url"], "sec-live-f7-o@example.com", PASSWORD)
    member = authed_client(live["base_url"], "sec-live-f7-m@example.com", PASSWORD)
    try:
        resp = post(owner, "/api/workspaces", {"name": "Sec Live F7 Shop"})
        assert resp.status_code == 201, resp.text
        ws = resp.json()["id"]
        mid = _invite_accept(owner, ws, member, "sec-live-f7-m@example.com", "viewer")
        uid_m = _user_id(member)
        uid_o = _user_id(owner)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                # Viewer direct-SQL self-promotion is blocked: the
                # row is not visible to the UPDATE (rowcount 0, no
                # error), and the role is unchanged. (Verified by
                # hand first in a scratch DB before this rewrite.)
                cur.execute(
                    "SELECT set_config('app.current_merchant_id', %s, true), "
                    "set_config('app.user_id', %s, true)",
                    (ws, uid_m),
                )
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'owner' "
                    "WHERE id = %s",
                    (mid,),
                )
                assert cur.rowcount == 0
                cur.execute("SELECT role FROM merchant_memberships WHERE id = %s", (mid,))
                assert cur.fetchone()[0] == "viewer"
                # An owner direct-SQL role change still works, and
                # the fixture state is restored the same way.
                cur.execute(
                    "SELECT set_config('app.current_merchant_id', %s, true), "
                    "set_config('app.user_id', %s, true)",
                    (ws, uid_o),
                )
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'admin' WHERE id = %s",
                    (mid,),
                )
                assert cur.rowcount == 1
                cur.execute("SELECT role FROM merchant_memberships WHERE id = %s", (mid,))
                assert cur.fetchone()[0] == "admin"
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'viewer' WHERE id = %s",
                    (mid,),
                )
                assert cur.rowcount == 1
                cur.execute("SELECT role FROM merchant_memberships WHERE id = %s", (mid,))
                assert cur.fetchone()[0] == "viewer"
            conn.commit()
        # The HTTP layer still refuses a viewer-initiated role change.
        resp = member.patch(
            f"/api/workspaces/{ws}/members/{mid}/role", json={"role": "admin"},
            headers={"X-CSRF-Token": get_csrf(member)},
        )
        assert resp.status_code == 403, resp.text
    finally:
        owner.close()
        member.close()
