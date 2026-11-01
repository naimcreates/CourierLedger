"""Member invitation / acceptance / role / revocation API tests
(M1 Milestone 9, part 1) -- SQLite fallback suite.

Covers: the invite -> accept flow, idempotent accept and
re-invite, unknown-email invite (422, no invented user), role
validation (never owner via these endpoints), role change incl.
owner immutability, revoke incl. the last-active-owner guard
(OD-11), immediate workspace denial for a revoked member (Phase
1.1, B-F15: a revoked user with no other active membership also
loses every session -- 401 everywhere, not just workspace 404s;
see test_revoke_denies_workspace_immediately_and_reinvite_works),
non-member 404s, non-admin 403s, missing-CSRF 403s, and the
CL-20 lock-key derivation (deterministic, total order).

Live-PostgreSQL coverage (RLS + runtime role + the advisory lock
really being taken + revoke-during-live-session) lives in
tests/test_members_live_pg.py.

NOTE: app/main.py is wired by the coordinator, so these tests mount
the members router themselves (guarded: skipped when the route
already exists). The live tests cannot do that (uvicorn subprocess)
and need the wiring in place.
"""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import members as members_api
from app.db.auth_models import MerchantMembership, User
from app.db.session import SessionLocal
from app.main import app
from app.services.member_service import cl20_lock_key
from tests.conftest import get_csrf, login, post, register

# app/main.py is wired by the coordinator, so these tests mount the
# members router themselves (guarded: skipped when the route already
# exists, e.g. after the coordinator wires it). NOTE: this FastAPI
# version (0.162) stores included routers as lazy _IncludedRouter
# objects in app.routes -- the plain `getattr(r, "path", "")` check
# used by older test files never sees them, so the guard must also
# look inside original_router.routes (paths there carry no /api
# prefix; the prefix is applied at match time).
def _members_router_mounted() -> bool:
    wanted = {
        "/api/workspaces/{workspace_id}/members",
        "/workspaces/{workspace_id}/members",
    }
    for route in app.routes:
        sub_routes = getattr(getattr(route, "original_router", None), "routes", None)
        for sub in sub_routes if sub_routes is not None else [route]:
            if getattr(sub, "path", "") in wanted:
                return True
    return False


if not _members_router_mounted():
    app.include_router(members_api.router, prefix="/api")


def _client_for(email: str) -> TestClient:
    """A fresh client with its own cookie jar, registered + logged in."""
    client = TestClient(app)
    register(client, email)
    login(client, email)
    return client


def _workspace(client: TestClient, name: str = "M9 Test Shop") -> str:
    resp = post(client, "/api/workspaces", {"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _members(client: TestClient, ws: str):
    resp = client.get(f"/api/workspaces/{ws}/members")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _member_by_email(client: TestClient, ws: str, email: str) -> dict:
    for member in _members(client, ws):
        if member["email"] == email:
            return member
    raise AssertionError(f"no member with email {email} in {ws}")


def _seed_owner_membership(ws: str, user_id: str) -> None:
    """Test-only direct seed: a second ACTIVE OWNER membership row.
    There is no API path that creates an owner (by design), so the
    last-owner guard's positive branch (revoke succeeds while
    another owner exists) can only be exercised by seeding."""
    db = SessionLocal()
    try:
        db.add(
            MerchantMembership(
                merchant_id=uuid.UUID(ws),
                user_id=uuid.UUID(user_id),
                role="owner",
                status="active",
                invited_by_user_id=uuid.UUID(user_id),
            )
        )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CL-20 lock key derivation (pure function)
# ---------------------------------------------------------------------------

def test_cl20_lock_key_deterministic_and_in_bigint_range():
    user_id = uuid.uuid4()
    key_a = cl20_lock_key(user_id)
    key_b = cl20_lock_key(user_id)
    assert key_a == key_b
    assert -(2**63) <= key_a <= 2**63 - 1
    # A different user (almost surely) gets a different key.
    assert cl20_lock_key(uuid.uuid4()) != key_a
    # Namespace separation: the "cl20:" prefix is part of the hashed
    # input, so the key differs from a bare UUID hash.
    import hashlib

    bare = int.from_bytes(
        hashlib.sha256(uuid.UUID(str(user_id)).bytes).digest()[:8],
        byteorder="big",
        signed=True,
    )
    assert key_a != bare


# ---------------------------------------------------------------------------
# Invite -> accept happy path (+ idempotency)
# ---------------------------------------------------------------------------

def test_invite_accept_flow_and_idempotent_accept():
    owner = _client_for("m9-owner@example.com")
    invitee = _client_for("m9-invitee@example.com")
    ws = _workspace(owner)

    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "m9-invitee@example.com", "role": "member"})
    assert resp.status_code == 201, resp.text
    invited = resp.json()
    assert invited["status"] == "invited"
    assert invited["role"] == "member"
    assert invited["email"] == "m9-invitee@example.com"
    assert invited["invited_by"] == "m9-owner@example.com"

    members = _members(owner, ws)
    assert len(members) == 2
    assert {m["status"] for m in members} == {"active", "invited"}

    # The invited user accepts with their own session.
    resp = post(invitee, f"/api/workspaces/{ws}/members/{invited['id']}/accept", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    # Accepting again is idempotent: 200, same membership.
    resp = post(invitee, f"/api/workspaces/{ws}/members/{invited['id']}/accept", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == invited["id"]
    assert resp.json()["status"] == "active"

    # The new member now sees the workspace.
    resp = invitee.get(f"/api/workspaces/{ws}")
    assert resp.status_code == 200, resp.text


def test_invite_is_idempotent_for_pending_invitation():
    owner = _client_for("m9-owner2@example.com")
    _client_for("m9-invitee2@example.com")
    ws = _workspace(owner)

    first = post(owner, f"/api/workspaces/{ws}/members/invite",
                 {"email": "m9-invitee2@example.com", "role": "viewer"})
    assert first.status_code == 201, first.text
    second = post(owner, f"/api/workspaces/{ws}/members/invite",
                  {"email": "m9-invitee2@example.com", "role": "viewer"})
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["status"] == "invited"
    assert len(_members(owner, ws)) == 2


def test_invite_unknown_email_is_422_and_invents_no_user():
    owner = _client_for("m9-owner3@example.com")
    ws = _workspace(owner)
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "nobody-here@example.com", "role": "member"})
    assert resp.status_code == 422, resp.text
    assert "unknown_email" in resp.text
    assert len(_members(owner, ws)) == 1  # no membership row created


def test_invite_as_owner_role_is_422():
    owner = _client_for("m9-owner4@example.com")
    _client_for("m9-invitee4@example.com")
    ws = _workspace(owner)
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "m9-invitee4@example.com", "role": "owner"})
    assert resp.status_code == 422, resp.text
    assert "role_not_invitable" in resp.text


def test_invite_already_active_member_is_409():
    owner = _client_for("m9-owner5@example.com")
    invitee = _client_for("m9-invitee5@example.com")
    ws = _workspace(owner)
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "m9-invitee5@example.com", "role": "member"})
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    assert post(invitee, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 200
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "m9-invitee5@example.com", "role": "member"})
    assert resp.status_code == 409, resp.text


def test_invite_email_is_case_insensitive():
    owner = _client_for("m9-owner6@example.com")
    _client_for("m9-invitee6@example.com")
    ws = _workspace(owner)
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "M9-INVITEE6@EXAMPLE.COM", "role": "member"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] == "m9-invitee6@example.com"


# ---------------------------------------------------------------------------
# Authorization: non-members 404, non-admins 403, wrong-user accept 404
# ---------------------------------------------------------------------------

def test_non_member_gets_404_everywhere():
    owner = _client_for("m9-owner7@example.com")
    stranger = _client_for("m9-stranger7@example.com")
    ws = _workspace(owner)
    assert stranger.get(f"/api/workspaces/{ws}/members").status_code == 404
    assert post(stranger, f"/api/workspaces/{ws}/members/invite",
                {"email": "x@example.com", "role": "member"}).status_code == 404
    assert post(stranger, f"/api/workspaces/{ws}/members/{uuid.uuid4()}/accept",
                {}).status_code == 404
    assert post(stranger, f"/api/workspaces/{ws}/members/{uuid.uuid4()}/revoke",
                {}).status_code == 404


def test_member_cannot_invite_or_change_roles_or_revoke():
    owner = _client_for("m9-owner8@example.com")
    member_client = _client_for("m9-member8@example.com")
    _client_for("m9-other8@example.com")
    ws = _workspace(owner)
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "m9-member8@example.com", "role": "member"})
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    assert post(member_client, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 200

    # A plain member may LIST, but not manage.
    assert member_client.get(f"/api/workspaces/{ws}/members").status_code == 200
    resp = post(member_client, f"/api/workspaces/{ws}/members/invite",
                {"email": "m9-other8@example.com", "role": "member"})
    assert resp.status_code == 403, resp.text
    resp = member_client.patch(
        f"/api/workspaces/{ws}/members/{mid}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(member_client)},
    )
    assert resp.status_code == 403, resp.text
    resp = post(member_client, f"/api/workspaces/{ws}/members/{mid}/revoke", {})
    assert resp.status_code == 403, resp.text


def test_accept_by_wrong_user_is_404():
    owner = _client_for("m9-owner9@example.com")
    _client_for("m9-invitee9@example.com")
    wrong = _client_for("m9-wrong9@example.com")
    ws = _workspace(owner)
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "m9-invitee9@example.com", "role": "member"})
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    # Neither a stranger nor the owner can accept someone else's invite.
    assert post(wrong, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 404
    assert post(owner, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 404


def test_unauthenticated_and_missing_csrf():
    owner = _client_for("m9-owner10@example.com")
    ws = _workspace(owner)
    anon = TestClient(app)
    assert anon.get(f"/api/workspaces/{ws}/members").status_code == 401
    # Missing CSRF token on a state-changing call -> 403.
    resp = owner.post(f"/api/workspaces/{ws}/members/invite",
                      json={"email": "x@example.com", "role": "member"})
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Role changes
# ---------------------------------------------------------------------------

def _setup_member(owner, member_email, ws, role="member"):
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": member_email, "role": role})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_role_change_and_owner_immutability():
    owner = _client_for("m9-owner11@example.com")
    member_client = _client_for("m9-member11@example.com")
    ws = _workspace(owner)
    invited = _setup_member(owner, "m9-member11@example.com", ws)
    mid = invited["id"]
    assert post(member_client, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 200

    # member -> admin works.
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{mid}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"

    # Promoting to owner via this endpoint is rejected.
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{mid}/role",
        json={"role": "owner"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 422, resp.text
    assert "role_not_invitable" in resp.text

    # The owner's own role cannot be changed here either.
    owner_membership = _member_by_email(owner, ws, "m9-owner11@example.com")
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{owner_membership['id']}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 422, resp.text
    assert "owner_role_immutable" in resp.text

    # An invited (not yet accepted) member's role can be corrected.
    _client_for("m9-invitee11@example.com")
    pending = _setup_member(owner, "m9-invitee11@example.com", ws, role="viewer")
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{pending['id']}/role",
        json={"role": "member"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "member"
    assert resp.json()["status"] == "invited"


def test_admin_can_manage_members():
    owner = _client_for("m9-owner12@example.com")
    admin_client = _client_for("m9-admin12@example.com")
    _client_for("m9-target12@example.com")
    ws = _workspace(owner)
    invited = _setup_member(owner, "m9-admin12@example.com", ws)
    assert post(admin_client, f"/api/workspaces/{ws}/members/{invited['id']}/accept", {}).status_code == 200
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{invited['id']}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text
    # The admin can now invite.
    resp = post(admin_client, f"/api/workspaces/{ws}/members/invite",
                {"email": "m9-target12@example.com", "role": "viewer"})
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Revoke + immediate denial + last-owner guard
# ---------------------------------------------------------------------------

def test_revoke_denies_workspace_immediately_and_reinvite_works():
    # Phase 1.1 (B-F15) BEHAVIOUR CHANGE, recorded here: revoking a
    # user with no other active membership now also revokes ALL of
    # their sessions (previously the session stayed valid globally
    # and only workspace endpoints 404'd). The assertions below
    # reflect the new behaviour: after revoke the member's session
    # is 401 everywhere, so the member must log in again before the
    # re-invite flow can continue. A multi-workspace user keeps
    # their session for their other workspaces (covered in
    # tests/test_security_phase11.py).
    owner = _client_for("m9-owner13@example.com")
    member_client = _client_for("m9-member13@example.com")
    ws = _workspace(owner)
    invited = _setup_member(owner, "m9-member13@example.com", ws)
    mid = invited["id"]
    assert post(member_client, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 200
    assert member_client.get(f"/api/workspaces/{ws}").status_code == 200

    resp = post(owner, f"/api/workspaces/{ws}/members/{mid}/revoke", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"

    # Revoking again is idempotent.
    resp = post(owner, f"/api/workspaces/{ws}/members/{mid}/revoke", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"

    # B-F15: the revoked member's session is gone entirely (they
    # hold no other active membership), so every endpoint is 401 --
    # including /api/auth/me -- not just the workspace 404s.
    assert member_client.get("/api/auth/me").status_code == 401
    assert member_client.get(f"/api/workspaces/{ws}").status_code == 401
    assert member_client.get(f"/api/workspaces/{ws}/members").status_code == 401
    assert member_client.get("/api/workspaces").status_code == 401

    # Log back in (the account still exists; only the session was
    # revoked) and continue the original flow: the revoked
    # membership still denies the workspace until re-invited.
    login(member_client, "m9-member13@example.com")
    assert member_client.get(f"/api/workspaces/{ws}").status_code == 404
    assert member_client.get("/api/workspaces").status_code == 200
    assert member_client.get("/api/workspaces").json() == []

    # A revoked invitation cannot be accepted...
    resp = post(member_client, f"/api/workspaces/{ws}/members/{mid}/accept", {})
    assert resp.status_code == 422, resp.text
    assert "invitation_revoked" in resp.text

    # ...but the role of a revoked membership is frozen...
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{mid}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 422, resp.text
    assert "membership_revoked" in resp.text

    # ...and a re-invite reactivates it (200, same membership row).
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": "m9-member13@example.com", "role": "viewer"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == mid
    assert resp.json()["status"] == "invited"
    assert resp.json()["role"] == "viewer"
    assert post(member_client, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 200
    assert member_client.get(f"/api/workspaces/{ws}").status_code == 200


def test_last_active_owner_cannot_be_revoked():
    owner = _client_for("m9-owner14@example.com")
    ws = _workspace(owner)
    owner_membership = _member_by_email(owner, ws, "m9-owner14@example.com")
    resp = post(owner, f"/api/workspaces/{ws}/members/{owner_membership['id']}/revoke", {})
    assert resp.status_code == 422, resp.text
    assert "last_active_owner" in resp.text
    # The owner is still active afterwards.
    assert _member_by_email(owner, ws, "m9-owner14@example.com")["status"] == "active"


def test_owner_revoke_succeeds_while_another_owner_exists():
    owner_a = _client_for("m9-ownera15@example.com")
    _client_for("m9-ownerb15@example.com")
    ws = _workspace(owner_a)
    # Seed a second active owner directly (no API creates owners).
    db = SessionLocal()
    try:
        b_id = db.execute(
            select(User.id).where(User.email == "m9-ownerb15@example.com")
        ).scalar_one()
    finally:
        db.close()
    _seed_owner_membership(ws, str(b_id))
    assert len(_members(owner_a, ws)) == 2

    # A can revoke B (B is an owner, but not the last one).
    b_membership = _member_by_email(owner_a, ws, "m9-ownerb15@example.com")
    resp = post(owner_a, f"/api/workspaces/{ws}/members/{b_membership['id']}/revoke", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"

    # Now A is the last active owner again: revoking A fails.
    a_membership = _member_by_email(owner_a, ws, "m9-ownera15@example.com")
    resp = post(owner_a, f"/api/workspaces/{ws}/members/{a_membership['id']}/revoke", {})
    assert resp.status_code == 422, resp.text
    assert "last_active_owner" in resp.text


def test_sequential_mutations_under_the_lock_all_succeed():
    """Two sequential mutations on the same membership (the lock is
    taken and released per transaction) both succeed; the last-owner
    guard still holds under the lock. True concurrent contention is
    timing-dependent and is covered by the live-PG lock test, not
    here (see the service docstring)."""
    owner = _client_for("m9-owner16@example.com")
    member_client = _client_for("m9-member16@example.com")
    ws = _workspace(owner)
    invited = _setup_member(owner, "m9-member16@example.com", ws)
    mid = invited["id"]
    assert post(member_client, f"/api/workspaces/{ws}/members/{mid}/accept", {}).status_code == 200
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{mid}/role",
        json={"role": "viewer"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{mid}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"
    assert post(owner, f"/api/workspaces/{ws}/members/{mid}/revoke", {}).status_code == 200
    owner_membership = _member_by_email(owner, ws, "m9-owner16@example.com")
    resp = post(owner, f"/api/workspaces/{ws}/members/{owner_membership['id']}/revoke", {})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Owner transfer (M10 step 2, remaining OD-11 slice)
# ---------------------------------------------------------------------------

def _setup_active_member(owner, member_client, member_email, ws, role="member"):
    invited = _setup_member(owner, member_email, ws, role=role)
    assert post(member_client, f"/api/workspaces/{ws}/members/{invited['id']}/accept", {}).status_code == 200
    return _member_by_email(owner, ws, member_email)


def test_transfer_owner_success_and_old_owner_becomes_admin():
    owner = _client_for("m10-owner1@example.com")
    member_client = _client_for("m10-member1@example.com")
    ws = _workspace(owner)
    member = _setup_active_member(owner, member_client, "m10-member1@example.com", ws)

    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": member["user_id"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == member["user_id"]
    assert resp.json()["role"] == "owner"
    assert resp.json()["status"] == "active"

    members = _members(owner, ws)
    owners = [m for m in members if m["role"] == "owner"]
    assert len(owners) == 1
    assert owners[0]["email"] == "m10-member1@example.com"
    old_owner = _member_by_email(owner, ws, "m10-owner1@example.com")
    assert old_owner["role"] == "admin"
    assert old_owner["status"] == "active"

    # The demoted owner (now admin) can no longer transfer ownership.
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": member["user_id"]})
    assert resp.status_code == 403, resp.text

    # The new owner CAN transfer it back.
    resp = post(member_client, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": old_owner["user_id"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == old_owner["user_id"]
    assert resp.json()["role"] == "owner"
    members = _members(member_client, ws)
    assert len([m for m in members if m["role"] == "owner"]) == 1
    assert _member_by_email(member_client, ws, "m10-member1@example.com")["role"] == "admin"


def test_transfer_owner_by_non_owner_is_403():
    owner = _client_for("m10-owner2@example.com")
    admin_client = _client_for("m10-admin2@example.com")
    member_client = _client_for("m10-member2@example.com")
    ws = _workspace(owner)
    admin = _setup_active_member(owner, admin_client, "m10-admin2@example.com", ws)
    member = _setup_active_member(owner, member_client, "m10-member2@example.com", ws)
    # Promote the admin so the 403 is about ownership, not management.
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{admin['id']}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text

    resp = post(admin_client, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": member["user_id"]})
    assert resp.status_code == 403, resp.text
    resp = post(member_client, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": admin["user_id"]})
    assert resp.status_code == 403, resp.text
    # Nothing changed: the original owner is still the only owner.
    members = _members(owner, ws)
    assert [m["email"] for m in members if m["role"] == "owner"] == ["m10-owner2@example.com"]


def test_transfer_owner_to_non_member_or_unknown_user_is_422():
    owner = _client_for("m10-owner3@example.com")
    outsider = _client_for("m10-outsider3@example.com")
    ws = _workspace(owner)
    # A real user who is not a member of this workspace.
    outsider_member = _members(owner, ws)
    outsider_id = None
    db = SessionLocal()
    try:
        outsider_id = str(db.execute(
            select(User.id).where(User.email == "m10-outsider3@example.com")
        ).scalar_one())
    finally:
        db.close()
    assert len(outsider_member) == 1
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": outsider_id})
    assert resp.status_code == 422, resp.text
    assert "target_not_member" in resp.text
    # A user id that belongs to nobody at all: same reason, no leak.
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": str(uuid.uuid4())})
    assert resp.status_code == 422, resp.text
    assert "target_not_member" in resp.text


def test_transfer_owner_to_revoked_or_invited_member_is_422():
    owner = _client_for("m10-owner4@example.com")
    member_client = _client_for("m10-member4@example.com")
    _client_for("m10-invited4@example.com")
    ws = _workspace(owner)
    member = _setup_active_member(owner, member_client, "m10-member4@example.com", ws)
    # Revoke the active member, then try to transfer to them.
    assert post(owner, f"/api/workspaces/{ws}/members/{member['id']}/revoke", {}).status_code == 200
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": member["user_id"]})
    assert resp.status_code == 422, resp.text
    assert "target_revoked" in resp.text
    # An invited-but-not-accepted member cannot receive ownership.
    pending = _setup_member(owner, "m10-invited4@example.com", ws)
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": pending["user_id"]})
    assert resp.status_code == 422, resp.text
    assert "target_not_active" in resp.text


def test_transfer_owner_to_self_or_existing_owner_is_422():
    owner = _client_for("m10-owner5@example.com")
    member_client = _client_for("m10-member5@example.com")
    ws = _workspace(owner)
    _setup_active_member(owner, member_client, "m10-member5@example.com", ws)
    owner_row = _member_by_email(owner, ws, "m10-owner5@example.com")
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": owner_row["user_id"]})
    assert resp.status_code == 422, resp.text
    assert "target_already_owner" in resp.text


def test_transfer_owner_non_member_gets_404():
    owner = _client_for("m10-owner6@example.com")
    stranger = _client_for("m10-stranger6@example.com")
    ws = _workspace(owner)
    resp = post(stranger, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": str(uuid.uuid4())})
    assert resp.status_code == 404, resp.text


def test_last_owner_guard_still_holds_after_transfer():
    owner = _client_for("m10-owner7@example.com")
    member_client = _client_for("m10-member7@example.com")
    ws = _workspace(owner)
    member = _setup_active_member(owner, member_client, "m10-member7@example.com", ws)
    assert post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": member["user_id"]}).status_code == 200

    # The new sole owner cannot be revoked -- not by the old owner
    # (now admin), and not by themselves.
    new_owner_row = _member_by_email(owner, ws, "m10-member7@example.com")
    resp = post(owner, f"/api/workspaces/{ws}/members/{new_owner_row['id']}/revoke", {})
    assert resp.status_code == 422, resp.text
    assert "last_active_owner" in resp.text
    resp = post(member_client, f"/api/workspaces/{ws}/members/{new_owner_row['id']}/revoke", {})
    assert resp.status_code == 422, resp.text
    assert "last_active_owner" in resp.text
    # Exactly one owner remains, and it is the new owner.
    members = _members(member_client, ws)
    assert [m["email"] for m in members if m["role"] == "owner"] == ["m10-member7@example.com"]
