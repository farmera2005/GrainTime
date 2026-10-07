"""Operating hours, public aggregates table and the read-only public role

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

PUBLIC_ROLE = "graintime_public"


def upgrade() -> None:
    op.add_column("sites", sa.Column("hours", postgresql.JSONB(), nullable=True))
    op.create_table(
        "public_site_status",
        sa.Column("code", sa.String(20), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("address", sa.Text()),
        sa.Column("map_url", sa.Text()),
        sa.Column("open_state", sa.Boolean()),
        sa.Column("hours_text", sa.String(80)),
        sa.Column("traffic", sa.String(10), nullable=False),
        sa.Column("minutes", sa.Integer()),
        sa.Column("level", sa.String(10)),
        sa.Column("trucks_on_site", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data_as_of", sa.DateTime(timezone=True)),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stale_after_min", sa.Integer(), nullable=False),
        sa.CheckConstraint("traffic IN ('ok','light','none')", name="ck_public_traffic"),
    )
    # The public service's role: SELECT on this one table and nothing else.
    # It is created without login; `graintime.common.public_role` (run by the
    # migrate service) sets its generated password and enables login.
    op.execute(f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PUBLIC_ROLE}') THEN
            CREATE ROLE {PUBLIC_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
          END IF;
        END $$;
    """)
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {PUBLIC_ROLE}")
    op.execute(f"GRANT SELECT ON public_site_status TO {PUBLIC_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON public_site_status FROM {PUBLIC_ROLE}")
    op.drop_table("public_site_status")
    op.drop_column("sites", "hours")
    # The role itself is cluster-wide and left in place (harmless without grants).
