"""Auth API (M1): register, login, logout, me, CSRF, password reset.

Sessions are opaque random tokens in an HttpOnly cookie; only the
SHA-256 hash is stored server-side. CSRF uses the double-submit
pattern: GET /api/auth/csrf issues a token (cookie + JSON body) and
every state-changing endpoint requires the same value in the
X-CSRF-Token header.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf
from app.config import settings
from app.db.auth_models import User
from app.db.session import get_db
from app.logging_config import get_logger
from app.security import generate_csrf_token
from app.services import auth_service, rate_limit

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ---- Schemas -------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12)
    display_name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=12)


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str | None
    status: str


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        status=user.status,
    )


def _set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.session_cookie_name, path="/")


def _set_csrf_cookie(response: Response, token: str) -> None:
    # NOT HttpOnly: the frontend JS must read it to send the header.
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        max_age=settings.session_max_age_seconds,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


# ---- Endpoints -----------------------------------------------------

@router.get("/csrf")
def get_csrf(response: Response) -> dict:
    token = generate_csrf_token()
    _set_csrf_cookie(response, token)
    return {"csrf_token": token}


@router.post("/register", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_csrf)])
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> UserOut:
    try:
        user = auth_service.register_user(db, body.email, body.password, body.display_name)
    except auth_service.EmailTakenError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _user_out(user)


@router.post("/login", dependencies=[Depends(require_csrf)])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> UserOut:
    client_ip = request.client.host if request.client else "unknown"
    email = body.email
    if rate_limit.is_blocked(client_ip, email):
        logger.warning("login_rate_limited", extra={"email": auth_service.normalize_email(email)})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
        )
    try:
        user = auth_service.authenticate(db, email, body.password)
    except auth_service.AuthError:
        rate_limit.record_failure(client_ip, email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password."
        )
    rate_limit.record_success(client_ip, email)
    device_label = request.headers.get("User-Agent", "")[:200] or None
    raw_token = auth_service.create_session(db, user, device_label=device_label)
    _set_session_cookie(response, raw_token)
    return _user_out(user)


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if raw_token:
        auth_service.revoke_session(db, raw_token)
    _clear_session_cookie(response)
    return {"detail": "Logged out."}


@router.post("/revoke-all", dependencies=[Depends(require_csrf)])
def revoke_all(
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    auth_service.revoke_all_sessions(db, user)
    _clear_session_cookie(response)
    return {"detail": "All sessions revoked."}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_out(user)


@router.post("/password-reset/request", dependencies=[Depends(require_csrf)])
def password_reset_request(body: PasswordResetRequest, db: Session = Depends(get_db)) -> dict:
    # Always 200 — never reveal whether the email is registered.
    auth_service.request_password_reset(db, body.email)
    return {"detail": "If an account exists for this email, a reset link has been issued."}


@router.post("/password-reset/confirm", dependencies=[Depends(require_csrf)])
def password_reset_confirm(body: PasswordResetConfirm, db: Session = Depends(get_db)) -> dict:
    try:
        auth_service.confirm_password_reset(db, body.token, body.new_password)
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"detail": "Password has been reset. Please log in with your new password."}
