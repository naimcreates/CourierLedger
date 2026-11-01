"""CourierLedger M1 backend — database package.

Identity/tenancy tables (users, merchants, merchant_memberships,
user_sessions, password_reset_tokens) are defined in
app/db/auth_models.py (auth agent) and REUSED by app/db/models.py,
which imports them onto the same shared Base -- so Base.metadata holds
all 32 M1 tables and is the single Alembic target. Do not duplicate
those definitions. See app/db/README.md for the reuse decision and the
known deviations of the auth definitions from v0.1 section C.
"""

from app.db.auth_models import Base  # noqa: F401  (re-exported for init_db)
from app.db.models import ALL_TABLES, APPEND_ONLY_TABLES, TENANT_TABLES  # noqa: F401

__all__ = ["ALL_TABLES", "APPEND_ONLY_TABLES", "Base", "TENANT_TABLES"]
