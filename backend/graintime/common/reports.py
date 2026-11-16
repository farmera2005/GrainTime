"""Reports: a period report (per site, with a comparison period) and a
truck-level export. All numbers come from common/metrics.py, so reports,
dashboards and the public page always agree.

The period report renders to HTML in two flavours from the same data:
- browser/print: SVG charts, print-friendly CSS ("Save as PDF" in the browser);
- email: tables only, inline styles (Outlook does not show SVG).
"""

from __future__ import annotations

import csv
import html
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from . import metrics
from .models import Site, Ticket
from .normalize import SITE_TZ

BRAND = "Mercer Landmark"
NOTE = ("Time on site is measured from the inbound scale to the outbound scale; time spent in line before "
        "the inbound scale is not included. Times are Eastern. Voided tickets, single-weigh tickets (stored "
        "tares) and stays over the ceiling are left out of the statistics and counted separately.")
COMPARE_LABEL = {"previous": "previous period", "last_year": "same period last year", "none": None}
EXPORT_MAX_ROWS = 250_000


@dataclass
class ReportParams:
    d_from: date
    d_to: date
    site_ids: list[int] = field(default_factory=list)       # empty = every dashboard site
    direction: str = "received"
    commodity: str | None = None
    compare: str = "previous"                                  # previous | last_year | none
    title: str | None = None


def compare_range(p: ReportParams) -> tuple[date, date] | None:
    if p.compare == "previous":
        days = (p.d_to - p.d_from).days + 1
        return p.d_from - timedelta(days=days), p.d_from - timedelta(days=1)
    if p.compare == "last_year":
        def back(d: date) -> date:
            try:
                return d.replace(year=d.year - 1)
            except ValueError:                 # 29 Feb
                return d.replace(year=d.year - 1, day=28)
        return back(p.d_from), back(p.d_to)
    return None


def report_sites(db: DbSession, ids: list[int]) -> list[dict]:
    q = select(Site).where(Site.show_on_dashboard.is_(True), Site.archived_at.is_(None)).order_by(Site.name)
    sites = db.scalars(q).all()
    return [{"id": s.id, "name": s.name, "code": s.code} for s in sites if not ids or s.id in ids]


def fmt_range(a: date, b: date) -> str:
    if a == b:
        return f"{a:%a %b} {a.day}, {a.year}"
    if a.year == b.year:
        return f"{a:%b} {a.day} – {b:%b} {b.day}, {b.year}"
    return f"{a:%b} {a.day}, {a.year} – {b:%b} {b.day}, {b.year}"


def _delta(now: float | None, before: float | None) -> dict | None:
    if now is None or before is None:
        return None
    diff = round(now - before, 1)
    pct = round((now - before) / before * 100) if before else None
    return {"diff": diff, "pct": pct}


def _busiest(rows: list[dict], key: str, label) -> dict | None:
    best = max(rows, key=lambda r: r[key] or 0, default=None)
    if not best or not best[key]:
        return None
    return {"label": label(best), "trucks": best[key]}


def _hour(h: int) -> str:
    return "12 AM" if h == 0 else f"{h} AM" if h < 12 else "12 PM" if h == 12 else f"{h - 12} PM"


def build_period_report(db: DbSession, p: ReportParams, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sites = report_sites(db, p.site_ids)
    ids = [s["id"] for s in sites]
    f = metrics.globals_filters(db, ids, p.d_from, p.d_to, p.direction, p.commodity)
    cr = compare_range(p)
    fc = metrics.globals_filters(db, ids, cr[0], cr[1], p.direction, p.commodity) if cr else None

    overall = metrics.summary(db, f)
    overall_prev = metrics.summary(db, fc) if fc else None
    per_site = []
    for s in sites:
        fs = metrics.globals_filters(db, [s["id"]], p.d_from, p.d_to, p.direction, p.commodity)
        sm = metrics.summary(db, fs)
        prev = metrics.summary(db, metrics.globals_filters(db, [s["id"]], cr[0], cr[1], p.direction,
                                                           p.commodity)) if cr else None
        days = metrics.trend(db, fs)
        hours = metrics.by_hour(db, fs)
        per_site.append({
            **s, **sm, "prev": prev,
            "median_delta": _delta(sm["median_min"], prev["median_min"]) if prev else None,
            "trucks_delta": _delta(sm["trucks"], prev["trucks"]) if prev else None,
            "busiest_day": _busiest(days, "completed", lambda r: f"{date.fromisoformat(r['date']):%a %b %d}"),
            "busiest_hour": _busiest(hours, "trucks", lambda r: _hour(r["hour"])),
            "level": metrics.level(sm["median_min"], f),
        })
    ranking = sorted([s for s in per_site if s["median_min"] is not None],
                     key=lambda s: -s["median_min"]) + [s for s in per_site if s["median_min"] is None]
    trend = metrics.trend(db, f) if (p.d_to - p.d_from).days < 400 else []
    return {
        "title": p.title or "Truck time on site",
        "brand": BRAND,
        "period": {"from": p.d_from.isoformat(), "to": p.d_to.isoformat(), "label": fmt_range(p.d_from, p.d_to)},
        "compare": ({"mode": p.compare, "from": cr[0].isoformat(), "to": cr[1].isoformat(),
                     "label": f"{COMPARE_LABEL[p.compare]} ({fmt_range(*cr)})"} if cr else None),
        "direction": p.direction, "commodity": p.commodity,
        "sites": sites, "generated_at": now.isoformat(),
        "generated_label": now.astimezone(SITE_TZ).strftime("%b %d, %Y %I:%M %p").replace(" 0", " "),
        "thresholds": {"green_max_min": f.green_max_min, "yellow_max_min": f.yellow_max_min},
        "ceiling_hours": f.ceiling_s / 3600,
        "overall": {**overall, "prev": overall_prev,
                    "median_delta": _delta(overall["median_min"], (overall_prev or {}).get("median_min")),
                    "p90_delta": _delta(overall["p90_min"], (overall_prev or {}).get("p90_min")),
                    "trucks_delta": _delta(overall["trucks"], (overall_prev or {}).get("trucks"))},
        "per_site": ranking,
        "trend": trend,
        "by_hour": metrics.by_hour(db, f),
        "commodities": metrics.by_commodity(db, f) if not p.commodity else [],
        "note": NOTE,
    }


# --------------------------------------------------------------------------- #
# Truck-level export
# --------------------------------------------------------------------------- #

EXPORT_COLUMNS = ["Site", "Site code", "Ticket", "Direction", "Commodity", "Status", "Arrived (inbound weigh)",
                  "Left (outbound weigh)", "Minutes on site", "Counted", "Why not counted"]


def _local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(SITE_TZ).replace(tzinfo=None) if dt else None


def truck_rows(db: DbSession, p: ReportParams) -> list[list]:
    """Every truck that arrived in the period (local dates), with the same
    counting rules as the statistics, and why a truck wasn't counted."""
    sites = {s["id"]: s for s in report_sites(db, p.site_ids)}
    f = metrics.globals_filters(db, list(sites), p.d_from, p.d_to, p.direction, p.commodity)
    start, end = metrics.local_bounds(p.d_from, p.d_to)
    q = (select(Ticket).where(Ticket.site_id.in_(list(sites) or [-1]), Ticket.direction == p.direction,
                              Ticket.inbound_at >= start, Ticket.inbound_at < end)
         .order_by(Ticket.inbound_at).limit(EXPORT_MAX_ROWS))
    if p.commodity:
        q = q.where(Ticket.commodity == p.commodity)
    out = []
    for t in db.scalars(q):
        why = None
        if t.status == "voided":
            why = "voided"
        elif t.status == "unknown":
            why = f"unknown status ({t.raw_status})"
        elif t.single_weigh:
            why = "single weigh (stored tare)"
        elif t.status == "open":
            why = "not weighed out yet"
        elif t.duration_s is None:
            why = "no outbound weigh"
        elif t.duration_s > f.ceiling_s:
            why = f"over {f.ceiling_s // 3600} h (probably left open)"
        s = sites[t.site_id]
        out.append([s["name"], s["code"], t.ticket_number or t.source_ticket_id, t.direction, t.commodity,
                    t.status, _local(t.inbound_at), _local(t.outbound_at),
                    round(t.duration_s / 60, 1) if t.duration_s is not None and not t.single_weigh else None,
                    "yes" if why is None else "no", why])
    return out


def trucks_csv(rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    for r in rows:
        w.writerow(["" if v is None else v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")        # BOM: Excel opens it as UTF-8


def trucks_xlsx(rows: list[list], title: str, report: dict | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Trucks"
    ws.append(EXPORT_COLUMNS)
    head = PatternFill("solid", fgColor="1D1D20")
    for c in ws[1]:
        c.font, c.fill, c.alignment = Font(bold=True, color="FFFFFF"), head, Alignment(vertical="center")
    for r in rows:
        ws.append(r)
    widths = [18, 9, 12, 10, 14, 11, 20, 20, 15, 9, 30]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for col in ("G", "H"):
        for cell in ws[col][1:]:
            cell.number_format = "yyyy-mm-dd hh:mm"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(EXPORT_COLUMNS))}{len(rows) + 1}"
    if report:
        sm = wb.create_sheet("Summary")
        sm.append([report["title"], report["period"]["label"]])
        sm.append(["Direction", report["direction"]])
        sm.append([])
        sm.append(["Site", "Trucks", "Counted", "Median (min)", "90th pct (min)", "Voided", "Single weigh",
                   f"Over {int(report['ceiling_hours'])} h"])
        for c in sm[4]:
            c.font = Font(bold=True)
        for s in report["per_site"]:
            e = s["excluded"]
            sm.append([s["name"], s["trucks"], s["completed"], s["median_min"], s["p90_min"], e["voided"],
                       e["single_weigh"], e["over_ceiling"]])
        sm.column_dimensions["A"].width = 24
    wb.properties.title = title
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

def _n(v, unit: str = "") -> str:
    if v is None:
        return "–"
    s = f"{v:,.1f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"
    return s + unit


def _delta_html(d: dict | None, unit: str, good_when_down: bool | None = True, email: bool = False) -> str:
    """A change vs the comparison period. good_when_down=None: neutral (truck
    volume going up or down is neither good nor bad)."""
    if not d or d["diff"] is None:
        return ""
    if d["diff"] == 0:
        return '<span class="d">no change</span>' if not email else "no change"
    up = d["diff"] > 0
    better = None if good_when_down is None else (not up) if good_when_down else up
    arrow = "▲" if up else "▼"
    pct = f" ({'+' if up else ''}{d['pct']}%)" if d.get("pct") is not None else ""
    txt = f"{arrow} {'+' if up else ''}{_n(d['diff'])}{unit}{pct}"
    if email:
        color = "#5c5c63" if better is None else "#1a7f37" if better else "#b42318"
        return f'<span style="color:{color};font-weight:600">{txt}</span>'
    return f'<span class="d {"" if better is None else "better" if better else "worse"}">{txt}</span>'


LEVEL_WORD = {"good": ("Short", "#0ca30c"), "warning": ("Moderate", "#b07800"), "critical": ("Long", "#d03b3b")}


def _svg_trend(rows: list[dict]) -> str:
    pts = [r for r in rows]
    vals = [v for r in pts for v in (r["median_min"], r["p90_min"]) if v is not None]
    if not vals or len(pts) < 2:
        return ""
    W, H, L, R, T, B = 720, 200, 40, 70, 10, 26
    vmax = max(vals)
    step = next(s for s in (1, 2, 5, 10, 15, 20, 30, 60, 120) if vmax / s <= 5)
    top = step * (int(vmax / step) + 1)
    x = lambda i: L + i * (W - L - R) / (len(pts) - 1)  # noqa: E731
    y = lambda v: T + (H - T - B) * (1 - v / top)        # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Median and 90th percentile time on site by day">']
    for t in range(0, top + 1, step):
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                   f'<text x="{L - 6}" y="{y(t) + 4:.1f}" class="ax" text-anchor="end">{t}</text>')
    every = max(1, len(pts) // 8)
    for i, r in enumerate(pts):
        if i % every == 0:
            d = date.fromisoformat(r["date"])
            out.append(f'<text x="{x(i):.1f}" y="{H - 8}" class="ax" text-anchor="middle">{d:%b} {d.day}</text>')
    for key, cls in (("p90_min", "s2"), ("median_min", "s1")):
        path, pen = "", False
        for i, r in enumerate(pts):
            v = r[key]
            if v is None:
                pen = False
                continue
            path += f'{"L" if pen else "M"}{x(i):.1f},{y(v):.1f}'
            pen = True
        out.append(f'<path d="{path}" class="{cls}"/>')
        last = next((i for i in range(len(pts) - 1, -1, -1) if pts[i][key] is not None), None)
        if last is not None:
            out.append(f'<circle cx="{x(last):.1f}" cy="{y(pts[last][key]):.1f}" r="3.5" class="{cls}d"/>'
                       f'<text x="{x(last) + 7:.1f}" y="{y(pts[last][key]) + 4:.1f}" class="lab">'
                       f'{_n(pts[last][key])} min</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_hours(rows: list[dict]) -> str:
    used = [r for r in rows if r["trucks"]]
    if not used:
        return ""
    h0, h1 = max(0, min(r["hour"] for r in used) - 1), min(23, max(r["hour"] for r in used) + 1)
    rows = [r for r in rows if h0 <= r["hour"] <= h1]
    W, H, L, T, B = 380, 160, 30, 10, 24             # drawn at its size in a half-width column
    vmax = max(r["trucks"] for r in rows)
    band = (W - L - 10) / len(rows)
    bw = min(22, band - 4)
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Trucks arriving by hour of day">',
           f'<line x1="{L}" x2="{W - 10}" y1="{H - B}" y2="{H - B}" class="base"/>']
    for i, r in enumerate(rows):
        h = (H - T - B) * r["trucks"] / vmax if vmax else 0
        xx = L + i * band + (band - bw) / 2
        if h:
            out.append(f'<rect x="{xx:.1f}" y="{H - B - h:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="3" class="bar">'
                       f'<title>{_hour(r["hour"])}: {r["trucks"]} trucks</title></rect>')
        if i % max(1, round(len(rows) / 6)) == 0:
            out.append(f'<text x="{xx + bw / 2:.1f}" y="{H - 8}" class="ax" text-anchor="middle">'
                       f'{_hour(r["hour"]).replace(" ", "").lower()}</text>')
    out.append(f'<text x="{L - 6}" y="{T + 8}" class="ax" text-anchor="end">{vmax}</text></svg>')
    return "".join(out)


CSS = """
:root{--ink:#1b1b1d;--muted:#5c5c63;--line:#e2e2e0;--red:#ee3324;--red-ink:#b5201a;--s1:#2a78d6;--s2:#eb6834}
*{box-sizing:border-box}body{margin:0;color:var(--ink);background:#fff;font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.page{max-width:900px;margin:0 auto;padding:28px 28px 40px}
.top{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:4px solid var(--red);padding-bottom:10px}
.brand{font-weight:800;letter-spacing:.12em;text-transform:uppercase;font-size:.75rem;color:var(--red-ink)}
h1{font-size:1.5rem;margin:2px 0 0}h2{font-size:1.05rem;margin:26px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--line)}
.meta{color:var(--muted);font-size:.85rem;text-align:right}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:10px 12px}.kpi .k{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:700}
.kpi .v{font-size:1.6rem;font-weight:750;font-variant-numeric:tabular-nums}.kpi .v small{font-size:.85rem;font-weight:500;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:700}th:first-child,td:first-child{text-align:left}
.d{font-size:.8rem;font-weight:600;color:var(--muted)}.better{color:#1a7f37}.worse{color:#b42318}
.lvl{display:inline-block;font-size:.72rem;font-weight:700;padding:1px 7px;border-radius:99px;margin-left:6px;vertical-align:1px}
.lvl.good{background:#e6f4e6}.lvl.warning{background:#fdf3dc}.lvl.critical{background:#fbe7e7}
.chart{width:100%;height:auto;display:block}.grid{stroke:#e1e0d9}.base{stroke:#898781}.ax{fill:#898781;font-size:11px}
.lab{fill:var(--ink);font-size:11px}.s1{fill:none;stroke:var(--s1);stroke-width:2}.s2{fill:none;stroke:var(--s2);stroke-width:2}
.s1d{fill:var(--s1);stroke:#fff;stroke-width:2}.s2d{fill:var(--s2);stroke:#fff;stroke-width:2}.bar{fill:var(--s1)}
.legend{display:flex;gap:16px;font-size:.8rem;margin-bottom:4px}.legend i{display:inline-block;width:14px;height:2px;vertical-align:middle;margin-right:6px}
.note{color:var(--muted);font-size:.8rem;margin-top:24px;border-top:1px solid var(--line);padding-top:10px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media print{.page{padding:0}h2{break-after:avoid}table,.kpis,.chart{break-inside:avoid}@page{margin:14mm}}
@media (max-width:640px){.kpis{grid-template-columns:1fr 1fr}.cols{grid-template-columns:1fr}.top{display:block}.meta{text-align:left}}
"""


def render_html(r: dict, email: bool = False, link: str | None = None) -> str:
    e = html.escape
    o = r["overall"]
    cmp = r["compare"]
    filt = (f"{'Received' if r['direction'] == 'received' else 'Shipped'} trucks"
            + (f" · {e(r['commodity'])}" if r["commodity"] else "")
            + f" · {len(r['sites'])} site{'s' if len(r['sites']) != 1 else ''}")
    if email:
        return _render_email(r, filt, link)
    k = [("Trucks", _n(o["trucks"]), _delta_html(o["trucks_delta"], "", good_when_down=None)),
         ("Median time on site", _n(o["median_min"], "<small> min</small>"), _delta_html(o["median_delta"], " min")),
         ("90th percentile", _n(o["p90_min"], "<small> min</small>"), _delta_html(o["p90_delta"], " min")),
         ("Counted / left out", f"{_n(o['completed'])}<small> / {_n(sum(o['excluded'].values()))}</small>", "")]
    kpis = "".join(f'<div class="kpi"><div class="k">{a}</div><div class="v">{b}</div>{c}</div>' for a, b, c in k)
    rows = []
    for s in r["per_site"]:
        lvl = s.get("level")
        badge = f'<span class="lvl {lvl}">{LEVEL_WORD[lvl][0]}</span>' if lvl in LEVEL_WORD else ""
        rows.append(f"<tr><td>{e(s['name'])}{badge}</td><td>{_n(s['trucks'])}</td><td>{_n(s['median_min'])}</td>"
                    f"<td>{_delta_html(s['median_delta'], ' min')}</td><td>{_n(s['p90_min'])}</td>"
                    f"<td>{e(s['busiest_day']['label']) if s['busiest_day'] else '–'}</td>"
                    f"<td>{e(s['busiest_hour']['label']) if s['busiest_hour'] else '–'}</td></tr>")
    sites_tbl = ("<table><thead><tr><th>Site (longest first)</th><th>Trucks</th><th>Median min</th>"
                 f"<th>vs {e(COMPARE_LABEL[cmp['mode']]) if cmp else ''}</th><th>90th pct min</th>"
                 "<th>Busiest day</th><th>Busiest hour</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")
    trend = _svg_trend(r["trend"])
    trend_html = (f'<h2>Time on site by day</h2><div class="legend"><span><i style="background:#2a78d6"></i>Median'
                  f'</span><span><i style="background:#eb6834"></i>90th percentile</span></div>{trend}') if trend else ""
    hours = _svg_hours(r["by_hour"])
    com = "".join(f"<tr><td>{e(c['commodity'])}</td><td>{_n(c['trucks'])}</td><td>{_n(c['median_min'])}</td>"
                  f"<td>{_n(c['p90_min'])}</td></tr>" for c in r["commodities"])
    ex = o["excluded"]
    excl = (f"<table><tbody><tr><td>Counted</td><td>{_n(o['completed'])}</td></tr>"
            f"<tr><td>Voided</td><td>{_n(ex['voided'])}</td></tr>"
            f"<tr><td>Single weigh (stored tare)</td><td>{_n(ex['single_weigh'])}</td></tr>"
            f"<tr><td>Over {_n(r['ceiling_hours'])} h</td><td>{_n(ex['over_ceiling'])}</td></tr>"
            + (f"<tr><td>Unknown status</td><td>{_n(ex['unknown_status'])}</td></tr>" if ex["unknown_status"] else "")
            + "</tbody></table>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(r['title'])} · {e(r['period']['label'])}</title>
<style>{CSS}</style></head><body><div class="page">
<div class="top"><div><div class="brand">{e(r['brand'])} · GrainTime</div><h1>{e(r['title'])}</h1>
<div>{e(r['period']['label'])} · {filt}</div></div>
<div class="meta">Generated {e(r['generated_label'])}{('<br>Compared with ' + e(cmp['label'])) if cmp else ''}</div></div>
<div class="kpis">{kpis}</div>
<h2>Sites</h2>{sites_tbl}
{trend_html}
<div class="cols"><div><h2>Trucks arriving by hour</h2>{hours or '<p>No trucks.</p>'}</div>
<div><h2>Tickets counted and left out</h2>{excl}</div></div>
{('<h2>Commodities</h2><table><thead><tr><th>Commodity</th><th>Trucks</th><th>Median min</th><th>90th pct min</th></tr></thead><tbody>' + com + '</tbody></table>') if com else ''}
<p class="note">{e(r['note'])}</p></div></body></html>"""


def _render_email(r: dict, filt: str, link: str | None) -> str:
    """Tables and inline styles only (Outlook-safe)."""
    e = html.escape
    o = r["overall"]
    cmp = r["compare"]
    td = 'style="padding:6px 8px;border-bottom:1px solid #e2e2e0;text-align:right;white-space:nowrap"'
    tdl = 'style="padding:6px 8px;border-bottom:1px solid #e2e2e0;text-align:left"'
    th = ('style="padding:6px 8px;border-bottom:2px solid #1d1d20;text-align:right;font-size:11px;'
          'text-transform:uppercase;color:#5c5c63"')
    thl = th.replace("text-align:right", "text-align:left")
    kpi = ('<td style="padding:10px 12px;border:1px solid #e2e2e0;width:25%;vertical-align:top">'
           '<div style="font-size:11px;text-transform:uppercase;color:#5c5c63;font-weight:700">{}</div>'
           '<div style="font-size:24px;font-weight:700">{}</div><div style="font-size:12px">{}</div></td>')
    kpis = "".join(kpi.format(a, b, c) for a, b, c in [
        ("Trucks", _n(o["trucks"]), _delta_html(o["trucks_delta"], "", None, True)),
        ("Median", _n(o["median_min"], " min"), _delta_html(o["median_delta"], " min", True, True)),
        ("90th percentile", _n(o["p90_min"], " min"), _delta_html(o["p90_delta"], " min", True, True)),
        ("Left out", _n(sum(o["excluded"].values())), "voided, single weigh, over ceiling")])
    vmax = max([s["median_min"] or 0 for s in r["per_site"]] + [1])
    rows = []
    for s in r["per_site"]:
        lvl = LEVEL_WORD.get(s.get("level") or "")
        bar = (f'<table cellpadding="0" cellspacing="0" style="width:120px"><tr><td style="background:#2a78d6;height:8px;'
               f'width:{int(120 * (s["median_min"] or 0) / vmax)}px"></td><td></td></tr></table>') if s["median_min"] else ""
        rows.append(f"<tr><td {tdl}>{e(s['name'])}"
                    + (f' <span style="color:{lvl[1]};font-size:11px;font-weight:700">{lvl[0]}</span>' if lvl else "")
                    + f"</td><td {td}>{_n(s['trucks'])}</td><td {td}>{_n(s['median_min'])}</td>"
                    f"<td {td}>{bar}</td><td {td}>{_delta_html(s['median_delta'], ' min', True, True)}</td>"
                    f"<td {td}>{_n(s['p90_min'])}</td></tr>")
    busiest = [s for s in r["per_site"] if s["busiest_hour"]]
    peak = max(r["by_hour"], key=lambda h: h["trucks"], default=None)
    lines = []
    if peak and peak["trucks"]:
        lines.append(f"Busiest hour overall: {_hour(peak['hour'])} ({peak['trucks']} trucks arriving).")
    if busiest:
        worst = r["per_site"][0]
        if worst["median_min"] is not None:
            lines.append(f"Longest median time on site: {e(worst['name'])} ({_n(worst['median_min'])} min).")
    return f"""<!doctype html><html><body style="margin:0;background:#f4f4f3">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f3"><tr><td align="center" style="padding:16px">
<table width="680" cellpadding="0" cellspacing="0" style="background:#ffffff;font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1b1b1d">
<tr><td style="padding:18px 22px 10px;border-bottom:4px solid #ee3324">
<div style="font-size:11px;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#b5201a">{e(r['brand'])} · GrainTime</div>
<div style="font-size:20px;font-weight:700;margin-top:2px">{e(r['title'])}</div>
<div style="color:#5c5c63">{e(r['period']['label'])} · {filt}{(' · compared with ' + e(cmp['label'])) if cmp else ''}</div></td></tr>
<tr><td style="padding:14px 22px"><table width="100%" cellpadding="0" cellspacing="0"><tr>{kpis}</tr></table></td></tr>
<tr><td style="padding:4px 22px">{''.join(f'<p style="margin:4px 0">{x}</p>' for x in lines)}</td></tr>
<tr><td style="padding:8px 22px"><div style="font-weight:700;margin:8px 0">Sites, longest median first</div>
<table width="100%" cellpadding="0" cellspacing="0"><tr><th {thl}>Site</th><th {th}>Trucks</th><th {th}>Median min</th>
<th {th}></th><th {th}>vs {e(COMPARE_LABEL[cmp['mode']]) if cmp else ''}</th><th {th}>90th pct</th></tr>{''.join(rows)}</table></td></tr>
{f'<tr><td style="padding:14px 22px"><a href="{e(link)}" style="color:#b5201a;font-weight:700">Open the full report in GrainTime</a></td></tr>' if link else ''}
<tr><td style="padding:10px 22px 18px;color:#5c5c63;font-size:12px">{e(r['note'])}<br>Generated {e(r['generated_label'])}.</td></tr>
</table></td></tr></table></body></html>"""


def render_text(r: dict) -> str:
    """Plain-text alternative for email clients that don't show HTML."""
    o = r["overall"]
    lines = [f"{r['title']} - {r['period']['label']}", "",
             f"Trucks: {_n(o['trucks'])}   Median: {_n(o['median_min'])} min   90th pct: {_n(o['p90_min'])} min", ""]
    for s in r["per_site"]:
        lines.append(f"{s['name']}: {_n(s['trucks'])} trucks, median {_n(s['median_min'])} min, "
                     f"90th {_n(s['p90_min'])} min")
    lines += ["", r["note"]]
    return "\n".join(lines)
