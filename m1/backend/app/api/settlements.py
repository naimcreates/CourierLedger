"""Settlement batch / bank receipt / allocation / variance API
(M1, Milestone 7 part 2).

POST /api/workspaces/{id}/settlement-batches -- create a batch (201).
GET  /api/workspaces/{id}/settlement-batches -- list, newest first,
  each with current_bank_variance_poisha (derived, nullable).
GET  /api/workspaces/{id}/settlement-batches/{batch_id} -- batch +
  receipts + allocations (with net total) + variance calculation
  history (newest first) + current variance + variance_status.
POST /api/workspaces/{id}/settlement-batches/{batch_id}/receipts
  -- create a receipt (201, is_complete=false).
POST /api/workspaces/{id}/settlement-batches/{batch_id}/receipts/{receipt_id}/complete
  -- mark a receipt complete (200; idempotent replay).
GET  /api/workspaces/{id}/bank-receipts -- all receipts, newest first.
POST /api/workspaces/{id}/settlement-batches/{batch_id}/allocations
  -- allocate from one receipt (201; 422 on over-allocation).
POST /api/workspaces/{id}/settlement-batches/{batch_id}/allocations/{allocation_id}/reverse
  -- reverse an allocation with a new reversal row (201).
POST /api/workspaces/{id}/settlement-batches/{batch_id}/calculate-variance
  -- record a variance calculation (201 shape; 422 when the
  preconditions do not hold).
GET  /api/workspaces/{id}/settlement-batches/{batch_id}/current-variance
  -- derived current variance + reason.

Tenancy: the workspace must belong to the caller (membership check);
a non-member gets 404, never a leak. All POSTs require the CSRF
double-submit check. No bank account number is accepted anywhere:
a `bank_account_number` field in a receipt body is rejected with
422, and no such value is ever stored. Receipts are never linked to
a batch by amount -- only by explicit creation under that batch.
"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf
from app.db.auth_models import User
from app.db.session import get_db
from app.services import settlement_service
from app.services import workspace_service

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["settlements"])


class CreateBatchRequest(BaseModel):
    merchant_courier_account_id: uuid.UUID
    run_id: uuid.UUID | None = None
    period_start: date | None = None
    period_end: date | None = None
    expected_batch_payout_poisha: int | None = Field(default=None, ge=0)


class CreateReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_poisha: int = Field(gt=0)
    received_on: date | None = None
    receipt_ref_label: str | None = None
    evidence_note: str | None = None
    # Declared only so its presence can be rejected with a clear
    # message: bank account numbers are never accepted or stored.
    bank_account_number: str | None = None


class CreateAllocationRequest(BaseModel):
    bank_receipt_id: uuid.UUID
    allocated_poisha: int = Field(gt=0)


def _require_membership(db: Session, user: User, workspace_id: uuid.UUID):
    membership = workspace_service.get_membership(db, user.id, workspace_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


def _not_found(exc: settlement_service.SettlementNotFoundError):
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _unprocessable(exc: settlement_service.SettlementValidationError):
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"message": str(exc), "reason": exc.reason},
    )


@router.post(
    "/settlement-batches",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
def create_batch(
    workspace_id: uuid.UUID,
    body: CreateBatchRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        return settlement_service.create_batch(
            db,
            user=user,
            merchant_id=workspace_id,
            merchant_courier_account_id=body.merchant_courier_account_id,
            run_id=body.run_id,
            period_start=body.period_start,
            period_end=body.period_end,
            expected_batch_payout_poisha=body.expected_batch_payout_poisha,
        )
    except settlement_service.SettlementNotFoundError as exc:
        db.rollback()
        raise _not_found(exc) from exc
    except settlement_service.SettlementValidationError as exc:
        db.rollback()
        raise _unprocessable(exc) from exc


@router.get("/settlement-batches")
def list_batches(
    workspace_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        items, total = settlement_service.list_batches(
            db, user=user, merchant_id=workspace_id, limit=limit, offset=offset
        )
    except settlement_service.SettlementNotFoundError as exc:
        raise _not_found(exc) from exc
    return {
        "settlement_batches": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/settlement-batches/{batch_id}")
def get_batch(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        return settlement_service.get_batch_detail(
            db, user=user, merchant_id=workspace_id, batch_id=batch_id
        )
    except settlement_service.SettlementNotFoundError as exc:
        raise _not_found(exc) from exc


@router.post(
    "/settlement-batches/{batch_id}/receipts",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
def create_receipt(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    body: CreateReceiptRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    if body.bank_account_number is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Bank account numbers are never accepted or stored.",
                "reason": "bank_account_number_rejected",
            },
        )
    try:
        return settlement_service.create_receipt(
            db,
            user=user,
            merchant_id=workspace_id,
            batch_id=batch_id,
            amount_poisha=body.amount_poisha,
            received_on=body.received_on,
            receipt_ref_label=body.receipt_ref_label,
            evidence_note=body.evidence_note,
        )
    except settlement_service.SettlementNotFoundError as exc:
        db.rollback()
        raise _not_found(exc) from exc
    except settlement_service.SettlementValidationError as exc:
        db.rollback()
        raise _unprocessable(exc) from exc


@router.post(
    "/settlement-batches/{batch_id}/receipts/{receipt_id}/complete",
    dependencies=[Depends(require_csrf)],
)
def complete_receipt(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    receipt_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        return settlement_service.complete_receipt(
            db,
            user=user,
            merchant_id=workspace_id,
            batch_id=batch_id,
            receipt_id=receipt_id,
        )
    except settlement_service.SettlementNotFoundError as exc:
        db.rollback()
        raise _not_found(exc) from exc
    except settlement_service.SettlementValidationError as exc:
        db.rollback()
        raise _unprocessable(exc) from exc


@router.get("/bank-receipts")
def list_receipts(
    workspace_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        items, total = settlement_service.list_receipts(
            db, user=user, merchant_id=workspace_id, limit=limit, offset=offset
        )
    except settlement_service.SettlementNotFoundError as exc:
        raise _not_found(exc) from exc
    return {"receipts": items, "total": total, "limit": limit, "offset": offset}


@router.post(
    "/settlement-batches/{batch_id}/allocations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
def create_allocation(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    body: CreateAllocationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        return settlement_service.create_allocation(
            db,
            user=user,
            merchant_id=workspace_id,
            batch_id=batch_id,
            bank_receipt_id=body.bank_receipt_id,
            allocated_poisha=body.allocated_poisha,
        )
    except settlement_service.SettlementNotFoundError as exc:
        db.rollback()
        raise _not_found(exc) from exc
    except settlement_service.SettlementValidationError as exc:
        db.rollback()
        raise _unprocessable(exc) from exc


@router.post(
    "/settlement-batches/{batch_id}/allocations/{allocation_id}/reverse",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
def reverse_allocation(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    allocation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        return settlement_service.reverse_allocation(
            db,
            user=user,
            merchant_id=workspace_id,
            batch_id=batch_id,
            allocation_id=allocation_id,
        )
    except settlement_service.SettlementNotFoundError as exc:
        db.rollback()
        raise _not_found(exc) from exc
    except settlement_service.SettlementValidationError as exc:
        db.rollback()
        raise _unprocessable(exc) from exc


@router.post(
    "/settlement-batches/{batch_id}/calculate-variance",
    dependencies=[Depends(require_csrf)],
)
def calculate_variance(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        result = settlement_service.calculate_variance(
            db, user=user, merchant_id=workspace_id, batch_id=batch_id
        )
    except settlement_service.SettlementNotFoundError as exc:
        db.rollback()
        raise _not_found(exc) from exc
    except settlement_service.SettlementValidationError as exc:
        db.rollback()
        raise _unprocessable(exc) from exc
    # 201: a new calculation row was recorded.
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=result)


@router.get("/settlement-batches/{batch_id}/current-variance")
def current_variance(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        return settlement_service.get_current_variance_view(
            db, user=user, merchant_id=workspace_id, batch_id=batch_id
        )
    except settlement_service.SettlementNotFoundError as exc:
        raise _not_found(exc) from exc
