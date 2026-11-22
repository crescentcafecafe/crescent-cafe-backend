"""notification centre: per-person read / cleared state

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('notification_receipts',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('event_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
        sa.Column('owner_role', sa.String(length=12), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['event_id'], ['notification_events.id'], name=op.f('fk_notification_receipts_event_id_notification_events'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_notification_receipts')),
        sa.UniqueConstraint('event_id', 'owner_role', 'owner_id', name='uq_receipt_event_owner'),
    )
    op.create_index(op.f('ix_notification_receipts_event_id'), 'notification_receipts', ['event_id'], unique=False)
    op.create_index('ix_receipt_owner', 'notification_receipts', ['owner_role', 'owner_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_receipt_owner', table_name='notification_receipts')
    op.drop_index(op.f('ix_notification_receipts_event_id'), table_name='notification_receipts')
    op.drop_table('notification_receipts')
