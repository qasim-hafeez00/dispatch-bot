"""
cortexbot/db/migrations/versions/001_initial_schema.py

Initial database schema — creates all Phase 1 tables.

Run with: alembic upgrade head
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001_initial"
down_revision = None   # This is the first migration
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all Phase 1 tables."""
    
    # ── carriers ──────────────────────────────────────────────
    op.create_table(
        "carriers",
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("mc_number", sa.String(20), unique=True, nullable=False),
        sa.Column("dot_number", sa.String(20), nullable=True),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("owner_name", sa.String(200), nullable=False),
        sa.Column("owner_email", sa.String(200), nullable=False),
        sa.Column("owner_phone", sa.String(30), nullable=False),
        sa.Column("driver_phone", sa.String(30), nullable=True),
        sa.Column("whatsapp_phone", sa.String(30), nullable=True),
        sa.Column("language_pref", sa.String(10), server_default="en"),
        sa.Column("equipment_type", sa.String(50), nullable=False),
        sa.Column("max_weight_lbs", sa.Integer(), server_default="44000"),
        sa.Column("home_base_city", sa.String(100), nullable=True),
        sa.Column("home_base_state", sa.CHAR(2), nullable=True),
        sa.Column("preferred_dest_states", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("avoid_states", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("rate_floor_cpm", sa.Numeric(5, 3), nullable=False),
        sa.Column("max_deadhead_mi", sa.Integer(), server_default="100"),
        sa.Column("no_touch_only", sa.Boolean(), server_default="false"),
        sa.Column("hazmat_cert", sa.Boolean(), server_default="false"),
        sa.Column("twic_card", sa.Boolean(), server_default="false"),
        sa.Column("status", sa.String(20), server_default="ACTIVE"),
        sa.Column("factoring_company", sa.String(100), nullable=True),
        sa.Column("dispatch_fee_pct", sa.Numeric(4, 3), server_default="0.060"),
        sa.Column("factoring_noa_url", sa.Text(), nullable=True),
        sa.Column("w9_url", sa.Text(), nullable=True),
        sa.Column("coi_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_carriers_status", "carriers", ["status"])
    op.create_index("idx_carriers_mc", "carriers", ["mc_number"])
    
    # ── brokers ───────────────────────────────────────────────
    op.create_table(
        "brokers",
        sa.Column("broker_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("mc_number", sa.String(20), unique=True, nullable=False),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("dat_credit_score", sa.Integer(), nullable=True),
        sa.Column("avg_days_to_pay", sa.Integer(), nullable=True),
        sa.Column("relationship_tier", sa.String(20), server_default="ACTIVE"),
        sa.Column("blacklisted", sa.Boolean(), server_default="false"),
        sa.Column("blacklist_reason", sa.Text(), nullable=True),
        sa.Column("loads_booked", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_brokers_mc", "brokers", ["mc_number"])
    
    # ── broker_contacts ───────────────────────────────────────
    op.create_table(
        "broker_contacts",
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("broker_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("brokers.broker_id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("best_lanes", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("equipment_focus", sa.String(50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    
    # ── loads ─────────────────────────────────────────────────
    op.create_table(
        "loads",
        sa.Column("load_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tms_ref", sa.String(50), unique=True, nullable=True),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=True),
        sa.Column("broker_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("brokers.broker_id"), nullable=True),
        sa.Column("broker_contact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("broker_contacts.contact_id"), nullable=True),
        sa.Column("driver_phone", sa.String(30), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="SEARCHING"),
        sa.Column("broker_load_ref", sa.String(100), nullable=True),
        sa.Column("dat_load_id", sa.String(100), nullable=True),
        sa.Column("bland_call_id", sa.String(100), nullable=True),
        sa.Column("origin_address", sa.Text(), nullable=True),
        sa.Column("origin_city", sa.String(100), nullable=True),
        sa.Column("origin_state", sa.CHAR(2), nullable=True),
        sa.Column("origin_zip", sa.String(10), nullable=True),
        sa.Column("destination_address", sa.Text(), nullable=True),
        sa.Column("destination_city", sa.String(100), nullable=True),
        sa.Column("destination_state", sa.CHAR(2), nullable=True),
        sa.Column("destination_zip", sa.String(10), nullable=True),
        sa.Column("loaded_miles", sa.Integer(), nullable=True),
        sa.Column("deadhead_miles", sa.Integer(), nullable=True),
        sa.Column("pickup_date", sa.Date(), nullable=True),
        sa.Column("pickup_appt_type", sa.String(10), nullable=True),
        sa.Column("pickup_appt_time", sa.Time(), nullable=True),
        sa.Column("pickup_appt_open", sa.Time(), nullable=True),
        sa.Column("pickup_appt_close", sa.Time(), nullable=True),
        sa.Column("delivery_date", sa.Date(), nullable=True),
        sa.Column("delivery_appt_type", sa.String(10), nullable=True),
        sa.Column("delivery_appt_time", sa.Time(), nullable=True),
        sa.Column("commodity", sa.String(200), nullable=True),
        sa.Column("weight_lbs", sa.Integer(), nullable=True),
        sa.Column("piece_count", sa.Integer(), nullable=True),
        sa.Column("equipment_type", sa.String(50), nullable=True),
        sa.Column("load_type", sa.String(30), nullable=True),
        sa.Column("unload_type", sa.String(30), nullable=True),
        sa.Column("temp_min_f", sa.Numeric(5, 1), nullable=True),
        sa.Column("temp_max_f", sa.Numeric(5, 1), nullable=True),
        sa.Column("driver_assist", sa.Boolean(), server_default="false"),
        sa.Column("lumper_required", sa.Boolean(), server_default="false"),
        sa.Column("lumper_payer", sa.String(20), nullable=True),
        sa.Column("agreed_rate_cpm", sa.Numeric(5, 3), nullable=True),
        sa.Column("agreed_rate_flat", sa.Numeric(10, 2), nullable=True),
        sa.Column("fuel_surcharge_included", sa.Boolean(), server_default="true"),
        sa.Column("detention_free_hrs", sa.Integer(), server_default="2"),
        sa.Column("detention_rate_hr", sa.Numeric(7, 2), nullable=True),
        sa.Column("tonu_amount", sa.Numeric(7, 2), nullable=True),
        sa.Column("layover_rate", sa.Numeric(7, 2), nullable=True),
        sa.Column("extra_stop_rate", sa.Numeric(7, 2), nullable=True),
        sa.Column("tracking_method", sa.String(50), nullable=True),
        sa.Column("tracking_id", sa.String(100), nullable=True),
        sa.Column("payment_terms_days", sa.Integer(), nullable=True),
        sa.Column("quick_pay_pct", sa.Numeric(4, 3), nullable=True),
        sa.Column("quick_pay_days", sa.Integer(), nullable=True),
        sa.Column("factoring_allowed", sa.Boolean(), server_default="true"),
        sa.Column("rc_url", sa.Text(), nullable=True),
        sa.Column("rc_signed_url", sa.Text(), nullable=True),
        sa.Column("carrier_packet_url", sa.Text(), nullable=True),
        sa.Column("market_rate_cpm", sa.Numeric(5, 3), nullable=True),
        sa.Column("anchor_rate_cpm", sa.Numeric(5, 3), nullable=True),
        sa.Column("call_recording_url", sa.Text(), nullable=True),
        sa.Column("extracted_call_data", postgresql.JSONB(), nullable=True),
        sa.Column("searched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("broker_called_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_agreed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("carrier_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rc_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rc_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_loads_status", "loads", ["status"])
    op.create_index("idx_loads_carrier", "loads", ["carrier_id"])
    op.create_index("idx_loads_created", "loads", ["created_at"])
    
    # ── events ────────────────────────────────────────────────
    op.create_table(
        "events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_code", sa.String(60), nullable=False),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("triggered_by", sa.String(50), nullable=True),
        sa.Column("actor", sa.String(50), server_default="cortex-bot"),
        sa.Column("data", postgresql.JSONB(), server_default="{}"),
        sa.Column("previous_status", sa.String(50), nullable=True),
        sa.Column("new_status", sa.String(50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_events_entity", "events", ["entity_type", "entity_id"])
    op.create_index("idx_events_code", "events", ["event_code"])
    op.create_index("idx_events_created", "events", ["created_at"])
    
    # ── load_checkpoints ──────────────────────────────────────
    op.create_table(
        "load_checkpoints",
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), primary_key=True),
        sa.Column("state_json", postgresql.JSONB(), nullable=False),
        sa.Column("current_skill", sa.String(60), nullable=True),
        sa.Column("checkpoint_seq", sa.Integer(), server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    
    # ── inbound_emails ────────────────────────────────────────
    op.create_table(
        "inbound_emails",
        sa.Column("email_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("message_id", sa.String(200), unique=True, nullable=True),
        sa.Column("from_email", sa.String(200), nullable=True),
        sa.Column("to_email", sa.String(200), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("has_attachment", sa.Boolean(), server_default="false"),
        sa.Column("attachment_s3_url", sa.Text(), nullable=True),
        sa.Column("attachment_filename", sa.String(200), nullable=True),
        sa.Column("category", sa.String(30), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=True),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=True),
        sa.Column("processed", sa.Boolean(), server_default="false"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    
    # ── call_log ──────────────────────────────────────────────
    op.create_table(
        "call_log",
        sa.Column("call_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("bland_ai_call_id", sa.String(100), unique=True, nullable=True),
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=True),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=True),
        sa.Column("broker_phone", sa.String(30), nullable=True),
        sa.Column("outcome", sa.String(30), nullable=True),
        sa.Column("agreed_rate_cpm", sa.Numeric(5, 3), nullable=True),
        sa.Column("call_duration_sec", sa.Integer(), nullable=True),
        sa.Column("recording_url", sa.Text(), nullable=True),
        sa.Column("transcript_raw", sa.Text(), nullable=True),
        sa.Column("extracted_data", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    
    # ── whatsapp_context ──────────────────────────────────────
    op.create_table(
        "whatsapp_context",
        sa.Column("phone", sa.String(30), primary_key=True),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=True),
        sa.Column("current_load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=True),
        sa.Column("awaiting", sa.String(50), nullable=True),
        sa.Column("language", sa.String(10), server_default="en"),
        sa.Column("conversation_json", postgresql.JSONB(), server_default="[]"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    """Drop all tables (reverses the upgrade)."""
    op.drop_table("whatsapp_context")
    op.drop_table("call_log")
    op.drop_table("inbound_emails")
    op.drop_table("load_checkpoints")
    op.drop_table("events")
    op.drop_table("loads")
    op.drop_table("broker_contacts")
    op.drop_table("brokers")
    op.drop_table("carriers")
