"""Scheduled reports and their run history

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("frequency", sa.String(10), nullable=False),
        sa.Column("weekday", sa.Integer()),
        sa.Column("month_day", sa.Integer()),
        sa.Column("send_time", sa.String(5), nullable=False),
        sa.Column("period", sa.String(20), nullable=False),
        sa.Column("compare", sa.String(10), nullable=False, server_default="previous"),
        sa.Column("site_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("direction", sa.String(10), nullable=False, server_default="received"),
        sa.Column("include_trucks", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recipients", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("run_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_status", sa.String(10)),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("frequency IN ('daily','weekly','monthly')", name="ck_report_schedules_frequency"),
    )
    op.create_index("ix_report_schedules_due", "report_schedules", ["enabled", "next_run_at"])
    op.create_table(
        "report_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("report_schedules.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("trigger", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("period_from", sa.Date()),
        sa.Column("period_to", sa.Date()),
        sa.Column("recipients", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_report_runs_schedule", "report_runs", ["schedule_id", "started_at"])


def downgrade() -> None:
    op.drop_table("report_runs")
    op.drop_table("report_schedules")
