"""Runs jobs the api queues in `collector_jobs` (test connection, discovery).

The api never connects to a site database itself; it inserts a job row and the
collector picks it up within about a second. At most one job runs per site
(or per unsaved connection target) at a time, so a site never gets more than
one connection from us.
"""

from __future__ import annotations

import base64
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import select, update

from ..common import crypto
from ..common.db import get_sessionmaker
from ..common.logging import get_logger
from ..common.models import CollectorJob, Site
from . import discovery, sitedb

log = get_logger("collector.jobs")

PROGRESS_WRITE_INTERVAL_S = 1.0
CANCEL_CHECK_INTERVAL_S = 1.0


def _now():
    return datetime.now(timezone.utc)


def job_key(job: CollectorJob) -> str:
    if job.site_id is not None:
        return f"site:{job.site_id}"
    c = job.params.get("connection") or {}
    return f"conn:{c.get('host')}:{c.get('port') or c.get('instance_name')}/{c.get('database')}"


def spec_for_job(db, job: CollectorJob) -> tuple[sitedb.ConnectionSpec, str]:
    """Connection for a job: an unsaved connection in params (optionally
    falling back to a saved site's stored password), or the saved site."""
    conn = job.params.get("connection")
    site = db.get(Site, job.site_id) if job.site_id is not None else None
    if conn:
        if conn.get("password_encrypted"):
            password = crypto.decrypt(base64.b64decode(conn["password_encrypted"]))
        elif site is not None:
            password = crypto.decrypt(site.password_encrypted)
        else:
            raise sitedb.SiteConnectionError("login_failed", "No password was entered",
                                             "Enter the SQL login's password.")
        spec = sitedb.ConnectionSpec(
            host=conn["host"], port=int(conn["port"]) if conn.get("port") else None,
            instance_name=conn.get("instance_name") or None, database=conn["database"],
            auth_method=conn.get("auth_method") or "sql", domain=conn.get("domain") or None,
            username=conn["username"], password=password, encrypt=conn.get("encrypt", "yes"),
            trust_server_certificate=bool(conn.get("trust_server_certificate")))
    elif site is not None:
        spec = sitedb.ConnectionSpec(
            host=site.host, port=site.port, instance_name=site.instance_name,
            auth_method=site.auth_method, domain=site.domain,
            database=site.database_name, username=site.username,
            password=crypto.decrypt(site.password_encrypted), encrypt=site.encrypt,
            trust_server_certificate=site.trust_server_certificate)
    else:
        raise ValueError("job has neither a site nor connection details")
    where = spec.host + (f"\\{spec.instance_name}" if spec.instance_name else "")
    who = sitedb.windows_principal(spec) if spec.auth_method == "windows" else spec.username
    return spec, f"{where}:{spec.port or '?'} / {spec.database} as {who}"


class JobRunner:
    def __init__(self, max_workers: int = 4):
        self.Session = get_sessionmaker()
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self.busy: set[str] = set()
        self.lock = threading.Lock()
        self.max_workers = max_workers

    # -- lifecycle ---------------------------------------------------------- #

    def recover_interrupted(self) -> None:
        """Jobs left 'running' by a previous collector process cannot resume."""
        with self.Session() as db:
            n = db.execute(
                update(CollectorJob).where(CollectorJob.status == "running").values(
                    status="failed", finished_at=_now(),
                    error={"code": "interrupted", "cause": "The collector restarted while this "
                           "job was running", "fix": "Run it again."},
                    params={})).rowcount
            db.commit()
        if n:
            log.warning("marked interrupted jobs failed", extra={"count": n})

    def tick(self) -> int:
        """Claim and start as many runnable jobs as there are free workers."""
        started = 0
        while True:
            with self.lock:
                if len(self.busy) >= self.max_workers:
                    return started
                claimed = self._claim()
                if claimed is None:
                    return started
                job_id, key = claimed
                self.busy.add(key)
            self.pool.submit(self._run_guarded, job_id, key)
            started += 1

    def _claim(self):
        with self.Session() as db:
            jobs = db.scalars(
                select(CollectorJob).where(CollectorJob.status == "queued")
                .order_by(CollectorJob.id).limit(50).with_for_update(skip_locked=True)).all()
            for job in jobs:
                key = job_key(job)
                if key in self.busy:
                    continue  # one connection per site at a time
                if job.cancel_requested:
                    job.status, job.finished_at, job.params = "cancelled", _now(), {}
                    continue
                job.status, job.started_at = "running", _now()
                db.commit()
                return job.id, key
            db.commit()
        return None

    # -- execution ---------------------------------------------------------- #

    def _run_guarded(self, job_id: int, key: str) -> None:
        try:
            self.run(job_id)
        except Exception:
            log.exception("job crashed", extra={"job_id": job_id})
            self._finish(job_id, "failed", error={"code": "internal", "cause": "Internal error "
                         "in the collector", "fix": "Check the collector logs."})
        finally:
            with self.lock:
                self.busy.discard(key)

    def run(self, job_id: int) -> None:
        with self.Session() as db:
            job = db.get(CollectorJob, job_id)
            kind = job.kind
            try:
                spec, target = spec_for_job(db, job)
            except sitedb.SiteConnectionError as exc:
                return self._finish(job_id, "failed", error=exc.as_dict())
            except crypto.DecryptionError as exc:
                return self._finish(job_id, "failed", error={
                    "code": "password_unreadable", "cause": str(exc),
                    "fix": "Re-enter the site password in the admin panel."})
            options = job.params.get("options") or {}

        log.info("job started", extra={"job_id": job_id, "kind": kind, "target": target})
        t0 = time.monotonic()
        try:
            conn, notes = sitedb.open_connection(spec)
        except sitedb.SiteConnectionError as exc:
            log.info("job connection failed", extra={"job_id": job_id, "code": exc.code})
            return self._finish(job_id, "failed", error=exc.as_dict())
        try:
            if kind == "test_connection":
                info = sitedb.server_info(conn)
                info.update(notes)  # resolved_port / port_entered when looked up
                info["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
                self._finish(job_id, "succeeded", result=info)
            elif kind == "discovery":
                self._run_discovery(job_id, conn, target, options)
            else:
                self._finish(job_id, "failed", error={"code": "unknown_kind",
                             "cause": f"Unknown job kind {kind!r}", "fix": ""})
        finally:
            conn.close()
        log.info("job finished", extra={"job_id": job_id, "kind": kind,
                                        "seconds": round(time.monotonic() - t0, 2)})

    def _run_discovery(self, job_id, conn, target, options) -> None:
        last_write = [0.0]
        last_cancel_check = [0.0]
        cancelled = [False]

        def on_progress(p):
            now = time.monotonic()
            if now - last_write[0] >= PROGRESS_WRITE_INTERVAL_S or p.get("step") == "done":
                last_write[0] = now
                with self.Session() as db:
                    db.execute(update(CollectorJob).where(CollectorJob.id == job_id)
                               .values(progress=p))
                    db.commit()

        def should_cancel():
            now = time.monotonic()
            if now - last_cancel_check[0] >= CANCEL_CHECK_INTERVAL_S:
                last_cancel_check[0] = now
                with self.Session() as db:
                    cancelled[0] = bool(db.scalar(select(CollectorJob.cancel_requested)
                                                  .where(CollectorJob.id == job_id)))
            return cancelled[0]

        opts = discovery.DiscoveryOptions(**{k: v for k, v in options.items()
                                             if k in discovery.DiscoveryOptions.__dataclass_fields__})
        try:
            rep = discovery.run_discovery(conn, target, opts, on_progress, should_cancel)
        except discovery.Cancelled:
            return self._finish(job_id, "cancelled")
        md = discovery.render_markdown(rep)
        summary = {"tables": len(rep.get("tables", [])), "candidates": len(rep.get("candidates", [])),
                   "step_errors": len(rep.get("errors", [])),
                   "server": {k: (rep.get("server") or {}).get(k) for k in
                              ("product_version", "edition", "product_level")},
                   "tls": rep.get("tls_assessment")}
        self._finish(job_id, "succeeded", result={"summary": summary, "report": rep, "markdown": md})

    def _finish(self, job_id, status, result=None, error=None) -> None:
        with self.Session() as db:
            # params are wiped so a one-off password does not outlive the job.
            db.execute(update(CollectorJob).where(CollectorJob.id == job_id).values(
                status=status, result=result, error=error, finished_at=_now(), params={}))
            db.commit()
