"""Central store schema (PostgreSQL). Migrations live in backend/migrations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer,
                        LargeBinary, String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    # NULL for single sign-on users; local accounts (break-glass admin) only.
    password_hash: Mapped[str | None] = mapped_column(Text)
    auth_source: Mapped[str] = mapped_column(String(20), default="local")
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("role IN ('viewer','admin')", name="ck_users_role"),)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    """Application settings edited in the browser (setup state, global defaults)."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class MappingProfile(Base):
    """How a site's scale database maps to tickets (see common/profiles.py)."""
    __tablename__ = "mapping_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(20), unique=True)
    address: Mapped[str | None] = mapped_column(Text)
    map_url: Mapped[str | None] = mapped_column(Text)
    # Connection. Host + static port, never instance-name resolution.
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    # Optional named instance (e.g. SQLEXPRESS). Only used to look up the port via
    # SQL Server Browser when testing, or when the saved port stops answering.
    instance_name: Mapped[str | None] = mapped_column(String(128))
    database_name: Mapped[str] = mapped_column(String(128))
    # "sql": SQL Server login. "windows": domain account, signed in with NTLM.
    auth_method: Mapped[str] = mapped_column(String(10), default="sql", server_default="sql")
    domain: Mapped[str | None] = mapped_column(String(100))
    username: Mapped[str] = mapped_column(String(128))
    # Fernet ciphertext. Write-only: never returned by the API, never logged.
    password_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    encrypt: Mapped[str] = mapped_column(String(10), default="yes")
    trust_server_certificate: Mapped[bool] = mapped_column(Boolean, default=False)
    mapping_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("mapping_profiles.id", ondelete="RESTRICT"))
    poll_interval_s: Mapped[int | None] = mapped_column(Integer)  # NULL = global default
    polling_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    show_on_dashboard: Mapped[bool] = mapped_column(Boolean, default=True)
    show_on_public: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
    __table_args__ = (
        CheckConstraint("port BETWEEN 1 AND 65535", name="ck_sites_port"),
        CheckConstraint("encrypt IN ('yes','no','strict')", name="ck_sites_encrypt"),
        CheckConstraint("auth_method IN ('sql','windows')", name="ck_sites_auth_method"),
    )


class CollectorJob(Base):
    """Work the api hands to the collector, the only component that talks to
    site databases (test connection, discovery; later preview and backfill)."""
    __tablename__ = "collector_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    # Connection details for testing an unsaved site. The password inside is
    # Fernet ciphertext and is wiped when the job finishes.
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')",
                        name="ck_jobs_status"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_name: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)


class Ticket(Base):
    """One truck ticket, normalized. No customer, driver, plate, weight or price data."""
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    source_ticket_id: Mapped[str] = mapped_column(String(64))
    ticket_number: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12))          # open / completed / voided / unknown
    raw_status: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(10))       # received / shipped
    transaction_type: Mapped[str | None] = mapped_column(String(64))
    commodity: Mapped[str | None] = mapped_column(String(100))
    inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[int | None] = mapped_column(Integer)
    single_weigh: Mapped[bool] = mapped_column(Boolean, default=False)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("site_id", "source_ticket_id", name="uq_tickets_site_source"),)


class SiteCollectorState(Base):
    __tablename__ = "site_collector_state"
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True)
    high_water_mark: Mapped[str | None] = mapped_column(String(64))
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[dict | None] = mapped_column(JSONB)
    rows_last_poll: Mapped[int] = mapped_column(Integer, default=0)
    rows_total: Mapped[int] = mapped_column(BigInteger, default=0)
    last_recheck_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_fingerprint: Mapped[str | None] = mapped_column(String(64))


class Dashboard(Base):
    """A user's customizable dashboard: shared filters and an ordered widget list."""
    __tablename__ = "dashboards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer, default=0)
    filters: Mapped[dict] = mapped_column(JSONB)
    widgets: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
