"""Phase 3 (Stream A) maintenance-job tests — live PostgreSQL only.

Run with the OWNER role (the default — the purge needs DELETE on
user_sessions / password_reset_tokens, which no application role
holds by design; the runtime role would fail with
InsufficientPrivilege and delete nothing):
  CL_LIVE_PG_PASSWORD=<owner password from m1/backend/.env>
  .venv/bin/python -m pytest tests/test_maintenance_live_pg.py -q

Proves, live:
- run_maintenance deletes ONLY expired sessions and expired/used
  reset tokens, and keeps live sessions, revoked-but-unexpired
  sessions, and live tokens;
- a second run deletes nothing (idempotent);
- --dry-run counts without deleting;
- a concurrent run holding the advisory lock is reported as
  ``skipped`` instead of racing;
- an audit_logs row (append-only history) survives the purge
  untouched.

Do NOT run these against SQLite: the job refuses non-PostgreSQL
sessions by design, and SQLite has no advisory locks or RLS.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.db.auth_models import Merchant, PasswordResetToken, User, UserSession
from app.db.models import AuditLog
from app.services import maintenance_service
from tests.live_pg import (
    drop_scratch_db,
    live_pg_enabled,
    owner_dsn,
    setup_scratch_db,
)

pytestmark = pytest.mark.skipif(
    not (live_pg_enabled()
         and os.environ.get("CL_LIVE_PG_ROLE", "courierledger") == "courierledger"),
    reason="maintenance live tests need CL_LIVE_PG_PASSWORD as the owner role",
)


@pytest.fixture(scope="module")
def owner_session_factory():
    setup_scratch_db()
    engine = create_engine(owner_dsn(), pool_pre_ping=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                           expire_on_commit=False)
    yield factory
    engine.dispose()
    drop_scratch_db()


def _seed(factory):
    """One user + a mix of deletable and must-keep auth rows + one
    append-only audit row. Returns the token_hashes of the rows that
    must survive, so tests can assert on them by value.

    The audit row is seeded under a merchant context: INSERT into
    audit_logs allows a NULL merchant_id, but SQLAlchemy's INSERT
    ... RETURNING is also checked against the SELECT policy, and a
    NULL-merchant row is invisible to tenant sessions (fail-closed,
    by design — see rls.sql). Production audit rows carry the
    merchant context, so the seed does the same.
    """
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=6)
    with factory() as db:
        user = User(email="maint-test@example.com", password_hash="x",
                    display_name="Maint Test")
        db.add(user)
        db.flush()
        merchant = Merchant(name="Maint Test Merchant",
                            created_by_user_id=user.id)
        db.add(merchant)
        db.flush()
        db.execute(
            text("SELECT set_config('app.current_merchant_id', :mid, true)"),
            {"mid": str(merchant.id)},
        )
        db.add(UserSession(user_id=user.id, token_hash="expired-session",
                           expires_at=past))
        db.add(UserSession(user_id=user.id, token_hash="revoked-session",
                           expires_at=future, revoked_at=past))
        db.add(UserSession(user_id=user.id, token_hash="live-session",
                           expires_at=future))
        db.add(PasswordResetToken(user_id=user.id, token_hash="expired-token",
                                  expires_at=past))
        db.add(PasswordResetToken(user_id=user.id, token_hash="used-token",
                                  expires_at=future, used_at=past))
        db.add(PasswordResetToken(user_id=user.id, token_hash="live-token",
                                  expires_at=future))
        db.add(AuditLog(merchant_id=merchant.id, actor_type="user",
                        actor_user_id=user.id,
                        action="test.maintenance_seed", detail={}))
        db.commit()
    return {
        "merchant_id": merchant.id,
        "keep_sessions": {"revoked-session", "live-session"},
        "keep_tokens": {"live-token"},
    }


def _row_exists(factory, model, token_hash):
    with factory() as db:
        return db.execute(
            select(model).where(model.token_hash == token_hash)
        ).scalar_one_or_none() is not None


def _audit_count(factory, merchant_id):
    # Audit SELECT is tenant-scoped: the count is only visible under
    # the merchant context (fail-closed without one).
    with factory() as db:
        db.execute(
            text("SELECT set_config('app.current_merchant_id', :mid, true)"),
            {"mid": str(merchant_id)},
        )
        return db.execute(select(func.count()).select_from(AuditLog)).scalar_one()


def test_purge_deletes_only_expired_rows(owner_session_factory):
    keep = _seed(owner_session_factory)
    assert _audit_count(owner_session_factory, keep["merchant_id"]) == 1
    with owner_session_factory() as db:
        result = maintenance_service.run_maintenance(db)
    assert result["status"] == "completed"
    assert result == {"status": "completed", "dry_run": False,
                      "sessions": 1, "reset_tokens": 2}
    assert not _row_exists(owner_session_factory, UserSession, "expired-session")
    assert not _row_exists(owner_session_factory, PasswordResetToken, "expired-token")
    assert not _row_exists(owner_session_factory, PasswordResetToken, "used-token")
    for token_hash in keep["keep_sessions"]:
        assert _row_exists(owner_session_factory, UserSession, token_hash)
    for token_hash in keep["keep_tokens"]:
        assert _row_exists(owner_session_factory, PasswordResetToken, token_hash)
    # Append-only history untouched.
    assert _audit_count(owner_session_factory, keep["merchant_id"]) == 1


def test_maintenance_is_idempotent(owner_session_factory):
    # The previous test already purged everything expired; a second
    # run must report zero deletions, not fail or delete the kept rows.
    with owner_session_factory() as db:
        result = maintenance_service.run_maintenance(db)
    assert result == {"status": "completed", "dry_run": False,
                      "sessions": 0, "reset_tokens": 0}
    assert _row_exists(owner_session_factory, UserSession, "live-session")
    assert _row_exists(owner_session_factory, PasswordResetToken, "live-token")


def test_dry_run_deletes_nothing(owner_session_factory):
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    with owner_session_factory() as db:
        user = db.execute(
            select(User).where(User.email == "maint-test@example.com")
        ).scalar_one()
        db.add(UserSession(user_id=user.id, token_hash="dryrun-session",
                           expires_at=past))
        db.commit()
    with owner_session_factory() as db:
        result = maintenance_service.run_maintenance(db, dry_run=True)
    assert result == {"status": "dry_run", "dry_run": True,
                      "sessions": 1, "reset_tokens": 0}
    # Still there afterwards — a dry run must not delete.
    assert _row_exists(owner_session_factory, UserSession, "dryrun-session")
    # And a real run afterwards removes exactly that one row.
    with owner_session_factory() as db:
        result = maintenance_service.run_maintenance(db)
    assert result["sessions"] == 1 and result["reset_tokens"] == 0
    assert not _row_exists(owner_session_factory, UserSession, "dryrun-session")


def test_concurrent_run_is_skipped_by_advisory_lock(owner_session_factory):
    # Hold the lock on a separate session, as a second overlapping
    # process would; the run must report skipped, not delete or error.
    with owner_session_factory() as holder:
        assert holder.execute(
            select(func.pg_try_advisory_lock(
                maintenance_service.MAINTENANCE_LOCK_KEY))
        ).scalar_one() is True
        try:
            with owner_session_factory() as db:
                result = maintenance_service.run_maintenance(db)
        finally:
            holder.execute(
                select(func.pg_advisory_unlock(
                    maintenance_service.MAINTENANCE_LOCK_KEY))
            )
    assert result == {"status": "skipped", "reason": "lock_held"}
    # Nothing was deleted by the skipped run.
    assert _row_exists(owner_session_factory, UserSession, "live-session")
