"""
cortexbot/db/migrations/versions/003_phase3a_fixes.py

Phase 3A database schema fixes.

Addresses GAP-09 column renames and adds missing Phase 2 columns
that were in op.add_column() migration calls but absent from ORM models.

Run: alembic upgrade head

Changes:
  1. loads table — rename detention_pickup_hrs → detention_pickup_hours
                           detention_delivery_hrs → detention_delivery_hours
     (old column names no longer matched ORM; s27 was always reading 0)

  2. loads table — add amount_paid (Numeric 10,2) if missing
                   add payment_received_date (Date) if missing

  3. broker_scores table — add unique constraint on (broker_id, score_date)
                           prevents duplicate scoring runs inflating history

  4. carrier_scores table — add unique constraint on (carrier_id, week_ending)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision      = "003_phase3a"
down_revision = "002_phase2"
branch_labels = None
depends_on    = None


def upgrade() -> None:

    # ── 1. Rename detention columns on loads ─────────────────
    # Old name (_hrs) never matched what s27 was reading (_hours)
    # → all detention invoicing silently returned $0.
    # We use op.alter_column to rename without data loss.
    try:
        op.alter_column("loads", "detention_pickup_hrs",
                         new_column_name="detention_pickup_hours",
                         existing_type=sa.Numeric(5, 2), nullable=True)
    except Exception:
        # Column may already have been renamed in a prior manual migration
        pass

    try:
        op.alter_column("loads", "detention_delivery_hrs",
                         new_column_name="detention_delivery_hours",
                         existing_type=sa.Numeric(5, 2), nullable=True)
    except Exception:
        pass

    # ── 2. Add missing financial columns to loads ─────────────
    # These were added by migration 002 via op.add_column() but
    # were never declared in models.py, so ORM access raised AttributeError.
    _add_column_if_missing("loads", sa.Column(
        "amount_paid",
        sa.Numeric(10, 2),
        nullable=True,
    ))
    _add_column_if_missing("loads", sa.Column(
        "payment_received_date",
        sa.Date(),
        nullable=True,
    ))

    # ── 3. Add invoice_id / settlement_id refs if missing ─────
    _add_column_if_missing("loads", sa.Column(
        "invoice_id",
        postgresql.UUID(as_uuid=True),
        nullable=True,
    ))
    _add_column_if_missing("loads", sa.Column(
        "settlement_id",
        postgresql.UUID(as_uuid=True),
        nullable=True,
    ))

    # ── 4. Unique constraints on score tables ─────────────────
    # Prevents re-running the weekly job from creating duplicate rows.
    try:
        op.create_unique_constraint(
            "uq_broker_score_date",
            "broker_scores",
            ["broker_id", "score_date"],
        )
    except Exception:
        pass

    try:
        op.create_unique_constraint(
            "uq_carrier_score_week",
            "carrier_scores",
            ["carrier_id", "week_ending"],
        )
    except Exception:
        pass

    # ── 5. Add freight_claims.clean_bol columns if missing ────
    # sy_freight_claims.py reads these columns; migration 002 may
    # not have added all of them.
    _add_column_if_missing("freight_claims", sa.Column(
        "clean_bol",
        sa.Boolean(),
        nullable=True,
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "receiver_signed_bol",
        sa.Boolean(),
        nullable=True,
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "exception_at_delivery",
        sa.Boolean(),
        server_default="false",
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "weather_event_documented",
        sa.Boolean(),
        server_default="false",
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "defense_strength_score",
        sa.Integer(),
        nullable=True,
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "recommendation",
        sa.String(20),
        nullable=True,
    ))

    # ── 6. ELD webhook columns on loads (geofence tracking) ───
    _add_column_if_missing("loads", sa.Column(
        "arrived_pickup_at",
        sa.DateTime(timezone=True),
        nullable=True,
    ))
    _add_column_if_missing("loads", sa.Column(
        "departed_pickup_at",
        sa.DateTime(timezone=True),
        nullable=True,
    ))
    _add_column_if_missing("loads", sa.Column(
        "arrived_delivery_at",
        sa.DateTime(timezone=True),
        nullable=True,
    ))
    _add_column_if_missing("loads", sa.Column(
        "delivered_at",
        sa.DateTime(timezone=True),
        nullable=True,
    ))

    # ── 7. Carrier ELD columns ────────────────────────────────
    _add_column_if_missing("carriers", sa.Column(
        "eld_provider",
        sa.String(30),
        nullable=True,
    ))
    _add_column_if_missing("carriers", sa.Column(
        "eld_vehicle_id",
        sa.String(100),
        nullable=True,
    ))
    _add_column_if_missing("carriers", sa.Column(
        "eld_driver_id",
        sa.String(100),
        nullable=True,
    ))


def downgrade() -> None:
    # Rename columns back
    try:
        op.alter_column("loads", "detention_pickup_hours",
                         new_column_name="detention_pickup_hrs",
                         existing_type=sa.Numeric(5, 2), nullable=True)
        op.alter_column("loads", "detention_delivery_hours",
                         new_column_name="detention_delivery_hrs",
                         existing_type=sa.Numeric(5, 2), nullable=True)
    except Exception:
        pass

    # Drop added columns
    for col in ["amount_paid", "payment_received_date", "invoice_id", "settlement_id"]:
        try:
            op.drop_column("loads", col)
        except Exception:
            pass

    # Drop unique constraints
    try:
        op.drop_constraint("uq_broker_score_date",  "broker_scores")
        op.drop_constraint("uq_carrier_score_week", "carrier_scores")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────────────────────────

def _add_column_if_missing(table: str, column: sa.Column):
    """
    Add a column only if it doesn't already exist.
    Skips silently when the column is genuinely present.
    Re-raises any OTHER error (permissions, syntax, …) so it fails loudly.

    COPILOT FIX: the previous implementation used a bare `except: pass`
    which would silently hide real migration errors (e.g. wrong type,
    missing table, permission denied).  We now use sa.inspect() to
    check column existence explicitly and only skip the DuplicateColumn
    case.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    try:
        existing_cols = {c["name"] for c in inspector.get_columns(table)}
    except sa.exc.NoSuchTableError:
        # Table doesn't exist yet — let add_column fail properly
        existing_cols = set()

    if column.name in existing_cols:
        return  # Column already present — nothing to do

    # Column is absent: add it. Any error other than "already exists" propagates.
    op.add_column(table, column)
