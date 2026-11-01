"""CourierLedger M1 core database models (SQLAlchemy 2.x, PostgreSQL target).

Financial authority: V2.2.1 + P0 patch (see ~/MEMORY.md).
Schema authority: M1 Database Design v0.1 sections A-J, as amended by
CL-01..CL-26 (amendments v0.2-v0.5). Where an amendment and v0.1 disagree,
the amendment wins for the named item only.

Conventions (all deliberate, all documented here):
- Money: every amount column is BIGINT integer poisha, named ``*_poisha``.
  No FLOAT/REAL/NUMERIC money column exists anywhere in this module.
  No money column carries a server DEFAULT, with ONE documented exception:
  ``workspace_settings.tolerance_poisha`` has DEFAULT 0 because v0.1 C.7
  mandates it (it is a typed configuration control, not a calculated amount;
  the approved default tolerance is 0 poisha, set in settings, not in a run).
  ``reconciliation_runs.tolerance_poisha`` has NO default (v0.1 C.5: the value
  must be read from settings and recorded per run).
- NULL means unknown / not yet calculable, never zero. Verified zero is 0
  plus, where a source value is involved, a data-quality state of
  ``explicit_zero`` (CL-02 state-to-value model).
- Enumerations are TEXT columns with CHECK constraints, used consistently
  for every status/code column. The spec states every enumeration as a CHECK
  list; no PostgreSQL ENUM type is introduced, so a closed-list change is a
  reviewed migration, not a type alteration, and the CHECK text is visible
  in the migration and in ``triggers.sql``/``rls.sql`` review.
- Tenancy: ``merchant_id`` on every tenant-owned table; children reference
  parents by composite foreign keys ``(merchant_id, parent_id)`` against a
  ``UNIQUE (merchant_id, id)`` pair on every tenant-owned parent table
  (v0.1 A.7 / R2). All foreign keys are ON DELETE RESTRICT (CL-12/CL-15:
  a merchant row cannot be deleted while history exists).
- Primary keys are application-generated UUIDs (``uuid4``).
- All timestamps are TIMESTAMPTZ, server default ``now()``.
- ``password_reset_tokens`` is NOT in v0.1 section C (v0.1 keeps only
  ``users.password_hash``). It is defined in app/db/auth_models.py (auth
  agent) and reused here. The build brief's list actually names 32 tables
  (see README.md "Count discrepancy"); this module implements all 32.

Table count: 32 models (see ``ALL_TABLES`` at the end of this module).
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Identity tables are OWNED by the auth agent in app/db/auth_models.py and
# are imported/reused here, never duplicated (M1 build brief). They share
# this module's Base, so Base.metadata holds all 32 tables and is the
# single Alembic target. Known deviations of those definitions from
# v0.1 section C are recorded in app/db/README.md (token_hash naming,
# membership default, application-side timestamp defaults); the DB layer
# follows the auth implementation because the auth service and its tests
# depend on it. Only the spec-required CL-15 column (anonymised_at) was
# added to auth_models.Merchant, as a nullable column.
from app.db.auth_models import (
    Base,
    Merchant,
    MerchantMembership,
    PasswordResetToken,
    User,
    UserSession,
)

# ---------------------------------------------------------------------------
# Closed CHECK lists (spec v0.1 section C, as amended).
# ---------------------------------------------------------------------------

DQ_STATES_5 = ("present_valid", "explicit_zero", "column_absent", "blank", "invalid")
# CL-08: fee lines carry three per-basis states with this extended list.
DQ_STATES_7 = DQ_STATES_5 + ("rule_unknown", "contract_missing")
USER_STATUSES = ("active", "invited", "suspended", "closed")
MERCHANT_STATUSES = ("active", "suspended", "closing", "closed")
MEMBERSHIP_ROLES = ("owner", "admin", "member", "viewer")
MEMBERSHIP_STATUSES = ("active", "invited", "revoked")
COURIER_CODES = ("steadfast", "pathao", "paperfly", "redx", "ecourier")
ACCOUNT_STATUSES = ("active", "inactive")
TEMPLATE_STATUSES = ("draft", "active", "retired")
RULE_STATUSES = ("draft", "active", "superseded", "retired")
COD_FEE_BASES = ("expected_collectible", "reported_collected", "cod_minus_delivery_fee")
VAT_MODES = ("included_rate_on_net", "included_rate_on_gross", "excluded", "unknown")
EVIDENCE_TYPES = (
    "merchant_confirmation",
    "signed_rate_card",
    "courier_letter",
    "statement_annotation",
    "other_documented",
)
# column_mappings canonical fields named in v0.1 C.2. The spec says "and the
# remaining fields enumerated in the implementation ticket"; no such ticket
# was supplied with this build, so the CHECK lists exactly the ten fields
# the spec names. Extending it is a reviewed migration. See README.md.
CANONICAL_FIELDS = (
    "tracking_id",
    "merchant_order_ref",
    "reported_collected",
    "reported_payout",
    "delivery_fee",
    "cod_fee",
    "return_fee",
    "other_fee",
    "shipment_status",
    "statement_date",
)
SHIPMENT_OUTCOMES = (
    "delivered",
    "partially_delivered",
    "returned",
    "cancelled",
    "in_transit",
    "unknown",
)
OUTCOME_SOURCES = ("merchant_confirmed", "statement", "system_inferred_label_only")
ORDER_SOURCES = ("manual", "csv_import", "demo_synthetic")
BATCH_STATUSES = ("uploaded", "mapped", "parsed", "parse_failed", "ready", "superseded")
PARSE_STATUSES = ("parsed", "parsed_with_warnings", "failed", "skipped_blank")
ERROR_SEVERITIES = ("error", "warning")
ERROR_CODES = (
    "COLUMN_ABSENT",
    "VALUE_BLANK",
    "VALUE_INVALID",
    "VALUE_UNPARSABLE",
    "DUPLICATE_TRACKING",
    "AMBIGUOUS_MATCH",
    # Phase 1 (C-2.3): formula-like cell flagged at ingest.
    "FORMULA_LIKE_VALUE",
)
MATCH_STATUSES = ("strong_matched", "human_confirmed", "unmatched", "ambiguous", "duplicate")
MATCH_BASES = ("strong_tracking", "weak_order_ref")  # CL-03: never amount alone.
DECISIONS = ("confirmed", "rejected", "superseded")  # CL-03/CL-13 decision events.
RUN_STATUSES = ("running", "completed", "failed")
CONTRACTUAL_PAYOUT_STATUSES = ("matched", "mismatch", "matched_with_tolerance", "not_calculable")
DATA_QUALITY_STATUSES = ("ok", "has_warnings", "needs_review")
# CL-01 / P0 patch: five-value overall status; scenario-only V_total can
# produce "open", never "mismatch"; "mismatch" is reserved for fee /
# statement-arithmetic / contractual-payout findings.
OVERALL_STATUSES = ("matched", "open", "mismatch", "needs_review", "matched_with_tolerance")
FEE_CODES = ("delivery_fee", "cod_fee", "return_fee", "vat_on_fee", "other_fee")
FINDING_CATEGORIES = ("financial", "data_quality")
FINDING_TYPES = (
    "collection",
    "fee_tax",
    "statement_arithmetic",
    "conditional_payout",
    "scenario",
    "data_quality",
)
ALLOCATION_STATUSES = ("unallocated", "partially_allocated", "fully_allocated")
BANK_STATUSES = ("no_receipt", "partial_receipt", "receipt_complete_unallocated", "fully_reconciled")
ALLOCATION_KINDS = ("allocation", "reversal")  # CL-10: is_active deleted.
ACTOR_TYPES = ("user", "system")
# CL-04 (supersedes v0.1 dotted names): only two operations in M1, with
# typed result references. Receipt/allocation idempotency would need a
# schema change (new typed column + CHECK extension) -- see README.md.
IDEMPOTENCY_OPERATIONS = ("import_create", "run_create")
IDEMPOTENCY_STATUSES = ("in_progress", "completed", "failed")
INPUT_KINDS = ("receipt", "allocation")


def _check_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[DateTime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[DateTime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _merchant_id(nullable: bool = False) -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, nullable=nullable)


# ---------------------------------------------------------------------------
# C.1 Identity and workspace
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# C.1 Identity and workspace -- REUSED from app/db/auth_models.py
# ---------------------------------------------------------------------------
# User, Merchant, MerchantMembership, UserSession and PasswordResetToken are
# defined in app/db/auth_models.py (auth agent) and imported above. They are
# NOT redefined here. Everything below references them only by table/column
# name in foreign keys, so the single shared Base.metadata stays consistent.
#
# Spec notes on the reused definitions (see README.md for the full list):
# - merchants.anonymised_at (CL-15 tombstone) was added to auth_models.
# - user_sessions.token_hash is the auth service's server-managed opaque
#   session token hash; v0.1 C.1 names the column refresh_token_hash.
# - merchant_memberships.status defaults to 'active' in auth_models
#   (workspace-create flow); v0.1 C.1 states DEFAULT 'invited'.
# - Identity tables use application-side UTC defaults (_utcnow) rather
#   than server_default now(); all other M1 tables use server_default now().

# ---------------------------------------------------------------------------
# C.2 Courier configuration
# ---------------------------------------------------------------------------


class Courier(Base):
    """Global reference list of supported couriers (v0.1 C.2).

    Global class, read-only for the runtime role. This table must never
    acquire rate columns: rates are per merchant, per account and per
    evidence-backed rule version. Adding a code is a migration.
    """

    __tablename__ = "couriers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint(_check_list("code", COURIER_CODES), name="ck_couriers_code"),
    )


class MerchantCourierAccount(Base):
    """A merchant's account with one courier (v0.1 C.2). Tenant-owned."""

    __tablename__ = "merchant_courier_accounts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    courier_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    merchant_account_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[DateTime] = _created_at()
    updated_at: Mapped[DateTime] = _updated_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_mca_merchant",
        ),
        ForeignKeyConstraint(
            ["courier_id"], ["couriers.id"], ondelete="RESTRICT", name="fk_mca_courier"
        ),
        UniqueConstraint("merchant_id", "id", name="uq_mca_merchant_id"),
        CheckConstraint(_check_list("status", ACCOUNT_STATUSES), name="ck_mca_status"),
        Index("ix_mca_merchant_courier_status", "merchant_id", "courier_id", "status"),
    )


class CourierStatementTemplate(Base):
    """Versioned description of one courier statement layout (v0.1 C.2).

    Versioned, immutable once used. Global form per v0.1; whether a
    merchant may override a shared template is OD-07 (open) -- no
    merchant_id column is added here.
    """

    __tablename__ = "courier_statement_templates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    courier_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    header_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["courier_id"], ["couriers.id"], ondelete="RESTRICT",
            name="fk_templates_courier",
        ),
        UniqueConstraint("courier_id", "version_no", name="uq_templates_courier_version"),
        UniqueConstraint("id", name="uq_templates_id"),
        CheckConstraint("version_no > 0", name="ck_templates_version_no"),
        CheckConstraint(_check_list("status", TEMPLATE_STATUSES), name="ck_templates_status"),
    )


class ColumnMapping(Base):
    """One canonical field mapped from one source header, within one template
    (v0.1 C.2). Versioned with its template; per-import truth is the
    snapshot on import_batches, not this table."""

    __tablename__ = "column_mappings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    canonical_field: Mapped[str] = mapped_column(Text, nullable=False)
    source_header: Mapped[str] = mapped_column(Text, nullable=False)
    source_header_normalised: Mapped[str] = mapped_column(Text, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["template_id"], ["courier_statement_templates.id"], ondelete="RESTRICT",
            name="fk_column_mappings_template",
        ),
        UniqueConstraint(
            "template_id", "canonical_field", name="uq_column_mappings_template_field"
        ),
        CheckConstraint(
            _check_list("canonical_field", CANONICAL_FIELDS),
            name="ck_column_mappings_canonical_field",
        ),
        Index("ix_column_mappings_template", "template_id"),
    )


class CourierRuleVersion(Base):
    """One immutable, evidence-backed version of fee/payout rules for one
    merchant courier account (v0.1 C.2, CL-02). Tenant-owned, versioned.

    D_c (``contractual_delivery_fee_poisha``) is the merchant-confirmed
    contractual delivery fee used ONLY as the subtractor for a
    ``cod_minus_delivery_fee`` base. The CHECK below enforces the v0.1/V2.2.1
    rule at the row level for active versions: an active
    ``cod_minus_delivery_fee`` version must have ``dc_confirmed`` true, a
    non-empty ``dc_evidence_ref`` and a NOT NULL D_c. A draft may exist
    without them, but any reconciliation using it must produce NULL fees
    and payouts for the affected base plus DQ_MISSING_CONTRACT (engine
    responsibility, asserted by the P0-derived tests, not by this CHECK).
    ``negative_net_payout_permitted`` is the CL-02 permission field: a
    negative reported payout may be stored only when the rule version for
    that account explicitly permits it AND the statement line carries
    evidence for that semantics (OD-12 remains open; default is False,
    i.e. negative payouts are invalid).
    """

    __tablename__ = "courier_rule_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    merchant_courier_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    effective_from: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_to: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cod_fee_base: Mapped[str | None] = mapped_column(Text, nullable=True)
    cod_fee_rate_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contractual_delivery_fee_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dc_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dc_evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_fee_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    return_fee_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vat_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    vat_rate_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    negative_net_payout_permitted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    rule_definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_rule_versions_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "merchant_courier_account_id"],
            ["merchant_courier_accounts.merchant_id", "merchant_courier_accounts.id"],
            ondelete="RESTRICT",
            name="fk_rule_versions_account",
        ),
        ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_rule_versions_created_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_rule_versions_merchant_id"),
        UniqueConstraint(
            "merchant_id", "merchant_courier_account_id", "version_no",
            name="uq_rule_versions_account_version",
        ),
        CheckConstraint("version_no > 0", name="ck_rule_versions_version_no"),
        CheckConstraint(_check_list("status", RULE_STATUSES), name="ck_rule_versions_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_rule_versions_effective_window",
        ),
        CheckConstraint(
            "cod_fee_base IS NULL OR " + _check_list("cod_fee_base", COD_FEE_BASES),
            name="ck_rule_versions_cod_fee_base",
        ),
        CheckConstraint(
            "cod_fee_rate_bps IS NULL OR (cod_fee_rate_bps BETWEEN 0 AND 10000)",
            name="ck_rule_versions_cod_fee_rate_bps",
        ),
        CheckConstraint(
            "vat_mode IS NULL OR " + _check_list("vat_mode", VAT_MODES),
            name="ck_rule_versions_vat_mode",
        ),
        CheckConstraint(
            "vat_rate_bps IS NULL OR (vat_rate_bps BETWEEN 0 AND 10000)",
            name="ck_rule_versions_vat_rate_bps",
        ),
        # V2.2.1 / P0 D_c rule, row-local form for ACTIVE versions.
        CheckConstraint(
            "NOT (status = 'active' AND cod_fee_base = 'cod_minus_delivery_fee') OR "
            "(dc_confirmed IS TRUE AND dc_evidence_ref IS NOT NULL "
            "AND length(dc_evidence_ref) > 0 "
            "AND contractual_delivery_fee_poisha IS NOT NULL)",
            name="ck_rule_versions_dc_active",
        ),
        Index(
            "ix_rule_versions_account_status",
            "merchant_id", "merchant_courier_account_id", "status",
        ),
    )


class CourierRuleEvidence(Base):
    """Provenance for a rule version: what the merchant confirmed, and how
    (v0.1 C.2). Tenant-owned, append-only."""

    __tablename__ = "courier_rule_evidence"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    evidence_type: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    file_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    confirmed_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    confirmed_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_rule_evidence_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "rule_version_id"],
            ["courier_rule_versions.merchant_id", "courier_rule_versions.id"],
            ondelete="RESTRICT",
            name="fk_rule_evidence_rule_version",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "file_object_id"],
            ["file_objects.merchant_id", "file_objects.id"],
            ondelete="RESTRICT",
            name="fk_rule_evidence_file_object",
        ),
        ForeignKeyConstraint(
            ["confirmed_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_rule_evidence_confirmed_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_rule_evidence_merchant_id"),
        CheckConstraint(
            _check_list("evidence_type", EVIDENCE_TYPES), name="ck_rule_evidence_type"
        ),
        CheckConstraint("length(evidence_ref) > 0", name="ck_rule_evidence_ref_nonempty"),
        Index("ix_rule_evidence_rule_version", "merchant_id", "rule_version_id"),
    )


# ---------------------------------------------------------------------------
# C.3 Orders and shipments
# ---------------------------------------------------------------------------


class Order(Base):
    """A seller order as the merchant records it (v0.1 C.3). Tenant-owned.

    ``order_cod_poisha`` is the ORIGINAL order COD; it is never copied onto
    shipments as an assumption (V2.1). ``merchant_order_ref`` is indexed,
    NOT unique: OD-08 (whether merchant refs are unique) is open, so no
    uniqueness is claimed.
    """

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    merchant_order_ref: Mapped[str] = mapped_column(Text, nullable=False)
    order_cod_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="BDT")
    ordered_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    created_at: Mapped[DateTime] = _created_at()
    updated_at: Mapped[DateTime] = _updated_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT", name="fk_orders_merchant"
        ),
        UniqueConstraint("merchant_id", "id", name="uq_orders_merchant_id"),
        CheckConstraint(
            "order_cod_poisha IS NULL OR order_cod_poisha >= 0",
            name="ck_orders_order_cod_nonnegative",
        ),
        CheckConstraint("currency = 'BDT'", name="ck_orders_currency_bdt"),
        CheckConstraint(_check_list("source", ORDER_SOURCES), name="ck_orders_source"),
        Index("ix_orders_merchant_ref", "merchant_id", "merchant_order_ref"),
        Index("ix_orders_merchant_created", "merchant_id", "created_at"),
    )


class Shipment(Base):
    """One physical shipment under an order, with confirmed outcome and
    expected collectible COD (v0.1 C.3, V2.1). Tenant-owned.

    Expected collectible COD (E basis): delivered = amount to collect;
    partially delivered = documented partial amount only, else NULL;
    confirmed return/cancellation = verified 0; unknown/in-transit = NULL.
    The partial unique index enforces "one active COD-bearing shipment
    per order" (MVP scope restriction).
    """

    __tablename__ = "shipments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    merchant_courier_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    tracking_id_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    tracking_id_normalised: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    outcome_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_collectible_cod_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    partial_amount_evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_cod_bearing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    superseded_by_shipment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    superseded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    shipped_on = mapped_column(Date, nullable=True)
    created_at: Mapped[DateTime] = _created_at()
    updated_at: Mapped[DateTime] = _updated_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_shipments_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "order_id"],
            ["orders.merchant_id", "orders.id"],
            ondelete="RESTRICT",
            name="fk_shipments_order",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "merchant_courier_account_id"],
            ["merchant_courier_accounts.merchant_id", "merchant_courier_accounts.id"],
            ondelete="RESTRICT",
            name="fk_shipments_account",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "superseded_by_shipment_id"],
            ["shipments.merchant_id", "shipments.id"],
            ondelete="RESTRICT",
            name="fk_shipments_superseded_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_shipments_merchant_id"),
        CheckConstraint(_check_list("outcome", SHIPMENT_OUTCOMES), name="ck_shipments_outcome"),
        CheckConstraint(
            "outcome_source IS NULL OR " + _check_list("outcome_source", OUTCOME_SOURCES),
            name="ck_shipments_outcome_source",
        ),
        CheckConstraint(
            "expected_collectible_cod_poisha IS NULL OR expected_collectible_cod_poisha >= 0",
            name="ck_shipments_expected_cod_nonnegative",
        ),
        CheckConstraint(
            "superseded_by_shipment_id IS NULL OR is_superseded IS TRUE",
            name="ck_shipments_superseded_by_requires_flag",
        ),
        Index("ix_shipments_merchant_tracking", "merchant_id", "tracking_id_normalised"),
        Index("ix_shipments_merchant_order", "merchant_id", "order_id"),
        Index(
            "ix_shipments_merchant_account_outcome",
            "merchant_id", "merchant_courier_account_id", "outcome",
        ),
        Index(
            "uq_shipments_one_active_cod_bearing_per_order",
            "merchant_id", "order_id",
            unique=True,
            postgresql_where=(is_cod_bearing.is_(True) & is_superseded.is_(False)),
        ),
    )


# ---------------------------------------------------------------------------
# C.4 CSV import
# ---------------------------------------------------------------------------


class FileObject(Base):
    """Metadata for an uploaded file held outside the database (v0.1 C.4).

    Tenant-owned, append-only. File bytes live in object storage, referenced
    by ``storage_key``; only metadata + hash live here.
    """

    __tablename__ = "file_objects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[DateTime] = _created_at()
    purged_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_file_objects_merchant",
        ),
        ForeignKeyConstraint(
            ["uploaded_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_file_objects_uploaded_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_file_objects_merchant_id"),
        UniqueConstraint("merchant_id", "sha256", name="uq_file_objects_merchant_sha256"),
        CheckConstraint("size_bytes >= 0", name="ck_file_objects_size_nonnegative"),
        CheckConstraint(
            "content_type IN ('text/csv', 'application/vnd.ms-excel', 'unknown')",
            name="ck_file_objects_content_type",
        ),
    )


class ImportBatch(Base):
    """One CSV import attempt for one courier account (v0.1 C.4, CL-04).

    CL-04 removed the ``idempotency_key`` column: idempotency_keys is the
    sole owner of idempotency. ``mapping_snapshot`` is immutable once status
    leaves 'uploaded'.
    """

    __tablename__ = "import_batches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    file_object_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    merchant_courier_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    template_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mapping_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="uploaded")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parsed_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    statement_period_start = mapped_column(Date, nullable=True)
    statement_period_end = mapped_column(Date, nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[DateTime] = _created_at()
    updated_at: Mapped[DateTime] = _updated_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_import_batches_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "file_object_id"],
            ["file_objects.merchant_id", "file_objects.id"],
            ondelete="RESTRICT",
            name="fk_import_batches_file_object",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "merchant_courier_account_id"],
            ["merchant_courier_accounts.merchant_id", "merchant_courier_accounts.id"],
            ondelete="RESTRICT",
            name="fk_import_batches_account",
        ),
        ForeignKeyConstraint(
            ["template_id"], ["courier_statement_templates.id"], ondelete="RESTRICT",
            name="fk_import_batches_template",
        ),
        ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_import_batches_created_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_import_batches_merchant_id"),
        CheckConstraint(_check_list("status", BATCH_STATUSES), name="ck_import_batches_status"),
        CheckConstraint("row_count >= 0", name="ck_import_batches_row_count"),
        CheckConstraint("parsed_row_count >= 0", name="ck_import_batches_parsed_row_count"),
        CheckConstraint("error_row_count >= 0", name="ck_import_batches_error_row_count"),
        Index("ix_import_batches_merchant_status_created", "merchant_id", "status", "created_at"),
        Index(
            "ix_import_batches_merchant_account_created",
            "merchant_id", "merchant_courier_account_id", "created_at",
        ),
    )


class ImportRow(Base):
    """One raw CSV row, preserved verbatim (v0.1 C.4). Tenant-owned,
    append-only after parsing. ``raw_values`` keeps absent keys absent and
    blanks as empty strings: absent / blank / "0" are three different states.
    Duplicate rows are kept and flagged; no unique constraint merges them.
    """

    __tablename__ = "import_rows"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    import_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_line_hash: Mapped[str] = mapped_column(Text, nullable=False)
    parse_status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_import_rows_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "import_batch_id"],
            ["import_batches.merchant_id", "import_batches.id"],
            ondelete="RESTRICT",
            name="fk_import_rows_batch",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_import_rows_merchant_id"),
        UniqueConstraint(
            "import_batch_id", "row_number", name="uq_import_rows_batch_row_number"
        ),
        CheckConstraint("row_number > 0", name="ck_import_rows_row_number"),
        CheckConstraint(
            _check_list("parse_status", PARSE_STATUSES), name="ck_import_rows_parse_status"
        ),
        Index("ix_import_rows_batch_row", "merchant_id", "import_batch_id", "row_number"),
    )


class ImportError(Base):
    """One error or warning against a batch, row or field (v0.1 C.4).
    Queryable projection of row validation, not a second source of truth;
    an error row always references the row and field it describes."""

    __tablename__ = "import_errors"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    import_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    import_row_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    field_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_import_errors_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "import_batch_id"],
            ["import_batches.merchant_id", "import_batches.id"],
            ondelete="RESTRICT",
            name="fk_import_errors_batch",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "import_row_id"],
            ["import_rows.merchant_id", "import_rows.id"],
            ondelete="RESTRICT",
            name="fk_import_errors_row",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_import_errors_merchant_id"),
        CheckConstraint(
            _check_list("severity", ERROR_SEVERITIES), name="ck_import_errors_severity"
        ),
        CheckConstraint(
            _check_list("error_code", ERROR_CODES), name="ck_import_errors_error_code"
        ),
        Index("ix_import_errors_batch_severity", "merchant_id", "import_batch_id", "severity"),
        Index("ix_import_errors_row", "merchant_id", "import_row_id"),
    )


def _state_value_check(amount_col: str, state_col: str, name: str, *, allow_negative: bool = False) -> CheckConstraint:
    """CL-02 state-to-value CHECK for one amount/state pair.

    present_valid -> value NOT NULL (and >= 0 unless ``allow_negative``);
    explicit_zero -> value exactly 0; column_absent / blank / invalid ->
    value NULL. Zero is never stored under present_valid.
    """
    if allow_negative:
        present = f"({state_col} = 'present_valid' AND {amount_col} IS NOT NULL)"
    else:
        present = (
            f"({state_col} = 'present_valid' AND {amount_col} IS NOT NULL "
            f"AND {amount_col} > 0)"
        )
    return CheckConstraint(
        f"({present} "
        f"OR ({state_col} = 'explicit_zero' AND {amount_col} = 0) "
        f"OR ({state_col} IN ('column_absent', 'blank', 'invalid') "
        f"AND {amount_col} IS NULL))",
        name=name,
    )


class StatementLine(Base):
    """Normalised courier-reported values for one imported row, with
    per-field data-quality state (v0.1 C.4, CL-02). Tenant-owned, append-only.

    Reported values live here, never on shipments. ``shipment_id`` is set
    only by a strong match or a human-confirmed candidate, never by amount
    alone. ``negative_payout_permitted`` is the CL-02 snapshot copied from
    the confirmed rule version at import time: a negative
    ``reported_payout_poisha`` is storable only when it is true (OD-12
    open; default False, i.e. negative payouts are invalid and stored NULL).
    """

    __tablename__ = "statement_lines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    import_row_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    import_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    match_status: Mapped[str] = mapped_column(Text, nullable=False, default="unmatched")
    tracking_id_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    tracking_id_normalised: Mapped[str | None] = mapped_column(Text, nullable=True)
    merchant_order_ref_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_status_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    statement_date = mapped_column(Date, nullable=True)
    reported_collected_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_collected_state: Mapped[str] = mapped_column(Text, nullable=False)
    reported_payout_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_payout_state: Mapped[str] = mapped_column(Text, nullable=False)
    reported_delivery_fee_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_delivery_fee_state: Mapped[str] = mapped_column(Text, nullable=False)
    reported_cod_fee_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_cod_fee_state: Mapped[str] = mapped_column(Text, nullable=False)
    reported_return_fee_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_return_fee_state: Mapped[str] = mapped_column(Text, nullable=False)
    reported_other_fee_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_other_fee_state: Mapped[str] = mapped_column(Text, nullable=False)
    negative_payout_permitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_statement_lines_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "import_row_id"],
            ["import_rows.merchant_id", "import_rows.id"],
            ondelete="RESTRICT",
            name="fk_statement_lines_import_row",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "import_batch_id"],
            ["import_batches.merchant_id", "import_batches.id"],
            ondelete="RESTRICT",
            name="fk_statement_lines_import_batch",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "shipment_id"],
            ["shipments.merchant_id", "shipments.id"],
            ondelete="RESTRICT",
            name="fk_statement_lines_shipment",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_statement_lines_merchant_id"),
        UniqueConstraint(
            "merchant_id", "import_row_id", name="uq_statement_lines_merchant_import_row"
        ),
        CheckConstraint(
            _check_list("match_status", MATCH_STATUSES), name="ck_statement_lines_match_status"
        ),
        CheckConstraint(
            _check_list("reported_collected_state", DQ_STATES_5),
            name="ck_statement_lines_collected_state",
        ),
        CheckConstraint(
            _check_list("reported_payout_state", DQ_STATES_5),
            name="ck_statement_lines_payout_state",
        ),
        CheckConstraint(
            _check_list("reported_delivery_fee_state", DQ_STATES_5),
            name="ck_statement_lines_delivery_fee_state",
        ),
        CheckConstraint(
            _check_list("reported_cod_fee_state", DQ_STATES_5),
            name="ck_statement_lines_cod_fee_state",
        ),
        CheckConstraint(
            _check_list("reported_return_fee_state", DQ_STATES_5),
            name="ck_statement_lines_return_fee_state",
        ),
        CheckConstraint(
            _check_list("reported_other_fee_state", DQ_STATES_5),
            name="ck_statement_lines_other_fee_state",
        ),
        _state_value_check(
            "reported_collected_poisha", "reported_collected_state",
            "ck_statement_lines_collected_state_value",
        ),
        # CL-02: negative P_r allowed only under the snapshotted permission.
        CheckConstraint(
            "((reported_payout_state = 'present_valid' "
            "AND reported_payout_poisha IS NOT NULL "
            "AND (reported_payout_poisha >= 0 OR negative_payout_permitted IS TRUE)) "
            "OR (reported_payout_state = 'explicit_zero' AND reported_payout_poisha = 0) "
            "OR (reported_payout_state IN ('column_absent', 'blank', 'invalid') "
            "AND reported_payout_poisha IS NULL))",
            name="ck_statement_lines_payout_state_value",
        ),
        _state_value_check(
            "reported_delivery_fee_poisha", "reported_delivery_fee_state",
            "ck_statement_lines_delivery_fee_state_value",
        ),
        _state_value_check(
            "reported_cod_fee_poisha", "reported_cod_fee_state",
            "ck_statement_lines_cod_fee_state_value",
        ),
        _state_value_check(
            "reported_return_fee_poisha", "reported_return_fee_state",
            "ck_statement_lines_return_fee_state_value",
        ),
        _state_value_check(
            "reported_other_fee_poisha", "reported_other_fee_state",
            "ck_statement_lines_other_fee_state_value",
        ),
        Index("ix_statement_lines_batch", "merchant_id", "import_batch_id"),
        Index("ix_statement_lines_shipment", "merchant_id", "shipment_id"),
        Index("ix_statement_lines_tracking", "merchant_id", "tracking_id_normalised"),
        Index(
            "ix_statement_lines_unmatched",
            "merchant_id", "match_status",
            postgresql_where=(match_status.in_(("unmatched", "ambiguous"))),
        ),
    )


class ShipmentMatchCandidate(Base):
    """Every candidate match considered for a statement line (v0.1 C.4,
    CL-03, CL-13). Tenant-owned, strictly insert-only -- NO decision columns
    (CL-03 removed them; decisions live in ShipmentMatchDecision).

    ``prior_candidate_id`` is CL-13 section 2(d): a successor candidate
    created to replace a conflicted candidate, linked by composite self-FK.
    """

    __tablename__ = "shipment_match_candidates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    statement_line_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    shipment_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    match_basis: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    prior_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_match_candidates_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "statement_line_id"],
            ["statement_lines.merchant_id", "statement_lines.id"],
            ondelete="RESTRICT",
            name="fk_match_candidates_statement_line",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "shipment_id"],
            ["shipments.merchant_id", "shipments.id"],
            ondelete="RESTRICT",
            name="fk_match_candidates_shipment",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "prior_candidate_id"],
            ["shipment_match_candidates.merchant_id", "shipment_match_candidates.id"],
            ondelete="RESTRICT",
            name="fk_match_candidates_prior_candidate",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_match_candidates_merchant_id"),
        CheckConstraint(_check_list("match_basis", MATCH_BASES), name="ck_match_candidates_basis"),
        CheckConstraint("rank > 0", name="ck_match_candidates_rank"),
        Index("ix_match_candidates_line", "merchant_id", "statement_line_id"),
        Index("ix_match_candidates_shipment", "merchant_id", "shipment_id"),
    )


class ShipmentMatchDecision(Base):
    """One decision event about one match candidate (v0.2 CL-03/CL-07,
    v0.3 CL-13, v0.4 CL-18). Append-only; a changed mind is a superseding
    row, never an edit.

    The current decision for a candidate is DERIVED (latest row in the
    linear chain; see triggers.sql and README.md), never stored mutably.
    Chain rules (CL-13): exactly one root per candidate in well-formed
    history, at most one successor per decision (partial unique index),
    supersession stays inside one candidate and one tenant (composite FK
    on ``(merchant_id, candidate_id, supersedes_decision_id)``).
    """

    __tablename__ = "shipment_match_decisions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_match_decisions_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "candidate_id"],
            ["shipment_match_candidates.merchant_id", "shipment_match_candidates.id"],
            ondelete="RESTRICT",
            name="fk_match_decisions_candidate",
        ),
        ForeignKeyConstraint(
            ["decided_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_match_decisions_decided_by",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "candidate_id", "supersedes_decision_id"],
            ["shipment_match_decisions.merchant_id", "shipment_match_decisions.candidate_id",
             "shipment_match_decisions.id"],
            ondelete="RESTRICT",
            name="fk_match_decisions_supersedes",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_match_decisions_merchant_id"),
        UniqueConstraint(
            "merchant_id", "candidate_id", "id",
            name="uq_match_decisions_merchant_candidate_id",
        ),
        CheckConstraint(_check_list("decision", DECISIONS), name="ck_match_decisions_decision"),
        CheckConstraint(
            "supersedes_decision_id IS NULL OR supersedes_decision_id <> id",
            name="ck_match_decisions_no_self_supersede",
        ),
        Index(
            "uq_match_decisions_one_successor",
            "merchant_id", "supersedes_decision_id",
            unique=True,
            postgresql_where=(supersedes_decision_id.is_not(None)),
        ),
        Index("ix_match_decisions_candidate", "merchant_id", "candidate_id"),
    )


# ---------------------------------------------------------------------------
# C.5 Reconciliation
# ---------------------------------------------------------------------------


class ReconciliationRun(Base):
    """One immutable execution of the reconciliation engine over one import
    batch (v0.1 C.5, CL-04, CL-05). Tenant-owned, append-only.

    CL-04 removed the ``idempotency_key`` column. CL-05: deduplication on
    UNIQUE (merchant_id, run_hash) is the single behaviour -- a run-create
    whose hash already exists returns the existing run. ``tolerance_poisha``
    has NO default: it is copied from workspace settings at run time.
    The runtime role may UPDATE only status/completed_at, and only through
    the controlled path (see roles.sql / rls.sql); there is no delete path.
    """

    __tablename__ = "reconciliation_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    import_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    merchant_courier_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rule_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    rule_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    mapping_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tolerance_poisha: Mapped[int] = mapped_column(BigInteger, nullable=False)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    run_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[DateTime] = _created_at()
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_runs_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "import_batch_id"],
            ["import_batches.merchant_id", "import_batches.id"],
            ondelete="RESTRICT",
            name="fk_runs_import_batch",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "merchant_courier_account_id"],
            ["merchant_courier_accounts.merchant_id", "merchant_courier_accounts.id"],
            ondelete="RESTRICT",
            name="fk_runs_account",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "rule_version_id"],
            ["courier_rule_versions.merchant_id", "courier_rule_versions.id"],
            ondelete="RESTRICT",
            name="fk_runs_rule_version",
        ),
        ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_runs_created_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_runs_merchant_id"),
        UniqueConstraint("merchant_id", "run_hash", name="uq_runs_merchant_run_hash"),
        CheckConstraint("tolerance_poisha >= 0", name="ck_runs_tolerance_nonnegative"),
        CheckConstraint(_check_list("status", RUN_STATUSES), name="ck_runs_status"),
        Index("ix_runs_batch_created", "merchant_id", "import_batch_id", "created_at"),
        Index("ix_runs_merchant_created", "merchant_id", "created_at"),
    )


def _identity_check(dependent: str, terms: tuple[str, ...], formula: str, name: str) -> CheckConstraint:
    """Row-local identity CHECK (v0.1 C.5, relaxed by M6-A1): when every
    term is NOT NULL the dependent must equal ``formula``; when any term
    is NULL the CHECK imposes nothing on the dependent. M6-A1 dropped
    the original NULL-propagation arm (any term NULL forced the
    dependent NULL) because the V2.2.1 engine legitimately emits a
    non-NULL dependent from a partial identity: V_total = P_r - P_e is
    defined whenever P_r and P_e are known even if C / delta / V_R / S
    are NULL (e.g. blank collected -> V_total -2000 with C/delta/V_R/S
    all NULL), and V_cond = P_r - P_cond is defined whenever P_r and
    P_cond are known even if S / V_R are NULL (e.g. blank COD-fee line
    -> V_cond 0 with S/V_R NULL). Both are correct needs_review
    results; forcing the dependent NULL would discard an exactly
    computed value. The identity is still enforced whenever it is
    fully defined, so a genuinely violated identity (all terms
    non-NULL, dependent != formula) is still rejected."""
    all_present = " AND ".join(f"{t} IS NOT NULL" for t in terms)
    any_missing = " OR ".join(f"{t} IS NULL" for t in terms)
    return CheckConstraint(
        f"(({all_present} AND {dependent} = ({formula})) OR ({any_missing}))",
        name=name,
    )


class ReconciliationResult(Base):
    """Calculated comparison for one shipment in one run (v0.1 C.5, CL-01,
    V2.2/V2.2.1). Tenant-owned, append-only.

    Money identities (V2.2.1): C = R - E; V_total = P_r - P_e = C - delta -
    V_R + S; V_cond = P_r - P_cond = S - V_R. The two CHECKs below enforce
    the V_total / V_cond identities row-locally, but only when every
    term is non-NULL (M6-A1): the engine legitimately emits a non-NULL
    dependent from a partial identity (V_total from P_r - P_e while
    C/delta/V_R/S are NULL; V_cond from P_r - P_cond while S/V_R are
    NULL), and those values are stored, not forced to NULL. The C
    identity is an engine construction rule (C is stored, E/R are stored
    beside it). Overall status derivation is CL-01 (see triggers.sql deferred trigger, CL-17:
    cross-table consistency is a trigger, not a CHECK): needs_review if any
    required value is NULL or a data-quality finding blocks exact
    calculation; mismatch if V_R, S or V_cond is non-zero outside tolerance;
    open if only C or scenario V_total is non-zero outside tolerance;
    matched_with_tolerance if a finding is non-zero but within tolerance;
    matched otherwise. V_total is scenario-only and never decides the
    contractual payout status (decided by V_cond alone).
    """

    __tablename__ = "reconciliation_results"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    shipment_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    statement_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    expected_collectible_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_collected_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    c_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    x_e_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    x_r_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    x_upper_r_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delta_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    v_r_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_e_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_cond_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_r_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    s_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    v_cond_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    v_total_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    contractual_payout_status: Mapped[str] = mapped_column(Text, nullable=False)
    data_quality_status: Mapped[str] = mapped_column(Text, nullable=False)
    overall_status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_results_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "run_id"],
            ["reconciliation_runs.merchant_id", "reconciliation_runs.id"],
            ondelete="RESTRICT",
            name="fk_results_run",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "shipment_id"],
            ["shipments.merchant_id", "shipments.id"],
            ondelete="RESTRICT",
            name="fk_results_shipment",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "statement_line_id"],
            ["statement_lines.merchant_id", "statement_lines.id"],
            ondelete="RESTRICT",
            name="fk_results_statement_line",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_results_merchant_id"),
        UniqueConstraint(
            "merchant_id", "run_id", "shipment_id",
            name="uq_results_merchant_run_shipment",
        ),
        CheckConstraint(
            _check_list("contractual_payout_status", CONTRACTUAL_PAYOUT_STATUSES),
            name="ck_results_contractual_payout_status",
        ),
        CheckConstraint(
            _check_list("data_quality_status", DATA_QUALITY_STATUSES),
            name="ck_results_data_quality_status",
        ),
        CheckConstraint(
            _check_list("overall_status", OVERALL_STATUSES), name="ck_results_overall_status"
        ),
        _identity_check(
            "v_cond_poisha", ("s_poisha", "v_r_poisha"), "s_poisha - v_r_poisha",
            "ck_results_v_cond_identity",
        ),
        _identity_check(
            "v_total_poisha",
            ("c_poisha", "delta_poisha", "v_r_poisha", "s_poisha"),
            "c_poisha - delta_poisha - v_r_poisha + s_poisha",
            "ck_results_v_total_identity",
        ),
        Index("ix_results_run", "merchant_id", "run_id"),
        Index("ix_results_shipment", "merchant_id", "shipment_id"),
        Index(
            "ix_results_work_queue",
            "merchant_id", "overall_status",
            postgresql_where=(overall_status.in_(("open", "mismatch", "needs_review"))),
        ),
    )


def _fee_basis_check(amount_col: str, state_col: str, name: str) -> CheckConstraint:
    """CL-08 per-basis state-to-value CHECK for one fee-line basis.

    present_valid -> amount NOT NULL; explicit_zero -> amount exactly 0;
    column_absent / blank / invalid / rule_unknown / contract_missing ->
    amount NULL. One basis being NULL never poisons the other two."""
    return CheckConstraint(
        f"(({state_col} = 'present_valid' AND {amount_col} IS NOT NULL) "
        f"OR ({state_col} = 'explicit_zero' AND {amount_col} = 0) "
        f"OR ({state_col} IN ('column_absent', 'blank', 'invalid', "
        f"'rule_unknown', 'contract_missing') AND {amount_col} IS NULL))",
        name=name,
    )


class ReconciliationFeeLine(Base):
    """One fee component of one result, in all three bases (v0.1 C.5,
    CL-08, CL-09). Tenant-owned, append-only.

    CL-08 replaced the single ``data_quality_state`` with three per-basis
    states. CL-09: key is (result_id, fee_code, component_seq) so two
    components of one category are representable without merging; each is
    rounded independently (per-line half-up, engine rule).
    ``included_tax_poisha`` is a breakdown only, never subtracted again.
    """

    __tablename__ = "reconciliation_fee_lines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    result_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    fee_code: Mapped[str] = mapped_column(Text, nullable=False)
    component_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_statement_field: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_basis_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_basis_state: Mapped[str] = mapped_column(Text, nullable=False)
    reported_basis_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_basis_state: Mapped[str] = mapped_column(Text, nullable=False)
    reported_actual_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_actual_state: Mapped[str] = mapped_column(Text, nullable=False)
    included_tax_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rounding_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_fee_lines_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "result_id"],
            ["reconciliation_results.merchant_id", "reconciliation_results.id"],
            ondelete="RESTRICT",
            name="fk_fee_lines_result",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_fee_lines_merchant_id"),
        UniqueConstraint(
            "merchant_id", "result_id", "fee_code", "component_seq",
            name="uq_fee_lines_merchant_result_fee_seq",
        ),
        CheckConstraint(_check_list("fee_code", FEE_CODES), name="ck_fee_lines_fee_code"),
        CheckConstraint("component_seq >= 1", name="ck_fee_lines_component_seq"),
        CheckConstraint(
            _check_list("expected_basis_state", DQ_STATES_7),
            name="ck_fee_lines_expected_basis_state",
        ),
        CheckConstraint(
            _check_list("reported_basis_state", DQ_STATES_7),
            name="ck_fee_lines_reported_basis_state",
        ),
        CheckConstraint(
            _check_list("reported_actual_state", DQ_STATES_7),
            name="ck_fee_lines_reported_actual_state",
        ),
        _fee_basis_check(
            "expected_basis_poisha", "expected_basis_state",
            "ck_fee_lines_expected_basis_state_value",
        ),
        _fee_basis_check(
            "reported_basis_poisha", "reported_basis_state",
            "ck_fee_lines_reported_basis_state_value",
        ),
        _fee_basis_check(
            "reported_actual_poisha", "reported_actual_state",
            "ck_fee_lines_reported_actual_state_value",
        ),
        Index("ix_fee_lines_result", "merchant_id", "result_id"),
    )


class ReconciliationFinding(Base):
    """One independent finding raised by one result (v0.1 C.5, CL-01).
    Tenant-owned, append-only. Findings are never summed into a single
    loss figure by the database or by a view; no column or view may label a
    finding a "confirmed loss"."""

    __tablename__ = "reconciliation_findings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    result_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    finding_type: Mapped[str] = mapped_column(Text, nullable=False)
    finding_code: Mapped[str] = mapped_column(Text, nullable=False)
    amount_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_within_tolerance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    explanation_en: Mapped[str] = mapped_column(Text, nullable=False)
    explanation_bn: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_findings_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "run_id"],
            ["reconciliation_runs.merchant_id", "reconciliation_runs.id"],
            ondelete="RESTRICT",
            name="fk_findings_run",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "result_id"],
            ["reconciliation_results.merchant_id", "reconciliation_results.id"],
            ondelete="RESTRICT",
            name="fk_findings_result",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_findings_merchant_id"),
        UniqueConstraint(
            "merchant_id", "result_id", "finding_code",
            name="uq_findings_merchant_result_code",
        ),
        CheckConstraint(
            _check_list("category", FINDING_CATEGORIES), name="ck_findings_category"
        ),
        CheckConstraint(
            _check_list("finding_type", FINDING_TYPES), name="ck_findings_finding_type"
        ),
        Index("ix_findings_run_category", "merchant_id", "run_id", "category"),
        Index("ix_findings_code", "merchant_id", "finding_code"),
        Index(
            "ix_findings_dq_queue",
            "merchant_id", "category",
            postgresql_where=(category == "data_quality"),
        ),
    )


# ---------------------------------------------------------------------------
# C.6 Settlement
# ---------------------------------------------------------------------------


class SettlementBatch(Base):
    """Payout expected from one courier account for one period, against
    which bank receipts are checked (v0.1 C.6, CL-16). Tenant-owned.

    CL-16 REMOVED the stored ``bank_variance_poisha`` column: bank variance
    is now a derived value from settlement_variance_calculations (current
    variance = highest-seq calculation whose input set still equals the
    batch's current input set and whose preconditions still hold; else
    NULL). ``allocated_total_poisha`` is NULL until at least one allocation
    exists.
    """

    __tablename__ = "settlement_batches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    merchant_courier_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    period_start = mapped_column(Date, nullable=True)
    period_end = mapped_column(Date, nullable=True)
    expected_batch_payout_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    allocated_total_poisha: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    allocation_status: Mapped[str] = mapped_column(Text, nullable=False, default="unallocated")
    bank_status: Mapped[str] = mapped_column(Text, nullable=False, default="no_receipt")
    created_at: Mapped[DateTime] = _created_at()
    updated_at: Mapped[DateTime] = _updated_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_settlement_batches_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "merchant_courier_account_id"],
            ["merchant_courier_accounts.merchant_id", "merchant_courier_accounts.id"],
            ondelete="RESTRICT",
            name="fk_settlement_batches_account",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "run_id"],
            ["reconciliation_runs.merchant_id", "reconciliation_runs.id"],
            ondelete="RESTRICT",
            name="fk_settlement_batches_run",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_settlement_batches_merchant_id"),
        CheckConstraint(
            "period_end IS NULL OR period_end >= period_start",
            name="ck_settlement_batches_period",
        ),
        CheckConstraint(
            _check_list("allocation_status", ALLOCATION_STATUSES),
            name="ck_settlement_batches_allocation_status",
        ),
        CheckConstraint(
            _check_list("bank_status", BANK_STATUSES),
            name="ck_settlement_batches_bank_status",
        ),
        Index(
            "ix_settlement_batches_account_period",
            "merchant_id", "merchant_courier_account_id", "period_start",
        ),
        Index("ix_settlement_batches_allocation_status", "merchant_id", "allocation_status"),
    )


class BankReceipt(Base):
    """Money the merchant confirms was received, as manual bank-confirmation
    evidence (v0.1 C.6). Tenant-owned. Multiple partial receipts per batch
    are expected; a partial receipt is a status, not a discrepancy.

    No bank account number column exists on this table, and none may be
    added (v0.1 Conflict 1 / OD-09). ``is_complete`` is set only by an
    explicit merchant action, never inferred from amounts matching.
    """

    __tablename__ = "bank_receipts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    settlement_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    amount_poisha: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_on = mapped_column(Date, nullable=True)
    receipt_ref_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    marked_complete_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    marked_complete_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = _created_at()
    updated_at: Mapped[DateTime] = _updated_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_bank_receipts_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "settlement_batch_id"],
            ["settlement_batches.merchant_id", "settlement_batches.id"],
            ondelete="RESTRICT",
            name="fk_bank_receipts_batch",
        ),
        ForeignKeyConstraint(
            ["marked_complete_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_bank_receipts_marked_complete_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_bank_receipts_merchant_id"),
        # CL-21 supporting key: lets variance inputs reference a receipt as
        # a receipt OF a specific batch.
        UniqueConstraint(
            "merchant_id", "settlement_batch_id", "id",
            name="uq_bank_receipts_merchant_batch_id",
        ),
        CheckConstraint("amount_poisha > 0", name="ck_bank_receipts_amount_positive"),
        CheckConstraint(
            "is_complete IS FALSE OR "
            "(marked_complete_by_user_id IS NOT NULL AND marked_complete_at IS NOT NULL)",
            name="ck_bank_receipts_complete_requires_actor",
        ),
        Index("ix_bank_receipts_batch", "merchant_id", "settlement_batch_id"),
        Index("ix_bank_receipts_received_on", "merchant_id", "received_on"),
    )


class SettlementAllocation(Base):
    """How much of one receipt is allocated to one settlement batch
    (v0.1 C.6, CL-10). Tenant-owned, append-only; a reallocation reverses
    and re-creates.

    CL-10 deleted ``is_active``: an allocation row has ``kind`` allocation
    or reversal; a reversal references the allocation it reverses via
    ``reverses_allocation_id`` (NOT NULL exactly when kind = 'reversal')
    and carries the same positive amount as the original. Net allocation is
    always allocations minus their reversals, computed, never stored as a
    mutable flag. At most one reversal per allocation (partial unique
    index). Over-allocation is enforced in the allocation transaction with
    a row lock on the receipt, backed by the deferred trigger in
    triggers.sql -- a plain CHECK cannot express a cross-row sum (CL-17).
    """

    __tablename__ = "settlement_allocations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    settlement_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    bank_receipt_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    allocated_poisha: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="allocation")
    reverses_allocation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_allocations_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "settlement_batch_id"],
            ["settlement_batches.merchant_id", "settlement_batches.id"],
            ondelete="RESTRICT",
            name="fk_allocations_batch",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "bank_receipt_id"],
            ["bank_receipts.merchant_id", "bank_receipts.id"],
            ondelete="RESTRICT",
            name="fk_allocations_receipt",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "reverses_allocation_id"],
            ["settlement_allocations.merchant_id", "settlement_allocations.id"],
            ondelete="RESTRICT",
            name="fk_allocations_reverses",
        ),
        ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_allocations_created_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_allocations_merchant_id"),
        # CL-21 supporting key: lets variance inputs reference an allocation
        # as an allocation OF a specific batch.
        UniqueConstraint(
            "merchant_id", "settlement_batch_id", "id",
            name="uq_allocations_merchant_batch_id",
        ),
        CheckConstraint("allocated_poisha > 0", name="ck_allocations_amount_positive"),
        CheckConstraint(_check_list("kind", ALLOCATION_KINDS), name="ck_allocations_kind"),
        CheckConstraint(
            "(kind = 'reversal' AND reverses_allocation_id IS NOT NULL) OR "
            "(kind = 'allocation' AND reverses_allocation_id IS NULL)",
            name="ck_allocations_reversal_reference",
        ),
        Index(
            "uq_allocations_one_reversal",
            "reverses_allocation_id",
            unique=True,
            postgresql_where=(kind == "reversal"),
        ),
        Index("ix_allocations_batch", "merchant_id", "settlement_batch_id"),
        Index("ix_allocations_receipt", "merchant_id", "bank_receipt_id"),
    )


class SettlementVarianceCalculation(Base):
    """One computation of bank variance for one settlement batch (CL-16).
    Tenant-owned, append-only. No ``is_current`` flag: current variance is
    DERIVED (CL-16/CL-21) -- the highest-seq calculation whose recorded
    input set exactly equals the batch's current input set and whose
    preconditions still hold; otherwise current variance is NULL.
    ``bank_variance_poisha`` is signed: allocated total minus expected
    payout. A negative variance is a legitimate arithmetic result, never a
    confirmed loss, and is not the OD-12 negative-payout question.
    """

    __tablename__ = "settlement_variance_calculations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    settlement_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    calculation_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_batch_payout_poisha: Mapped[int] = mapped_column(BigInteger, nullable=False)
    allocated_bank_confirmed_total_poisha: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    bank_variance_poisha: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    computed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False, default="system")

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_variance_calcs_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "settlement_batch_id"],
            ["settlement_batches.merchant_id", "settlement_batches.id"],
            ondelete="RESTRICT",
            name="fk_variance_calcs_batch",
        ),
        ForeignKeyConstraint(
            ["computed_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_variance_calcs_computed_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_variance_calcs_merchant_id"),
        UniqueConstraint(
            "merchant_id", "settlement_batch_id", "calculation_seq",
            name="uq_variance_calcs_merchant_batch_seq",
        ),
        # CL-21: lets variance inputs reference a calculation OF a batch.
        UniqueConstraint(
            "merchant_id", "settlement_batch_id", "id",
            name="uq_variance_calcs_merchant_batch_id",
        ),
        CheckConstraint("calculation_seq >= 1", name="ck_variance_calcs_seq"),
        CheckConstraint(
            "bank_variance_poisha = allocated_bank_confirmed_total_poisha "
            "- expected_batch_payout_poisha",
            name="ck_variance_calcs_arithmetic",
        ),
        CheckConstraint(
            _check_list("actor_type", ACTOR_TYPES), name="ck_variance_calcs_actor_type"
        ),
        Index("ix_variance_calcs_batch_seq", "merchant_id", "settlement_batch_id", "calculation_seq"),
    )


class SettlementVarianceCalculationInput(Base):
    """Typed reference set for one variance calculation (CL-16, CL-21).
    Tenant-owned, append-only.

    CL-21: ``settlement_batch_id`` is carried on every input row and the
    receipt/allocation references are batch-scoped composite FKs, so an
    input cannot reference a receipt or allocation of a different batch.
    A receipt with ``settlement_batch_id`` NULL can never be an input
    (it contributes to no variance, v0.1 C.6).
    """

    __tablename__ = "settlement_variance_calculation_inputs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    settlement_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    calculation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    input_kind: Mapped[str] = mapped_column(Text, nullable=False)
    bank_receipt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    settlement_allocation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_variance_inputs_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "settlement_batch_id", "calculation_id"],
            ["settlement_variance_calculations.merchant_id",
             "settlement_variance_calculations.settlement_batch_id",
             "settlement_variance_calculations.id"],
            ondelete="RESTRICT",
            name="fk_variance_inputs_calculation",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "settlement_batch_id", "bank_receipt_id"],
            ["bank_receipts.merchant_id", "bank_receipts.settlement_batch_id",
             "bank_receipts.id"],
            ondelete="RESTRICT",
            name="fk_variance_inputs_receipt",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "settlement_batch_id", "settlement_allocation_id"],
            ["settlement_allocations.merchant_id", "settlement_allocations.settlement_batch_id",
             "settlement_allocations.id"],
            ondelete="RESTRICT",
            name="fk_variance_inputs_allocation",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_variance_inputs_merchant_id"),
        CheckConstraint(
            _check_list("input_kind", INPUT_KINDS), name="ck_variance_inputs_kind"
        ),
        CheckConstraint(
            "(input_kind = 'receipt' AND bank_receipt_id IS NOT NULL "
            "AND settlement_allocation_id IS NULL) OR "
            "(input_kind = 'allocation' AND settlement_allocation_id IS NOT NULL "
            "AND bank_receipt_id IS NULL)",
            name="ck_variance_inputs_exactly_one_reference",
        ),
        Index("ix_variance_inputs_calculation", "merchant_id", "calculation_id"),
    )


# ---------------------------------------------------------------------------
# C.7 Audit, settings and supporting records
# ---------------------------------------------------------------------------


class WorkspaceSettings(Base):
    """Typed per-workspace controls (v0.1 C.7). Tenant-owned, one row per
    merchant. Tolerance is a typed financial control; the gross difference
    is always stored and shown regardless of tolerance. A tolerance change
    affects future runs only (each run copies the tolerance it used).

    NOTE on the money-default rule: ``tolerance_poisha`` carries
    DEFAULT 0 because v0.1 C.7 mandates it. It is a configuration control,
    not a calculated amount; every calculated amount column in this module
    has no default. See the module docstring and README.md.
    """

    __tablename__ = "workspace_settings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    tolerance_poisha: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    locale: Mapped[str] = mapped_column(Text, nullable=False, default="en_bn")
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[DateTime] = _created_at()
    updated_at: Mapped[DateTime] = _updated_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_workspace_settings_merchant",
        ),
        ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_workspace_settings_updated_by",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_workspace_settings_merchant_id"),
        CheckConstraint("tolerance_poisha >= 0", name="ck_workspace_settings_tolerance"),
        CheckConstraint(
            "locale IN ('en', 'en_bn')", name="ck_workspace_settings_locale"
        ),
    )


class AuditLog(Base):
    """Append-only record of consequential actions (v0.1 C.7). Tenant-owned
    where a merchant context exists (``merchant_id`` NULL only for
    pre-workspace identity events such as sign-in).

    ``entity_table`` / ``entity_id`` is the ONE deliberate unenforced
    reference in the design: audit must survive deletion of the rows it
    describes. Compensating rule: ``entity_id`` is recorded with a snapshot
    of the identifying fields in ``detail``. ``detail`` must never contain a
    bank account number, a raw credential or a full uploaded file.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_table: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_audit_logs_merchant",
        ),
        ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="RESTRICT",
            name="fk_audit_logs_actor",
        ),
        CheckConstraint(
            _check_list("actor_type", ACTOR_TYPES), name="ck_audit_logs_actor_type"
        ),
        Index("ix_audit_logs_merchant_created", "merchant_id", "created_at"),
        Index("ix_audit_logs_merchant_action_created", "merchant_id", "action", "created_at"),
        Index("ix_audit_logs_actor_created", "actor_user_id", "created_at"),
    )


class IdempotencyKey(Base):
    """One stored key per retried operation, scoped to a merchant (v0.1 C.7,
    v0.2 CL-04, v0.3 CL-14, v0.4 CL-19, v0.5 CL-24). Tenant-owned. The SOLE
    owner of idempotency (CL-04); import_batches and reconciliation_runs
    carry no key columns.

    Typed result references (CL-04): exactly the reference matching
    ``operation`` is NOT NULL, all others NULL. Two clocks, never
    conflated (CL-14): ``expires_at`` is the replay horizon (24h design
    default); ``lease_expires_at`` is the ownership horizon (lease
    durations: 5 min import_create, 10 min run_create, ADAPT defaults).
    Uniform ownership rule (CL-24): a worker owns a claim only while
    status = in_progress AND claim_token matches AND lease_expires_at is
    later than the database clock at execution. Every worker-initiated
    transition (renewal, completion, worker-reported failure) requires all
    three; see triggers.sql / roles.sql and README.md for the exact
    conditional-update predicates (CL-19/CL-24). Lease comparisons use
    ``clock_timestamp()`` -- NOT ``now()`` -- because ``now()`` is
    transaction-start time and a long work transaction would otherwise see
    a stale clock; see triggers.sql header comment.

    This table is explicitly NOT append-only: the claim/reclaim/completion/
    failure updates are the one sanctioned UPDATE path in the design
    (CL-04/CL-14). Purge is maintenance-role only, after ``expires_at``,
    and only for completed/failed rows -- an in_progress row is never
    purgeable (CL-19).
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    merchant_id: Mapped[uuid.UUID] = _merchant_id()
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="in_progress")
    result_import_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    result_reconciliation_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by_instance: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="RESTRICT",
            name="fk_idempotency_keys_merchant",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "result_import_batch_id"],
            ["import_batches.merchant_id", "import_batches.id"],
            ondelete="RESTRICT",
            name="fk_idempotency_keys_result_batch",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "result_reconciliation_run_id"],
            ["reconciliation_runs.merchant_id", "reconciliation_runs.id"],
            ondelete="RESTRICT",
            name="fk_idempotency_keys_result_run",
        ),
        UniqueConstraint("merchant_id", "id", name="uq_idempotency_keys_merchant_id"),
        UniqueConstraint(
            "merchant_id", "operation", "idempotency_key",
            name="uq_idempotency_keys_merchant_operation_key",
        ),
        CheckConstraint(
            _check_list("operation", IDEMPOTENCY_OPERATIONS),
            name="ck_idempotency_keys_operation",
        ),
        CheckConstraint(
            _check_list("status", IDEMPOTENCY_STATUSES),
            name="ck_idempotency_keys_status",
        ),
        # CL-04 typed-reference XOR, keyed by operation.
        CheckConstraint(
            "(operation = 'import_create' AND result_import_batch_id IS NOT NULL "
            "AND result_reconciliation_run_id IS NULL) OR "
            "(operation = 'run_create' AND result_reconciliation_run_id IS NOT NULL "
            "AND result_import_batch_id IS NULL) OR "
            "(status IN ('in_progress', 'failed') "
            "AND result_import_batch_id IS NULL "
            "AND result_reconciliation_run_id IS NULL)",
            name="ck_idempotency_keys_typed_reference",
        ),
        # CL-14 state-to-column consistency (row-local, so a CHECK is correct).
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND lease_expires_at IS NULL) OR "
            "(status = 'in_progress' AND lease_expires_at IS NOT NULL "
            "AND result_import_batch_id IS NULL "
            "AND result_reconciliation_run_id IS NULL) OR "
            "(status = 'failed' AND lease_expires_at IS NULL "
            "AND result_import_batch_id IS NULL "
            "AND result_reconciliation_run_id IS NULL)",
            name="ck_idempotency_keys_state_columns",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_idempotency_keys_attempt_count"),
        Index(
            "ix_idempotency_keys_merchant_operation_key",
            "merchant_id", "operation", "idempotency_key",
        ),
    )


# ---------------------------------------------------------------------------
# Registry (used by Alembic env.py, tests and the README count check)
# ---------------------------------------------------------------------------

ALL_TABLES: tuple[str, ...] = (
    "users",
    "merchants",
    "merchant_memberships",
    "user_sessions",
    "password_reset_tokens",
    "couriers",
    "merchant_courier_accounts",
    "courier_statement_templates",
    "column_mappings",
    "courier_rule_versions",
    "courier_rule_evidence",
    "orders",
    "shipments",
    "file_objects",
    "import_batches",
    "import_rows",
    "import_errors",
    "statement_lines",
    "shipment_match_candidates",
    "shipment_match_decisions",
    "reconciliation_runs",
    "reconciliation_results",
    "reconciliation_fee_lines",
    "reconciliation_findings",
    "settlement_batches",
    "bank_receipts",
    "settlement_allocations",
    "settlement_variance_calculations",
    "settlement_variance_calculation_inputs",
    "workspace_settings",
    "audit_logs",
    "idempotency_keys",
)

# Tenant-owned tables: every table carrying merchant_id and therefore
# subject to RLS ENABLE + FORCE (see rls.sql). Global tables (couriers,
# courier_statement_templates) and identity tables (users, user_sessions,
# password_reset_tokens) are excluded; audit_logs is tenant-owned where a
# merchant context exists and is included.
TENANT_TABLES: tuple[str, ...] = (
    "merchant_memberships",
    "merchant_courier_accounts",
    "courier_rule_versions",
    "courier_rule_evidence",
    "orders",
    "shipments",
    "file_objects",
    "import_batches",
    "import_rows",
    "import_errors",
    "statement_lines",
    "shipment_match_candidates",
    "shipment_match_decisions",
    "reconciliation_runs",
    "reconciliation_results",
    "reconciliation_fee_lines",
    "reconciliation_findings",
    "settlement_batches",
    "bank_receipts",
    "settlement_allocations",
    "settlement_variance_calculations",
    "settlement_variance_calculation_inputs",
    "workspace_settings",
    "audit_logs",
    "idempotency_keys",
)

# Append-only tables: the runtime role gets INSERT + SELECT only, no UPDATE
# or DELETE policy or grant (v0.1 E, CL-03, CL-10, CL-16). Exceptions are
# documented in roles.sql: reconciliation_runs (status/completed_at only,
# controlled path) and idempotency_keys (sanctioned lifecycle updates).
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "courier_rule_evidence",
    "file_objects",
    "import_rows",
    "import_errors",
    "statement_lines",
    "shipment_match_candidates",
    "shipment_match_decisions",
    "reconciliation_runs",
    "reconciliation_results",
    "reconciliation_fee_lines",
    "reconciliation_findings",
    "settlement_allocations",
    "settlement_variance_calculations",
    "settlement_variance_calculation_inputs",
    "audit_logs",
)

__all__ = [
    "Base",
    "User",
    "Merchant",
    "MerchantMembership",
    "UserSession",
    "PasswordResetToken",
    "Courier",
    "MerchantCourierAccount",
    "CourierStatementTemplate",
    "ColumnMapping",
    "CourierRuleVersion",
    "CourierRuleEvidence",
    "Order",
    "Shipment",
    "FileObject",
    "ImportBatch",
    "ImportRow",
    "ImportError",
    "StatementLine",
    "ShipmentMatchCandidate",
    "ShipmentMatchDecision",
    "ReconciliationRun",
    "ReconciliationResult",
    "ReconciliationFeeLine",
    "ReconciliationFinding",
    "SettlementBatch",
    "BankReceipt",
    "SettlementAllocation",
    "SettlementVarianceCalculation",
    "SettlementVarianceCalculationInput",
    "WorkspaceSettings",
    "AuditLog",
    "IdempotencyKey",
    "ALL_TABLES",
    "TENANT_TABLES",
    "APPEND_ONLY_TABLES",
]
