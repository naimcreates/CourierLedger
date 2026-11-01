"""Phase 3 (Workstream 3): add the genuinely missing unique constraint
``uq_memberships_merchant_id`` on ``merchant_memberships (merchant_id, id)``.

Drift inventory (Phase 3, ``alembic check`` on a correctly provisioned
database at head 0006 reported 21 operations): this is the ONLY
genuine schema mismatch among them. The SQLAlchemy model
(``app/db/auth_models.py`` ``MerchantMembership``) declares
``UniqueConstraint("merchant_id", "id", name="uq_memberships_merchant_id")``
— the tenant-parent pattern (v0.1 A.7/R2) every other tenant-owned
table follows — but migration 0001 created ``merchant_memberships``
without it, so the live database never had it.

Why this correction is safe:
- ``id`` is already the primary key, so ``(merchant_id, id)``
  uniqueness is already implied by the data: the constraint can
  never fail on existing rows. Adding it changes enforcement only
  in the sense that the declared pattern is now real (it also lets
  composite foreign keys reference ``(merchant_id, id)`` on this
  table in future, as they do on every other tenant table).
- Additive only: one constraint, no column, index, policy, or
  trigger change; no data movement; no lock beyond the short
  ``ALTER TABLE ... ADD CONSTRAINT`` (a unique-index build on the
  PK-ordered column set).
- Downgrade drops the constraint again.

Deliberately NOT in this migration (see MIGRATION_VERIFICATION.md
§3 for the full inventory and evidence): the six foreign keys whose
live ``ON DELETE`` is NO ACTION while the models declare RESTRICT
(behaviourally identical for these non-deferrable constraints —
recreating them would be DDL churn with exclusive locks for zero
behavioural change), the redundant ``UNIQUE (id)`` on
``courier_statement_templates`` (already enforced by the primary
key, whose live name is ``uq_templates_id``), and the three 0006
indexes / RLS-layer index (handled by model metadata alignment and
the documented allow-list, not by DDL).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        'uq_memberships_merchant_id',
        'merchant_memberships',
        ['merchant_id', 'id'],
    )


def downgrade():
    op.drop_constraint(
        'uq_memberships_merchant_id',
        'merchant_memberships',
        type_='unique',
    )
