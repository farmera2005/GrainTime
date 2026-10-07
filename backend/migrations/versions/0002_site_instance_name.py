"""sites.instance_name: optional named instance used to look up the port

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("instance_name", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "instance_name")
