"""Customizable dashboards per user

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filters", postgresql.JSONB(), nullable=False),
        sa.Column("widgets", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_dashboards_user", "dashboards", ["user_id", "position"])
    op.create_index("ix_tickets_site_dir_outbound", "tickets", ["site_id", "direction", "outbound_at"])


def downgrade() -> None:
    op.drop_index("ix_tickets_site_dir_outbound", "tickets")
    op.drop_table("dashboards")
