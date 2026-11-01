"""Auth service: registration, login sessions, password reset (M1).

Sessions are server-managed: an opaque random token is issued as an
HttpOnly cookie, and ONLY its SHA-256 hash is stored in user_sessions.
Logout / revoke-all REVOKE the row (revoked_at set — Phase 1, B-F1):
the runtime role has no DELETE grant on user_sessions, so DELETE
failed with a 500 and left the session valid. Revoked sessions
resolve to None via the revoked_at check in
get_user_by_session_token.

Password reset: POST /password-reset/request always returns 200 (no
account-enumeration oracle). When the email belongs to a user, a
hashed one-time token with a 1h expiry is stored. NO email is sent
in M1 and the raw token / reset link is NEVER logged (Phase 1,
B-F2/C-4.4): production must send the link by email. See README.md
"Open items".
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.auth_models import PasswordResetToken, User, UserSession
from app.logging_config import get_logger
from app.security import (
    generate_password_reset_token,
    generate_session_token,
    hash_password,
    hash_token,
    verify_password,
)

logger = get_logger(__name__)

# Fixed dummy Argon2 hash used ONLY for timing parity on the
# unknown-user / no-local-password login path (B-F9): verifying the
# supplied password against it costs the same as a real check, so an
# attacker cannot distinguish "unknown email" from "wrong password"
# by timing. It is not a credential and matches no real password.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-timing-parity-only")


class AuthError(Exception):
    """Raised for credential failures; the API maps it to 401."""


class EmailTakenError(Exception):
    """Raised when registering an already-registered email; maps to 400."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    """SQLite stores TIMESTAMPTZ values as naive datetimes; PostgreSQL
    returns aware ones. Normalise to aware UTC so comparisons work on
    both (the test-only SQLite fallback and real PostgreSQL)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.execute(
        select(User).where(User.email == normalize_email(email))
    ).scalar_one_or_none()


def _check_password_length(password: str) -> None:
    if len(password) < settings.password_min_length:
        raise ValueError(f"Password must be at least {settings.password_min_length} characters.")
    if len(password) > settings.password_max_length:
        raise ValueError(
            f"Password must be at most {settings.password_max_length} characters."
        )


def register_user(db: Session, email: str, password: str, display_name: str | None) -> User:
    _check_password_length(password)
    email_norm = normalize_email(email)
    if get_user_by_email(db, email_norm) is not None:
        raise EmailTakenError("An account with this email already exists.")
    user = User(
        email=email_norm,
        display_name=display_name,
        password_hash=hash_password(password),
        status="active",
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Race: two concurrent registrations for the same email both
        # passed the pre-check; the unique constraint decides. Map to
        # the same generic "taken" outcome, never a 500 (B-F14).
        db.rollback()
        raise EmailTakenError("An account with this email already exists.") from None
    db.refresh(user)
    logger.info("user_registered", extra={"user_id": str(user.id)})
    return user


def create_session(db: Session, user: User, device_label: str | None = None) -> str:
    """Create a session row and return the RAW token (to be set as a
    cookie once). The raw token is never stored."""
    token = generate_session_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=_utcnow() + timedelta(seconds=settings.session_max_age_seconds),
            device_label=device_label,
        )
    )
    db.commit()
    return token


def authenticate(db: Session, email: str, password: str) -> User:
    """Return the user on success; raise AuthError on any failure
    (unknown email and wrong password are indistinguishable — and,
    Phase 1, take the same time: the unknown-user and no-local-
    password paths run a dummy Argon2 verify, B-F9)."""
    user = get_user_by_email(db, email)
    if user is None or user.status != "active":
        verify_password(_DUMMY_PASSWORD_HASH, password)
        raise AuthError("Invalid email or password.")
    if user.password_hash is None:
        # External identity provider account (OD-03); no local password.
        verify_password(_DUMMY_PASSWORD_HASH, password)
        raise AuthError("Invalid email or password.")
    if not verify_password(user.password_hash, password):
        raise AuthError("Invalid email or password.")
    return user


def get_user_by_session_token(db: Session, raw_token: str) -> User | None:
    """Resolve a raw session cookie token to its user, or None.
    Expired or revoked sessions resolve to None."""
    if not raw_token:
        return None
    row = db.execute(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or _as_aware(row.expires_at) <= _utcnow():
        return None
    user = db.get(User, row.user_id)
    if user is None or user.status != "active":
        return None
    return user


def revoke_session(db: Session, raw_token: str) -> None:
    """Revoke the session for this token (logout). Idempotent.
    Phase 1 (B-F1): UPDATE revoked_at instead of DELETE — the runtime
    role has SELECT/INSERT/UPDATE only on user_sessions, so DELETE
    raised a 500, the cookie was never cleared and the session stayed
    valid. get_user_by_session_token already treats revoked_at as
    revoked, so this covers every resolution path."""
    db.execute(
        update(UserSession)
        .where(UserSession.token_hash == hash_token(raw_token))
        .values(revoked_at=_utcnow())
    )
    db.commit()


def revoke_all_sessions(db: Session, user: User) -> int:
    """Revoke every session for the user (UPDATE revoked_at, B-F1).
    Returns rows revoked."""
    result = db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id)
        .where(UserSession.revoked_at.is_(None))
        .values(revoked_at=_utcnow())
    )
    db.commit()
    logger.info("all_sessions_revoked", extra={"user_id": str(user.id)})
    return result.rowcount or 0


def request_password_reset(db: Session, email: str) -> None:
    """Always succeeds from the caller's point of view. If the email
    belongs to an active user with a local password, invalidate any
    prior unused tokens (Phase 1, B-F5) and store a hashed one-time
    token (1h expiry). The raw token is NEVER logged (Phase 1,
    B-F2/C-4.4) — only the user id is logged. NO email is sent in M1;
    see module docstring and README.md "Open items"."""
    user = get_user_by_email(db, email)
    if user is None or user.status != "active" or user.password_hash is None:
        logger.info("password_reset_requested_unknown_account")
        return
    # Invalidate prior unused tokens so only the newest request can
    # be used (UPDATE grant exists on password_reset_tokens).
    db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id)
        .where(PasswordResetToken.used_at.is_(None))
        .values(used_at=_utcnow())
    )
    token = generate_password_reset_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=_utcnow() + timedelta(seconds=settings.password_reset_ttl_seconds),
        )
    )
    db.commit()
    # The reset link is NOT logged: anyone with log access could
    # otherwise reset the account. Production must email the link
    # (with the raw token) and must not log it.
    logger.info("password_reset_token_issued", extra={"user_id": str(user.id)})


def confirm_password_reset(db: Session, raw_token: str, new_password: str) -> User:
    """Consume a reset token and set the new password. Raises AuthError
    for unknown/expired/used tokens; ValueError for a short or
    over-long password.
    On success all existing sessions are revoked."""
    _check_password_length(new_password)
    row = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_token(raw_token)
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or _as_aware(row.expires_at) <= _utcnow():
        raise AuthError("Invalid or expired reset token.")
    user = db.get(User, row.user_id)
    if user is None or user.status != "active":
        raise AuthError("Invalid or expired reset token.")
    row.used_at = _utcnow()
    user.password_hash = hash_password(new_password)
    user.updated_at = _utcnow()
    db.commit()
    revoke_all_sessions(db, user)
    logger.info("password_reset_completed", extra={"user_id": str(user.id)})
    return user
