import { Job } from "../api";
import { Notice } from "./Field";

/** Result of a Test connection job: version on success, specific cause + fix on failure. */
export function TestConnectionOutcome({ job, onUsePort }: { job: Job | null; onUsePort?: (port: number) => void }) {
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
        {r.resolved_port ? (
          <>
            <br />
            <strong>
              {r.port_entered
                ? `Port ${r.port_entered} no longer answers; the instance is now on port ${r.resolved_port}.`
                : `Found the instance on TCP port ${r.resolved_port}.`}
            </strong>{" "}
            The port has been filled in; save to keep it. If this instance uses a dynamic port it can change when
            SQL Server restarts. Setting a static port at the site avoids that.
          </>
        ) : null}
      </Notice>
    );
  }
  if (job.status === "failed" && job.error) return <JobErrorNotice error={job.error} onUsePort={onUsePort} />;
  if (job.status === "cancelled") return <Notice kind="warn">Cancelled.</Notice>;
  return null;
}

export function JobErrorNotice({ error, onUsePort }: { error: NonNullable<Job["error"]>; onUsePort?: (port: number) => void }) {
  return (
    <Notice kind="error">
      <strong>{error.cause}</strong>
      <div className="fix">How to fix: {error.fix}</div>
      {error.instances?.length ? (
        <div className="instances">
          <div className="small">SQL Server instances reported by this server:</div>
          <ul>
            {error.instances.map((i) => (
              <li key={i.instance}>
                <code>{i.instance}</code>{" "}
                {i.tcp_port ? <>on TCP port <strong>{i.tcp_port}</strong></> : "(TCP/IP turned off)"}
                {i.version ? <span className="muted small"> · version {i.version}</span> : null}{" "}
                {i.tcp_port && onUsePort ? (
                  <button type="button" className="link" onClick={() => onUsePort(i.tcp_port!)}>
                    Use port {i.tcp_port}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
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
