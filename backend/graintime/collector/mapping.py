"""Read tickets from a site database using a mapping profile.

Every statement is built here from the validated profile: bracket-quoted
identifiers, bound parameters, and a TOP limit on every SELECT. The session is
already READ UNCOMMITTED with lock and command timeouts (sitedb.connect).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from ..common.normalize import (SITE_TZ, Lookups, WeighSummary, normalize, raw_key,
                                resolve_families)
from ..common.profiles import ProfileConfig

NEW_ROWS_LIMIT = 500
WINDOW_LIMIT = 2000
RANGE_LIMIT = 2000
WEIGH_BATCH = 500
PAUSE_S = 0.05


def qi(name: str) -> str:
    """[schema].[table] / [column] from an already-validated identifier."""
    return ".".join("[" + part.replace("]", "]]") + "]" for part in name.split("."))


def _is_pytds(conn) -> bool:
    return type(conn).__module__.startswith("pytds")


def run(conn, sql: str, params: tuple | list = ()) -> list[dict]:
    """Execute one SELECT with '?' placeholders on either driver."""
    if _is_pytds(conn):
        sql = sql.replace("%", "%%").replace("?", "%s")
    time.sleep(PAUSE_S)
    cur = conn.cursor()
    try:
        cur.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cur.close()


def local_now() -> datetime:
    """Current Eastern wall-clock time, naive, as the site databases store it."""
    return datetime.now(SITE_TZ).replace(tzinfo=None)


def _hwm_value(v: str | None):
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return v


class SiteReader:
    def __init__(self, conn, profile: ProfileConfig):
        self.conn, self.p = conn, profile
        self.cols = ", ".join(qi(c) for c in profile.ticket_columns())
        self.table = qi(profile.ticket_table)

    # --- ticket rows ------------------------------------------------------- #

    def _where(self, cond: str, params: list) -> tuple[str, list]:
        if self.p.filter_column:
            cond = f"({cond}) AND {qi(self.p.filter_column)} = ?"
            params = [*params, self.p.filter_value]
        return cond, params

    def _select(self, top: int, cond: str, params: list, order: str) -> list[dict]:
        cond, params = self._where(cond, params)
        sql = f"SELECT TOP ({int(top)}) {self.cols} FROM {self.table} WHERE {cond} ORDER BY {order}"
        return run(self.conn, sql, params)

    def max_high_water(self):
        hw = qi(self.p.high_water_column)
        cond, params = self._where("1 = 1", [])
        rows = run(self.conn, f"SELECT TOP (1) {hw} AS hw FROM {self.table} WHERE {cond} "
                              f"ORDER BY {hw} DESC", params)
        return rows[0]["hw"] if rows else None

    def new_rows(self, after, limit: int = NEW_ROWS_LIMIT) -> list[dict]:
        hw = qi(self.p.high_water_column)
        return self._select(limit, f"{hw} > ?", [_hwm_value(after)], f"{hw}")

    def window_rows(self, created_since: datetime, completed_since: datetime | None
                    ) -> list[dict]:
        """Recently created tickets, plus recently completed ones (two index seeks)."""
        idc = self.p.id_column
        rows = self._select(WINDOW_LIMIT, f"{qi(self.p.created_column)} >= ?", [created_since],
                            f"{qi(idc)} DESC")
        if self.p.completed_column and completed_since is not None:
            rows += self._select(WINDOW_LIMIT, f"{qi(self.p.completed_column)} >= ?",
                                 [completed_since], f"{qi(idc)} DESC")
        seen, out = set(), []
        for r in rows:
            k = raw_key(r[idc])
            if k not in seen:
                seen.add(k)
                out.append(r)
        return out

    def created_range(self, start: datetime, end: datetime, should_cancel=lambda: False
                      ) -> list[dict]:
        """All tickets created in [start, end), paged by id so each query is bounded."""
        idc, cc = qi(self.p.id_column), qi(self.p.created_column)
        out, after = [], None
        while True:
            if should_cancel():
                return out
            cond, params = f"{cc} >= ? AND {cc} < ?", [start, end]
            if after is not None:
                cond, params = cond + f" AND {idc} > ?", params + [after]
            page = self._select(RANGE_LIMIT, cond, params, idc)
            out += page
            if len(page) < RANGE_LIMIT:
                return out
            after = page[-1][self.p.id_column]

    def recent_rows(self, n: int) -> list[dict]:
        return self._select(n, "1 = 1", [], f"{qi(self.p.id_column)} DESC")

    def status_examples(self, raw_value: str, n: int = 5) -> list[dict]:
        return self._select(n, f"{qi(self.p.status_column)} = ?", [_hwm_value(raw_value)],
                            f"{qi(self.p.id_column)} DESC")

    # --- weigh steps and lookups ------------------------------------------- #

    def weighs(self, ids: list) -> dict[str, WeighSummary]:
        ws = self.p.weigh_steps
        out: dict[str, WeighSummary] = {}
        if ws is None or not ids:
            return out
        fk, t, wt = qi(ws.ticket_fk_column), qi(ws.time_column), qi(ws.weight_type_column)
        for i in range(0, len(ids), WEIGH_BATCH):
            batch = ids[i:i + WEIGH_BATCH]
            marks = ", ".join("?" for _ in batch)
            cond = f"{fk} IN ({marks}) AND {wt} IS NOT NULL AND {t} IS NOT NULL"
            params = list(batch)
            if ws.status_column and ws.status_done_value not in (None, ""):
                cond += f" AND {qi(ws.status_column)} = ?"
                params.append(_hwm_value(ws.status_done_value))
            # Grouped: at most 2-3 rows per ticket come back.
            sql = (f"SELECT TOP ({len(batch) * 10}) {fk} AS tid, {wt} AS wtype, MIN({t}) AS first_at, "
                   f"MAX({t}) AS last_at, COUNT(*) AS n FROM {qi(ws.table)} WHERE {cond} "
                   f"GROUP BY {fk}, {wt}")
            for r in run(self.conn, sql, params):
                out.setdefault(raw_key(r["tid"]), WeighSummary()).add(
                    r["wtype"], r["first_at"], r["last_at"], int(r["n"]))
        return out

    def lookups(self) -> Lookups:
        lk = Lookups()
        tl = self.p.type_lookup
        if tl:
            cols = [tl.key_column, tl.code_column] + [c for c in (tl.description_column,
                                                                    tl.direction_column) if c]
            for r in run(self.conn, f"SELECT TOP (1000) {', '.join(qi(c) for c in cols)} "
                                    f"FROM {qi(tl.table)}"):
                lk.types[raw_key(r[tl.key_column])] = {
                    "code": raw_key(r[tl.code_column]),
                    "description": r.get(tl.description_column) if tl.description_column else None,
                    "direction": r.get(tl.direction_column) if tl.direction_column else None}
        pl = self.p.product_lookup
        if pl:
            cols = [pl.key_column, pl.code_column] + ([pl.description_column]
                                                       if pl.description_column else [])
            for r in run(self.conn, f"SELECT TOP (5000) {', '.join(qi(c) for c in cols)} "
                                    f"FROM {qi(pl.table)}"):
                lk.products[raw_key(r[pl.key_column])] = {
                    "code": raw_key(r[pl.code_column]),
                    "description": r.get(pl.description_column) if pl.description_column else None}
        return lk

    # --- rows -> tickets ---------------------------------------------------- #

    def normalize_rows(self, rows: list[dict], lookups: Lookups) -> tuple[list[dict], int]:
        ids = [r[self.p.id_column] for r in rows]
        weighs = self.weighs(ids)
        rows, weighs, merged = resolve_families(self.p, rows, weighs)
        tickets = [normalize(self.p, r, weighs.get(raw_key(r[self.p.id_column])), lookups)
                   for r in rows]
        return tickets, merged


def window_bounds(p: ProfileConfig, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or local_now()
    return now - timedelta(hours=p.lookback_hours), now - timedelta(hours=p.completed_lookback_hours)
