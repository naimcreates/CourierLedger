"""Phase 1 (C-2.2): widen the file_objects content_type CHECK.

The upload endpoint now stores the upload's declared content type
instead of hardcoding 'text/csv'; a declaration that is absent or
is not one of the two CSV/Excel types is stored as 'unknown'. The
CHECK in 0001 only allowed the two CSV/Excel values, so it gains
'unknown' here. The declared type is a client claim, not a verified
fact — the only real gate remains "does it decode as UTF-8 and parse
as CSV".

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

WIDENED = "content_type IN ('text/csv', 'application/vnd.ms-excel', 'unknown')"
ORIGINAL = "content_type IN ('text/csv', 'application/vnd.ms-excel')"


def upgrade():
    op.drop_constraint('ck_file_objects_content_type', 'file_objects', type_='check')
    op.create_check_constraint('ck_file_objects_content_type', 'file_objects', WIDENED)


def downgrade():
    op.drop_constraint('ck_file_objects_content_type', 'file_objects', type_='check')
    op.create_check_constraint('ck_file_objects_content_type', 'file_objects', ORIGINAL)
