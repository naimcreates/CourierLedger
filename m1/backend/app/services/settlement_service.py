"""Settlement batches, bank receipts, allocations and bank-variance
calculation (M1 Milestone 7, part 2).

Money is integer poisha. No bank account number is accepted or stored
anywhere here (the BankReceipt table has no such column, by design);
no matching is ever done on amount alone -- receipts are linked to a
batch only by the merchant creating them under that batch, never
auto-linked. No real bank integration exists; a receipt is complete
only when the merchant explicitly marks it complete, never inferred
from amounts matching.

Allocation-status derivation (implemented in _recompute_batch, the
single place batch statuses are set):
  * net = allocations minus reversals over the batch's allocation rows.
  * allocated_total_poisha is NULL until at least one allocation row
    exists (model docstring), else the net.
  * allocation_status: "unallocated" when there are no allocation rows
    or the net is 0; "fully_allocated" when every receipt linked to
    the batch has been allocated to this batch in full, i.e. the net
    allocated to the batch equals the total amount of its linked
    receipts (per-receipt over-allocation is impossible, CL-10, so
    this is exactly "each linked receipt fully allocated here");
    otherwise "partially_allocated". This is the v0.1 T-17 meaning:
    a partial receipt allocated in part, then a second receipt, then
    "complete allocation" moves the batch to fully_allocated -- it is
    about the receipts being fully allocated, NOT about the net
    equalling the expected payout. (An earlier draft defined
    fully_allocated as net == expected payout; that made every
    recorded bank variance exactly 0 by construction and made a
    variance impossible precisely when receipts fall short of or
    exceed the expectation -- the cases bank variance exists for.
    It also contradicted the M6 CL-16 live test, where a -40000
    variance committed on a fully_allocated batch.) A NULL expected
    payout does not block fully_allocated -- it only blocks variance
    calculation, which 422s on expected_payout_null.
  * bank_status: "no_receipt" when the batch has no receipts;
    "fully_reconciled" when fully_allocated AND every receipt is
    complete; "receipt_complete_unallocated" when at least one receipt
    is complete but the batch is not fully reconciled; otherwise
    "partial_receipt".

Variance arithmetic: bank_variance = net allocated - expected
payout, so it is informative exactly when receipts differ from the
expectation: receipts totalling 60000 against an expected 100000,
fully allocated, give variance -40000; receipts totalling 120000
give +20000. A variance is recorded only after full allocation and
receipt completion (CL-16 preconditions); a later receipt or
allocation supersedes it by derivation until a new calculation is
recorded.

Append-only discipline: allocation rows are never UPDATED or
DELETED; a correction is a new reversal row. Variance calculation
rows and their input rows are never edited or deleted either -- a
new allocation or receipt after a calculation supersedes it by
derivation (the CL-16/CL-21 current variance becomes NULL until a
new calculation is recorded). The only UPDATEs in this module are
the sanctioned status/total updates on settlement_batches and the
explicit completion marking on bank_receipts.

Transaction discipline: each mutating function owns one transaction
and commits on success; on any failure the caller must roll back
(API layer does). A deferred-trigger failure at commit (CL-10
over-allocation backstop, CL-16 calculation validation) is caught
and raised as SettlementValidationError (422), never a 500.

NOTE on variance arithmetic: bank_variance = net allocated -
expected payout. See the derivation rules above: a fully allocated
batch whose receipts total less (or more) than the expectation
records a genuinely non-zero variance at creation time (e.g.
-40000 for 60000 allocated against 100000 expected).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models import (
    BankReceipt,
    MerchantCourierAccount,
    ReconciliationRun,
    SettlementAllocation,
    SettlementBatch,
    SettlementVarianceCalculation,
    SettlementVarianceCalculationInput,
)
from app.db.session import set_tenant_context
from app.services import audit_service, settlement_variance
from app.services.workspace_service import get_membership


class SettlementNotFoundError(Exception):
    """The batch / receipt / allocation is not in this workspace (API
    maps to 404). No existence is leaked to non-members."""


class SettlementValidationError(Exception):
    """The request cannot be applied (API maps to 422).

    Carries a machine-readable ``reason``."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def _coerce_uuid(value):
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _net_allocated(allocations) -> int:
    return sum(
        -a.allocated_poisha if a.kind == "reversal" else a.allocated_poisha
        for a in allocations
    )


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# Serializers (plain dicts, built BEFORE commit -- commit expires ORM
# attributes and clears the transaction-local tenant context, so
# touching row attributes afterwards could fail under RLS).
# ---------------------------------------------------------------------------

def _batch_dict(batch: SettlementBatch, *, current_variance=None) -> dict:
    return {
        "batch_id": str(batch.id),
        "merchant_id": str(batch.merchant_id),
        "merchant_courier_account_id": str(batch.merchant_courier_account_id),
        "run_id": str(batch.run_id) if batch.run_id else None,
        "period_start": batch.period_start.isoformat() if batch.period_start else None,
        "period_end": batch.period_end.isoformat() if batch.period_end else None,
        "expected_batch_payout_poisha": batch.expected_batch_payout_poisha,
        "allocated_total_poisha": batch.allocated_total_poisha,
        "allocation_status": batch.allocation_status,
        "bank_status": batch.bank_status,
        "current_bank_variance_poisha": current_variance,
        "created_at": _iso(batch.created_at),
        "updated_at": _iso(batch.updated_at),
    }


def _receipt_dict(receipt: BankReceipt) -> dict:
    return {
        "receipt_id": str(receipt.id),
        "merchant_id": str(receipt.merchant_id),
        "settlement_batch_id": (
            str(receipt.settlement_batch_id) if receipt.settlement_batch_id else None
        ),
        "amount_poisha": receipt.amount_poisha,
        "received_on": receipt.received_on.isoformat() if receipt.received_on else None,
        "receipt_ref_label": receipt.receipt_ref_label,
        "is_complete": receipt.is_complete,
        "marked_complete_by_user_id": (
            str(receipt.marked_complete_by_user_id)
            if receipt.marked_complete_by_user_id
            else None
        ),
        "marked_complete_at": _iso(receipt.marked_complete_at),
        "evidence_note": receipt.evidence_note,
        "created_at": _iso(receipt.created_at),
        "updated_at": _iso(receipt.updated_at),
    }


def _allocation_dict(allocation: SettlementAllocation) -> dict:
    return {
        "allocation_id": str(allocation.id),
        "merchant_id": str(allocation.merchant_id),
        "settlement_batch_id": str(allocation.settlement_batch_id),
        "bank_receipt_id": str(allocation.bank_receipt_id),
        "allocated_poisha": allocation.allocated_poisha,
        "kind": allocation.kind,
        "reverses_allocation_id": (
            str(allocation.reverses_allocation_id)
            if allocation.reverses_allocation_id
            else None
        ),
        "created_by_user_id": (
            str(allocation.created_by_user_id) if allocation.created_by_user_id else None
        ),
        "created_at": _iso(allocation.created_at),
    }


def _calculation_dict(calc: SettlementVarianceCalculation) -> dict:
    return {
        "calculation_id": str(calc.id),
        "merchant_id": str(calc.merchant_id),
        "settlement_batch_id": str(calc.settlement_batch_id),
        "calculation_seq": calc.calculation_seq,
        "expected_batch_payout_poisha": calc.expected_batch_payout_poisha,
        "allocated_bank_confirmed_total_poisha": calc.allocated_bank_confirmed_total_poisha,
        "bank_variance_poisha": calc.bank_variance_poisha,
        "computed_at": _iso(calc.computed_at),
        "computed_by_user_id": (
            str(calc.computed_by_user_id) if calc.computed_by_user_id else None
        ),
        "actor_type": calc.actor_type,
    }


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def _require_membership(db: Session, user, merchant_id) -> None:
    merchant_id = _coerce_uuid(merchant_id)
    if merchant_id is None:
        raise SettlementNotFoundError("Workspace not found.")
    if get_membership(db, user.id, merchant_id) is None:
        raise SettlementNotFoundError("Workspace not found.")
    # Tenant context must be set BEFORE any tenant-table read (B-1
    # pattern: the tenant-table SELECT policies key on it).
    set_tenant_context(db, merchant_id)


def _require_batch(db: Session, user, merchant_id, batch_id) -> SettlementBatch:
    merchant_id = _coerce_uuid(merchant_id)
    batch_id = _coerce_uuid(batch_id)
    _require_membership(db, user, merchant_id)
    if batch_id is None:
        raise SettlementNotFoundError("Settlement batch not found.")
    batch = db.get(SettlementBatch, batch_id)
    if batch is None or batch.merchant_id != merchant_id:
        raise SettlementNotFoundError("Settlement batch not found.")
    return batch


def _receipts_for_batch(db: Session, merchant_id, batch_id):
    return db.execute(
        select(BankReceipt)
        .where(
            (BankReceipt.merchant_id == merchant_id)
            & (BankReceipt.settlement_batch_id == batch_id)
        )
        .order_by(BankReceipt.created_at, BankReceipt.id)
    ).scalars().all()


def _allocations_for_batch(db: Session, merchant_id, batch_id):
    return db.execute(
        select(SettlementAllocation)
        .where(
            (SettlementAllocation.merchant_id == merchant_id)
            & (SettlementAllocation.settlement_batch_id == batch_id)
        )
        .order_by(SettlementAllocation.created_at, SettlementAllocation.id)
    ).scalars().all()


def _net_for_receipt(db: Session, merchant_id, receipt_id) -> int:
    rows = db.execute(
        select(SettlementAllocation)
        .where(
            (SettlementAllocation.merchant_id == merchant_id)
            & (SettlementAllocation.bank_receipt_id == receipt_id)
        )
    ).scalars().all()
    return _net_allocated(rows)


def _recompute_batch(db: Session, batch: SettlementBatch) -> None:
    """Re-derive allocated_total_poisha, allocation_status and
    bank_status from the batch's current receipts and allocations.
    See the module docstring for the derivation rules. Caller flushes
    / commits."""
    merchant_id = batch.merchant_id
    allocs = _allocations_for_batch(db, merchant_id, batch.id)
    receipts = _receipts_for_batch(db, merchant_id, batch.id)
    net = _net_allocated(allocs)
    batch.allocated_total_poisha = net if allocs else None
    # Fully allocated = every receipt linked to this batch has been
    # allocated to this batch in full (net == total of linked receipt
    # amounts). It is NOT net == expected payout: the bank variance is
    # precisely the difference between the two, so equating them would
    # make every variance 0 by construction (see module docstring).
    receipts_total = sum(r.amount_poisha for r in receipts)
    if not allocs or net == 0:
        batch.allocation_status = "unallocated"
    elif receipts and net == receipts_total:
        batch.allocation_status = "fully_allocated"
    else:
        batch.allocation_status = "partially_allocated"
    if not receipts:
        batch.bank_status = "no_receipt"
    elif batch.allocation_status == "fully_allocated" and all(
        r.is_complete for r in receipts
    ):
        batch.bank_status = "fully_reconciled"
    elif any(r.is_complete for r in receipts):
        batch.bank_status = "receipt_complete_unallocated"
    else:
        batch.bank_status = "partial_receipt"


def _commit_or_422(db: Session, *, reason: str) -> None:
    """Commit; a deferred-trigger (or other DB) failure at commit is
    a 422 with the given reason, never a 500. Rolls back first."""
    try:
        db.commit()
    except DBAPIError as exc:
        db.rollback()
        detail = str(exc.orig) if exc.orig is not None else str(exc)
        raise SettlementValidationError(detail, reason=reason) from exc


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------

def create_batch(
    db: Session,
    *,
    user,
    merchant_id,
    merchant_courier_account_id,
    run_id=None,
    period_start=None,
    period_end=None,
    expected_batch_payout_poisha=None,
) -> dict:
    """Create a settlement batch (201 shape). Raises
    SettlementNotFoundError when the courier account (or run) is not
    in this workspace, SettlementValidationError when the period is
    inverted."""
    merchant_id = _coerce_uuid(merchant_id)
    account_id = _coerce_uuid(merchant_courier_account_id)
    run_id = _coerce_uuid(run_id)
    _require_membership(db, user, merchant_id)
    if account_id is None:
        raise SettlementNotFoundError("Courier account not found in this workspace.")
    account = db.execute(
        select(MerchantCourierAccount).where(
            (MerchantCourierAccount.merchant_id == merchant_id)
            & (MerchantCourierAccount.id == account_id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise SettlementNotFoundError("Courier account not found in this workspace.")
    if run_id is not None:
        run = db.get(ReconciliationRun, run_id)
        if run is None or run.merchant_id != merchant_id:
            raise SettlementNotFoundError("Reconciliation run not found in this workspace.")
    if (
        period_start is not None
        and period_end is not None
        and period_end < period_start
    ):
        raise SettlementValidationError(
            "period_end must be on or after period_start.",
            reason="invalid_period",
        )
    batch = SettlementBatch(
        merchant_id=merchant_id,
        merchant_courier_account_id=account_id,
        run_id=run_id,
        period_start=period_start,
        period_end=period_end,
        expected_batch_payout_poisha=expected_batch_payout_poisha,
        allocated_total_poisha=None,
        allocation_status="unallocated",
        bank_status="no_receipt",
    )
    db.add(batch)
    db.flush()
    out = _batch_dict(batch, current_variance=None)
    audit_service.record_audit(
        db,
        actor=user,
        merchant_id=merchant_id,
        action=audit_service.SETTLEMENTS_BATCH_CREATED,
        entity_table="settlement_batches",
        entity_id=batch.id,
        detail={"merchant_courier_account_id": str(account_id)},
    )
    _commit_or_422(db, reason="batch_create_failed")
    return out


def list_batches(db: Session, *, user, merchant_id, limit: int = 50, offset: int = 0):
    """(items, total): batches newest first, each with its current
    bank variance (nullable, derived)."""
    merchant_id = _coerce_uuid(merchant_id)
    _require_membership(db, user, merchant_id)
    total = db.execute(
        select(func.count())
        .select_from(SettlementBatch)
        .where(SettlementBatch.merchant_id == merchant_id)
    ).scalar_one()
    batches = db.execute(
        select(SettlementBatch)
        .where(SettlementBatch.merchant_id == merchant_id)
        .order_by(SettlementBatch.created_at.desc(), SettlementBatch.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    items = []
    for batch in batches:
        current = settlement_variance.current_bank_variance_poisha(
            db, merchant_id, batch.id
        )
        items.append(_batch_dict(batch, current_variance=current))
    return items, total


def get_batch_detail(db: Session, *, user, merchant_id, batch_id) -> dict:
    """Batch + receipts + allocations (with net total) + variance
    calculation history (newest first) + current variance + reason.
    Raises SettlementNotFoundError when the batch is not in this
    workspace."""
    batch = _require_batch(db, user, merchant_id, batch_id)
    receipts = _receipts_for_batch(db, batch.merchant_id, batch.id)
    allocs = _allocations_for_batch(db, batch.merchant_id, batch.id)
    history = db.execute(
        select(SettlementVarianceCalculation)
        .where(
            (SettlementVarianceCalculation.merchant_id == batch.merchant_id)
            & (SettlementVarianceCalculation.settlement_batch_id == batch.id)
        )
        .order_by(
            SettlementVarianceCalculation.calculation_seq.desc(),
            SettlementVarianceCalculation.id.desc(),
        )
    ).scalars().all()
    current = settlement_variance.get_current_variance(
        db, batch.merchant_id, batch.id
    )
    detail = _batch_dict(
        batch,
        current_variance=current["bank_variance_poisha"] if current else None,
    )
    detail["receipts"] = [_receipt_dict(r) for r in receipts]
    detail["allocations"] = [_allocation_dict(a) for a in allocs]
    detail["net_allocated_total_poisha"] = _net_allocated(allocs)
    detail["variance_calculations"] = [_calculation_dict(c) for c in history]
    detail["variance_status"] = current["variance_status"] if current else "no_calculation"
    detail["current_calculation_seq"] = current["calculation_seq"] if current else None
    return detail


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def create_receipt(
    db: Session,
    *,
    user,
    merchant_id,
    batch_id,
    amount_poisha,
    received_on=None,
    receipt_ref_label=None,
    evidence_note=None,
) -> dict:
    """Create a receipt under a batch (is_complete=False). The batch's
    bank_status is re-derived (partial_receipt while none complete).
    No bank account number is ever accepted -- the API layer rejects
    that field before this is called."""
    batch = _require_batch(db, user, merchant_id, batch_id)
    receipt = BankReceipt(
        merchant_id=batch.merchant_id,
        settlement_batch_id=batch.id,
        amount_poisha=amount_poisha,
        received_on=received_on,
        receipt_ref_label=receipt_ref_label,
        is_complete=False,
        evidence_note=evidence_note,
    )
    db.add(receipt)
    db.flush()
    _recompute_batch(db, batch)
    db.flush()
    out = _receipt_dict(receipt)
    audit_service.record_audit(
        db,
        actor=user,
        merchant_id=merchant_id,
        action=audit_service.SETTLEMENTS_RECEIPT_CREATED,
        entity_table="bank_receipts",
        entity_id=receipt.id,
        detail={"settlement_batch_id": str(batch.id), "amount_poisha": amount_poisha},
    )
    _commit_or_422(db, reason="receipt_create_failed")
    return out


def complete_receipt(db: Session, *, user, merchant_id, batch_id, receipt_id) -> dict:
    """Mark one receipt complete -- the merchant's explicit action
    only, never inferred from amounts. Idempotent: an already
    complete receipt is returned unchanged (200, no duplicate row).
    marked_complete_at uses the database clock."""
    batch = _require_batch(db, user, merchant_id, batch_id)
    receipt_id = _coerce_uuid(receipt_id)
    receipt = db.get(BankReceipt, receipt_id) if receipt_id is not None else None
    if (
        receipt is None
        or receipt.merchant_id != batch.merchant_id
        or receipt.settlement_batch_id != batch.id
    ):
        raise SettlementNotFoundError("Bank receipt not found.")
    if receipt.is_complete:
        # Idempotent replay: nothing to change, nothing to commit.
        return _receipt_dict(receipt)
    receipt.is_complete = True
    receipt.marked_complete_by_user_id = user.id
    receipt.marked_complete_at = func.now()
    db.flush()
    # Load the DB-clock value while still in this transaction (the
    # tenant context is transaction-local; after commit it is gone).
    _ = receipt.marked_complete_at
    _recompute_batch(db, batch)
    db.flush()
    out = _receipt_dict(receipt)
    audit_service.record_audit(
        db,
        actor=user,
        merchant_id=merchant_id,
        action=audit_service.SETTLEMENTS_RECEIPT_COMPLETED,
        entity_table="bank_receipts",
        entity_id=receipt.id,
        detail={"settlement_batch_id": str(batch.id)},
    )
    _commit_or_422(db, reason="receipt_complete_failed")
    return out


def list_receipts(db: Session, *, user, merchant_id, limit: int = 50, offset: int = 0):
    """(items, total): all receipts for the workspace, newest first,
    each with its settlement_batch_id (nullable) and is_complete."""
    merchant_id = _coerce_uuid(merchant_id)
    _require_membership(db, user, merchant_id)
    total = db.execute(
        select(func.count())
        .select_from(BankReceipt)
        .where(BankReceipt.merchant_id == merchant_id)
    ).scalar_one()
    receipts = db.execute(
        select(BankReceipt)
        .where(BankReceipt.merchant_id == merchant_id)
        .order_by(BankReceipt.created_at.desc(), BankReceipt.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return [_receipt_dict(r) for r in receipts], total


# ---------------------------------------------------------------------------
# Allocations (CL-10)
# ---------------------------------------------------------------------------

def create_allocation(
    db: Session,
    *,
    user,
    merchant_id,
    batch_id,
    bank_receipt_id,
    allocated_poisha,
) -> dict:
    """Allocate part of one receipt to a batch (kind "allocation").
    The receipt must belong to this workspace AND be linked to this
    batch, else 404. Enforced in-transaction: the receipt row is
    locked (SELECT ... FOR UPDATE on PostgreSQL; SQLite has no row
    locks, so the check there is best-effort) and the allocation is
    rejected with 422 reason "over_allocation" when the net allocated
    total from that receipt would exceed its amount. The deferred
    CL-10 trigger is the commit-time backstop and is surfaced as 422
    too."""
    batch = _require_batch(db, user, merchant_id, batch_id)
    receipt_id = _coerce_uuid(bank_receipt_id)
    if receipt_id is None:
        raise SettlementNotFoundError("Bank receipt not found for this batch.")
    stmt = select(BankReceipt).where(
        (BankReceipt.merchant_id == batch.merchant_id)
        & (BankReceipt.id == receipt_id)
        & (BankReceipt.settlement_batch_id == batch.id)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    receipt = db.execute(stmt).scalar_one_or_none()
    if receipt is None:
        raise SettlementNotFoundError("Bank receipt not found for this batch.")
    net_receipt = _net_for_receipt(db, batch.merchant_id, receipt.id)
    if net_receipt + allocated_poisha > receipt.amount_poisha:
        raise SettlementValidationError(
            "Allocation of "
            f"{allocated_poisha} poisha would exceed the receipt amount of "
            f"{receipt.amount_poisha} poisha "
            f"(already allocated from this receipt: {net_receipt} poisha).",
            reason="over_allocation",
        )
    allocation = SettlementAllocation(
        merchant_id=batch.merchant_id,
        settlement_batch_id=batch.id,
        bank_receipt_id=receipt.id,
        allocated_poisha=allocated_poisha,
        kind="allocation",
        reverses_allocation_id=None,
        created_by_user_id=user.id,
    )
    db.add(allocation)
    db.flush()
    _recompute_batch(db, batch)
    db.flush()
    out = _allocation_dict(allocation)
    audit_service.record_audit(
        db,
        actor=user,
        merchant_id=merchant_id,
        action=audit_service.SETTLEMENTS_ALLOCATED,
        entity_table="settlement_allocations",
        entity_id=allocation.id,
        detail={
            "settlement_batch_id": str(batch.id),
            "bank_receipt_id": str(receipt.id),
            "allocated_poisha": allocated_poisha,
        },
    )
    _commit_or_422(db, reason="over_allocation")
    return out


def reverse_allocation(db: Session, *, user, merchant_id, batch_id, allocation_id) -> dict:
    """Reverse one allocation: creates a NEW row (kind "reversal",
    same amount, reverses_allocation_id = the original). Never
    UPDATEs or DELETEs allocation rows. 422 when the original is
    itself a reversal (reason "cannot_reverse_reversal"), when it
    has already been reversed (reason "already_reversed" -- also
    enforced by the partial unique index), or when reversing would
    make the batch net negative (reason "reversal_would_overdraw")."""
    batch = _require_batch(db, user, merchant_id, batch_id)
    allocation_id = _coerce_uuid(allocation_id)
    original = (
        db.get(SettlementAllocation, allocation_id) if allocation_id is not None else None
    )
    if (
        original is None
        or original.merchant_id != batch.merchant_id
        or original.settlement_batch_id != batch.id
    ):
        raise SettlementNotFoundError("Allocation not found.")
    if original.kind == "reversal":
        raise SettlementValidationError(
            "A reversal cannot itself be reversed.",
            reason="cannot_reverse_reversal",
        )
    already = db.execute(
        select(SettlementAllocation)
        .where(SettlementAllocation.reverses_allocation_id == original.id)
        .limit(1)
    ).scalar_one_or_none()
    if already is not None:
        raise SettlementValidationError(
            "This allocation has already been reversed.",
            reason="already_reversed",
        )
    allocs = _allocations_for_batch(db, batch.merchant_id, batch.id)
    if _net_allocated(allocs) - original.allocated_poisha < 0:
        raise SettlementValidationError(
            "Reversing this allocation would make the batch net negative.",
            reason="reversal_would_overdraw",
        )
    reversal = SettlementAllocation(
        merchant_id=batch.merchant_id,
        settlement_batch_id=batch.id,
        bank_receipt_id=original.bank_receipt_id,
        allocated_poisha=original.allocated_poisha,
        kind="reversal",
        reverses_allocation_id=original.id,
        created_by_user_id=user.id,
    )
    db.add(reversal)
    db.flush()
    _recompute_batch(db, batch)
    db.flush()
    out = _allocation_dict(reversal)
    audit_service.record_audit(
        db,
        actor=user,
        merchant_id=merchant_id,
        action=audit_service.SETTLEMENTS_REVERSED,
        entity_table="settlement_allocations",
        entity_id=reversal.id,
        detail={
            "settlement_batch_id": str(batch.id),
            "reverses_allocation_id": str(original.id),
            "allocated_poisha": original.allocated_poisha,
        },
    )
    _commit_or_422(db, reason="reversal_failed")
    return out


# ---------------------------------------------------------------------------
# Variance calculation (CL-16 / CL-21)
# ---------------------------------------------------------------------------

def calculate_variance(db: Session, *, user, merchant_id, batch_id) -> dict:
    """Record a new variance calculation for a fully-allocated batch
    with all receipts complete. Preconditions (else 422, nothing
    written): expected payout NOT NULL (reason
    "expected_payout_null"), allocation_status == "fully_allocated"
    (reason "not_fully_allocated"), every receipt complete (reason
    "receipt_incomplete"). The calculation lists one input row per
    current receipt and one per current allocation row (reversals
    included); the CL-16 deferred trigger validates the snapshots at
    commit and its failure is surfaced as 422 reason
    "variance_validation_failed"."""
    batch = _require_batch(db, user, merchant_id, batch_id)
    if batch.expected_batch_payout_poisha is None:
        raise SettlementValidationError(
            "Batch has no expected payout; bank variance cannot be calculated.",
            reason="expected_payout_null",
        )
    if batch.allocation_status != "fully_allocated":
        raise SettlementValidationError(
            f"Batch is {batch.allocation_status}, not fully_allocated; "
            "bank variance cannot be calculated yet.",
            reason="not_fully_allocated",
        )
    receipts = _receipts_for_batch(db, batch.merchant_id, batch.id)
    if any(not r.is_complete for r in receipts):
        raise SettlementValidationError(
            "Not every receipt linked to the batch is complete; "
            "bank variance cannot be calculated yet.",
            reason="receipt_incomplete",
        )
    allocs = _allocations_for_batch(db, batch.merchant_id, batch.id)
    net = _net_allocated(allocs)
    seq = (
        db.execute(
            select(func.coalesce(func.max(SettlementVarianceCalculation.calculation_seq), 0))
            .where(
                (SettlementVarianceCalculation.merchant_id == batch.merchant_id)
                & (SettlementVarianceCalculation.settlement_batch_id == batch.id)
            )
        ).scalar_one()
        + 1
    )
    calc = SettlementVarianceCalculation(
        merchant_id=batch.merchant_id,
        settlement_batch_id=batch.id,
        calculation_seq=seq,
        expected_batch_payout_poisha=batch.expected_batch_payout_poisha,
        allocated_bank_confirmed_total_poisha=net,
        bank_variance_poisha=net - batch.expected_batch_payout_poisha,
        computed_by_user_id=user.id,
        actor_type="user",
    )
    db.add(calc)
    db.flush()
    for receipt in receipts:
        db.add(
            SettlementVarianceCalculationInput(
                merchant_id=batch.merchant_id,
                settlement_batch_id=batch.id,
                calculation_id=calc.id,
                input_kind="receipt",
                bank_receipt_id=receipt.id,
                settlement_allocation_id=None,
            )
        )
    for allocation in allocs:
        db.add(
            SettlementVarianceCalculationInput(
                merchant_id=batch.merchant_id,
                settlement_batch_id=batch.id,
                calculation_id=calc.id,
                input_kind="allocation",
                bank_receipt_id=None,
                settlement_allocation_id=allocation.id,
            )
        )
    db.flush()
    out = {
        "calculation_seq": seq,
        "expected_batch_payout_poisha": batch.expected_batch_payout_poisha,
        "allocated_bank_confirmed_total_poisha": net,
        "bank_variance_poisha": net - batch.expected_batch_payout_poisha,
    }
    audit_service.record_audit(
        db,
        actor=user,
        merchant_id=merchant_id,
        action=audit_service.SETTLEMENTS_VARIANCE_CALCULATED,
        entity_table="settlement_variance_calculations",
        entity_id=calc.id,
        detail={
            "settlement_batch_id": str(batch.id),
            "calculation_seq": seq,
            "bank_variance_poisha": net - batch.expected_batch_payout_poisha,
        },
    )
    _commit_or_422(db, reason="variance_validation_failed")
    return out


def get_current_variance_view(db: Session, *, user, merchant_id, batch_id) -> dict:
    """{bank_variance_poisha, calculation_seq, variance_status} for
    one batch, from the CL-16/CL-21 derivation. Raises
    SettlementNotFoundError when the batch is not in this
    workspace."""
    batch = _require_batch(db, user, merchant_id, batch_id)
    current = settlement_variance.get_current_variance(
        db, batch.merchant_id, batch.id
    )
    if current is None:  # pragma: no cover - _require_batch already proved it exists
        raise SettlementNotFoundError("Settlement batch not found.")
    return current
