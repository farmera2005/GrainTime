"""Mapping profiles: how a site's scale database maps to GrainTime tickets.

A profile is structured fields, never free-form SQL: table and column names
(validated identifiers), value translations, and filters. The collector builds
every query from it itself, so each one is a bounded, read-only SELECT.

Three shapes are supported:

* weigh times as columns on the ticket table (`inbound_column` /
  `outbound_column`),
* two weigh time columns whose order depends on direction
  (`weigh_pair_columns`, e.g. AgVantage gross and tare dates): the earlier is
  the inbound weigh, the later the outbound, or
* weigh times from a child table of weigh steps (`weigh_steps`), as in
  CompuWeigh GMS (TransactionID + TransactionLog): inbound = first recorded
  weigh, outbound = last, and a ticket with only one kind of weight
  (gross or tare) is a single-weigh / stored-tare ticket.

Direction, type and commodity come either from lookup tables (GMS) or straight
from columns on the ticket row (AgVantage).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#@]{0,127}$")
TABLE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_$#@]{0,127}\.)?[A-Za-z_][A-Za-z0-9_$#@]{0,127}$")

TicketStatus = Literal["open", "completed", "voided"]
Direction = Literal["received", "shipped"]


def _ident(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    v = v.strip()
    if not IDENT_RE.match(v):
        raise ValueError(f"'{v}' is not a valid column name.")
    return v


def _table(v: str) -> str:
    v = v.strip()
    if not TABLE_RE.match(v):
        raise ValueError(f"'{v}' is not a valid table name (use Table or schema.Table).")
    return v


class Lookup(BaseModel):
    """A small lookup table joined by key (e.g. TransactionType, Product)."""
    table: str
    key_column: str
    code_column: str
    description_column: str | None = None
    direction_column: str | None = None   # transaction types: value translated via direction_map

    @field_validator("table")
    @classmethod
    def _t(cls, v):
        return _table(v)

    @field_validator("key_column", "code_column", "description_column", "direction_column")
    @classmethod
    def _c(cls, v):
        return _ident(v)


class WeighSteps(BaseModel):
    """Child table with one row per weigh (CompuWeigh GMS: TransactionLog)."""
    table: str
    ticket_fk_column: str
    time_column: str                        # when the weight was recorded
    weight_type_column: str                 # rows where this is NULL are not weighs
    status_column: str | None = None
    status_done_value: str | None = None    # only steps with this status count

    @field_validator("table")
    @classmethod
    def _t(cls, v):
        return _table(v)

    @field_validator("ticket_fk_column", "time_column", "weight_type_column", "status_column")
    @classmethod
    def _c(cls, v):
        return _ident(v)


class ProfileConfig(BaseModel):
    # Ticket table
    ticket_table: str
    id_column: str                          # unique, stable ticket key
    high_water_column: str                  # ever-increasing (identity) column for new rows
    ticket_number_column: str | None = None # printed ticket number (shown in Preview only)
    status_column: str
    status_map: dict[str, TicketStatus]     # raw value -> open / completed / voided
    void_column: str | None = None          # optional separate void flag (truthy = voided)
    created_column: str                     # indexed creation time: look-back and backfill
    completed_column: str | None = None     # indexed completion time: look-back
    type_column: str | None = None
    product_column: str | None = None
    parent_column: str | None = None        # linked (split) tickets: counted once, with parent

    # Weigh times: columns on the ticket table, or a weigh-steps child table
    inbound_column: str | None = None
    outbound_column: str | None = None
    weigh_pair_columns: list[str] | None = None   # two columns; earlier = inbound, later = outbound
    weigh_steps: WeighSteps | None = None

    # Direction straight from a ticket column (translated by direction_map)
    direction_column: str | None = None

    # Weights keyed in by hand (truthy = manual). "keep" counts the ticket as
    # usual; "exclude" leaves it out of statistics and counts it like a
    # single-weigh ticket, because a keyed weight's time is when it was typed.
    manual_columns: list[str] = Field(default_factory=list, max_length=4)
    manual_rule: Literal["keep", "exclude"] = "keep"

    # Lookups and translations
    type_lookup: Lookup | None = None
    direction_map: dict[str, Direction] = Field(default_factory=dict)
    included_type_codes: list[str] = Field(default_factory=list)   # empty = all types
    product_lookup: Lookup | None = None

    # Optional fixed filter, e.g. one facility in a shared database
    filter_column: str | None = None
    filter_value: str | None = None

    # Polling windows. "time": re-read tickets created/completed recently
    # (needs indexed time columns). "recent_rows": re-read the newest N tickets
    # by key (an index seek), for databases whose time columns are not indexed.
    lookback_mode: Literal["time", "recent_rows"] = "time"
    lookback_rows: int = Field(default=200, ge=10, le=2000)
    lookback_hours: int = Field(default=24, ge=1, le=168)
    completed_lookback_hours: int = Field(default=2, ge=1, le=48)
    nightly_recheck_days: int = Field(default=7, ge=0, le=31)

    # Agent sites only: the database on the PC whose name ends with this
    # (AgVantagePC: the attached file LOCALSET.MDF).
    agent_database_match: str | None = Field(default=None, max_length=200)

    @field_validator("ticket_table")
    @classmethod
    def _t(cls, v):
        return _table(v)

    @field_validator("id_column", "high_water_column", "ticket_number_column", "status_column",
                     "void_column", "created_column", "completed_column", "type_column",
                     "product_column", "parent_column", "inbound_column", "outbound_column",
                     "filter_column", "direction_column")
    @classmethod
    def _c(cls, v):
        return _ident(v)

    @field_validator("weigh_pair_columns")
    @classmethod
    def _pair(cls, v):
        if v is None:
            return None
        cols = [_ident(c) for c in v if c and str(c).strip()]
        if not cols:
            return None
        if len(cols) != 2 or cols[0] == cols[1]:
            raise ValueError("Give exactly two different weigh time columns.")
        return cols

    @field_validator("manual_columns")
    @classmethod
    def _manual(cls, v):
        return [_ident(c) for c in v if c and str(c).strip()]

    @field_validator("agent_database_match")
    @classmethod
    def _dbmatch(cls, v):
        v = (v or "").strip()
        if v and any(ch in v for ch in "%_[]*?'\"\x00"):
            raise ValueError("Give the end of the database name as plain text, e.g. LOCALSET.MDF.")
        return v or None

    @field_validator("status_map", "direction_map")
    @classmethod
    def _keys(cls, v: dict) -> dict:
        return {str(k).strip(): val for k, val in v.items()}

    @field_validator("included_type_codes")
    @classmethod
    def _codes(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c and c.strip()]

    @model_validator(mode="after")
    def _shape(self):
        has_cols = bool(self.inbound_column and self.outbound_column)
        shapes = sum([has_cols, bool(self.weigh_pair_columns), self.weigh_steps is not None])
        if shapes == 0:
            raise ValueError("Give inbound and outbound weigh time columns, two weigh time columns "
                             "(earlier = inbound), or a weigh steps table.")
        if shapes > 1:
            raise ValueError("Use only one way of reading weigh times.")
        if not self.status_map:
            raise ValueError("Translate at least one status value.")
        if self.included_type_codes and not self.type_column:
            raise ValueError("Filtering by transaction type needs the type column.")
        if self.direction_column and not self.direction_map:
            raise ValueError("Translate the direction values (e.g. I = received, O = shipped).")
        if self.type_lookup and self.type_lookup.direction_column and not self.direction_map:
            raise ValueError("Translate the direction values (e.g. 1 = received, 2 = shipped).")
        if bool(self.filter_column) != bool(self.filter_value not in (None, "")):
            raise ValueError("Give both the filter column and its value, or neither.")
        return self

    def time_columns(self) -> list[str]:
        """Columns holding local date/times (decoded from text on agent uploads)."""
        return [c for c in (self.created_column, self.completed_column, self.inbound_column,
                            self.outbound_column, *(self.weigh_pair_columns or [])) if c]

    def agent_compatible(self) -> str | None:
        """Agents send ticket rows only; lookups and weigh-step tables are read by
        the collector. Returns why a profile cannot be used by an agent site."""
        if self.weigh_steps is not None:
            return "it reads weigh times from a weigh steps table"
        if self.type_lookup or self.product_lookup:
            return "it uses lookup tables"
        if self.parent_column:
            return "it merges linked (split) tickets"
        if not self.agent_database_match:
            return "it does not say which database on the PC to read (agent database)"
        return None

    def ticket_columns(self) -> list[str]:
        cols = [self.id_column, self.high_water_column, self.ticket_number_column,
                self.status_column, self.void_column, self.created_column,
                self.completed_column, self.type_column, self.product_column,
                self.parent_column, self.inbound_column, self.outbound_column,
                *(self.weigh_pair_columns or []), self.direction_column, *self.manual_columns,
                self.filter_column]
        out: list[str] = []
        for c in cols:
            if c and c not in out:
                out.append(c)
        return out


# CompuWeigh GMS, as discovered at the first site (SQL Server 2019, database GMS)
# and confirmed: TRUCKIN only; statuses 2 and 4 treated as voided until confirmed.
COMPUWEIGH_GMS = {
    "ticket_table": "dbo.TransactionID",
    "id_column": "tid_pk",
    "high_water_column": "tid_pk",
    "ticket_number_column": "tid_ticket",
    "status_column": "tid_status",
    "status_map": {"0": "open", "1": "completed", "2": "voided", "4": "voided"},
    "created_column": "tid_createdate",
    "completed_column": "tid_completetime",
    "type_column": "tid_trtfk",
    "product_column": "tid_prdfk",
    "parent_column": "tid_parenttidfk",
    "weigh_steps": {
        "table": "dbo.TransactionLog",
        "ticket_fk_column": "tlg_tidfk",
        "time_column": "tlg_FinishTime",
        "weight_type_column": "tlg_WeightType",
        "status_column": "tlg_status",
        "status_done_value": "1",
    },
    "type_lookup": {"table": "dbo.TransactionType", "key_column": "trt_pk",
                    "code_column": "trt_code", "description_column": "trt_description",
                    "direction_column": "trt_direction"},
    "direction_map": {"1": "received", "2": "shipped"},
    "included_type_codes": ["TRUCKIN"],
    "product_lookup": {"table": "dbo.Product", "key_column": "prd_pk",
                       "code_column": "prd_code", "description_column": "prd_description"},
    "lookback_hours": 24,
    "completed_lookback_hours": 2,
    "nightly_recheck_days": 7,
}


# AgVantagePC: the scale PC's local working database (LOCALSET.MDF, attached by
# a SQL Server Express user instance), mapped from the New Weston PC's
# discovery and confirmed: APP (Applied) counts as completed; purchases (P) are
# received and sales (S) shipped; transfers (T) are left out; manually keyed
# weights are kept. Read through the GrainTime Agent on the PC. The table has
# no indexed time column, so polls re-read the newest tickets by key.
AGVANTAGE_PC = {
    "ticket_table": "dbo.scale_ticket",
    "id_column": "st_load",
    "high_water_column": "st_load",
    "ticket_number_column": "st_scaleticket",
    "status_column": "st_status",
    "status_map": {"CPT": "completed", "APP": "completed", "V": "voided", "TAR": "open"},
    "created_column": "st_date",
    "completed_column": "st_completedDate",
    "type_column": "st_type",
    "included_type_codes": ["P", "S"],
    "product_column": "st_desc",
    "weigh_pair_columns": ["st_grsdate", "st_tardate"],
    "direction_column": "st_inout",
    "direction_map": {"I": "received", "O": "shipped"},
    "manual_columns": ["st_grsmanual", "st_tarmanual"],
    "manual_rule": "keep",
    "lookback_mode": "recent_rows",
    "lookback_rows": 200,
    "lookback_hours": 24,
    "completed_lookback_hours": 2,
    "nightly_recheck_days": 7,
    "agent_database_match": "LOCALSET.MDF",
}
