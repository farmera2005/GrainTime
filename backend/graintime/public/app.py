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
           "level", "trucks_on_site", "data_as_of", "computed_at", "stale_after_min", "green_max_min",
           "yellow_max_min")
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
        "level_limits": ({"short_max_min": r["green_max_min"], "moderate_max_min": r["yellow_max_min"]}
                         if r.get("green_max_min") and r.get("yellow_max_min") else None),
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


LEVELS = {"good": ("Short", "✓"), "warning": ("Moderate", "!"), "critical": ("Long", "✕")}
BRAND = "Mercer Landmark"

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='7' fill='%231f4d2b'/%3E%3Cpath d='M9 24V13l7-5 7 5v11h-4v-6h-6v6z' "
           "fill='%23fff'/%3E%3C/svg%3E")

# Inline icons (no external requests; the CSP allows none).
ICON_PIN = ('<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a7 7 0 0 0-7 7c0 5.2 7 13 7 13s7-7.8 '
            '7-13a7 7 0 0 0-7-7zm0 9.5A2.5 2.5 0 1 1 12 6.5a2.5 2.5 0 0 1 0 5z" fill="currentColor"/></svg>')
ICON_CLOCK = ('<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" '
              'stroke="currentColor" stroke-width="2"/><path d="M12 7v5l3 2" fill="none" stroke="currentColor" '
              'stroke-width="2" stroke-linecap="round"/></svg>')
ICON_TRUCK = ('<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h11v9H3zM14 9h4l3 3v3h-7z" '
              'fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><circle cx="7" cy="17.5" '
              'r="1.8" fill="currentColor"/><circle cx="17" cy="17.5" r="1.8" fill="currentColor"/></svg>')

CSS = """
:root{--bg:#f3f4f1;--card:#fff;--text:#18201b;--muted:#5b665f;--faint:#8a948d;--border:#dfe3dd;
--brand:#1f4d2b;--brand-2:#2f6b3a;--brand-ink:#fff;--accent:#2f6b3a;
--good:#0ca30c;--warning:#e09c00;--critical:#d03b3b;
--good-bg:#e6f4e6;--warning-bg:#fdf3dc;--critical-bg:#fbe7e7;--neutral-bg:#eef0ec;
--zone-good:#bfe3bf;--zone-warning:#f7dd9c;--zone-critical:#f1b9b9;
--shadow:0 1px 2px rgb(16 24 18/.06),0 4px 16px rgb(16 24 18/.06);color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#111512;--card:#1a1f1b;--text:#e8ede9;--muted:#a3ada6;
--faint:#7c867f;--border:#2c342e;--brand:#173a21;--brand-2:#245730;--accent:#6dbb7d;
--good:#3fbf3f;--warning:#f0b429;--critical:#ec6a6a;
--good-bg:#17301b;--warning-bg:#33290f;--critical-bg:#3a1b1b;--neutral-bg:#232924;
--zone-good:#1f4d26;--zone-warning:#5a4613;--zone-critical:#5c2525;--shadow:none;color-scheme:dark}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
a{color:var(--accent)}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
header.top{background:linear-gradient(135deg,var(--brand),var(--brand-2));color:var(--brand-ink);padding:22px 0 54px}
.top .wrap{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap}
.brand{font-size:.75rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;opacity:.85}
h1{font-size:clamp(1.5rem,4.5vw,2.1rem);line-height:1.15;margin:4px 0 0;font-weight:750;letter-spacing:-.01em}
.live{display:inline-flex;align-items:center;gap:8px;background:rgb(255 255 255/.12);
border:1px solid rgb(255 255 255/.22);border-radius:999px;padding:6px 12px;font-size:.85rem;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:#7ee08a;box-shadow:0 0 0 3px rgb(126 224 138/.25)}
main.wrap{margin-top:-34px;padding-bottom:40px}
.summary{background:var(--card);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);
display:flex;flex-wrap:wrap;gap:0;margin-bottom:18px;overflow:hidden}
.summary>div{flex:1 1 160px;padding:14px 18px;border-right:1px solid var(--border)}
.summary>div:last-child{border-right:none}
.summary .k{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:650}
.summary .v{font-size:1.15rem;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}
.summary .v small{font-weight:500;color:var(--muted);font-size:.85rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:16px}
.site{background:var(--card);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);
display:flex;flex-direction:column;overflow:hidden}
.site-head{padding:16px 18px 0;display:flex;justify-content:space-between;align-items:flex-start;gap:6px 10px;flex-wrap:wrap}
.site-head>div{flex:1 1 140px;min-width:0}
.site h2{font-size:1.15rem;margin:0;line-height:1.25;font-weight:700}
.addr{font-size:.85rem;color:var(--muted);margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:3px 10px;font-size:.78rem;
font-weight:650;white-space:nowrap;background:var(--neutral-bg);color:var(--muted)}
.pill.open{background:var(--good-bg);color:var(--text)}
.pill.open::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--good)}
.pill.closed::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--faint)}
.stats{display:grid;grid-template-columns:1fr auto;gap:12px;padding:14px 18px 6px;align-items:start}
.stat .k{display:flex;align-items:center;gap:6px;font-size:.78rem;color:var(--muted);font-weight:650;
text-transform:uppercase;letter-spacing:.05em}
.big{font-size:2.6rem;font-weight:780;line-height:1.05;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.big .u{font-size:1rem;font-weight:550;color:var(--muted);letter-spacing:0;margin-left:3px}
.stat.trucks{text-align:right;padding-left:16px;border-left:1px solid var(--border)}
.stat.trucks .k{justify-content:flex-end}
.stat.trucks .big{font-size:2rem}
.word{font-size:1.45rem;font-weight:720;line-height:1.2;margin-top:4px}
.word.muted{color:var(--muted)}
.hint{font-size:.83rem;color:var(--muted);margin-top:2px}
.chip{display:inline-flex;align-items:center;gap:6px;margin-top:8px;padding:3px 10px 3px 4px;border-radius:999px;
font-size:.82rem;font-weight:650}
.chip .ico{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;font-size:.7rem;color:#fff;font-weight:800}
.chip.good{background:var(--good-bg)}.chip.good .ico{background:var(--good)}
.chip.warning{background:var(--warning-bg)}.chip.warning .ico{background:var(--warning);color:#1d1d1b}
.chip.critical{background:var(--critical-bg)}.chip.critical .ico{background:var(--critical)}
.chip.delayed{background:var(--warning-bg)}.chip.delayed .ico{background:var(--warning);color:#1d1d1b}
.gauge{padding:6px 18px 4px}
.bar{position:relative;height:8px;display:flex;gap:2px;border-radius:999px}
.bar span{display:block;height:100%}
.bar span:first-child{border-radius:999px 0 0 999px}.bar span:nth-child(3){border-radius:0 999px 999px 0}
.z1{background:var(--zone-good)}.z2{background:var(--zone-warning)}.z3{background:var(--zone-critical)}
.mark{position:absolute;top:-4px;width:4px;height:16px;margin-left:-2px;border-radius:2px;background:var(--text);
box-shadow:0 0 0 2px var(--card)}
.scale{position:relative;height:16px;font-size:.7rem;color:var(--faint);font-variant-numeric:tabular-nums;margin-top:3px}
.scale span{position:absolute;transform:translateX(-50%)}
.scale span:first-child{transform:none}
.site-foot{margin-top:auto;padding:10px 18px 14px;display:flex;justify-content:space-between;gap:10px;
flex-wrap:wrap;font-size:.8rem;color:var(--muted);border-top:1px solid var(--border);margin-top:12px}
.site-foot a{display:inline-flex;align-items:center;gap:4px;font-weight:600;text-decoration:none}
.site-foot a:hover{text-decoration:underline}
.i{width:15px;height:15px;flex:none}
.notice{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:22px;text-align:center;
color:var(--muted);box-shadow:var(--shadow)}
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;margin:22px 0 6px;font-size:.85rem;color:var(--muted)}
.legend .chip{margin:0}.legend>span{display:inline-flex;align-items:center;gap:8px}
footer{border-top:1px solid var(--border);margin-top:22px;padding-top:16px;font-size:.82rem;color:var(--muted);
display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
footer p{margin:0;max-width:640px}
@media (max-width:420px){.big{font-size:2.25rem}.stat.trucks .big{font-size:1.7rem}.summary>div{flex-basis:50%;border-bottom:1px solid var(--border)}
.summary>div:nth-child(2n){border-right:none}}
@media print{header.top{background:none;color:#000;padding:0}.live{border-color:#999}.site,.summary{box-shadow:none}}
"""


def _level_chip(level: str | None) -> str:
    if level not in LEVELS:
        return ""
    word, icon = LEVELS[level]
    return f'<span class="chip {level}"><span class="ico" aria-hidden="true">{icon}</span>{word}</span>'


def _gauge(v: dict) -> str:
    """A three-zone bar (short / moderate / long) with a marker at the current time."""
    lim, m = v["level_limits"], v["time_on_site_min"]
    if not lim or m is None:
        return ""
    g, y = lim["short_max_min"], lim["moderate_max_min"]
    top = max(y + g, int(m * 1.1) + 1)
    pct = lambda x: round(min(max(x, 0), top) / top * 100, 2)  # noqa: E731
    word = LEVELS.get(v["level"] or "", ("", ""))[0].lower()
    label = f"{m} minutes on site, {word} (short is up to {g} minutes, moderate up to {y})"
    return (f'<div class="gauge" role="img" aria-label="{html.escape(label)}"><div class="bar">'
            f'<span class="z1" style="width:{pct(g)}%"></span><span class="z2" style="width:{pct(y) - pct(g)}%"></span>'
            f'<span class="z3" style="flex:1"></span><span class="mark" style="left:{pct(m)}%"></span></div>'
            f'<div class="scale" aria-hidden="true"><span style="left:0">0</span><span style="left:{pct(g)}%">{g}</span>'
            f'<span style="left:{pct(y)}%">{y}</span></div></div>')


STATE_TEXT = {
    "light_traffic": ("Light traffic", "Too few recent trucks to give a time"),
    "no_recent_trucks": ("No recent trucks", "No trucks have weighed out recently"),
    "closed": ("Closed", ""),
    "stale": ("Data delayed", "Not updated recently. Please check back soon"),
}


def _card(v: dict, now: datetime) -> str:
    e = html.escape
    pill = ""
    if v["hours"]:
        pill = f'<span class="pill {"open" if v["open"] else "closed"}">{e(v["hours"])}</span>'
    addr = f'<div class="addr">{e(v["address"])}</div>' if v["address"] else ""
    s = v["status"]
    if s == "ok":
        main = (f'<div class="stat"><div class="k">{ICON_CLOCK}Time on site now</div>'
                f'<div class="big">{v["time_on_site_min"]}<span class="u">min</span></div>{_level_chip(v["level"])}</div>')
    else:
        word, hint = STATE_TEXT[s]
        chip = '<span class="chip delayed"><span class="ico" aria-hidden="true">!</span>Data delayed</span>' if s == "stale" else ""
        main = (f'<div class="stat"><div class="k">{ICON_CLOCK}Time on site now</div>'
                f'<div class="word{" muted" if s in ("closed", "stale") else ""}">{word if s != "stale" else "Not available"}</div>'
                f'{f"<div class=hint>{e(hint)}</div>" if hint else ""}{chip}</div>')
    trucks = ""
    if v["trucks_on_site"] is not None:
        n = v["trucks_on_site"]
        trucks = (f'<div class="stat trucks"><div class="k">{ICON_TRUCK}On site</div>'
                  f'<div class="big">{n}</div><div class="hint">truck{"" if n == 1 else "s"}</div></div>')
    foot = [f'<span>Updated {e(fmt_time(datetime.fromisoformat(v["last_updated"]), now))}</span>'
            if v["last_updated"] else "<span>No data yet</span>"]
    if v["map_url"]:
        foot.append(f'<a href="{e(v["map_url"])}" rel="noopener noreferrer" target="_blank">{ICON_PIN}Directions</a>')
    return (f'<article class="site" aria-label="{e(v["name"])}">'
            f'<div class="site-head"><div><h2>{e(v["name"])}</h2>{addr}</div>{pill}</div>'
            f'<div class="stats">{main}{trucks}</div>{_gauge(v)}'
            f'<div class="site-foot">{"".join(foot)}</div></article>')


def _summary(views: list[dict]) -> str:
    if not views:
        return ""
    open_known = [v for v in views if v["open"] is not None]
    n_open = sum(1 for v in open_known if v["open"])
    timed = [v for v in views if v["status"] == "ok"]
    cells = [f'<div><div class="k">Locations</div><div class="v">{len(views)}</div></div>']
    if open_known:
        cells.append(f'<div><div class="k">Open now</div><div class="v">{n_open} <small>of {len(open_known)}</small></div></div>')
    if timed:
        best = min(timed, key=lambda v: v["time_on_site_min"])
        cells.append(f'<div><div class="k">Shortest time on site</div><div class="v">{best["time_on_site_min"]} min '
                     f'<small>{html.escape(best["name"])}</small></div></div>')
    trucks = [v["trucks_on_site"] for v in views if v["trucks_on_site"] is not None]
    if trucks:
        cells.append(f'<div><div class="k">Trucks on site</div><div class="v">{sum(trucks)} <small>all locations</small></div></div>')
    return f'<section class="summary" aria-label="Summary">{"".join(cells)}</section>'


def _legend(views: list[dict]) -> str:
    lim = next((v["level_limits"] for v in views if v["level_limits"]), None)
    if not lim:
        return ""
    g, y = lim["short_max_min"], lim["moderate_max_min"]
    return (f'<div class="legend" aria-label="What the labels mean"><span>{_level_chip("good")} up to {g} min</span>'
            f'<span>{_level_chip("warning")} {g}–{y} min</span><span>{_level_chip("critical")} over {y} min</span></div>')


def render(views: list[dict], now: datetime, unavailable: bool) -> str:
    e = html.escape
    if unavailable:
        body = '<div class="notice">Wait times are temporarily unavailable. Please check back in a few minutes.</div>'
    elif not views:
        body = '<div class="notice">No locations are listed right now.</div>'
    else:
        body = (_summary(views) + '<section class="grid" aria-label="Locations">'
                + "".join(_card(v, now) for v in views) + "</section>" + _legend(views))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<meta name="description" content="Current truck time on site at {e(BRAND)} grain elevators, updated every minute.">
<meta name="theme-color" content="#1f4d2b">
<link rel="icon" href="{FAVICON}">
<title>Grain Elevator Wait Times · {e(BRAND)}</title>
<style>{CSS}</style></head>
<body>
<header class="top"><div class="wrap">
<div><div class="brand">{e(BRAND)}</div><h1>Grain elevator wait times</h1></div>
<div class="live" role="status"><span class="dot" aria-hidden="true"></span>Live · updated {e(fmt_time(now, now))}</div>
</div></header>
<main class="wrap">
{body}
<footer>
<p>{e(NOTE)} Times are Eastern and refresh every minute.</p>
<p><a href="feed.json">Data feed (JSON)</a> · © {now.astimezone(TZ).year} {e(BRAND)}</p>
</footer>
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


