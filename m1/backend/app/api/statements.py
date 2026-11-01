"""Courier-statement line API (M1, Milestone 8 part 2).

GET  /api/workspaces/{id}/statements -- paginated list of statement
  lines for the workspace, newest first. Query params: import_batch_id,
  match_status, page (1-based, default 1), page_size (default 20,
  max 100).
GET  /api/workspaces/{id}/statements/{line_id} -- one line in full:
  raw CSV values verbatim (from its import row), parse/match status,
  the linked shipment where matched, the import batch reference and
  the courier account.

Read-only: there are deliberately no POST / PUT / PATCH / DELETE
routes. The rows shown here are exactly the statement_lines written
by the M4 import service -- no courier statement format or fee
semantics is invented here; what is stored is what is shown.

Tenancy: the workspace must belong to the caller (membership check);
a non-member gets 404, never a leak. The tenant context is set
before any tenant-keyed lookup (B-1 pattern).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.auth_models import User
from app.db.models import (
    MATCH_STATUSES,
    Courier,
    ImportBatch,
    ImportRow,
    MerchantCourierAccount,
    Shipment,
    StatementLine,
)
from app.db.session import get_db, set_tenant_context
from app.services import workspace_service

# Mounted by the coordinator in app/main.py with prefix "/api", so the
# full paths are /api/workspaces/{workspace_id}/statements[...].
router = APIRouter(prefix="/workspaces/{workspace_id}/statements", tags=["statements"])


def _require_membership(db: Session, user: User, workspace_id: uuid.UUID):
    membership = workspace_service.get_membership(db, user.id, workspace_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


def _iso(value):
    return value.isoformat() if value is not None else None


def _shipment_dict(shipment: Shipment | None) -> dict | None:
    if shipment is None:
        return None
    return {
        "shipment_id": str(shipment.id),
        "tracking_id_raw": shipment.tracking_id_raw,
        "tracking_id_normalised": shipment.tracking_id_normalised,
        "outcome": shipment.outcome,
        "expected_collectible_cod_poisha": shipment.expected_collectible_cod_poisha,
        "is_cod_bearing": shipment.is_cod_bearing,
    }


def _account_dict(
    account: MerchantCourierAccount | None, courier_code: str | None
) -> dict | None:
    if account is None:
        return None
    return {
        "merchant_courier_account_id": str(account.id),
        "courier_code": courier_code,
        "display_label": account.display_label,
        "merchant_account_ref": account.merchant_account_ref,
        "status": account.status,
    }


def _batch_dict(batch: ImportBatch | None) -> dict | None:
    if batch is None:
        return None
    return {
        "batch_id": str(batch.id),
        "status": batch.status,
        "statement_period_start": _iso(batch.statement_period_start),
        "statement_period_end": _iso(batch.statement_period_end),
        "row_count": batch.row_count,
        "parsed_row_count": batch.parsed_row_count,
        "error_row_count": batch.error_row_count,
        "created_at": _iso(batch.created_at),
    }


def _line_dict(
    line: StatementLine,
    row: ImportRow | None,
    shipment: Shipment | None,
    batch: ImportBatch | None,
    account: MerchantCourierAccount | None,
    courier_code: str | None,
) -> dict:
    return {
        "statement_line_id": str(line.id),
        "import_batch_id": str(line.import_batch_id),
        "import_row_id": str(line.import_row_id),
        "row_number": row.row_number if row is not None else None,
        "parse_status": row.parse_status if row is not None else None,
        # Raw CSV values verbatim, exactly as stored on the import row.
        "raw_values": row.raw_values if row is not None else None,
        "match_status": line.match_status,
        "tracking_id_raw": line.tracking_id_raw,
        "tracking_id_normalised": line.tracking_id_normalised,
        "merchant_order_ref_raw": line.merchant_order_ref_raw,
        "reported_status_raw": line.reported_status_raw,
        "statement_date": _iso(line.statement_date),
        "reported_collected_poisha": line.reported_collected_poisha,
        "reported_collected_state": line.reported_collected_state,
        "reported_payout_poisha": line.reported_payout_poisha,
        "reported_payout_state": line.reported_payout_state,
        "reported_delivery_fee_poisha": line.reported_delivery_fee_poisha,
        "reported_delivery_fee_state": line.reported_delivery_fee_state,
        "reported_cod_fee_poisha": line.reported_cod_fee_poisha,
        "reported_cod_fee_state": line.reported_cod_fee_state,
        "reported_return_fee_poisha": line.reported_return_fee_poisha,
        "reported_return_fee_state": line.reported_return_fee_state,
        "reported_other_fee_poisha": line.reported_other_fee_poisha,
        "reported_other_fee_state": line.reported_other_fee_state,
        "shipment_id": str(line.shipment_id) if line.shipment_id else None,
        "shipment": _shipment_dict(shipment),
        "courier_account": _account_dict(account, courier_code),
        "import_batch": _batch_dict(batch),
        "created_at": _iso(line.created_at),
    }


def _load_related(db: Session, lines):
    """Batch-load the rows / shipments / batches / accounts / courier
    codes for a page of lines, so the list endpoint does not issue
    one query per line."""
    row_ids = [line.import_row_id for line in lines]
    rows = {
        r.id: r
        for r in db.execute(
            select(ImportRow).where(ImportRow.id.in_(row_ids))
        ).scalars().all()
    } if row_ids else {}
    shipment_ids = [line.shipment_id for line in lines if line.shipment_id]
    shipments = {
        s.id: s
        for s in db.execute(
            select(Shipment).where(Shipment.id.in_(shipment_ids))
        ).scalars().all()
    } if shipment_ids else {}
    batch_ids = {line.import_batch_id for line in lines}
    batches = {
        b.id: b
        for b in db.execute(
            select(ImportBatch).where(ImportBatch.id.in_(batch_ids))
        ).scalars().all()
    } if batch_ids else {}
    account_ids = {
        b.merchant_courier_account_id for b in batches.values()
    }
    accounts = {
        a.id: a
        for a in db.execute(
            select(MerchantCourierAccount).where(
                MerchantCourierAccount.id.in_(account_ids)
            )
        ).scalars().all()
    } if account_ids else {}
    courier_ids = {a.courier_id for a in accounts.values()}
    couriers = {
        c.id: c
        for c in db.execute(
            select(Courier).where(Courier.id.in_(courier_ids))
        ).scalars().all()
    } if courier_ids else {}
    return rows, shipments, batches, accounts, couriers


def _serialize_page(db: Session, lines):
    rows, shipments, batches, accounts, couriers = _load_related(db, lines)
    out = []
    for line in lines:
        batch = batches.get(line.import_batch_id)
        account = (
            accounts.get(batch.merchant_courier_account_id)
            if batch is not None
            else None
        )
        courier = couriers.get(account.courier_id) if account is not None else None
        out.append(
            _line_dict(
                line,
                rows.get(line.import_row_id),
                shipments.get(line.shipment_id) if line.shipment_id else None,
                batch,
                account,
                courier.code if courier is not None else None,
            )
        )
    return out


@router.get("")
def list_statements(
    workspace_id: uuid.UUID,
    import_batch_id: uuid.UUID | None = Query(default=None),
    match_status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    set_tenant_context(db, workspace_id)
    if match_status is not None and match_status not in MATCH_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"match_status must be one of: {', '.join(MATCH_STATUSES)}.",
        )
    if import_batch_id is not None:
        batch = db.get(ImportBatch, import_batch_id)
        if batch is None or batch.merchant_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Import batch not found."
            )
    stmt = select(StatementLine).where(StatementLine.merchant_id == workspace_id)
    if import_batch_id is not None:
        stmt = stmt.where(StatementLine.import_batch_id == import_batch_id)
    if match_status is not None:
        stmt = stmt.where(StatementLine.match_status == match_status)
    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    lines = db.execute(
        stmt.order_by(StatementLine.created_at.desc(), StatementLine.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).scalars().all()
    return {
        "statements": _serialize_page(db, lines),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{line_id}")
def get_statement(
    workspace_id: uuid.UUID,
    line_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    set_tenant_context(db, workspace_id)
    line = db.get(StatementLine, line_id)
    if line is None or line.merchant_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Statement line not found."
        )
    return _serialize_page(db, [line])[0]
