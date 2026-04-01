"""
cortexbot/db/models.py — PHASE 3B UPDATED

PHASE 3B CHANGES:

1. BrokerScore / CarrierScore promoted from score_models.py into this file.
   They are still re-exported from cortexbot.db (package __init__.py) for
   backward compatibility, but now also importable directly from here:
       from cortexbot.db.models import BrokerScore, CarrierScore

2. Carrier.stripe_account_id added as an explicit column alongside the
   Phase 2 stripe_connected_account_id.  Both now exist.  stripe_client.py
   and orchestrator.py use stripe_account_id; the Phase 2 column is kept
   for any code that already references the longer name.

3. Load model: added amount_paid + payment_received_date as proper columns
   (they existed in migration 002 but were never declared in the ORM model,
   causing AttributeError in api/loads.py → HTTP 500s).

4. Carrier.eld_driver_id column added (was in migration 002 but absent here).

5. Load: detention columns declared with canonical _hours names.
   Backward-compat @property aliases for _hrs kept unchanged.

6. LoadExpense model added (Skill U expense table).

PHASE 3A FIXES (kept from prior version):
   - detention_pickup_hours / detention_delivery_hours naming fix (GAP-09)
   - amount_paid, payment_received_date added to Load (GAP-09)
   - Base imported from cortexbot.db.base (avoids circular import)
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Boolean, Float, Text,
    DateTime, Date, Time, ForeignKey, Index,
    Numeric, CHAR, ARRAY, BigInteger
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from cortexbot.db.base import Base


# ============================================================
# CARRIER
# ============================================================

class Carrier(Base):
    __tablename__ = "carriers"

    carrier_id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mc_number          = Column(String(20), unique=True, nullable=False)
    dot_number         = Column(String(20), nullable=True)
    company_name       = Column(String(200), nullable=False)
    owner_name         = Column(String(200), nullable=False)
    owner_email        = Column(String(200), nullable=False)
    owner_phone        = Column(String(30), nullable=False)
    driver_phone       = Column(String(30), nullable=True)
    whatsapp_phone     = Column(String(30), nullable=True)
    language_pref      = Column(String(10), default="en")
    equipment_type     = Column(String(50), nullable=False)
    max_weight_lbs     = Column(Integer, default=44000)
    home_base_city     = Column(String(100), nullable=True)
    home_base_state    = Column(CHAR(2), nullable=True)
    preferred_dest_states = Column(ARRAY(String), nullable=True)
    avoid_states       = Column(ARRAY(String), nullable=True)
    rate_floor_cpm     = Column(Numeric(5, 3), nullable=False)
    max_deadhead_mi    = Column(Integer, default=100)
    no_touch_only      = Column(Boolean, default=False)
    hazmat_cert        = Column(Boolean, default=False)
    twic_card          = Column(Boolean, default=False)
    status             = Column(String(20), default="ACTIVE")
    factoring_company  = Column(String(100), nullable=True)
    dispatch_fee_pct   = Column(Numeric(4, 3), default=0.060)
    factoring_noa_url  = Column(Text, nullable=True)
    w9_url             = Column(Text, nullable=True)
    coi_url            = Column(Text, nullable=True)

    # ── Phase 2 ELD ──────────────────────────────────────────
    eld_provider       = Column(String(30), nullable=True)
    eld_vehicle_id     = Column(String(100), nullable=True)
    # PHASE 3B FIX: eld_driver_id was in migration 002 but absent from ORM
    eld_driver_id      = Column(String(100), nullable=True)

    # ── Phase 2 Financial ─────────────────────────────────────
    # PHASE 3B FIX: stripe_account_id is the canonical name used by
    # stripe_client.py verify_carrier_bank() and orchestrator.py.
    # stripe_connected_account_id is the longer name from migration 001.
    # Both columns exist in DB; both declared here.
    stripe_account_id           = Column(String(100), nullable=True)
    stripe_connected_account_id = Column(String(100), nullable=True)
    bank_account_last4          = Column(String(4), nullable=True)
    truck_weight_lbs            = Column(Integer, default=17000)
    trailer_weight_lbs          = Column(Integer, default=13000)
    truck_mpg                   = Column(Numeric(4, 1), default=6.5)
    fuel_card_network           = Column(String(30), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    loads       = relationship("Load",            back_populates="carrier")
    settlements = relationship("DriverSettlement", back_populates="carrier")
    advances    = relationship("DriverAdvance",    back_populates="carrier")
    expenses    = relationship("LoadExpense",       back_populates="carrier")

    def __repr__(self):
        return f"<Carrier {self.mc_number} — {self.company_name}>"


# ============================================================
# BROKER
# ============================================================

class Broker(Base):
    __tablename__ = "brokers"

    broker_id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mc_number          = Column(String(20), unique=True, nullable=False)
    company_name       = Column(String(200), nullable=False)
    dat_credit_score   = Column(Integer, nullable=True)
    avg_days_to_pay    = Column(Integer, nullable=True)
    relationship_tier  = Column(String(20), default="ACTIVE")
    blacklisted        = Column(Boolean, default=False)
    blacklist_reason   = Column(Text, nullable=True)
    loads_booked       = Column(Integer, default=0)
    ap_email           = Column(String(200), nullable=True)
    ap_phone           = Column(String(30), nullable=True)
    ops_manager_email  = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    contacts = relationship("BrokerContact", back_populates="broker")
    loads    = relationship("Load",          back_populates="broker")
    invoices = relationship("Invoice",       back_populates="broker")

    def __repr__(self):
        return f"<Broker {self.mc_number} — {self.company_name}>"


class BrokerContact(Base):
    __tablename__ = "broker_contacts"

    contact_id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    broker_id       = Column(UUID(as_uuid=True), ForeignKey("brokers.broker_id"), nullable=False)
    name            = Column(String(200), nullable=True)
    phone           = Column(String(30), nullable=True)
    email           = Column(String(200), nullable=True)
    best_lanes      = Column(ARRAY(String), nullable=True)
    equipment_focus = Column(String(50), nullable=True)
    notes           = Column(Text, nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    broker = relationship("Broker", back_populates="contacts")


# ============================================================
# LOAD
# ============================================================

class Load(Base):
    __tablename__ = "loads"

    load_id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tms_ref           = Column(String(50), unique=True, nullable=True)
    carrier_id        = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=True)
    broker_id         = Column(UUID(as_uuid=True), ForeignKey("brokers.broker_id"), nullable=True)
    broker_contact_id = Column(UUID(as_uuid=True), ForeignKey("broker_contacts.contact_id"), nullable=True)
    driver_phone      = Column(String(30), nullable=True)

    status            = Column(String(50), nullable=False, default="SEARCHING")
    broker_load_ref   = Column(String(100), nullable=True)
    dat_load_id       = Column(String(100), nullable=True)
    bland_call_id     = Column(String(100), nullable=True)

    # Route
    origin_address    = Column(Text, nullable=True)
    origin_city       = Column(String(100), nullable=True)
    origin_state      = Column(CHAR(2), nullable=True)
    origin_zip        = Column(String(10), nullable=True)
    origin_lat        = Column(Numeric(9, 6), nullable=True)
    origin_lng        = Column(Numeric(9, 6), nullable=True)
    destination_address = Column(Text, nullable=True)
    destination_city  = Column(String(100), nullable=True)
    destination_state = Column(CHAR(2), nullable=True)
    destination_zip   = Column(String(10), nullable=True)
    destination_lat   = Column(Numeric(9, 6), nullable=True)
    destination_lng   = Column(Numeric(9, 6), nullable=True)
    loaded_miles      = Column(Integer, nullable=True)
    deadhead_miles    = Column(Integer, nullable=True)

    # Pickup
    pickup_date       = Column(Date, nullable=True)
    pickup_appt_type  = Column(String(10), nullable=True)
    pickup_appt_time  = Column(Time, nullable=True)
    pickup_appt_open  = Column(Time, nullable=True)
    pickup_appt_close = Column(Time, nullable=True)

    # Delivery
    delivery_date      = Column(Date, nullable=True)
    delivery_appt_type = Column(String(10), nullable=True)
    delivery_appt_time = Column(Time, nullable=True)

    # Load specs
    commodity         = Column(String(200), nullable=True)
    weight_lbs        = Column(Integer, nullable=True)
    piece_count       = Column(Integer, nullable=True)
    equipment_type    = Column(String(50), nullable=True)
    load_type         = Column(String(30), nullable=True)
    unload_type       = Column(String(30), nullable=True)
    temp_min_f        = Column(Numeric(5, 1), nullable=True)
    temp_max_f        = Column(Numeric(5, 1), nullable=True)
    driver_assist     = Column(Boolean, default=False)
    lumper_required   = Column(Boolean, default=False)
    lumper_payer      = Column(String(20), nullable=True)

    # Rate
    agreed_rate_cpm   = Column(Numeric(5, 3), nullable=True)
    agreed_rate_flat  = Column(Numeric(10, 2), nullable=True)
    fuel_surcharge_included = Column(Boolean, default=True)

    # Accessorials
    detention_free_hrs = Column(Integer, default=2)
    detention_rate_hr  = Column(Numeric(7, 2), nullable=True)
    tonu_amount        = Column(Numeric(7, 2), nullable=True)
    layover_rate       = Column(Numeric(7, 2), nullable=True)
    extra_stop_rate    = Column(Numeric(7, 2), nullable=True)

    # Tracking
    tracking_method    = Column(String(50), nullable=True)
    tracking_id        = Column(String(100), nullable=True)

    # Payment terms
    payment_terms_days = Column(Integer, nullable=True)
    quick_pay_pct      = Column(Numeric(4, 3), nullable=True)
    quick_pay_days     = Column(Integer, nullable=True)
    factoring_allowed  = Column(Boolean, default=True)

    # Documents
    rc_url             = Column(Text, nullable=True)
    rc_signed_url      = Column(Text, nullable=True)
    carrier_packet_url = Column(Text, nullable=True)

    # Negotiation context
    market_rate_cpm    = Column(Numeric(5, 3), nullable=True)
    anchor_rate_cpm    = Column(Numeric(5, 3), nullable=True)
    call_recording_url = Column(Text, nullable=True)
    extracted_call_data = Column(JSONB, nullable=True)

    # ── Phase 2: Transit tracking ─────────────────────────────
    eld_provider          = Column(String(30), nullable=True)
    last_gps_lat          = Column(Numeric(9, 6), nullable=True)
    last_gps_lng          = Column(Numeric(9, 6), nullable=True)
    last_gps_speed_mph    = Column(Numeric(5, 1), nullable=True)
    last_gps_updated      = Column(DateTime(timezone=True), nullable=True)
    current_eta           = Column(DateTime(timezone=True), nullable=True)
    delay_minutes         = Column(Integer, nullable=True)
    broker_delay_notified = Column(Boolean, default=False)

    # Transit milestones
    arrived_pickup_at    = Column(DateTime(timezone=True), nullable=True)
    loaded_at            = Column(DateTime(timezone=True), nullable=True)
    departed_pickup_at   = Column(DateTime(timezone=True), nullable=True)
    arrived_delivery_at  = Column(DateTime(timezone=True), nullable=True)
    delivered_at         = Column(DateTime(timezone=True), nullable=True)

    # ── Phase 2+3: Detention (canonical _hours names) ─────────
    # GAP-09 FIX: previously named _hrs; skill 27 expects _hours.
    detention_pickup_hours    = Column(Numeric(5, 2), nullable=True)
    detention_pickup_amount   = Column(Numeric(7, 2), nullable=True)
    detention_delivery_hours  = Column(Numeric(5, 2), nullable=True)
    detention_delivery_amount = Column(Numeric(7, 2), nullable=True)
    tonu_triggered            = Column(Boolean, default=False)
    tonu_claimed_amount       = Column(Numeric(7, 2), nullable=True)

    # Lumper
    lumper_actual_amount = Column(Numeric(7, 2), nullable=True)
    lumper_receipt_url   = Column(Text, nullable=True)

    # POD documents
    bol_pickup_url    = Column(Text, nullable=True)
    bol_delivery_url  = Column(Text, nullable=True)
    pod_url           = Column(Text, nullable=True)
    pod_collected_at  = Column(DateTime(timezone=True), nullable=True)

    # ── Phase 2+3: Financial summary ──────────────────────────
    # PHASE 3B FIX: amount_paid and payment_received_date were added by
    # migration 002 but never declared in the ORM model → AttributeError
    # in api/loads.py → HTTP 500 on every load detail request.
    amount_paid              = Column(Numeric(10, 2), nullable=True)
    payment_received_date    = Column(Date, nullable=True)
    gross_revenue            = Column(Numeric(10, 2), nullable=True)
    total_accessorials       = Column(Numeric(10, 2), nullable=True)
    total_invoice_amount     = Column(Numeric(10, 2), nullable=True)
    dispatch_fee_amount      = Column(Numeric(8, 2), nullable=True)
    driver_settlement_amount = Column(Numeric(10, 2), nullable=True)

    # FK references to settlement/invoice (populated after Phase 2)
    invoice_id    = Column(UUID(as_uuid=True), nullable=True)
    settlement_id = Column(UUID(as_uuid=True), nullable=True)

    # Timestamps
    searched_at          = Column(DateTime(timezone=True), nullable=True)
    broker_called_at     = Column(DateTime(timezone=True), nullable=True)
    rate_agreed_at       = Column(DateTime(timezone=True), nullable=True)
    carrier_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    booked_at            = Column(DateTime(timezone=True), nullable=True)
    rc_received_at       = Column(DateTime(timezone=True), nullable=True)
    rc_signed_at         = Column(DateTime(timezone=True), nullable=True)
    dispatched_at        = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    carrier           = relationship("Carrier",         back_populates="loads")
    broker            = relationship("Broker",          back_populates="loads")
    invoices          = relationship("Invoice",         back_populates="load")
    detention_records = relationship("DetentionRecord", back_populates="load")
    check_calls       = relationship("CheckCall",       back_populates="load")
    transit_events    = relationship("TransitEvent",    back_populates="load")
    weather_alerts    = relationship("WeatherAlert",    back_populates="load")
    expenses          = relationship("LoadExpense",      back_populates="load")

    # ── Backward-compat @property aliases ─────────────────────
    # Code that used the old _hrs spelling continues to work unchanged.
    @property
    def detention_pickup_hrs(self):
        return self.detention_pickup_hours

    @detention_pickup_hrs.setter
    def detention_pickup_hrs(self, value):
        self.detention_pickup_hours = value

    @property
    def detention_delivery_hrs(self):
        return self.detention_delivery_hours

    @detention_delivery_hrs.setter
    def detention_delivery_hrs(self, value):
        self.detention_delivery_hours = value

    def __repr__(self):
        return f"<Load {self.tms_ref} — {self.status}>"


# ============================================================
# EVENT (audit log)
# ============================================================

class Event(Base):
    __tablename__ = "events"

    event_id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_code      = Column(String(60), nullable=False)
    entity_type     = Column(String(20), nullable=False)
    entity_id       = Column(UUID(as_uuid=True), nullable=False)
    triggered_by    = Column(String(50), nullable=True)
    actor           = Column(String(50), default="cortex-bot")
    data            = Column(JSONB, default={})
    previous_status = Column(String(50), nullable=True)
    new_status      = Column(String(50), nullable=True)
    notes           = Column(Text, nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_events_entity",  "entity_type", "entity_id"),
        Index("idx_events_code",    "event_code"),
        Index("idx_events_created", "created_at"),
    )


# ============================================================
# MISC CORE TABLES
# ============================================================

class LoadCheckpoint(Base):
    __tablename__ = "load_checkpoints"

    load_id        = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), primary_key=True)
    state_json     = Column(JSONB, nullable=False)
    current_skill  = Column(String(60), nullable=True)
    checkpoint_seq = Column(Integer, default=0)
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class InboundEmail(Base):
    __tablename__ = "inbound_emails"

    email_id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id          = Column(String(200), unique=True, nullable=True)
    from_email          = Column(String(200), nullable=True)
    to_email            = Column(String(200), nullable=True)
    subject             = Column(Text, nullable=True)
    body_text           = Column(Text, nullable=True)
    body_html           = Column(Text, nullable=True)
    has_attachment      = Column(Boolean, default=False)
    attachment_s3_url   = Column(Text, nullable=True)
    attachment_filename = Column(String(200), nullable=True)
    category            = Column(String(30), nullable=True)
    confidence          = Column(Numeric(4, 3), nullable=True)
    load_id             = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=True)
    carrier_id          = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=True)
    processed           = Column(Boolean, default=False)
    processed_at        = Column(DateTime(timezone=True), nullable=True)
    created_at          = Column(DateTime(timezone=True), server_default=func.now())


class CallLog(Base):
    __tablename__ = "call_log"

    call_id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bland_ai_call_id  = Column(String(100), unique=True, nullable=True)
    load_id           = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=True)
    carrier_id        = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=True)
    broker_phone      = Column(String(30), nullable=True)
    outcome           = Column(String(30), nullable=True)
    agreed_rate_cpm   = Column(Numeric(5, 3), nullable=True)
    call_duration_sec = Column(Integer, nullable=True)
    recording_url     = Column(Text, nullable=True)
    transcript_raw    = Column(Text, nullable=True)
    extracted_data    = Column(JSONB, nullable=True)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())


class WhatsAppContext(Base):
    __tablename__ = "whatsapp_context"

    phone             = Column(String(30), primary_key=True)
    carrier_id        = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=True)
    current_load_id   = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=True)
    awaiting          = Column(String(50), nullable=True)
    language          = Column(String(10), default="en")
    conversation_json = Column(JSONB, default=list)
    last_message_at   = Column(DateTime(timezone=True), nullable=True)
    updated_at        = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ============================================================
# LOAD EXPENSES (Phase 3B — Skill U)
# ============================================================

class LoadExpense(Base):
    __tablename__ = "load_expenses"

    expense_id     = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    load_id        = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=False)
    carrier_id     = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=False)
    expense_type   = Column(String(30), nullable=False)
    category_label = Column(String(100), nullable=True)
    amount         = Column(Numeric(10, 2), nullable=False)
    description    = Column(Text, nullable=True)
    receipt_url    = Column(Text, nullable=True)
    reference      = Column(String(100), nullable=True)
    recorded_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    load    = relationship("Load",    back_populates="expenses")
    carrier = relationship("Carrier", back_populates="expenses")

    __table_args__ = (
        Index("idx_expenses_load",    "load_id"),
        Index("idx_expenses_carrier", "carrier_id"),
    )


# ============================================================
# PHASE 2 TABLES
# ============================================================

class TransitEvent(Base):
    __tablename__ = "transit_events"

    event_id     = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    load_id      = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=False)
    carrier_id   = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=True)
    event_type   = Column(String(50), nullable=False)
    lat          = Column(Numeric(9, 6), nullable=True)
    lng          = Column(Numeric(9, 6), nullable=True)
    speed_mph    = Column(Numeric(5, 1), nullable=True)
    heading      = Column(Integer, nullable=True)
    odometer     = Column(Integer, nullable=True)
    hos_drive_remaining  = Column(Numeric(4, 2), nullable=True)
    hos_window_remaining = Column(Numeric(4, 2), nullable=True)
    eld_provider = Column(String(30), nullable=True)
    raw_eld_data = Column(JSONB, nullable=True)
    notes        = Column(Text, nullable=True)
    event_ts     = Column(DateTime(timezone=True), nullable=False)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    load = relationship("Load", back_populates="transit_events")

    __table_args__ = (
        Index("idx_transit_load_ts", "load_id", "event_ts"),
        Index("idx_transit_type",    "event_type"),
    )


class CheckCall(Base):
    __tablename__ = "check_calls"

    checkcall_id    = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    load_id         = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=False)
    sequence        = Column(Integer, nullable=False)
    scheduled_at    = Column(DateTime(timezone=True), nullable=False)
    sent_at         = Column(DateTime(timezone=True), nullable=True)
    responded_at    = Column(DateTime(timezone=True), nullable=True)
    status          = Column(String(20), default="PENDING")
    driver_response = Column(Text, nullable=True)
    driver_location = Column(String(200), nullable=True)
    driver_eta      = Column(String(100), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    load = relationship("Load", back_populates="check_calls")

    __table_args__ = (
        Index("idx_checkcall_load_seq", "load_id", "sequence"),
        Index("idx_checkcall_status",   "status"),
    )


class DetentionRecord(Base):
    __tablename__ = "detention_records"

    detention_id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    load_id           = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=False)
    stop_type         = Column(String(20), nullable=False)
    facility_name     = Column(String(200), nullable=True)
    facility_address  = Column(Text, nullable=True)
    arrival_ts        = Column(DateTime(timezone=True), nullable=False)
    departure_ts      = Column(DateTime(timezone=True), nullable=True)
    total_hours       = Column(Numeric(5, 2), nullable=True)
    free_hours        = Column(Integer, default=2)
    billable_hours    = Column(Numeric(5, 2), nullable=True)
    hourly_rate       = Column(Numeric(7, 2), nullable=True)
    total_amount      = Column(Numeric(8, 2), nullable=True)
    bol_times_noted   = Column(Boolean, default=False)
    broker_pre_alerted = Column(Boolean, default=False)
    broker_alerted_at = Column(DateTime(timezone=True), nullable=True)
    bol_in_time       = Column(String(20), nullable=True)
    bol_out_time      = Column(String(20), nullable=True)
    status            = Column(String(20), default="TRACKING")
    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    updated_at        = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    load = relationship("Load", back_populates="detention_records")

    __table_args__ = (
        Index("idx_detention_load",   "load_id"),
        Index("idx_detention_status", "status"),
    )


class Invoice(Base):
    __tablename__ = "invoices"

    invoice_id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_number    = Column(String(50), unique=True, nullable=False)
    load_id           = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=False)
    carrier_id        = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=False)
    broker_id         = Column(UUID(as_uuid=True), ForeignKey("brokers.broker_id"), nullable=True)
    linehaul_amount   = Column(Numeric(10, 2), nullable=False)
    detention_amount  = Column(Numeric(8, 2), default=0)
    lumper_amount     = Column(Numeric(8, 2), default=0)
    tonu_amount       = Column(Numeric(7, 2), default=0)
    extra_stop_amount = Column(Numeric(7, 2), default=0)
    driver_assist_amount = Column(Numeric(7, 2), default=0)
    fuel_surcharge    = Column(Numeric(8, 2), default=0)
    total_amount      = Column(Numeric(10, 2), nullable=False)
    payment_terms_days = Column(Integer, default=30)
    due_date          = Column(Date, nullable=False)
    quick_pay_pct     = Column(Numeric(4, 3), nullable=True)
    status            = Column(String(30), default="GENERATED")
    factoring_used    = Column(Boolean, default=False)
    factoring_company = Column(String(100), nullable=True)
    factoring_submission_id = Column(String(100), nullable=True)
    factoring_advance_amount = Column(Numeric(10, 2), nullable=True)
    factoring_advanced_at   = Column(DateTime(timezone=True), nullable=True)
    invoice_pdf_url   = Column(Text, nullable=True)
    submitted_to_email = Column(String(200), nullable=True)
    amount_paid       = Column(Numeric(10, 2), nullable=True)
    paid_at           = Column(DateTime(timezone=True), nullable=True)
    payment_variance  = Column(Numeric(8, 2), nullable=True)
    last_reminder_sent = Column(DateTime(timezone=True), nullable=True)
    reminder_count    = Column(Integer, default=0)
    dispute_reason    = Column(Text, nullable=True)
    dispute_opened_at = Column(DateTime(timezone=True), nullable=True)
    dispute_resolved_at = Column(DateTime(timezone=True), nullable=True)
    collections_referred_at = Column(DateTime(timezone=True), nullable=True)
    generated_at      = Column(DateTime(timezone=True), server_default=func.now())
    submitted_at      = Column(DateTime(timezone=True), nullable=True)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    updated_at        = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    load       = relationship("Load",            back_populates="invoices")
    carrier    = relationship("Carrier")
    broker     = relationship("Broker",          back_populates="invoices")
    line_items = relationship("InvoiceLineItem", back_populates="invoice")
    payments   = relationship("Payment",         back_populates="invoice")

    __table_args__ = (
        Index("idx_invoice_load",   "load_id"),
        Index("idx_invoice_status", "status"),
        Index("idx_invoice_due",    "due_date"),
    )


class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_items"

    item_id     = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id  = Column(UUID(as_uuid=True), ForeignKey("invoices.invoice_id"), nullable=False)
    item_type   = Column(String(50), nullable=False)
    description = Column(String(300), nullable=True)
    quantity    = Column(Numeric(7, 3), nullable=True)
    unit        = Column(String(20), nullable=True)
    unit_rate   = Column(Numeric(8, 2), nullable=True)
    amount      = Column(Numeric(10, 2), nullable=False)
    documented  = Column(Boolean, default=True)
    proof_url   = Column(Text, nullable=True)

    invoice = relationship("Invoice", back_populates="line_items")


class Payment(Base):
    __tablename__ = "payments"

    payment_id     = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id     = Column(UUID(as_uuid=True), ForeignKey("invoices.invoice_id"), nullable=False)
    load_id        = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=True)
    carrier_id     = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=True)
    amount         = Column(Numeric(10, 2), nullable=False)
    payment_method = Column(String(30), nullable=True)
    reference      = Column(String(200), nullable=True)
    payment_date   = Column(Date, nullable=True)
    received_at    = Column(DateTime(timezone=True), nullable=True)
    status         = Column(String(20), default="RECEIVED")
    notes          = Column(Text, nullable=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    invoice = relationship("Invoice", back_populates="payments")

    __table_args__ = (Index("idx_payment_invoice", "invoice_id"),)


class DriverSettlement(Base):
    __tablename__ = "driver_settlements"

    settlement_id     = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    load_id           = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=False)
    carrier_id        = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=False)
    invoice_id        = Column(UUID(as_uuid=True), ForeignKey("invoices.invoice_id"), nullable=True)
    gross_revenue     = Column(Numeric(10, 2), nullable=False)
    dispatch_fee      = Column(Numeric(8, 2), nullable=False)
    fuel_advance_deduction   = Column(Numeric(8, 2), default=0)
    lumper_advance_deduction = Column(Numeric(7, 2), default=0)
    repair_advance_deduction = Column(Numeric(7, 2), default=0)
    other_deductions  = Column(Numeric(7, 2), default=0)
    total_deductions  = Column(Numeric(10, 2), nullable=False)
    net_settlement    = Column(Numeric(10, 2), nullable=False)
    payment_method    = Column(String(30), nullable=True)
    stripe_transfer_id = Column(String(100), nullable=True)
    bank_last4        = Column(String(4), nullable=True)
    status            = Column(String(20), default="CALCULATED")
    settlement_pdf_url = Column(Text, nullable=True)
    calculated_at     = Column(DateTime(timezone=True), server_default=func.now())
    approved_at       = Column(DateTime(timezone=True), nullable=True)
    paid_at           = Column(DateTime(timezone=True), nullable=True)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())

    carrier = relationship("Carrier", back_populates="settlements")

    __table_args__ = (
        Index("idx_settlement_load",    "load_id"),
        Index("idx_settlement_carrier", "carrier_id"),
        Index("idx_settlement_status",  "status"),
    )


class DriverAdvance(Base):
    __tablename__ = "driver_advances"

    advance_id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    carrier_id      = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=False)
    load_id         = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=True)
    advance_type    = Column(String(20), nullable=False)
    amount          = Column(Numeric(8, 2), nullable=False)
    network         = Column(String(20), nullable=True)
    check_code      = Column(String(50), nullable=True)
    code_expires_at = Column(DateTime(timezone=True), nullable=True)
    status          = Column(String(20), default="ISSUED")
    reason          = Column(String(200), nullable=True)
    redeemed_at     = Column(DateTime(timezone=True), nullable=True)
    settlement_deducted = Column(Boolean, default=False)
    settlement_id   = Column(UUID(as_uuid=True),
                             ForeignKey("driver_settlements.settlement_id"), nullable=True)
    issued_at       = Column(DateTime(timezone=True), server_default=func.now())
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    carrier = relationship("Carrier", back_populates="advances")

    __table_args__ = (
        Index("idx_advance_carrier", "carrier_id"),
        Index("idx_advance_load",    "load_id"),
        Index("idx_advance_status",  "status"),
    )


class WeatherAlert(Base):
    __tablename__ = "weather_alerts"

    alert_id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    load_id       = Column(UUID(as_uuid=True), ForeignKey("loads.load_id"), nullable=False)
    severity      = Column(String(20), nullable=False)
    alert_type    = Column(String(100), nullable=True)
    description   = Column(Text, nullable=True)
    affected_area = Column(String(200), nullable=True)
    route_segment = Column(String(200), nullable=True)
    start_time    = Column(DateTime(timezone=True), nullable=True)
    end_time      = Column(DateTime(timezone=True), nullable=True)
    driver_impact = Column(Text, nullable=True)
    driver_alerted    = Column(Boolean, default=False)
    driver_alerted_at = Column(DateTime(timezone=True), nullable=True)
    broker_notified   = Column(Boolean, default=False)
    broker_notified_at = Column(DateTime(timezone=True), nullable=True)
    action_taken      = Column(String(50), nullable=True)
    force_majeure_documented = Column(Boolean, default=False)
    documentation_url = Column(Text, nullable=True)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())

    load = relationship("Load", back_populates="weather_alerts")

    __table_args__ = (
        Index("idx_weather_load",     "load_id"),
        Index("idx_weather_severity", "severity"),
    )


class QuickbooksSyncLog(Base):
    __tablename__ = "quickbooks_sync_log"

    sync_id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type     = Column(String(30), nullable=False)
    entity_id       = Column(UUID(as_uuid=True), nullable=False)
    qbo_entity_type = Column(String(30), nullable=True)
    qbo_entity_id   = Column(String(50), nullable=True)
    event_type      = Column(String(30), nullable=False)
    status          = Column(String(20), default="PENDING")
    error_message   = Column(Text, nullable=True)
    synced_at       = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_qb_entity", "entity_type", "entity_id"),
        Index("idx_qb_status", "status"),
    )


# ============================================================
# SCORING TABLES (Phase 3B — promoted from score_models.py)
# ============================================================
# These are still exported from cortexbot.db (package __init__) for
# backward compatibility. They can now also be imported directly:
#   from cortexbot.db.models import BrokerScore, CarrierScore


class BrokerScore(Base):
    """Weekly broker relationship score snapshot."""
    __tablename__ = "broker_scores"

    score_id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    broker_id          = Column(UUID(as_uuid=True), ForeignKey("brokers.broker_id"), nullable=False)
    score_date         = Column(Date, nullable=False)
    overall_score      = Column(Integer, nullable=False)
    payment_score      = Column(Integer, default=0)
    rate_score         = Column(Integer, default=0)
    load_quality_score = Column(Integer, default=0)
    comm_score         = Column(Integer, default=0)
    dispute_score      = Column(Integer, default=0)
    relationship_tier  = Column(String(20), nullable=True)
    avg_days_to_pay    = Column(Numeric(5, 1), nullable=True)
    avg_rate_vs_market = Column(Numeric(4, 3), nullable=True)
    loads_last_90d     = Column(Integer, default=0)
    dispute_rate       = Column(Numeric(4, 3), default=0)
    created_at         = Column(DateTime(timezone=True), server_default=func.now())

    broker = relationship("Broker")

    __table_args__ = (
        Index("idx_broker_scores_broker", "broker_id", "score_date"),
    )


class CarrierScore(Base):
    """Weekly carrier KPI score snapshot."""
    __tablename__ = "carrier_scores"

    score_id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    carrier_id                = Column(UUID(as_uuid=True), ForeignKey("carriers.carrier_id"), nullable=False)
    week_ending               = Column(Date, nullable=False)
    overall_score             = Column(Integer, nullable=False)
    revenue_score             = Column(Integer, default=0)
    reliability_score         = Column(Integer, default=0)
    utilization_score         = Column(Integer, default=0)
    compliance_score          = Column(Integer, default=0)
    responsiveness_score      = Column(Integer, default=0)
    weekly_miles              = Column(Integer, default=0)
    loaded_miles              = Column(Integer, default=0)
    deadhead_miles            = Column(Integer, default=0)
    gross_revenue             = Column(Numeric(10, 2), default=0)
    avg_rpm                   = Column(Numeric(5, 3), nullable=True)
    loads_count               = Column(Integer, default=0)
    on_time_pickup_pct        = Column(Numeric(5, 2), nullable=True)
    on_time_delivery_pct      = Column(Numeric(5, 2), nullable=True)
    check_call_compliance_pct = Column(Numeric(5, 2), nullable=True)
    doc_submission_speed_hrs  = Column(Numeric(6, 2), nullable=True)
    hos_violations            = Column(Integer, default=0)
    detention_events          = Column(Integer, default=0)
    detention_revenue         = Column(Numeric(8, 2), default=0)
    acceptance_rate_pct       = Column(Numeric(5, 2), nullable=True)
    report_sent               = Column(Boolean, default=False)
    created_at                = Column(DateTime(timezone=True), server_default=func.now())

    carrier = relationship("Carrier")

    __table_args__ = (
        Index("idx_carrier_scores_carrier", "carrier_id", "week_ending"),
    )
