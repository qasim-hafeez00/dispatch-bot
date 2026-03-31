"""
cortexbot/db/migrations/versions/002_phase2_schema.py
Phase 2 schema — adds transit, detention, invoicing, settlement tables
and Phase 2 columns to existing tables.

Run: alembic upgrade head
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002_phase2"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── Add Phase 2 columns to carriers ──────────────────────
    op.add_column("carriers", sa.Column("eld_provider", sa.String(30), nullable=True))
    op.add_column("carriers", sa.Column("eld_vehicle_id", sa.String(100), nullable=True))
    op.add_column("carriers", sa.Column("eld_driver_id", sa.String(100), nullable=True))
    op.add_column("carriers", sa.Column("stripe_account_id", sa.String(100), nullable=True))
    op.add_column("carriers", sa.Column("bank_account_last4", sa.String(4), nullable=True))
    op.add_column("carriers", sa.Column("truck_weight_lbs", sa.Integer(), server_default="17000"))
    op.add_column("carriers", sa.Column("trailer_weight_lbs", sa.Integer(), server_default="13000"))
    op.add_column("carriers", sa.Column("truck_mpg", sa.Numeric(4, 1), server_default="6.5"))
    op.add_column("carriers", sa.Column("fuel_card_network", sa.String(30), nullable=True))

    # ── Add Phase 2 columns to brokers ───────────────────────
    op.add_column("brokers", sa.Column("ap_email", sa.String(200), nullable=True))
    op.add_column("brokers", sa.Column("ap_phone", sa.String(30), nullable=True))
    op.add_column("brokers", sa.Column("ops_manager_email", sa.String(200), nullable=True))

    # ── Add Phase 2 columns to loads ─────────────────────────
    p2_load_cols = [
        ("eld_provider", sa.String(30), True),
        ("last_gps_lat", sa.Numeric(9, 6), True),
        ("last_gps_lng", sa.Numeric(9, 6), True),
        ("last_gps_speed_mph", sa.Numeric(5, 1), True),
        ("last_gps_updated", sa.DateTime(timezone=True), True),
        ("current_eta", sa.DateTime(timezone=True), True),
        ("delay_minutes", sa.Integer(), True),
        ("broker_delay_notified", sa.Boolean(), False),
        ("arrived_pickup_at", sa.DateTime(timezone=True), True),
        ("loaded_at", sa.DateTime(timezone=True), True),
        ("departed_pickup_at", sa.DateTime(timezone=True), True),
        ("arrived_delivery_at", sa.DateTime(timezone=True), True),
        ("delivered_at", sa.DateTime(timezone=True), True),
        ("detention_pickup_hrs", sa.Numeric(5, 2), True),
        ("detention_pickup_amount", sa.Numeric(7, 2), True),
        ("detention_delivery_hrs", sa.Numeric(5, 2), True),
        ("detention_delivery_amount", sa.Numeric(7, 2), True),
        ("tonu_triggered", sa.Boolean(), False),
        ("tonu_claimed_amount", sa.Numeric(7, 2), True),
        ("lumper_actual_amount", sa.Numeric(7, 2), True),
        ("lumper_receipt_url", sa.Text(), True),
        ("bol_pickup_url", sa.Text(), True),
        ("bol_delivery_url", sa.Text(), True),
        ("pod_url", sa.Text(), True),
        ("pod_collected_at", sa.DateTime(timezone=True), True),
        ("gross_revenue", sa.Numeric(10, 2), True),
        ("total_accessorials", sa.Numeric(10, 2), True),
        ("total_invoice_amount", sa.Numeric(10, 2), True),
        ("dispatch_fee_amount", sa.Numeric(8, 2), True),
        ("driver_settlement_amount", sa.Numeric(10, 2), True),
        ("origin_lat", sa.Numeric(9, 6), True),
        ("origin_lng", sa.Numeric(9, 6), True),
        ("destination_lat", sa.Numeric(9, 6), True),
        ("destination_lng", sa.Numeric(9, 6), True),
    ]
    for col_name, col_type, nullable in p2_load_cols:
        op.add_column("loads", sa.Column(col_name, col_type, nullable=nullable,
                                          server_default=("false" if col_type == sa.Boolean() else None)))

    # ── transit_events ────────────────────────────────────────
    op.create_table(
        "transit_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("carriers.carrier_id"), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("lat", sa.Numeric(9, 6), nullable=True),
        sa.Column("lng", sa.Numeric(9, 6), nullable=True),
        sa.Column("speed_mph", sa.Numeric(5, 1), nullable=True),
        sa.Column("heading", sa.Integer(), nullable=True),
        sa.Column("odometer", sa.Integer(), nullable=True),
        sa.Column("hos_drive_remaining", sa.Numeric(4, 2), nullable=True),
        sa.Column("hos_window_remaining", sa.Numeric(4, 2), nullable=True),
        sa.Column("eld_provider", sa.String(30), nullable=True),
        sa.Column("raw_eld_data", postgresql.JSONB(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_transit_load_ts", "transit_events", ["load_id", "event_ts"])
    op.create_index("idx_transit_type", "transit_events", ["event_type"])

    # ── check_calls ───────────────────────────────────────────
    op.create_table(
        "check_calls",
        sa.Column("checkcall_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), server_default="PENDING"),
        sa.Column("driver_response", sa.Text(), nullable=True),
        sa.Column("driver_location", sa.String(200), nullable=True),
        sa.Column("driver_eta", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_checkcall_load_seq", "check_calls", ["load_id", "sequence"])
    op.create_index("idx_checkcall_status", "check_calls", ["status"])

    # ── detention_records ─────────────────────────────────────
    op.create_table(
        "detention_records",
        sa.Column("detention_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("stop_type", sa.String(20), nullable=False),
        sa.Column("facility_name", sa.String(200), nullable=True),
        sa.Column("facility_address", sa.Text(), nullable=True),
        sa.Column("arrival_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("departure_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_hours", sa.Numeric(5, 2), nullable=True),
        sa.Column("free_hours", sa.Integer(), server_default="2"),
        sa.Column("billable_hours", sa.Numeric(5, 2), nullable=True),
        sa.Column("hourly_rate", sa.Numeric(7, 2), nullable=True),
        sa.Column("total_amount", sa.Numeric(8, 2), nullable=True),
        sa.Column("bol_times_noted", sa.Boolean(), server_default="false"),
        sa.Column("broker_pre_alerted", sa.Boolean(), server_default="false"),
        sa.Column("broker_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bol_in_time", sa.String(20), nullable=True),
        sa.Column("bol_out_time", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), server_default="TRACKING"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_detention_load", "detention_records", ["load_id"])
    op.create_index("idx_detention_status", "detention_records", ["status"])

    # ── invoices ──────────────────────────────────────────────
    op.create_table(
        "invoices",
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("invoice_number", sa.String(50), unique=True, nullable=False),
        sa.Column("load_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("carriers.carrier_id"), nullable=False),
        sa.Column("broker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brokers.broker_id"), nullable=True),
        sa.Column("linehaul_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("detention_amount", sa.Numeric(8, 2), server_default="0"),
        sa.Column("lumper_amount", sa.Numeric(8, 2), server_default="0"),
        sa.Column("tonu_amount", sa.Numeric(7, 2), server_default="0"),
        sa.Column("extra_stop_amount", sa.Numeric(7, 2), server_default="0"),
        sa.Column("driver_assist_amount", sa.Numeric(7, 2), server_default="0"),
        sa.Column("fuel_surcharge", sa.Numeric(8, 2), server_default="0"),
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("payment_terms_days", sa.Integer(), server_default="30"),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("quick_pay_pct", sa.Numeric(4, 3), nullable=True),
        sa.Column("status", sa.String(30), server_default="GENERATED"),
        sa.Column("factoring_used", sa.Boolean(), server_default="false"),
        sa.Column("factoring_company", sa.String(100), nullable=True),
        sa.Column("factoring_submission_id", sa.String(100), nullable=True),
        sa.Column("factoring_advance_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("factoring_advanced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invoice_pdf_url", sa.Text(), nullable=True),
        sa.Column("submitted_to_email", sa.String(200), nullable=True),
        sa.Column("amount_paid", sa.Numeric(10, 2), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_variance", sa.Numeric(8, 2), nullable=True),
        sa.Column("last_reminder_sent", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_count", sa.Integer(), server_default="0"),
        sa.Column("dispute_reason", sa.Text(), nullable=True),
        sa.Column("dispute_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispute_resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collections_referred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_invoice_load", "invoices", ["load_id"])
    op.create_index("idx_invoice_status", "invoices", ["status"])
    op.create_index("idx_invoice_due", "invoices", ["due_date"])

    # ── invoice_line_items ────────────────────────────────────
    op.create_table(
        "invoice_line_items",
        sa.Column("item_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices.invoice_id"), nullable=False),
        sa.Column("item_type", sa.String(50), nullable=False),
        sa.Column("description", sa.String(300), nullable=True),
        sa.Column("quantity", sa.Numeric(7, 3), nullable=True),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("unit_rate", sa.Numeric(8, 2), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("documented", sa.Boolean(), server_default="true"),
        sa.Column("proof_url", sa.Text(), nullable=True),
    )

    # ── payments ──────────────────────────────────────────────
    op.create_table(
        "payments",
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices.invoice_id"), nullable=False),
        sa.Column("load_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loads.load_id"), nullable=True),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("carriers.carrier_id"), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("payment_method", sa.String(30), nullable=True),
        sa.Column("reference", sa.String(200), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), server_default="RECEIVED"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_payment_invoice", "payments", ["invoice_id"])

    # ── driver_settlements ────────────────────────────────────
    op.create_table(
        "driver_settlements",
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("carriers.carrier_id"), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices.invoice_id"), nullable=True),
        sa.Column("gross_revenue", sa.Numeric(10, 2), nullable=False),
        sa.Column("dispatch_fee", sa.Numeric(8, 2), nullable=False),
        sa.Column("fuel_advance_deduction", sa.Numeric(8, 2), server_default="0"),
        sa.Column("lumper_advance_deduction", sa.Numeric(7, 2), server_default="0"),
        sa.Column("repair_advance_deduction", sa.Numeric(7, 2), server_default="0"),
        sa.Column("other_deductions", sa.Numeric(7, 2), server_default="0"),
        sa.Column("total_deductions", sa.Numeric(10, 2), nullable=False),
        sa.Column("net_settlement", sa.Numeric(10, 2), nullable=False),
        sa.Column("payment_method", sa.String(30), nullable=True),
        sa.Column("stripe_transfer_id", sa.String(100), nullable=True),
        sa.Column("bank_last4", sa.String(4), nullable=True),
        sa.Column("status", sa.String(20), server_default="CALCULATED"),
        sa.Column("settlement_pdf_url", sa.Text(), nullable=True),
        sa.Column("calculated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_settlement_load", "driver_settlements", ["load_id"])
    op.create_index("idx_settlement_carrier", "driver_settlements", ["carrier_id"])
    op.create_index("idx_settlement_status", "driver_settlements", ["status"])

    # ── driver_advances ───────────────────────────────────────
    op.create_table(
        "driver_advances",
        sa.Column("advance_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("carriers.carrier_id"), nullable=False),
        sa.Column("load_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loads.load_id"), nullable=True),
        sa.Column("advance_type", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(8, 2), nullable=False),
        sa.Column("network", sa.String(20), nullable=True),
        sa.Column("check_code", sa.String(50), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), server_default="ISSUED"),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settlement_deducted", sa.Boolean(), server_default="false"),
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("driver_settlements.settlement_id"), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_advance_carrier", "driver_advances", ["carrier_id"])
    op.create_index("idx_advance_load", "driver_advances", ["load_id"])
    op.create_index("idx_advance_status", "driver_advances", ["status"])

    # ── weather_alerts ────────────────────────────────────────
    op.create_table(
        "weather_alerts",
        sa.Column("alert_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("alert_type", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("affected_area", sa.String(200), nullable=True),
        sa.Column("route_segment", sa.String(200), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("driver_impact", sa.Text(), nullable=True),
        sa.Column("driver_alerted", sa.Boolean(), server_default="false"),
        sa.Column("driver_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("broker_notified", sa.Boolean(), server_default="false"),
        sa.Column("broker_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("action_taken", sa.String(50), nullable=True),
        sa.Column("force_majeure_documented", sa.Boolean(), server_default="false"),
        sa.Column("documentation_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_weather_load", "weather_alerts", ["load_id"])
    op.create_index("idx_weather_severity", "weather_alerts", ["severity"])

    # ── quickbooks_sync_log ───────────────────────────────────
    op.create_table(
        "quickbooks_sync_log",
        sa.Column("sync_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("qbo_entity_type", sa.String(30), nullable=True),
        sa.Column("qbo_entity_id", sa.String(50), nullable=True),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), server_default="PENDING"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_qb_entity", "quickbooks_sync_log", ["entity_type", "entity_id"])
    op.create_index("idx_qb_status", "quickbooks_sync_log", ["status"])


def downgrade() -> None:
    op.drop_table("quickbooks_sync_log")
    op.drop_table("weather_alerts")
    op.drop_table("driver_advances")
    op.drop_table("driver_settlements")
    op.drop_table("payments")
    op.drop_table("invoice_line_items")
    op.drop_table("invoices")
    op.drop_table("detention_records")
    op.drop_table("check_calls")
    op.drop_table("transit_events")
    # Remove added columns (simplified — production would list each)
