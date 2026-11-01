"""Password-reset tests (M1).

- The reset token is stored ONLY as a SHA-256 hash (never raw).
- Request always returns 200, including for unknown emails.
- Confirm with a valid token resets the password and revokes sessions.
- Reusing a token, or using an expired token, is rejected.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.auth_models import PasswordResetToken, UserSession
from app.db.session import SessionLocal
from app.security import hash_token
from conftest import get_csrf, login, post, register


def _issued_token_from_log(caplog) -> str:
    for record in caplog.records:
        link = getattr(record, "reset_link", None)
        if link:
            return link.split("token=")[1]
    raise AssertionError("no password-reset link was logged")


def test_reset_token_stored_hashed_only(client, caplog):
    register(client, "gail@example.com")
    with caplog.at_level(logging.INFO, logger="app.services.auth_service"):
        resp = post(client, "/api/auth/password-reset/request", {"email": "gail@example.com"})
    assert resp.status_code == 200

    raw_token = _issued_token_from_log(caplog)
    assert raw_token and len(raw_token) >= 32

    with SessionLocal() as db:
        row = db.execute(select(PasswordResetToken)).scalar_one()
        assert row.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
        assert row.token_hash == hash_token(raw_token)
        assert row.token_hash != raw_token  # raw token is never stored
        assert row.used_at is None


def test_reset_request_unknown_email_still_200(client):
    resp = post(client, "/api/auth/password-reset/request", {"email": "ghost@example.com"})
    assert resp.status_code == 200
    with SessionLocal() as db:
        assert db.execute(select(PasswordResetToken)).scalar_one_or_none() is None


def test_reset_confirm_happy_path_and_reuse_rejected(client, caplog):
    register(client, "henry@example.com")
    login(client, "henry@example.com")
    assert client.get("/api/auth/me").status_code == 200

    with caplog.at_level(logging.INFO, logger="app.services.auth_service"):
        post(client, "/api/auth/password-reset/request", {"email": "henry@example.com"})
    raw_token = _issued_token_from_log(caplog)

    resp = post(
        client,
        "/api/auth/password-reset/confirm",
        {"token": raw_token, "new_password": "brand-new-password-2"},
    )
    assert resp.status_code == 200, resp.text

    # Old session was revoked by the reset.
    assert client.get("/api/auth/me").status_code == 401
    # New password works.
    login(client, "henry@example.com", "brand-new-password-2")

    # Token is single-use.
    resp = post(
        client,
        "/api/auth/password-reset/confirm",
        {"token": raw_token, "new_password": "another-password-3"},
    )
    assert resp.status_code == 400


def test_reset_confirm_expired_token_rejected(client):
    register(client, "iris@example.com")
    with SessionLocal() as db:
        from app.db.auth_models import User

        user = db.execute(select(User).where(User.email == "iris@example.com")).scalar_one()
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token("expired-token-value"),
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        db.commit()

    resp = post(
        client,
        "/api/auth/password-reset/confirm",
        {"token": "expired-token-value", "new_password": "brand-new-password-2"},
    )
    assert resp.status_code == 400


def test_reset_confirm_unknown_token_rejected(client):
    register(client, "jack@example.com")
    resp = post(
        client,
        "/api/auth/password-reset/confirm",
        {"token": "no-such-token", "new_password": "brand-new-password-2"},
    )
    assert resp.status_code == 400


def test_reset_revokes_other_sessions(client, caplog):
    from fastapi.testclient import TestClient

    from app.main import app

    c2 = TestClient(app)
    register(client, "kate@example.com")
    login(client, "kate@example.com")
    login(c2, "kate@example.com")
    assert c2.get("/api/auth/me").status_code == 200

    with caplog.at_level(logging.INFO, logger="app.services.auth_service"):
        post(client, "/api/auth/password-reset/request", {"email": "kate@example.com"})
    raw_token = _issued_token_from_log(caplog)
    post(client, "/api/auth/password-reset/confirm", {"token": raw_token, "new_password": "brand-new-password-2"})

    assert c2.get("/api/auth/me").status_code == 401
    with SessionLocal() as db:
        assert db.execute(select(UserSession)).scalar_one_or_none() is None
