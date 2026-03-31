"""
cortexbot/db/migrations/versions/002_phase2_schema.py

Phase 2 database schema additions.

New tables:
  - invoices            Invoice lifecycle from generated to paid
  - driver_settlements  Settlement records per load
  - driver_advances     Comchek/EFS advance issuance
  - detention_claims    Accessorial claim records
  - compliance_docs     Carrier document compliance tracking
  - broker_scores       Weekly broker scoring snapshots
  - carrier_scores      Weekly carrier performance snapshots
  - freight_claims      Cargo damage/loss claim management
  - fraud_assessments   Pre-booking fraud check results

Run with: alembic upgrade head
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002_phase2"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add all Phase 2 tables."""

    # ── invoices ──────────────────────────────────────────────
    op.create_table(
        "invoices",
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("invoice_number", sa.String(50), unique=True, nullable=False),
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=False),
        sa.Column("broker_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("brokers.broker_id"), nullable=True),

        # Amounts
        sa.Column("linehaul_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("detention_pickup_amount", sa.Numeric(10, 2), server_default="0"),
        sa.Column("detention_delivery_amount", sa.Numeric(10, 2), server_default="0"),
        sa.Column("lumper_amount", sa.Numeric(10, 2), server_default="0"),
        sa.Column("tonu_amount", sa.Numeric(10, 2), server_default="0"),
        sa.Column("extra_stop_amount", sa.Numeric(10, 2), server_default="0"),
        sa.Column("driver_assist_amount", sa.Numeric(10, 2), server_default="0"),
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=False),

        # Payment tracking
        sa.Column("payment_terms_days", sa.Integer(), server_default="30"),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(30), server_default="GENERATED"),
        # GENERATED | SUBMITTED | FACTORED | PAID | SHORT_PAID | DISPUTED | IN_COLLECTIONS

        # Factoring
        sa.Column("factoring_company", sa.String(100), nullable=True),
        sa.Column("factoring_advance_pct", sa.Numeric(4, 3), nullable=True),
        sa.Column("factoring_advance_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("factoring_fee_amount", sa.Numeric(10, 2), nullable=True),

        # Payment received
        sa.Column("amount_paid", sa.Numeric(10, 2), nullable=True),
        sa.Column("payment_received_date", sa.Date(), nullable=True),
        sa.Column("days_to_pay", sa.Integer(), nullable=True),

        # Documents
        sa.Column("invoice_pdf_url", sa.Text(), nullable=True),
        sa.Column("submission_ref", sa.String(200), nullable=True),

        # Follow-up state
        sa.Column("followup_step", sa.String(30), server_default="INITIAL"),
        # INITIAL | DUE_MINUS_3 | DUE_DATE | DUE_PLUS_3 | DUE_PLUS_7 | DUE_PLUS_14 | DUE_PLUS_21 | COLLECTIONS
        sa.Column("last_followup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispute_reason", sa.Text(), nullable=True),

        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_invoices_load", "invoices", ["load_id"])
    op.create_index("idx_invoices_status", "invoices", ["status"])
    op.create_index("idx_invoices_due", "invoices", ["due_date"])

    # ── driver_settlements ────────────────────────────────────
    op.create_table(
        "driver_settlements",
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("invoices.invoice_id"), nullable=True),

        # Amounts
        sa.Column("gross_revenue", sa.Numeric(10, 2), nullable=False),
        sa.Column("dispatch_fee_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("dispatch_fee_pct", sa.Numeric(4, 3), nullable=False),
        sa.Column("fuel_advances_deducted", sa.Numeric(10, 2), server_default="0"),
        sa.Column("lumper_advances_deducted", sa.Numeric(10, 2), server_default="0"),
        sa.Column("repair_advances_deducted", sa.Numeric(10, 2), server_default="0"),
        sa.Column("other_deductions", sa.Numeric(10, 2), server_default="0"),
        sa.Column("net_settlement", sa.Numeric(10, 2), nullable=False),

        # Payment
        sa.Column("status", sa.String(30), server_default="PENDING"),
        # PENDING | PAID | HELD | NEGATIVE_BALANCE
        sa.Column("payment_method", sa.String(30), nullable=True),
        sa.Column("stripe_transfer_id", sa.String(100), nullable=True),
        sa.Column("dwolla_transfer_id", sa.String(100), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),

        sa.Column("settlement_sheet_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_settlements_load", "driver_settlements", ["load_id"])
    op.create_index("idx_settlements_carrier", "driver_settlements", ["carrier_id"])

    # ── dispatch_fees ─────────────────────────────────────────
    op.create_table(
        "dispatch_fees",
        sa.Column("fee_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=False),
        sa.Column("gross_revenue", sa.Numeric(10, 2), nullable=False),
        sa.Column("fee_model", sa.String(30), nullable=False),
        sa.Column("fee_pct", sa.Numeric(4, 3), nullable=True),
        sa.Column("fee_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(20), server_default="INVOICED"),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # ── driver_advances ───────────────────────────────────────
    op.create_table(
        "driver_advances",
        sa.Column("advance_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=False),
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=True),

        sa.Column("advance_type", sa.String(30), nullable=False),
        # FUEL | LUMPER | EMERGENCY | TOLL | REPAIR
        sa.Column("amount", sa.Numeric(8, 2), nullable=False),
        sa.Column("network", sa.String(20), nullable=True),  # EFS | COMDATA
        sa.Column("check_code", sa.String(50), nullable=True),
        sa.Column("expiry_datetime", sa.DateTime(timezone=True), nullable=True),

        sa.Column("status", sa.String(20), server_default="ISSUED"),
        # ISSUED | REDEEMED | EXPIRED | CANCELLED
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settlement_deducted", sa.Boolean(), server_default="false"),
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True), nullable=True),

        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_advances_carrier", "driver_advances", ["carrier_id"])
    op.create_index("idx_advances_load", "driver_advances", ["load_id"])

    # ── detention_claims ──────────────────────────────────────
    op.create_table(
        "detention_claims",
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("facility_type", sa.String(20), nullable=False),  # pickup | delivery
        sa.Column("arrival_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("departure_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("free_hours", sa.Numeric(3, 1), server_default="2"),
        sa.Column("billable_hours", sa.Numeric(5, 2), server_default="0"),
        sa.Column("rate_per_hour", sa.Numeric(7, 2), nullable=True),
        sa.Column("claim_amount", sa.Numeric(8, 2), server_default="0"),
        sa.Column("documented", sa.Boolean(), server_default="false"),
        sa.Column("bol_documented", sa.Boolean(), server_default="false"),
        sa.Column("geofence_documented", sa.Boolean(), server_default="false"),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_detention_load", "detention_claims", ["load_id"])

    # ── compliance_docs ───────────────────────────────────────
    op.create_table(
        "compliance_docs",
        sa.Column("doc_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=False),
        sa.Column("doc_type", sa.String(50), nullable=False),
        # COI_AUTO | COI_CARGO | COI_GENERAL | CDL | MEDICAL | HAZMAT | TWIC | DOT_REG | IFTA | DOT_INSPECTION | MC_AUTHORITY
        sa.Column("doc_url", sa.Text(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("issued_date", sa.Date(), nullable=True),
        sa.Column("issuer", sa.String(200), nullable=True),
        sa.Column("policy_number", sa.String(100), nullable=True),
        sa.Column("alert_sent_90d", sa.Boolean(), server_default="false"),
        sa.Column("alert_sent_30d", sa.Boolean(), server_default="false"),
        sa.Column("alert_sent_7d", sa.Boolean(), server_default="false"),
        sa.Column("suspended_on_expiry", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_compliance_carrier", "compliance_docs", ["carrier_id"])
    op.create_index("idx_compliance_expiry", "compliance_docs", ["expiry_date"])
    op.create_index("idx_compliance_type", "compliance_docs", ["carrier_id", "doc_type"])

    # ── broker_scores ─────────────────────────────────────────
    op.create_table(
        "broker_scores",
        sa.Column("score_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("broker_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("brokers.broker_id"), nullable=False),
        sa.Column("score_date", sa.Date(), nullable=False),
        sa.Column("overall_score", sa.Integer(), nullable=False),
        sa.Column("payment_score", sa.Integer(), server_default="0"),
        sa.Column("rate_score", sa.Integer(), server_default="0"),
        sa.Column("load_quality_score", sa.Integer(), server_default="0"),
        sa.Column("comm_score", sa.Integer(), server_default="0"),
        sa.Column("dispute_score", sa.Integer(), server_default="0"),
        sa.Column("relationship_tier", sa.String(20), nullable=True),
        sa.Column("avg_days_to_pay", sa.Numeric(5, 1), nullable=True),
        sa.Column("avg_rate_vs_market", sa.Numeric(4, 3), nullable=True),
        sa.Column("loads_last_90d", sa.Integer(), server_default="0"),
        sa.Column("dispute_rate", sa.Numeric(4, 3), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_broker_scores_broker", "broker_scores", ["broker_id", "score_date"])

    # ── carrier_scores ────────────────────────────────────────
    op.create_table(
        "carrier_scores",
        sa.Column("score_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=False),
        sa.Column("week_ending", sa.Date(), nullable=False),
        sa.Column("overall_score", sa.Integer(), nullable=False),
        sa.Column("revenue_score", sa.Integer(), server_default="0"),
        sa.Column("reliability_score", sa.Integer(), server_default="0"),
        sa.Column("utilization_score", sa.Integer(), server_default="0"),
        sa.Column("compliance_score", sa.Integer(), server_default="0"),
        sa.Column("responsiveness_score", sa.Integer(), server_default="0"),
        # KPIs
        sa.Column("weekly_miles", sa.Integer(), server_default="0"),
        sa.Column("loaded_miles", sa.Integer(), server_default="0"),
        sa.Column("deadhead_miles", sa.Integer(), server_default="0"),
        sa.Column("gross_revenue", sa.Numeric(10, 2), server_default="0"),
        sa.Column("avg_rpm", sa.Numeric(5, 3), nullable=True),
        sa.Column("loads_count", sa.Integer(), server_default="0"),
        sa.Column("on_time_pickup_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("on_time_delivery_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("check_call_compliance_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("doc_submission_speed_hrs", sa.Numeric(6, 2), nullable=True),
        sa.Column("hos_violations", sa.Integer(), server_default="0"),
        sa.Column("detention_events", sa.Integer(), server_default="0"),
        sa.Column("detention_revenue", sa.Numeric(8, 2), server_default="0"),
        sa.Column("acceptance_rate_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("report_sent", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_carrier_scores_carrier", "carrier_scores", ["carrier_id", "week_ending"])

    # ── freight_claims ────────────────────────────────────────
    op.create_table(
        "freight_claims",
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=False),
        sa.Column("carrier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("carriers.carrier_id"), nullable=True),
        sa.Column("claim_type", sa.String(30), nullable=False),
        # DAMAGE | SHORTAGE | LOSS | CONCEALED_DAMAGE | DELAY
        sa.Column("claimed_by", sa.String(30), nullable=True),  # driver | receiver | broker
        sa.Column("claimed_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("status", sa.String(30), server_default="OPEN"),
        # OPEN | UNDER_REVIEW | CONTESTED | SETTLED | PAID | CLOSED
        # Carmack deadlines
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledge_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_sent_at", sa.DateTime(timezone=True), nullable=True),
        # Evidence
        sa.Column("clean_bol", sa.Boolean(), nullable=True),
        sa.Column("receiver_signed_bol", sa.Boolean(), nullable=True),
        sa.Column("exception_at_delivery", sa.Boolean(), server_default="false"),
        sa.Column("weather_event_documented", sa.Boolean(), server_default="false"),
        sa.Column("defense_strength_score", sa.Integer(), nullable=True),  # 0-100
        sa.Column("recommendation", sa.String(20), nullable=True),  # CONTEST | NEGOTIATE | SETTLE
        # Resolution
        sa.Column("settlement_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_freight_claims_load", "freight_claims", ["load_id"])
    op.create_index("idx_freight_claims_deadline", "freight_claims", ["acknowledge_deadline"])

    # ── fraud_assessments ─────────────────────────────────────
    op.create_table(
        "fraud_assessments",
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("load_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loads.load_id"), nullable=True),
        sa.Column("broker_mc", sa.String(20), nullable=False),
        sa.Column("fraud_risk_score", sa.Integer(), nullable=False),
        sa.Column("recommendation", sa.String(20), nullable=False),
        # BOOK | CAUTION | DO_NOT_BOOK | EMERGENCY
        sa.Column("flags", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("highway_result", postgresql.JSONB(), nullable=True),
        sa.Column("fmcsa_result", postgresql.JSONB(), nullable=True),
        sa.Column("dat_credit", postgresql.JSONB(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_fraud_broker_mc", "fraud_assessments", ["broker_mc"])

    # ── Add columns to existing tables ────────────────────────
    # Add invoice-related columns to loads
    op.add_column("loads", sa.Column("invoice_id",
                  postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("loads", sa.Column("settlement_id",
                  postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("loads", sa.Column("amount_paid",
                  sa.Numeric(10, 2), nullable=True))
    op.add_column("loads", sa.Column("payment_received_date",
                  sa.Date(), nullable=True))
    op.add_column("loads", sa.Column("detention_pickup_hours",
                  sa.Numeric(5, 2), nullable=True))
    op.add_column("loads", sa.Column("detention_delivery_hours",
                  sa.Numeric(5, 2), nullable=True))

    # Add GPS / transit columns to loads
    op.add_column("loads", sa.Column("last_gps_lat", sa.Numeric(9, 6), nullable=True))
    op.add_column("loads", sa.Column("last_gps_lon", sa.Numeric(9, 6), nullable=True))
    op.add_column("loads", sa.Column("last_gps_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("loads", sa.Column("current_eta", sa.DateTime(timezone=True), nullable=True))
    op.add_column("loads", sa.Column("arrived_pickup_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("loads", sa.Column("departed_pickup_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("loads", sa.Column("arrived_delivery_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("loads", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("loads", sa.Column("pod_url", sa.Text(), nullable=True))
    op.add_column("loads", sa.Column("bol_delivery_url", sa.Text(), nullable=True))
    op.add_column("loads", sa.Column("bol_pickup_url", sa.Text(), nullable=True))

    # Add factoring columns to carriers
    op.add_column("carriers", sa.Column("stripe_customer_id", sa.String(100), nullable=True))
    op.add_column("carriers", sa.Column("stripe_connected_account_id", sa.String(100), nullable=True))
    op.add_column("carriers", sa.Column("eld_provider", sa.String(30), nullable=True))
    op.add_column("carriers", sa.Column("eld_vehicle_id", sa.String(100), nullable=True))
    op.add_column("carriers", sa.Column("fuel_card_network", sa.String(30), nullable=True))
    op.add_column("carriers", sa.Column("fuel_card_number", sa.String(50), nullable=True))
    op.add_column("carriers", sa.Column("bank_account_last4", sa.String(4), nullable=True))

    # Update broker table
    op.add_column("brokers", sa.Column("tia_watchlist", sa.Boolean(), server_default="false"))
    op.add_column("brokers", sa.Column("highway_freight_guard_score", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop Phase 2 additions."""
    # Remove added columns from carriers
    for col in ["bank_account_last4", "fuel_card_number", "fuel_card_network",
                "eld_vehicle_id", "eld_provider", "stripe_connected_account_id",
                "stripe_customer_id"]:
        op.drop_column("carriers", col)

    # Remove added columns from brokers
    for col in ["highway_freight_guard_score", "tia_watchlist"]:
        op.drop_column("brokers", col)

    # Remove added columns from loads
    for col in ["bol_pickup_url", "bol_delivery_url", "pod_url", "delivered_at",
                "arrived_delivery_at", "departed_pickup_at", "arrived_pickup_at",
                "current_eta", "last_gps_at", "last_gps_lon", "last_gps_lat",
                "detention_delivery_hours", "detention_pickup_hours",
                "payment_received_date", "amount_paid", "settlement_id", "invoice_id"]:
        op.drop_column("loads", col)

    op.drop_table("fraud_assessments")
    op.drop_table("freight_claims")
    op.drop_table("carrier_scores")
    op.drop_table("broker_scores")
    op.drop_table("compliance_docs")
    op.drop_table("detention_claims")
    op.drop_table("driver_advances")
    op.drop_table("dispatch_fees")
    op.drop_table("driver_settlements")
    op.drop_table("invoices")
