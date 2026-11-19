"""brands: share_token for read-only client share links

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19 14:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('brands', schema=None) as batch_op:
        batch_op.add_column(sa.Column('share_token', sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint('uq_brands_share_token', ['share_token'])


def downgrade() -> None:
    with op.batch_alter_table('brands', schema=None) as batch_op:
        batch_op.drop_constraint('uq_brands_share_token', type_='unique')
        batch_op.drop_column('share_token')