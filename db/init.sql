-- ============================================================
-- CortexBot Phase 1 — Database Initialization
-- 
-- Creates all tables for the autonomous dispatch loop.
-- Run automatically by Docker via:
--   volumes: ./db/init.sql:/docker-entrypoint-initdb.d/init.sql
--
-- NOTE: In production, use Alembic migrations instead.
--       This file is for initial local development setup.
-- ============================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Carriers ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS carriers (
    carrier_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    mc_number       VARCHAR(20) NOT NULL UNIQUE,
    dot_number      VARCHAR(20),
    company_name    VARCHAR(200) NOT NULL,
    owner_name      VARCHAR(150),
    owner_email     VARCHAR(254),
    owner_phone     VARCHAR(20),
    driver_phone    VARCHAR(20),
    whatsapp_phone  VARCHAR(20),
    language_pref   VARCHAR(5) DEFAULT 'en',
    
    -- Equipment & Capabilities
    equipment_type  VARCHAR(30) DEFAULT '53_dry_van',
    max_weight_lbs  INTEGER DEFAULT 44000,
    home_base_city  VARCHAR(100),
    home_base_state VARCHAR(2),
    preferred_dest_states JSONB DEFAULT '[]',
    avoid_states    JSONB DEFAULT '[]',
    rate_floor_cpm  NUMERIC(5,2) DEFAULT 2.00,
    max_deadhead_mi INTEGER DEFAULT 100,
    no_touch_only   BOOLEAN DEFAULT FALSE,
    hazmat_cert     BOOLEAN DEFAULT FALSE,
    twic_card       BOOLEAN DEFAULT FALSE,
    
    -- Business
    factoring_company    VARCHAR(200),
    factoring_noa_url    TEXT,
    dispatch_fee_pct     NUMERIC(4,3) DEFAULT 0.060,
    
    -- Documents
    w9_url          TEXT,
    coi_url         TEXT,
    
    -- Status
    status          VARCHAR(20) DEFAULT 'ACTIVE',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_carriers_status ON carriers(status);
CREATE INDEX IF NOT EXISTS idx_carriers_mc ON carriers(mc_number);

-- ── Brokers ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS brokers (
    broker_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    mc_number       VARCHAR(20) UNIQUE,
    company_name    VARCHAR(200),
    
    -- Credit & History
    dat_credit_score     INTEGER,
    avg_days_to_pay      INTEGER,
    total_loads_with_us  INTEGER DEFAULT 0,
    blacklisted          BOOLEAN DEFAULT FALSE,
    
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_brokers_mc ON brokers(mc_number);

-- ── Broker Contacts ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broker_contacts (
    contact_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    broker_id       UUID REFERENCES brokers(broker_id),
    name            VARCHAR(150),
    email           VARCHAR(254),
    phone           VARCHAR(20),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_broker_contacts_broker ON broker_contacts(broker_id);

-- ── Loads ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loads (
    load_id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tms_ref             VARCHAR(20) UNIQUE,
    carrier_id          UUID REFERENCES carriers(carrier_id),
    broker_id           UUID REFERENCES brokers(broker_id),
    broker_contact_id   UUID REFERENCES broker_contacts(contact_id),
    broker_load_ref     VARCHAR(50),
    
    -- Status
    status              VARCHAR(30) DEFAULT 'SEARCHING',
    
    -- Route
    origin_city         VARCHAR(100),
    origin_state        VARCHAR(2),
    destination_city    VARCHAR(100),
    destination_state   VARCHAR(2),
    loaded_miles        INTEGER,
    deadhead_miles      INTEGER,
    equipment_type      VARCHAR(30),
    
    -- Dates
    pickup_date         DATE,
    delivery_date       DATE,
    
    -- Load Details
    commodity           VARCHAR(200),
    weight_lbs          INTEGER,
    load_type           VARCHAR(20),
    tracking_method     VARCHAR(50),
    
    -- Rate
    agreed_rate_cpm     NUMERIC(6,2),
    
    -- Payment
    detention_free_hrs  INTEGER DEFAULT 2,
    detention_rate_hr   NUMERIC(6,2),
    tonu_amount         NUMERIC(8,2),
    lumper_payer        VARCHAR(10),
    payment_terms_days  INTEGER DEFAULT 30,
    factoring_allowed   BOOLEAN DEFAULT TRUE,
    
    -- Documents
    rc_url              TEXT,
    rc_signed_url       TEXT,
    
    -- Driver
    driver_phone        VARCHAR(20),
    
    -- Timeline
    searched_at          TIMESTAMPTZ,
    broker_called_at     TIMESTAMPTZ,
    carrier_confirmed_at TIMESTAMPTZ,
    booked_at            TIMESTAMPTZ,
    rc_received_at       TIMESTAMPTZ,
    rc_signed_at         TIMESTAMPTZ,
    dispatched_at        TIMESTAMPTZ,
    
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_loads_status ON loads(status);
CREATE INDEX IF NOT EXISTS idx_loads_carrier ON loads(carrier_id);
CREATE INDEX IF NOT EXISTS idx_loads_broker ON loads(broker_id);
CREATE INDEX IF NOT EXISTS idx_loads_tms_ref ON loads(tms_ref);

-- ── Events (Audit Log) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    event_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_code      VARCHAR(50) NOT NULL,
    entity_type     VARCHAR(20) NOT NULL,
    entity_id       VARCHAR(50) NOT NULL,
    triggered_by    VARCHAR(50),
    data            JSONB DEFAULT '{}',
    new_status      VARCHAR(30),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_events_code ON events(event_code);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

-- ── Load Checkpoints (Workflow State) ─────────────────────────
CREATE TABLE IF NOT EXISTS load_checkpoints (
    load_id         UUID PRIMARY KEY REFERENCES loads(load_id),
    state_json      JSONB NOT NULL,
    current_skill   VARCHAR(50),
    checkpoint_seq  INTEGER DEFAULT 1,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Inbound Emails ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inbound_emails (
    email_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    message_id          VARCHAR(500),
    from_email          VARCHAR(254),
    subject             VARCHAR(500),
    body_text           TEXT,
    has_attachment       BOOLEAN DEFAULT FALSE,
    attachment_s3_url    TEXT,
    category            VARCHAR(30),
    confidence          NUMERIC(3,2),
    processed           BOOLEAN DEFAULT FALSE,
    matched_load_id     UUID REFERENCES loads(load_id),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_emails_category ON inbound_emails(category);
CREATE INDEX IF NOT EXISTS idx_emails_processed ON inbound_emails(processed);

-- ── Call Log ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS call_log (
    call_id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    load_id             UUID REFERENCES loads(load_id),
    bland_call_id       VARCHAR(100),
    direction           VARCHAR(10) DEFAULT 'outbound',
    to_phone            VARCHAR(20),
    from_phone          VARCHAR(20),
    status              VARCHAR(20),
    duration_secs       INTEGER,
    recording_url       TEXT,
    transcript          TEXT,
    extracted_data      JSONB,
    initiated_at        TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_calls_load ON call_log(load_id);
CREATE INDEX IF NOT EXISTS idx_calls_bland ON call_log(bland_call_id);

-- ── WhatsApp Context ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS whatsapp_context (
    phone               VARCHAR(20) PRIMARY KEY,
    carrier_id          UUID REFERENCES carriers(carrier_id),
    current_load_id     UUID REFERENCES loads(load_id),
    awaiting            VARCHAR(30),
    language            VARCHAR(5) DEFAULT 'en',
    last_message_at     TIMESTAMPTZ DEFAULT NOW(),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Done! All Phase 1 tables are ready.
-- ============================================================
