"""Agent sites (GrainTime Agent on the scale PC) and the AgVantagePC profile

Revision ID: 0008
Revises: 0007
"""
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

CONN_COLS = ("host", "port", "database_name", "username", "password_encrypted")


def upgrade() -> None:
    op.add_column("sites", sa.Column("source", sa.String(10), nullable=False, server_default="sql"))
    for c in CONN_COLS:
        op.alter_column("sites", c, nullable=True)
    op.create_check_constraint("ck_sites_source", "sites", "source IN ('sql','agent')")
    op.create_check_constraint(
        "ck_sites_sql_connection", "sites",
        "source <> 'sql' OR (host IS NOT NULL AND port IS NOT NULL AND database_name IS NOT NULL "
        "AND username IS NOT NULL AND password_encrypted IS NOT NULL)")
    op.create_table(
        "site_agents",
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key_id", sa.String(32), unique=True),
        sa.Column("secret_encrypted", sa.LargeBinary()),
        sa.Column("pairing_code_hash", sa.String(64), unique=True),
        sa.Column("pairing_expires_at", sa.DateTime(timezone=True)),
        sa.Column("paired_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_request_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("version", sa.String(40)),
        sa.Column("hostname", sa.String(100)),
        sa.Column("windows_user", sa.String(150)),
        sa.Column("status", postgresql.JSONB()),
        sa.Column("clock_skew_s", sa.Integer()),
    )
    # The AgVantagePC profile (kept in sync with graintime.common.profiles.AGVANTAGE_PC).
    from graintime.common.profiles import AGVANTAGE_PC
    op.get_bind().execute(sa.text(
        "INSERT INTO mapping_profiles (name, description, config) "
        "SELECT 'AgVantagePC', :d, CAST(:c AS jsonb) "
        "WHERE NOT EXISTS (SELECT 1 FROM mapping_profiles WHERE name = 'AgVantagePC')"),
        {"d": "AgVantagePC scale PCs (LOCALSET.MDF), read by the GrainTime Agent. Purchases "
              "received, sales shipped, transfers left out.",
         "c": json.dumps(AGVANTAGE_PC)})


def downgrade() -> None:
    op.execute("DELETE FROM mapping_profiles WHERE name = 'AgVantagePC' AND id NOT IN "
               "(SELECT mapping_profile_id FROM sites WHERE mapping_profile_id IS NOT NULL)")
    op.drop_table("site_agents")
    op.execute("DELETE FROM sites WHERE source = 'agent'")
    op.drop_constraint("ck_sites_sql_connection", "sites")
    op.drop_constraint("ck_sites_source", "sites")
    for c in CONN_COLS:
        op.alter_column("sites", c, nullable=False)
    op.drop_column("sites", "source")
