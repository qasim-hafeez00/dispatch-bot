# cortexbot.db package
#
# PHASE 3B: BrokerScore and CarrierScore are now defined in models.py
# alongside all other ORM models. They are still re-exported here so any
# existing code that does:
#     from cortexbot.db import BrokerScore, CarrierScore
# continues to work without modification.
#
# score_models.py still exists for backward compatibility; it imports from
# models.py rather than defining its own copies.

from cortexbot.db.base import Base
from cortexbot.db.models import BrokerScore, CarrierScore

__all__ = ["Base", "BrokerScore", "CarrierScore"]
