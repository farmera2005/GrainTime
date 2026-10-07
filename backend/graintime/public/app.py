"""The public service: a mobile-first page and a JSON feed of site aggregates.

Isolation (this is the only part meant to face the internet):
- Runs in its own container, on a network shared only with the database.
- Connects as `graintime_public`, a role that can SELECT public_site_status
  and nothing else. That table holds precomputed aggregates written by the
  collector; there is no code path here to tickets, the internal api or any
  site database, and this module imports nothing else from GrainTime.
- Responses are cached (15 s in process, 30 s for clients) and rate limited
  per client address. GET and HEAD only; no docs or OpenAPI endpoints.
"""

from __future__ import annotations

import html
import ipaddress
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

TZ = ZoneInfo("America/New_York")
CACHE_S = 15
CLIENT_MAX_AGE = 30
RATE_PER_MIN = 60          # per client address
RATE_BURST = 20
GLOBAL_PER_MIN = 3000      # all clients together
COLUMNS = ("code", "name", "address", "map_url", "open_state", "hours_text", "traffic", "minutes",
           "level", "trucks_on_site", "data_as_of", "computed_at", "stale_after_min")
NOTE = ("Time on site is measured from the inbound scale to the outbound scale. Time spent in line "
        "before the inbound scale is not included.")


def conninfo() -> str:
    url = os.environ.get("GRAINTIME_PUBLIC_DATABASE_URL")
    if url:
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    secret = Path(os.environ.get("GRAINTIME_PUBLIC_SECRETS_DIR", "/run/graintime-public-secrets"))
    password = (secret / "public_db_password").read_text(encoding="utf-8").strip()
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("GRAINTIME_DB_HOST", "db"), port=os.environ.get("GRAINTIME_DB_PORT", "5432"),
        dbname=os.environ.get("GRAINTIME_DB_NAME", "graintime"), user="graintime_public",
        password=password, connect_timeout=5, application_name="graintime-public")


# --------------------------------------------------------------------------- #
# Snapshot cache
# --------------------------------------------------------------------------- #

class Snapshot:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[dict] | None = None
        self.loaded = 0.0
        self.ok = False

    def get(self) -> list[dict] | None:
        with self.lock:
            if self.rows is not None and time.monotonic() - self.loaded < CACHE_S:
                return self.rows
            try:
                with psycopg.connect(conninfo(), autocommit=True) as c:
                    c.execute("SET statement_timeout = 5000")
                    cur = c.execute(f"SELECT {', '.join(COLUMNS)} FROM public_site_status "
                                    "ORDER BY position, name LIMIT 500")
                    self.rows = [dict(zip(COLUMNS, r)) for r in cur.fetchall()]
                self.ok = True
            except Exception:
                # Keep serving the last good snapshot; its timestamps make it
                # show as delayed once it is old.
                self.ok = False
            self.loaded = time.monotonic()
            return self.rows


snapshot = Snapshot()


# --------------------------------------------------------------------------- #
# Rate limiting (token buckets, in memory)
# --------------------------------------------------------------------------- #

class Limiter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buckets: dict[str, tuple[float, float]] = {}
        self.global_bucket = (float(GLOBAL_PER_MIN), time.monotonic())

    @staticmethod
    def _take(bucket: tuple[float, float], rate_per_min: float, burst: float) -> tuple[bool, tuple[float, float]]:
        tokens, at = bucket
        now = time.monotonic()
        tokens = min(burst, tokens + (now - at) * rate_per_min / 60.0)
        if tokens < 1:
            return False, (tokens, now)
        return True, (tokens - 1, now)

    def allow(self, key: str) -> bool:
        with self.lock:
            ok, self.global_bucket = self._take(self.global_bucket, GLOBAL_PER_MIN, GLOBAL_PER_MIN)
            if not ok:
                return False
            if len(self.buckets) > 20000:
                self.buckets.clear()
            ok, self.buckets[key] = self._take(self.buckets.get(key, (RATE_BURST, time.monotonic())),
                                               RATE_PER_MIN, RATE_BURST)
            return ok


limiter = Limiter()


def client_key(request: Request) -> str:
    """The client address. X-Forwarded-For is trusted only from a private-network
    peer (a reverse proxy, tunnel connector or the internal web server), and then
    only its last entry, which that proxy appended itself."""
    peer = request.client.host if request.client else "unknown"
    try:
        private = ipaddress.ip_address(peer).is_private
    except ValueError:
        private = False
    xff = request.headers.get("x-forwarded-for")
    if private and xff:
        last = xff.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass
    return peer


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

def site_view(r: dict, now: datetime) -> dict:
    """What the public sees for one site. Stale data is never shown as current."""
    limit = r["stale_after_min"] * 60
    as_of = r["data_as_of"]
    stale = (as_of is None or (now - as_of).total_seconds() > limit
             or (now - r["computed_at"]).total_seconds() > limit)
    if stale:
        status = "stale"
    elif r["traffic"] == "ok":
        status = "ok"
    elif r["traffic"] == "light":
        status = "light_traffic"
    elif r["open_state"] is False:
        status = "closed"
    else:
        status = "no_recent_trucks"
    return {
        "code": r["code"], "name": r["name"], "address": r["address"], "map_url": safe_url(r["map_url"]),
        "open": r["open_state"], "hours": r["hours_text"], "status": status,
        "time_on_site_min": r["minutes"] if status == "ok" else None,
        "level": r["level"] if status == "ok" else None,
        "trucks_on_site": None if stale else r["trucks_on_site"],
        "last_updated": as_of.astimezone(timezone.utc).isoformat() if as_of else None,
    }


def safe_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.netloc else None


def fmt_time(dt: datetime, now: datetime) -> str:
    local = dt.astimezone(TZ)
    h = local.hour % 12 or 12
    t = f"{h}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    if local.date() != now.astimezone(TZ).date():
        t += f", {local.strftime('%b')} {local.day}"
    return t


LEVEL_TEXT = {"good": ("✓", "Short"), "warning": ("!", "Moderate"), "critical": ("✕", "Long")}

CSS = """
:root{--bg:#f4f5f2;--card:#fff;--text:#1d2420;--muted:#5d6862;--border:#d6dbd5;--accent:#2f6b3a;
--good:#0ca30c;--warning:#fab219;--critical:#d03b3b;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#151916;--card:#1f2420;--text:#e6ebe7;--muted:#9aa69f;
--border:#353d37;--accent:#5aa36a;--good:#2fb52f;--critical:#e05656;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px 16px 40px}
h1{font-size:1.4rem;margin:8px 0 2px}.sub{color:var(--muted);font-size:.9rem;margin:0 0 14px}
.site{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin:10px 0}
.top{display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap}
.name{font-weight:700;font-size:1.1rem;margin:0}.hours{font-size:.85rem;color:var(--muted)}
.hours.open{color:var(--accent);font-weight:600}
.row{display:flex;gap:22px;flex-wrap:wrap;margin-top:8px;align-items:flex-end}
.big{font-size:2rem;font-weight:750;line-height:1.1;font-variant-numeric:tabular-nums}
.unit{font-size:1rem;font-weight:500;color:var(--muted)}.label{font-size:.8rem;color:var(--muted)}
.word{font-size:1.25rem;font-weight:700}.delayed{color:var(--muted)}
.lvl{display:inline-flex;align-items:center;gap:5px;font-size:.85rem;font-weight:600}
.ico{display:inline-grid;place-items:center;width:17px;height:17px;border-radius:50%;font-size:.7rem;color:#fff}
.good .ico{background:var(--good)}.warning .ico{background:var(--warning);color:#1d1d1b}.critical .ico{background:var(--critical)}
.meta{margin-top:8px;font-size:.8rem;color:var(--muted)}.meta a{color:var(--accent)}
footer{color:var(--muted);font-size:.8rem;margin-top:18px}a{color:var(--accent)}
"""


def render(views: list[dict], now: datetime, unavailable: bool) -> str:
    e = html.escape
    cards = []
    for v in views:
        hours = ""
        if v["hours"]:
            hours = f'<span class="hours{" open" if v["open"] else ""}">{e(v["hours"])}</span>'
        s = v["status"]
        if s == "ok":
            icon, word = LEVEL_TEXT.get(v["level"] or "", ("", ""))
            lvl = f'<div class="lvl {e(v["level"] or "")}"><span class="ico" aria-hidden="true">{icon}</span>{word}</div>' if word else ""
            headline = (f'<div><div class="big">{v["time_on_site_min"]}<span class="unit"> min</span></div>'
                        f'<div class="label">Time on site now</div>{lvl}</div>')
        else:
            text = {"light_traffic": "Light traffic", "no_recent_trucks": "No recent trucks",
                    "closed": "Closed", "stale": "Data delayed"}[s]
            hint = {"light_traffic": "Too few recent trucks for a time",
                    "no_recent_trucks": "No trucks have weighed out recently",
                    "closed": "", "stale": "Not updated recently. Check back soon"}[s]
            headline = (f'<div><div class="word{" delayed" if s == "stale" else ""}">{text}</div>'
                        f'{f"<div class=label>{hint}</div>" if hint else ""}</div>')
        trucks = ""
        if v["trucks_on_site"] is not None:
            n = v["trucks_on_site"]
            trucks = f'<div><div class="big">{n}</div><div class="label">Truck{"" if n == 1 else "s"} on site now</div></div>'
        meta = []
        if v["last_updated"]:
            meta.append(f'Updated {e(fmt_time(datetime.fromisoformat(v["last_updated"]), now))}')
        if v["address"]:
            meta.append(e(v["address"]))
        if v["map_url"]:
            meta.append(f'<a href="{e(v["map_url"])}" rel="noopener noreferrer" target="_blank">Map</a>')
        cards.append(f'<section class="site" aria-label="{e(v["name"])}"><div class="top">'
                     f'<h2 class="name">{e(v["name"])}</h2>{hours}</div>'
                     f'<div class="row">{headline}{trucks}</div>'
                     f'<div class="meta">{" · ".join(meta)}</div></section>')
    if unavailable:
        body = '<p class="site">Wait times are temporarily unavailable. Please check back in a few minutes.</p>'
    elif not cards:
        body = '<p class="site">No locations are listed right now.</p>'
    else:
        body = "".join(cards)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Grain elevator wait times</title>
<style>{CSS}</style></head>
<body><main>
<h1>Grain elevator wait times</h1>
<p class="sub">Mercer Landmark locations · as of {e(fmt_time(now, now))} · refreshes every minute</p>
{body}
<footer><p>{e(NOTE)} Times are Eastern. Data feed: <a href="feed.json">feed.json</a></p></footer>
</main></body></html>"""


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
}


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method not in ("GET", "HEAD"):
        resp: Response = PlainTextResponse("Method not allowed", status_code=405, headers={"Allow": "GET, HEAD"})
    elif request.url.path != "/healthz" and not limiter.allow(client_key(request)):
        resp = PlainTextResponse("Too many requests. Please wait a minute.", status_code=429,
                                 headers={"Retry-After": "30"})
    else:
        resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def _views() -> tuple[list[dict], datetime, bool]:
    now = datetime.now(timezone.utc)
    rows = snapshot.get()
    return [site_view(r, now) for r in rows or []], now, rows is None


@app.get("/", response_class=HTMLResponse)
def page():
    views, now, unavailable = _views()
    return HTMLResponse(render(views, now, unavailable), status_code=503 if unavailable else 200,
                        headers={"Cache-Control": f"public, max-age={CLIENT_MAX_AGE}"})


@app.get("/feed.json")
def feed():
    views, now, unavailable = _views()
    if unavailable:
        return JSONResponse({"error": "temporarily unavailable"}, status_code=503,
                            headers={"Retry-After": "60", "Access-Control-Allow-Origin": "*"})
    return JSONResponse(
        {"generated_at": now.isoformat(), "timezone": "America/New_York", "note": NOTE, "sites": views},
        headers={"Cache-Control": f"public, max-age={CLIENT_MAX_AGE}", "Access-Control-Allow-Origin": "*"})


@app.get("/healthz")
def healthz():
    snapshot.get()
    return PlainTextResponse("ok" if snapshot.ok else "database unavailable", status_code=200 if snapshot.ok else 503)


@app.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nAllow: /\n")


