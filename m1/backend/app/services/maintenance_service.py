"""Scheduled maintenance jobs (Phase 3, Stream A — Workstream 4).

This module is the ONLY approved housekeeping entry point. It wraps
the purge function the codebase already provides and adds the three
things a scheduled job needs that the function alone does not:

  * a PostgreSQL advisory lock, so two overlapping runs (timer +
    manual invocation, or a timer firing twice) cannot delete the
    same rows twice or race each other — the second run reports
    ``status: skipped`` instead of failing;
  * a ``--dry-run`` count path that reports what WOULD be deleted
    without deleting anything;
  * a single structured result dict the CLI (``m1/scripts/
    maintenance.py``) logs as one JSON line, so failures are visible
    in the journal instead of silent.

Scope — deliberately narrow (see DATABASE_BACKUP_RECOVERY.md and the
Phase 3 brief): the ONLY deletion this job performs is the one
``auth_service.purge_expired_auth_rows`` already implements (B-F19):

  * ``user_sessions`` rows whose ``expires_at`` has passed
    (expired, whether revoked or not — a revoked-but-unexpired
    session is KEPT, because purge is by expiry, not revocation);
  * ``password_reset_tokens`` rows that are expired OR already used.

What this job NEVER does:

  * never deletes active records (the predicates above are the
    whole deletion set — there is no other DELETE in this module);
  * never touches immutable reconciliation history
    (``reconciliation_results`` / ``reconciliation_runs`` /
    ``reconciliation_fee_lines`` / ``reconciliation_findings``),
    settlement history, or ``audit_logs`` (append-only, CL-03/CL-10/
    CL-16) — those tables appear in no statement here;
  * never runs the idempotency-key purge (CL-19): the maintenance
    role holds a DELETE grant on ``idempotency_keys`` in roles.sql,
    but NO purge function for it exists in the codebase, so there
    is nothing approved to run. Adding one is a reviewed change,
    not a scheduler decision — see DATABASE_BACKUP_RECOVERY.md.

Role: the job MUST run as the table-owner role (``courierledger``),
never as the runtime role. roles.sql grants NO DELETE on
``user_sessions`` / ``password_reset_tokens`` to either application
role by design — session-expiry cleanup is a maintenance DELETE
under a controlled procedure, not a standing grant — so the runtime
role would fail with InsufficientPrivilege and delete nothing.
The CLI resolves the owner credential the same way
``m1/scripts/provision_db.sh`` does (``MIGRATION_DATABASE_URL``,
falling back to the ``DATABASE_URL`` password, which shares the
local-dev value). PostgreSQL only: this module refuses to run on
any other dialect (the SQLite fallback in the test suite is not
the M1 data model and must never be used for maintenance).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.auth_models import PasswordResetToken, UserSession
from app.logging_config import get_logger
from app.services import auth_service

logger = get_logger(__name__)

# Advisory-lock key for the maintenance job. A fixed 64-bit key in
# the application's namespace (date of the Phase 3 maintenance
# design, 2026-11-02). Held for the whole run on the job's own
# session; released in a finally block (or by session close).
# Any other maintenance job that must not overlap this one should
# reuse this key; a job that MAY overlap must use a different one.
MAINTENANCE_LOCK_KEY = 20261102


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_postgresql(db: Session) -> None:
    if db.bind is None or db.bind.dialect.name != "postgresql":
        raise RuntimeError(
            "maintenance runs on PostgreSQL only; refusing non-PostgreSQL session"
        )


def count_expired_auth_rows(db: Session) -> dict:
    """Count the rows purge_expired_auth_rows would delete, using the
    exact same predicates. Read-only: no DELETE is issued."""
    _require_postgresql(db)
    now = _utcnow()
    sessions = db.execute(
        select(func.count()).select_from(UserSession).where(UserSession.expires_at <= now)
    ).scalar_one()
    tokens = db.execute(
        select(func.count())
        .select_from(PasswordResetToken)
        .where(
            (PasswordResetToken.expires_at <= now)
            | (PasswordResetToken.used_at.is_not(None))
        )
    ).scalar_one()
    return {"sessions": int(sessions), "reset_tokens": int(tokens)}


def run_maintenance(db: Session, *, dry_run: bool = False) -> dict:
    """Run the approved maintenance job on an owner-role session.

    Returns one of:
      {"status": "completed", "sessions": n, "reset_tokens": m,
       "dry_run": false}
      {"status": "dry_run", "sessions": n, "reset_tokens": m,
       "dry_run": true}          (nothing deleted)
      {"status": "skipped", "reason": "lock_held"}   (another run
       holds the advisory lock — not an error, not a retry)

    Raises RuntimeError on a non-PostgreSQL session, and propagates
    any database error to the caller (the CLI logs it and exits
    non-zero — a failed maintenance run must be visible, never
    silently swallowed).
    """
    _require_postgresql(db)
    locked = db.execute(
        select(func.pg_try_advisory_lock(MAINTENANCE_LOCK_KEY))
    ).scalar_one()
    if not locked:
        logger.warning("maintenance_skipped_lock_held")
        return {"status": "skipped", "reason": "lock_held"}
    try:
        if dry_run:
            counts = count_expired_auth_rows(db)
            logger.info("maintenance_dry_run", extra=counts)
            return {"status": "dry_run", "dry_run": True, **counts}
        counts = auth_service.purge_expired_auth_rows(db)
        logger.info("maintenance_completed", extra=counts)
        return {"status": "completed", "dry_run": False, **counts}
    finally:
        # Session-level lock: survives the COMMIT inside
        # purge_expired_auth_rows, so it must be released explicitly.
        # Best-effort: if the purge failed mid-transaction the
        # session may be unusable, in which case closing it (the
        # CLI always does) releases the lock anyway.
        try:
            db.rollback()
            db.execute(select(func.pg_advisory_unlock(MAINTENANCE_LOCK_KEY)))
        except Exception:  # noqa: BLE001 - lock release must not mask the run result
            logger.warning("maintenance_lock_release_failed")
