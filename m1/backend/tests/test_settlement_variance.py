"""Current-variance derivation service tests (M1 Milestone 7, part 2)
-- SQLite fallback suite. The service is exercised directly with
seeded rows, including states the API cannot produce (a calculation
whose snapshot disagrees with the batch), so the derivation and the
NULL-reason helper are covered independently of the endpoints.

Covers: no calculation, happy path (exact value + seq), highest-seq
wins only when its inputs still match (an older matching calculation
stays current), not_fully_allocated, receipt_incomplete,
expected_changed, input_set_changed (new receipt / new allocation),
and a snapshot/net disagreement.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.db.auth_models import Merchant, MerchantMembership, User
from app.db.models import (
    BankReceipt,
    MerchantCourierAccount,
    Courier,
    SettlementAllocation,
    SettlementBatch,
    SettlementVarianceCalculation,
    SettlementVarianceCalculationInput,
)
from app.db.session import SessionLocal
from app.services import settlement_variance


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def env(db):
    user = User(email="m7-var@example.com", password_hash="x", status="active")
    db.add(user)
    db.flush()
    merchant = Merchant(name="M7 Variance Shop", created_by_user_id=user.id)
    db.add(merchant)
    db.flush()
    db.add(MerchantMembership(
        merchant_id=merchant.id, user_id=user.id, role="owner", status="active"
    ))
    courier = Courier(code="steadfast", name="Steadfast (synthetic test row)")
    db.add(courier)
    db.flush()
    account = MerchantCourierAccount(merchant_id=merchant.id, courier_id=courier.id)
    db.add(account)
    db.commit()
    return {"user": user, "merchant": merchant, "account": account}


def _batch(db, env, expected=100000, status="fully_allocated"):
    batch = SettlementBatch(
        merchant_id=env["merchant"].id,
        merchant_courier_account_id=env["account"].id,
        expected_batch_payout_poisha=expected,
        allocated_total_poisha=100000 if status == "fully_allocated" else 60000,
        allocation_status=status,
        bank_status="fully_reconciled" if status == "fully_allocated" else "partial_receipt",
    )
    db.add(batch)
    db.flush()
    return batch


def _receipt(db, env, batch, amount=100000, complete=True):
    receipt = BankReceipt(
        merchant_id=env["merchant"].id,
        settlement_batch_id=batch.id,
        amount_poisha=amount,
        is_complete=complete,
        # The CHECK ck_bank_receipts_complete_requires_actor requires
        # an actor + timestamp exactly when is_complete is true.
        marked_complete_by_user_id=env["user"].id if complete else None,
        marked_complete_at=datetime.now(timezone.utc) if complete else None,
    )
    db.add(receipt)
    db.flush()
    return receipt


def _allocation(db, env, batch, receipt, amount=100000, kind="allocation",
                reverses=None):
    alloc = SettlementAllocation(
        merchant_id=env["merchant"].id,
        settlement_batch_id=batch.id,
        bank_receipt_id=receipt.id,
        allocated_poisha=amount,
        kind=kind,
        reverses_allocation_id=reverses,
        created_by_user_id=env["user"].id,
    )
    db.add(alloc)
    db.flush()
    return alloc


def _calc(db, env, batch, seq, expected=100000, allocated=100000,
          receipts=(), allocs=()):
    calc = SettlementVarianceCalculation(
        merchant_id=env["merchant"].id,
        settlement_batch_id=batch.id,
        calculation_seq=seq,
        expected_batch_payout_poisha=expected,
        allocated_bank_confirmed_total_poisha=allocated,
        bank_variance_poisha=allocated - expected,
        computed_by_user_id=env["user"].id,
        actor_type="user",
    )
    db.add(calc)
    db.flush()
    for receipt in receipts:
        db.add(SettlementVarianceCalculationInput(
            merchant_id=env["merchant"].id,
            settlement_batch_id=batch.id,
            calculation_id=calc.id,
            input_kind="receipt",
            bank_receipt_id=receipt.id,
        ))
    for alloc in allocs:
        db.add(SettlementVarianceCalculationInput(
            merchant_id=env["merchant"].id,
            settlement_batch_id=batch.id,
            calculation_id=calc.id,
            input_kind="allocation",
            settlement_allocation_id=alloc.id,
        ))
    db.flush()
    return calc


def _seed_full(db, env, expected=100000):
    """Batch + complete receipt + full allocation, no calculation."""
    batch = _batch(db, env, expected=expected)
    receipt = _receipt(db, env, batch)
    alloc = _allocation(db, env, batch, receipt, amount=expected)
    db.commit()
    return batch, receipt, alloc


def test_no_calculation(db, env):
    batch, _, _ = _seed_full(db, env)
    assert settlement_variance.get_current_variance(
        db, env["merchant"].id, batch.id
    ) == {
        "bank_variance_poisha": None,
        "calculation_seq": None,
        "variance_status": "no_calculation",
    }
    assert settlement_variance.current_bank_variance_poisha(
        db, env["merchant"].id, batch.id
    ) is None


def test_happy_path_returns_exact_value_and_seq(db, env):
    batch, receipt, alloc = _seed_full(db, env)
    _calc(db, env, batch, seq=1, receipts=[receipt], allocs=[alloc])
    db.commit()
    assert settlement_variance.get_current_variance(
        db, env["merchant"].id, batch.id
    ) == {
        "bank_variance_poisha": 0,
        "calculation_seq": 1,
        "variance_status": "ok",
    }


def test_older_matching_calculation_stays_current(db, env):
    batch, receipt, alloc = _seed_full(db, env)
    _calc(db, env, batch, seq=1, receipts=[receipt], allocs=[alloc])
    # A newer calculation whose listed inputs do NOT match the current
    # set (it lists no allocation) is skipped; seq 1 stays current.
    _calc(db, env, batch, seq=2, receipts=[receipt], allocs=[])
    db.commit()
    assert settlement_variance.get_current_variance(
        db, env["merchant"].id, batch.id
    ) == {
        "bank_variance_poisha": 0,
        "calculation_seq": 1,
        "variance_status": "ok",
    }


def test_not_fully_allocated(db, env):
    batch = _batch(db, env, status="partially_allocated")
    receipt = _receipt(db, env, batch, amount=60000)
    alloc = _allocation(db, env, batch, receipt, amount=60000)
    _calc(db, env, batch, seq=1, allocated=60000,
          receipts=[receipt], allocs=[alloc])
    db.commit()
    result = settlement_variance.get_current_variance(
        db, env["merchant"].id, batch.id
    )
    assert result["bank_variance_poisha"] is None
    assert result["variance_status"] == "not_fully_allocated"


def test_receipt_incomplete(db, env):
    batch, receipt, alloc = _seed_full(db, env)
    _calc(db, env, batch, seq=1, receipts=[receipt], allocs=[alloc])
    receipt.is_complete = False
    db.commit()
    result = settlement_variance.get_current_variance(
        db, env["merchant"].id, batch.id
    )
    assert result["bank_variance_poisha"] is None
    assert result["variance_status"] == "receipt_incomplete"


def test_expected_changed(db, env):
    batch, receipt, alloc = _seed_full(db, env)
    _calc(db, env, batch, seq=1, receipts=[receipt], allocs=[alloc])
    batch.expected_batch_payout_poisha = 120000
    db.commit()
    result = settlement_variance.get_current_variance(
        db, env["merchant"].id, batch.id
    )
    assert result["bank_variance_poisha"] is None
    assert result["variance_status"] == "expected_changed"


def test_input_set_changed_new_receipt(db, env):
    batch, receipt, alloc = _seed_full(db, env)
    _calc(db, env, batch, seq=1, receipts=[receipt], allocs=[alloc])
    _receipt(db, env, batch, amount=5000, complete=True)
    db.commit()
    result = settlement_variance.get_current_variance(
        db, env["merchant"].id, batch.id
    )
    assert result["bank_variance_poisha"] is None
    assert result["variance_status"] == "input_set_changed"


def test_input_set_changed_new_allocation(db, env):
    batch, receipt, alloc = _seed_full(db, env)
    _calc(db, env, batch, seq=1, receipts=[receipt], allocs=[alloc])
    # A reversal row is a new allocation input: the old calculation
    # no longer lists the current set.
    _allocation(db, env, batch, receipt, amount=100000,
                kind="reversal", reverses=alloc.id)
    db.commit()
    result = settlement_variance.get_current_variance(
        db, env["merchant"].id, batch.id
    )
    assert result["bank_variance_poisha"] is None
    assert result["variance_status"] == "input_set_changed"


def test_snapshot_net_disagreement_is_null(db, env):
    batch, receipt, alloc = _seed_full(db, env)
    # Snapshot claims an allocated total of 90000 while the listed
    # allocation nets to 100000: the derivation rejects it (the
    # arithmetic CHECK still holds: -10000 == 90000 - 100000).
    _calc(db, env, batch, seq=1, allocated=90000,
          receipts=[receipt], allocs=[alloc])
    db.commit()
    result = settlement_variance.get_current_variance(
        db, env["merchant"].id, batch.id
    )
    assert result["bank_variance_poisha"] is None
    assert result["variance_status"] == "input_set_changed"


def test_unknown_batch_returns_none(db, env):
    assert settlement_variance.get_current_variance(
        db, env["merchant"].id, uuid.uuid4()
    ) is None
    assert settlement_variance.current_bank_variance_poisha(
        db, env["merchant"].id, uuid.uuid4()
    ) is None
