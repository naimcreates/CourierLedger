"""CourierLedger M1 backend — application configuration.

Environment-based only (pydantic-settings). No hardcoded secrets:
SECRET_KEY / SESSION_SECRET ship as CHANGE_ME placeholders and MUST be
replaced via the environment (see ../../.env.example) before any
non-local use.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL is the target database. The application connects as the
    # RUNTIME role (courierledger_runtime, B-4 fix) -- never as the owner
    # role. The owner role (courierledger) is for Alembic migrations and
    # admin SQL only; see MIGRATION_DATABASE_URL in alembic/env.py and
    # ../../.env.example. A sqlite:// URL is accepted ONLY for the
    # temporary health-endpoint smoke test and the pytest suite when no
    # PostgreSQL server is available; it is NOT the M1 data model and must
    # never be used for production data (see README.md).
    database_url: str = (
        "postgresql+psycopg://courierledger_runtime:CHANGE_ME@127.0.0.1:5432/courierledger"
    )

    # Placeholder — replace via SECRET_KEY env var. Used to sign nothing
    # directly in M0/M1 (sessions are opaque random tokens), but reserved
    # for future signed values; the app refuses to start in production
    # mode with the placeholder (see main.py).
    secret_key: str = "CHANGE_ME_REPLACE_WITH_A_LONG_RANDOM_SECRET"
    session_secret: str = "CHANGE_ME_REPLACE_WITH_A_LONG_RANDOM_SECRET"

    environment: str = "development"  # development | test | production
    log_level: str = "INFO"

    frontend_url: str = "http://127.0.0.1:5173"

    session_cookie_name: str = "cl_session"
    session_max_age_seconds: int = 7 * 24 * 60 * 60  # 7 days
    session_cookie_secure: bool = False  # True behind HTTPS in production
    session_cookie_samesite: str = "lax"

    # Phase 1.1 (B-F11): session lifecycle hardening.
    # Maximum concurrent live sessions per user; on login past the cap
    # the oldest sessions are revoked (evict-oldest, not reject).
    session_max_concurrent: int = 10
    # Step-up verification (POST /api/auth/step-up) stays valid this long.
    step_up_ttl_seconds: int = 10 * 60  # 10 minutes
    # A session created within this window counts as freshly
    # authenticated for step-up purposes (no extra password prompt).
    step_up_fresh_login_seconds: int = 15 * 60  # 15 minutes

    csrf_cookie_name: str = "cl_csrf"
    csrf_header_name: str = "X-CSRF-Token"

    # Dev/test convenience ONLY. When true, the app creates the auth tables
    # (auth_models.Base.metadata.create_all) at startup. Production schema
    # is owned by Alembic migrations (backend/alembic/, another agent).
    # Default false.
    create_tables_on_startup: bool = False

    # Password policy (M1). Phase 1: a maximum length as well — Argon2id
    # hashes the entire input, so an unbounded password is a CPU/memory
    # DoS vector on register/login/reset-confirm (C-1.6 / B-F14).
    password_min_length: int = 12
    password_max_length: int = 128

    # Login rate limiting: max failures per (IP + email) in the window.
    login_max_failures: int = 5
    login_window_seconds: int = 15 * 60  # 15 minutes

    # Phase 1 generic rate limits (B-F5/B-F9): register and
    # password-reset request, per IP; member invites, per workspace.
    # 50/hour per IP: strict enough to stop mass registration, loose
    # enough that the live test suite (many users from 127.0.0.1 in one
    # module run) and small-office NATs are not blocked. Per-IP only;
    # the per-account bucket is the login one (B-F6).
    register_max_attempts: int = 50
    register_window_seconds: int = 60 * 60  # 1 hour
    reset_request_max_attempts: int = 10
    reset_request_window_seconds: int = 60 * 60  # 1 hour
    invite_max_attempts: int = 10
    invite_window_seconds: int = 60 * 60  # 1 hour

    # Password-reset token lifetime.
    password_reset_ttl_seconds: int = 60 * 60  # 1 hour

    # Phase 1.1: password-reset confirm attempts, per IP. The token is
    # 256-bit, so this is a backstop against online guessing loops, not
    # the primary defence; 50/hour matches the register bucket so the
    # live test suite (many confirms from 127.0.0.1) is not blocked.
    reset_confirm_max_attempts: int = 50
    reset_confirm_window_seconds: int = 60 * 60  # 1 hour


settings = Settings()
