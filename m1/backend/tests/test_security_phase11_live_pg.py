"""Phase 1.1 security tests — live PostgreSQL, runtime role.

Run ONLY with the runtime role:
  CL_LIVE_PG_PASSWORD=<password from gitignored backend/.env>
  CL_LIVE_PG_ROLE=courierledger_runtime
  .venv/bin/python -m pytest tests/test_security_phase11_live_pg.py -q

Proves, live (same scenarios as tests/test_security_phase11.py,
under the runtime role with real RLS):
- B-F15 membership revocation + existing session: a single-workspace
  user loses every session on revoke (401 on /me); a multi-workspace
  user keeps their session for their other workspace (revoked one
  404s).
- B-F11 owner transfer + last-owner protection still hold with
  step-up in place: transfer with a fresh login works; transfer with
  a 16-minute-old session is 403 until step-up re-verifies the
  password; revoking the last owner is still 422.
- B-F10 audit immutability: UPDATE/DELETE on audit_logs is rejected
  for the runtime role (no grant + append-only trigger).
- B-F10 audit tenant isolation: under B's merchant context, none of
  A's audit rows are visible via direct SQL.
- B-F11 session cap live: 11 logins via HTTP, the oldest revoked.

Do NOT run these against SQLite: the live file must run against real
PostgreSQL under CL_LIVE_PG_ROLE=courierledger_runtime.
"""

from __future__ import annotations

import os
import secrets

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
    not (live_pg_enabled() and os.environ.get("CL_LIVE_PG_ROLE") == "courierledger_runtime"),
    reason="Phase 1.1 live tests need CL_LIVE_PG_PASSWORD + CL_LIVE_PG_ROLE=courierledger_runtime",
)

PASSWORD = secrets.token_urlsafe(16)  # Phase 1 (A-F5): random per run


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


def _workspace(client: httpx.Client, name: str = "P11 Live Shop") -> str:
    resp = post(client, "/api/workspaces", {"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _invite_accept(owner: httpx.Client, ws: str, invitee: httpx.Client,
                   email: str, role: str = "member") -> str:
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": email, "role": role})
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    resp = post(invitee, f"/api/workspaces/{ws}/members/{mid}/accept", {})
    assert resp.status_code == 200, resp.text
    return mid


def _member_user_id(owner: httpx.Client, ws: str, email: str) -> str:
    resp = owner.get(f"/api/workspaces/{ws}/members")
    assert resp.status_code == 200, resp.text
    for m in resp.json():
        if m["email"] == email:
            return m["user_id"]
    raise AssertionError(f"no member {email} in {ws}")


def _age_session(user_id: str, *, minutes: int) -> None:
    """Direct-SQL probe (same pattern as the Phase 1 live expiry
    test): make a session row look old. The runtime role has UPDATE
    on user_sessions, so this works; it only ages the row, it does
    not forge a step-up verification."""
    with _runtime_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_sessions SET created_at = now() - (%s || ' minutes')::interval "
                "WHERE user_id = %s",
                (str(minutes), user_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# B-F15 — revoked-member sessions, live
# ---------------------------------------------------------------------------

def test_revoke_single_workspace_session_dead_live(live):
    owner = authed_client(live["base_url"], "p11-live-rev-owner@example.com", PASSWORD)
    member = authed_client(live["base_url"], "p11-live-rev-member@example.com", PASSWORD)
    try:
        ws = _workspace(owner)
        mid = _invite_accept(owner, ws, member, "p11-live-rev-member@example.com")
        assert member.get(f"/api/workspaces/{ws}").status_code == 200
        resp = post(owner, f"/api/workspaces/{ws}/members/{mid}/revoke", {})
        assert resp.status_code == 200, resp.text
        # Single-workspace user: the session is revoked entirely.
        assert member.get("/api/auth/me").status_code == 401
        assert member.get(f"/api/workspaces/{ws}").status_code == 401
        # The account still exists: log back in and the revoked
        # membership still denies the workspace (404).
        from tests.live_pg import login

        login(member, "p11-live-rev-member@example.com", PASSWORD)
        assert member.get(f"/api/workspaces/{ws}").status_code == 404
    finally:
        owner.close()
        member.close()


def test_revoke_multi_workspace_session_survives_live(live):
    owner_a = authed_client(live["base_url"], "p11-live-multi-ownera@example.com", PASSWORD)
    owner_b = authed_client(live["base_url"], "p11-live-multi-ownerb@example.com", PASSWORD)
    member = authed_client(live["base_url"], "p11-live-multi-member@example.com", PASSWORD)
    try:
        ws_a = _workspace(owner_a, "P11 Live Multi A")
        ws_b = _workspace(owner_b, "P11 Live Multi B")
        mid_a = _invite_accept(owner_a, ws_a, member, "p11-live-multi-member@example.com")
        _invite_accept(owner_b, ws_b, member, "p11-live-multi-member@example.com")
        assert member.get(f"/api/workspaces/{ws_a}").status_code == 200
        assert member.get(f"/api/workspaces/{ws_b}").status_code == 200
        resp = post(owner_a, f"/api/workspaces/{ws_a}/members/{mid_a}/revoke", {})
        assert resp.status_code == 200, resp.text
        # Session survives for the other workspace; the revoked one 404s.
        assert member.get("/api/auth/me").status_code == 200
        assert member.get(f"/api/workspaces/{ws_b}").status_code == 200
        assert member.get(f"/api/workspaces/{ws_a}").status_code == 404
    finally:
        owner_a.close()
        owner_b.close()
        member.close()


# ---------------------------------------------------------------------------
# B-F11 — step-up for owner transfer + last-owner guard, live
# ---------------------------------------------------------------------------

def test_transfer_owner_step_up_live(live):
    owner = authed_client(live["base_url"], "p11-live-step-owner@example.com", PASSWORD)
    member = authed_client(live["base_url"], "p11-live-step-member@example.com", PASSWORD)
    try:
        ws = _workspace(owner)
        _invite_accept(owner, ws, member, "p11-live-step-member@example.com")
        target = _member_user_id(owner, ws, "p11-live-step-member@example.com")

        # Fresh login: transfer works with no extra prompt.
        resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                    {"user_id": target})
        assert resp.status_code == 200, resp.text

        # Transfer it back (the new owner's session is also fresh).
        back = _member_user_id(member, ws, "p11-live-step-owner@example.com")
        resp = post(member, f"/api/workspaces/{ws}/members/transfer-owner",
                    {"user_id": back})
        assert resp.status_code == 200, resp.text

        # Age the owner's session 16 minutes: transfer is 403 now.
        _age_session(_user_id(owner), minutes=16)
        resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                    {"user_id": target})
        assert resp.status_code == 403, resp.text
        assert "Step-up authentication required" in resp.text

        # Step-up with the wrong password is 401; with the right one
        # it is 200 and the transfer succeeds.
        resp = post(owner, "/api/auth/step-up", {"password": "wrong-password-123"})
        assert resp.status_code == 401, resp.text
        resp = post(owner, "/api/auth/step-up", {"password": PASSWORD})
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"] == "Step-up verified."
        resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                    {"user_id": target})
        assert resp.status_code == 200, resp.text

        # The last-owner guard still holds after the transfer: the
        # new sole owner cannot be revoked.
        members = owner.get(f"/api/workspaces/{ws}/members").json()
        new_owner = next(m for m in members if m["email"] == "p11-live-step-member@example.com")
        resp = post(member, f"/api/workspaces/{ws}/members/{new_owner['id']}/revoke", {})
        assert resp.status_code == 422, resp.text
        assert "last_active_owner" in resp.text
    finally:
        owner.close()
        member.close()


# ---------------------------------------------------------------------------
# B-F10 — audit immutability + tenant isolation, live
# ---------------------------------------------------------------------------

def test_audit_immutability_live(live):
    a = authed_client(live["base_url"], "p11-live-audit-owner@example.com", PASSWORD)
    try:
        ws = _workspace(a)
        # Registering + creating the workspace wrote audit rows
        # (auth.register); the invite below writes a tenant row.
        member = authed_client(live["base_url"], "p11-live-audit-member@example.com", PASSWORD)
        try:
            _invite_accept(a, ws, member, "p11-live-audit-member@example.com")
        finally:
            member.close()
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, action FROM audit_logs "
                    "WHERE merchant_id = %s AND action = 'members.invited' LIMIT 1",
                    (ws,),
                )
                found = cur.fetchone()
                # The SELECT above runs without a merchant context, so
                # RLS hides tenant rows; re-run it under A's context.
                if found is None:
                    cur.execute(
                        "SELECT set_config('app.current_merchant_id', %s, true)",
                        (ws,),
                    )
                    cur.execute(
                        "SELECT id, action FROM audit_logs "
                        "WHERE merchant_id = %s AND action = 'members.invited' LIMIT 1",
                        (ws,),
                    )
                    found = cur.fetchone()
            assert found is not None, "no members.invited audit row found"
            row_id, action = found
            conn.rollback()
        # UPDATE and DELETE are rejected for the runtime role (no
        # grant on audit_logs) -- the append-only trigger is the
        # backstop behind the grant. The connection is left in an
        # aborted transaction; roll back before reusing it.
        conn = _runtime_conn()
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE audit_logs SET action = 'auth.login_success' WHERE id = %s",
                        (row_id,),
                    )
                conn.commit()
        finally:
            conn.rollback()
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM audit_logs WHERE id = %s", (row_id,))
                conn.commit()
        finally:
            conn.rollback()
            conn.close()
        # The row is unchanged.
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.current_merchant_id', %s, true)", (ws,)
                )
                cur.execute("SELECT action FROM audit_logs WHERE id = %s", (row_id,))
                assert cur.fetchone()[0] == action
            conn.rollback()
    finally:
        a.close()


def test_audit_tenant_isolation_live(live):
    owner_a = authed_client(live["base_url"], "p11-live-iso-ownera@example.com", PASSWORD)
    owner_b = authed_client(live["base_url"], "p11-live-iso-ownerb@example.com", PASSWORD)
    try:
        ws_a = _workspace(owner_a, "P11 Live Iso A")
        ws_b = _workspace(owner_b, "P11 Live Iso B")
        member = authed_client(live["base_url"], "p11-live-iso-member@example.com", PASSWORD)
        try:
            _invite_accept(owner_a, ws_a, member, "p11-live-iso-member@example.com")
        finally:
            member.close()
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                # Under A's merchant context, A's tenant rows are visible.
                cur.execute(
                    "SELECT set_config('app.current_merchant_id', %s, true)", (ws_a,)
                )
                cur.execute(
                    "SELECT count(*) FROM audit_logs WHERE action = 'members.invited'"
                )
                assert cur.fetchone()[0] >= 1
                # Under B's merchant context, NONE of A's rows are visible.
                cur.execute(
                    "SELECT set_config('app.current_merchant_id', %s, true)", (ws_b,)
                )
                cur.execute("SELECT count(*) FROM audit_logs")
                assert cur.fetchone()[0] == 0
            conn.rollback()
    finally:
        owner_a.close()
        owner_b.close()


# ---------------------------------------------------------------------------
# B-F11 — session cap, live
# ---------------------------------------------------------------------------

def test_session_cap_live(live):
    email = "p11-live-cap@example.com"
    clients = []
    tokens = []
    try:
        for _ in range(11):
            c = authed_client(live["base_url"], email, PASSWORD)
            clients.append(c)
            tokens.append(c.cookies.get("cl_session"))
        assert len(set(tokens)) == 11
        # The oldest token is revoked: 401. The newest still works.
        assert clients[0].get("/api/auth/me").status_code == 401
        assert clients[10].get("/api/auth/me").status_code == 200
        assert clients[1].get("/api/auth/me").status_code == 200
        user_id = _user_id(clients[10])
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM user_sessions "
                    "WHERE user_id = %s AND revoked_at IS NULL",
                    (user_id,),
                )
                assert cur.fetchone()[0] == 10
                cur.execute(
                    "SELECT count(*) FROM user_sessions "
                    "WHERE user_id = %s AND revoked_at IS NOT NULL",
                    (user_id,),
                )
                assert cur.fetchone()[0] == 1
            conn.rollback()
    finally:
        for c in clients:
            c.close()
