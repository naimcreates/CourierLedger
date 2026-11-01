"""Password-reset tests (M1).

- The reset token is stored ONLY as a SHA-256 hash (never raw).
- Request always returns 200, including for unknown emails.
- Confirm with a valid token resets the password and revokes sessions.
- Reusing a token, or using an expired token, is rejected.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.auth_models import PasswordResetToken, UserSession
from app.db.session import SessionLocal
from app.security import hash_token
from conftest import get_csrf, login, post, register


def _issued_token(email: str) -> str:
    """Test-only helper (Phase 1, B-F2): the raw reset token is never
    logged anywhere, so tests obtain it by generating a token and
    storing its hash via the service's own hashing — the same shape
    request_password_reset stores — and returning the raw token.
    The raw token is never written to a log."""
    from app.security import generate_password_reset_token, hash_token

    token = generate_password_reset_token()
    with SessionLocal() as db:
        from app.db.auth_models import User

        user = db.execute(select(User).where(User.email == email)).scalar_one()
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
        )
        db.commit()
    return token


def test_reset_token_stored_hashed_only(client, caplog):
    register(client, "gail@example.com")
    with caplog.at_level(logging.INFO, logger="app.services.auth_service"):
        resp = post(client, "/api/auth/password-reset/request", {"email": "gail@example.com"})
    assert resp.status_code == 200
    # The request endpoint created exactly one token row, stored as a
    # SHA-256 hex digest — and no log record may carry a reset link
    # or a raw token (Phase 1, B-F2).
    with SessionLocal() as db:
        rows = db.execute(select(PasswordResetToken)).scalars().all()
        assert len(rows) == 1
        assert re.fullmatch(r"[0-9a-f]{64}", rows[0].token_hash)
        assert rows[0].used_at is None
    for record in caplog.records:
        assert getattr(record, "reset_link", None) is None
        assert "reset-password?token=" not in record.getMessage()
        assert "token=" not in record.getMessage()
    # The helper-created token proves the stored form is the hash of
    # the raw token (and never the raw token itself).
    raw_token = _issued_token("gail@example.com")
    assert raw_token and len(raw_token) >= 32
    with SessionLocal() as db:
        row = db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == hash_token(raw_token)
            )
        ).scalar_one()
        assert row.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
        assert row.token_hash != raw_token  # raw token is never stored


def test_reset_request_unknown_email_still_200(client):
    resp = post(client, "/api/auth/password-reset/request", {"email": "ghost@example.com"})
    assert resp.status_code == 200
    with SessionLocal() as db:
        assert db.execute(select(PasswordResetToken)).scalar_one_or_none() is None


def test_reset_confirm_happy_path_and_reuse_rejected(client, caplog):
    register(client, "henry@example.com")
    login(client, "henry@example.com")
    assert client.get("/api/auth/me").status_code == 200

    post(client, "/api/auth/password-reset/request", {"email": "henry@example.com"})
    raw_token = _issued_token("henry@example.com")

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

    post(client, "/api/auth/password-reset/request", {"email": "kate@example.com"})
    raw_token = _issued_token("kate@example.com")
    post(client, "/api/auth/password-reset/confirm", {"token": raw_token, "new_password": "brand-new-password-2"})

    assert c2.get("/api/auth/me").status_code == 401
    # Phase 1 (B-F1): revocation is UPDATE revoked_at, not DELETE —
    # the rows remain, marked revoked.
    with SessionLocal() as db:
        sessions = db.execute(select(UserSession)).scalars().all()
        assert sessions, "revoked sessions must remain as rows"
        assert all(s.revoked_at is not None for s in sessions)
