"""Phase 1.1 security tests (SQLite suite).

Proves the app-layer Phase 1.1 hardening at the HTTP/service layer:
- B-F10 audit trail: every consequential mutation writes an audit_logs
  row in the same transaction, with server-side actor/merchant only
  and no secrets in detail.
- B-F11 session cap (evict-oldest at 10) and step-up authentication
  for owner transfer (fresh-login window, step-up endpoint, stale
  step-up rejected).
- B-F15 revoked-member sessions: a single-workspace user loses every
  session on revoke; a multi-workspace user keeps theirs for their
  other workspaces; the manager-only force-revoke endpoint logs a
  member out for cause.
- Password-reset confirm is a single commit (password + token +
  session revocation) and is rate-limited per IP (50/hour).
- Logout clears the CSRF cookie as well as the session cookie.
- B-F19 purge_expired_auth_rows deletes only expired/used auth rows.

Live-PostgreSQL coverage (same scenarios under the runtime role,
plus audit immutability and tenant isolation) lives in
tests/test_security_phase11_live_pg.py.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db.auth_models import PasswordResetToken, User, UserSession
from app.db.models import AuditLog
from app.db.session import SessionLocal
from app.main import app
from app.security import hash_token
from app.services import auth_service
from conftest import get_csrf, login, post, register
from test_password_reset import _issued_token
from test_security_phase1 import (
    FIXTURE,
    _client_for,
    _setup_ws_with_account,
    _upload,
    _workspace,
)
from test_settlement import _allocate, _batch, _complete, _receipt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _me_id(client: TestClient) -> str:
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _session_cookie(client: TestClient) -> str:
    return client.cookies.get(settings.session_cookie_name)


def _client_with_token(token: str) -> TestClient:
    """A fresh client carrying exactly one session cookie."""
    c = TestClient(app)
    c.cookies.set(settings.session_cookie_name, token)
    return c


def _audit_rows() -> list[AuditLog]:
    with SessionLocal() as db:
        return db.execute(select(AuditLog).order_by(AuditLog.created_at)).scalars().all()


def _audit_actions() -> list[str]:
    return [row.action for row in _audit_rows()]


def _invite_accept(owner: TestClient, ws: str, invitee: TestClient, email: str,
                   role: str = "member") -> str:
    resp = post(owner, f"/api/workspaces/{ws}/members/invite",
                {"email": email, "role": role})
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    resp = post(invitee, f"/api/workspaces/{ws}/members/{mid}/accept", {})
    assert resp.status_code == 200, resp.text
    return mid


def _member_row(owner: TestClient, ws: str, email: str) -> dict:
    resp = owner.get(f"/api/workspaces/{ws}/members")
    assert resp.status_code == 200, resp.text
    for m in resp.json():
        if m["email"] == email:
            return m
    raise AssertionError(f"no member {email} in {ws}")


def _age_session(user_id: str, *, minutes: int, step_up_expires_at=None) -> None:
    """Test-only direct manipulation: make a session look old."""
    import uuid as _uuid

    with SessionLocal() as db:
        row = db.execute(
            select(UserSession).where(UserSession.user_id == _uuid.UUID(user_id))
        ).scalar_one()
        row.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=minutes
        )
        row.step_up_expires_at = step_up_expires_at
        db.commit()


def _setup_order_shipment(client: TestClient, ws: str) -> None:
    """Create ORD-1001/TRK-1001 (delivered, COD-bearing) so the first
    fixture row strong-matches and a run has an eligible line."""
    resp = post(client, f"/api/workspaces/{ws}/orders",
                {"merchant_order_ref": "ORD-1001", "order_cod_poisha": 100000})
    assert resp.status_code == 201, resp.text
    order = resp.json()["order_id"]
    resp = post(client, f"/api/workspaces/{ws}/orders/{order}/shipments",
                {"tracking_id": "TRK-1001", "outcome": "delivered",
                 "is_cod_bearing": True, "amount_to_collect_poisha": 100000})
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# B-F11 — session cap (evict-oldest)
# ---------------------------------------------------------------------------

def test_session_cap_evicts_oldest_on_11th_login(client):
    email = "p11-cap@example.com"
    register(client, email)
    tokens = []
    for _ in range(11):
        c = TestClient(app)
        login(c, email)
        tokens.append(_session_cookie(c))
    assert len(set(tokens)) == 11
    # The oldest token is revoked: 401 everywhere.
    assert _client_with_token(tokens[0]).get("/api/auth/me").status_code == 401
    # The newest token still works, and the 2nd-oldest survived too
    # (only one eviction was needed to stay at the cap of 10).
    assert _client_with_token(tokens[10]).get("/api/auth/me").status_code == 200
    assert _client_with_token(tokens[1]).get("/api/auth/me").status_code == 200
    with SessionLocal() as db:
        rows = db.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalars().all()
        assert len(rows) == 10
        revoked = db.execute(
            select(UserSession).where(UserSession.revoked_at.is_not(None))
        ).scalars().all()
        assert len(revoked) == 1


# ---------------------------------------------------------------------------
# B-F11 — step-up for owner transfer
# ---------------------------------------------------------------------------

def test_step_up_transfer_flow():
    """Aged session -> 403; wrong step-up password -> 401 and still
    403; correct step-up password -> 200 and the transfer succeeds."""
    owner = TestClient(app)
    register(owner, "p11-step-owner@example.com", password="correct-horse-battery-1")
    login(owner, "p11-step-owner@example.com", password="correct-horse-battery-1")
    member = _client_for("p11-step-member@example.com")
    ws = _workspace(owner)
    _invite_accept(owner, ws, member, "p11-step-member@example.com")
    target_user_id = _member_row(owner, ws, "p11-step-member@example.com")["user_id"]

    # Age the owner's session past the 15-minute fresh-login window
    # (no step-up verification yet) -> transfer is 403.
    _age_session(_me_id(owner), minutes=16)
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": target_user_id})
    assert resp.status_code == 403, resp.text
    assert "Step-up authentication required" in resp.text

    # Wrong password -> 401, and transfer is still 403 afterwards.
    resp = post(owner, "/api/auth/step-up", {"password": "definitely-wrong-password"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Invalid email or password."
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": target_user_id})
    assert resp.status_code == 403, resp.text

    # Correct password -> 200, and the transfer now succeeds.
    resp = post(owner, "/api/auth/step-up", {"password": "correct-horse-battery-1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["detail"] == "Step-up verified."
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": target_user_id})
    assert resp.status_code == 200, resp.text
    assert "auth.step_up_verified" in _audit_actions()


def test_step_up_fresh_login_and_stale_verification():
    """A fresh login needs no step-up prompt; an expired step-up
    verification on an old session is rejected until re-verified."""
    owner = TestClient(app)
    register(owner, "p11-step2-owner@example.com", password="correct-horse-battery-1")
    login(owner, "p11-step2-owner@example.com", password="correct-horse-battery-1")
    member = _client_for("p11-step2-member@example.com")
    ws = _workspace(owner)
    _invite_accept(owner, ws, member, "p11-step2-member@example.com")
    target_user_id = _member_row(owner, ws, "p11-step2-member@example.com")["user_id"]

    # Fresh login: no step-up prompt needed.
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": target_user_id})
    assert resp.status_code == 200, resp.text

    # Transfer it back: the new owner (member client) is also fresh.
    owner_row = _member_row(member, ws, "p11-step2-owner@example.com")
    resp = post(member, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": owner_row["user_id"]})
    assert resp.status_code == 200, resp.text

    # Age the session AND expire a step-up verification -> 403.
    _age_session(_me_id(owner), minutes=16,
                 step_up_expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                 - timedelta(minutes=1))
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": target_user_id})
    assert resp.status_code == 403, resp.text
    assert "Step-up authentication required" in resp.text

    # Step-up with the correct password re-enables the transfer.
    resp = post(owner, "/api/auth/step-up", {"password": "correct-horse-battery-1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["detail"] == "Step-up verified."
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": target_user_id})
    assert resp.status_code == 200, resp.text
    # And the step-up itself was audited.
    assert "auth.step_up_verified" in _audit_actions()


# ---------------------------------------------------------------------------
# B-F15 — revoked-member sessions
# ---------------------------------------------------------------------------

def test_revoke_single_workspace_user_session_dead_everywhere():
    owner = _client_for("p11-rev-owner@example.com")
    member = _client_for("p11-rev-member@example.com")
    ws = _workspace(owner)
    mid = _invite_accept(owner, ws, member, "p11-rev-member@example.com")
    assert member.get(f"/api/workspaces/{ws}").status_code == 200
    resp = post(owner, f"/api/workspaces/{ws}/members/{mid}/revoke", {})
    assert resp.status_code == 200, resp.text
    # Single-workspace user: the session is revoked entirely.
    assert member.get("/api/auth/me").status_code == 401
    assert member.get(f"/api/workspaces/{ws}").status_code == 401
    assert member.get("/api/workspaces").status_code == 401


def test_revoke_multi_workspace_user_keeps_other_workspace():
    owner_a = _client_for("p11-multi-ownera@example.com")
    owner_b = _client_for("p11-multi-ownerb@example.com")
    member = _client_for("p11-multi-member@example.com")
    ws_a = _workspace(owner_a, "Multi Shop A")
    ws_b = _workspace(owner_b, "Multi Shop B")
    mid_a = _invite_accept(owner_a, ws_a, member, "p11-multi-member@example.com")
    _invite_accept(owner_b, ws_b, member, "p11-multi-member@example.com")
    assert member.get(f"/api/workspaces/{ws_a}").status_code == 200
    assert member.get(f"/api/workspaces/{ws_b}").status_code == 200
    resp = post(owner_a, f"/api/workspaces/{ws_a}/members/{mid_a}/revoke", {})
    assert resp.status_code == 200, resp.text
    # Session survives for the other workspace; the revoked one 404s.
    assert member.get("/api/auth/me").status_code == 200
    assert member.get(f"/api/workspaces/{ws_b}").status_code == 200
    assert member.get(f"/api/workspaces/{ws_a}").status_code == 404
    assert member.get(f"/api/workspaces/{ws_a}/members").status_code == 404


def test_force_revoke_sessions_endpoint():
    owner = _client_for("p11-force-owner@example.com")
    member = _client_for("p11-force-member@example.com")
    member2 = TestClient(app)
    login(member2, "p11-force-member@example.com")
    plain = _client_for("p11-force-plain@example.com")
    ws = _workspace(owner)
    _invite_accept(owner, ws, member, "p11-force-member@example.com")
    _invite_accept(owner, ws, plain, "p11-force-plain@example.com", role="member")
    target_user_id = _me_id(member)

    # A plain member may not force-revoke; a non-member gets 404.
    resp = post(plain, f"/api/workspaces/{ws}/members/{target_user_id}/revoke-sessions", {})
    assert resp.status_code == 403, resp.text
    stranger = _client_for("p11-force-stranger@example.com")
    resp = post(stranger, f"/api/workspaces/{ws}/members/{target_user_id}/revoke-sessions", {})
    assert resp.status_code == 404, resp.text
    # An unknown user id in a real workspace is 404 too.
    import uuid as _uuid

    resp = post(owner, f"/api/workspaces/{ws}/members/{_uuid.uuid4()}/revoke-sessions", {})
    assert resp.status_code == 404, resp.text

    # The manager's force-revoke logs the member out of BOTH sessions.
    resp = post(owner, f"/api/workspaces/{ws}/members/{target_user_id}/revoke-sessions", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked"] == 2
    assert member.get("/api/auth/me").status_code == 401
    assert member2.get("/api/auth/me").status_code == 401
    # Everyone else is unaffected.
    assert owner.get("/api/auth/me").status_code == 200
    assert plain.get("/api/auth/me").status_code == 200
    assert "members.sessions_force_revoked" in _audit_actions()


# ---------------------------------------------------------------------------
# Password-reset confirm: single commit + rate limit
# ---------------------------------------------------------------------------

def test_reset_confirm_revokes_sessions_and_new_password_works(client):
    c2 = TestClient(app)
    register(client, "p11-reset@example.com")
    login(client, "p11-reset@example.com")
    login(c2, "p11-reset@example.com")
    assert client.get("/api/auth/me").status_code == 200
    assert c2.get("/api/auth/me").status_code == 200
    raw = _issued_token("p11-reset@example.com")
    resp = post(client, "/api/auth/password-reset/confirm",
                {"token": raw, "new_password": "brand-new-password-9"})
    assert resp.status_code == 200, resp.text
    # Both prior sessions are dead, the token is consumed, and the new
    # password works -- the password change and the revocation landed
    # in the one confirm_password_reset commit (no window between
    # them for old sessions to survive a crash).
    assert client.get("/api/auth/me").status_code == 401
    assert c2.get("/api/auth/me").status_code == 401
    with SessionLocal() as db:
        token_row = db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == hash_token(raw)
            )
        ).scalar_one()
        assert token_row.used_at is not None
        user = db.execute(select(User).where(User.email == "p11-reset@example.com")).scalar_one()
        sessions = db.execute(
            select(UserSession).where(UserSession.user_id == user.id)
        ).scalars().all()
        assert sessions and all(s.revoked_at is not None for s in sessions)
    c3 = TestClient(app)
    login(c3, "p11-reset@example.com", password="brand-new-password-9")
    assert c3.get("/api/auth/me").status_code == 200
    assert "auth.password_reset_completed" in _audit_actions()


def test_reset_confirm_rate_limit_50_then_429(client):
    for _ in range(50):
        resp = post(client, "/api/auth/password-reset/confirm",
                    {"token": "bogus-token", "new_password": "brand-new-password-9"})
        assert resp.status_code == 400, resp.text
    resp = post(client, "/api/auth/password-reset/confirm",
                {"token": "bogus-token", "new_password": "brand-new-password-9"})
    assert resp.status_code == 429, resp.text


# ---------------------------------------------------------------------------
# Logout clears the CSRF cookie
# ---------------------------------------------------------------------------

def test_logout_clears_csrf_cookie(client):
    register(client, "p11-logout@example.com")
    login(client, "p11-logout@example.com")
    assert client.get("/api/auth/csrf").status_code == 200
    assert settings.csrf_cookie_name in client.cookies
    resp = post(client, "/api/auth/logout", {})
    assert resp.status_code == 200, resp.text
    cleared = [h for h in resp.headers.get_list("set-cookie")
               if h.startswith(settings.csrf_cookie_name)]
    assert cleared, resp.headers.get_list("set-cookie")
    assert any("Max-Age=0" in h for h in cleared), cleared
    session_cleared = [h for h in resp.headers.get_list("set-cookie")
                       if h.startswith(settings.session_cookie_name)]
    assert session_cleared and any("Max-Age=0" in h for h in session_cleared)
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# B-F10 — audit trail
# ---------------------------------------------------------------------------

def test_audit_identity_and_member_events():
    owner = _client_for("p11-audit-owner@example.com")
    member = _client_for("p11-audit-member@example.com")
    owner_id = _me_id(owner)
    member_id = _me_id(member)
    ws = _workspace(owner)
    mid = _invite_accept(owner, ws, member, "p11-audit-member@example.com")
    # Role change.
    resp = owner.patch(
        f"/api/workspaces/{ws}/members/{mid}/role",
        json={"role": "admin"},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text
    # Force-revoke the member's session, then let them log back in.
    resp = post(owner, f"/api/workspaces/{ws}/members/{member_id}/revoke-sessions", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked"] == 1
    assert member.get("/api/auth/me").status_code == 401
    login(member, "p11-audit-member@example.com")
    # Owner transfer (fresh login -> step-up satisfied).
    resp = post(owner, f"/api/workspaces/{ws}/members/transfer-owner",
                {"user_id": member_id})
    assert resp.status_code == 200, resp.text
    # The new owner revokes the old owner (now admin). The old owner
    # holds no other membership, so B-F15 also kills their session.
    old_owner_row = _member_row(member, ws, "p11-audit-owner@example.com")
    resp = post(member, f"/api/workspaces/{ws}/members/{old_owner_row['id']}/revoke", {})
    assert resp.status_code == 200, resp.text
    assert owner.get("/api/auth/me").status_code == 401
    # Failed login is audited without the email.
    resp = post(member, "/api/auth/login",
                {"email": "p11-audit-member@example.com", "password": "wrong-password-123"})
    assert resp.status_code == 401, resp.text
    # Logout + step-up + reset request/confirm + revoke-all.
    resp = post(member, "/api/auth/logout", {})
    assert resp.status_code == 200, resp.text
    login(member, "p11-audit-member@example.com")
    resp = post(member, "/api/auth/step-up", {"password": "test-password"})
    # The member client logged in with the conftest default password,
    # which this test does not know -- so step-up with it must fail;
    # the success path is covered by
    # test_step_up_fresh_login_and_stale_verification.
    assert resp.status_code == 401, resp.text
    resp = post(member, "/api/auth/password-reset/request",
                {"email": "p11-audit-member@example.com"})
    assert resp.status_code == 200, resp.text
    raw = _issued_token("p11-audit-member@example.com")
    resp = post(member, "/api/auth/password-reset/confirm",
                {"token": raw, "new_password": "brand-new-password-9"})
    assert resp.status_code == 200, resp.text
    login(member, "p11-audit-member@example.com", password="brand-new-password-9")
    resp = post(member, "/api/auth/revoke-all", {})
    assert resp.status_code == 200, resp.text

    rows = _audit_rows()
    by_action = {}
    for row in rows:
        by_action.setdefault(row.action, []).append(row)
    # Identity events: actor is the user, merchant is NULL.
    for action in ("auth.register", "auth.login_success", "auth.logout",
                   "auth.password_reset_requested",
                   "auth.password_reset_completed", "auth.revoke_all"):
        assert action in by_action, _audit_actions()
    assert by_action["auth.register"][0].actor_user_id is not None
    assert by_action["auth.register"][0].merchant_id is None
    assert by_action["auth.login_success"][0].merchant_id is None
    # Failed login: actor None, no email anywhere in detail.
    assert "auth.login_failed" in by_action
    failed = by_action["auth.login_failed"][0]
    assert failed.actor_user_id is None and failed.merchant_id is None
    assert failed.detail == {"reason": "invalid_credentials"}
    # Member events: actor + merchant are server-side values.
    for action in ("members.invited", "members.invitation_accepted",
                   "members.role_changed", "members.sessions_force_revoked",
                   "members.owner_transferred", "members.revoked"):
        assert action in by_action, _audit_actions()
        assert by_action[action][0].merchant_id is not None
        assert str(by_action[action][0].merchant_id) == ws
    assert by_action["members.role_changed"][0].detail == {
        "target_user_id": member_id, "old_role": "member", "new_role": "admin",
    }
    assert by_action["members.owner_transferred"][0].detail == {
        "from_user_id": owner_id, "to_user_id": member_id,
    }
    assert by_action["members.sessions_force_revoked"][0].detail["revoked_sessions"] == 1


def test_audit_business_events():
    owner = _client_for("p11-auditbiz-owner@example.com")
    owner_id = _me_id(owner)
    ws, account_id = _setup_ws_with_account(owner)
    _setup_order_shipment(owner, ws)
    resp = _upload(owner, ws, account_id, FIXTURE.read_bytes(), key="p11-audit-import-1")
    assert resp.status_code == 201, resp.text
    batch = resp.json()["batch_id"]
    resp = post(owner, f"/api/workspaces/{ws}/imports/{batch}/runs",
                {"courier_fixture": "Fixture Alpha"})
    assert resp.status_code in (200, 201), resp.text
    run_id = resp.json()["run_id"]
    # Settlement chain: batch -> receipt -> complete -> allocate ->
    # reverse -> re-allocate -> variance.
    sbatch = _batch(owner, ws, account_id, expected=100000)
    sbatch_id = sbatch["batch_id"]
    receipt = _receipt(owner, ws, sbatch_id, amount=60000)
    receipt_id = receipt["receipt_id"]
    _complete(owner, ws, sbatch_id, receipt_id)
    resp = _allocate(owner, ws, sbatch_id, receipt_id, 60000)
    assert resp.status_code == 201, resp.text
    alloc_id = resp.json()["allocation_id"]
    resp = post(owner,
                f"/api/workspaces/{ws}/settlement-batches/{sbatch_id}/allocations/{alloc_id}/reverse",
                {})
    assert resp.status_code == 201, resp.text
    resp = _allocate(owner, ws, sbatch_id, receipt_id, 60000)
    assert resp.status_code == 201, resp.text
    resp = post(owner,
                f"/api/workspaces/{ws}/settlement-batches/{sbatch_id}/calculate-variance",
                {})
    assert resp.status_code == 201, resp.text
    assert resp.json()["bank_variance_poisha"] == -40000
    # Settings: tolerance 0 -> 500.
    resp = owner.patch(
        f"/api/workspaces/{ws}/settings",
        json={"tolerance_poisha": 500},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tolerance_poisha"] == 500

    rows = _audit_rows()
    by_action = {}
    for row in rows:
        by_action.setdefault(row.action, []).append(row)
    for action in ("imports.uploaded", "runs.created",
                   "settlements.batch_created", "settlements.receipt_created",
                   "settlements.receipt_completed", "settlements.allocated",
                   "settlements.reversed", "settlements.variance_calculated",
                   "settings.updated"):
        assert action in by_action, _audit_actions()
        row = by_action[action][0]
        assert str(row.actor_user_id) == owner_id
        assert str(row.merchant_id) == ws
    assert by_action["imports.uploaded"][0].entity_id is not None
    assert str(by_action["runs.created"][0].entity_id) == run_id
    assert by_action["settlements.variance_calculated"][0].detail["bank_variance_poisha"] == -40000
    assert by_action["settings.updated"][0].detail == {
        "tolerance_poisha": {"old": 0, "new": 500},
    }
    # A no-change settings PATCH writes no audit row.
    n_before = len(_audit_rows())
    resp = owner.patch(
        f"/api/workspaces/{ws}/settings",
        json={"tolerance_poisha": 500},
        headers={"X-CSRF-Token": get_csrf(owner)},
    )
    assert resp.status_code == 200, resp.text
    assert len(_audit_rows()) == n_before


def test_audit_detail_never_contains_secrets(client):
    """No audit detail may contain a password or a reset token, and
    no login-failed detail may contain an email address."""
    password = "super-secret-password-1"
    register(client, "p11-nosecret@example.com", password=password)
    login(client, "p11-nosecret@example.com", password=password)
    resp = post(client, "/api/auth/login",
                {"email": "p11-nosecret@example.com", "password": "wrong-password-123"})
    assert resp.status_code == 401
    raw = _issued_token("p11-nosecret@example.com")
    resp = post(client, "/api/auth/password-reset/confirm",
                {"token": raw, "new_password": "another-secret-password-2"})
    assert resp.status_code == 200, resp.text
    for row in _audit_rows():
        blob = json.dumps(row.detail)
        assert password not in blob
        assert "another-secret-password-2" not in blob
        assert raw not in blob
        assert "p11-nosecret@example.com" not in blob
        assert "token" not in blob.lower()


# ---------------------------------------------------------------------------
# B-F19 — purge expired auth rows
# ---------------------------------------------------------------------------

def test_purge_expired_auth_rows(client):
    register(client, "p11-purge@example.com")
    login(client, "p11-purge@example.com")
    with SessionLocal() as db:
        user = db.execute(
            select(User).where(User.email == "p11-purge@example.com")
        ).scalar_one()
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=6)
        # Expired session (to be purged) + revoked-but-unexpired one
        # (kept: purge is by expiry, not revocation).
        db.add(UserSession(user_id=user.id, token_hash="expired-hash",
                           expires_at=past))
        db.add(UserSession(user_id=user.id, token_hash="revoked-hash",
                           expires_at=future, revoked_at=past))
        # Expired token + used token (purged) + live token (kept).
        db.add(PasswordResetToken(user_id=user.id, token_hash="expired-token",
                                  expires_at=past))
        db.add(PasswordResetToken(user_id=user.id, token_hash="used-token",
                                  expires_at=future, used_at=past))
        db.add(PasswordResetToken(user_id=user.id, token_hash="live-token",
                                  expires_at=future))
        db.commit()
        live_before = db.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalars().all()
        # The login session + the expired (not yet purged) session.
        assert len(live_before) == 2
    with SessionLocal() as db:
        result = auth_service.purge_expired_auth_rows(db)
    assert result == {"sessions": 1, "reset_tokens": 2}
    with SessionLocal() as db:
        assert db.execute(
            select(UserSession).where(UserSession.token_hash == "expired-hash")
        ).scalar_one_or_none() is None
        assert db.execute(
            select(UserSession).where(UserSession.token_hash == "revoked-hash")
        ).scalar_one_or_none() is not None
        assert db.execute(
            select(UserSession).where(UserSession.revoked_at.is_(None))
        ).scalar_one_or_none() is not None  # the login session kept
        assert db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == "live-token")
        ).scalar_one_or_none() is not None
        assert db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash.in_(("expired-token", "used-token")))
        ).scalars().all() == []
