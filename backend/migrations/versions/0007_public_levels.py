"""Time-on-site level limits on the public aggregates (for the page's gauge and legend)

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("public_site_status", sa.Column("green_max_min", sa.Integer()))
    op.add_column("public_site_status", sa.Column("yellow_max_min", sa.Integer()))


def downgrade() -> None:
    op.drop_column("public_site_status", "yellow_max_min")
    op.drop_column("public_site_status", "green_max_min")
