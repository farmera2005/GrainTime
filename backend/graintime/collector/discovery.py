"""Schema discovery for one site database (Phase 0), run by the collector.

Started from the setup wizard or the admin panel; the api queues a
`discovery` job and the collector runs it. Reports what is needed to design
the mapping profile: version/edition, tables and columns, metadata row counts,
indexes, foreign keys, triggers, a few recent sample rows from ticket-like
tables (name/plate/address-like text masked), value distributions for
status/void/direction-like columns from a bounded recent sample, and the date
range of indexed date columns.

Safety: SELECT only; every data query is bounded with TOP; row counts come
from partition metadata; no COUNT(*) over whole tables, no unbounded scans.
The connection itself comes from sitedb.connect (timeouts, READ UNCOMMITTED).
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .sitedb import tls_assessment

PAUSE_BETWEEN_QUERIES_S = 0.1

# Table names that suggest scale tickets, weighs, or the lookups they reference.
CANDIDATE_TABLE_RE = re.compile(
    r"ticket|weigh|scale|load|trans|receipt|inbound|outbound|truck|settle|tare|"
    r"commod|grain|crop|direction|status|void|shipment|receiv",
    re.IGNORECASE,
)
# Columns worth a value distribution (status codes, void flags, direction, commodity).
DISTRIBUTION_COL_RE = re.compile(
    r"status|void|cancel|delet|type|direction|dir$|inout|in_out|ship|recv|receiv|"
    r"flag|commod|crop|grain|kind|mode|state|complete|closed|open|posted",
    re.IGNORECASE,
)
# Columns likely to hold personal or customer data; string values are masked.
PII_COL_RE = re.compile(
    r"name|driver|plate|licen|addr|street|city|zip|phone|email|cust|farm|grower|"
    r"producer|owner|hauler|carrier|trucker|vendor|payee|bank|ssn|taxid|tax_id|"
    r"note|comment|memo|remark|sign",
    re.IGNORECASE,
)
# ...unless the column name says it is a code/lookup rather than a person.
NOT_PII_COL_RE = re.compile(
    r"commod|crop|grain|status|type|direction|grade|bin|product|item|location|"
    r"site|station|scale|elevator|description|desc$|code$|unit|user|operator",
    re.IGNORECASE,
)
DATE_TYPES = {"datetime", "datetime2", "smalldatetime", "date", "datetimeoffset", "time"}
# Types never pulled into samples (large objects, binaries, spatial).
SKIP_SAMPLE_TYPES = {"image", "text", "ntext", "xml", "binary", "varbinary",
                     "geography", "geometry", "hierarchyid", "sql_variant"}
GROUPABLE_TYPES = {"bit", "tinyint", "smallint", "int", "bigint", "char", "nchar",
                   "varchar", "nvarchar", "decimal", "numeric"}


def jsonable(v):
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


def q(conn, sql: str, params=()):
    time.sleep(PAUSE_BETWEEN_QUERIES_S)
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, (jsonable(v) for v in r))) for r in cur.fetchall()]
        return cols, rows
    finally:
        cur.close()


def safe(errors: list, label: str, fn, default=None):
    try:
        return fn()
    except Exception as exc:  # report and keep going; partial output is still useful
        errors.append({"step": label, "error": str(exc).splitlines()[0][:500]})
        return default


def qi(name: str) -> str:
    """Quote an identifier taken from the catalog."""
    return "[" + name.replace("]", "]]") + "]"


# --------------------------------------------------------------------------- #
# Discovery steps
# --------------------------------------------------------------------------- #

def server_info(conn):
    _, rows = q(conn, """
        SELECT
          CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128))      AS product_version,
          CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(128))        AS product_level,
          CAST(SERVERPROPERTY('ProductUpdateLevel') AS nvarchar(128))  AS product_update_level,
          CAST(SERVERPROPERTY('Edition') AS nvarchar(128))             AS edition,
          CAST(SERVERPROPERTY('EngineEdition') AS int)                 AS engine_edition,
          CAST(SERVERPROPERTY('ServerName') AS nvarchar(256))          AS server_name,
          CAST(SERVERPROPERTY('InstanceName') AS nvarchar(256))        AS instance_name,
          CAST(SERVERPROPERTY('Collation') AS nvarchar(128))           AS server_collation,
          DB_NAME()                                                    AS database_name,
          CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS nvarchar(128)) AS database_collation,
          (SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME()) AS compatibility_level,
          SYSDATETIME()                                                AS server_local_time,
          SYSUTCDATETIME()                                             AS server_utc_time,
          DATENAME(TZOFFSET, SYSDATETIMEOFFSET())                      AS server_utc_offset,
          @@VERSION                                                    AS version_string
    """)
    return rows[0]


def connection_info(conn):
    # Needs VIEW SERVER STATE on most versions; optional.
    _, rows = q(conn, """
        SELECT encrypt_option, protocol_type, net_transport, auth_scheme
        FROM sys.dm_exec_connections WHERE session_id = @@SPID
    """)
    return rows[0] if rows else None


def tables_and_columns(conn):
    _, tables = q(conn, """
        SELECT TABLE_SCHEMA AS [schema], TABLE_NAME AS [name], TABLE_TYPE AS [type]
        FROM INFORMATION_SCHEMA.TABLES
        ORDER BY TABLE_SCHEMA, TABLE_NAME
    """)
    _, cols = q(conn, """
        SELECT c.TABLE_SCHEMA AS [schema], c.TABLE_NAME AS [table], c.ORDINAL_POSITION AS ordinal,
               c.COLUMN_NAME AS [column], c.DATA_TYPE AS data_type,
               c.CHARACTER_MAXIMUM_LENGTH AS max_length, c.NUMERIC_PRECISION AS [precision],
               c.NUMERIC_SCALE AS scale, c.IS_NULLABLE AS nullable,
               c.COLUMN_DEFAULT AS default_value,
               COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)),
                              c.COLUMN_NAME, 'IsIdentity') AS is_identity,
               COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)),
                              c.COLUMN_NAME, 'IsComputed') AS is_computed
        FROM INFORMATION_SCHEMA.COLUMNS c
        ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
    """)
    return tables, cols


def row_counts(conn):
    # Partition metadata, not COUNT(*): no table is read.
    _, rows = q(conn, """
        SELECT s.name AS [schema], t.name AS [table], SUM(p.rows) AS approx_rows
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
        GROUP BY s.name, t.name
        ORDER BY s.name, t.name
    """)
    return rows


def indexes(conn):
    _, rows = q(conn, """
        SELECT s.name AS [schema], o.name AS [table], i.name AS index_name, i.type_desc,
               i.is_unique, i.is_primary_key, ic.key_ordinal, ic.is_included_column,
               ic.is_descending_key, c.name AS [column]
        FROM sys.indexes i
        JOIN sys.objects o ON o.object_id = i.object_id AND o.type IN ('U', 'V')
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE i.type > 0
        ORDER BY s.name, o.name, i.index_id, ic.is_included_column, ic.key_ordinal
    """)
    return rows


def foreign_keys(conn):
    _, rows = q(conn, """
        SELECT fk.name AS fk_name,
               SCHEMA_NAME(pt.schema_id) AS [schema], pt.name AS [table], pc.name AS [column],
               SCHEMA_NAME(rt.schema_id) AS ref_schema, rt.name AS ref_table, rc.name AS ref_column
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.tables pt ON pt.object_id = fkc.parent_object_id
        JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
        JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
        JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
        ORDER BY pt.name, fk.name
    """)
    return rows


def triggers(conn):
    _, rows = q(conn, """
        SELECT SCHEMA_NAME(t.schema_id) AS [schema], t.name AS [table], tr.name AS trigger_name,
               tr.is_disabled
        FROM sys.triggers tr
        JOIN sys.tables t ON t.object_id = tr.parent_id
        ORDER BY t.name, tr.name
    """)
    return rows


def pick_candidates(tables, cols_by_table, counts, extra):
    scored = []
    for t in tables:
        key = (t["schema"], t["name"])
        cols = cols_by_table.get(key, [])
        n_dates = sum(1 for c in cols if c["data_type"] in DATE_TYPES)
        score = 0
        if CANDIDATE_TABLE_RE.search(t["name"]):
            score += 2
        if n_dates >= 2:
            score += 2
        if any(re.search(r"weigh|gross|tare|net", c["column"], re.I) for c in cols):
            score += 1
        if f"{t['schema']}.{t['name']}".lower() in extra or t["name"].lower() in extra:
            score += 100
        if score >= 2:
            scored.append((score, counts.get(key, 0) or 0, key))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [k for _, _, k in scored]


def clustered_key(idx_rows, key):
    cols = [r for r in idx_rows
            if (r["schema"], r["table"]) == key and r["type_desc"] == "CLUSTERED"
            and not r["is_included_column"]]
    cols.sort(key=lambda r: r["key_ordinal"])
    return [r["column"] for r in cols]


def leading_index_columns(idx_rows, key):
    return {r["column"] for r in idx_rows
            if (r["schema"], r["table"]) == key and r["key_ordinal"] == 1
            and not r["is_included_column"]}


FREE_TEXT_MAX = 16


def mask_value(col, v, enabled):
    """Mask text that may identify a person, truck or farm. Columns named like
    names/plates/addresses are always masked; in other columns, free text
    (contains a space, or longer than a short code) is masked too, since names
    such as 'SMITH FARMS KENWORTH' can sit in generically named columns.
    Codes, statuses, types and descriptions are kept: the mapping needs them."""
    if not enabled or v is None or not isinstance(v, str):
        return v
    safe_col = bool(NOT_PII_COL_RE.search(col))
    if PII_COL_RE.search(col) and not safe_col:
        return f"<masked len={len(v)}>"
    if not safe_col and (" " in v.strip() or len(v) > FREE_TEXT_MAX):
        return f"<masked len={len(v)}>"
    return v


def sample_table(conn, key, cols, idx_rows, n, mask):
    schema, name = key
    fq = f"{qi(schema)}.{qi(name)}"
    selectable, omitted = [], []
    for c in cols:
        lob = c["max_length"] == -1
        if c["data_type"] in SKIP_SAMPLE_TYPES or lob:
            omitted.append(c["column"])
        else:
            selectable.append(c["column"])
    order_cols = clustered_key(idx_rows, key)
    order_sql = (" ORDER BY " + ", ".join(qi(c) + " DESC" for c in order_cols)) if order_cols else ""
    col_sql = ", ".join(qi(c) for c in selectable)
    sql = f"SELECT TOP ({int(n)}) {col_sql} FROM {fq}{order_sql}"
    _, rows = q(conn, sql)
    # Columns + value lists rather than dicts: the report is stored as JSONB,
    # which does not keep object key order.
    return {"ordered_by": order_cols or "(no clustered index: arbitrary rows)",
            "omitted_columns": omitted, "columns": selectable,
            "rows": [[mask_value(c, r[c], mask) for c in selectable] for r in rows]}


def distributions(conn, key, cols, idx_rows, sample_rows, mask, errors):
    schema, name = key
    fq = f"{qi(schema)}.{qi(name)}"
    order_cols = clustered_key(idx_rows, key)
    order_sql = (" ORDER BY " + ", ".join(qi(c) + " DESC" for c in order_cols)) if order_cols else ""
    out = {}
    for c in cols:
        if c["data_type"] not in GROUPABLE_TYPES or c["max_length"] == -1:
            continue
        if not DISTRIBUTION_COL_RE.search(c["column"]):
            continue
        if PII_COL_RE.search(c["column"]) and not NOT_PII_COL_RE.search(c["column"]) and mask:
            continue
        col = qi(c["column"])
        sql = (f"SELECT TOP (25) v AS value, COUNT(*) AS n FROM "
               f"(SELECT TOP ({int(sample_rows)}) {col} AS v FROM {fq}{order_sql}) x "
               f"GROUP BY v ORDER BY COUNT(*) DESC")
        res = safe(errors, f"distribution {schema}.{name}.{c['column']}", lambda: q(conn, sql)[1])
        if res is not None:
            out[c["column"]] = res
    return {"from_most_recent_rows": sample_rows if order_cols else f"{sample_rows} arbitrary rows",
            "columns": out}


def date_ranges(conn, key, cols, idx_rows, errors):
    """MIN/MAX of date columns that lead an index: two index seeks each."""
    schema, name = key
    fq = f"{qi(schema)}.{qi(name)}"
    leading = leading_index_columns(idx_rows, key)
    out = {}
    for c in cols:
        if c["data_type"] in DATE_TYPES and c["column"] in leading:
            col = qi(c["column"])
            sql = (f"SELECT (SELECT TOP (1) {col} FROM {fq} WHERE {col} IS NOT NULL ORDER BY {col} ASC) AS oldest, "
                   f"(SELECT TOP (1) {col} FROM {fq} WHERE {col} IS NOT NULL ORDER BY {col} DESC) AS newest")
            res = safe(errors, f"date range {schema}.{name}.{c['column']}", lambda: q(conn, sql)[1])
            if res:
                out[c["column"]] = res[0]
    return out


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def md_table(rows, cols=None):
    if not rows:
        return "_(none)_\n"
    cols = cols or list(rows[0].keys())
    def cell(v):
        s = "" if v is None else str(v)
        return s.replace("|", "\\|").replace("\n", " ")[:80]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(cell(r.get(c)) for c in cols) + " |" for r in rows]
    return "\n".join(out) + "\n"


def render_markdown(rep):
    L = []
    s = rep.get("server") or {}
    L.append("# GrainTime site discovery report\n")
    L.append(f"Generated (client clock): {rep['generated_at']}  ")
    L.append(f"Target: {rep['target']}\n")
    L.append("## Server\n")
    L.append(md_table([{"property": k, "value": v} for k, v in s.items()]))
    tls = rep.get("tls_assessment") or {}
    flag = {True: "AFFECTED. ", False: "", None: ""}.get(tls.get("affected"), "")
    L.append(f"\n**TLS assessment:** {flag}{tls.get('note', '')}\n")
    if rep.get("connection"):
        L.append("\n**This connection:** " + ", ".join(f"{k}={v}" for k, v in rep["connection"].items()) + "\n")
    L.append("\n## Tables (approximate rows from metadata)\n")
    counts = {(r["schema"], r["table"]): r["approx_rows"] for r in rep.get("row_counts", [])}
    L.append(md_table([{"schema": t["schema"], "name": t["name"], "type": t["type"],
                        "approx_rows": counts.get((t["schema"], t["name"]), "")}
                       for t in rep.get("tables", [])]))
    L.append("\n## Candidate tables\n")
    for c in rep.get("candidates", []):
        L.append(f"\n### {c['table']}  (~{c['approx_rows']} rows)\n")
        L.append("**Columns**\n")
        L.append(md_table(c["columns"], ["column", "data_type", "max_length", "nullable",
                                         "is_identity", "is_computed", "default_value"]))
        L.append("\n**Indexes**\n")
        L.append(md_table(c["indexes"], ["index_name", "type_desc", "is_unique", "is_primary_key",
                                         "key_ordinal", "is_included_column", "is_descending_key", "column"]))
        if c.get("triggers"):
            L.append("\n**Triggers**\n")
            L.append(md_table(c["triggers"]))
        if c.get("foreign_keys"):
            L.append("\n**Foreign keys**\n")
            L.append(md_table(c["foreign_keys"], ["column", "ref_schema", "ref_table", "ref_column"]))
        if c.get("date_ranges"):
            L.append("\n**Oldest / newest (indexed date columns)**\n")
            L.append(md_table([{"column": k, **v} for k, v in c["date_ranges"].items()]))
        smp = c.get("sample")
        if smp:
            L.append(f"\n**Sample rows** (ordered by: {smp['ordered_by']})\n")
            if smp["omitted_columns"]:
                L.append(f"Omitted large/binary columns: {', '.join(smp['omitted_columns'])}\n")
            L.append(md_table([dict(zip(smp["columns"], r)) for r in smp["rows"]], smp["columns"]))
        dist = c.get("distributions")
        if dist and dist["columns"]:
            L.append(f"\n**Value distributions** (from {dist['from_most_recent_rows']} most recent rows)\n")
            for col, vals in dist["columns"].items():
                L.append(f"\n`{col}`: " + ", ".join(f"{v['value']!r}={v['n']}" for v in vals) + "\n")
    L.append("\n## All columns (every table and view)\n")
    by_table = {}
    for col in rep.get("columns", []):
        by_table.setdefault(f"{col['schema']}.{col['table']}", []).append(
            f"{col['column']} {col['data_type']}"
            + ("(max)" if col.get("max_length") == -1 else
               f"({col['max_length']})" if col.get("max_length") is not None else "")
            + (" IDENTITY" if col.get("is_identity") == 1 else ""))
    for t, cs in by_table.items():
        L.append(f"- **{t}**: " + ", ".join(cs))
    L.append("\n## Foreign keys (all)\n")
    L.append(md_table(rep.get("foreign_keys", [])))
    L.append("\n## Triggers (all)\n")
    L.append(md_table(rep.get("triggers", [])))
    if rep.get("errors"):
        L.append("\n## Steps that failed (non-fatal)\n")
        L.append(md_table(rep["errors"]))
    return "\n".join(L) + "\n"




# --------------------------------------------------------------------------- #
# Entry point used by the collector job runner
# --------------------------------------------------------------------------- #

class Cancelled(Exception):
    pass


@dataclass
class DiscoveryOptions:
    extra_tables: list[str] = field(default_factory=list)
    max_candidates: int = 25
    sample_rows: int = 5
    distribution_rows: int = 2000
    include_samples: bool = True
    mask: bool = True

    def clamp(self) -> "DiscoveryOptions":
        self.max_candidates = max(1, min(int(self.max_candidates), 50))
        self.sample_rows = max(1, min(int(self.sample_rows), 20))
        self.distribution_rows = max(100, min(int(self.distribution_rows), 10000))
        return self


def run_discovery(conn, target: str, opts: DiscoveryOptions,
                  on_progress: Callable[[dict], None] = lambda p: None,
                  should_cancel: Callable[[], bool] = lambda: False) -> dict:
    """Run every discovery step on an open site connection. Steps that fail
    (usually a missing optional permission) are recorded and skipped."""
    opts.clamp()
    errors: list = []
    rep: dict = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                 "target": target, "errors": errors}

    def step(label, done, total):
        if should_cancel():
            raise Cancelled()
        on_progress({"step": label, "done": done, "total": total})

    step("server", 0, 1)
    rep["server"] = safe(errors, "server info", lambda: server_info(conn), {})
    rep["tls_assessment"] = tls_assessment((rep["server"] or {}).get("product_version", ""))
    rep["connection"] = safe(errors, "connection info (needs VIEW SERVER STATE; optional)",
                             lambda: connection_info(conn))
    step("catalog", 0, 1)
    tables, cols = safe(errors, "tables and columns", lambda: tables_and_columns(conn), ([], []))
    rep["tables"], rep["columns"] = tables, cols
    rep["row_counts"] = safe(errors, "row counts", lambda: row_counts(conn), [])
    idx = safe(errors, "indexes", lambda: indexes(conn), [])
    rep["foreign_keys"] = safe(errors, "foreign keys", lambda: foreign_keys(conn), [])
    rep["triggers"] = safe(errors, "triggers", lambda: triggers(conn), [])

    cols_by_table: dict = {}
    for c in cols:
        cols_by_table.setdefault((c["schema"], c["table"]), []).append(c)
    counts = {(r["schema"], r["table"]): r["approx_rows"] for r in rep["row_counts"]}
    extra = {t.strip().lower() for t in opts.extra_tables if t.strip()}
    cand_keys = pick_candidates(tables, cols_by_table, counts, extra)[: opts.max_candidates]

    rep["candidates"] = []
    for i, key in enumerate(cand_keys):
        label = f"{key[0]}.{key[1]}"
        step(f"inspecting {label}", i, len(cand_keys))
        tcols = cols_by_table.get(key, [])
        entry = {
            "table": label,
            "approx_rows": counts.get(key, "view/unknown"),
            "columns": tcols,
            "indexes": [r for r in idx if (r["schema"], r["table"]) == key],
            "triggers": [r for r in rep["triggers"] if (r["schema"], r["table"]) == key],
            "foreign_keys": [r for r in rep["foreign_keys"] if (r["schema"], r["table"]) == key],
            "date_ranges": date_ranges(conn, key, tcols, idx, errors),
        }
        if opts.include_samples:
            entry["sample"] = safe(errors, f"sample {label}",
                                   lambda: sample_table(conn, key, tcols, idx, opts.sample_rows,
                                                        opts.mask))
            entry["distributions"] = distributions(conn, key, tcols, idx, opts.distribution_rows,
                                                   opts.mask, errors)
        rep["candidates"].append(entry)
    on_progress({"step": "done", "done": len(cand_keys), "total": len(cand_keys)})
    return rep
