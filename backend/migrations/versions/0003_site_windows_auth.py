"""sites.auth_method and sites.domain: Windows (domain) account sign-in

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("auth_method", sa.String(length=10), nullable=False,
                                     server_default="sql"))
    op.add_column("sites", sa.Column("domain", sa.String(length=100), nullable=True))
    op.create_check_constraint("ck_sites_auth_method", "sites", "auth_method IN ('sql','windows')")


def downgrade() -> None:
    op.drop_constraint("ck_sites_auth_method", "sites")
    op.drop_column("sites", "domain")
    op.drop_column("sites", "auth_method")
