"""Phase 1.1 (B-F11): step-up authentication support.

Adds ``step_up_expires_at`` to ``user_sessions``: the timestamp until
which the session counts as freshly step-up-verified (a password
re-check via POST /api/auth/step-up). Sensitive actions (owner
transfer) require either a live step-up verification or a session
created within ``step_up_fresh_login_seconds``. NULL means no step-up
verification has ever been recorded for the session.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'user_sessions',
        sa.Column('step_up_expires_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column('user_sessions', 'step_up_expires_at')
