"""Scheduled polling of every enabled site.

Reads the list of sites from the central store on every tick, so sites added,
changed, disabled or archived in the admin panel take effect on the next
cycle with no restart. Each site is polled on its own thread with its own
back-off; one unreachable site never delays the others. Shares the "one
connection per site" lock with the admin jobs (test connection, preview,
backfill).
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..common import settings_store
from ..common.db import get_sessionmaker
from ..common.logging import get_logger
from ..common.models import MappingProfile, Site, SiteCollectorState
from ..common.profiles import ProfileConfig
from ..common.tickets_store import upsert_tickets
from . import collect, sitedb
from .jobs import JobRunner, spec_from_site

log = get_logger("collector.poller")

MAX_BACKOFF_S = 30 * 60
LOOKUP_TTL_S = 3600
RECHECK_EVERY = timedelta(hours=20)
RECHECK_LOCAL_HOURS = range(1, 5)     # nightly re-check between 01:00 and 05:00 Eastern


def _now():
    return datetime.now(timezone.utc)


def fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:32]


def backoff_seconds(interval: int, failures: int) -> int:
    return int(min(interval * (2 ** max(failures - 1, 0)), MAX_BACKOFF_S))


class Poller:
    def __init__(self, runner: JobRunner, max_workers: int = 8):
        self.runner = runner
        self.Session = get_sessionmaker()
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="poll")
        self.lookups: dict[int, tuple[float, str, object]] = {}

    def due_sites(self) -> list[tuple[int, int]]:
        """(site_id, interval_s) for enabled sites whose next poll is due."""
        now = _now()
        with self.Session() as db:
            default = int(settings_store.get_globals(db)["poll_interval_s"])
            sites = db.scalars(select(Site).where(
                Site.polling_enabled.is_(True), Site.archived_at.is_(None),
                Site.mapping_profile_id.is_not(None))).all()
            states = {s.site_id: s for s in db.scalars(select(SiteCollectorState))}
            out = []
            for s in sites:
                st = states.get(s.id)
                if st is None or st.next_poll_at is None or st.next_poll_at <= now:
                    out.append((s.id, s.poll_interval_s or default))
            return out

    def tick(self) -> int:
        started = 0
        for site_id, interval in self.due_sites():
            key = f"site:{site_id}"
            with self.runner.lock:
                if key in self.runner.busy:
                    continue        # a job or the previous poll is still using this site
                self.runner.busy.add(key)
            self.pool.submit(self._poll_guarded, site_id, interval, key)
            started += 1
        return started

    def _poll_guarded(self, site_id: int, interval: int, key: str) -> None:
        try:
            self.poll_site(site_id, interval)
        except Exception:
            log.exception("poll crashed", extra={"site_id": site_id})
        finally:
            with self.runner.lock:
                self.runner.busy.discard(key)

    def poll_site(self, site_id: int, interval: int) -> None:
        with self.Session() as db:
            site = db.get(Site, site_id)
            if site is None or not site.polling_enabled or site.archived_at is not None:
                return
            prof = db.get(MappingProfile, site.mapping_profile_id)
            state = db.get(SiteCollectorState, site_id) or SiteCollectorState(site_id=site_id)
            fp = fingerprint(prof.config)
            hwm = state.high_water_mark if state.profile_fingerprint in (None, fp) else None
            last_recheck = state.last_recheck_at
            spec = spec_from_site(site)
            profile = ProfileConfig(**prof.config)

        cached = self.lookups.get(site_id)
        lookups = cached[2] if cached and cached[1] == fp and time.time() - cached[0] < LOOKUP_TTL_S else None
        local_hour = collect.local_now().hour
        recheck = (local_hour in RECHECK_LOCAL_HOURS and
                   (last_recheck is None or _now() - last_recheck > RECHECK_EVERY))
        t0 = time.monotonic()
        try:
            conn, _notes = sitedb.open_connection(spec)
            try:
                res = collect.poll(conn, profile, hwm, lookups, recheck)
            finally:
                conn.close()
        except (sitedb.SiteConnectionError, collect.MappingError) as exc:
            return self._failed(site_id, interval, exc.as_dict())
        except Exception as exc:
            return self._failed(site_id, interval, {"code": "internal", "cause": str(exc)[:300],
                                                    "fix": "Check the collector logs."})
        if lookups is None:
            self.lookups[site_id] = (time.time(), fp, res["lookups"])
        with self.Session() as db:
            stored = upsert_tickets(db, site_id, res["tickets"])
            st = db.get(SiteCollectorState, site_id)
            if st is None:
                st = SiteCollectorState(site_id=site_id, rows_total=0, consecutive_failures=0)
                db.add(st)
            now = _now()
            st.high_water_mark = res["high_water"]
            st.profile_fingerprint = fp
            st.last_poll_at = st.last_success_at = now
            st.next_poll_at = now + timedelta(seconds=interval)
            st.consecutive_failures = 0
            st.last_error = None
            st.rows_last_poll = stored
            st.rows_total = (st.rows_total or 0) + stored
            if recheck:
                st.last_recheck_at = now
            db.commit()
        log.info("polled site", extra={"site_id": site_id, "rows_read": res["rows_read"],
                                       "stored": stored, "seconds": round(time.monotonic() - t0, 2),
                                       "recheck": recheck})

    def _failed(self, site_id: int, interval: int, error: dict) -> None:
        with self.Session() as db:
            st = db.get(SiteCollectorState, site_id)
            if st is None:
                st = SiteCollectorState(site_id=site_id, rows_total=0, consecutive_failures=0)
                db.add(st)
            now = _now()
            st.consecutive_failures = (st.consecutive_failures or 0) + 1
            st.last_poll_at = now
            st.last_error = {**error, "at": now.isoformat()}
            st.next_poll_at = now + timedelta(seconds=backoff_seconds(interval, st.consecutive_failures))
            db.commit()
            failures = st.consecutive_failures
        log.warning("poll failed", extra={"site_id": site_id, "code": error.get("code"),
                                          "failures": failures})
