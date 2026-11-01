"""Phase 2: add indexes on foreign keys that had no leading index.

- user_sessions.user_id — revoke-all-sessions and prior-token
  invalidation filter on it; previously a sequential scan.
- password_reset_tokens.user_id — same invalidation path.
- import_batches.template_id — join/filter key for statement
  templates; previously unindexed.

Plain CREATE INDEX (not CONCURRENTLY): Alembic runs migrations
inside a single transaction and CONCURRENTLY cannot run inside a
transaction block. On the small tables involved the lock is brief;
on a large production table, prefer creating the index out of band
with CREATE INDEX CONCURRENTLY and recording it here instead.

These indexes were identified by the Phase 2 live index-coverage
review (pg_indexes on courierledger_app, 2026-11-01). They are
additive only: no column, constraint, or policy changes.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        'ix_user_sessions_user_id', 'user_sessions', ['user_id'],
        unique=False,
    )
    op.create_index(
        'ix_password_reset_tokens_user_id', 'password_reset_tokens',
        ['user_id'], unique=False,
    )
    op.create_index(
        'ix_import_batches_template_id', 'import_batches',
        ['template_id'], unique=False,
    )


def downgrade():
    op.drop_index('ix_import_batches_template_id', table_name='import_batches')
    op.drop_index(
        'ix_password_reset_tokens_user_id',
        table_name='password_reset_tokens',
    )
    op.drop_index('ix_user_sessions_user_id', table_name='user_sessions')
