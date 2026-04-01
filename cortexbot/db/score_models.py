"""
cortexbot/db/score_models.py — PHASE 3A NEW FILE

PHASE 3A FIX (GAP-10):
Fix: Define BrokerScore and CarrierScore as proper SQLAlchemy ORM
models here. Import them from cortexbot.db.models.
"""

import uuid
from sqlalchemy import Column, String, Integer, Float, Date, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import UUID

# Import Base from models (models.py imports us at the bottom)
from cortexbot.db.models import Base

class BrokerScore(Base):
    __tablename__ = "broker_scores"
    
    score_id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    broker_id           = Column(UUID(as_uuid=True), ForeignKey("brokers.broker_id"), nullable=False)
    score_date          = Column(Date, nullable=False)
    overall_score       = Column(Integer, nullable=False)
    payment_score       = Column(Integer, nullable=False)
    rate_score          = Column(Integer, nullable=False)
    load_quality_score  = Column(Integer, nullable=False)
    comm_score          = Column(Integer, nullable=False)
    dispute_score       = Column(Integer, nullable=False)
    relationship_tier   = Column(String(20), nullable=True)
    avg_days_to_pay     = Column(Float, nullable=True)
    avg_rate_vs_market  = Column(Float, nullable=True)
    loads_last_90d      = Column(Integer, default=0)
    dispute_rate        = Column(Float, default=0.0)

class CarrierScore(Base):
    __tablename__ = "carrier_scores"

    score_id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    carrier_id                = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=False)
    week_ending               = Column(Date, nullable=False)
    overall_score             = Column(Integer, nullable=False)
    revenue_score             = Column(Integer, nullable=False)
    reliability_score         = Column(Integer, nullable=False)
    utilization_score         = Column(Integer, nullable=False)
    compliance_score          = Column(Integer, nullable=False)
    responsiveness_score      = Column(Integer, nullable=False)
    weekly_miles              = Column(Integer, default=0)
    loaded_miles              = Column(Integer, default=0)
    deadhead_miles            = Column(Integer, default=0)
    gross_revenue             = Column(Numeric(10, 2), default=0)
    avg_rpm                   = Column(Float, default=0.0)
    loads_count               = Column(Integer, default=0)
    on_time_pickup_pct        = Column(Float, default=0.0)
    on_time_delivery_pct      = Column(Float, default=0.0)
    check_call_compliance_pct = Column(Float, default=0.0)
    hos_violations            = Column(Integer, default=0)
    detention_hours           = Column(Float, default=0.0)
