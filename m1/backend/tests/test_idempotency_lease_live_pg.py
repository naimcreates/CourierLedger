"""Live-PostgreSQL idempotency lease tests (B-3, CL-14/CL-24).

Runs against a scratch PostgreSQL database (schema + RLS + roles
applied; no HTTP server -- these tests drive the service directly).
Skipped unless CL_LIVE_PG_PASSWORD is set.

Proves, against real PostgreSQL:
  * completion before expiry succeeds; after expiry it affects zero
    rows and the row stays in_progress;
  * the lease clock is clock_timestamp() (statement-execution wall
    clock), NOT now() (transaction-start time): a transaction that
    sleeps past its own 2-second lease must fail to complete -- with
    now() it would wrongly succeed;
  * a reclaim race has exactly one winner (old token loses, new token
    wins, a third claimant conflicts while the new lease is live);
  * renewal and worker-reported failure obey the same ownership
    predicate; hash mismatch conflicts; a completed key replays.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.db.models import IdempotencyKey
from app.services import idempotency_service as svc
from tests.live_pg import (
    app_dsn,
    drop_scratch_db,
    live_pg_enabled,
    pg_host,
    setup_scratch_db,
)

pytestmark = pytest.mark.skipif(
    not live_pg_enabled(), reason="live-PG tests need CL_LIVE_PG_PASSWORD"
)

PASSWORD = os.environ.get("CL_LIVE_PG_PASSWORD", "")


@pytest.fixture(scope="module")
def env():
    setup_scratch_db()
    engine = None
    try:
        merchant_id, user_id, batch_id = _seed()
        engine = create_engine(app_dsn(), pool_size=1, max_overflow=0)
        factory = sessionmaker(bind=engine)
        yield {"merchant_id": merchant_id, "user_id": user_id,
               "batch_id": batch_id, "Session": factory}
    finally:
        if engine is not None:
            engine.dispose()  # close pooled connections or DROP DATABASE hangs
        drop_scratch_db()


def _owner_conn():
    return psycopg.connect(
        f"host={pg_host()} dbname=courierledger_live_test "
        f"user=courierledger password={PASSWORD}"
    )


def _seed():
    """User + merchant (via the controlled function, as the runtime
    role) + courier/account/file/batch (raw SQL as the owner role,
    with the merchant context set -- the owner is FORCE-RLS policed
    on those tables). All synthetic."""
    user_id = uuid.uuid4()
    with _owner_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name, password_hash, status, created_at, updated_at) "
                "VALUES (%s, 'lease-live@example.com', 'Lease Live', 'x', 'active', now(), now())",
                (str(user_id),),
            )
    with psycopg.connect(
        f"host={pg_host()} dbname=courierledger_live_test "
        f"user=courierledger_runtime password={PASSWORD}"
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT public.create_workspace(%s, 'Lease Live Shop', NULL)", (str(user_id),))
            merchant_id = cur.fetchone()[0]
    courier_id = uuid.uuid4()
    account_id = uuid.uuid4()
    file_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    with _owner_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_merchant_id', %s, true)", (str(merchant_id),))
            # The 'steadfast' code is already seeded by
            # app/db/seed_couriers.sql (applied by setup_scratch_db);
            # reuse that reference row instead of inserting a duplicate.
            cur.execute("SELECT id FROM couriers WHERE code = 'steadfast'")
            courier_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO merchant_courier_accounts (id, merchant_id, courier_id, status) "
                "VALUES (%s, %s, %s, 'active')",
                (str(account_id), str(merchant_id), str(courier_id)),
            )
            cur.execute(
                "INSERT INTO file_objects (id, merchant_id, storage_key, original_filename, sha256, "
                "size_bytes, content_type, uploaded_by_user_id) "
                "VALUES (%s, %s, 'local://lease-live/file.csv', 'file.csv', %s, 10, 'text/csv', %s)",
                (str(file_id), str(merchant_id), "b" * 64, str(user_id)),
            )
            cur.execute(
                "INSERT INTO import_batches (id, merchant_id, file_object_id, "
                "merchant_courier_account_id, created_by_user_id, status, "
                "row_count, parsed_row_count, error_row_count) "
                "VALUES (%s, %s, %s, %s, %s, 'uploaded', 0, 0, 0)",
                (str(batch_id), str(merchant_id), str(file_id), str(account_id), str(user_id)),
            )
        conn.commit()
    return merchant_id, user_id, batch_id


def _fetch(env, key: str) -> dict | None:
    """Read a key's state as plain scalars (the ORM row is detached
    once its session closes, so attributes must be read inside it)."""
    session = env["Session"]()
    try:
        from app.db.session import set_tenant_context

        set_tenant_context(session, env["merchant_id"])
        row = session.execute(
            select(IdempotencyKey).where(
                (IdempotencyKey.merchant_id == env["merchant_id"])
                & (IdempotencyKey.operation == "import_create")
                & (IdempotencyKey.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "status": row.status,
            "claim_token": row.claim_token,
            "lease_expires_at": row.lease_expires_at,
            "completed_at": row.completed_at,
            "result_import_batch_id": row.result_import_batch_id,
            "attempt_count": row.attempt_count,
        }
    finally:
        session.close()


def _expire_lease(env, key: str) -> None:
    """Move a key's lease into the past (test helper, not a service
    operation): the runtime role, tenant context set, conditional on
    the key still being in_progress."""
    from app.db.session import set_tenant_context

    session = env["Session"]()
    try:
        set_tenant_context(session, env["merchant_id"])
        session.execute(
            text("UPDATE idempotency_keys SET lease_expires_at = clock_timestamp() - interval '1 minute' "
                 "WHERE merchant_id = :mid AND operation = 'import_create' AND idempotency_key = :key"),
            {"mid": env["merchant_id"], "key": key},
        )
        session.commit()
    finally:
        session.close()


def test_complete_before_expiry_succeeds(env):
    session = env["Session"]()
    try:
        outcome, row = svc.claim(
            session, env["merchant_id"], "import_create", "live-key-1", "hash-1", "worker-1"
        )
        assert outcome == "claimed"
        assert svc.complete(
            session, env["merchant_id"], "import_create", "live-key-1",
            row.claim_token, env["batch_id"],
        ) is True
        session.commit()
    finally:
        session.close()
    finished = _fetch(env, "live-key-1")
    assert finished["status"] == "completed"
    assert finished["lease_expires_at"] is None
    assert finished["completed_at"] is not None
    assert finished["result_import_batch_id"] == env["batch_id"]


def test_complete_after_expiry_affects_zero_rows(env):
    session = env["Session"]()
    try:
        outcome, row = svc.claim(
            session, env["merchant_id"], "import_create", "live-key-2", "hash-1", "worker-1"
        )
        assert outcome == "claimed"
        token_2 = row.claim_token
        session.commit()
    finally:
        session.close()
    _expire_lease(env, "live-key-2")
    session = env["Session"]()
    try:
        assert svc.complete(
            session, env["merchant_id"], "import_create", "live-key-2",
            token_2, env["batch_id"],
        ) is False
        session.commit()
    finally:
        session.close()
    assert _fetch(env, "live-key-2")["status"] == "in_progress"


def test_clock_is_statement_time_not_transaction_time(env):
    """Decisive clock test: claim a 2-second lease, then sleep 3
    seconds INSIDE the same transaction and try to complete. now()
    is transaction-start time, so it would still call the lease valid
    and complete; clock_timestamp() is statement-execution time, so
    the lease has lapsed and completion must affect zero rows."""
    session = env["Session"]()
    try:
        with session.begin():
            outcome, row = svc.claim(
                session, env["merchant_id"], "import_create", "live-key-3", "hash-1",
                "worker-1", lease_seconds=2,
            )
            assert outcome == "claimed"
            session.execute(text("SELECT pg_sleep(3)"))
            assert svc.complete(
                session, env["merchant_id"], "import_create", "live-key-3",
                row.claim_token, env["batch_id"],
            ) is False
    finally:
        session.close()
    assert _fetch(env, "live-key-3")["status"] == "in_progress"


def test_reclaim_race_has_exactly_one_winner(env):
    session = env["Session"]()
    try:
        outcome, row = svc.claim(
            session, env["merchant_id"], "import_create", "live-key-4", "hash-1", "worker-1"
        )
        assert outcome == "claimed"
        old_token = row.claim_token
        session.commit()
    finally:
        session.close()
    _expire_lease(env, "live-key-4")
    session = env["Session"]()
    try:
        outcome, reclaimed = svc.claim(
            session, env["merchant_id"], "import_create", "live-key-4", "hash-1", "worker-2"
        )
        assert outcome == "claimed"
        assert reclaimed.claim_token != old_token
        assert reclaimed.attempt_count == 1
        # A third claimant conflicts while the new lease is live.
        with pytest.raises(svc.IdempotencyConflictError):
            svc.claim(
                session, env["merchant_id"], "import_create", "live-key-4", "hash-1", "worker-3"
            )
        # The old token lost: completion with it affects zero rows.
        assert svc.complete(
            session, env["merchant_id"], "import_create", "live-key-4",
            old_token, env["batch_id"],
        ) is False
        # The new token wins.
        assert svc.complete(
            session, env["merchant_id"], "import_create", "live-key-4",
            reclaimed.claim_token, env["batch_id"],
        ) is True
        session.commit()
    finally:
        session.close()


def test_renew_and_fail_obey_ownership(env):
    session = env["Session"]()
    try:
        _, row = svc.claim(
            session, env["merchant_id"], "import_create", "live-key-5", "hash-1", "worker-1"
        )
        assert svc.renew(
            session, env["merchant_id"], "import_create", "live-key-5", row.claim_token
        ) is True
        assert svc.fail(
            session, env["merchant_id"], "import_create", "live-key-5", row.claim_token
        ) is True
        session.commit()
    finally:
        session.close()
    assert _fetch(env, "live-key-5")["status"] == "failed"

    session = env["Session"]()
    try:
        _, row = svc.claim(
            session, env["merchant_id"], "import_create", "live-key-6", "hash-1", "worker-1"
        )
        token_6 = row.claim_token
        session.commit()
    finally:
        session.close()
    _expire_lease(env, "live-key-6")
    session = env["Session"]()
    try:
        assert svc.renew(
            session, env["merchant_id"], "import_create", "live-key-6", token_6
        ) is False
        assert svc.fail(
            session, env["merchant_id"], "import_create", "live-key-6", token_6
        ) is False
        session.commit()
    finally:
        session.close()
    assert _fetch(env, "live-key-6")["status"] == "in_progress"


def test_hash_mismatch_conflicts_and_completed_key_replays(env):
    session = env["Session"]()
    try:
        svc.claim(
            session, env["merchant_id"], "import_create", "live-key-7", "hash-a", "worker-1"
        )
        with pytest.raises(svc.IdempotencyConflictError):
            svc.claim(
                session, env["merchant_id"], "import_create", "live-key-7", "hash-b", "worker-1"
            )
        session.commit()
    finally:
        session.close()
    # Complete it, then the same key+hash replays.
    token_7 = _fetch(env, "live-key-7")["claim_token"]
    session = env["Session"]()
    try:
        assert svc.complete(
            session, env["merchant_id"], "import_create", "live-key-7",
            token_7, env["batch_id"],
        ) is True
        outcome, replayed = svc.claim(
            session, env["merchant_id"], "import_create", "live-key-7", "hash-a", "worker-2"
        )
        assert outcome == "replay"
        assert replayed.result_import_batch_id == env["batch_id"]
        session.commit()
    finally:
        session.close()
