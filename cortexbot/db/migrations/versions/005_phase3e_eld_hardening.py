from alembic import op
import sqlalchemy as sa

revision      = "005_phase3e"
down_revision = "004_phase3b"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # 1. Carrier ELD webhook secret (per-carrier override for multi-tenant setups)
    _add_column_if_missing("carriers", sa.Column(
        "eld_webhook_secret", sa.String(200), nullable=True,
        comment="Per-carrier Samsara/Motive webhook signing secret override"
    ))

    # 2. transit_events — store event hash for idempotency audit
    _add_column_if_missing("transit_events", sa.Column(
        "event_hash", sa.String(48), nullable=True,
        comment="SHA-256 hex prefix used for webhook deduplication"
    ))

    # 3. detention_records — link back to ELD geofence ID
    _add_column_if_missing("detention_records", sa.Column(
        "geofence_id", sa.String(200), nullable=True,
        comment="ELD provider geofence ID that triggered this detention record"
    ))

    # 4. loads — geofence registration audit columns
    _add_column_if_missing("loads", sa.Column(
        "pickup_geofence_id", sa.String(200), nullable=True
    ))
    _add_column_if_missing("loads", sa.Column(
        "delivery_geofence_id", sa.String(200), nullable=True
    ))


def downgrade() -> None:
    for col in ["eld_webhook_secret"]:
        try: op.drop_column("carriers", col)
        except: pass
    for col in ["event_hash"]:
        try: op.drop_column("transit_events", col)
        except: pass
    for col in ["geofence_id"]:
        try: op.drop_column("detention_records", col)
        except: pass
    for col in ["pickup_geofence_id", "delivery_geofence_id"]:
        try: op.drop_column("loads", col)
        except: pass


def _add_column_if_missing(table: str, column: sa.Column):
    bind      = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        existing = {c["name"] for c in inspector.get_columns(table)}
    except sa.exc.NoSuchTableError:
        existing = set()
    if column.name not in existing:
        op.add_column(table, column)
