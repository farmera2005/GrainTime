from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,19}$")


def validate_host(v: str) -> str:
    v = v.strip()
    if "\\" in v:
        raise ValueError("Enter the host only, not HOST\\INSTANCE. Named instances use dynamic "
                         "ports; give the instance a static TCP port and enter that port below.")
    if "," in v or ":" in v and not _is_ipv6(v):
        raise ValueError("Enter the port in the Port field, not in the host.")
    if not (HOST_RE.match(v) or _is_ip(v)):
        raise ValueError("Not a valid host name or IP address.")
    return v


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def _is_ipv6(v: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(v), ipaddress.IPv6Address)
    except ValueError:
        return False


def validate_code(v: str) -> str:
    v = v.strip().upper()
    if not CODE_RE.match(v):
        raise ValueError("2-20 characters: letters, digits, - and _ (e.g. CELINA).")
    return v


def validate_url(v: str | None) -> str | None:
    if v and not re.match(r"^https?://", v):
        raise ValueError("Must start with http:// or https://")
    return v or None


# --- setup / auth ---------------------------------------------------------- #

class AdminCreate(BaseModel):
    username: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=200)

    @field_validator("username")
    @classmethod
    def _u(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9._@-]+$", v):
            raise ValueError("Use letters, digits, and . _ @ - only.")
        return v


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    auth_source: str


# --- global defaults ------------------------------------------------------- #

class Defaults(BaseModel):
    poll_interval_s: int = Field(ge=15, le=3600)
    threshold_green_max_min: int = Field(ge=1, le=600)
    threshold_yellow_max_min: int = Field(ge=1, le=600)
    recent_window_min: int = Field(ge=10, le=240)
    open_ticket_cutoff_hours: int = Field(ge=1, le=48)
    duration_ceiling_hours: int = Field(ge=1, le=48)
    backfill_default_days: int = Field(ge=1, le=3650)
    public_stale_after_min: int = Field(ge=1, le=1440)
    public_min_trucks: int = Field(ge=1, le=50)

    @model_validator(mode="after")
    def _order(self):
        if self.threshold_green_max_min >= self.threshold_yellow_max_min:
            raise ValueError("The green limit must be lower than the yellow limit.")
        return self


# --- sites ----------------------------------------------------------------- #

class ConnectionFields(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    # Write-only. Optional when testing a saved site (the stored one is used).
    password: str | None = Field(default=None, max_length=256)
    encrypt: Literal["yes", "no", "strict"] = "yes"
    trust_server_certificate: bool = False

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return validate_host(v)

    def __repr__(self) -> str:
        return f"ConnectionFields(host={self.host!r}, port={self.port}, database={self.database!r})"


class SiteCreate(ConnectionFields):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=2, max_length=20)
    address: str | None = Field(default=None, max_length=500)
    map_url: str | None = Field(default=None, max_length=1000)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        return validate_code(v)

    @field_validator("map_url")
    @classmethod
    def _url(cls, v):
        return validate_url(v)


class SiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = None
    address: str | None = None
    map_url: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, min_length=1, max_length=128)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: str | None = Field(default=None, max_length=256)  # blank/None = keep
    encrypt: Literal["yes", "no", "strict"] | None = None
    trust_server_certificate: bool | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, v):
        return None if v is None else validate_code(v)

    @field_validator("host")
    @classmethod
    def _host(cls, v):
        return None if v is None else validate_host(v)

    @field_validator("map_url")
    @classmethod
    def _url(cls, v):
        return validate_url(v)


class SiteOut(BaseModel):
    id: int
    name: str
    code: str
    address: str | None
    map_url: str | None
    host: str
    port: int
    database: str
    username: str
    has_password: bool
    encrypt: str
    trust_server_certificate: bool
    polling_enabled: bool
    show_on_dashboard: bool
    show_on_public: bool
    archived: bool


class TestConnectionIn(BaseModel):
    site_id: int | None = None
    connection: ConnectionFields | None = None

    @model_validator(mode="after")
    def _one(self):
        if self.site_id is None and self.connection is None:
            raise ValueError("Give a saved site or connection details.")
        if self.site_id is None and not (self.connection and self.connection.password):
            raise ValueError("Enter the password to test an unsaved connection.")
        return self


class DiscoveryIn(BaseModel):
    extra_tables: list[str] = Field(default_factory=list, max_length=20)
    include_samples: bool = True
    mask: bool = True
