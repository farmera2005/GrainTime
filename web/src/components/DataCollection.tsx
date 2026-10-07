import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, CollectionStatus, Job, MappingProfile, NormalizedTicket, Site } from "../api";
import { ago, fmtDateTime, fmtDuration, localDateISO } from "../format";
import { isDone, useJob } from "../useJob";
import { Field, Notice } from "./Field";
import { JobErrorNotice } from "./JobOutcome";

/** Mapping profile, polling, Preview data, backfill, and live collection status for one site. */
export function DataCollection({ site, onChange }: { site: Site; onChange: (s: Site) => void }) {
  const [profiles, setProfiles] = useState<MappingProfile[]>([]);
  const [status, setStatus] = useState<CollectionStatus | null>(null);
  const [note, setNote] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [interval, setIntervalText] = useState(site.poll_interval_s ? String(site.poll_interval_s) : "");

  const refresh = useCallback(() => {
    api.collection(site.id).then(setStatus).catch(() => undefined);
  }, [site.id]);

  useEffect(() => {
    api.listProfiles().then(setProfiles).catch(() => undefined);
  }, []);
  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 10000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const update = async (patch: Partial<Site>, text: string) => {
    setBusy(true);
    setNote(null);
    try {
      onChange(await api.updateSite(site.id, patch));
      setNote({ kind: "ok", text });
      refresh();
    } catch (e) {
      setNote({ kind: "error", text: e instanceof Error ? e.message : "Could not save." });
    } finally {
      setBusy(false);
    }
  };


  return (
    <div className="collection">
      <div className="row">
        <Field label="Mapping profile" hint={<Link to="/admin/profiles">Manage mapping profiles</Link>}>
          <select
            value={site.mapping_profile_id ?? ""}
            disabled={busy}
            onChange={(e) => update({ mapping_profile_id: e.target.value ? Number(e.target.value) : null },
              e.target.value ? "Mapping profile saved." : "Mapping profile removed; polling is off.")}
          >
            <option value="">(none)</option>
            {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>
        <Field label="Poll every" hint={`Blank = the default (${status?.interval_s ?? 60} s)`}>
          <span className="with-unit">
            <input inputMode="numeric" value={interval} onChange={(e) => setIntervalText(e.target.value.replace(/\D/g, ""))}
              onBlur={() => {
                const v = interval ? Number(interval) : null;
                if (v !== site.poll_interval_s) update({ poll_interval_s: v }, "Poll interval saved.");
              }} />
            <span className="unit">seconds</span>
          </span>
        </Field>
      </div>
      <label className="check">
        <input
          type="checkbox"
          checked={site.polling_enabled}
          disabled={busy || !site.mapping_profile_id || site.archived}
          onChange={(e) => update({ polling_enabled: e.target.checked }, e.target.checked ? "Polling is on. The first poll starts within a few seconds." : "Polling is off. History is kept.")}
        />
        <span>
          <strong>Polling</strong>: collect new tickets from this site every poll interval.
          {!site.mapping_profile_id ? <span className="muted small"> Choose a mapping profile first.</span> : null}
        </span>
      </label>
      {note ? <Notice kind={note.kind}>{note.text}</Notice> : null}

      {status ? <CollectionState status={status} /> : null}

      {site.mapping_profile_id || profiles.length ? (
        <>
          <h4>Preview data</h4>
          <PreviewPanel siteId={site.id} profiles={profiles} defaultProfileId={site.mapping_profile_id} />
        </>
      ) : null}

      {site.mapping_profile_id ? (
        <>
          <h4>Backfill history</h4>
          <BackfillPanel siteId={site.id} last={status?.last_backfill ?? null} onDone={refresh} />
        </>
      ) : null}

      {status?.recent.length ? (
        <>
          <h4>Latest tickets stored</h4>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Ticket</th><th>Status</th><th>Commodity</th><th>Inbound</th><th>Outbound</th><th>Time on site</th></tr></thead>
              <tbody>
                {status.recent.map((t) => (
                  <tr key={t.source_ticket_id}>
                    <td>{t.ticket_number ?? <span className="muted">#{t.source_ticket_id}</span>}</td>
                    <td>{t.status}{t.single_weigh ? " (single weigh)" : ""}</td>
                    <td>{t.commodity}</td>
                    <td>{fmtDateTime(t.inbound_at)}</td>
                    <td>{fmtDateTime(t.outbound_at)}</td>
                    <td>{fmtDuration(t.duration_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </div>
  );
}

function CollectionState({ status }: { status: CollectionStatus }) {
  const st = status.state;
  return (
    <div className="stat-row">
      <div className={`stat ${status.stale ? "stat-bad" : ""}`}>
        <div className="stat-label">Last successful poll</div>
        <div className="stat-value">{status.polling_enabled ? ago(st?.last_success_at) : "polling off"}</div>
        {status.stale ? <div className="small">Stale: no successful poll recently</div> : null}
      </div>
      <div className="stat"><div className="stat-label">Trucks today</div><div className="stat-value">{status.tickets.today}</div></div>
      <div className="stat"><div className="stat-label">On site now</div><div className="stat-value">{status.tickets.on_site_now}</div></div>
      <div className="stat"><div className="stat-label">Tickets stored</div><div className="stat-value">{status.tickets.total}</div></div>
      {st?.last_error ? (
        <div className="stat-wide">
          <JobErrorNotice error={st.last_error} />
          <div className="muted small">
            {st.consecutive_failures} failed attempt(s); next try {fmtDateTime(st.next_poll_at)}. Other sites are not affected.
          </div>
        </div>
      ) : null}
    </div>
  );
}

function PreviewPanel(props: { siteId: number; profiles: MappingProfile[]; defaultProfileId: number | null }) {
  const [profileId, setProfileId] = useState<number | null>(props.defaultProfileId);
  const [jobId, setJobId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const job = useJob(jobId);
  useEffect(() => { setProfileId(props.defaultProfileId ?? props.profiles[0]?.id ?? null); }, [props.defaultProfileId, props.profiles]);

  const run = async () => {
    setError(null);
    try {
      const j = await api.startPreview(props.siteId, { profile_id: profileId ?? undefined, limit: 40 });
      setJobId(j.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the preview");
    }
  };
  return (
    <div>
      <p className="muted small">
        Reads the 40 newest tickets with a profile and shows how GrainTime understands them. Nothing is saved. Use it to
        check a profile before turning polling on.
      </p>
      <div className="actions-inline">
        <select aria-label="Profile to preview" value={profileId ?? ""} onChange={(e) => setProfileId(Number(e.target.value))}>
          {props.profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <button type="button" className="secondary" onClick={run} disabled={!!job && !isDone(job)}>Preview data</button>
      </div>
      {error ? <Notice kind="error">{error}</Notice> : null}
      {job && !isDone(job) ? <Notice kind="info">Reading tickets from the site…</Notice> : null}
      {job?.status === "failed" && job.error ? <JobErrorNotice error={job.error} /> : null}
      {job?.status === "succeeded" && job.result ? <PreviewResult result={job.result} /> : null}
    </div>
  );
}

function PreviewResult({ result }: { result: Record<string, any> }) {
  const s = result.summary;
  const tickets: NormalizedTicket[] = result.tickets;
  const examples: Record<string, NormalizedTicket[]> = result.status_examples ?? {};
  return (
    <div>
      <Notice kind={s.unknown_status?.length ? "warn" : "ok"}>
        Read {s.read} tickets: <strong>{s.included} tracked</strong> ({s.completed} completed, {s.open} on site,{" "}
        {s.voided} voided, {s.single_weigh} single-weigh), {s.not_tracked} of other types not tracked
        {s.merged_split_tickets ? `, ${s.merged_split_tickets} split ticket(s) merged into their truck` : ""}.
        {s.unknown_status?.length ? <><br />Status values not in the profile: {s.unknown_status.join(", ")}. Add them to the profile.</> : null}
      </Notice>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Ticket</th><th>Type</th><th>Status</th><th>Commodity</th><th>Inbound weigh</th><th>Outbound weigh</th><th>Time on site</th><th>Notes</th></tr>
          </thead>
          <tbody>
            {tickets.map((t) => (
              <tr key={t.source_ticket_id} className={t.included ? "" : "archived"}>
                <td>{t.ticket_number ?? <span className="muted">#{t.source_ticket_id}</span>}</td>
                <td>{t.transaction_type}</td>
                <td>{t.status} <span className="muted small">({t.raw_status})</span></td>
                <td>{t.commodity}</td>
                <td>{fmtDateTime(t.inbound_at)}</td>
                <td>{fmtDateTime(t.outbound_at)}</td>
                <td>{fmtDuration(t.duration_s)}</td>
                <td className="small">{t.note ?? (t.single_weigh ? "single weigh (stored tare): excluded" : t.status === "voided" ? "excluded" : "")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {Object.keys(examples).length ? (
        <details open>
          <summary>Check what these status values mean</summary>
          <p className="small">
            These status values are treated as <strong>voided</strong> (excluded from time on site). Open a few of these tickets in
            CompuWeigh to confirm. If a value means something else, such as a truck still on site, change it in the mapping profile.
          </p>
          {Object.entries(examples).map(([raw, list]) => (
            <p key={raw} className="small">
              <code>{raw}</code>:{" "}
              {list.length ? list.map((t) => `${t.ticket_number ?? "#" + t.source_ticket_id} (${t.transaction_type ?? "?"}, ${fmtDateTime(t.inbound_at) || "no weigh"})`).join("; ") : "no tickets with this status"}
            </p>
          ))}
        </details>
      ) : null}
    </div>
  );
}

function BackfillPanel(props: { siteId: number; last: Job | null; onDone: () => void }) {
  const today = localDateISO(new Date());
  const [from, setFrom] = useState(() => localDateISO(new Date(Date.now() - 730 * 86400000)));
  const [to, setTo] = useState(today);
  const [jobId, setJobId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const touched = useRef(false);
  useEffect(() => {
    // Default start date from the global setting, unless the admin already picked one.
    api.getDefaults().then((d) => {
      if (!touched.current) setFrom(localDateISO(new Date(Date.now() - d.backfill_default_days * 86400000)));
    }).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (props.last && !isDone(props.last) && jobId == null) setJobId(props.last.id);
  }, [props.last, jobId]);
  const job = useJob(jobId) ?? props.last;
  useEffect(() => { if (job && isDone(job)) { props.onDone(); setCancelling(false); } }, [job?.status]);

  const running = !!job && !isDone(job);
  const start = async () => {
    setError(null);
    try {
      const j = await api.startBackfill(props.siteId, { date_from: from, date_to: to });
      setJobId(j.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the backfill");
    }
  };
  const p = job?.progress;
  return (
    <div>
      <p className="muted small">
        Loads past tickets one day at a time, pausing between days so the scale system is never slowed. Polling for this site
        waits while it runs. Running it again over the same dates is safe.
      </p>
      <div className="row">
        <Field label="From"><input type="date" value={from} max={to} onChange={(e) => { touched.current = true; setFrom(e.target.value); }} disabled={running} /></Field>
        <Field label="To"><input type="date" value={to} max={today} onChange={(e) => { touched.current = true; setTo(e.target.value); }} disabled={running} /></Field>
      </div>
      <div className="actions-inline">
        <button type="button" onClick={start} disabled={running}>Run backfill</button>
        {running ? (
          <button type="button" className="secondary" disabled={cancelling} onClick={async () => {
            setCancelling(true);
            if (job) await api.cancelJob(job.id);
          }}>{cancelling ? "Cancelling…" : "Cancel"}</button>
        ) : null}
      </div>
      {error ? <Notice kind="error">{error}</Notice> : null}
      {running ? (
        <Notice kind="info">
          {job!.status === "queued" ? "Waiting for the collector…" : `Loading ${p?.step ?? "…"}: day ${p?.done ?? 0} of ${p?.total ?? "?"}, ${p?.tickets_stored ?? 0} tickets stored`}
          {p?.total ? <progress value={p.done} max={p.total} /> : null}
        </Notice>
      ) : null}
      {job?.status === "succeeded" && job.result ? (
        <Notice kind="ok">Backfill finished: {job.result.days_done} days, {job.result.tickets_stored} tickets stored{job.result.merged_split_tickets ? `, ${job.result.merged_split_tickets} split tickets merged` : ""}.</Notice>
      ) : null}
      {job?.status === "cancelled" ? <Notice kind="warn">Backfill cancelled. Days already loaded are kept.</Notice> : null}
      {job?.status === "failed" && job.error ? <JobErrorNotice error={job.error} /> : null}
    </div>
  );
}
