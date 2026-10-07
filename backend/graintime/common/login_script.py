"""T-SQL that creates GrainTime's read-only login at a site.

SELECT is granted only on the tables and columns the mapping profile reads,
so the login cannot read customer, driver, plate, weight or price columns at
all. Nothing else is granted: no writes, no schema changes.
"""

from __future__ import annotations

from .profiles import ProfileConfig


def profile_grants(p: ProfileConfig) -> dict[str, list[str]]:
    """table -> columns the collector reads."""
    grants: dict[str, list[str]] = {}

    def add(table: str, *cols: str | None) -> None:
        lst = grants.setdefault(table, [])
        for c in cols:
            if c and c not in lst:
                lst.append(c)

    add(p.ticket_table, *p.ticket_columns(), p.filter_column)
    if p.weigh_steps:
        ws = p.weigh_steps
        add(ws.table, ws.ticket_fk_column, ws.time_column, ws.weight_type_column, ws.status_column)
    for lk in (p.type_lookup, p.product_lookup):
        if lk:
            add(lk.table, lk.key_column, lk.code_column, lk.description_column, lk.direction_column)
    return grants


def _q(name: str) -> str:
    return ".".join("[" + part.replace("]", "]]") + "]" for part in name.split("."))


def _lit(s: str) -> str:
    return "N'" + s.replace("'", "''") + "'"


def login_script(p: ProfileConfig, *, database: str, login: str = "graintime",
                 windows_account: bool = False, password_placeholder: str = "CHANGE-ME") -> str:
    grants = profile_grants(p)
    L = [
        "/*",
        "  GrainTime read-only login, generated from the mapping profile.",
        "  Run as a sysadmin on the site's SQL Server. It creates one login with",
        "  SELECT on only the columns GrainTime reads. Nothing else is granted.",
        "  Re-running it is safe.",
        "  Password rules (SQL Server policy): 8+ characters, three of upper case,",
        "  lower case, digits and symbols, and it must not contain the login name.",
        "*/",
        "USE [master];",
        f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {_lit(login)})",
    ]
    if windows_account:
        L.append(f"    CREATE LOGIN {_q(login)} FROM WINDOWS WITH DEFAULT_DATABASE = {_q(database)};")
    else:
        L += [f"    CREATE LOGIN {_q(login)} WITH PASSWORD = {_lit(password_placeholder)},",
              f"        DEFAULT_DATABASE = {_q(database)}, CHECK_POLICY = ON, CHECK_EXPIRATION = OFF;"]
    L += ["GO", f"USE {_q(database)};",
          f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = {_lit(login)})",
          f"    CREATE USER {_q(login)} FOR LOGIN {_q(login)};", "GO"]
    for table, cols in grants.items():
        L.append(f"GRANT SELECT ON {_q(table)} ({', '.join(_q(c) for c in cols)}) TO {_q(login)};")
    L += ["GO", "",
          "/* Remove a broader discovery login once collection works:",
          "   DROP USER IF EXISTS [graintime_discovery];  (and DROP LOGIN in master) */"]
    return "\n".join(L) + "\n"
