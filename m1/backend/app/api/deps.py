"""Shared FastAPI dependencies: current-user resolution and CSRF.

- get_current_user: resolves the HttpOnly session cookie to a user via
  the server-side session table. Missing/expired/revoked -> 401.
- require_csrf: double-submit check for state-changing requests.
  The X-CSRF-Token header must equal the cl_csrf cookie value
  (constant-time compare). Missing/mismatched -> 403.
"""

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db.auth_models import User
from app.db.session import get_db, set_user_context
from app.security import tokens_equal
from app.services import auth_service


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
    csrf_cookie: str | None = Cookie(default=None, alias="cl_csrf"),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    """Double-submit cookie check. Both the cookie and the header must
    be present and equal. (Cookie/header names come from settings;
    the aliases above match the defaults.)"""
    if not csrf_cookie or not csrf_header or not tokens_equal(csrf_cookie, csrf_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF token.",
        )
