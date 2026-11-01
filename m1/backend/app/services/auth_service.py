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

from sqlalchemy import delete, select, update
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
from app.services import audit_service

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
        # Flush first so the audit row can reference the new user's
        # id; the audit row shares this transaction, so a commit
        # failure (the race below) rolls it back too.
        db.flush()
        audit_service.record_audit(
            db,
            actor=user,
            merchant_id=None,
            action=audit_service.AUTH_REGISTER,
            entity_table="users",
            entity_id=user.id,
        )
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
    cookie once). The raw token is never stored.

    Phase 1.1 (B-F11) session cap: a user holds at most
    ``settings.session_max_concurrent`` live sessions. When this
    login would exceed the cap, the OLDEST live sessions are revoked
    (evict-oldest, not reject) down to cap - 1, then the new session
    is inserted; the evictions and the insert share this function's
    single commit. A revoked session resolves to None everywhere, so
    the evicted device is logged out on its next request."""
    cap = settings.session_max_concurrent
    now = _utcnow()
    if cap >= 1:
        live = db.execute(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None),
            )
        ).scalars().all()
        live = [row for row in live if _as_aware(row.expires_at) > now]
        if len(live) >= cap:
            live.sort(key=lambda row: _as_aware(row.created_at) or now)
            evict = live[: len(live) - (cap - 1)]
            for row in evict:
                row.revoked_at = now
            logger.info(
                "session_cap_evict_oldest",
                extra={"user_id": str(user.id), "evicted": len(evict)},
            )
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


def get_session_by_token(db: Session, raw_token: str) -> UserSession | None:
    """Return the UserSession row for a raw cookie token, or None.
    Unlike get_user_by_session_token this does NOT check expiry or
    revocation -- it is the step-up helper (deps.require_step_up
    applies its own freshness checks)."""
    if not raw_token:
        return None
    return db.execute(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    ).scalar_one_or_none()


def verify_step_up(db: Session, user: User, password: str, raw_token: str) -> bool:
    """Phase 1.1 (B-F11) step-up verification: re-check the
    authenticated user's password; on success stamp
    ``step_up_expires_at`` (now + ``settings.step_up_ttl_seconds``)
    on the session row for this token and commit. Returns True on
    success, False on any failure (wrong password, no local
    password, missing/expired/revoked session) -- never raises, so
    the API maps False to a generic 401. The dummy-verify path is
    not needed here: the caller is already authenticated, so an
    unknown-user oracle does not exist on this endpoint."""
    if user.password_hash is None or not verify_password(user.password_hash, password):
        return False
    row = get_session_by_token(db, raw_token)
    if (
        row is None
        or row.user_id != user.id
        or row.revoked_at is not None
        or _as_aware(row.expires_at) <= _utcnow()
    ):
        return False
    row.step_up_expires_at = _utcnow() + timedelta(seconds=settings.step_up_ttl_seconds)
    db.commit()
    logger.info("step_up_verified", extra={"user_id": str(user.id)})
    return True


def purge_expired_auth_rows(db: Session) -> dict:
    """Delete expired auth rows (Phase 1.1, B-F19 maintenance
    procedure -- NOT an HTTP endpoint).

    Deletes user_sessions rows whose ``expires_at`` has passed
    (expired, whether revoked or not) and password_reset_tokens
    rows that are expired OR already used. Returns
    ``{"sessions": n, "reset_tokens": m}``.

    MUST run with an owner/migrator connection during a maintenance
    window: the runtime role has NO DELETE grant on either table
    (roles.sql), so calling this through the app's runtime session
    fails with InsufficientPrivilege and deletes nothing. Run it
    directly against PostgreSQL as the owner role (e.g. a scheduled
    psql / maintenance-script invocation), never from a request
    handler.
    """
    now = _utcnow()
    sessions = db.execute(
        delete(UserSession).where(UserSession.expires_at <= now)
    ).rowcount or 0
    tokens = db.execute(
        delete(PasswordResetToken).where(
            (PasswordResetToken.expires_at <= now)
            | (PasswordResetToken.used_at.is_not(None))
        )
    ).rowcount or 0
    db.commit()
    logger.info(
        "expired_auth_rows_purged",
        extra={"sessions": sessions, "reset_tokens": tokens},
    )
    return {"sessions": sessions, "reset_tokens": tokens}


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
        # Unknown / inactive / no-local-password account: the caller
        # still gets a generic 200, but the request itself is audited
        # (actor None -- no email is recorded) so reset-request abuse
        # is visible. Bounded by the per-IP rate limit on the endpoint.
        audit_service.record_audit(
            db,
            actor=None,
            merchant_id=None,
            action=audit_service.AUTH_PASSWORD_RESET_REQUESTED,
            detail={"reason": "unknown_account"},
        )
        db.commit()
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
    audit_service.record_audit(
        db,
        actor=user,
        merchant_id=None,
        action=audit_service.AUTH_PASSWORD_RESET_REQUESTED,
        entity_table="users",
        entity_id=user.id,
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
    On success all existing sessions are revoked.

    Phase 1.1: the password change, the token's ``used_at`` and the
    session revocation happen in ONE commit. Previously the password
    committed first and ``revoke_all_sessions`` committed separately,
    so a crash between the two left the new password active with the
    old sessions still alive. The revocation is inlined here as an
    UPDATE in the same transaction; ``revoke_all_sessions`` itself
    is unchanged for its other callers."""
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
    revoked = db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id)
        .where(UserSession.revoked_at.is_(None))
        .values(revoked_at=_utcnow())
    ).rowcount or 0
    audit_service.record_audit(
        db,
        actor=user,
        merchant_id=None,
        action=audit_service.AUTH_PASSWORD_RESET_COMPLETED,
        entity_table="users",
        entity_id=user.id,
        detail={"sessions_revoked": revoked},
    )
    db.commit()
    logger.info("password_reset_completed", extra={"user_id": str(user.id)})
    return user
