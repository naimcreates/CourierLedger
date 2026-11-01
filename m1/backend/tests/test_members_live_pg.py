"""Live-PostgreSQL member tests (M1 Milestone 9, part 1).

Runs the real app (uvicorn subprocess) against a scratch PostgreSQL
database with the full schema + RLS + roles applied (see
tests/live_pg.py). Skipped unless CL_LIVE_PG_PASSWORD is set.

Run with the owner role (default) and with the runtime role:
CL_LIVE_PG_PASSWORD=<your-local-db-password>
CL_LIVE_PG_PASSWORD=<your-local-db-password>

Asserts, live:
  * Invite -> accept: the invited user becomes an active member and
    sees the workspace; the members list shows both.
  * Unknown-email invite is 422 (no invented user).
  * Role change member -> admin, and the new admin can invite.
  * Revoke: the revoked user's EXISTING session is denied on
    workspace endpoints immediately (404), while /me still resolves
    (per-workspace denial, not a global logout -- see the service
    docstring).
  * Last-active-owner revoke is 422; revoke succeeds while a second
    (seeded) owner exists, and the last owner is then guarded again.
  * Non-member members-list is 404 (no leak).
  * The CL-20 advisory lock is really taken on PostgreSQL: a second
    session cannot acquire the same per-user lock while the first
    transaction holds it (lock_timeout-bounded), and the documented
    SQL-side key derivation equals the Python derivation.
"""

from __future__ import annotations

import os
import secrets
import uuid

import httpx
import psycopg
import pytest

from app.services.member_service import cl20_lock_key
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


@pytest.fixture(scope="module")
def live():
    with live_pg_server() as server:
        yield server


def _create_workspace(client: httpx.Client, name: str = "Live Members Shop") -> dict:
    resp = post(client, "/api/workspaces", {"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _invite(client: httpx.Client, ws: str, email: str, role: str = "member"):
    return post(client, f"/api/workspaces/{ws}/members/invite",
                {"email": email, "role": role})


def _list_members(client: httpx.Client, ws: str):
    resp = client.get(f"/api/workspaces/{ws}/members")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _owner_conn():
    """psycopg connection as the owner role (table owner; bypasses
    RLS on the NO FORCE merchant_memberships table). Same keyword
    form as test_workspaces_live_pg.py (the SQLAlchemy DSN from
    live_pg.py is not a psycopg conninfo string)."""
    return psycopg.connect(
        f"host={pg_host()} dbname=courierledger_live_test "
        f"user=courierledger password={os.environ['CL_LIVE_PG_PASSWORD']}"
    )


def _seed_owner_membership(ws: str, user_email: str) -> None:
    """Direct seed of a second ACTIVE OWNER membership (no API creates
    owners). Runs as the table-owner role, which bypasses RLS on the
    NO FORCE merchant_memberships table."""
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (user_email,))
            user_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO merchant_memberships "
                "(id, merchant_id, user_id, role, status, invited_by_user_id, "
                " created_at, updated_at) "
                "VALUES (gen_random_uuid(), %s, %s, 'owner', 'active', %s, now(), now())",
                (ws, user_id, user_id),
            )
        conn.commit()


def test_invite_accept_flow_live(live):
    owner = authed_client(live["base_url"], "live-m9-owner@example.com")
    invitee = authed_client(live["base_url"], "live-m9-invitee@example.com")
    ws = _create_workspace(owner)["id"]

    resp = _invite(owner, ws, "live-m9-invitee@example.com", "member")
    assert resp.status_code == 201, resp.text
    invited = resp.json()
    assert invited["status"] == "invited"
    assert invited["role"] == "member"

    resp = post(invitee, f"/api/workspaces/{ws}/members/{invited['id']}/accept", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    # Idempotent accept.
    resp = post(invitee, f"/api/workspaces/{ws}/members/{invited['id']}/accept", {})
    assert resp.status_code == 200, resp.text

    assert invitee.get(f"/api/workspaces/{ws}").status_code == 200
    members = _list_members(owner, ws)
    assert len(members) == 2
    assert {m["status"] for m in members} == {"active"}


def test_unknown_email_invite_live(live):
    owner = authed_client(live["base_url"], "live-m9-owner2@example.com")
    ws = _create_workspace(owner)["id"]
    resp = _invite(owner, ws, "live-m9-nobody@example.com", "member")
    assert resp.status_code == 422, resp.text
    assert "unknown_email" in resp.text
    assert len(_list_members(owner, ws)) == 1


def test_role_change_live(live):
    owner = authed_client(live["base_url"], "live-m9-owner3@example.com")
    member = authed_client(live["base_url"], "live-m9-member3@example.com")
    target = authed_client(live["base_url"], "live-m9-target3@example.com")
    ws = _create_workspace(owner)["id"]

    resp = _invite(owner, ws, "live-m9-member3@example.com", "member")
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    assert post(member, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 200

    resp = member.patch(
        f"/api/workspaces/{ws}/members/{mid}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(member)},
    )
    assert resp.status_code == 403, resp.text  # members cannot self-promote
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{mid}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"

    # The new admin can invite.
    resp = _invite(member, ws, "live-m9-target3@example.com", "viewer")
    assert resp.status_code == 201, resp.text
    assert target.get(f"/api/workspaces/{ws}").status_code == 404  # not accepted yet


def test_revoke_denies_live_session_live(live):
    owner = authed_client(live["base_url"], "live-m9-owner4@example.com")
    member = authed_client(live["base_url"], "live-m9-member4@example.com")
    ws = _create_workspace(owner)["id"]

    resp = _invite(owner, ws, "live-m9-member4@example.com", "member")
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    assert post(member, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 200
    assert member.get(f"/api/workspaces/{ws}").status_code == 200

    resp = post(owner, f"/api/workspaces/{ws}/members/{mid}/revoke", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"

    # The revoked user's EXISTING session is denied on workspace
    # endpoints immediately.
    assert member.get(f"/api/workspaces/{ws}").status_code == 404
    assert member.get(f"/api/workspaces/{ws}/members").status_code == 404
    assert member.get("/api/workspaces").status_code == 200
    assert member.get("/api/workspaces").json() == []
    # ...but the session itself still resolves globally: revocation
    # is per-workspace, not a global logout (documented).
    assert member.get("/api/auth/me").status_code == 200


def test_last_owner_guard_live(live):
    owner_a = authed_client(live["base_url"], "live-m9-ownera5@example.com")
    owner_b = authed_client(live["base_url"], "live-m9-ownerb5@example.com")
    ws = _create_workspace(owner_a)["id"]

    a_membership = _list_members(owner_a, ws)[0]
    assert a_membership["role"] == "owner"
    resp = post(owner_a, f"/api/workspaces/{ws}/members/{a_membership['id']}/revoke", {})
    assert resp.status_code == 422, resp.text
    assert "last_active_owner" in resp.text

    # Seed a second active owner; revoking one owner now succeeds...
    _seed_owner_membership(ws, "live-m9-ownerb5@example.com")
    members = _list_members(owner_a, ws)
    assert len(members) == 2
    b_membership = next(m for m in members if m["email"] == "live-m9-ownerb5@example.com")
    resp = post(owner_a, f"/api/workspaces/{ws}/members/{b_membership['id']}/revoke", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"

    # ...and A is the last active owner again.
    resp = post(owner_a, f"/api/workspaces/{ws}/members/{a_membership['id']}/revoke", {})
    assert resp.status_code == 422, resp.text
    assert "last_active_owner" in resp.text


def test_non_member_members_list_is_404_live(live):
    owner = authed_client(live["base_url"], "live-m9-owner6@example.com")
    stranger = authed_client(live["base_url"], "live-m9-stranger6@example.com")
    ws = _create_workspace(owner)["id"]
    assert stranger.get(f"/api/workspaces/{ws}/members").status_code == 404
    assert post(stranger, f"/api/workspaces/{ws}/members/invite",
                {"email": "x@example.com", "role": "member"}).status_code == 404


def _transfer(client: httpx.Client, ws: str, user_id: str):
    return post(client, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": user_id})


def _accept(client: httpx.Client, ws: str, membership_id: str):
    resp = post(client, f"/api/workspaces/{ws}/members/{membership_id}/accept", {})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_transfer_owner_live(live):
    """Owner transfer end-to-end on real PostgreSQL, under the
    CL-20 locks and RLS: the target becomes the only owner, the old
    owner becomes an admin, and the last-owner guard still holds
    afterwards (the new sole owner cannot be revoked)."""
    owner = authed_client(live["base_url"], "live-m10-owner@example.com")
    member = authed_client(live["base_url"], "live-m10-member@example.com")
    ws = _create_workspace(owner)["id"]

    resp = _invite(owner, ws, "live-m10-member@example.com", "member")
    assert resp.status_code == 201, resp.text
    _accept(member, ws, resp.json()["id"])
    target = next(m for m in _list_members(owner, ws)
                  if m["email"] == "live-m10-member@example.com")
    assert target["role"] == "member"

    resp = _transfer(owner, ws, target["user_id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == target["user_id"]
    assert resp.json()["role"] == "owner"

    members = _list_members(owner, ws)
    owners = [m for m in members if m["role"] == "owner"]
    assert len(owners) == 1
    assert owners[0]["email"] == "live-m10-member@example.com"
    old_owner = next(m for m in members if m["email"] == "live-m10-owner@example.com")
    assert old_owner["role"] == "admin"
    assert old_owner["status"] == "active"

    # The demoted owner (now admin) can no longer transfer.
    resp = _transfer(owner, ws, target["user_id"])
    assert resp.status_code == 403, resp.text

    # Last-owner guard still holds after the transfer: revoking the
    # new sole owner is 422, from either side.
    new_owner_row = next(m for m in members if m["email"] == "live-m10-member@example.com")
    resp = post(owner, f"/api/workspaces/{ws}/members/{new_owner_row['id']}/revoke", {})
    assert resp.status_code == 422, resp.text
    assert "last_active_owner" in resp.text
    resp = post(member, f"/api/workspaces/{ws}/members/{new_owner_row['id']}/revoke", {})
    assert resp.status_code == 422, resp.text
    assert "last_active_owner" in resp.text


def test_transfer_owner_rejections_live(live):
    owner = authed_client(live["base_url"], "live-m10-owner2@example.com")
    member = authed_client(live["base_url"], "live-m10-member2@example.com")
    outsider = authed_client(live["base_url"], "live-m10-outsider2@example.com")
    ws = _create_workspace(owner)["id"]

    resp = _invite(owner, ws, "live-m10-member2@example.com", "member")
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    _accept(member, ws, mid)
    target = next(m for m in _list_members(owner, ws)
                  if m["email"] == "live-m10-member2@example.com")

    # A plain member cannot transfer (403), and nothing changes.
    resp = _transfer(member, ws, target["user_id"])
    assert resp.status_code == 403, resp.text
    # A non-member gets 404 (no leak).
    resp = _transfer(outsider, ws, target["user_id"])
    assert resp.status_code == 404, resp.text
    # An unknown user id and a non-member user id are both 422
    # "target_not_member".
    resp = _transfer(owner, ws, str(uuid.uuid4()))
    assert resp.status_code == 422, resp.text
    assert "target_not_member" in resp.text

    # Revoke the member, then transferring to them is 422.
    resp = post(owner, f"/api/workspaces/{ws}/members/{mid}/revoke", {})
    assert resp.status_code == 200, resp.text
    resp = _transfer(owner, ws, target["user_id"])
    assert resp.status_code == 422, resp.text
    assert "target_revoked" in resp.text
    # The owner is still the only owner after all the rejections.
    members = _list_members(owner, ws)
    assert [m["email"] for m in members if m["role"] == "owner"] == [
        "live-m10-owner2@example.com"
    ]


def test_cl20_lock_is_really_taken_live(live):
    """The CL-20 per-user lock is a real PostgreSQL advisory lock:
    while one transaction holds it, a second session cannot acquire
    the same key (lock_timeout-bounded). Also verifies the documented
    SQL-side key derivation equals the Python derivation."""
    user_id = uuid.uuid4()
    key = cl20_lock_key(user_id)

    conn1 = _owner_conn()
    conn2 = _owner_conn()
    try:
        # SQL-side derivation (module docstring) == Python derivation.
        with conn1.cursor() as cur:
            cur.execute(
                "SELECT ('x' || substr(encode(sha256('cl20:'::bytea || "
                "decode(replace(%s::text, '-', ''), 'hex')), 'hex'), 1, 16)"
                ")::bit(64)::bigint",
                (str(user_id),),
            )
            assert cur.fetchone()[0] == key
            # Hold the lock in an open transaction.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        # Second session, bounded wait: must fail to acquire.
        with conn2.cursor() as cur:
            cur.execute("SET lock_timeout = '2s'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        conn2.rollback()
    finally:
        conn1.rollback()  # releases the advisory lock
        conn1.close()
        conn2.close()
