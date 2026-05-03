"""
006_carrier_profile_extended.py

Adds carrier profile fields that were missing from the original schema,
required for complete workflow automation (Steps 2-6 of dispatch workflow):

Equipment capabilities:
  - tarp_capable, straps_count, load_locks_qty (flatbed/van equipment checks)
  - team_capable (team-driver loads)
  - reefer_temp_min_f / reefer_temp_max_f (temperature range the carrier can maintain)
  - max_loaded_length_ft (max trailer length for triage)
  - commodity_exclusions (carrier-defined blacklist beyond hazmat)

Geographic constraints:
  - avoid_nyc (NYC surcharge / carrier preference)
  - avoid_ports (port/intermodal restriction)
  - canada_ok (cross-border capable)

Carrier preferences:
  - preferred_home_time_days (how many days home per week)
  - comm_start_hour / comm_end_hour (quick-approval availability window)

Compliance:
  - cdl_url (CDL document — was missing from CarrierDocument types)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision      = "006_carrier_extended"
down_revision = "005_phase3e"
branch_labels = None
depends_on    = None


def _add_column_if_missing(table: str, column: sa.Column):
    """Idempotent column addition — no-op if column already exists."""
    try:
        op.add_column(table, column)
    except Exception:
        pass


def upgrade() -> None:
    # ── Equipment capabilities ────────────────────────────────
    _add_column_if_missing("carriers", sa.Column(
        "tarp_capable", sa.Boolean(), nullable=True, server_default="false"
    ))
    _add_column_if_missing("carriers", sa.Column(
        "straps_count", sa.Integer(), nullable=True
    ))
    _add_column_if_missing("carriers", sa.Column(
        "load_locks_qty", sa.Integer(), nullable=True
    ))
    _add_column_if_missing("carriers", sa.Column(
        "team_capable", sa.Boolean(), nullable=True, server_default="false"
    ))
    _add_column_if_missing("carriers", sa.Column(
        "reefer_temp_min_f", sa.Numeric(5, 1), nullable=True
    ))
    _add_column_if_missing("carriers", sa.Column(
        "reefer_temp_max_f", sa.Numeric(5, 1), nullable=True
    ))
    _add_column_if_missing("carriers", sa.Column(
        "max_loaded_length_ft", sa.Integer(), nullable=True, server_default="53"
    ))
    _add_column_if_missing("carriers", sa.Column(
        "commodity_exclusions", postgresql.ARRAY(sa.String()), nullable=True
    ))

    # ── Geographic constraints ────────────────────────────────
    _add_column_if_missing("carriers", sa.Column(
        "avoid_nyc", sa.Boolean(), nullable=True, server_default="false"
    ))
    _add_column_if_missing("carriers", sa.Column(
        "avoid_ports", sa.Boolean(), nullable=True, server_default="false"
    ))
    _add_column_if_missing("carriers", sa.Column(
        "canada_ok", sa.Boolean(), nullable=True, server_default="false"
    ))

    # ── Carrier preferences ───────────────────────────────────
    _add_column_if_missing("carriers", sa.Column(
        "preferred_home_time_days", sa.Integer(), nullable=True
    ))
    _add_column_if_missing("carriers", sa.Column(
        "comm_start_hour", sa.Integer(), nullable=True
    ))
    _add_column_if_missing("carriers", sa.Column(
        "comm_end_hour", sa.Integer(), nullable=True
    ))

    # ── Compliance docs ───────────────────────────────────────
    _add_column_if_missing("carriers", sa.Column(
        "cdl_url", sa.Text(), nullable=True
    ))


def downgrade() -> None:
    new_cols = [
        "tarp_capable", "straps_count", "load_locks_qty", "team_capable",
        "reefer_temp_min_f", "reefer_temp_max_f", "max_loaded_length_ft",
        "commodity_exclusions", "avoid_nyc", "avoid_ports", "canada_ok",
        "preferred_home_time_days", "comm_start_hour", "comm_end_hour", "cdl_url",
    ]
    for col in new_cols:
        try:
            op.drop_column("carriers", col)
        except Exception:
            pass
