"""Mapping profiles, tickets, per-site collector state; seed the CompuWeigh GMS profile

Revision ID: 0004
Revises: 0003
"""
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mapping_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.Text()),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.add_column("sites", sa.Column("mapping_profile_id", sa.Integer(),
                                     sa.ForeignKey("mapping_profiles.id", ondelete="RESTRICT")))
    op.create_table(
        "tickets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_ticket_id", sa.String(64), nullable=False),
        sa.Column("ticket_number", sa.String(64)),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("raw_status", sa.String(32)),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("transaction_type", sa.String(64)),
        sa.Column("commodity", sa.String(100)),
        sa.Column("inbound_at", sa.DateTime(timezone=True)),
        sa.Column("outbound_at", sa.DateTime(timezone=True)),
        sa.Column("duration_s", sa.Integer()),
        sa.Column("single_weigh", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_created_at", sa.DateTime(timezone=True)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("site_id", "source_ticket_id", name="uq_tickets_site_source"),
    )
    op.create_index("ix_tickets_site_outbound", "tickets", ["site_id", "outbound_at"])
    op.create_index("ix_tickets_site_inbound", "tickets", ["site_id", "inbound_at"])
    op.create_index("ix_tickets_open", "tickets", ["site_id", "inbound_at"],
                    postgresql_where=sa.text("status = 'open'"))
    op.create_table(
        "site_collector_state",
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("high_water_mark", sa.String(64)),
        sa.Column("last_poll_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("next_poll_at", sa.DateTime(timezone=True)),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", postgresql.JSONB()),
        sa.Column("rows_last_poll", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_recheck_at", sa.DateTime(timezone=True)),
        sa.Column("profile_fingerprint", sa.String(64)),
    )
    from graintime.common.profiles import COMPUWEIGH_GMS
    profiles = sa.table("mapping_profiles", sa.column("name", sa.String),
                        sa.column("description", sa.Text), sa.column("config", postgresql.JSONB))
    op.bulk_insert(profiles, [{
        "name": "CompuWeigh GMS",
        "description": "CompuWeigh GMS (TransactionID + TransactionLog), from the first site's "
                       "discovery. Grain Inbound (TRUCKIN) only. Statuses 2 and 4 are treated as "
                       "voided until confirmed.",
        "config": json.loads(json.dumps(COMPUWEIGH_GMS)),
    }])


def downgrade() -> None:
    op.drop_table("site_collector_state")
    op.drop_table("tickets")
    op.drop_column("sites", "mapping_profile_id")
    op.drop_table("mapping_profiles")
