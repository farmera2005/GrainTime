import { Job } from "../api";
import { Notice } from "./Field";

/** Result of a Test connection job: version on success, specific cause + fix on failure. */
export function TestConnectionOutcome({ job }: { job: Job | null }) {
  if (!job) return null;
  if (job.status === "queued" || job.status === "running") {
    return <Notice kind="info">Testing the connection from the collector… (up to about 20 seconds)</Notice>;
  }
  if (job.status === "succeeded" && job.result) {
    const r = job.result;
    return (
      <Notice kind={r.tls?.affected ? "warn" : "ok"}>
        <strong>Connected.</strong> {r.edition} — version {r.product_version}
        {r.product_level ? ` (${r.product_level}${r.product_update_level ? ` ${r.product_update_level}` : ""})` : ""},
        database <code>{r.database}</code>, in {r.elapsed_ms} ms.
        <br />
        Server clock {r.server_local_time?.replace("T", " ").slice(0, 19)} (UTC offset {r.server_utc_offset}).
        {r.tls?.affected ? <><br /><strong>TLS warning:</strong> {r.tls.note}</> : null}
      </Notice>
    );
  }
  if (job.status === "failed" && job.error) return <JobErrorNotice error={job.error} />;
  if (job.status === "cancelled") return <Notice kind="warn">Cancelled.</Notice>;
  return null;
}

export function JobErrorNotice({ error }: { error: NonNullable<Job["error"]> }) {
  return (
    <Notice kind="error">
      <strong>{error.cause}</strong>
      <div className="fix">How to fix: {error.fix}</div>
      {error.docs ? <div className="muted small">Deployment notes: {error.docs}</div> : null}
      {error.detail ? (
        <details className="small">
          <summary>Driver message</summary>
          <code className="wrap">{error.detail}</code>
        </details>
      ) : null}
    </Notice>
  );
}
