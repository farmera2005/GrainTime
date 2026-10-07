"""Collector service entry point: admin jobs, scheduled site polling, /health.

Jobs (test connection, discovery, preview, backfill) are claimed every second;
sites due for a poll are checked every 5 seconds; the public page's aggregates
are recomputed every 30 seconds (central database only).
"""

from __future__ import annotations

import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..common.db import get_sessionmaker
from ..common.logging import get_logger, setup_logging
from ..common.publish import publish
from .jobs import JobRunner
from .poller import Poller

log = get_logger("collector")
HEALTH_PORT = 8081
LOOP_INTERVAL_S = 1.0
POLL_CHECK_EVERY_S = 5.0
PUBLISH_EVERY_S = 30.0
STALE_LOOP_S = 30

state = {"last_loop": 0.0, "db_ok": False, "started": time.time()}


class Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        healthy = state["db_ok"] and time.time() - state["last_loop"] < STALE_LOOP_S
        body = json.dumps({"status": "ok" if healthy else "unhealthy", "db": state["db_ok"],
                           "last_loop_age_s": round(time.time() - state["last_loop"], 1)}).encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main() -> None:
    setup_logging("collector")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    server = ThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    runner = JobRunner()
    poller = Poller(runner)
    last_poll_check = 0.0
    last_publish = 0.0
    backoff = 1.0
    recovered = False
    log.info("collector started")
    while not stop.is_set():
        try:
            if not recovered:
                runner.recover_interrupted()
                recovered = True
            runner.tick()
            if time.monotonic() - last_poll_check >= POLL_CHECK_EVERY_S:
                last_poll_check = time.monotonic()
                poller.tick()
            if time.monotonic() - last_publish >= PUBLISH_EVERY_S:
                last_publish = time.monotonic()
                try:
                    with get_sessionmaker()() as db:
                        publish(db)
                except Exception as exc:   # never let the public page stop collection
                    log.warning("public aggregates not updated", extra={"error": str(exc)[:300]})
            state["db_ok"] = True
            backoff = 1.0
            state["last_loop"] = time.time()
            stop.wait(LOOP_INTERVAL_S)
        except Exception as exc:
            state["db_ok"] = False
            state["last_loop"] = time.time()
            log.warning("central database unavailable", extra={"error": str(exc)[:300]})
            stop.wait(backoff)
            backoff = min(backoff * 2, 30)
    log.info("collector stopping")
    runner.pool.shutdown(wait=False, cancel_futures=True)
    poller.pool.shutdown(wait=False, cancel_futures=True)
    server.shutdown()


if __name__ == "__main__":
    main()
