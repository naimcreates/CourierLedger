"""Phase 1.1 role-scoped RLS tests — live PostgreSQL, runtime role.

Run ONLY with the runtime role:
  CL_LIVE_PG_PASSWORD=<password from gitignored backend/.env>
  CL_LIVE_PG_ROLE=courierledger_runtime
  .venv/bin/python -m pytest tests/test_rls_roles_live_pg.py -q

Proves, live, the Phase 1.1 (B-F7) DB-layer close-out:
- public.app_current_user_role() returns NULL with no context (or
  for an invited / non-member caller) and the caller's ACTIVE role
  with context; PUBLIC cannot EXECUTE it, the runtime role can.
- Cross-tenant membership UPDATE/INSERT is blocked (rowcount 0 /
  InsufficientPrivilege), and the other workspace's row is unchanged.
- A viewer/member cannot INSERT a membership row; an admin can.
- A viewer/member cannot self-promote via direct SQL (rowcount 0,
  role unchanged) — the B-F7 hole, closed.
- An admin can change a member's role via direct SQL but cannot set
  role='owner' (WITH CHECK failure raises InsufficientPrivilege).
- An owner can change roles via direct SQL, including a
  transfer-style swap in both directions.
- A transfer-style swap works with the demotion flushed FIRST
  (the order SQLAlchemy produces for transfer_owner) and with a
  second owner present: the guard records the caller's pre-change
  role (transaction-start snapshot) on the first role-changing
  UPDATE, so the demotion does not turn the caller into an admin
  mid-swap. An admin cannot manufacture an 'owner' snapshot by
  touching their own row or by making a legitimate role change
  first -- the grant is still refused.
- An invited user cannot smuggle a role change into an
  accept-shaped UPDATE (the fn_membership_role_guard trigger raises),
  but a status-only accept-shaped UPDATE works (role unchanged).
- An invited user can still accept via HTTP end-to-end after the
  policy change (the self-accept arm is intact).
- An admin can revoke and reactivate (revoked -> invited, with a
  role change) via direct SQL.

Financial-table role-scoping (workspace_settings, orders,
shipments, etc.) is deliberately NOT covered here: it was evaluated
and deferred in Phase 1.1 (see rls.sql, workspace_settings
comment) because the live-test fixtures seed those tables by
direct SQL with merchant context only and no app.user_id.
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
    not (live_pg_enabled() and os.environ.get("CL_LIVE_PG_ROLE") == "courierledger_runtime"),
    reason="Phase 1.1 live RLS tests need CL_LIVE_PG_PASSWORD + CL_LIVE_PG_ROLE=courierledger_runtime",
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


def _invite_accept(owner: httpx.Client, ws: str, invitee: httpx.Client,
                   email: str, role: str) -> str:
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": email, "role": role})
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    resp = post(invitee, f"/api/workspaces/{ws}/members/{mid}/accept", {})
    assert resp.status_code == 200, resp.text
    return mid


def _set_ctx(cur, ws: str, uid: str) -> None:
    cur.execute(
        "SELECT set_config('app.current_merchant_id', %s, true), "
        "set_config('app.user_id', %s, true)",
        (ws, uid),
    )


def _role_status(cur, ws: str, uid: str, mid: str) -> tuple[str, str]:
    _set_ctx(cur, ws, uid)
    cur.execute(
        "SELECT role, status FROM merchant_memberships WHERE id = %s", (mid,))
    row = cur.fetchone()
    assert row is not None, mid
    return row[0], row[1]


def _setup_ws(owner: httpx.Client, name: str) -> str:
    resp = post(owner, "/api/workspaces", {"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_role_helper_values_live(live):
    owner = authed_client(live["base_url"], "rls-role-owner@example.com", PASSWORD)
    viewer = authed_client(live["base_url"], "rls-role-viewer@example.com", PASSWORD)
    invited = authed_client(live["base_url"], "rls-role-invited@example.com", PASSWORD)
    outsider = authed_client(live["base_url"], "rls-role-outsider@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner, "RLS Role Helper Shop")
        _invite_accept(owner, ws, viewer, "rls-role-viewer@example.com", "viewer")
        # Invited but not yet accepted: invite, do not accept.
        resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                    {"email": "rls-role-invited@example.com", "role": "member"})
        assert resp.status_code == 201, resp.text
        uid_o, uid_v = _user_id(owner), _user_id(viewer)
        uid_i, uid_x = _user_id(invited), _user_id(outsider)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                _set_ctx(cur, ws, uid_o)
                cur.execute("SELECT public.app_current_user_role()")
                assert cur.fetchone()[0] == "owner"
                _set_ctx(cur, ws, uid_v)
                cur.execute("SELECT public.app_current_user_role()")
                assert cur.fetchone()[0] == "viewer"
                _set_ctx(cur, ws, uid_i)
                cur.execute("SELECT public.app_current_user_role()")
                assert cur.fetchone()[0] is None  # invited, not active
                _set_ctx(cur, ws, uid_x)
                cur.execute("SELECT public.app_current_user_role()")
                assert cur.fetchone()[0] is None  # non-member
                # No context at all -> NULL (fail-closed).
                cur.execute(
                    "SELECT set_config('app.current_merchant_id', '', true), "
                    "set_config('app.user_id', '', true)")
                cur.execute("SELECT public.app_current_user_role()")
                assert cur.fetchone()[0] is None
                # PUBLIC cannot EXECUTE it; the runtime role can
                # (B-F17 pattern for the new helper).
                cur.execute(
                    "SELECT has_function_privilege('public', %s, 'EXECUTE')",
                    ("public.app_current_user_role()",),
                )
                assert cur.fetchone()[0] is False
                cur.execute(
                    "SELECT has_function_privilege('courierledger_runtime', %s, 'EXECUTE')",
                    ("public.app_current_user_role()",),
                )
                assert cur.fetchone()[0] is True
    finally:
        owner.close()
        viewer.close()
        invited.close()
        outsider.close()


def test_cross_tenant_membership_update_blocked_live(live):
    a = authed_client(live["base_url"], "rls-xt-a@example.com", PASSWORD)
    b = authed_client(live["base_url"], "rls-xt-b@example.com", PASSWORD)
    try:
        ws_a = _setup_ws(a, "RLS Cross-Tenant Shop A")
        ws_b = _setup_ws(b, "RLS Cross-Tenant Shop B")
        # B's own member row, read back as B (owner context).
        uid_a, uid_b = _user_id(a), _user_id(b)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                _set_ctx(cur, ws_b, uid_b)
                cur.execute(
                    "SELECT id, role FROM merchant_memberships "
                    "WHERE merchant_id = %s AND user_id = %s", (ws_b, uid_b))
                mid_b, role_b = cur.fetchone()
                # Owner of A, in A's context, cannot touch B's row.
                _set_ctx(cur, ws_a, uid_a)
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'viewer' WHERE id = %s",
                    (mid_b,),
                )
                assert cur.rowcount == 0
                # Cross-tenant INSERT is rejected, not silently dropped.
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(
                        "INSERT INTO merchant_memberships "
                        "(id, merchant_id, user_id, role, status, "
                        " invited_by_user_id, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), %s, %s, 'member', 'invited', %s, now(), now())",
                        (ws_b, uid_a, uid_a),
                    )
                conn.rollback()
                # B's row is unchanged.
                assert _role_status(cur, ws_b, uid_b, mid_b) == (role_b, "active")
    finally:
        a.close()
        b.close()


def test_viewer_and_member_cannot_insert_membership_live(live):
    owner = authed_client(live["base_url"], "rls-ins-owner@example.com", PASSWORD)
    viewer = authed_client(live["base_url"], "rls-ins-viewer@example.com", PASSWORD)
    member = authed_client(live["base_url"], "rls-ins-member@example.com", PASSWORD)
    admin = authed_client(live["base_url"], "rls-ins-admin@example.com", PASSWORD)
    newbie = authed_client(live["base_url"], "rls-ins-newbie@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner, "RLS Insert Shop")
        _invite_accept(owner, ws, viewer, "rls-ins-viewer@example.com", "viewer")
        _invite_accept(owner, ws, member, "rls-ins-member@example.com", "member")
        _invite_accept(owner, ws, admin, "rls-ins-admin@example.com", "admin")
        uid_o = _user_id(owner)
        uid_v, uid_m, uid_a = _user_id(viewer), _user_id(member), _user_id(admin)
        uid_n = _user_id(newbie)
        insert_sql = (
            "INSERT INTO merchant_memberships "
            "(id, merchant_id, user_id, role, status, "
            " invited_by_user_id, created_at, updated_at) "
            "VALUES (gen_random_uuid(), %s, %s, 'member', 'invited', %s, now(), now())"
        )
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                for uid in (uid_v, uid_m):
                    _set_ctx(cur, ws, uid)
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        cur.execute(insert_sql, (ws, uid_n, uid))
                    conn.rollback()
                # An admin CAN insert (the invite path at the DB layer).
                _set_ctx(cur, ws, uid_a)
                cur.execute(insert_sql, (ws, uid_n, uid_a))
                assert cur.rowcount == 1
                cur.execute(
                    "SELECT role, status FROM merchant_memberships "
                    "WHERE merchant_id = %s AND user_id = %s", (ws, uid_n))
                assert cur.fetchone() == ("member", "invited")
            conn.commit()
        # The owner sees the admin-inserted invitation over HTTP.
        resp = owner.get(f"/api/workspaces/{ws}/members")
        assert resp.status_code == 200, resp.text
        assert any(m["user_id"] == uid_n and m["status"] == "invited"
                   for m in resp.json())
    finally:
        owner.close()
        viewer.close()
        member.close()
        admin.close()
        newbie.close()


def test_member_cannot_self_promote_via_direct_sql_live(live):
    owner = authed_client(live["base_url"], "rls-self-o@example.com", PASSWORD)
    member = authed_client(live["base_url"], "rls-self-m@example.com", PASSWORD)
    viewer = authed_client(live["base_url"], "rls-self-v@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner, "RLS Self-Promote Shop")
        mid_m = _invite_accept(owner, ws, member, "rls-self-m@example.com", "member")
        mid_v = _invite_accept(owner, ws, viewer, "rls-self-v@example.com", "viewer")
        uid_o, uid_m, uid_v = _user_id(owner), _user_id(member), _user_id(viewer)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                # Member tries member -> admin on their own row.
                _set_ctx(cur, ws, uid_m)
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'admin' WHERE id = %s",
                    (mid_m,),
                )
                assert cur.rowcount == 0
                assert _role_status(cur, ws, uid_o, mid_m) == ("member", "active")
                # Viewer tries viewer -> owner on their own row.
                _set_ctx(cur, ws, uid_v)
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'owner' WHERE id = %s",
                    (mid_v,),
                )
                assert cur.rowcount == 0
                assert _role_status(cur, ws, uid_o, mid_v) == ("viewer", "active")
                # A member cannot touch ANOTHER member's row either.
                _set_ctx(cur, ws, uid_m)
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'member' WHERE id = %s",
                    (mid_v,),
                )
                assert cur.rowcount == 0
    finally:
        owner.close()
        member.close()
        viewer.close()


def test_admin_can_change_role_but_cannot_create_owner_live(live):
    owner = authed_client(live["base_url"], "rls-adm-o@example.com", PASSWORD)
    admin = authed_client(live["base_url"], "rls-adm-a@example.com", PASSWORD)
    member = authed_client(live["base_url"], "rls-adm-m@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner, "RLS Admin Role Shop")
        _invite_accept(owner, ws, admin, "rls-adm-a@example.com", "admin")
        mid = _invite_accept(owner, ws, member, "rls-adm-m@example.com", "member")
        uid_o, uid_a = _user_id(owner), _user_id(admin)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                # Admin member -> admin works.
                _set_ctx(cur, ws, uid_a)
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'admin' WHERE id = %s",
                    (mid,),
                )
                assert cur.rowcount == 1
                assert _role_status(cur, ws, uid_o, mid) == ("admin", "active")
            # Commit the admin change before the grant attempt: the
            # rollback after the expected error below would otherwise
            # undo it too, and the post-error assertion would see
            # 'member' again.
            conn.commit()
            with conn.cursor() as cur:
                # Admin member -> owner is rejected (the role-guard
                # trigger raises -- the admin's transaction-start
                # snapshot is 'admin', so the owner-grant check
                # fails), and the row is unchanged.
                _set_ctx(cur, ws, uid_a)
                with pytest.raises(psycopg.errors.RaiseException):
                    cur.execute(
                        "UPDATE merchant_memberships SET role = 'owner' WHERE id = %s",
                        (mid,),
                    )
                conn.rollback()
                assert _role_status(cur, ws, uid_o, mid) == ("admin", "active")
                # Restore the fixture state (owner can set it back).
                _set_ctx(cur, ws, uid_o)
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'member' WHERE id = %s",
                    (mid,),
                )
                assert cur.rowcount == 1
            conn.commit()
    finally:
        owner.close()
        admin.close()
        member.close()


def test_owner_can_change_roles_and_transfer_style_swap_live(live):
    owner = authed_client(live["base_url"], "rls-swap-o@example.com", PASSWORD)
    member = authed_client(live["base_url"], "rls-swap-m@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner, "RLS Swap Shop")
        mid_m = _invite_accept(owner, ws, member, "rls-swap-m@example.com", "member")
        uid_o, uid_m = _user_id(owner), _user_id(member)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                _set_ctx(cur, ws, uid_o)
                cur.execute("SELECT id FROM merchant_memberships "
                            "WHERE merchant_id = %s AND user_id = %s", (ws, uid_o))
                mid_o = cur.fetchone()[0]
                # Owner changes a member's role.
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'viewer' WHERE id = %s",
                    (mid_m,),
                )
                assert cur.rowcount == 1
                assert _role_status(cur, ws, uid_o, mid_m) == ("viewer", "active")
                # Transfer-style swap, step 1: member -> owner.
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'owner' WHERE id = %s",
                    (mid_m,),
                )
                assert cur.rowcount == 1
                # Step 2: old owner -> admin, in the same transaction.
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'admin' WHERE id = %s",
                    (mid_o,),
                )
                assert cur.rowcount == 1
            conn.commit()
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                assert _role_status(cur, ws, uid_m, mid_m) == ("owner", "active")
                assert _role_status(cur, ws, uid_m, mid_o) == ("admin", "active")
                # Swap back, now with the NEW owner as the caller.
                _set_ctx(cur, ws, uid_m)
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'owner' WHERE id = %s",
                    (mid_o,),
                )
                assert cur.rowcount == 1
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'admin' WHERE id = %s",
                    (mid_m,),
                )
                assert cur.rowcount == 1
            conn.commit()
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                assert _role_status(cur, ws, uid_o, mid_o) == ("owner", "active")
                assert _role_status(cur, ws, uid_o, mid_m) == ("admin", "active")
    finally:
        owner.close()
        member.close()


def test_owner_transfer_demotion_first_and_two_owner_live(live):
    """Transfer works in BOTH flush orders, including with a second
    owner present: the guard records the caller's pre-change role
    (transaction-start snapshot) on the first role-changing UPDATE,
    so a demotion flushed before the promotion does not turn the
    caller into an admin mid-swap. Verified by hand in a scratch DB
    before this test was written (the live-role-only design failed
    transfer_owner through the real service layer)."""
    owner1 = authed_client(live["base_url"], "rls-swap2-o1@example.com", PASSWORD)
    owner2 = authed_client(live["base_url"], "rls-swap2-o2@example.com", PASSWORD)
    member = authed_client(live["base_url"], "rls-swap2-m@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner1, "RLS Swap Demotion-First Shop")
        uid_o1, uid_o2, uid_m = _user_id(owner1), _user_id(owner2), _user_id(member)
        # Second owner, seeded by direct SQL as the first owner
        # (the INSERT policy's manager arm allows it).
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                _set_ctx(cur, ws, uid_o1)
                cur.execute(
                    "INSERT INTO merchant_memberships "
                    "(id, merchant_id, user_id, role, status, "
                    " invited_by_user_id, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), %s, %s, 'owner', 'active', %s, now(), now())",
                    (ws, uid_o2, uid_o1),
                )
                assert cur.rowcount == 1
            conn.commit()
        mid_m = _invite_accept(owner1, ws, member, "rls-swap2-m@example.com", "member")
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                _set_ctx(cur, ws, uid_o1)
                cur.execute("SELECT id FROM merchant_memberships "
                            "WHERE merchant_id = %s AND user_id = %s", (ws, uid_o1))
                mid_o1 = cur.fetchone()[0]
                # DEMOTION FIRST (the order SQLAlchemy's flush
                # produces for transfer_owner): owner1 steps down,
                # then the member is promoted -- both must succeed.
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'admin' WHERE id = %s",
                    (mid_o1,),
                )
                assert cur.rowcount == 1
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'owner' WHERE id = %s",
                    (mid_m,),
                )
                assert cur.rowcount == 1
            conn.commit()
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                assert _role_status(cur, ws, uid_m, mid_o1) == ("admin", "active")
                assert _role_status(cur, ws, uid_m, mid_m) == ("owner", "active")
                # Swap back, promotion first this time.
                _set_ctx(cur, ws, uid_m)
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'owner' WHERE id = %s",
                    (mid_o1,),
                )
                assert cur.rowcount == 1
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'admin' WHERE id = %s",
                    (mid_m,),
                )
                assert cur.rowcount == 1
            conn.commit()
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                assert _role_status(cur, ws, uid_o1, mid_o1) == ("owner", "active")
                assert _role_status(cur, ws, uid_o1, mid_m) == ("admin", "active")
    finally:
        owner1.close()
        owner2.close()
        member.close()


def test_admin_cannot_manufacture_owner_snapshot_live(live):
    """An admin cannot work around the owner-grant check by
    touching their own row first: the snapshot is recorded only on
    a ROLE-changing update, from the live role -- a non-role update
    (updated_at) or a legitimate role change earlier in the same
    transaction leaves the snapshot 'admin', and the grant is
    still refused."""
    owner = authed_client(live["base_url"], "rls-snap-o@example.com", PASSWORD)
    admin = authed_client(live["base_url"], "rls-snap-a@example.com", PASSWORD)
    member = authed_client(live["base_url"], "rls-snap-m@example.com", PASSWORD)
    member2 = authed_client(live["base_url"], "rls-snap-m2@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner, "RLS Snapshot Shop")
        _invite_accept(owner, ws, admin, "rls-snap-a@example.com", "admin")
        mid = _invite_accept(owner, ws, member, "rls-snap-m@example.com", "member")
        mid2 = _invite_accept(owner, ws, member2, "rls-snap-m2@example.com", "member")
        uid_o, uid_a = _user_id(owner), _user_id(admin)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                _set_ctx(cur, ws, uid_a)
                cur.execute("SELECT id FROM merchant_memberships "
                            "WHERE merchant_id = %s AND user_id = %s", (ws, uid_a))
                mid_a = cur.fetchone()[0]
                # Touch own row (no role change), then try the grant.
                cur.execute(
                    "UPDATE merchant_memberships SET updated_at = now() WHERE id = %s",
                    (mid_a,),
                )
                assert cur.rowcount == 1
                with pytest.raises(psycopg.errors.RaiseException):
                    cur.execute(
                        "UPDATE merchant_memberships SET role = 'owner' WHERE id = %s",
                        (mid,),
                    )
                conn.rollback()
                # A legitimate role change first, then the grant:
                # still refused.
                _set_ctx(cur, ws, uid_a)
                cur.execute(
                    "UPDATE merchant_memberships SET role = 'viewer' WHERE id = %s",
                    (mid2,),
                )
                assert cur.rowcount == 1
                with pytest.raises(psycopg.errors.RaiseException):
                    cur.execute(
                        "UPDATE merchant_memberships SET role = 'owner' WHERE id = %s",
                        (mid,),
                    )
                conn.rollback()
                assert _role_status(cur, ws, uid_o, mid) == ("member", "active")
                assert _role_status(cur, ws, uid_o, mid2) == ("member", "active")
    finally:
        owner.close()
        admin.close()
        member.close()
        member2.close()


def test_invited_user_cannot_smuggle_role_change_live(live):
    owner = authed_client(live["base_url"], "rls-smug-o@example.com", PASSWORD)
    invitee = authed_client(live["base_url"], "rls-smug-i@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner, "RLS Smuggle Shop")
        resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                    {"email": "rls-smug-i@example.com", "role": "viewer"})
        assert resp.status_code == 201, resp.text
        mid = resp.json()["id"]
        uid_o, uid_i = _user_id(owner), _user_id(invitee)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                # Smuggle: role='owner' + status='active' in one
                # UPDATE. The policy arms both pass (own invited
                # row -> active), so the trigger must stop it.
                _set_ctx(cur, ws, uid_i)
                with pytest.raises(psycopg.errors.RaiseException):
                    cur.execute(
                        "UPDATE merchant_memberships "
                        "SET role = 'owner', status = 'active' WHERE id = %s",
                        (mid,),
                    )
                conn.rollback()
                assert _role_status(cur, ws, uid_o, mid) == ("viewer", "invited")
                # The clean accept-shaped UPDATE (status only) works
                # and leaves the role untouched.
                _set_ctx(cur, ws, uid_i)
                cur.execute(
                    "UPDATE merchant_memberships SET status = 'active' WHERE id = %s",
                    (mid,),
                )
                assert cur.rowcount == 1
            conn.commit()
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                assert _role_status(cur, ws, uid_o, mid) == ("viewer", "active")
    finally:
        owner.close()
        invitee.close()


def test_invited_user_accepts_via_http_live(live):
    owner = authed_client(live["base_url"], "rls-acc-o@example.com", PASSWORD)
    invitee = authed_client(live["base_url"], "rls-acc-i@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner, "RLS Accept Shop")
        mid = _invite_accept(owner, ws, invitee, "rls-acc-i@example.com", "viewer")
        # The accept went through the self-accept policy arm + the
        # role guard (role unchanged): the invitee is active, still
        # a viewer, and sees the workspace.
        assert invitee.get(f"/api/workspaces/{ws}").status_code == 200
        resp = owner.get(f"/api/workspaces/{ws}/members")
        assert resp.status_code == 200, resp.text
        mine = [m for m in resp.json() if m["id"] == mid]
        assert len(mine) == 1
        assert mine[0]["status"] == "active"
        assert mine[0]["role"] == "viewer"
        # Idempotent accept still works.
        resp = post(invitee, f"/api/workspaces/{ws}/members/{mid}/accept", {})
        assert resp.status_code == 200, resp.text
    finally:
        owner.close()
        invitee.close()


def test_admin_revoke_and_reactivate_via_direct_sql_live(live):
    owner = authed_client(live["base_url"], "rls-rev-o@example.com", PASSWORD)
    admin = authed_client(live["base_url"], "rls-rev-a@example.com", PASSWORD)
    member = authed_client(live["base_url"], "rls-rev-m@example.com", PASSWORD)
    try:
        ws = _setup_ws(owner, "RLS Revoke Shop")
        _invite_accept(owner, ws, admin, "rls-rev-a@example.com", "admin")
        mid = _invite_accept(owner, ws, member, "rls-rev-m@example.com", "member")
        uid_o, uid_a = _user_id(owner), _user_id(admin)
        with _runtime_conn() as conn:
            with conn.cursor() as cur:
                # Revoke (status only; role unchanged, so the guard
                # does not fire).
                _set_ctx(cur, ws, uid_a)
                cur.execute(
                    "UPDATE merchant_memberships SET status = 'revoked' WHERE id = %s",
                    (mid,),
                )
                assert cur.rowcount == 1
                assert _role_status(cur, ws, uid_o, mid) == ("member", "revoked")
                # Reactivate (revoked -> invited) with a role change,
                # the invite_member() reactivation path at the DB layer.
                cur.execute(
                    "UPDATE merchant_memberships "
                    "SET status = 'invited', role = 'admin' WHERE id = %s",
                    (mid,),
                )
                assert cur.rowcount == 1
                assert _role_status(cur, ws, uid_o, mid) == ("admin", "invited")
                # Restore to an active member (owner context).
                _set_ctx(cur, ws, uid_o)
                cur.execute(
                    "UPDATE merchant_memberships "
                    "SET status = 'active', role = 'member' WHERE id = %s",
                    (mid,),
                )
                assert cur.rowcount == 1
            conn.commit()
    finally:
        owner.close()
        admin.close()
        member.close()
