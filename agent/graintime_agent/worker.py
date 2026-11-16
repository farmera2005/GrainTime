"""The agent's loop: report status, poll, run jobs, sleep; repeat.

Each cycle:
  1. If the scale software's database is running and polling is on, read new
     tickets (after the server's high-water mark) and the look-back window,
     exactly as the collector would (graintime.collector.mapping), and send
     the mapped columns. The server normalizes and stores them, and only then
     advances the high-water mark, so nothing is lost if a send fails.
  2. Send status (and the rows) in one signed /sync request. The answer
     carries the mapping profile, the high-water mark, when to sync next, and
     at most one job (Preview data or backfill) to run now.
"""

from __future__ import annotations

import logging
import platform
import time
from datetime import date, datetime, timedelta, timezone

from graintime.collector.mapping import (MappingError, SiteReader, classify_mapping_error, encode_value,
                                         read_poll_rows, read_preview_rows)
from graintime.common.profiles import ProfileConfig

from . import VERSION
from .client import ServerError
from .localdb import LocalDbError, explain, find_candidates, open_first, open_scale_database, windows_user

log = logging.getLogger("graintime_agent")

BACKFILL_PAGE = 500
BACKFILL_PAUSE_S = 1.0
IDLE_SYNC_S = 60
MAX_ERROR_SLEEP_S = 30 * 60
SERVICE_RETRY_S = 3600


def _is_user_pipe(pipe: str) -> bool:
    return pipe.lower().endswith("\\tsql\\query")


def encode_rows(rows: list[dict]) -> list[dict]:
    return [{k: encode_value(v) for k, v in r.items()} for r in rows]


def as_error(exc: Exception) -> dict:
    if isinstance(exc, (LocalDbError, MappingError)):
        return exc.as_dict()
    try:
        import pyodbc
        if isinstance(exc, pyodbc.Error):
            return classify_mapping_error(exc).as_dict()
    except ImportError:
        pass
    return {"code": "agent_error", "cause": str(exc)[:300], "fix": "See agent.log on the scale PC."}


class Worker:
    def __init__(self, server, find_instance=find_candidates, open_db=open_scale_database,
                 sleep=time.sleep):
        self.server, self.find_instance, self.open_db, self.sleep = server, find_instance, open_db, sleep
        self.instructions: dict | None = None
        self.failures = 0
        self.found_at: str | None = None        # where the database was last found (tried first)
        self.skip_until: dict[str, float] = {}  # service instances that refused this account

    def _candidates(self) -> tuple[list[str], dict]:
        got = self.find_instance()
        if isinstance(got, tuple):
            cands, detail = got
        else:                                  # a single pipe or None
            cands, detail = ([got] if got else []), {}
        now = time.monotonic()
        cands = [c for c in cands if self.skip_until.get(c, 0) <= now]
        if self.found_at in cands:
            cands = [self.found_at] + [c for c in cands if c != self.found_at]
        return cands, detail

    def _open(self, cands: list[str], match: str):
        def refused(c):   # don't knock on a service that refused us every minute
            self.skip_until[c] = time.monotonic() + SERVICE_RETRY_S
        conn = open_first(cands, match, opener=self.open_db, refused=refused)
        self.found_at = getattr(conn, "target", None) or self.found_at
        return conn

    # -- one cycle ---------------------------------------------------------- #

    def cycle(self) -> float:
        """Run one cycle; return seconds to wait before the next."""
        cands, detail = self._candidates()
        ins = self.instructions or {}
        match = ins.get("database_match")
        status = {"instance": "not_running", "database": None, "driver": None, "error": None,
                  "active_job_id": None, "detail": None}
        poll, conn = None, None
        if cands and match:
            # One short connection: confirms where the database is open, and
            # is used for this cycle's poll or job, then closed.
            try:
                conn = self._open(cands, match)
                status.update(instance="running", database=conn.database, driver=conn.driver)
            except LocalDbError as exc:
                if exc.code != "database_not_found":
                    status["instance"] = "running"
                    status["error"] = exc.as_dict()
                    log.warning("scale database: %s", exc.cause)
        elif cands and _is_user_pipe(cands[0]):
            status["instance"] = "running"         # no profile yet: a running user instance is enough to say so
        if status["instance"] == "not_running":
            status["detail"] = {**detail, "reason": explain(detail, match)}
        try:
            if conn is not None and ins.get("poll"):
                try:
                    poll = self._poll(conn, ins)
                except Exception as exc:          # reported to GrainTime, shown on the site page
                    status["error"] = as_error(exc)
                    log.warning("poll failed: %s", status["error"].get("cause"))
            resp = self.server.post("/api/agent/sync", {
                "agent": {"version": VERSION, "hostname": platform.node(), "windows_user": windows_user(),
                          "clock_utc": datetime.now(timezone.utc).isoformat()},
                "status": status, **({"poll": poll} if poll else {})})
            first = self.instructions is None or (self.instructions.get("poll") or {}).get("fingerprint") != \
                (resp.get("poll") or {}).get("fingerprint")
            self.instructions = resp
            if resp.get("message"):
                log.info("server says: %s", resp["message"])
            if resp.get("job") and conn is not None:
                self._run_job(conn, resp["job"])
                return 1
        finally:
            if conn is not None:
                conn.close()
        if first and (resp.get("poll") or resp.get("database_match")) and cands and poll is None:
            return 1                           # got a profile: take the first reading now
        return float(resp.get("next_sync_s") or IDLE_SYNC_S)

    def _poll(self, conn, ins: dict) -> dict:
        p = ins["poll"]
        profile = ProfileConfig(**p["profile"])
        got = read_poll_rows(SiteReader(conn, profile), p.get("high_water"), bool(p.get("recheck")))
        return {"fingerprint": p["fingerprint"], "high_water": got["high_water"],
                "rows": encode_rows(got["rows"]), "rechecked": bool(p.get("recheck"))}

    # -- jobs ------------------------------------------------------------------ #

    def _run_job(self, conn, job: dict) -> None:
        path = f"/api/agent/jobs/{job['id']}"
        log.info("running %s job %s", job["kind"], job["id"])
        try:
            profile = ProfileConfig(**job["profile"])
            reader = SiteReader(conn, profile)
            if job["kind"] == "preview":
                limit = max(1, min(int(job["options"].get("limit") or 25), 100))
                got = read_preview_rows(reader, limit)
                self.server.post(path, {"rows": encode_rows(got["rows"]), "done": True,
                                        "status_examples": {k: encode_rows(v) for k, v in
                                                            got["status_examples"].items()}})
            elif job["kind"] == "backfill":
                self._backfill(reader, profile, job, path)
            else:
                self.server.post(path, {"error": {"code": "unsupported", "cause": f"Unknown job {job['kind']}",
                                                  "fix": "Update the GrainTime Agent."}})
        except ServerError:
            raise
        except Exception as exc:
            log.warning("job %s failed: %s", job["id"], exc)
            self.server.post(path, {"error": as_error(exc)})

    def _backfill(self, reader: SiteReader, profile: ProfileConfig, job: dict, path: str) -> None:
        start = date.fromisoformat(job["options"]["date_from"])
        end = date.fromisoformat(job["options"]["date_to"])
        lo = datetime.combine(start, datetime.min.time())
        hi = datetime.combine(end + timedelta(days=1), datetime.min.time())
        if profile.lookback_mode == "recent_rows":
            # No index on the dates: walk the table once in key order (index
            # seeks, 500 rows a page) and keep tickets created in the range,
            # instead of scanning the whole table once per day.
            after, page_no, read = "", 0, 0
            while True:
                page = reader.new_rows(after, BACKFILL_PAGE)
                page_no += 1
                read += len(page)
                keep = [r for r in page if isinstance(r.get(profile.created_column), datetime)
                        and lo <= r[profile.created_column] < hi]
                last = len(page) < BACKFILL_PAGE
                resp = self.server.post(path, {"rows": encode_rows(keep), "progress": {
                    "step": f"{read} tickets checked", "done": page_no, "total": None}})
                if resp.get("cancel") or last:
                    break
                after = str(page[-1][profile.high_water_column]).strip()
                self.sleep(BACKFILL_PAUSE_S)
        else:
            days = (end - start).days + 1
            for i in range(days):
                day = lo + timedelta(days=i)
                rows = reader.created_range(day, day + timedelta(days=1))
                resp = self.server.post(path, {"rows": encode_rows(rows), "progress": {
                    "step": day.date().isoformat(), "done": i + 1, "total": days}})
                if resp.get("cancel"):
                    break
                self.sleep(BACKFILL_PAUSE_S)
        self.server.post(path, {"done": True})

    # -- forever --------------------------------------------------------------- #

    def run_forever(self, stop=lambda: False) -> None:
        log.info("GrainTime Agent %s started", VERSION)
        while not stop():
            try:
                wait = self.cycle()
                self.failures = 0
            except ServerError as exc:
                self.failures += 1
                wait = min(IDLE_SYNC_S * 2 ** min(self.failures, 5), MAX_ERROR_SLEEP_S)
                log.warning("GrainTime refused the request (%s); retrying in %ss", exc, int(wait))
                if exc.status == 401 and "not paired" in exc.detail:
                    wait = MAX_ERROR_SLEEP_S
            except OSError as exc:              # GrainTime server unreachable
                self.failures += 1
                wait = min(IDLE_SYNC_S * 2 ** min(self.failures, 5), MAX_ERROR_SLEEP_S)
                log.warning("GrainTime server unreachable (%s); retrying in %ss", exc, int(wait))
            self.sleep(wait)
