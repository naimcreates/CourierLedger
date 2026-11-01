"""M6-A1: relax the reconciliation-result identity CHECKs.

The approved schema (0001) required ``v_cond_poisha`` / ``v_total_poisha``
to be NULL whenever any identity term was NULL. The V2.2.1 engine
legitimately emits non-NULL values from partial identities:

  * delivered shipment, blank collected, payout 90000, expected payout
    92000 -> V_total -2000 with C / delta / V_R / S all NULL;
  * blank COD-fee line, collected 95000, payout 87050 -> V_cond 0 with
    S / V_R NULL.

Both are correct ``needs_review`` results; runs containing them failed
at INSERT and rolled back. This amendment drops the NULL-propagation
arm of both CHECKs: the identity is enforced only when all terms are
non-NULL, and no constraint is imposed on the dependent when any term
is NULL. A genuinely violated identity (all terms non-NULL, dependent
!= formula) is still rejected. The engine is unchanged.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

# Relaxed CHECK SQL (same constraint names as 0001).
V_COND_RELAXED = (
    '((s_poisha IS NOT NULL AND v_r_poisha IS NOT NULL '
    'AND v_cond_poisha = (s_poisha - v_r_poisha)) '
    'OR (s_poisha IS NULL OR v_r_poisha IS NULL))'
)
V_TOTAL_RELAXED = (
    '((c_poisha IS NOT NULL AND delta_poisha IS NOT NULL '
    'AND v_r_poisha IS NOT NULL AND s_poisha IS NOT NULL '
    'AND v_total_poisha = (c_poisha - delta_poisha - v_r_poisha + s_poisha)) '
    'OR (c_poisha IS NULL OR delta_poisha IS NULL '
    'OR v_r_poisha IS NULL OR s_poisha IS NULL))'
)

# Original 0001 CHECK SQL (verbatim, for the downgrade).
V_COND_ORIGINAL = (
    '((s_poisha IS NOT NULL AND v_r_poisha IS NOT NULL '
    'AND v_cond_poisha = (s_poisha - v_r_poisha)) '
    'OR (s_poisha IS NULL OR v_r_poisha IS NULL AND v_cond_poisha IS NULL))'
)
V_TOTAL_ORIGINAL = (
    '((c_poisha IS NOT NULL AND delta_poisha IS NOT NULL '
    'AND v_r_poisha IS NOT NULL AND s_poisha IS NOT NULL '
    'AND v_total_poisha = (c_poisha - delta_poisha - v_r_poisha + s_poisha)) '
    'OR (c_poisha IS NULL OR delta_poisha IS NULL '
    'OR v_r_poisha IS NULL OR s_poisha IS NULL AND v_total_poisha IS NULL))'
)


def upgrade():
    op.drop_constraint('ck_results_v_cond_identity', 'reconciliation_results',
                       type_='check')
    op.drop_constraint('ck_results_v_total_identity', 'reconciliation_results',
                       type_='check')
    op.create_check_constraint('ck_results_v_cond_identity',
                               'reconciliation_results', V_COND_RELAXED)
    op.create_check_constraint('ck_results_v_total_identity',
                               'reconciliation_results', V_TOTAL_RELAXED)


def downgrade():
    op.drop_constraint('ck_results_v_cond_identity', 'reconciliation_results',
                       type_='check')
    op.drop_constraint('ck_results_v_total_identity', 'reconciliation_results',
                       type_='check')
    op.create_check_constraint('ck_results_v_cond_identity',
                               'reconciliation_results', V_COND_ORIGINAL)
    op.create_check_constraint('ck_results_v_total_identity',
                               'reconciliation_results', V_TOTAL_ORIGINAL)
