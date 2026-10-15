// Agent sites: pairing the GrainTime Agent on the scale PC, and its status.
import { FormEvent, useEffect, useState } from "react";
import { AgentStatus, api, ApiError, Site } from "../api";
import { ago, fmtDateTime } from "../format";
import { Field, Notice } from "./Field";

export function AgentPanel({ site }: { site: Site }) {
  const [st, setSt] = useState<AgentStatus | null>(site.agent);
  const [code, setCode] = useState<{ code: string; expires_at: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const load = () => api.agentStatus(site.id).then(setSt).catch(() => {});
    load();
    const t = window.setInterval(load, 10000);
    return () => window.clearInterval(t);
  }, [site.id]);

  const newCode = async () => {
    if (st?.paired && !window.confirm("A new code lets you pair a different PC. The PC paired now keeps working until the new code is used. Continue?")) return;
    setBusy(true); setError(null);
    try {
      const r = await api.agentPairingCode(site.id);
      setCode(r); setSt(r.agent);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not create a code."); }
    finally { setBusy(false); }
  };
  const revoke = async () => {
    if (!window.confirm("Disconnect the paired PC? It stops sending data until it is paired again.")) return;
    try { setSt(await api.agentRevoke(site.id)); setCode(null); } catch (e) { setError(e instanceof Error ? e.message : "Could not disconnect."); }
  };

  const skew = st?.clock_skew_s ?? 0;
  return (
    <>
      <p className="muted small">
        This site's scale software keeps its tickets in a database only programs on the scale PC can open, so the
        GrainTime Agent runs on that PC, reads read-only while the scale software is open, and sends the tickets here.
      </p>
      {error ? <Notice kind="error">{error}</Notice> : null}

      {st?.paired ? (
        <div className="stat-row">
          <div className={`stat ${st.online ? "" : "stat-bad"}`}>
            <div className="stat-label">Agent</div>
            <div className="stat-value">{st.online ? "Online" : "Offline"}</div>
            <div className="small muted">last heard {ago(st.last_seen_at)}</div>
          </div>
          <div className={`stat ${st.instance === "running" ? "" : "stat-bad"}`}>
            <div className="stat-label">Scale software database</div>
            <div className="stat-value">{st.instance === "running" ? "Open" : st.instance === "not_running" ? "Closed" : "–"}</div>
            <div className="small muted">{st.instance === "not_running" ? "Nothing to read until it's opened" : st.driver ?? ""}</div>
          </div>
          <div className="stat">
            <div className="stat-label">Scale PC</div>
            <div className="stat-value small-value">{st.hostname ?? "–"}</div>
            <div className="small muted">{st.windows_user ?? ""}{st.version ? ` · agent ${st.version}` : ""}</div>
          </div>
        </div>
      ) : null}
      {st?.error ? (
        <Notice kind="error"><strong>{st.error.cause}</strong>{st.error.fix ? <div className="fix">{st.error.fix}</div> : null}</Notice>
      ) : null}
      {st?.paired && Math.abs(skew) > 120 ? (
        <Notice kind="warn">The scale PC's clock is {Math.round(Math.abs(skew) / 60)} minutes {skew > 0 ? "ahead" : "behind"}. Ticket times come from that clock; correct the PC's time (Windows time sync).</Notice>
      ) : null}

      {code ? (
        <div className="pair-box">
          <div className="small muted">Pairing code (works once, until {fmtDateTime(code.expires_at)})</div>
          <div className="pair-code">{code.code}</div>
          <ol className="small">
            <li>On the scale PC, signed in as the account that runs the scale software, open <strong>GrainTimeAgent.exe</strong>.</li>
            <li>Enter the GrainTime address <code>{window.location.origin}</code> and this code, then click <strong>Pair</strong>.</li>
            <li>Turn on <strong>Polling</strong> below. Use <strong>Preview data</strong> first to check the tickets look right.</li>
          </ol>
        </div>
      ) : null}

      <div className="actions-inline">
        <button className={st?.paired ? "secondary" : ""} disabled={busy || site.archived} onClick={newCode}>
          {st?.paired ? "Pair a different PC" : st?.pairing_pending ? "Create a new pairing code" : "Create pairing code"}
        </button>
        {st?.paired ? <button className="link danger-text" onClick={revoke}>Disconnect the PC</button> : null}
        {st?.paired && st.paired_at ? <span className="muted small">Paired {fmtDateTime(st.paired_at)}</span> : null}
      </div>
    </>
  );
}

/** Name, code, address and map link for an agent site (no connection details). */
export function AgentSiteDetails({ site, onSaved }: { site: Site; onSaved: (s: Site) => void }) {
  const [f, setF] = useState({ name: site.name, code: site.code, address: site.address ?? "", map_url: site.map_url ?? "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    setErrors({}); setError(null);
    try {
      onSaved(await api.updateSite(site.id, { name: f.name, code: f.code, address: f.address || null, map_url: f.map_url || null }));
    } catch (e) {
      if (e instanceof ApiError) { setErrors(e.fields); setError(e.message); } else setError("Could not save.");
    }
  };
  return <SiteBasics f={f} setF={setF} errors={errors} error={error} onSubmit={submit} submitLabel="Save details" />;
}

/** Create an agent site. */
export function NewAgentSite({ onCreated }: { onCreated: (s: Site) => void }) {
  const [f, setF] = useState({ name: "", code: "", address: "", map_url: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    setErrors({}); setError(null);
    try {
      onCreated(await api.createAgentSite({ name: f.name, code: f.code, address: f.address || null, map_url: f.map_url || null }));
    } catch (e) {
      if (e instanceof ApiError) { setErrors(e.fields); setError(e.message); } else setError("Could not create the site.");
    }
  };
  return <SiteBasics f={f} setF={setF} errors={errors} error={error} onSubmit={submit} submitLabel="Create site" />;
}

type Basics = { name: string; code: string; address: string; map_url: string };

function SiteBasics(p: { f: Basics; setF: (f: Basics) => void; errors: Record<string, string>; error: string | null;
  onSubmit: (e: FormEvent) => void; submitLabel: string }) {
  const { f, setF, errors } = p;
  return (
    <form className="form" onSubmit={p.onSubmit} noValidate>
      <div className="row">
        <Field label="Name" error={errors.name}><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Short code" hint="Letters and digits, e.g. NEWW" error={errors.code}><input value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })} /></Field>
      </div>
      <div className="row">
        <Field label="Address (public page)" error={errors.address}><input value={f.address} onChange={(e) => setF({ ...f, address: e.target.value })} /></Field>
        <Field label="Map link (public page)" error={errors.map_url}><input value={f.map_url} onChange={(e) => setF({ ...f, map_url: e.target.value })} /></Field>
      </div>
      {p.error ? <Notice kind="error">{p.error}</Notice> : null}
      <div className="actions"><button type="submit">{p.submitLabel}</button></div>
    </form>
  );
}
