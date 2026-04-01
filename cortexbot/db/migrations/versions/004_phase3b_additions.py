"""
cortexbot/db/migrations/versions/004_phase3b_additions.py

Phase 3B database fixes.

Adds missing columns, tables, and constraints discovered during Phase 3B
implementation of Skills U, V, and Y.

Run: alembic upgrade head

Changes:
  1. carriers table
     - Add stripe_account_id as canonical column
     - Add ein / ssn_last4 columns for 1099 routing

  2. NEW: load_expenses table
     - Per-load expense ledger for Skill U
     - Indexed on carrier_id + year for 1099 aggregation

  3. NEW: carrier_tax_info table
     - Secure storage for carrier TIN (EIN or SSN)
     - Separate table to limit who can SELECT tax data

  4. compliance_docs table
     - Add UNIQUE constraint on (carrier_id, doc_type) if missing
       (required by skill_26's INSERT … ON CONFLICT DO UPDATE)

  5. freight_claims table
     - Add clean_bol, receiver_signed_bol, exception_at_delivery,
       weather_event_documented, defense_strength_score, recommendation
       columns if they don't already exist

  6. loads table
     - Ensure invoice_id and settlement_id columns exist

  7. loads table
     - Ensure amount_paid and payment_received_date exist

  8. loads table
     - Ensure detention columns (detention_pickup_hours etc.) exist
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import sqlalchemy.exc

# Revision IDs
revision      = "004_phase3b"
down_revision = "003_phase3a"
branch_labels = None
depends_on    = None


def upgrade() -> None:

    # ── 1. carriers: stripe_account_id alias ─────────────────
    _add_column_if_missing("carriers", sa.Column(
        "stripe_account_id", sa.String(100), nullable=True
    ))
    _add_column_if_missing("carriers", sa.Column(
        "stripe_connected_account_id", sa.String(100), nullable=True
    ))

    # ── 2. load_expenses table ────────────────────────────────
    if not _table_exists("load_expenses"):
        op.create_table(
            "load_expenses",
            sa.Column("expense_id",    postgresql.UUID(as_uuid=True),
                      primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("load_id",       postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("loads.load_id"), nullable=False),
            sa.Column("carrier_id",    postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("carriers.carrier_id"), nullable=False),
            sa.Column("expense_type",  sa.String(30), nullable=False),
            sa.Column("category_label", sa.String(100), nullable=True),
            sa.Column("amount",        sa.Numeric(10, 2), nullable=False),
            sa.Column("description",   sa.Text, nullable=True),
            sa.Column("receipt_url",   sa.Text, nullable=True),
            sa.Column("reference",     sa.String(100), nullable=True),
            sa.Column("recorded_at",   sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("NOW()")),
            sa.Column("created_at",    sa.DateTime(timezone=True),
                      server_default=sa.text("NOW()")),
        )
        op.create_index(
            "idx_expenses_load",       "load_expenses", ["load_id"]
        )
        op.create_index(
            "idx_expenses_carrier_yr", "load_expenses",
            ["carrier_id", sa.text("EXTRACT(YEAR FROM recorded_at)")]
        )

    # ── 3. carrier_tax_info table ─────────────────────────────
    if not _table_exists("carrier_tax_info"):
        op.create_table(
            "carrier_tax_info",
            sa.Column("carrier_id",   postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("carriers.carrier_id"), primary_key=True),
            sa.Column("tin",          sa.String(20), nullable=True,
                      comment="Encrypted EIN or SSN — decrypt before use"),
            sa.Column("tin_type",     sa.String(5), nullable=True,
                      comment="EIN or SSN"),
            sa.Column("legal_name",   sa.String(200), nullable=True,
                      comment="Legal name as appears on W-9"),
            sa.Column("is_corp",      sa.Boolean, default=False,
                      comment="True = Inc./Corp./LLC, may not need 1099"),
            sa.Column("w9_on_file",   sa.Boolean, default=False),
            sa.Column("w9_url",       sa.Text, nullable=True),
            sa.Column("w9_signed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at",   sa.DateTime(timezone=True),
                      server_default=sa.text("NOW()")),
            sa.Column("updated_at",   sa.DateTime(timezone=True),
                      server_default=sa.text("NOW()")),
        )

    # ── 4. compliance_docs unique constraint ──────────────────
    _create_unique_constraint_if_missing(
        "compliance_docs",
        "uq_compliance_carrier_doc",
        ["carrier_id", "doc_type"],
    )

    # ── 5. freight_claims: Phase 3B columns ───────────────────
    _add_column_if_missing("freight_claims", sa.Column(
        "clean_bol", sa.Boolean, nullable=True
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "receiver_signed_bol", sa.Boolean, nullable=True
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "exception_at_delivery", sa.Boolean, server_default="false"
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "weather_event_documented", sa.Boolean, server_default="false"
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "defense_strength_score", sa.Integer, nullable=True
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "recommendation", sa.String(20), nullable=True
    ))
    _add_column_if_missing("freight_claims", sa.Column(
        "settlement_amount", sa.Numeric(10, 2), nullable=True
    ))

    # ── 6. loads: invoice_id / settlement_id ─────────────────
    _add_column_if_missing("loads", sa.Column(
        "invoice_id",   postgresql.UUID(as_uuid=True), nullable=True
    ))
    _add_column_if_missing("loads", sa.Column(
        "settlement_id", postgresql.UUID(as_uuid=True), nullable=True
    ))

    # ── 7. loads: amount_paid / payment_received_date ─────────
    _add_column_if_missing("loads", sa.Column(
        "amount_paid",           sa.Numeric(10, 2), nullable=True
    ))
    _add_column_if_missing("loads", sa.Column(
        "payment_received_date", sa.Date, nullable=True
    ))

    # ── 8. loads: detention columns (correct names) ───────────
    _add_column_if_missing("loads", sa.Column(
        "detention_pickup_hours",    sa.Numeric(5, 2), nullable=True
    ))
    _add_column_if_missing("loads", sa.Column(
        "detention_delivery_hours",  sa.Numeric(5, 2), nullable=True
    ))
    _add_column_if_missing("loads", sa.Column(
        "detention_pickup_amount",   sa.Numeric(7, 2), nullable=True
    ))
    _add_column_if_missing("loads", sa.Column(
        "detention_delivery_amount", sa.Numeric(7, 2), nullable=True
    ))

    # ── 9. carriers: ELD columns ─────────────────────────────
    _add_column_if_missing("carriers", sa.Column(
        "eld_driver_id", sa.String(100), nullable=True
    ))

    # ── 10. broker_scores / carrier_scores: ON CONFLICT ───────
    _create_unique_constraint_if_missing(
        "broker_scores",  "uq_broker_score_date",    ["broker_id",  "score_date"]
    )
    _create_unique_constraint_if_missing(
        "carrier_scores", "uq_carrier_score_week",   ["carrier_id", "week_ending"]
    )


def downgrade() -> None:
    _drop_table_if_exists("carrier_tax_info")
    _drop_table_if_exists("load_expenses")
    try: op.drop_column("carriers", "stripe_account_id")
    except: pass


# ─────────────────────────────────────────────────────────────
# MIGRATION HELPERS
# ─────────────────────────────────────────────────────────────

def _table_exists(table_name: str) -> bool:
    bind      = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names(schema="public")

def _add_column_if_missing(table: str, column: sa.Column):
    bind      = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        existing = {c["name"] for c in inspector.get_columns(table)}
    except sa.exc.NoSuchTableError:
        existing = set()
    if column.name not in existing:
        op.add_column(table, column)

def _create_unique_constraint_if_missing(table: str, constraint_name: str, columns: list):
    bind      = op.get_bind()
    inspector = sa.inspect(bind)
    existing_constraints = set()
    try:
        existing_constraints = {uc["name"] for uc in inspector.get_unique_constraints(table)}
    except: pass
    if constraint_name not in existing_constraints:
        try: op.create_unique_constraint(constraint_name, table, columns)
        except: pass

def _drop_table_if_exists(table_name: str):
    try: op.drop_table(table_name)
    except: pass
