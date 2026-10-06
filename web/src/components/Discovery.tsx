import { useEffect, useState } from "react";
import { api, Job } from "../api";
import { isDone, useJob } from "../useJob";
import { Field, Notice } from "./Field";
import { JobErrorNotice } from "./JobOutcome";

/**
 * Runs schema discovery against a saved site (through the collector) and
 * shows the report. The report is what the mapping profile is designed from.
 */
export function DiscoveryPanel({ siteId, onFinished }: { siteId: number; onFinished?: (job: Job) => void }) {
  const [jobId, setJobId] = useState<number | null>(null);
  const [extra, setExtra] = useState("");
  const [samples, setSamples] = useState(true);
  const [mask, setMask] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const job = useJob(jobId);

  // Show the most recent discovery for this site, if any.
  useEffect(() => {
    api.siteJobs(siteId, "discovery").then((jobs) => {
      if (jobs.length) setJobId(jobs[0].id);
    }).catch(() => undefined);
  }, [siteId]);

  useEffect(() => {
    if (job && isDone(job) && onFinished) onFinished(job);
  }, [job?.status]);

  const start = async () => {
    setError(null);
    try {
      const j = await api.startDiscovery(siteId, {
        extra_tables: extra.split(/[\s,]+/).filter(Boolean),
        include_samples: samples,
        mask,
      });
      setJobId(j.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start discovery");
    }
  };

  const running = !!job && !isDone(job);

  return (
    <div className="discovery">
      <p>
        Discovery reads the scale database's structure (tables, columns, indexes), metadata row counts,
        and a few recent sample rows. It changes nothing on the site server: SELECT only, every query is
        capped, and it reads uncommitted so it never blocks a weigh. It takes a few seconds.
      </p>
      <details>
        <summary>Options</summary>
        <div className="row">
          <Field label="Extra tables to inspect (optional)" hint="Comma-separated, e.g. dbo.Tickets">
            <input value={extra} onChange={(e) => setExtra(e.target.value)} />
          </Field>
        </div>
        <label className="check">
          <input type="checkbox" checked={samples} onChange={(e) => setSamples(e.target.checked)} />
          Include a few recent sample rows per ticket-like table
        </label>
        <label className="check">
          <input type="checkbox" checked={mask} onChange={(e) => setMask(e.target.checked)} />
          Mask names, plates, addresses and notes in samples
        </label>
      </details>
      <div className="actions-inline">
        <button type="button" onClick={start} disabled={running}>
          {job && isDone(job) ? "Run discovery again" : "Run discovery"}
        </button>
        {running ? (
          <button type="button" className="secondary" onClick={() => jobId && api.cancelJob(jobId)}>Cancel</button>
        ) : null}
      </div>
      {error ? <Notice kind="error">{error}</Notice> : null}
      {running ? (
        <Notice kind="info">
          {job.status === "queued" ? "Waiting for the collector…" : `Running: ${job.progress?.step ?? "connecting"}`}
          {job.progress && job.progress.total > 0 ? (
            <progress value={job.progress.done} max={job.progress.total} />
          ) : null}
        </Notice>
      ) : null}
      {job?.status === "failed" && job.error ? <JobErrorNotice error={job.error} /> : null}
      {job?.status === "cancelled" ? <Notice kind="warn">Discovery was cancelled.</Notice> : null}
      {job?.status === "succeeded" ? <DiscoveryResult job={job} /> : null}
    </div>
  );
}

function DiscoveryResult({ job }: { job: Job }) {
  const [report, setReport] = useState<Record<string, any> | null>(null);
  useEffect(() => {
    api.reportJson(job.id).then(setReport).catch(() => setReport(null));
  }, [job.id]);
  const s = job.result?.summary;
  return (
    <div>
      <Notice kind={s?.tls?.affected ? "warn" : "ok"}>
        <strong>Discovery finished</strong> {job.finished_at ? `at ${new Date(job.finished_at).toLocaleString()}` : ""}:
        {" "}{s?.tables} tables and views, {s?.candidates} look like tickets or weighs.
        {" "}{s?.server?.edition} {s?.server?.product_version}.
        {s?.tls?.affected ? <><br /><strong>TLS warning:</strong> {s.tls.note}</> : null}
        {s?.step_errors ? <><br />{s.step_errors} optional step(s) were skipped (listed at the end of the report).</> : null}
      </Notice>
      <div className="actions-inline">
        <a className="button secondary" href={`/api/admin/jobs/${job.id}/report.md?download=true`}>Download report (.md)</a>
        <a className="button secondary" href={`/api/admin/jobs/${job.id}/report.json`}>Download data (.json)</a>
        <span className="muted small">Send both files to the project team to design the mapping profile.</span>
      </div>
      {report ? <ReportView report={report} /> : null}
    </div>
  );
}

function Table({ rows, cols }: { rows: Record<string, any>[]; cols?: string[] }) {
  if (!rows?.length) return <p className="muted small">(none)</p>;
  const keys = cols ?? Object.keys(rows[0]);
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{keys.map((k) => <th key={k}>{k}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>{keys.map((k) => <td key={k}>{fmt(r[k])}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const fmt = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));

/** Renders the discovery report from its JSON (as text; never as HTML). */
const SERVER_ORDER = ["edition", "product_version", "product_level", "product_update_level", "version_string",
  "server_name", "instance_name", "database_name", "compatibility_level", "server_collation", "database_collation",
  "server_local_time", "server_utc_time", "server_utc_offset", "engine_edition"];

export function ReportView({ report }: { report: Record<string, any> }) {
  const raw = report.server ?? {};
  const server = Object.fromEntries([...SERVER_ORDER.filter((k) => k in raw), ...Object.keys(raw).filter((k) => !SERVER_ORDER.includes(k))].map((k) => [k, raw[k]]));
  return (
    <div className="report">
      <h3>Server</h3>
      <Table rows={Object.entries(server).map(([property, value]) => ({ property, value }))} />
      <h3>Ticket-like tables</h3>
      {(report.candidates ?? []).map((c: any) => (
        <details key={c.table} className="report-table">
          <summary>
            <strong>{c.table}</strong> <span className="muted">~{fmt(c.approx_rows)} rows · {c.columns.length} columns</span>
          </summary>
          <h4>Columns</h4>
          <Table rows={c.columns.map((col: any) => ({ ...col, max_length: col.max_length === -1 ? "max" : col.max_length }))} cols={["column", "data_type", "max_length", "nullable", "is_identity", "is_computed"]} />
          <h4>Indexes</h4>
          <Table rows={c.indexes} cols={["index_name", "type_desc", "is_unique", "key_ordinal", "is_included_column", "column"]} />
          {Object.keys(c.date_ranges ?? {}).length ? (
            <>
              <h4>Oldest / newest</h4>
              <Table cols={["column", "oldest", "newest"]} rows={Object.entries(c.date_ranges).map(([column, v]: [string, any]) => ({ column, ...v }))} />
            </>
          ) : null}
          {c.sample ? (
            <>
              <h4>Recent sample rows</h4>
              {c.sample.omitted_columns?.length ? (
                <p className="muted small">Not read (large text or binary): {c.sample.omitted_columns.join(", ")}</p>
              ) : null}
              <Table
                cols={c.sample.columns}
                rows={c.sample.rows.map((r: unknown[]) => Object.fromEntries(c.sample.columns.map((k: string, i: number) => [k, r[i]])))}
              />
            </>
          ) : null}
          {c.distributions && Object.keys(c.distributions.columns).length ? (
            <>
              <h4>Values seen in recent rows</h4>
              <ul className="small">
                {Object.entries(c.distributions.columns).map(([col, vals]: [string, any]) => (
                  <li key={col}><code>{col}</code>: {vals.map((v: any) => `${fmt(v.value)} (${v.n})`).join(", ")}</li>
                ))}
              </ul>
            </>
          ) : null}
        </details>
      ))}
      <details>
        <summary>All {report.tables?.length ?? 0} tables and views</summary>
        <Table rows={report.tables ?? []} />
      </details>
      {report.errors?.length ? (
        <details>
          <summary>{report.errors.length} skipped step(s)</summary>
          <Table rows={report.errors} />
        </details>
      ) : null}
    </div>
  );
}
