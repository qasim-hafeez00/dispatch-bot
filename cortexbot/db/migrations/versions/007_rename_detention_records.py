"""
rename detention_claims to detention_records

Revision ID: 007_rename_detention_records
Revises: 006_carrier_profile_extended
Create Date: 2026-05-03

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '007_rename_detention_records'
down_revision = '006_carrier_profile_extended'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Rename table
    op.rename_table('detention_claims', 'detention_records')
    
    # Also fix some column names to match the ORM model if they differ
    # Migration 002 used:
    #   facility_type
    #   arrival_time
    #   departure_time
    #   rate_per_hour
    #   claim_amount
    #
    # ORM DetentionRecord uses:
    #   stop_type
    #   arrival_ts
    #   departure_ts
    #   hourly_rate
    #   total_amount
    
    op.alter_column('detention_records', 'facility_type', new_column_name='stop_type')
    op.alter_column('detention_records', 'arrival_time', new_column_name='arrival_ts')
    op.alter_column('detention_records', 'departure_time', new_column_name='departure_ts')
    op.alter_column('detention_records', 'rate_per_hour', new_column_name='hourly_rate')
    op.alter_column('detention_records', 'claim_amount', new_column_name='total_amount')

def downgrade() -> None:
    op.alter_column('detention_records', 'total_amount', new_column_name='claim_amount')
    op.alter_column('detention_records', 'hourly_rate', new_column_name='rate_per_hour')
    op.alter_column('detention_records', 'departure_ts', new_column_name='departure_time')
    op.alter_column('detention_records', 'arrival_ts', new_column_name='arrival_time')
    op.alter_column('detention_records', 'stop_type', new_column_name='facility_type')
    
    op.rename_table('detention_records', 'detention_claims')
