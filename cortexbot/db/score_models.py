"""
cortexbot/db/score_models.py — PHASE 3B UPDATED

PHASE 3B FIX (GAP-10):
BrokerScore and CarrierScore have been promoted into models.py.
This file now re-exports them for backward compatibility so any
existing imports of the form:
    from cortexbot.db.score_models import BrokerScore, CarrierScore
continue to work unchanged.

DO NOT define new ORM models here.
"""

# Re-export from the canonical location (models.py)
from cortexbot.db.models import BrokerScore, CarrierScore

__all__ = ["BrokerScore", "CarrierScore"]
