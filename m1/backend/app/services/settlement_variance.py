"""Current bank-variance derivation (M1 Milestone 7, CL-16 / CL-21).

Read-side only: nothing here writes. The current bank variance of a
settlement batch is DERIVED, never stored (CL-16 removed the stored
``bank_variance_poisha`` column): it is the highest-seq
SettlementVarianceCalculation whose recorded input set exactly equals
the batch's current receipt/allocation set and whose preconditions
still hold; otherwise the current variance is NULL and the old
calculation rows stay untouched.

This ports CURRENT_VARIANCE_SQL from tests/test_triggers_live_pg.py
verbatim (plus the calculation_seq column the API needs), using
SQLAlchemy text() with bound parameters -- never string-formatted
values. UUID parameters are bound with type Uuid so the same query
runs on PostgreSQL (native UUID) and on the test-only SQLite
fallback (CHAR(32) hex).

NULL-reason strings (checked in this order, matching the derivation's
preconditions):
  ok                  -- a current calculation exists.
  no_calculation      -- no calculation has ever been recorded.
  not_fully_allocated -- the batch is not fully_allocated.
  receipt_incomplete  -- some receipt linked to the batch is not complete.
  expected_changed    -- the newest calculation snapshots a different
                         expected payout than the batch now expects.
  input_set_changed   -- the newest calculation's listed receipt /
                         allocation set (or its net) no longer equals
                         the batch's current set. A new allocation
                         after a calculation supersedes it this way:
                         the old rows are never edited or deleted.
"""

from __future__ import annotations

import uuid

from sqlalchemy import bindparam, select, text, Uuid
from sqlalchemy.orm import Session

from app.db.models import (
    BankReceipt,
    SettlementAllocation,
    SettlementBatch,
    SettlementVarianceCalculation,
    SettlementVarianceCalculationInput,
)

# Port of CURRENT_VARIANCE_SQL (tests/test_triggers_live_pg.py), with
# calculation_seq selected as well. %s placeholders become named
# bound parameters.
_CURRENT_VARIANCE_STMT = text(
    """
SELECT c.bank_variance_poisha, c.calculation_seq
  FROM settlement_variance_calculations c
  JOIN settlement_batches b
    ON b.merchant_id = c.merchant_id AND b.id = c.settlement_batch_id
 WHERE c.merchant_id = :merchant_id AND c.settlement_batch_id = :batch_id
   AND b.allocation_status = 'fully_allocated'
   AND b.expected_batch_payout_poisha IS NOT NULL
   AND c.expected_batch_payout_poisha = b.expected_batch_payout_poisha
   AND NOT EXISTS (
     SELECT 1 FROM bank_receipts r
      WHERE r.merchant_id = c.merchant_id
        AND r.settlement_batch_id = c.settlement_batch_id
        AND r.is_complete IS NOT TRUE)
   AND NOT EXISTS (
     SELECT 1 FROM settlement_variance_calculation_inputs i
      WHERE i.merchant_id = c.merchant_id AND i.calculation_id = c.id
        AND i.input_kind = 'receipt'
        AND NOT EXISTS (
          SELECT 1 FROM bank_receipts r
           WHERE r.merchant_id = i.merchant_id
             AND r.settlement_batch_id = i.settlement_batch_id
             AND r.id = i.bank_receipt_id))
   AND NOT EXISTS (
     SELECT 1 FROM bank_receipts r
      WHERE r.merchant_id = c.merchant_id
        AND r.settlement_batch_id = c.settlement_batch_id
        AND NOT EXISTS (
          SELECT 1 FROM settlement_variance_calculation_inputs i
           WHERE i.merchant_id = r.merchant_id AND i.calculation_id = c.id
             AND i.input_kind = 'receipt' AND i.bank_receipt_id = r.id))
   AND NOT EXISTS (
     SELECT 1 FROM settlement_variance_calculation_inputs i
      WHERE i.merchant_id = c.merchant_id AND i.calculation_id = c.id
        AND i.input_kind = 'allocation'
        AND NOT EXISTS (
          SELECT 1 FROM settlement_allocations a
           WHERE a.merchant_id = i.merchant_id
             AND a.settlement_batch_id = i.settlement_batch_id
             AND a.id = i.settlement_allocation_id))
   AND NOT EXISTS (
     SELECT 1 FROM settlement_allocations a
      WHERE a.merchant_id = c.merchant_id
        AND a.settlement_batch_id = c.settlement_batch_id
        AND NOT EXISTS (
          SELECT 1 FROM settlement_variance_calculation_inputs i
           WHERE i.merchant_id = a.merchant_id AND i.calculation_id = c.id
             AND i.input_kind = 'allocation'
             AND i.settlement_allocation_id = a.id))
   AND (SELECT COALESCE(sum(CASE WHEN a.kind = 'reversal'
                               THEN -a.allocated_poisha ELSE a.allocated_poisha END), 0)
          FROM settlement_allocations a
         WHERE a.merchant_id = c.merchant_id
           AND a.id IN (
             SELECT i.settlement_allocation_id
               FROM settlement_variance_calculation_inputs i
              WHERE i.merchant_id = c.merchant_id AND i.calculation_id = c.id
                AND i.input_kind = 'allocation'))
       = c.allocated_bank_confirmed_total_poisha
 ORDER BY c.calculation_seq DESC
 LIMIT 1
"""
).bindparams(
    bindparam("merchant_id", type_=Uuid),
    bindparam("batch_id", type_=Uuid),
)


def _coerce_uuid(value):
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _net_allocated(allocations) -> int:
    return sum(
        -a.allocated_poisha if a.kind == "reversal" else a.allocated_poisha
        for a in allocations
    )


def _null_reason(db: Session, batch: SettlementBatch) -> str:
    """Why the derivation returns NULL for this batch (checked in the
    documented order). Only called when the derivation found no
    current calculation."""
    merchant_id = batch.merchant_id
    calcs = db.execute(
        select(SettlementVarianceCalculation)
        .where(
            (SettlementVarianceCalculation.merchant_id == merchant_id)
            & (SettlementVarianceCalculation.settlement_batch_id == batch.id)
        )
        .order_by(SettlementVarianceCalculation.calculation_seq.desc())
    ).scalars().all()
    if not calcs:
        return "no_calculation"
    if batch.allocation_status != "fully_allocated":
        return "not_fully_allocated"
    incomplete = db.execute(
        select(BankReceipt.id)
        .where(
            (BankReceipt.merchant_id == merchant_id)
            & (BankReceipt.settlement_batch_id == batch.id)
            & (BankReceipt.is_complete.is_(False))
        )
        .limit(1)
    ).first()
    if incomplete is not None:
        return "receipt_incomplete"
    latest = calcs[0]
    if (
        batch.expected_batch_payout_poisha is None
        or latest.expected_batch_payout_poisha != batch.expected_batch_payout_poisha
    ):
        return "expected_changed"
    inputs = db.execute(
        select(SettlementVarianceCalculationInput)
        .where(
            (SettlementVarianceCalculationInput.merchant_id == merchant_id)
            & (SettlementVarianceCalculationInput.calculation_id == latest.id)
        )
    ).scalars().all()
    listed_receipts = {
        i.bank_receipt_id for i in inputs if i.input_kind == "receipt"
    }
    listed_allocs = {
        i.settlement_allocation_id for i in inputs if i.input_kind == "allocation"
    }
    current_receipts = {
        r.id
        for r in db.execute(
            select(BankReceipt)
            .where(
                (BankReceipt.merchant_id == merchant_id)
                & (BankReceipt.settlement_batch_id == batch.id)
            )
        ).scalars().all()
    }
    current_allocs = {
        a.id
        for a in db.execute(
            select(SettlementAllocation)
            .where(
                (SettlementAllocation.merchant_id == merchant_id)
                & (SettlementAllocation.settlement_batch_id == batch.id)
            )
        ).scalars().all()
    }
    if listed_receipts != current_receipts or listed_allocs != current_allocs:
        return "input_set_changed"
    # Input sets agree, so the only remaining derivation condition is
    # the net-from-listed-allocations equality. Amounts are immutable
    # and rows are append-only, so this can only disagree when the
    # snapshot itself was written wrong (direct seeding / a bug); it
    # is still an input-set disagreement in substance.
    listed_alloc_rows = [
        a for a in db.execute(
            select(SettlementAllocation)
            .where(
                (SettlementAllocation.merchant_id == merchant_id)
                & (SettlementAllocation.settlement_batch_id == batch.id)
            )
        ).scalars().all()
        if a.id in listed_allocs
    ]
    if _net_allocated(listed_alloc_rows) != latest.allocated_bank_confirmed_total_poisha:
        return "input_set_changed"
    # Unreachable when the derivation above is correct: every
    # precondition holds for the newest calculation, so it would have
    # been returned. Fail closed with the generic reason rather than
    # claiming "ok" with a NULL value.
    return "input_set_changed"


def get_current_variance(
    db: Session, merchant_id, batch_id
) -> dict | None:
    """Current variance for one batch.

    Returns {"bank_variance_poisha": int | None,
             "calculation_seq": int | None,
             "variance_status": str} or None when the batch is not in
    this workspace. Never writes. The batch's tenant context must
    already be set by the caller (settlement_service does this)."""
    merchant_id = _coerce_uuid(merchant_id)
    batch_id = _coerce_uuid(batch_id)
    if merchant_id is None or batch_id is None:
        return None
    batch = db.get(SettlementBatch, batch_id)
    if batch is None or batch.merchant_id != merchant_id:
        return None
    row = db.execute(
        _CURRENT_VARIANCE_STMT,
        {"merchant_id": merchant_id, "batch_id": batch_id},
    ).first()
    if row is not None:
        return {
            "bank_variance_poisha": row[0],
            "calculation_seq": row[1],
            "variance_status": "ok",
        }
    return {
        "bank_variance_poisha": None,
        "calculation_seq": None,
        "variance_status": _null_reason(db, batch),
    }


def current_bank_variance_poisha(db: Session, merchant_id, batch_id) -> int | None:
    """Convenience for list views: just the current variance value
    (None when there is none, or when the batch is not in this
    workspace)."""
    result = get_current_variance(db, merchant_id, batch_id)
    if result is None:
        return None
    return result["bank_variance_poisha"]
