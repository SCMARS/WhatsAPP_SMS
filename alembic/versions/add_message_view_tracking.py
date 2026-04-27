"""Add message view tracking fields.

Revision ID: 001_add_view_tracking
Revises: 
Create Date: 2026-04-27

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '001_add_view_tracking'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('telegram_messages', 
        sa.Column('viewed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('telegram_messages',
        sa.Column('replied_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('telegram_messages',
        sa.Column('check_until', sa.DateTime(timezone=True), nullable=True))

def downgrade():
    op.drop_column('telegram_messages', 'check_until')
    op.drop_column('telegram_messages', 'replied_at')
    op.drop_column('telegram_messages', 'viewed_at')
