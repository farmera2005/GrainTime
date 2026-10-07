// Statistics dashboard. Each user keeps up to 20 dashboards; each one has a
// filter row (date range, sites, direction, commodity) and a list of widgets
// that can be added, removed, reordered, resized and configured.

import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, ByHour, CompareRow, Dashboard, DashFilters, DistBin, HeatCell, KpiMetric, Level, RangeKey,
  SiteOverview, statsQuery, StatsOptions, StatsQuery, Summary, TrendDay, Widget, WidgetType,
} from "../api";
import { Columns, DataTable, HBars, Heatmap, hourLabel, Lines, TipRow } from "../charts/charts";
import { Notice } from "../components/Field";
import { ago } from "../format";

const REFRESH_MS = 60_000;

// --------------------------------------------------------------------------- //
// Date ranges (local dates, America/New_York, resolved against the server's today)
// --------------------------------------------------------------------------- //

const RANGES: { key: RangeKey; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "7d", label: "Last 7 days" },
  { key: "30d", label: "Last 30 days" },
  { key: "90d", label: "Last 90 days" },
  { key: "season", label: "Harvest season (since Sep 1)" },
  { key: "ytd", label: "Year to date" },
  { key: "custom", label: "Custom…" },
];

function addDays(iso: string, n: number): string {
  const d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export function resolveRange(f: DashFilters, today: string): { from: string; to: string } {
  const y = Number(today.slice(0, 4));
  switch (f.range) {
    case "today": return { from: today, to: today };
    case "yesterday": return { from: addDays(today, -1), to: addDays(today, -1) };
    case "7d": return { from: addDays(today, -6), to: today };
    case "30d": return { from: addDays(today, -29), to: today };
    case "90d": return { from: addDays(today, -89), to: today };
    case "season": {
      const start = today >= `${y}-09-01` ? `${y}-09-01` : `${y - 1}-09-01`;
      return { from: start, to: today };
    }
    case "ytd": return { from: `${y}-01-01`, to: today };
    default: return { from: f.date_from || today, to: f.date_to || today };
  }
}

const dayFmt = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const dayLong = new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const shortDay = (iso: string) => dayFmt.format(new Date(iso + "T12:00:00Z"));
const longDay = (iso: string) => dayLong.format(new Date(iso + "T12:00:00Z"));

function rangeText(r: { from: string; to: string }): string {
  return r.from === r.to ? longDay(r.from) : `${shortDay(r.from)} – ${longDay(r.to)}`;
}

// --------------------------------------------------------------------------- //
// Data loading: one request per (kind, query, refresh tick), shared by widgets
// --------------------------------------------------------------------------- //

// Widgets that need the same numbers share one request. Entries are short-lived
// so a remounted page or a new site never shows stale results.
const cache = new Map<string, { at: number; p: Promise<unknown> }>();
const CACHE_MS = 20_000;

function useStats<T>(kind: string, q: StatsQuery, tick: number): { data: T | null; error: string | null } {
  const key = `${kind}?${statsQuery(q)}#${tick}`;
  const [state, setState] = useState<{ key: string; data: T | null; error: string | null }>({ key: "", data: null, error: null });
  useEffect(() => {
    let live = true;
    const now = Date.now();
    for (const [k, v] of cache) if (now - v.at > CACHE_MS) cache.delete(k);
    let p = cache.get(key)?.p as Promise<T> | undefined;
    if (!p) {
      p = api.stats<T>(kind, q);
      cache.set(key, { at: now, p });
      p.catch(() => cache.delete(key));
    }
    p.then((data) => live && setState({ key, data, error: null }))
      .catch((e) => live && setState({ key, data: null, error: e.message || "Could not load" }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  // Keep showing the previous data while a refresh loads, so tiles don't flicker.
  return { data: state.data, error: state.error };
}

// --------------------------------------------------------------------------- //
// Widget catalogue
// --------------------------------------------------------------------------- //

const WIDGETS: Record<WidgetType, { title: string; help: string; size: Widget["size"] }> = {
  kpi: { title: "Number tile", help: "One headline number: current time on site, trucks on site now, trucks today, median, 90th percentile or completed trucks.", size: "S" },
  sites_table: { title: "Sites right now", help: "Every site with its current time on site, trucks on site, today's counts and last update. Sortable.", size: "L" },
  trend: { title: "Time on site by day", help: "Daily median (and 90th percentile) time on site over the date range.", size: "L" },
  by_hour: { title: "By hour of day", help: "Trucks arriving and median time on site for each hour of the day.", size: "M" },
  heatmap: { title: "Day × hour heatmap", help: "Median time on site for each weekday and hour, to spot the busy times.", size: "M" },
  compare: { title: "Compare sites", help: "Median time on site for each site over the date range.", size: "M" },
  distribution: { title: "How long trucks stay", help: "How many trucks fall in each 5-minute band of time on site.", size: "M" },
  exclusions: { title: "Tickets included and excluded", help: "How many tickets count toward the statistics and how many were left out, and why.", size: "M" },
};

const KPI: Record<KpiMetric, { title: string; live: boolean }> = {
  current: { title: "Current time on site", live: true },
  on_site_now: { title: "Trucks on site now", live: true },
  trucks_today: { title: "Trucks today", live: true },
  median: { title: "Median time on site", live: false },
  p90: { title: "90th percentile time on site", live: false },
  completed: { title: "Completed trucks", live: false },
};

function widgetTitle(w: Widget): string {
  if (w.title) return w.title;
  if (w.type === "kpi") return KPI[w.settings.metric || "current"].title;
  return WIDGETS[w.type].title;
}

function newWidget(type: WidgetType): Widget {
  return {
    id: Math.random().toString(16).slice(2, 12), type, title: null, size: WIDGETS[type].size,
    settings: { site_id: null, metric: type === "kpi" ? "current" : null, show_p90: true },
  };
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function DashboardPage() {
  const [options, setOptions] = useState<StatsOptions | null>(null);
  const [boards, setBoards] = useState<Dashboard[] | null>(null);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [tick, setTick] = useState(0);
  const [updatedAt, setUpdatedAt] = useState(() => new Date().toISOString());

  useEffect(() => {
    cache.clear();
    Promise.all([api.statsOptions(), api.listDashboards()])
      .then(([o, b]) => {
        setOptions(o);
        setBoards(b);
        let saved: number | null = null;
        try { saved = Number(localStorage.getItem("graintime.dashboard")) || null; } catch { /* private window */ }
        setActiveId(b.find((d) => d.id === saved)?.id ?? b[0]?.id ?? null);
      })
      .catch((e) => setError(e.message));
  }, []);

  // Live numbers refresh every minute (the collector polls every 60 s by default).
  useEffect(() => {
    const t = window.setInterval(() => { setTick((n) => n + 1); setUpdatedAt(new Date().toISOString()); }, REFRESH_MS);
    return () => window.clearInterval(t);
  }, []);

  const board = boards?.find((b) => b.id === activeId) ?? null;

  const choose = (id: number) => {
    setActiveId(id);
    try { localStorage.setItem("graintime.dashboard", String(id)); } catch { /* ignore */ }
  };

  // Saves are queued so quick edits never arrive out of order.
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const update = useCallback((next: Dashboard) => {
    setBoards((bs) => bs?.map((b) => (b.id === next.id ? next : b)) ?? bs);
    setSaving(true);
    queue.current = queue.current
      .then(() => api.saveDashboard(next.id, { name: next.name, filters: next.filters, widgets: next.widgets }))
      .then(() => setError(null))
      .catch((e) => setError(`Could not save the dashboard: ${e.message}`))
      .finally(() => setSaving(false));
  }, []);

  const addBoard = async () => {
    const name = window.prompt("Name for the new dashboard", "My dashboard");
    if (!name?.trim()) return;
    try {
      const d = await api.createDashboard({ name: name.trim() });
      setBoards((bs) => [...(bs ?? []), d]);
      choose(d.id);
      setEditing(true);
    } catch (e: any) { setError(e.message); }
  };

  const removeBoard = async () => {
    if (!board || !window.confirm(`Delete the dashboard "${board.name}"? This cannot be undone.`)) return;
    try {
      await api.deleteDashboard(board.id);
      const rest = (boards ?? []).filter((b) => b.id !== board.id);
      if (!rest.length) {
        const fresh = await api.listDashboards();
        setBoards(fresh);
        choose(fresh[0].id);
      } else {
        setBoards(rest);
        choose(rest[0].id);
      }
      setEditing(false);
    } catch (e: any) { setError(e.message); }
  };

  const resetBoard = async () => {
    if (!board || !window.confirm(`Put "${board.name}" back to the standard widgets and filters?`)) return;
    try {
      const d = await api.resetDashboard(board.id);
      setBoards((bs) => bs?.map((b) => (b.id === d.id ? d : b)) ?? bs);
    } catch (e: any) { setError(e.message); }
  };

  if (error && !boards) return <Notice kind="error">{error}</Notice>;
  if (!options || !boards || !board) return <p className="muted">Loading the dashboard…</p>;

  const range = resolveRange(board.filters, options.today);
  const setFilters = (f: Partial<DashFilters>) => update({ ...board, filters: { ...board.filters, ...f } });
  const setWidgets = (widgets: Widget[]) => update({ ...board, widgets });

  return (
    <div className="dash">
      <div className="dash-tabs" role="tablist" aria-label="Dashboards">
        {boards.map((b) => (
          <button key={b.id} role="tab" aria-selected={b.id === board.id}
            className={b.id === board.id ? "dash-tab active" : "dash-tab"} onClick={() => choose(b.id)}>
            {b.name}
          </button>
        ))}
        {boards.length < 20 ? <button className="dash-tab add" onClick={addBoard}>+ New dashboard</button> : null}
        <span className="spacer" />
        <span className="muted small" aria-live="polite">{saving ? "Saving…" : `Updated ${new Date(updatedAt).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" })}`}</span>
        <button className="secondary small-button" onClick={() => { setTick((n) => n + 1); setUpdatedAt(new Date().toISOString()); }}>Refresh</button>
        <button className={editing ? "small-button" : "secondary small-button"} onClick={() => setEditing(!editing)}>
          {editing ? "Done" : "Customize"}
        </button>
      </div>

      {error ? <Notice kind="error">{error}</Notice> : null}

      {editing ? (
        <EditBar board={board} onRename={(name) => update({ ...board, name })}
          onAdd={(t) => setWidgets([...board.widgets, newWidget(t)])}
          onReset={resetBoard} onDelete={removeBoard} />
      ) : null}

      <FilterRow filters={board.filters} options={options} range={range} onChange={setFilters} />

      {!options.sites.length ? (
        <Notice kind="info">No sites are shown on the dashboard yet. An administrator can turn on <strong>Show on dashboard</strong> for a site under Sites.</Notice>
      ) : null}

      {!board.widgets.length ? (
        <p className="muted">This dashboard has no widgets. Use <strong>Customize</strong> to add some.</p>
      ) : null}

      <div className="dash-grid">
        {board.widgets.map((w, i) => (
          <WidgetCard key={w.id} widget={w} index={i} count={board.widgets.length} editing={editing}
            filters={board.filters} range={range} options={options} tick={tick}
            onChange={(nw) => setWidgets(board.widgets.map((x) => (x.id === w.id ? nw : x)))}
            onMove={(d) => {
              const ws = [...board.widgets];
              const j = i + d;
              [ws[i], ws[j]] = [ws[j], ws[i]];
              setWidgets(ws);
            }}
            onRemove={() => setWidgets(board.widgets.filter((x) => x.id !== w.id))} />
        ))}
      </div>

      <p className="muted small dash-foot">
        Time on site is measured from the inbound weigh to the outbound weigh. Time spent in line before the inbound scale
        isn't captured. Voided tickets, single-weigh tickets (stored tares) and stays over {options.ceiling_hours} h are
        left out of the statistics and counted separately. Times are Eastern.
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Filter row and edit bar
// --------------------------------------------------------------------------- //

function FilterRow(props: { filters: DashFilters; options: StatsOptions; range: { from: string; to: string }; onChange: (f: Partial<DashFilters>) => void }) {
  const { filters: f, options: o } = props;
  const allSites = !f.site_ids.length;
  return (
    <div className="filter-row" role="group" aria-label="Filters">
      <label>
        <span>Dates</span>
        <select value={f.range} onChange={(e) => {
          const r = e.target.value as RangeKey;
          if (r === "custom") props.onChange({ range: r, date_from: props.range.from, date_to: props.range.to });
          else props.onChange({ range: r });
        }}>
          {RANGES.map((r) => <option key={r.key} value={r.key}>{r.label}</option>)}
        </select>
      </label>
      {f.range === "custom" ? (
        <>
          <label><span>From</span>
            <input type="date" value={f.date_from || ""} min={o.first_date || undefined} max={o.today}
              onChange={(e) => e.target.value && props.onChange({ date_from: e.target.value })} />
          </label>
          <label><span>To</span>
            <input type="date" value={f.date_to || ""} min={f.date_from || undefined} max={o.today}
              onChange={(e) => e.target.value && props.onChange({ date_to: e.target.value })} />
          </label>
        </>
      ) : null}
      <SitePicker options={o} selected={f.site_ids} onChange={(ids) => props.onChange({ site_ids: ids })} allSites={allSites} />
      <label>
        <span>Trucks</span>
        <select value={f.direction} onChange={(e) => props.onChange({ direction: e.target.value as DashFilters["direction"] })}>
          <option value="received">Received (inbound grain)</option>
          <option value="shipped">Shipped (outbound grain)</option>
        </select>
      </label>
      <label>
        <span>Commodity</span>
        <select value={f.commodity || ""} onChange={(e) => props.onChange({ commodity: e.target.value || null })}>
          <option value="">All commodities</option>
          {o.commodities.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </label>
      <span className="filter-range muted small">{rangeText(props.range)}</span>
    </div>
  );
}

function SitePicker(props: { options: StatsOptions; selected: number[]; allSites: boolean; onChange: (ids: number[]) => void }) {
  const sites = props.options.sites;
  const label = props.allSites ? `All sites (${sites.length})`
    : props.selected.length === 1 ? sites.find((s) => s.id === props.selected[0])?.name ?? "1 site"
    : `${props.selected.length} sites`;
  const toggle = (id: number) => {
    const cur = props.allSites ? sites.map((s) => s.id) : props.selected;
    const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
    props.onChange(next.length === sites.length || !next.length ? [] : next);
  };
  return (
    <div className="filter-sites">
      <span>Sites</span>
      <details className="picker">
        <summary>{label}</summary>
        <div className="picker-menu">
          <label className="check"><input type="checkbox" checked={props.allSites} onChange={() => props.onChange([])} /> All sites</label>
          {sites.map((s) => (
            <label key={s.id} className="check">
              <input type="checkbox" checked={props.allSites || props.selected.includes(s.id)} onChange={() => toggle(s.id)} /> {s.name}
            </label>
          ))}
        </div>
      </details>
    </div>
  );
}

function EditBar(props: { board: Dashboard; onRename: (n: string) => void; onAdd: (t: WidgetType) => void; onReset: () => void; onDelete: () => void }) {
  const [name, setName] = useState(props.board.name);
  const [type, setType] = useState<WidgetType>("kpi");
  useEffect(() => setName(props.board.name), [props.board.id, props.board.name]);
  return (
    <div className="edit-bar card">
      <label className="field">
        <span className="field-label">Dashboard name</span>
        <input value={name} maxLength={100} onChange={(e) => setName(e.target.value)}
          onBlur={() => name.trim() && name.trim() !== props.board.name && props.onRename(name.trim())} />
      </label>
      <div className="field">
        <label htmlFor="add-widget" className="field-label">Add a widget</label>
        <div className="actions-inline">
          <select id="add-widget" value={type} onChange={(e) => setType(e.target.value as WidgetType)}>
            {(Object.keys(WIDGETS) as WidgetType[]).map((t) => <option key={t} value={t}>{WIDGETS[t].title}</option>)}
          </select>
          <button className="small-button" onClick={() => props.onAdd(type)} disabled={props.board.widgets.length >= 40}>Add</button>
        </div>
        <span className="field-hint">{WIDGETS[type].help}</span>
      </div>
      <div className="edit-bar-actions">
        <button className="secondary small-button" onClick={props.onReset}>Reset to standard</button>
        <button className="secondary small-button danger-text" onClick={props.onDelete}>Delete dashboard</button>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Widget card: title, chart/table toggle, CSV, and edit controls
// --------------------------------------------------------------------------- //

type WProps = {
  widget: Widget; index: number; count: number; editing: boolean; filters: DashFilters;
  range: { from: string; to: string }; options: StatsOptions; tick: number;
  onChange: (w: Widget) => void; onMove: (d: -1 | 1) => void; onRemove: () => void;
};

const CSV_KIND: Record<WidgetType, string> = {
  kpi: "summary", sites_table: "overview", trend: "trend", by_hour: "by-hour", heatmap: "heatmap",
  compare: "compare", distribution: "distribution", exclusions: "summary",
};

function WidgetCard(p: WProps) {
  const w = p.widget;
  const [asTable, setAsTable] = useState(false);
  const sites = w.settings.site_id ? [w.settings.site_id] : p.filters.site_ids;
  const q: StatsQuery = { sites, from: p.range.from, to: p.range.to, direction: p.filters.direction, commodity: p.filters.commodity };
  const live = w.type === "sites_table" || (w.type === "kpi" && KPI[w.settings.metric || "current"].live);
  const siteName = w.settings.site_id ? p.options.sites.find((s) => s.id === w.settings.site_id)?.name ?? "Site not shown" : null;
  const sub = [siteName, live ? "Right now" : rangeText(p.range)].filter(Boolean).join(" · ");
  const csvKind = w.type === "kpi" && live ? "overview" : CSV_KIND[w.type];
  const csvHref = `/api/stats/${csvKind}?${statsQuery(q, "csv")}`;
  const hasTable = w.type !== "kpi" && w.type !== "exclusions" && w.type !== "sites_table";

  let body: ReactNode;
  switch (w.type) {
    case "kpi": body = <KpiTile widget={w} q={q} tick={p.tick} options={p.options} />; break;
    case "sites_table": body = <SitesTable q={q} tick={p.tick} options={p.options} />; break;
    case "trend": body = <TrendChart q={q} tick={p.tick} showP90={w.settings.show_p90} asTable={asTable} />; break;
    case "by_hour": body = <ByHourChart q={q} tick={p.tick} asTable={asTable} />; break;
    case "heatmap": body = <HeatmapChart q={q} tick={p.tick} asTable={asTable} />; break;
    case "compare": body = <CompareChart q={q} tick={p.tick} asTable={asTable} />; break;
    case "distribution": body = <DistributionChart q={q} tick={p.tick} asTable={asTable} />; break;
    case "exclusions": body = <Exclusions q={q} tick={p.tick} />; break;
  }

  return (
    <section className={`widget size-${w.size} widget-${w.type}`} aria-label={widgetTitle(w)}>
      <header className="widget-head">
        <div>
          <h3>{widgetTitle(w)}</h3>
          <div className="muted small">{sub}</div>
        </div>
        {!p.editing && w.type !== "kpi" ? (
          <div className="widget-tools">
            {hasTable ? <button className="link small" onClick={() => setAsTable(!asTable)}>{asTable ? "Chart" : "Table"}</button> : null}
            <a className="small" href={csvHref} download>CSV</a>
          </div>
        ) : null}
      </header>
      {p.editing ? <WidgetSettings {...p} /> : null}
      <div className="widget-body">{body}</div>
      {!live && w.type !== "exclusions" && w.type !== "kpi" ? <ExcludedNote q={q} tick={p.tick} /> : null}
    </section>
  );
}

function WidgetSettings(p: WProps) {
  const w = p.widget;
  const set = (patch: Partial<Widget>) => p.onChange({ ...w, ...patch });
  const setS = (patch: Partial<Widget["settings"]>) => set({ settings: { ...w.settings, ...patch } });
  const siteScoped = w.type === "kpi" || w.type === "trend" || w.type === "by_hour" || w.type === "heatmap" || w.type === "distribution" || w.type === "exclusions";
  return (
    <div className="widget-edit">
      <label><span>Title</span>
        <input value={w.title ?? ""} placeholder={widgetTitle({ ...w, title: null })} maxLength={80}
          onChange={(e) => set({ title: e.target.value || null })} />
      </label>
      {w.type === "kpi" ? (
        <label><span>Number</span>
          <select value={w.settings.metric || "current"} onChange={(e) => setS({ metric: e.target.value as KpiMetric })}>
            {(Object.keys(KPI) as KpiMetric[]).map((m) => <option key={m} value={m}>{KPI[m].title}{KPI[m].live ? " (live)" : ""}</option>)}
          </select>
        </label>
      ) : null}
      {siteScoped ? (
        <label><span>Site</span>
          <select value={w.settings.site_id ?? ""} onChange={(e) => setS({ site_id: e.target.value ? Number(e.target.value) : null })}>
            <option value="">Sites in the filter</option>
            {p.options.sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
      ) : null}
      {w.type === "trend" ? (
        <label className="check"><input type="checkbox" checked={w.settings.show_p90} onChange={(e) => setS({ show_p90: e.target.checked })} /> Show 90th percentile</label>
      ) : null}
      <div className="widget-edit-row">
        <span className="seg" role="group" aria-label="Size">
          {(["S", "M", "L"] as const).map((s) => (
            <button key={s} className={w.size === s ? "seg-on" : ""} aria-pressed={w.size === s} onClick={() => set({ size: s })}>
              {s === "S" ? "Small" : s === "M" ? "Half" : "Full"}
            </button>
          ))}
        </span>
        <button className="link" disabled={p.index === 0} onClick={() => p.onMove(-1)} aria-label="Move earlier">↑ Earlier</button>
        <button className="link" disabled={p.index === p.count - 1} onClick={() => p.onMove(1)} aria-label="Move later">↓ Later</button>
        <button className="link danger-text" onClick={p.onRemove}>Remove</button>
      </div>
    </div>
  );
}

function Loading({ error }: { error: string | null }) {
  return error ? <Notice kind="error">{error}</Notice> : <p className="muted small">Loading…</p>;
}

function ExcludedNote({ q, tick }: { q: StatsQuery; tick: number }) {
  const { data } = useStats<Summary>("summary", q, tick);
  if (!data) return null;
  const e = data.excluded;
  const n = e.voided + e.single_weigh + e.over_ceiling + e.unknown_status;
  return (
    <p className="widget-note muted small">
      {data.completed.toLocaleString()} trucks counted.{" "}
      {n ? `${n.toLocaleString()} tickets left out: ${e.voided} voided, ${e.single_weigh} single weigh, ${e.over_ceiling} over ${data.ceiling_hours} h${e.unknown_status ? `, ${e.unknown_status} unknown status` : ""}.` : "No tickets left out."}
    </p>
  );
}

// --------------------------------------------------------------------------- //
// Status (time on site against the thresholds): colour + icon + label
// --------------------------------------------------------------------------- //

function Status({ level, options }: { level: Level; options: StatsOptions }) {
  if (!level) return null;
  const t = options.thresholds;
  const text = level === "good" ? `Under ${t.green_max_min} min` : level === "warning" ? `${t.green_max_min}–${t.yellow_max_min} min` : `Over ${t.yellow_max_min} min`;
  const icon = level === "good" ? "✓" : level === "warning" ? "!" : "✕";
  return <span className={`status status-${level}`}><span className="status-icon" aria-hidden="true">{icon}</span>{text}</span>;
}

// --------------------------------------------------------------------------- //
// KPI tile
// --------------------------------------------------------------------------- //

function KpiTile({ widget, q, tick, options }: { widget: Widget; q: StatsQuery; tick: number; options: StatsOptions }) {
  const metric = widget.settings.metric || "current";
  const live = KPI[metric].live;
  const ov = useStats<SiteOverview[]>(live ? "overview" : "summary", q, tick);
  if (!ov.data) return <Loading error={ov.error} />;

  if (!live) {
    const s = ov.data as unknown as Summary;
    const v = metric === "median" ? s.median_min : metric === "p90" ? s.p90_min : s.completed;
    return (
      <div className="kpi">
        <div className="kpi-value">{v == null ? "–" : v.toLocaleString()}<span className="kpi-unit">{metric === "completed" ? " trucks" : v == null ? "" : " min"}</span></div>
        <div className="muted small">{v == null ? "No completed trucks in this range" : metric === "completed" ? `${s.trucks.toLocaleString()} arrived` : `${s.completed.toLocaleString()} trucks`}</div>
      </div>
    );
  }

  const rows = ov.data;
  if (!rows.length) return <p className="muted small">No sites selected.</p>;
  if (metric === "on_site_now" || metric === "trucks_today") {
    const total = rows.reduce((a, r) => a + (metric === "on_site_now" ? r.on_site_now : r.trucks_today), 0);
    const oldest = Math.max(-1, ...rows.map((r) => r.oldest_on_site_min ?? -1));
    return (
      <div className="kpi">
        <div className="kpi-value">{total.toLocaleString()}<span className="kpi-unit"> trucks</span></div>
        <div className="muted small">
          {metric === "on_site_now" ? (oldest >= 0 ? `Longest here: ${Math.round(oldest)} min` : "None on site") : `${rows.reduce((a, r) => a + r.completed_today, 0)} weighed out`}
          {rows.length > 1 ? ` · ${rows.length} sites` : ""}
        </div>
      </div>
    );
  }

  // Current time on site is a per-site measure: one site shows one number,
  // several sites show a short list (a median of medians would mislead).
  if (rows.length === 1) {
    const c = rows[0].current;
    return (
      <div className="kpi">
        <div className="kpi-value">{c.minutes == null ? "–" : Math.round(c.minutes)}<span className="kpi-unit">{c.minutes == null ? "" : " min"}</span></div>
        <Status level={c.level} options={options} />
        <div className="muted small">{c.basis}{rows[0].stale ? " · data may be out of date" : ""}</div>
      </div>
    );
  }
  return (
    <ul className="kpi-list">
      {rows.map((r) => (
        <li key={r.id}>
          <span className="kpi-list-name">{r.name}</span>
          <strong>{r.current.minutes == null ? "no recent trucks" : `${Math.round(r.current.minutes)} min`}</strong>
          <Status level={r.current.level} options={options} />
        </li>
      ))}
    </ul>
  );
}

// --------------------------------------------------------------------------- //
// Sites table
// --------------------------------------------------------------------------- //

type SortKey = "name" | "current" | "on_site_now" | "trucks_today" | "median_today_min" | "p90_today_min";

function SitesTable({ q, tick, options: opts }: { q: StatsQuery; tick: number; options: StatsOptions }) {
  const { data, error } = useStats<SiteOverview[]>("overview", { ...q, from: undefined, to: undefined }, tick);
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "current", desc: true });
  const rows = useMemo(() => {
    if (!data) return [];
    const val = (r: SiteOverview): number | string | null =>
      sort.key === "name" ? r.name : sort.key === "current" ? r.current.minutes : (r[sort.key] as number | null);
    return [...data].sort((a, b) => {
      const va = val(a), vb = val(b);
      if (va == null && vb == null) return a.name.localeCompare(b.name);
      if (va == null) return 1;
      if (vb == null) return -1;
      const c = typeof va === "string" ? va.localeCompare(vb as string) : (va as number) - (vb as number);
      return sort.desc ? -c : c;
    });
  }, [data, sort]);
  if (!data) return <Loading error={error} />;
  if (!data.length) return <p className="muted small">No sites selected.</p>;
  const th = (key: SortKey, label: string) => (
    <th aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : "none"}>
      <button className="sort" onClick={() => setSort({ key, desc: sort.key === key ? !sort.desc : key !== "name" })}>
        {label}{sort.key === key ? (sort.desc ? " ▼" : " ▲") : ""}
      </button>
    </th>
  );
  return (
    <div className="table-wrap">
      <table className="num-table sites-table">
        <thead>
          <tr>
            {th("name", "Site")}{th("current", "Current time on site")}{th("on_site_now", "On site now")}
            {th("trucks_today", "Trucks today")}{th("median_today_min", "Median today")}{th("p90_today_min", "90th pct today")}
            <th>Usual for this hour</th><th>Last update</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{r.name}</td>
              <td>
                {r.current.minutes == null ? <span className="muted">no recent trucks</span> : <strong>{Math.round(r.current.minutes)} min</strong>}{" "}
                <Status level={r.current.level} options={opts} />
              </td>
              <td>{r.on_site_now}{r.oldest_on_site_min != null ? <span className="muted"> (longest {Math.round(r.oldest_on_site_min)} min)</span> : null}</td>
              <td>{r.trucks_today}</td>
              <td>{r.median_today_min == null ? "–" : `${r.median_today_min} min`}</td>
              <td>{r.p90_today_min == null ? "–" : `${r.p90_today_min} min`}</td>
              <td>{r.typical_this_hour_min == null ? "–" : `${r.typical_this_hour_min} min`}</td>
              <td>
                {ago(r.last_update)}{" "}
                {r.stale ? <span className="badge badge-warn">⚠ Out of date</span> : null}
                {!r.polling ? <span className="badge badge-muted">Not collecting</span> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Charts
// --------------------------------------------------------------------------- //

function TrendChart({ q, tick, showP90, asTable }: { q: StatsQuery; tick: number; showP90: boolean; asTable: boolean }) {
  const { data, error } = useStats<TrendDay[]>("trend", q, tick);
  if (!data) return <Loading error={error} />;
  if (asTable) {
    return <DataTable columns={[{ key: "date", label: "Date" }, { key: "completed", label: "Trucks" }, { key: "median_min", label: "Median (min)" }, { key: "p90_min", label: "90th pct (min)" }]}
      rows={data.map((d) => ({ ...d, date: longDay(d.date) }))} />;
  }
  if (!data.some((d) => d.median_min != null)) return <p className="muted small">No completed trucks in this range.</p>;
  const series = [{ name: "Median", color: "var(--series-1)", values: data.map((d) => d.median_min) }];
  if (showP90) series.push({ name: "90th percentile", color: "var(--series-2)", values: data.map((d) => d.p90_min) });
  return <Lines labels={data.map((d) => shortDay(d.date))} series={series} unit="min" ariaLabel="Time on site by day" />;
}

function ByHourChart({ q, tick, asTable }: { q: StatsQuery; tick: number; asTable: boolean }) {
  const { data, error } = useStats<ByHour[]>("by-hour", q, tick);
  if (!data) return <Loading error={error} />;
  if (asTable) {
    return <DataTable columns={[{ key: "hour", label: "Hour" }, { key: "trucks", label: "Trucks arriving" }, { key: "completed", label: "Weighed out" }, { key: "median_min", label: "Median (min)" }, { key: "p90_min", label: "90th pct (min)" }]}
      rows={data.map((d) => ({ ...d, hour: hourLabel(d.hour) }))} />;
  }
  const used = data.filter((d) => d.trucks || d.completed);
  if (!used.length) return <p className="muted small">No trucks in this range.</p>;
  const h0 = Math.max(0, Math.min(...used.map((d) => d.hour)) - 1);
  const h1 = Math.min(23, Math.max(...used.map((d) => d.hour)) + 1);
  const rows = data.filter((d) => d.hour >= h0 && d.hour <= h1);
  // Two measures, two charts on the same hour axis (never a dual axis).
  return (
    <div className="small-multiples">
      <div className="sm-title small">Trucks arriving</div>
      <Columns height={130} ariaLabel="Trucks arriving by hour"
        data={rows.map((d) => ({ label: hourLabel(d.hour), value: d.trucks, tip: <><div className="viz-tip-title">{hourLabel(d.hour)}</div><TipRow value={d.trucks} label="trucks arrived" /></> }))} />
      <div className="sm-title small">Median time on site (by hour weighed out)</div>
      <Columns height={130} ariaLabel="Median time on site by hour" color="var(--series-1)"
        data={rows.map((d) => ({ label: hourLabel(d.hour), value: d.median_min, tip: <><div className="viz-tip-title">{hourLabel(d.hour)}</div><TipRow value={d.median_min == null ? "no trucks" : `${d.median_min} min`} label={`median of ${d.completed}`} /></> }))} />
    </div>
  );
}

function HeatmapChart({ q, tick, asTable }: { q: StatsQuery; tick: number; asTable: boolean }) {
  const { data, error } = useStats<HeatCell[]>("heatmap", q, tick);
  if (!data) return <Loading error={error} />;
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  if (asTable) {
    return <DataTable columns={[{ key: "day", label: "Day" }, { key: "hour", label: "Hour" }, { key: "completed", label: "Trucks" }, { key: "median_min", label: "Median (min)" }]}
      rows={data.map((d) => ({ day: days[d.dow], hour: hourLabel(d.hour), completed: d.completed, median_min: d.median_min }))} />;
  }
  if (!data.length) return <p className="muted small">No completed trucks in this range.</p>;
  return <Heatmap cells={data.map((d) => ({ dow: d.dow, hour: d.hour, value: d.median_min, count: d.completed }))} unit="min" ariaLabel="Median time on site by weekday and hour" />;
}

function CompareChart({ q, tick, asTable }: { q: StatsQuery; tick: number; asTable: boolean }) {
  const { data, error } = useStats<CompareRow[]>("compare", q, tick);
  if (!data) return <Loading error={error} />;
  if (asTable) {
    return <DataTable columns={[{ key: "name", label: "Site" }, { key: "completed", label: "Trucks" }, { key: "median_min", label: "Median (min)" }, { key: "p90_min", label: "90th pct (min)" }]} rows={data} />;
  }
  if (!data.length) return <p className="muted small">No sites selected.</p>;
  const sorted = [...data].sort((a, b) => (b.median_min ?? -1) - (a.median_min ?? -1));
  return <HBars unit="min" ariaLabel="Median time on site by site"
    data={sorted.map((r) => ({ label: r.name, value: r.median_min, tip: <><div className="viz-tip-title">{r.name}</div><TipRow value={r.median_min == null ? "no trucks" : `${r.median_min} min`} label="median" /><TipRow value={r.p90_min == null ? "–" : `${r.p90_min} min`} label="90th percentile" /><TipRow value={r.completed} label="trucks" /></> }))} />;
}

function DistributionChart({ q, tick, asTable }: { q: StatsQuery; tick: number; asTable: boolean }) {
  const { data, error } = useStats<DistBin[]>("distribution", q, tick);
  const s = useStats<Summary>("summary", q, tick);
  if (!data) return <Loading error={error} />;
  if (asTable) {
    return <DataTable columns={[{ key: "band", label: "Time on site" }, { key: "count", label: "Trucks" }]}
      rows={data.map((d) => ({ band: `${d.from_min}–${d.to_min} min`, count: d.count }))} />;
  }
  if (!data.some((d) => d.count)) return <p className="muted small">No completed trucks in this range.</p>;
  return (
    <>
      <Columns ariaLabel="Trucks by time on site, 5-minute bands"
        data={data.map((d) => ({ label: String(d.from_min), value: d.count, tip: <><div className="viz-tip-title">{d.from_min}–{d.to_min} min</div><TipRow value={d.count} label="trucks" /></> }))} />
      <div className="muted small axis-note">Minutes on site{s.data?.median_min != null ? ` · median ${s.data.median_min} min, 90th percentile ${s.data.p90_min} min` : ""}</div>
    </>
  );
}

function Exclusions({ q, tick }: { q: StatsQuery; tick: number }) {
  const { data, error } = useStats<Summary>("summary", q, tick);
  if (!data) return <Loading error={error} />;
  const e = data.excluded;
  const rows: [string, number, string][] = [
    ["Counted", data.completed, "completed, both weighs, under the ceiling"],
    ["Voided", e.voided, "cancelled tickets"],
    ["Single weigh", e.single_weigh, "stored tare, no inbound weigh to time from"],
    [`Over ${data.ceiling_hours} h`, e.over_ceiling, "stays above the ceiling (often a forgotten ticket)"],
  ];
  if (e.unknown_status) rows.push(["Unknown status", e.unknown_status, "status not in the mapping profile"]);
  return (
    <table className="num-table excl-table">
      <tbody>
        {rows.map(([k, v, h]) => (
          <tr key={k}><th scope="row">{k}</th><td className="num">{v.toLocaleString()}</td><td className="muted small">{h}</td></tr>
        ))}
      </tbody>
    </table>
  );
}
