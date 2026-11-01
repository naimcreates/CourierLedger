"""Database engine / session wiring (sync SQLAlchemy + psycopg).

PostgreSQL is the only production target. A sqlite:// DATABASE_URL is
accepted ONLY as a clearly-labelled temporary fallback for the health
smoke test and the pytest suite while no PostgreSQL server is
available; the M1 data model is NOT built on SQLite (see README.md).

Tenant context (v0.1 Section E, v0.2 CL-11, v0.2 CL-06):
- set_tenant_context() runs transaction-local
  SELECT set_config('app.current_merchant_id', '<uuid>', true).
- The merchant id MUST come from server-side session state (the
  authenticated user's verified membership), never from a request
  parameter, header or body field.
- A malformed / non-UUID value is set to NULL and the call returns
  False — fail closed, no exception (CL-06): a NULL right-hand side
  matches no rows under RLS, so the request sees nothing rather than
  erroring or leaking another tenant.
- There is no default tenant anywhere in the design.
"""

import uuid
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        # TEST-ONLY fallback. Not the M1 data model, not production.
        logger.warning("sqlite_database_url_in_use", extra={"note": "test-only fallback, not production"})
        return create_engine(url, connect_args={"check_same_thread": False})
    # Pool-reset guarantee (CL-11, B-4): tenant/user context is set
    # transaction-local only (set_config(..., true) in
    # set_tenant_context/set_user_context), so it ends with the
    # transaction; SQLAlchemy's pool reset rolls back the transaction
    # when a connection returns to the pool, so a pooled connection
    # can never carry a previous request's context into the next
    # checkout. Verified live in tests/test_runtime_role_live_pg.py
    # (test_pooled_connection_context_does_not_leak).
    # Pool values are explicit (Phase 2) and equal the SQLAlchemy
    # defaults that were in effect before: at most 15 connections
    # per backend process. pool_recycle=1800 retires connections
    # after 30 minutes so none lives forever. No external pooler is
    # configured; if one is ever added it MUST be PgBouncer in
    # transaction mode (v0.1 §E.5) — session-mode pooling is forbidden
    # because tenant context is transaction-local.
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
    )


engine: Engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_connection() -> bool:
    """True only if a real round-trip to the configured database
    succeeds (SELECT 1). Used by /api/health; never assumed."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - health must not raise
        logger.warning("database_health_check_failed", extra={"error": str(exc)})
        return False


def database_kind() -> str:
    """'postgresql' | 'sqlite-test-only' | 'unknown', from the URL scheme."""
    url = settings.database_url
    if url.startswith("postgresql"):
        return "postgresql"
    if url.startswith("sqlite"):
        return "sqlite-test-only"
    return "unknown"


def set_tenant_context(db: Session, merchant_id: uuid.UUID | str | None) -> bool:
    """Set the transaction-local RLS tenant context from a server-side
    verified merchant id. Returns True when a valid UUID context was
    set, False when the value was missing/malformed (context set to
    NULL — fail closed, per CL-06/CL-11). Never raises for bad input.
    Callers must pass only ids verified against merchant_memberships
    for the authenticated user."""
    valid: str | None = None
    if merchant_id is not None:
        try:
            valid = str(uuid.UUID(str(merchant_id)))
        except (ValueError, AttributeError, TypeError):
            valid = None
    if valid is None:
        logger.warning("tenant_context_rejected_malformed_or_missing")
    # set_config() is PostgreSQL-only. On the temporary test-only
    # SQLite fallback the statement is skipped (no RLS exists there);
    # the UUID validation above is still exercised and its result
    # still returned, so callers behave identically.
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        logger.debug("tenant_context_skipped_non_postgresql_dialect")
        return valid is not None
    db.execute(
        text("SELECT set_config('app.current_merchant_id', :mid, true)"),
        {"mid": valid},
    )
    return valid is not None


def set_user_context(db: Session, user_id: uuid.UUID | str | None) -> bool:
    """Set the transaction-local RLS user context (v0.1 E.2, B-1 fix).

    Mirrors set_tenant_context exactly: the setting is 'app.user_id'
    (the name v0.1 E.2 gives it -- note the asymmetry with the merchant
    setting, which this build names 'app.current_merchant_id' per the
    kickoff brief; see IMPLEMENTATION_CHECKLIST.md discrepancy 2).
    A malformed / non-UUID value is set to NULL and the call returns
    False -- fail closed, no exception (CL-06): a NULL user context
    matches no membership row, so the request sees no workspace rather
    than erroring or leaking another tenant's. Called from
    api/deps.py:get_current_user() on every authenticated request, so
    the membership-based merchants/memberships SELECT policies in
    rls.sql can resolve the caller's workspaces. Never raises for bad
    input. The user id MUST come from server-side session state, never
    from a request parameter, header or body field."""
    valid: str | None = None
    if user_id is not None:
        try:
            valid = str(uuid.UUID(str(user_id)))
        except (ValueError, AttributeError, TypeError):
            valid = None
    if valid is None:
        logger.warning("user_context_rejected_malformed_or_missing")
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        logger.debug("user_context_skipped_non_postgresql_dialect")
        return valid is not None
    db.execute(
        text("SELECT set_config('app.user_id', :uid, true)"),
        {"uid": valid},
    )
    return valid is not None
