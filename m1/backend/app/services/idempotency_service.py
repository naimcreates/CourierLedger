"""Idempotency lease service (M1, B-3 fix).

Implements the CL-14 / CL-19 / CL-24 conditional-update predicates
documented in app/db/roles.sql ("Idempotency lifecycle predicates").
The service owns every worker-initiated idempotency_keys transition;
no other module may UPDATE idempotency_keys directly.

LEASE CLOCK (the point of B-3): every lease comparison uses the
DATABASE clock at statement-execution time -- PostgreSQL
``clock_timestamp()`` -- NEVER ``now()``, which is transaction-start
time. A worker that lets its lease lapse mid-operation loses the
operation: completion is the last write of the work transaction and a
zero-row completion means "roll the whole work transaction back"
(CL-24). The choice is stated here and in the SQL the service builds
(``func.clock_timestamp()`` below); the live tests in
tests/test_idempotency_lease_live_pg.py prove it with a transaction
that sleeps past its own lease (``now()`` would still call the lease
valid; ``clock_timestamp()`` does not).

Uniform ownership rule (CL-24): a worker owns a claim only while
status = 'in_progress' AND claim_token matches AND lease_expires_at
is later than the database clock at execution. Completion,
worker-reported failure and renewal ALL require all three; zero rows
means ownership was lost -- stop and roll back, do not complete.

Two clocks, never conflated (CL-14): ``expires_at`` is the replay
horizon (24h design default); ``lease_expires_at`` is the ownership
horizon (lease durations: 5 min import_create, 10 min run_create).

Transaction discipline: these functions execute statements but NEVER
commit -- the caller owns the transaction (claim + work + complete
must be atomic for the work, and a failed complete must roll the work
back). They may roll back to a SAVEPOINT internally (concurrent-claim
handling in claim()) but never commit the outer transaction.

SQLite: the test-only fallback has no clock_timestamp(), so the same
predicates are built with datetime('now') there. The SQLite branch
exists ONLY so the pytest suite exercises the same predicates without
a server; PostgreSQL is the authoritative implementation and the live
tests prove the clock semantics there.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import IdempotencyKey
from app.db.session import set_tenant_context
from app.logging_config import get_logger

logger = get_logger(__name__)

OPERATIONS = ("import_create", "run_create")
LEASE_SECONDS = {"import_create": 5 * 60, "run_create": 10 * 60}
REPLAY_HORIZON = timedelta(hours=24)
ATTEMPT_CAP = 5


class IdempotencyConflictError(Exception):
    """The key exists but cannot be (re)claimed: hash mismatch, another
    live owner, a failed key, or the attempt cap was reached. The
    caller must not proceed with the work; a retry needs a new key."""


def _is_postgresql(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _clock_now(db: Session):
    """Database clock at statement-execution time.

    PostgreSQL: clock_timestamp() -- NOT now() (transaction-start
    time). SQLite fallback: datetime('now')."""
    if _is_postgresql(db):
        return func.clock_timestamp()
    return func.datetime("now")


def _clock_plus(db: Session, seconds: int):
    """Database clock + ``seconds`` (the new lease / replay horizon)."""
    if _is_postgresql(db):
        # make_interval(secs => N) is the interval constructor; the
        # value is bound, never interpolated into SQL text.
        return func.clock_timestamp() + text(
            "make_interval(secs => :seconds)"
        ).bindparams(seconds=seconds)
    return func.datetime("now", f"+{seconds} seconds")


def _lease_predicate(db: Session, claim_token: uuid.UUID):
    """CL-24 uniform ownership predicate, as a WHERE fragment builder:
    status in_progress AND matching claim_token AND lease unexpired
    against the database clock. Used by complete/fail/renew."""
    return (
        (IdempotencyKey.status == "in_progress")
        & (IdempotencyKey.claim_token == claim_token)
        & (IdempotencyKey.lease_expires_at > _clock_now(db))
    )


def _identity(db: Session, merchant_id, operation: str, key: str):
    """Base WHERE for one key: tenant + operation + key. Callers add
    the state predicate. Tenant context is asserted first (RLS)."""
    set_tenant_context(db, merchant_id)
    return (
        (IdempotencyKey.merchant_id == merchant_id)
        & (IdempotencyKey.operation == operation)
        & (IdempotencyKey.idempotency_key == key)
    )


def _get_row(db: Session, merchant_id, operation: str, key: str) -> IdempotencyKey | None:
    set_tenant_context(db, merchant_id)
    return db.execute(
        select(IdempotencyKey)
        .where(
            (IdempotencyKey.merchant_id == merchant_id)
            & (IdempotencyKey.operation == operation)
            & (IdempotencyKey.idempotency_key == key)
        )
        .with_for_update()
    ).scalar_one_or_none()


def claim(
    db: Session,
    merchant_id,
    operation: str,
    key: str,
    request_hash: str,
    claimed_by_instance: str,
    lease_seconds: int | None = None,
) -> tuple[str, IdempotencyKey]:
    """Claim (or reclaim / replay) an idempotency key.

    Returns (outcome, row) where outcome is:
      "claimed"  -- fresh claim, or a reclaim after an expired lease
                    (new claim_token; the caller owns the work).
      "replay"   -- the key already completed with the same request
                    hash; the caller must return the stored result and
                    do NO work.
    Raises IdempotencyConflictError when the key exists with a
    different request hash (CL-04), is owned live by another worker,
    previously failed, or hit the attempt cap (CL-14: 5 attempts).
    The caller owns the returned row's claim_token and must pass it to
    complete()/fail()/renew(); the token is the fencing token.
    """
    if operation not in OPERATIONS:
        raise ValueError(f"Unknown idempotency operation: {operation!r}")
    if lease_seconds is None:
        lease_seconds = LEASE_SECONDS[operation]
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    set_tenant_context(db, merchant_id)

    row = _get_row(db, merchant_id, operation, key)
    if row is None:
        # First claim. A concurrent claim may win the race; the
        # UNIQUE (merchant_id, operation, idempotency_key) constraint
        # raises IntegrityError, in which case re-read the winner
        # inside the savepoint and fall through to the existing-row
        # logic below instead of failing the caller's transaction.
        # The INSERT is a Core insert (not an ORM add) so the lease /
        # replay horizons are computed by the DATABASE clock in SQL
        # (clock_timestamp() on PostgreSQL); reading them back into
        # Python first would hand the ORM a string on SQLite.
        token = uuid.uuid4()
        new_id = uuid.uuid4()
        try:
            with db.begin_nested():
                db.execute(
                    insert(IdempotencyKey).values(
                        id=new_id,
                        merchant_id=merchant_id,
                        operation=operation,
                        idempotency_key=key,
                        request_hash=request_hash,
                        status="in_progress",
                        expires_at=_clock_plus(db, int(REPLAY_HORIZON.total_seconds())),
                        lease_expires_at=_clock_plus(db, lease_seconds),
                        claim_token=token,
                        attempt_count=0,
                        claimed_by_instance=claimed_by_instance,
                    )
                )
        except IntegrityError:
            row = _get_row(db, merchant_id, operation, key)
            if row is None:  # pragma: no cover - defensive
                raise
        else:
            row = db.get(IdempotencyKey, new_id)
            logger.info("idempotency_claimed", extra={"operation": operation, "key": key})
            return "claimed", row

    # Existing row (first read, or the race winner).
    if row.request_hash != request_hash:
        raise IdempotencyConflictError(
            f"Idempotency key {key!r} already used with a different request."
        )
    if row.status == "completed":
        return "replay", row
    if row.status == "failed":
        raise IdempotencyConflictError(
            f"Idempotency key {key!r} previously failed; use a new key to retry."
        )
    # In progress: live lease means another worker owns it.
    live = db.execute(
        select(IdempotencyKey.id).where(
            (IdempotencyKey.id == row.id)
            & (IdempotencyKey.status == "in_progress")
            & (IdempotencyKey.lease_expires_at > _clock_now(db))
        )
    ).scalar_one_or_none()
    if live is not None:
        raise IdempotencyConflictError(
            f"Idempotency key {key!r} is already in progress."
        )
    # Expired lease: reclaim (CL-14) if attempts remain, else cap-fail.
    if row.attempt_count >= ATTEMPT_CAP:
        db.execute(
            update(IdempotencyKey)
            .where(
                (IdempotencyKey.id == row.id)
                & (IdempotencyKey.status == "in_progress")
                & (IdempotencyKey.lease_expires_at <= _clock_now(db))
                & (IdempotencyKey.attempt_count >= ATTEMPT_CAP)
            )
            .values(status="failed", lease_expires_at=None)
        )
        raise IdempotencyConflictError(
            f"Idempotency key {key!r} reached the attempt cap ({ATTEMPT_CAP})."
        )
    new_token = uuid.uuid4()
    reclaimed = db.execute(
        update(IdempotencyKey)
        .where(
            (IdempotencyKey.id == row.id)
            & (IdempotencyKey.status == "in_progress")
            & (IdempotencyKey.lease_expires_at <= _clock_now(db))
            & (IdempotencyKey.attempt_count < ATTEMPT_CAP)
        )
        .values(
            claim_token=new_token,
            lease_expires_at=_clock_plus(db, lease_seconds),
            attempt_count=IdempotencyKey.attempt_count + 1,
            claimed_by_instance=claimed_by_instance,
        )
    ).rowcount
    if reclaimed == 0:
        # Lost the race to another reclaimer: exactly one winner.
        raise IdempotencyConflictError(
            f"Idempotency key {key!r} was reclaimed by another worker."
        )
    db.refresh(row)
    logger.info("idempotency_reclaimed", extra={"operation": operation, "key": key})
    return "claimed", row


def complete(
    db: Session,
    merchant_id,
    operation: str,
    key: str,
    claim_token: uuid.UUID,
    result_id,
) -> bool:
    """Complete a claim (CL-24). Sets status completed, completed_at,
    the typed result reference for ``operation``, and clears the lease
    -- in ONE conditional UPDATE requiring the full ownership
    predicate. Returns True when exactly one row was completed.
    False means ownership was lost (lease lapsed, token mismatch, or
    already finished): the caller MUST roll its work transaction back
    and must NOT treat the work as done."""
    if operation not in OPERATIONS:
        raise ValueError(f"Unknown idempotency operation: {operation!r}")
    result_column = (
        "result_import_batch_id" if operation == "import_create"
        else "result_reconciliation_run_id"
    )
    affected = db.execute(
        update(IdempotencyKey)
        .where(_identity(db, merchant_id, operation, key) & _lease_predicate(db, claim_token))
        .values(
            status="completed",
            completed_at=_clock_now(db),
            lease_expires_at=None,
            **{result_column: result_id},
        )
    ).rowcount
    if affected == 1:
        logger.info("idempotency_completed", extra={"operation": operation, "key": key})
    return affected == 1


def fail(
    db: Session,
    merchant_id,
    operation: str,
    key: str,
    claim_token: uuid.UUID,
) -> bool:
    """Worker-reported failure (CL-24). Same ownership predicate as
    complete(); sets status failed and clears the lease. Returns True
    when exactly one row was failed; False means ownership was lost
    (the row stays in_progress for a reclaimer / the sweeper)."""
    affected = db.execute(
        update(IdempotencyKey)
        .where(_identity(db, merchant_id, operation, key) & _lease_predicate(db, claim_token))
        .values(status="failed", lease_expires_at=None)
    ).rowcount
    return affected == 1


def renew(
    db: Session,
    merchant_id,
    operation: str,
    key: str,
    claim_token: uuid.UUID,
    lease_seconds: int | None = None,
) -> bool:
    """Lease renewal (CL-24). Same ownership predicate; extends the
    lease from the database clock. Returns True when exactly one row
    was renewed; False means ownership was lost -- stop and roll back,
    do not complete."""
    if lease_seconds is None:
        lease_seconds = LEASE_SECONDS[operation]
    affected = db.execute(
        update(IdempotencyKey)
        .where(_identity(db, merchant_id, operation, key) & _lease_predicate(db, claim_token))
        .values(lease_expires_at=_clock_plus(db, lease_seconds))
    ).rowcount
    return affected == 1
