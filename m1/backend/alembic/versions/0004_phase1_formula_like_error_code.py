"""Phase 1 (C-2.3): allow the FORMULA_LIKE_VALUE import-error code.

The CSV importer flags formula-like cells at ingest with this code;
the 0001 CHECK on import_errors.error_code did not include it, so
the flag row was rejected. The code is a warning only — raw values
are still stored verbatim.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

WIDENED = (
    "error_code IN ('COLUMN_ABSENT', 'VALUE_BLANK', 'VALUE_INVALID', "
    "'VALUE_UNPARSABLE', 'DUPLICATE_TRACKING', 'AMBIGUOUS_MATCH', "
    "'FORMULA_LIKE_VALUE')"
)
ORIGINAL = (
    "error_code IN ('COLUMN_ABSENT', 'VALUE_BLANK', 'VALUE_INVALID', "
    "'VALUE_UNPARSABLE', 'DUPLICATE_TRACKING', 'AMBIGUOUS_MATCH')"
)


def upgrade():
    op.drop_constraint('ck_import_errors_error_code', 'import_errors', type_='check')
    op.create_check_constraint('ck_import_errors_error_code', 'import_errors', WIDENED)


def downgrade():
    op.drop_constraint('ck_import_errors_error_code', 'import_errors', type_='check')
    op.create_check_constraint('ck_import_errors_error_code', 'import_errors', ORIGINAL)
