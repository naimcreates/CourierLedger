"""M1 domain package: the pure-Python COD reconciliation engine.

No FastAPI, SQLAlchemy, CSV, or database imports here — see
app/domain/engine.py and app/domain/README.md.
"""
from app.domain.engine import FIXTURES, reconcile_batch, reconcile_shipment

__all__ = ["FIXTURES", "reconcile_batch", "reconcile_shipment"]
