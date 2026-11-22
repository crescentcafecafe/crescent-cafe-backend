"""password-reset OTPs, rider cash ledger, saved addresses, rider-collected payment columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    now = sa.text('now()')
    op.create_table('password_reset_otps',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('customer_id', sa.Uuid(), nullable=False),
        sa.Column('otp_hash', sa.String(length=64), nullable=False),
        sa.Column('salt', sa.String(length=32), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('max_attempts', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_password_reset_otps_customer_id_customers')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_password_reset_otps')),
    )
    op.create_index(op.f('ix_password_reset_otps_customer_id'), 'password_reset_otps', ['customer_id'], unique=False)
    op.create_index('ix_pwreset_customer_created', 'password_reset_otps', ['customer_id', 'created_at'], unique=False)

    op.create_table('customer_addresses',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('customer_id', sa.Uuid(), nullable=False),
        sa.Column('label', sa.String(length=20), nullable=False),
        sa.Column('line1', sa.String(length=200), nullable=False),
        sa.Column('line2', sa.String(length=200), nullable=True),
        sa.Column('landmark', sa.String(length=200), nullable=True),
        sa.Column('city', sa.String(length=80), nullable=True),
        sa.Column('postal_code', sa.String(length=12), nullable=False),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('location_source', sa.String(length=8), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_customer_addresses_customer_id_customers')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_customer_addresses')),
    )
    op.create_index(op.f('ix_customer_addresses_customer_id'), 'customer_addresses', ['customer_id'], unique=False)

    op.create_table('rider_cash_ledger',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('branch_id', sa.Uuid(), nullable=False),
        sa.Column('rider_id', sa.Uuid(), nullable=False),
        sa.Column('order_id', sa.Uuid(), nullable=True),
        sa.Column('payment_id', sa.Uuid(), nullable=True),
        sa.Column('entry_type', sa.String(length=12), nullable=False),
        sa.Column('amount_paise', sa.Integer(), nullable=False),
        sa.Column('note', sa.String(length=300), nullable=True),
        sa.Column('created_by_role', sa.String(length=12), nullable=False),
        sa.Column('created_by_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.CheckConstraint('amount_paise > 0', name=op.f('ck_rider_cash_ledger_ledger_amount_positive')),
        sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], name=op.f('fk_rider_cash_ledger_branch_id_branches')),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_rider_cash_ledger_order_id_orders')),
        sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], name=op.f('fk_rider_cash_ledger_payment_id_payments')),
        sa.ForeignKeyConstraint(['rider_id'], ['riders.id'], name=op.f('fk_rider_cash_ledger_rider_id_riders')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_rider_cash_ledger')),
    )
    op.create_index('ix_cash_ledger_rider_created', 'rider_cash_ledger', ['rider_id', 'created_at'], unique=False)

    op.add_column('payments', sa.Column('collected_by_rider_id', sa.Uuid(), nullable=True))
    op.add_column('payments', sa.Column('collected_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('payments', sa.Column('collection_channel', sa.String(length=16), nullable=True))
    op.create_foreign_key(op.f('fk_payments_collected_by_rider_id_riders'), 'payments', 'riders', ['collected_by_rider_id'], ['id'])
    op.add_column('rider_assignments', sa.Column('arrival_notified_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('rider_assignments', 'arrival_notified_at')
    op.drop_constraint(op.f('fk_payments_collected_by_rider_id_riders'), 'payments', type_='foreignkey')
    op.drop_column('payments', 'collection_channel')
    op.drop_column('payments', 'collected_at')
    op.drop_column('payments', 'collected_by_rider_id')
    op.drop_index('ix_cash_ledger_rider_created', table_name='rider_cash_ledger')
    op.drop_table('rider_cash_ledger')
    op.drop_index(op.f('ix_customer_addresses_customer_id'), table_name='customer_addresses')
    op.drop_table('customer_addresses')
    op.drop_index('ix_pwreset_customer_created', table_name='password_reset_otps')
    op.drop_index(op.f('ix_password_reset_otps_customer_id'), table_name='password_reset_otps')
    op.drop_table('password_reset_otps')
