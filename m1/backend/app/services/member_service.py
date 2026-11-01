"""Member invitation / acceptance / role / revocation service (M1,
Milestone 9, part 1).

Endpoints live in app/api/members.py; this module owns the rules.

Tenancy (v0.2 CL-11): the workspace id comes from the URL path and is
checked against merchant_memberships for the authenticated user before
any data is returned. A non-member gets "not found" (404), never a
leak. Request bodies do not accept a merchant_id (Pydantic
extra="forbid" in the API layer).

No new tables or columns. Invitations are MerchantMembership rows
with status='invited'; the model requires user_id (FK to users.id,
NOT NULL), so there is NO email-only invitation row. Inviting an
email address that belongs to no registered user is rejected with
422 / reason "unknown_email" (see invite_member). No user row is
ever invented for an invitee.

Roles: 'owner' | 'admin' | 'member' | 'viewer'. Only owner/admin may
invite, change roles or revoke. The role endpoint never creates an
owner membership and never changes a role to (or from) owner --
owner transfer is a separate endpoint (transfer_owner, M10 step 2)
because it must move the owner role, not just set it, and must
preserve the last-owner invariant. The only owner-affecting guard
in the role/revoke paths is the last-active-owner guard on revoke
(OD-11); transfer_owner carries its own in-transaction assertion
that at least one active owner remains (see transfer_owner).

Revocation and sessions: revoking a membership sets status='revoked'.
The workspace-authorization layer (workspace_service.get_membership)
only ever returns ACTIVE memberships, so a revoked member's existing
session is denied on every workspace endpoint (404) on the very next
request -- no session row has to be touched for the denial to take
effect. Global session revocation (auth_service.revoke_all_sessions)
is deliberately NOT done here: revocation is per-workspace, and the
revoked user may still hold active memberships in other workspaces;
deleting all of their sessions would log them out of workspaces
where they are still a member. A future admin action that needs a
global logout is a separate endpoint.

CL-20 advisory-lock protocol (v0.4 amendment, "CL-20 - CL-15
shared-user anonymisation serialisation")
---------------------------------------------------------------
Every membership mutation (invite, accept, role change, revoke)
acquires a per-user transaction-scoped advisory lock
(pg_advisory_xact_lock) for each affected user BEFORE the
mutation decision is made, and holds it to commit. The lock exists
independently of whether a membership row exists yet, which is the
whole point: it serialises a membership creation against a
concurrent Stage 2 anonymisation (or any other conforming
membership mutation) for the same user, where a row lock could not,
because there may be no row to lock.

Lock-key derivation (one canonical form, implemented in
cl20_lock_key): the key is the first 8 bytes of
SHA-256("cl20:" || <user UUID raw 16 bytes>), interpreted as a
signed big-endian 64-bit integer, passed as the single bigint
argument to pg_advisory_xact_lock. The "cl20:" namespace prefix is
part of the hashed input, so these keys cannot collide with any
other advisory-lock user (e.g. the CL-13 candidate locks, which use
a different derivation) even if the underlying UUIDs coincided.
The SQL-side equivalent, for a DBA reproducing the key, is
  ('x' || substr(encode(sha256('cl20:'::bytea ||
      decode(replace(:uuid::text, '-', ''), 'hex')), 'hex'), 1, 16)
      )::bit(64)::bigint
i.e. SHA-256 over the same "cl20:" prefix plus the same raw UUID
bytes, first 8 bytes, signed big-endian. The application only ever
takes the lock through cl20_lock_key / _acquire_user_locks below;
there is no second derivation to drift.

Lock ordering: where a mutation involves more than one user, the
locks are acquired in ASCENDING LOCK-KEY order (CL-20's global
order), and no other advisory lock is acquired afterwards. This is
a total order over users, so two conforming transactions can never
deadlock on these locks by taking them in opposite orders.

Lock set per operation:
  * invite:  the inviter (actor) AND the invitee, ascending key
    order. Both users are involved: the actor's authorization is
    rechecked under the lock and the invitee gains a membership.
  * accept:  the invitee (who is also the actor) only.
  * role:    the target member only.
  * revoke:  the target member; if the target is an ACTIVE OWNER,
    the per-user locks of ALL currently active owners of the
    workspace, ascending key order. That closes the two-owner race:
    two concurrent revokes of two different owners would otherwise
    both count "2 active owners" and both proceed, leaving zero
    owners. Holding every active owner's lock serialises them, so
    the under-lock count is exact (see revoke_member).
  * transfer-owner: the current owner (actor) AND the target AND
    ALL currently active owners of the workspace, ascending key
    order -- the revoke pattern, because the transfer demotes an
    owner, so the same last-owner race applies: two concurrent
    transfers (or a transfer racing a revoke) must not both read
    "1 active owner" and leave zero. Holding every active owner's
    lock makes the under-lock owner count exact, and the swap
    itself (target -> owner, actor -> admin) is one transaction,
    so the workspace can never pass through a zero-owner state.

Pre-lock reads (CL-20 Stage 2 pattern): the lock set has to be
chosen before the locks can be taken, so each mutation does a
small pre-lock read used ONLY to choose whom to lock -- the
caller's own membership row (authorization pre-check), the target
membership row (to learn its user_id), or the invitee's users row
(to learn their id). Those readings are stale by construction and
are used for nothing else: every decision that mutates state --
the caller's role, the target's status, the invitee's existence
and user status, the active-owner count -- is re-read and decided
AFTER all locks are held. A conforming concurrent transaction
cannot change any of those conditions while the locks are held.

What is and is not testable here:
  * Testable (and tested): the key derivation is deterministic and
    a pure function; sequential mutations under the lock all
    succeed; the last-owner guard holds under the lock; a second
    session cannot acquire the same per-user lock while the first
    transaction holds it (live-PG test, lock_timeout-bounded).
  * NOT deterministically testable: true concurrent contention
    between two HTTP requests (timing-dependent by nature). The
    live test proves the lock is really taken (a second session
    blocks), which is the mechanism the contention behaviour rests
    on; the interleavings themselves are covered by the ordering
    argument above, not by a test.
  * Out of scope for M9: the Stage 2 anonymisation side of CL-20
    (no Stage 2 endpoint exists yet) and the live-session half of
    the CL-20 eligibility recheck (session creation does not take
    the per-user lock in this build). Membership mutations taking
    the lock is what makes a future conforming Stage 2 safe; the
    Stage 2 procedure itself, including its recheck, is future
    work. This is recorded, not hidden.

SQLite (test-only fallback): the advisory-lock statements are
PostgreSQL-only, so on any non-PostgreSQL dialect
_acquire_user_locks is a no-op. PostgreSQL is authoritative for
the locking behaviour; the SQLite branch exists ONLY so the unit
suite can exercise the service logic without a server.
"""

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.auth_models import MerchantMembership, User
from app.db.session import set_tenant_context
from app.logging_config import get_logger
from app.services import auth_service
from app.services.workspace_service import get_membership

logger = get_logger(__name__)

# Roles an invitation / role change may target. 'owner' is never a
# valid target: invitations cannot create owners and the role endpoint
# cannot promote to (or demote from) owner.
INVITABLE_ROLES = ("admin", "member", "viewer")
MANAGER_ROLES = ("owner", "admin")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Errors (the API layer maps these to HTTP statuses)
# ---------------------------------------------------------------------------

class MembershipNotFoundError(Exception):
    """Workspace or membership not found (caller maps to 404)."""


class NotManagerError(Exception):
    """Caller is not an owner/admin of the workspace (403)."""


class AlreadyMemberError(Exception):
    """Invitee already holds an active membership (409)."""


class MemberValidationError(Exception):
    """422: carries a machine-readable reason plus a human message."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# CL-20 advisory lock
# ---------------------------------------------------------------------------

def cl20_lock_key(user_id: uuid.UUID) -> int:
    """The CL-20 per-user advisory-lock key for a user id.

    First 8 bytes of SHA-256("cl20:" || raw UUID bytes), signed
    big-endian int64. Deterministic, stable across processes; the
    "cl20:" prefix namespaces the key so it cannot collide with any
    other advisory-lock user (e.g. CL-13 candidate locks).
    """
    digest = hashlib.sha256(b"cl20:" + uuid.UUID(str(user_id)).bytes).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _acquire_user_locks(db: Session, user_ids) -> None:
    """Acquire the CL-20 per-user lock for each id, ascending key
    order. PostgreSQL only; on SQLite (test-only) this is a no-op
    (see module docstring). Locks are transaction-scoped
    (pg_advisory_xact_lock): held to commit/rollback, never leaked
    through a pooled connection."""
    keys = sorted({cl20_lock_key(u) for u in user_ids})
    if not keys:
        return
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return  # test-only SQLite fallback: no advisory locks exist
    for key in keys:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _active_owner_count(db: Session, merchant_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(MerchantMembership)
        .where(
            MerchantMembership.merchant_id == merchant_id,
            MerchantMembership.role == "owner",
            MerchantMembership.status == "active",
        )
    ).scalar_one()


def _active_owner_ids(db: Session, merchant_id: uuid.UUID) -> list[uuid.UUID]:
    return [
        row[0]
        for row in db.execute(
            select(MerchantMembership.user_id).where(
                MerchantMembership.merchant_id == merchant_id,
                MerchantMembership.role == "owner",
                MerchantMembership.status == "active",
            )
        ).all()
    ]


def _member_out(db: Session, membership: MerchantMembership) -> dict:
    """Serialise a membership row with its user's email/name and its
    inviter's email. Built BEFORE commit, from rows already loaded,
    so the caller never has to refresh after the transaction-local
    RLS context is cleared by commit."""
    user = db.get(User, membership.user_id)
    inviter_email = None
    if membership.invited_by_user_id is not None:
        inviter = db.get(User, membership.invited_by_user_id)
        inviter_email = inviter.email if inviter is not None else None
    return {
        "id": str(membership.id),
        "user_id": str(membership.user_id),
        "email": user.email if user is not None else None,
        "display_name": user.display_name if user is not None else None,
        "role": membership.role,
        "status": membership.status,
        "invited_by": inviter_email,
        "created_at": membership.created_at.isoformat() if membership.created_at else None,
        "updated_at": membership.updated_at.isoformat() if membership.updated_at else None,
    }


def _require_manager(db: Session, actor: User, merchant_id: uuid.UUID) -> MerchantMembership:
    """Caller's active membership must exist and be owner/admin.
    Raises MembershipNotFoundError (404, no leak) or NotManagerError
    (403). Callers re-check this under the CL-20 lock before
    mutating; this is the pre-lock authorization pre-check."""
    membership = get_membership(db, actor.id, merchant_id)
    if membership is None:
        raise MembershipNotFoundError("Workspace not found.")
    if membership.role not in MANAGER_ROLES:
        raise NotManagerError("Only workspace owners and admins can manage members.")
    return membership


def _require_owner(db: Session, actor: User, merchant_id: uuid.UUID) -> MerchantMembership:
    """Caller's active membership must exist and be role 'owner'.
    Raises MembershipNotFoundError (404, no leak) or NotManagerError
    (403). Like _require_manager, callers re-check this under the
    CL-20 lock before mutating; this is the pre-lock authorization
    pre-check. Only the current owner may transfer ownership --
    admins may manage members but may not move the owner role."""
    membership = get_membership(db, actor.id, merchant_id)
    if membership is None:
        raise MembershipNotFoundError("Workspace not found.")
    if membership.role != "owner":
        raise NotManagerError("Only the workspace owner can transfer ownership.")
    return membership


def list_members(db: Session, caller: User, merchant_id: uuid.UUID) -> list[dict]:
    """All memberships of the workspace (any status), with each
    user's email/name and the inviter's email. Any ACTIVE member may
    list; non-members get 404 (no leak). Read-only: no CL-20 lock."""
    membership = get_membership(db, caller.id, merchant_id)
    if membership is None:
        raise MembershipNotFoundError("Workspace not found.")
    # Merchant-context SELECT arm (rls.sql) is needed to read OTHER
    # users' membership rows; the caller can read only their own row
    # without it.
    set_tenant_context(db, merchant_id)
    rows = db.execute(
        select(MerchantMembership)
        .where(MerchantMembership.merchant_id == merchant_id)
        .order_by(MerchantMembership.created_at)
    ).scalars().all()
    return [_member_out(db, row) for row in rows]


def invite_member(
    db: Session, actor: User, merchant_id: uuid.UUID, email: str, role: str
) -> tuple[dict, bool]:
    """Invite an existing user to the workspace as member/viewer/
    admin. Returns (member dict, created?).

    - Unknown email -> 422 reason "unknown_email": the model has no
      email-only invitation (MerchantMembership.user_id is a NOT
      NULL FK to users.id), so an invitation for a user who does not
      exist cannot be recorded without inventing a user row, which
      is prohibited. The invitee must register first, then be
      invited (or re-invited).
    - Invitee's users.status is not 'active' -> 422 "user_not_active".
    - Already an active member -> 409 AlreadyMemberError.
    - Already invited (same workspace) -> the existing invitation is
      returned unchanged (200, created=False): invite is idempotent.
    - Previously revoked -> the membership is reactivated to
      'invited' with the new role and inviter (200, created=False).
    - Role 'owner' -> 422 "role_not_invitable".
    """
    if role not in INVITABLE_ROLES:
        raise MemberValidationError(
            "role_not_invitable",
            "Role must be one of 'member', 'viewer' or 'admin'; owners cannot be invited.",
        )
    # Pre-lock reads (CL-20 Stage 2 pattern -- used ONLY to choose
    # whom to lock; every decision is re-made under the lock):
    # caller's authorization, and the invitee's identity.
    _require_manager(db, actor, merchant_id)
    invitee = auth_service.get_user_by_email(db, email)
    if invitee is None:
        raise MemberValidationError(
            "unknown_email",
            "No user exists with this email address. Invitations can only be sent to "
            "registered users; the person must register first, then be invited.",
        )
    # CL-20: lock inviter + invitee, ascending lock-key order.
    _acquire_user_locks(db, [actor.id, invitee.id])
    # Tenant context is required for the merchant_memberships
    # INSERT/UPDATE RLS policies (merchant-context only).
    set_tenant_context(db, merchant_id)
    # Recheck under the lock.
    _require_manager(db, actor, merchant_id)
    invitee = auth_service.get_user_by_email(db, email)
    if invitee is None:  # pragma: no cover - user deleted mid-flight
        raise MemberValidationError("unknown_email", "No user exists with this email address.")
    if invitee.status != "active":
        raise MemberValidationError(
            "user_not_active",
            "That user account is not active, so it cannot be invited.",
        )
    existing = db.execute(
        select(MerchantMembership).where(
            MerchantMembership.merchant_id == merchant_id,
            MerchantMembership.user_id == invitee.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == "active":
            raise AlreadyMemberError("That user is already an active member of this workspace.")
        if existing.status == "invited":
            out = _member_out(db, existing)
            return out, False
        # status == "revoked": reactivation (CL-20 lists reactivation
        # as a locked operation -- the lock above covers it).
        existing.status = "invited"
        existing.role = role
        existing.invited_by_user_id = actor.id
        existing.updated_at = _utcnow()
        db.commit()
        out = _member_out(db, existing)
        logger.info(
            "member_reinvited",
            extra={"merchant_id": str(merchant_id), "user_id": str(invitee.id)},
        )
        return out, False
    membership = MerchantMembership(
        merchant_id=merchant_id,
        user_id=invitee.id,
        role=role,
        status="invited",
        invited_by_user_id=actor.id,
    )
    db.add(membership)
    db.commit()
    out = _member_out(db, membership)
    logger.info(
        "member_invited",
        extra={"merchant_id": str(merchant_id), "user_id": str(invitee.id), "role": role},
    )
    return out, True


def accept_invitation(
    db: Session, caller: User, merchant_id: uuid.UUID, membership_id: uuid.UUID
) -> dict:
    """The invited user (and only that user) accepts. Membership
    becomes active. Accepting an already-active membership returns
    it (200) -- idempotent. A revoked invitation cannot be accepted
    (422 "invitation_revoked": it must be re-invited). The caller
    needs no ACTIVE membership for this endpoint -- the invited
    membership itself is their credential -- so the workspace
    membership check is intentionally not used here."""
    # Pre-lock read (lock-set determination only): the caller can
    # read their OWN membership rows without tenant context (RLS
    # user-based SELECT arm).
    membership = db.execute(
        select(MerchantMembership).where(
            MerchantMembership.id == membership_id,
            MerchantMembership.user_id == caller.id,
        )
    ).scalar_one_or_none()
    if membership is None or membership.merchant_id != merchant_id:
        # 404 either way: no leak about other workspaces' invitations.
        raise MembershipNotFoundError("Membership not found.")
    # CL-20: lock the invitee (== the actor here).
    _acquire_user_locks(db, [caller.id])
    set_tenant_context(db, merchant_id)
    # Recheck under the lock.
    db.refresh(membership)
    if membership.status == "active":
        return _member_out(db, membership)
    if membership.status == "revoked":
        raise MemberValidationError(
            "invitation_revoked",
            "This invitation was revoked. Ask a workspace owner or admin to invite you again.",
        )
    membership.status = "active"
    membership.updated_at = _utcnow()
    db.commit()
    out = _member_out(db, membership)
    logger.info(
        "member_invitation_accepted",
        extra={"merchant_id": str(merchant_id), "user_id": str(caller.id)},
    )
    return out


def change_role(
    db: Session, actor: User, merchant_id: uuid.UUID, membership_id: uuid.UUID, new_role: str
) -> dict:
    """Owner/Admin only. Change a member's role among member/viewer/
    admin. Never to (or from) owner via this endpoint: a target whose
    role is 'owner' -> 422 "owner_role_immutable"; new_role 'owner'
    -> 422 "role_not_invitable". Owner transfer is a separate
    endpoint (transfer_owner, M10 step 2): it must move the owner
    role, not just set it, and must preserve the last-owner
    invariant.
    An invited membership's role may be changed before acceptance
    (role correction); a revoked membership's role is frozen
    (422 "membership_revoked": re-invite instead). Because the role
    endpoint never touches an owner row, the last-owner guard is not
    needed here -- it lives in revoke_member and transfer_owner."""
    if new_role not in INVITABLE_ROLES:
        raise MemberValidationError(
            "role_not_invitable",
            "Role must be one of 'member', 'viewer' or 'admin'; the owner role cannot be set here.",
        )
    _require_manager(db, actor, merchant_id)
    # Tenant context first: the merchant-based SELECT arm is needed
    # to read another user's membership row (lock-set determination).
    set_tenant_context(db, merchant_id)
    target = db.execute(
        select(MerchantMembership).where(
            MerchantMembership.id == membership_id,
            MerchantMembership.merchant_id == merchant_id,
        )
    ).scalar_one_or_none()
    if target is None:
        raise MembershipNotFoundError("Membership not found.")
    # CL-20: lock the target user.
    _acquire_user_locks(db, [target.user_id])
    # Recheck under the lock.
    _require_manager(db, actor, merchant_id)
    db.refresh(target)
    if target.status == "revoked":
        raise MemberValidationError(
            "membership_revoked",
            "This membership was revoked. Invite the user again instead of changing its role.",
        )
    if target.role == "owner":
        raise MemberValidationError(
            "owner_role_immutable",
            "The owner role cannot be changed here. Owner transfer is a separate endpoint (POST /members/transfer-owner, M10).",
        )
    target.role = new_role
    target.updated_at = _utcnow()
    db.commit()
    out = _member_out(db, target)
    logger.info(
        "member_role_changed",
        extra={
            "merchant_id": str(merchant_id),
            "user_id": str(target.user_id),
            "role": new_role,
        },
    )
    return out


def revoke_member(
    db: Session, actor: User, merchant_id: uuid.UUID, membership_id: uuid.UUID
) -> dict:
    """Owner/Admin only. Sets status='revoked'. Idempotent: revoking
    an already-revoked membership returns it (200).

    Last-active-owner guard (OD-11): if the target is an active
    owner, the active-owner count is re-read UNDER the CL-20 locks
    (which, for an owner target, cover every active owner -- see the
    module docstring) and revocation is refused with 422
    "last_active_owner" when the count is 1. An owner may revoke
    themselves only when another active owner exists.
    """
    _require_manager(db, actor, merchant_id)
    set_tenant_context(db, merchant_id)
    target = db.execute(
        select(MerchantMembership).where(
            MerchantMembership.id == membership_id,
            MerchantMembership.merchant_id == merchant_id,
        )
    ).scalar_one_or_none()
    if target is None:
        raise MembershipNotFoundError("Membership not found.")
    # CL-20: lock the target; for an active-owner target, lock ALL
    # active owners (ascending key order) so the under-lock owner
    # count below is exact against concurrent owner revokes.
    lock_users = [target.user_id]
    if target.role == "owner" and target.status == "active":
        lock_users.extend(_active_owner_ids(db, merchant_id))
    _acquire_user_locks(db, lock_users)
    # Recheck under the lock.
    _require_manager(db, actor, merchant_id)
    db.refresh(target)
    if target.status == "revoked":
        return _member_out(db, target)
    if target.role == "owner" and target.status == "active":
        if _active_owner_count(db, merchant_id) <= 1:
            raise MemberValidationError(
                "last_active_owner",
                "This workspace would be left with no active owner. "
                "Transfer ownership before revoking the last owner.",
            )
    target.status = "revoked"
    target.updated_at = _utcnow()
    db.commit()
    out = _member_out(db, target)
    logger.info(
        "member_revoked",
        extra={"merchant_id": str(merchant_id), "user_id": str(target.user_id)},
    )
    return out


def transfer_owner(
    db: Session, actor: User, merchant_id: uuid.UUID, target_user_id: uuid.UUID
) -> dict:
    """Move the owner role from the caller to another ACTIVE member
    of the workspace (M10 step 2, the remaining OD-11 slice).

    Only the current owner may call it: a non-member gets 404 (no
    leak), an admin/member/viewer gets 403. The target is identified
    by USER id (not membership id): the members list the frontend
    already holds carries user_id, and a transfer is about a person,
    not about a membership row. The API layer takes the user id in
    the body as ``user_id`` (extra="forbid").

    Target rules (all 422, decided under the CL-20 locks):
      * no membership row for that user in this workspace, or the
        user does not exist at all -> "target_not_member". One
        reason for both: the caller cannot tell whether the user id
        belongs to a real user outside this workspace.
      * membership status 'invited' -> "target_not_active": the
        person must accept the invitation first; ownership is only
        held by active members.
      * membership status 'revoked' -> "target_revoked": invite
        them again first.
      * membership role already 'owner' -> "target_already_owner".
        This also rejects transferring to yourself (the caller is
        an owner): a transfer that would leave the owner unchanged
        is not a transfer. Stepping down to admin while ANOTHER
        owner already exists is not supported by this endpoint --
        transfer to a non-owner, or revoke the extra owner first
        (the last-owner guard still applies there).

    The old owner becomes an ADMIN, not a plain member: the person
    who ran the workspace keeps the ability to manage members and
    settings, which is what a handover normally means, and an admin
    cannot move the owner role again (only an owner can), so the
    transfer cannot be silently undone by the demoted owner. This
    choice is recorded here, not silently assumed.

    Atomicity and the last-owner guard (OD-11): the swap is one
    transaction -- the target's role is set to 'owner' and the
    caller's to 'admin' before a single commit, so the workspace
    never passes through a zero-owner state. The CL-20 lock set is
    the actor + the target + ALL currently active owners (the
    revoke pattern, ascending key order), so a transfer racing a
    revoke (or another transfer) sees an exact under-lock owner
    count. After the swap the active-owner count is asserted to be
    >= 1 before commit; the assertion cannot fail by construction
    (the target just became an owner in the same transaction), but
    it is the same guard as revoke_member's, kept as a backstop
    rather than trusted to the construction alone. The count is
    NOT asserted to be exactly 1: a workspace may legitimately
    have had two owners before the transfer, in which case two
    remain afterwards (the target plus the other owner).

    Returns the NEW owner's membership dict (MemberOut shape); the
    caller re-lists members to see their own demotion to admin.
    """
    # Pre-lock reads (CL-20 Stage 2 pattern -- used ONLY to choose
    # whom to lock; every decision is re-made under the lock):
    # the caller's owner authorization, the target's membership row,
    # and the current active owners (lock set).
    _require_owner(db, actor, merchant_id)
    set_tenant_context(db, merchant_id)
    target = db.execute(
        select(MerchantMembership).where(
            MerchantMembership.merchant_id == merchant_id,
            MerchantMembership.user_id == target_user_id,
        )
    ).scalar_one_or_none()
    lock_users = [actor.id, target_user_id]
    lock_users.extend(_active_owner_ids(db, merchant_id))
    # CL-20: lock actor + target + all active owners, ascending
    # lock-key order (_acquire_user_locks sorts and dedupes).
    _acquire_user_locks(db, lock_users)
    # Recheck under the lock. The pre-lock target row may be stale
    # in every field, so it is refreshed (the revoke_member pattern);
    # a row created concurrently cannot appear here -- the target
    # user's lock is held, and any invite/accept for that user takes
    # the same lock -- so a pre-lock None stays None.
    actor_membership = _require_owner(db, actor, merchant_id)
    if target is not None:
        db.refresh(target)
    if target is None:
        raise MemberValidationError(
            "target_not_member",
            "That user is not a member of this workspace. Only an active member "
            "can become the workspace owner.",
        )
    if target.status == "revoked":
        raise MemberValidationError(
            "target_revoked",
            "That member was revoked. Invite them again and have them accept "
            "before transferring ownership.",
        )
    if target.status != "active":
        raise MemberValidationError(
            "target_not_active",
            "That member has not accepted their invitation yet. Only an active "
            "member can become the workspace owner.",
        )
    if target.role == "owner":
        raise MemberValidationError(
            "target_already_owner",
            "That member is already an owner of this workspace. Transfer "
            "ownership to a member who is not already an owner.",
        )
    # The swap: target becomes owner first, then the caller steps
    # down to admin -- one transaction, one commit.
    target.role = "owner"
    target.updated_at = _utcnow()
    actor_membership.role = "admin"
    actor_membership.updated_at = _utcnow()
    # Last-owner backstop (OD-11): unreachable by construction --
    # the target just became an owner in this transaction -- but
    # asserted anyway, the same guard as revoke_member's.
    if _active_owner_count(db, merchant_id) < 1:
        raise MemberValidationError(
            "last_active_owner",
            "This workspace would be left with no active owner. "
            "Transfer ownership before revoking the last owner.",
        )
    db.commit()
    out = _member_out(db, target)
    logger.info(
        "member_owner_transferred",
        extra={
            "merchant_id": str(merchant_id),
            "from_user_id": str(actor.id),
            "to_user_id": str(target_user_id),
        },
    )
    return out
