"""Shared FastAPI dependencies: current-user resolution and CSRF.

- get_current_user: resolves the HttpOnly session cookie to a user via
  the server-side session table. Missing/expired/revoked -> 401.
- require_csrf: double-submit check for state-changing requests.
  The X-CSRF-Token header must equal the cl_csrf cookie value
  (constant-time compare). Missing/mismatched -> 403.
- require_step_up: Phase 1.1 (B-F11) -- the session must ALSO be
  freshly authenticated (step-up verified within
  step_up_ttl_seconds, or created within step_up_fresh_login_seconds).
  Otherwise 403 "Step-up authentication required."
"""

from datetime import datetime, timezone

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db.auth_models import User
from app.db.session import get_db, set_user_context
from app.security import tokens_equal
from app.services import auth_service


def _as_aware(value: datetime | None) -> datetime | None:
    """SQLite stores TIMESTAMPTZ values as naive datetimes; PostgreSQL
    returns aware ones. Normalise to aware UTC so comparisons work on
    both (mirrors auth_service._as_aware)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    user = auth_service.get_user_by_session_token(db, raw_token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    # B-1 fix (v0.1 E.2): assert the transaction-local user context for
    # this request from the server-side session state. The membership-
    # based RLS policies in rls.sql resolve the caller's workspaces
    # from it. This db session is the same one the endpoint uses
    # (FastAPI caches get_db per request), so the setting applies to
    # every statement the endpoint runs in this transaction.
    set_user_context(db, user.id)
    return user


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        return None
    user = auth_service.get_user_by_session_token(db, raw_token)
    if user is not None:
        set_user_context(db, user.id)
    return user


def require_csrf(
    request: Request,
    csrf_cookie: str | None = Cookie(default=None, alias=settings.csrf_cookie_name),
    csrf_header: str | None = Header(default=None, alias=settings.csrf_header_name),
) -> None:
    """Double-submit cookie check. Both the cookie and the header must
    be present and equal. (Phase 1, B-F13: the cookie/header names
    come from settings — the aliases above are evaluated from settings
    at import time, so a renamed cookie/header is honoured.)"""
    if not csrf_cookie or not csrf_header or not tokens_equal(csrf_cookie, csrf_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF token.",
        )


# Phase 1 (B-F3): viewer members are read-only. Every mutating
# workspace endpoint enforces this AFTER the membership check, so a
# non-member still gets 404 (existence is not leaked) while a viewer
# gets 403. Owner/admin/member may write.
WRITE_ROLES = ("owner", "admin", "member")


def require_step_up(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Phase 1.1 (B-F11): require fresh authentication for a
    highest-impact action (currently owner transfer only).

    Satisfied when the session row for this request's cookie either
    carries a live step-up verification (``step_up_expires_at`` in
    the future, set by POST /api/auth/step-up) or was created within
    ``step_up_fresh_login_seconds`` (15 minutes) -- so the normal
    login -> transfer flow needs no extra password prompt. Otherwise
    403: the frontend asks for the password again (the step-up
    prompt UI is a follow-up; no UI change in 1.1)."""
    raw_token = request.cookies.get(settings.session_cookie_name)
    session = auth_service.get_session_by_token(db, raw_token) if raw_token else None
    now = datetime.now(timezone.utc)
    if session is not None and session.user_id == current_user.id:
        step_up_until = _as_aware(session.step_up_expires_at)
        created_at = _as_aware(session.created_at)
        if step_up_until is not None and step_up_until > now:
            return current_user
        if created_at is not None and (now - created_at).total_seconds() <= (
            settings.step_up_fresh_login_seconds
        ):
            return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Step-up authentication required. Please re-enter your password.",
    )


def require_write_role(membership) -> None:
    """Raise 403 when the caller's workspace role is viewer-only."""
    if membership is None or membership.role not in WRITE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewers have read-only access to this workspace.",
        )
