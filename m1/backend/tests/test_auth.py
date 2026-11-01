"""Auth + workspace tests (M1): register/login/me/logout, wrong
password, CSRF enforcement, session revocation, rate limiting,
tenant isolation, and the no-client-supplied-merchant_id rule.
"""

import uuid

from conftest import authed_client, get_csrf, login, post, register


def test_register_login_me_logout(client):
    user = register(client, "alice@example.com")
    assert user["email"] == "alice@example.com"
    assert user["status"] == "active"
    assert "password" not in user and "password_hash" not in user

    me = client.get("/api/auth/me")
    assert me.status_code == 401  # not logged in yet

    login(client, "alice@example.com")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == user["id"]

    resp = post(client, "/api/auth/logout", {})
    assert resp.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_register_duplicate_email_generic_response(client):
    # Phase 1 (B-F9): registering an existing email returns a generic
    # 200 that does not reveal existence — never a 400 naming the
    # email, and no second account is created.
    register(client, "dup@example.com")
    resp = post(client, "/api/auth/register", {"email": "dup@example.com", "password": "correct-horse-battery-1"})
    assert resp.status_code == 200, resp.text
    assert "already registered" in resp.json()["detail"]
    assert "id" not in resp.json()
    from sqlalchemy import select

    from app.db.auth_models import User
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        assert len(db.execute(select(User)).scalars().all()) == 1


def test_register_short_password_rejected(client):
    resp = post(client, "/api/auth/register", {"email": "short@example.com", "password": "tooshort"})
    assert resp.status_code == 422


def test_login_wrong_password_401(client):
    register(client, "bob@example.com")
    resp = post(client, "/api/auth/login", {"email": "bob@example.com", "password": "wrong-password-123"})
    assert resp.status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_login_unknown_email_401(client):
    resp = post(client, "/api/auth/login", {"email": "nobody@example.com", "password": "whatever-password-1"})
    assert resp.status_code == 401


def test_csrf_missing_forbidden(client):
    # State-changing request without the CSRF header -> 403, even
    # with otherwise valid input.
    resp = client.post(
        "/api/auth/register",
        json={"email": "csrf@example.com", "password": "correct-horse-battery-1"},
    )
    assert resp.status_code == 403


def test_csrf_wrong_token_forbidden(client):
    token = get_csrf(client)
    assert token
    resp = client.post(
        "/api/auth/register",
        json={"email": "csrf2@example.com", "password": "correct-horse-battery-1"},
        headers={"X-CSRF-Token": "not-the-token"},
    )
    assert resp.status_code == 403


def test_me_without_session_401(client):
    assert client.get("/api/auth/me").status_code == 401


def test_session_revocation_via_revoke_all(client):
    from fastapi.testclient import TestClient

    from app.main import app

    c1 = TestClient(app)
    c2 = TestClient(app)
    register(c1, "carol@example.com")
    login(c1, "carol@example.com")
    login(c2, "carol@example.com")
    assert c1.get("/api/auth/me").status_code == 200
    assert c2.get("/api/auth/me").status_code == 200

    resp = post(c1, "/api/auth/revoke-all", {})
    assert resp.status_code == 200
    assert c1.get("/api/auth/me").status_code == 401
    assert c2.get("/api/auth/me").status_code == 401


def test_login_rate_limit_after_repeated_failures(client):
    register(client, "dave@example.com")
    for _ in range(5):
        resp = post(client, "/api/auth/login", {"email": "dave@example.com", "password": "wrong-password-123"})
        assert resp.status_code == 401
    # 6th attempt inside the window is rate-limited, even with the
    # correct password.
    resp = post(client, "/api/auth/login", {"email": "dave@example.com", "password": "correct-horse-battery-1"})
    assert resp.status_code == 429


def test_workspace_create_list_get(client):
    authed_client(client, "erin@example.com")
    resp = post(client, "/api/workspaces", {"name": "Erin's Shop", "trading_name": "Erin Traders"})
    assert resp.status_code == 201, resp.text
    ws = resp.json()
    assert ws["name"] == "Erin's Shop"
    assert ws["role"] == "owner"

    listed = client.get("/api/workspaces")
    assert listed.status_code == 200
    assert [w["id"] for w in listed.json()] == [ws["id"]]

    got = client.get(f"/api/workspaces/{ws['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == ws["id"]


def test_tenant_isolation_user_b_cannot_see_user_a_workspace(client):
    from fastapi.testclient import TestClient

    from app.main import app

    a = TestClient(app)
    b = TestClient(app)
    authed_client(a, "alice-a@example.com")
    authed_client(b, "bob-b@example.com")

    ws = post(a, "/api/workspaces", {"name": "A Shop"}).json()

    # B's list does not contain A's workspace.
    assert b.get("/api/workspaces").json() == []
    # B asking for A's workspace id directly gets 404 (no leak).
    assert b.get(f"/api/workspaces/{ws['id']}").status_code == 404
    # A still sees it.
    assert a.get(f"/api/workspaces/{ws['id']}").status_code == 200


def test_client_supplied_merchant_id_rejected(client):
    authed_client(client, "fran@example.com")
    forged = str(uuid.uuid4())
    resp = post(client, "/api/workspaces", {"name": "Forged", "merchant_id": forged})
    assert resp.status_code == 422


def test_workspace_requires_auth(client):
    assert client.get("/api/workspaces").status_code == 401
    resp = post(client, "/api/workspaces", {"name": "No Auth"})
    assert resp.status_code in (401, 403)
