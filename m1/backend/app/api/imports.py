"""CSV import API (M1, Milestone 4).

POST /api/workspaces/{id}/imports -- multipart upload:
  file: the CSV bytes (required)
  mapping: JSON object, canonical field -> source header (required)
  merchant_courier_account_id: uuid form field (required)
  idempotency_key: optional form field; the Idempotency-Key header
    is accepted instead. Same key + same bytes + same mapping +
    same account replays the stored batch (no duplicate import).
    No key is supplied: a fresh random key is used (not idempotent).

GET  /api/workspaces/{id}/imports -- list batches (newest first).
GET  /api/workspaces/{id}/imports/{batch_id} -- batch + rows (capped
  at 500, truncated flag) + statement lines + import errors.

Tenancy: the workspace must belong to the caller (membership check);
a non-member gets 404, never a leak. Upload is state-changing, so
it requires the CSRF double-submit check.
"""

import json
import uuid

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf, require_write_role
from app.db.auth_models import User
from app.db.models import ImportBatch
from app.db.session import get_db, set_tenant_context
from app.services import csv_import
from app.services import workspace_service

router = APIRouter(prefix="/workspaces/{workspace_id}/imports", tags=["imports"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB local-dev cap


def _require_membership(db: Session, user: User, workspace_id: uuid.UUID):
    membership = workspace_service.get_membership(db, user.id, workspace_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


@router.post("", dependencies=[Depends(require_csrf)])
async def upload_import(
    workspace_id: uuid.UUID,
    file: UploadFile = File(...),
    mapping: str = Form(...),
    merchant_courier_account_id: uuid.UUID = Form(...),
    idempotency_key: str | None = Form(default=None, max_length=128),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_write_role(_require_membership(db, user, workspace_id))
    try:
        mapping_dict = json.loads(mapping)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mapping must be a JSON object of canonical field -> source header.",
        ) from None
    # Phase 1 (C-2.2): stream-read in 1 MB chunks and abort as soon as
    # the 10 MB cap is crossed, instead of reading the whole file into
    # memory and only then rejecting it.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File is larger than the 10 MB local-dev limit.",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Uploaded file is empty."
        )
    try:
        summary = csv_import.run_import(
            db,
            user=user,
            merchant_id=workspace_id,
            merchant_courier_account_id=merchant_courier_account_id,
            filename=file.filename or "statement.csv",
            data=data,
            mapping=mapping_dict,
            idempotency_key=idempotency_header or idempotency_key,
            content_type=file.content_type,
        )
    except csv_import.ImportAccessError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    except csv_import.MappingError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except csv_import.ImportFailedError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    # Phase 1 (C-2.4a): a batch that failed to parse is a 422, not a
    # 201 — a client that checks only the status code must not think
    # the upload succeeded. The parse_failed batch itself is still
    # persisted (with its VALUE_UNPARSABLE error row), as before.
    if summary.get("status") == "parse_failed":
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=summary
        )
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=summary)


@router.get("")
def list_imports(
    workspace_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Paginated (Phase 1, C-1.9): same {imports, total, limit, offset}
    envelope as the other list endpoints. The frontend unwraps it
    (and still accepts the old bare-array shape)."""
    _require_membership(db, user, workspace_id)
    set_tenant_context(db, workspace_id)
    total = db.execute(
        select(func.count())
        .select_from(ImportBatch)
        .where(ImportBatch.merchant_id == workspace_id)
    ).scalar_one()
    batches = db.execute(
        select(ImportBatch)
        .where(ImportBatch.merchant_id == workspace_id)
        .order_by(ImportBatch.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return {
        "imports": [
            {
                "batch_id": str(b.id),
                "status": b.status,
                "row_count": b.row_count,
                "parsed_row_count": b.parsed_row_count,
                "error_row_count": b.error_row_count,
                "file_object_id": str(b.file_object_id),
                "mapping_snapshot": b.mapping_snapshot,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in batches
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{batch_id}")
def get_import(
    workspace_id: uuid.UUID,
    batch_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    detail = csv_import.get_batch_detail(db, workspace_id, batch_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found.")
    return detail
