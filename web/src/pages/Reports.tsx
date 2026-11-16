// Reports: run a period report (print / save as PDF, Excel or CSV of every
// truck), and, for admins, scheduled email reports.
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  api, ApiError, ReportQuery, ReportRunRow, ReportSchedule, reportUrl, ScheduleList, StatsOptions,
} from "../api";
import { Field, Notice } from "../components/Field";
import { fmtDateTime } from "../format";

type Preset = "yesterday" | "last7" | "lastweek" | "mtd" | "lastmonth" | "season" | "custom";
const PRESETS: { key: Preset; label: string }[] = [
  { key: "yesterday", label: "Yesterday" },
  { key: "last7", label: "Last 7 days" },
  { key: "lastweek", label: "Last week (Mon–Sun)" },
  { key: "mtd", label: "Month to date" },
  { key: "lastmonth", label: "Last month" },
  { key: "season", label: "Harvest season (since Sep 1)" },
  { key: "custom", label: "Custom…" },
];
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function iso(d: Date) { return d.toISOString().slice(0, 10); }
function add(s: string, n: number) { const d = new Date(s + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return iso(d); }

function presetRange(p: Preset, today: string): { from: string; to: string } {
  const y = add(today, -1);
  const d = new Date(today + "T12:00:00Z");
  switch (p) {
    case "yesterday": return { from: y, to: y };
    case "last7": return { from: add(today, -7), to: y };
    case "lastweek": { const mon = add(today, -((d.getUTCDay() + 6) % 7) - 7); return { from: mon, to: add(mon, 6) }; }
    case "mtd": return today.endsWith("-01") ? presetRange("lastmonth", today) : { from: today.slice(0, 8) + "01", to: y };
    case "lastmonth": { const last = add(today.slice(0, 8) + "01", -1); return { from: last.slice(0, 8) + "01", to: last }; }
    case "season": { const yr = Number(y.slice(0, 4)); const s = y >= `${yr}-09-01` ? `${yr}-09-01` : `${yr - 1}-09-01`; return { from: s, to: y }; }
    default: return { from: y, to: y };
  }
}

export function ReportsPage({ isAdmin }: { isAdmin: boolean }) {
  const [tab, setTab] = useState<"run" | "scheduled">("run");
  const [options, setOptions] = useState<StatsOptions | null>(null);
  useEffect(() => { api.statsOptions().then(setOptions).catch(() => {}); }, []);
  return (
    <>
      <div className="page-head">
        <h2>Reports</h2>
        {isAdmin ? (
          <div className="seg" role="tablist">
            <button role="tab" aria-selected={tab === "run"} className={tab === "run" ? "seg-on" : ""} onClick={() => setTab("run")}>Run a report</button>
            <button role="tab" aria-selected={tab === "scheduled"} className={tab === "scheduled" ? "seg-on" : ""} onClick={() => setTab("scheduled")}>Scheduled</button>
          </div>
        ) : null}
      </div>
      {!options ? <p className="muted">Loading…</p> : tab === "run" ? <RunReport options={options} /> : <Schedules options={options} />}
    </>
  );
}

// --------------------------------------------------------------------------- //
// Run a report
// --------------------------------------------------------------------------- //

function SitesPick({ options, value, onChange }: { options: StatsOptions; value: number[]; onChange: (v: number[]) => void }) {
  const all = !value.length;
  const toggle = (id: number) => {
    const cur = all ? options.sites.map((s) => s.id) : value;
    const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
    onChange(next.length === options.sites.length || !next.length ? [] : next);
  };
  return (
    <details className="picker">
      <summary>{all ? `All sites (${options.sites.length})` : value.length === 1 ? options.sites.find((s) => s.id === value[0])?.name : `${value.length} sites`}</summary>
      <div className="picker-menu">
        <label className="check"><input type="checkbox" checked={all} onChange={() => onChange([])} /> All sites</label>
        {options.sites.map((s) => (
          <label key={s.id} className="check"><input type="checkbox" checked={all || value.includes(s.id)} onChange={() => toggle(s.id)} /> {s.name}</label>
        ))}
      </div>
    </details>
  );
}

function RunReport({ options }: { options: StatsOptions }) {
  const loc = useLocation();
  const initial = useMemo(() => {
    const p = new URLSearchParams(loc.search);
    const from = p.get("from"), to = p.get("to");
    return {
      preset: (from && to ? "custom" : "yesterday") as Preset,
      q: {
        ...(from && to ? { from, to } : presetRange("yesterday", options.today)),
        sites: (p.get("sites") || "").split(",").filter(Boolean).map(Number),
        direction: (p.get("direction") === "shipped" ? "shipped" : "received") as ReportQuery["direction"],
        compare: (["previous", "last_year", "none"].includes(p.get("compare") || "") ? p.get("compare") : "previous") as ReportQuery["compare"],
      },
      auto: Boolean(from && to),
    };
  }, [loc.search, options.today]);
  const [preset, setPreset] = useState<Preset>(initial.preset);
  const [q, setQ] = useState<ReportQuery>(initial.q);
  const [html, setHtml] = useState<string | null>(null);
  const [shown, setShown] = useState<ReportQuery | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const frame = useRef<HTMLIFrameElement>(null);

  const run = async (query: ReportQuery) => {
    setBusy(true); setError(null);
    try { setHtml(await api.reportHtml(query)); setShown(query); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Could not build the report."); }
    finally { setBusy(false); }
  };
  useEffect(() => { if (initial.auto) run(initial.q); }, [initial]); // eslint-disable-line react-hooks/exhaustive-deps

  const choose = (p: Preset) => { setPreset(p); if (p !== "custom") setQ({ ...q, ...presetRange(p, options.today) }); };
  const submit = (ev: FormEvent) => { ev.preventDefault(); run(q); };
  const fit = () => {
    const f = frame.current;
    if (f?.contentDocument?.body) f.style.height = `${f.contentDocument.documentElement.scrollHeight + 20}px`;
  };

  return (
    <>
      <form className="filter-row report-form" onSubmit={submit}>
        <label><span>Period</span>
          <select value={preset} onChange={(e) => choose(e.target.value as Preset)}>
            {PRESETS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
          </select>
        </label>
        {preset === "custom" ? (
          <>
            <label><span>From</span><input type="date" value={q.from} max={options.today} onChange={(e) => e.target.value && setQ({ ...q, from: e.target.value })} /></label>
            <label><span>To</span><input type="date" value={q.to} max={options.today} onChange={(e) => e.target.value && setQ({ ...q, to: e.target.value })} /></label>
          </>
        ) : <span className="filter-range muted small">{q.from === q.to ? q.from : `${q.from} to ${q.to}`}</span>}
        <div className="filter-sites"><span>Sites</span><SitesPick options={options} value={q.sites} onChange={(sites) => setQ({ ...q, sites })} /></div>
        <label><span>Trucks</span>
          <select value={q.direction} onChange={(e) => setQ({ ...q, direction: e.target.value as ReportQuery["direction"] })}>
            <option value="received">Received</option><option value="shipped">Shipped</option>
          </select>
        </label>
        <label><span>Compare with</span>
          <select value={q.compare} onChange={(e) => setQ({ ...q, compare: e.target.value as ReportQuery["compare"] })}>
            <option value="previous">Previous period</option><option value="last_year">Same period last year</option><option value="none">Nothing</option>
          </select>
        </label>
        <button type="submit" disabled={busy}>{busy ? "Building…" : "Show report"}</button>
      </form>
      {error ? <Notice kind="error">{error}</Notice> : null}

      {html && shown ? (
        <>
          <div className="actions-inline report-actions">
            <button className="secondary small-button" onClick={() => frame.current?.contentWindow?.print()}>Print / Save as PDF</button>
            <a className="button secondary small-button" href={reportUrl(shown, "trucks", "xlsx")} download>Every truck (Excel)</a>
            <a className="button secondary small-button" href={reportUrl(shown, "trucks", "csv")} download>Every truck (CSV)</a>
            <a className="small" href={reportUrl(shown, "period", "html")} target="_blank" rel="noopener">Open in a new tab ↗</a>
          </div>
          <iframe ref={frame} className="report-frame" title="Report" srcDoc={html} onLoad={fit} />
        </>
      ) : !busy ? <p className="muted">Choose a period and click <strong>Show report</strong>.</p> : null}
    </>
  );
}

// --------------------------------------------------------------------------- //
// Scheduled reports (admins)
// --------------------------------------------------------------------------- //

const BLANK: Omit<ReportSchedule, "id"> = {
  name: "Daily truck times", enabled: true, frequency: "daily", weekday: 0, month_day: 1, send_time: "06:30",
  period: "yesterday", compare: "previous", site_ids: [], direction: "received", include_trucks: false, recipients: [],
};

function whenText(s: ReportSchedule) {
  const t = (() => { const [h, m] = s.send_time.split(":").map(Number); return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; })();
  if (s.frequency === "weekly") return `${DAYS[s.weekday ?? 0]}s at ${t}`;
  if (s.frequency === "monthly") return `Day ${s.month_day ?? 1} of each month at ${t}`;
  return `Every day at ${t}`;
}

function Schedules({ options }: { options: StatsOptions }) {
  const [list, setList] = useState<ScheduleList | null>(null);
  const [editing, setEditing] = useState<ReportSchedule | Omit<ReportSchedule, "id"> | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [runsFor, setRunsFor] = useState<number | null>(null);
  const load = () => api.listSchedules().then(setList).catch((e) => setMsg({ kind: "error", text: e.message }));
  useEffect(() => { load(); }, []);

  const act = async (fn: () => Promise<unknown>, text: string) => {
    setMsg(null);
    try { await fn(); setMsg({ kind: "ok", text }); load(); }
    catch (e) { setMsg({ kind: "error", text: e instanceof Error ? e.message : "Something went wrong." }); }
  };

  if (!list) return msg ? <Notice kind="error">{msg.text}</Notice> : <p className="muted">Loading…</p>;
  return (
    <>
      {!list.email_configured ? (
        <Notice kind="warn">Email isn't set up yet, so scheduled reports can't be sent. Set it up under <Link to="/config/email">Configuration → Email</Link>.</Notice>
      ) : null}
      {msg ? <Notice kind={msg.kind}>{msg.text}</Notice> : null}
      <div className="actions-inline"><button onClick={() => setEditing({ ...BLANK })}>New scheduled report</button></div>
      {editing ? (
        <ScheduleForm options={options} periods={list.periods} value={editing}
          onCancel={() => setEditing(null)}
          onSaved={(text) => { setEditing(null); setMsg({ kind: "ok", text }); load(); }} />
      ) : null}
      {list.schedules.length ? (
        <div className="table-wrap card">
          <table>
            <thead><tr><th>Report</th><th>When (Eastern)</th><th>Covers</th><th>Recipients</th><th>Next</th><th>Last</th><th></th></tr></thead>
            <tbody>
              {list.schedules.map((s) => (
                <tr key={s.id} className={s.enabled ? "" : "archived"}>
                  <td><strong>{s.name}</strong>{s.include_trucks ? <div className="small muted">+ Excel of every truck</div> : null}</td>
                  <td>{whenText(s)}</td>
                  <td>{s.period_label}{s.next_period_label ? <div className="small muted">next: {s.next_period_label}</div> : null}</td>
                  <td title={s.recipients.join(", ")}>{s.recipients.length === 1 ? s.recipients[0] : `${s.recipients.length} people`}</td>
                  <td>{s.enabled ? (s.run_requested ? "sending now…" : fmtDateTime(s.next_run_at)) : <span className="muted">off</span>}</td>
                  <td>{s.last_status === "sent" ? <span className="badge badge-ok">Sent</span> : s.last_status === "failed" ? <span className="badge badge-bad">Failed</span> : <span className="muted">–</span>}
                    {s.last_run_at ? <div className="small muted">{fmtDateTime(s.last_run_at)}</div> : null}</td>
                  <td><div className="row-actions">
                    <button className="link" onClick={() => setEditing(s)}>Edit</button>
                    <button className="link" disabled={!s.enabled || !list.email_configured} onClick={() => act(() => api.sendScheduleNow(s.id), `"${s.name}" will be sent within a minute.`)}>Send now</button>
                    <button className="link" onClick={() => setRunsFor(runsFor === s.id ? null : s.id)}>History</button>
                    <button className="link danger-text" onClick={() => window.confirm(`Delete "${s.name}"?`) && act(() => api.deleteSchedule(s.id), "Deleted.")}>Delete</button>
                  </div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">No scheduled reports yet.</p>}
      {runsFor ? <Runs id={runsFor} /> : null}
    </>
  );
}

function Runs({ id }: { id: number }) {
  const [runs, setRuns] = useState<ReportRunRow[] | null>(null);
  useEffect(() => { api.scheduleRuns(id).then(setRuns).catch(() => setRuns([])); }, [id]);
  if (!runs) return null;
  return (
    <section className="card">
      <h3>History</h3>
      {runs.length ? (
        <table><thead><tr><th>When</th><th>How</th><th>Covered</th><th>Result</th></tr></thead><tbody>
          {runs.map((r) => (
            <tr key={r.id}><td>{fmtDateTime(r.started_at)}</td><td>{r.trigger === "manual" ? "Send now" : "Schedule"}</td>
              <td>{r.period_from === r.period_to ? r.period_from : `${r.period_from} to ${r.period_to}`}</td>
              <td className="wrap-cell">{r.status === "sent" ? <span className="badge badge-ok">Sent</span> : r.status === "failed" ? <><span className="badge badge-bad">Failed</span> {r.error}</> : "sending…"}</td></tr>
          ))}
        </tbody></table>
      ) : <p className="muted small">Not sent yet.</p>}
    </section>
  );
}

function ScheduleForm(props: { options: StatsOptions; periods: Record<string, string>; value: ReportSchedule | Omit<ReportSchedule, "id">;
  onCancel: () => void; onSaved: (text: string) => void }) {
  const [s, setS] = useState(props.value);
  const [rcpt, setRcpt] = useState(props.value.recipients.join(", "));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<ReportSchedule>) => setS({ ...s, ...patch });
  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    setErrors({}); setError(null);
    const body = { ...s, recipients: rcpt.split(/[,;\s]+/).filter(Boolean) } as Omit<ReportSchedule, "id">;
    try {
      if ("id" in s) await api.updateSchedule(s.id, body); else await api.createSchedule(body);
      props.onSaved(`Saved "${s.name}".`);
    } catch (e) {
      if (e instanceof ApiError) { setErrors(e.fields); setError(e.message); } else setError("Could not save.");
    }
  };
  return (
    <form className="form card" onSubmit={submit} noValidate>
      <h3>{"id" in s ? "Edit scheduled report" : "New scheduled report"}</h3>
      <div className="row">
        <Field label="Name (email subject)" error={errors.name}><input value={s.name} onChange={(e) => set({ name: e.target.value })} /></Field>
        <Field label="Recipients" hint="Email addresses, separated by commas" error={errors.recipients}>
          <input value={rcpt} onChange={(e) => setRcpt(e.target.value)} placeholder="gm@mercerlandmark.com, ops@mercerlandmark.com" />
        </Field>
      </div>
      <div className="row">
        <Field label="How often"><select value={s.frequency} onChange={(e) => set({ frequency: e.target.value as ReportSchedule["frequency"] })}>
          <option value="daily">Every day</option><option value="weekly">Every week</option><option value="monthly">Every month</option></select></Field>
        {s.frequency === "weekly" ? (
          <Field label="On"><select value={s.weekday ?? 0} onChange={(e) => set({ weekday: Number(e.target.value) })}>
            {DAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}</select></Field>
        ) : s.frequency === "monthly" ? (
          <Field label="On day"><select value={s.month_day ?? 1} onChange={(e) => set({ month_day: Number(e.target.value) })}>
            {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => <option key={d} value={d}>{d}</option>)}</select></Field>
        ) : null}
        <Field label="At (Eastern)" error={errors.send_time}><input type="time" value={s.send_time} onChange={(e) => set({ send_time: e.target.value })} /></Field>
      </div>
      <div className="row">
        <Field label="Covers"><select value={s.period} onChange={(e) => set({ period: e.target.value })}>
          {Object.entries(props.periods).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
        <Field label="Compare with"><select value={s.compare} onChange={(e) => set({ compare: e.target.value as ReportSchedule["compare"] })}>
          <option value="previous">Previous period</option><option value="last_year">Same period last year</option><option value="none">Nothing</option></select></Field>
        <Field label="Trucks"><select value={s.direction} onChange={(e) => set({ direction: e.target.value as ReportSchedule["direction"] })}>
          <option value="received">Received</option><option value="shipped">Shipped</option></select></Field>
        <div className="field"><span className="field-label">Sites</span><SitesPick options={props.options} value={s.site_ids} onChange={(site_ids) => set({ site_ids })} /></div>
      </div>
      <label className="check"><input type="checkbox" checked={s.include_trucks} onChange={(e) => set({ include_trucks: e.target.checked })} /> Attach an Excel file of every truck</label>
      <label className="check"><input type="checkbox" checked={s.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> On</label>
      {error ? <Notice kind="error">{error}</Notice> : null}
      <div className="actions"><button type="button" className="secondary" onClick={props.onCancel}>Cancel</button><button type="submit">Save</button></div>
    </form>
  );
}
