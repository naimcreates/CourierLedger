"""Idempotency lease service tests (B-3) -- SQLite fallback suite.

Exercises the same predicates the PostgreSQL implementation uses
(claim / replay / conflict / complete / fail / renew / reclaim /
attempt cap), against the test-only SQLite fallback. The decisive
clock test -- clock_timestamp() vs now() -- cannot run on SQLite and
lives in tests/test_idempotency_lease_live_pg.py.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.auth_models import Merchant, User
from app.db.models import Courier, FileObject, IdempotencyKey, ImportBatch, MerchantCourierAccount
from app.db.session import SessionLocal
from app.services import idempotency_service as svc


def _utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def merchant(db):
    user = User(email="lease-user@example.com", password_hash="x", status="active")
    db.add(user)
    db.flush()
    m = Merchant(name="Lease Shop", created_by_user_id=user.id)
    db.add(m)
    db.commit()
    return m


@pytest.fixture()
def batch(db, merchant):
    courier = Courier(code="steadfast", name="Steadfast (synthetic test row)")
    db.add(courier)
    db.flush()
    account = MerchantCourierAccount(merchant_id=merchant.id, courier_id=courier.id)
    db.add(account)
    db.flush()
    user = db.query(User).first()
    fo = FileObject(
        merchant_id=merchant.id,
        storage_key="local://lease-test/file.csv",
        original_filename="file.csv",
        sha256="a" * 64,
        size_bytes=10,
        content_type="text/csv",
        uploaded_by_user_id=user.id,
    )
    db.add(fo)
    db.flush()
    b = ImportBatch(
        merchant_id=merchant.id,
        file_object_id=fo.id,
        merchant_courier_account_id=account.id,
        created_by_user_id=user.id,
    )
    db.add(b)
    db.commit()
    return b


def test_claim_then_complete(db, merchant, batch):
    outcome, row = svc.claim(
        db, merchant.id, "import_create", "key-1", "hash-1", "worker-1"
    )
    assert outcome == "claimed"
    assert row.status == "in_progress"
    assert row.claim_token is not None
    assert row.lease_expires_at is not None
    assert row.attempt_count == 0
    assert row.expires_at is not None

    # Second claim while the lease is live: conflict, no work.
    with pytest.raises(svc.IdempotencyConflictError):
        svc.claim(db, merchant.id, "import_create", "key-1", "hash-1", "worker-2")

    # Completion with the fencing token succeeds exactly once...
    assert svc.complete(db, merchant.id, "import_create", "key-1", row.claim_token, batch.id) is True
    db.commit()
    finished = db.get(IdempotencyKey, row.id)
    assert finished.status == "completed"
    assert finished.lease_expires_at is None
    assert finished.completed_at is not None
    assert finished.result_import_batch_id == batch.id
    # ...and a second completion affects zero rows.
    assert svc.complete(db, merchant.id, "import_create", "key-1", row.claim_token, batch.id) is False

    # A completed key with the same hash replays instead of re-running.
    outcome, replayed = svc.claim(db, merchant.id, "import_create", "key-1", "hash-1", "worker-3")
    assert outcome == "replay"
    assert replayed.result_import_batch_id == batch.id


def test_hash_mismatch_is_conflict(db, merchant):
    svc.claim(db, merchant.id, "import_create", "key-2", "hash-a", "worker-1")
    with pytest.raises(svc.IdempotencyConflictError):
        svc.claim(db, merchant.id, "import_create", "key-2", "hash-b", "worker-1")


def test_complete_with_wrong_token_fails(db, merchant, batch):
    _, row = svc.claim(db, merchant.id, "import_create", "key-3", "hash-1", "worker-1")
    assert svc.complete(db, merchant.id, "import_create", "key-3", uuid.uuid4(), batch.id) is False
    # The claim is still in progress for its real owner.
    assert svc.complete(db, merchant.id, "import_create", "key-3", row.claim_token, batch.id) is True


def test_complete_after_expiry_affects_zero_rows(db, merchant, batch):
    _, row = svc.claim(
        db, merchant.id, "import_create", "key-4", "hash-1", "worker-1", lease_seconds=300
    )
    # Move the lease into the past (as the database would see it after
    # a lapse); completion must now affect zero rows.
    row.lease_expires_at = _utcnow() - timedelta(minutes=1)
    db.commit()
    assert svc.complete(db, merchant.id, "import_create", "key-4", row.claim_token, batch.id) is False
    assert db.get(IdempotencyKey, row.id).status == "in_progress"


def test_renew_before_and_after_expiry(db, merchant):
    _, row = svc.claim(db, merchant.id, "import_create", "key-5", "hash-1", "worker-1")
    assert svc.renew(db, merchant.id, "import_create", "key-5", row.claim_token) is True
    row.lease_expires_at = _utcnow() - timedelta(minutes=1)
    db.commit()
    assert svc.renew(db, merchant.id, "import_create", "key-5", row.claim_token) is False


def test_worker_failure_before_and_after_expiry(db, merchant):
    _, row = svc.claim(db, merchant.id, "import_create", "key-6", "hash-1", "worker-1")
    assert svc.fail(db, merchant.id, "import_create", "key-6", row.claim_token) is True
    assert db.get(IdempotencyKey, row.id).status == "failed"
    # A failed key cannot be re-claimed with the same key.
    with pytest.raises(svc.IdempotencyConflictError):
        svc.claim(db, merchant.id, "import_create", "key-6", "hash-1", "worker-1")

    _, row2 = svc.claim(db, merchant.id, "import_create", "key-7", "hash-1", "worker-1")
    row2.lease_expires_at = _utcnow() - timedelta(minutes=1)
    db.commit()
    assert svc.fail(db, merchant.id, "import_create", "key-7", row2.claim_token) is False
    assert db.get(IdempotencyKey, row2.id).status == "in_progress"


def test_reclaim_after_expiry_new_token_old_token_loses(db, merchant, batch):
    _, row = svc.claim(db, merchant.id, "import_create", "key-8", "hash-1", "worker-1")
    old_token = row.claim_token
    row.lease_expires_at = _utcnow() - timedelta(minutes=1)
    db.commit()
    outcome, reclaimed = svc.claim(db, merchant.id, "import_create", "key-8", "hash-1", "worker-2")
    assert outcome == "claimed"
    assert reclaimed.claim_token != old_token
    assert reclaimed.attempt_count == 1
    # The old token lost: completion with it affects zero rows.
    assert svc.complete(db, merchant.id, "import_create", "key-8", old_token, batch.id) is False
    # The new token wins.
    assert svc.complete(db, merchant.id, "import_create", "key-8", reclaimed.claim_token, batch.id) is True


def test_attempt_cap_fails_the_key(db, merchant):
    _, row = svc.claim(db, merchant.id, "import_create", "key-9", "hash-1", "worker-1")
    for _ in range(svc.ATTEMPT_CAP):
        row.lease_expires_at = _utcnow() - timedelta(minutes=1)
        db.commit()
        outcome, row = svc.claim(db, merchant.id, "import_create", "key-9", "hash-1", "worker-1")
        assert outcome == "claimed"
    assert row.attempt_count == svc.ATTEMPT_CAP
    row.lease_expires_at = _utcnow() - timedelta(minutes=1)
    db.commit()
    with pytest.raises(svc.IdempotencyConflictError):
        svc.claim(db, merchant.id, "import_create", "key-9", "hash-1", "worker-1")
    assert db.get(IdempotencyKey, row.id).status == "failed"


def test_unknown_operation_rejected(db, merchant):
    with pytest.raises(ValueError):
        svc.claim(db, merchant.id, "no_such_op", "key-10", "hash-1", "worker-1")
