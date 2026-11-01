"""Workspace (merchant) service (M1).

Tenancy rule (v0.2 CL-11): the tenant is derived ONLY from the
authenticated user's server-side membership. A client-supplied
merchant_id is NEVER trusted for scoping — request bodies for these
endpoints do not even accept a merchant_id field (Pydantic
extra="forbid"), and path ids are checked against merchant_memberships
before any data is returned. A non-member asking for another
workspace's id gets 404 (existence is not leaked).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.auth_models import Merchant, MerchantMembership, User
from app.db.session import set_tenant_context, set_user_context
from app.logging_config import get_logger

logger = get_logger(__name__)

ROLES = ("owner", "admin", "member", "viewer")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_membership(db: Session, user_id: uuid.UUID, merchant_id: uuid.UUID) -> MerchantMembership | None:
    return db.execute(
        select(MerchantMembership).where(
            MerchantMembership.user_id == user_id,
            MerchantMembership.merchant_id == merchant_id,
            MerchantMembership.status == "active",
        )
    ).scalar_one_or_none()


def create_workspace(
    db: Session, user: User, name: str, trading_name: str | None
) -> Merchant:
    """Create a merchant workspace and the creator's owner membership
    in one transaction. The creator is the owner, status active.

    PostgreSQL (the real path, v0.1 E.3 / B-1 fix): creation runs ONLY
    through the controlled SECURITY DEFINER function
    public.create_workspace() (see app/db/rls.sql). There is no general
    INSERT policy on merchants, so a direct ORM insert would be denied
    by RLS; the function inserts the merchant and the owner membership
    atomically and returns the new merchant id. The user context for
    this request is already set (api/deps.py), so the membership-based
    SELECT policy lets the just-created row be read back below.
    SQLite (test-only fallback, no RLS): the function does not exist,
    so the same two rows are inserted directly. This branch exists
    ONLY so the pytest suite can exercise the service without a
    PostgreSQL server; it is not a second creation path in production.
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        merchant_id = db.execute(
            text("SELECT public.create_workspace(:user_id, :name, :trading_name)"),
            {"user_id": user.id, "name": name, "trading_name": trading_name},
        ).scalar_one()
        merchant = db.get(Merchant, merchant_id)
        if merchant is None:  # pragma: no cover - function + policy disagree
            raise RuntimeError("Workspace creation succeeded but the row is not readable.")
        db.commit()
        # Commit ends the transaction and clears the transaction-local
        # app.user_id setting; re-assert it before refresh, or the
        # membership-based SELECT policy hides the row from the runtime
        # role and refresh raises "Could not refresh instance".
        set_user_context(db, user.id)
        db.refresh(merchant)
    else:
        merchant = Merchant(name=name, trading_name=trading_name, created_by_user_id=user.id)
        db.add(merchant)
        db.flush()  # assigns merchant.id so the membership can reference it
        db.add(
            MerchantMembership(
                merchant_id=merchant.id,
                user_id=user.id,
                role="owner",
                status="active",
                invited_by_user_id=user.id,
            )
        )
        db.commit()
        db.refresh(merchant)
    logger.info(
        "workspace_created",
        extra={"merchant_id": str(merchant.id), "user_id": str(user.id)},
    )
    return merchant


def list_workspaces_for_user(db: Session, user: User) -> list[dict]:
    """All workspaces where the user holds an active membership,
    with their role. No client-supplied filter is involved."""
    rows = db.execute(
        select(Merchant, MerchantMembership.role)
        .join(MerchantMembership, MerchantMembership.merchant_id == Merchant.id)
        .where(
            MerchantMembership.user_id == user.id,
            MerchantMembership.status == "active",
        )
        .order_by(Merchant.created_at)
    ).all()
    return [
        {
            "id": str(m.id),
            "name": m.name,
            "trading_name": m.trading_name,
            "status": m.status,
            "role": role,
            "created_at": m.created_at.isoformat(),
        }
        for m, role in rows
    ]


def get_workspace_for_user(
    db: Session, user: User, merchant_id: uuid.UUID
) -> dict | None:
    """Return the workspace only if the user is an active member,
    else None (caller maps to 404). The RLS tenant context is set
    from this server-side verified id, never from the raw path value
    before the membership check."""
    membership = get_membership(db, user.id, merchant_id)
    if membership is None:
        return None
    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        return None
    # Membership verified: now it is safe to assert tenant context.
    set_tenant_context(db, merchant.id)
    return {
        "id": str(merchant.id),
        "name": merchant.name,
        "trading_name": merchant.trading_name,
        "status": merchant.status,
        "role": membership.role,
        "created_at": merchant.created_at.isoformat(),
    }
