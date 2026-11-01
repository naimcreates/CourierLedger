"""Reports API (M1, Milestone 8 part 2).

GET  /api/workspaces/{id}/reports/summary -- read-only aggregates
  computed from real rows only. Where there is no data, counts are
  zero and totals are 0 -- nothing is invented and no demo data is
  included in this endpoint.

The five reconciliation variances (C, V_R, S, V_cond, V_total) are
reported as SEPARATE totals over stored results: each total sums
only that variance's non-NULL values, with the NULL count reported
beside it. The five totals are never summed together -- they measure
different things and a combined total would be meaningless.

Tenancy: the workspace must belong to the caller (membership check);
a non-member gets 404, never a leak. The tenant context is set
before any tenant-keyed lookup (B-1 pattern).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.auth_models import User
from app.db.models import (
    ALLOCATION_STATUSES,
    BANK_STATUSES,
    BATCH_STATUSES,
    OVERALL_STATUSES,
    RUN_STATUSES,
    BankReceipt,
    ImportBatch,
    ReconciliationResult,
    ReconciliationRun,
    SettlementBatch,
)
from app.db.session import get_db, set_tenant_context
from app.services import workspace_service

# Mounted by the coordinator in app/main.py with prefix "/api", so the
# full path is /api/workspaces/{workspace_id}/reports/summary.
router = APIRouter(prefix="/workspaces/{workspace_id}/reports", tags=["reports"])

# (response key, ReconciliationResult column) -- kept separate, never
# summed together.
_VARIANCE_COLUMNS = (
    ("collection_c", ReconciliationResult.c_poisha),
    ("fee_v_r", ReconciliationResult.v_r_poisha),
    ("statement_s", ReconciliationResult.s_poisha),
    ("conditional_payout_v_cond", ReconciliationResult.v_cond_poisha),
    ("scenario_v_total", ReconciliationResult.v_total_poisha),
)


def _require_membership(db: Session, user: User, workspace_id: uuid.UUID):
    membership = workspace_service.get_membership(db, user.id, workspace_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


def _grouped_counts(db: Session, model, column, workspace_id, all_keys):
    """{key: count} over one column for this workspace, with every
    known key present (0 where there are no rows)."""
    rows = db.execute(
        select(column, func.count())
        .where(model.merchant_id == workspace_id)
        .group_by(column)
    ).all()
    counts = {key: 0 for key in all_keys}
    for key, count in rows:
        if key in counts:
            counts[key] = count
    return counts


@router.get("/summary")
def reports_summary(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    set_tenant_context(db, workspace_id)

    runs_by_status = _grouped_counts(
        db, ReconciliationRun, ReconciliationRun.status, workspace_id, RUN_STATUSES
    )
    results_by_status = _grouped_counts(
        db,
        ReconciliationResult,
        ReconciliationResult.overall_status,
        workspace_id,
        OVERALL_STATUSES,
    )
    batches_by_allocation = _grouped_counts(
        db,
        SettlementBatch,
        SettlementBatch.allocation_status,
        workspace_id,
        ALLOCATION_STATUSES,
    )
    batches_by_bank = _grouped_counts(
        db, SettlementBatch, SettlementBatch.bank_status, workspace_id, BANK_STATUSES
    )
    imports_by_status = _grouped_counts(
        db, ImportBatch, ImportBatch.status, workspace_id, BATCH_STATUSES
    )

    # Bank receipts have no status column: complete vs incomplete is
    # the is_complete flag.
    receipt_rows = db.execute(
        select(BankReceipt.is_complete, func.count())
        .where(BankReceipt.merchant_id == workspace_id)
        .group_by(BankReceipt.is_complete)
    ).all()
    receipts_complete = 0
    receipts_incomplete = 0
    for is_complete, count in receipt_rows:
        if is_complete:
            receipts_complete = count
        else:
            receipts_incomplete = count

    # One aggregate query for all five variances: each total sums
    # only that column's non-NULL values (func.count(col) counts
    # non-NULL rows), each NULL count is total rows minus that.
    # The five totals are reported separately, never added together.
    total_results = sum(results_by_status.values())
    variances = {}
    if total_results:
        aggregates = [
            func.coalesce(func.sum(column), 0) for _, column in _VARIANCE_COLUMNS
        ] + [func.count(column) for _, column in _VARIANCE_COLUMNS]
        row = db.execute(
            select(*aggregates).where(
                ReconciliationResult.merchant_id == workspace_id
            )
        ).one()
        n = len(_VARIANCE_COLUMNS)
        for i, (key, _column) in enumerate(_VARIANCE_COLUMNS):
            non_null = row[n + i]
            variances[key] = {
                "total_poisha": int(row[i]),
                "non_null_count": non_null,
                "null_count": total_results - non_null,
            }
    else:
        for key, _column in _VARIANCE_COLUMNS:
            variances[key] = {
                "total_poisha": 0,
                "non_null_count": 0,
                "null_count": 0,
            }

    return {
        "workspace_id": str(workspace_id),
        "reconciliation_runs": {
            "total": sum(runs_by_status.values()),
            "by_status": runs_by_status,
        },
        "reconciliation_results": {
            "total": total_results,
            "by_overall_status": results_by_status,
            "variances_poisha": variances,
        },
        "settlement_batches": {
            "total": sum(batches_by_allocation.values()),
            "by_allocation_status": batches_by_allocation,
            "by_bank_status": batches_by_bank,
        },
        "bank_receipts": {
            "total": receipts_complete + receipts_incomplete,
            "complete": receipts_complete,
            "incomplete": receipts_incomplete,
        },
        "import_batches": {
            "total": sum(imports_by_status.values()),
            "by_status": imports_by_status,
        },
    }
