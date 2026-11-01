"""Reconciliation run API (M1, Milestone 6).

POST /api/workspaces/{id}/imports/{batch_id}/runs -- create a run
  from one import batch (201 created; 200 on idempotency replay of
  the same key + same request; 422 when the batch cannot produce a
  run; 409 on idempotency conflict; 404 when the batch is not in
  this workspace).
GET  /api/workspaces/{id}/runs -- list runs (newest first).
GET  /api/workspaces/{id}/runs/{run_id} -- full run detail
  (results, each with fee lines + findings).
GET  /api/workspaces/{id}/runs/{run_id}/results -- paginated results.
GET  /api/workspaces/{id}/runs/{run_id}/findings -- paginated findings.
GET  /api/workspaces/{id}/imports/{batch_id}/runs -- runs for one
  batch (newest first).

Runs are append-only: there are deliberately no PUT / PATCH / DELETE
routes for runs, results, fee lines or findings. A run is created
only from a real import batch -- POST /api/reconcile stays
compute-only and no synthetic import batch may be fabricated (see
app/services/run_service.py).

Tenancy: the workspace must belong to the caller (membership check);
a non-member gets 404, never a leak. Run creation is state-changing,
so it requires the CSRF double-submit check.
"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf
from app.db.auth_models import User
from app.db.session import get_db
from app.services import run_service
from app.services.idempotency_service import IdempotencyConflictError
from app.services import workspace_service

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["runs"])


class CreateRunRequest(BaseModel):
    """POST .../imports/{batch_id}/runs body.

    Exactly one of courier_fixture / rule_version_id (both omitted
    defaults to the "Fixture Alpha" synthetic fixture). tolerance is
    optional: when omitted the workspace setting is used (0 when no
    settings row exists)."""

    courier_fixture: str | None = None
    rule_version_id: uuid.UUID | None = None
    tolerance_poisha: int | None = None
    idempotency_key: str | None = None


def _require_membership(db: Session, user: User, workspace_id: uuid.UUID):
    membership = workspace_service.get_membership(db, user.id, workspace_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


@router.post(
    "/imports/{batch_id}/runs",
    dependencies=[Depends(require_csrf)],
)
def create_run(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    body: CreateRunRequest,
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        summary = run_service.create_run_from_import(
            db,
            user=user,
            merchant_id=workspace_id,
            import_batch_id=batch_id,
            courier_fixture=body.courier_fixture,
            rule_version_id=body.rule_version_id,
            tolerance_poisha=body.tolerance_poisha,
            idempotency_key=idempotency_header or body.idempotency_key,
        )
    except run_service.RunNotFoundError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import batch not found.")
    except run_service.RunCreationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "reason": exc.reason, "counts": exc.counts},
        )
    except IdempotencyConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    # 201 for a freshly created run, 200 when the idempotency key (or
    # the CL-05 run hash) replayed an existing run.
    return JSONResponse(
        status_code=(
            status.HTTP_201_CREATED
            if summary["idempotency_outcome"] == "claimed"
            else status.HTTP_200_OK
        ),
        content=summary,
    )


@router.get("/runs")
def list_runs(
    workspace_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    items, total = run_service.list_runs(db, workspace_id, limit=limit, offset=offset)
    return {"runs": items, "total": total, "limit": limit, "offset": offset}


@router.get("/runs/{run_id}")
def get_run(
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    detail = run_service.get_run_detail(db, workspace_id, run_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return detail


@router.get("/runs/{run_id}/results")
def list_results(
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        items, total = run_service.list_run_results(db, workspace_id, run_id, limit=limit, offset=offset)
    except run_service.RunNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return {"results": items, "total": total, "limit": limit, "offset": offset}


@router.get("/runs/{run_id}/findings")
def list_findings(
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        items, total = run_service.list_run_findings(db, workspace_id, run_id, limit=limit, offset=offset)
    except run_service.RunNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return {"findings": items, "total": total, "limit": limit, "offset": offset}


@router.get("/imports/{batch_id}/runs")
def list_batch_runs(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        runs = run_service.list_runs_for_batch(db, workspace_id, batch_id)
    except run_service.RunNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import batch not found.")
    return {"runs": runs, "total": len(runs)}
