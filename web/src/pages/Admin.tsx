import { useEffect, useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, Site, User } from "../api";
import { DefaultsForm } from "../components/DefaultsForm";
import { DataCollection } from "../components/DataCollection";
import { DiscoveryPanel } from "../components/Discovery";
import { ago } from "../format";
import { ProfileEdit, ProfileList } from "./Profiles";
import { Notice } from "../components/Field";
import { SiteForm } from "../components/SiteForm";

/** Admin panel. Phase 1 grows this (mapping profiles, preview, backfill). */
export function AdminApp({ user, onLogout }: { user: User; onLogout: () => Promise<void> }) {
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/admin/sites" className="brand">GrainTime</Link>
        <nav>
          <Link to="/admin/sites">Sites</Link>
          <Link to="/admin/profiles">Mapping profiles</Link>
          <Link to="/admin/settings">Defaults</Link>
        </nav>
        <span className="spacer" />
        <span className="muted">{user.display_name}</span>
        <button className="link" onClick={onLogout}>Sign out</button>
      </header>
      <main className="content">
        <Routes>
          <Route path="/admin/sites" element={<SiteList />} />
          <Route path="/admin/sites/new" element={<NewSite />} />
          <Route path="/admin/sites/:id" element={<SiteDetail />} />
          <Route path="/admin/settings" element={<SettingsPage />} />
          <Route path="/admin/profiles" element={<ProfileList />} />
          <Route path="/admin/profiles/:id" element={<ProfileEdit />} />
          <Route path="*" element={<Navigate to="/admin/sites" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function SiteList() {
  const [sites, setSites] = useState<Site[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  useEffect(() => {
    api.listSites().then(setSites).catch((e) => setError(e.message));
  }, []);
  const archivedCount = sites?.filter((s) => s.archived).length ?? 0;
  const shown = sites?.filter((s) => showArchived || !s.archived) ?? [];
  return (
    <>
      <div className="page-head">
        <h2>Sites</h2>
        <Link className="button" to="/admin/sites/new">Add site</Link>
      </div>
      {error ? <Notice kind="error">{error}</Notice> : null}
      {sites && !sites.length ? (
        <p className="muted">No sites yet. Use <strong>Add site</strong> to connect the first scale database.</p>
      ) : null}
      {archivedCount ? (
        <label className="check">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
          Show {archivedCount} archived site{archivedCount === 1 ? "" : "s"}
        </label>
      ) : null}
      {shown.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th><th>Code</th><th>Status</th><th>Last poll</th><th>Trucks today</th>
                <th>Server</th><th>Dashboard</th><th>Public page</th><th></th>
              </tr>
            </thead>
            <tbody>
              {shown.map((s) => (
                <tr key={s.id} className={s.archived ? "archived" : ""}>
                  <td><Link to={`/admin/sites/${s.id}`}>{s.name}</Link></td>
                  <td>{s.code}</td>
                  <td><StatusBadge site={s} /></td>
                  <td title={s.last_error?.cause ?? ""}>
                    {s.polling_enabled ? ago(s.last_success_at) : ""}
                    {s.last_error ? <span className="field-error"> · {s.last_error.cause}</span> : null}
                  </td>
                  <td>{s.polling_enabled || s.trucks_today ? s.trucks_today : ""}</td>
                  <td>{s.host}:{s.port}</td>
                  <td>{s.show_on_dashboard ? "Shown" : "Hidden"}</td>
                  <td>{s.show_on_public ? "Shown" : "Hidden"}</td>
                  <td><Link className="button secondary small-button" to={`/admin/sites/${s.id}`}>Edit</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}

function StatusBadge({ site }: { site: Site }) {
  if (site.archived) return <span className="badge badge-muted">Archived</span>;
  if (site.polling_enabled && site.stale) return <span className="badge badge-bad">Stale</span>;
  if (site.polling_enabled) return <span className="badge badge-ok">Polling</span>;
  return <span className="badge badge-warn">Not polling</span>;
}

function NewSite() {
  const nav = useNavigate();
  return (
    <>
      <h2>Add site</h2>
      <SiteForm onSaved={(s) => nav(`/admin/sites/${s.id}`)} />
    </>
  );
}

function SiteDetail() {
  const id = Number(useParams().id);
  const nav = useNavigate();
  const [site, setSite] = useState<Site | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.getSite(id).then(setSite).catch((e) => setError(e.message));
  }, [id]);
  if (error) return <Notice kind="error">{error}</Notice>;
  if (!site) return <p className="muted">Loading…</p>;

  const updated = (s: Site, text: string) => {
    setSite(s);
    setMessage({ kind: "ok", text });
    window.scrollTo({ top: 0 });
  };
  const failed = (e: unknown) => {
    setMessage({ kind: "error", text: e instanceof Error ? e.message : "Something went wrong." });
    window.scrollTo({ top: 0 });
  };

  return (
    <>
      <div className="page-head">
        <h2>
          {site.name} <span className="muted">({site.code})</span> <StatusBadge site={site} />
        </h2>
        <Link to="/admin/sites">All sites</Link>
      </div>
      {message ? <Notice kind={message.kind}>{message.text}</Notice> : null}
      {site.archived ? (
        <Notice kind="warn">
          This site is archived: it is hidden everywhere and not polled. Its history is kept. Restore it below to use it
          again.
        </Notice>
      ) : null}

      <section className="card">
        <h3>Details and connection</h3>
        <p className="muted small">
          Changes take effect on the collector's next cycle. Leave the password blank to keep the saved one.
        </p>
        <SiteForm key={`${site.id}-form`} site={site} onSaved={(s) => updated(s, "Site details saved.")} />
      </section>

      <section className="card">
        <h3>Data collection</h3>
        <DataCollection site={site} onChange={setSite} />
      </section>

      <section className="card">
        <h3>Where this site appears</h3>
        <SiteSwitches site={site} onChange={setSite} />
      </section>

      <section className="card">
        <h3>Discovery</h3>
        <DiscoveryPanel siteId={site.id} />
      </section>

      <section className="card danger-zone">
        <h3>Archive or delete</h3>
        <ArchiveControls site={site} onChange={(s, text) => updated(s, text)} onError={failed} />
        <DeleteSite site={site} onDeleted={() => nav("/admin/sites")} onError={failed} />
      </section>
    </>
  );
}

function SiteSwitches(props: { site: Site; onChange: (s: Site) => void }) {
  const { site } = props;
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const toggle = async (field: "show_on_dashboard" | "show_on_public", value: boolean) => {
    setBusy(true);
    setNote(null);
    try {
      props.onChange(await api.updateSite(site.id, { [field]: value }));
      setNote({ kind: "ok", text: "Saved." });
    } catch (e) {
      setNote({ kind: "error", text: e instanceof Error ? e.message : "Could not save." });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="switches">
      <label className="check">
        <input
          type="checkbox"
          checked={site.show_on_dashboard}
          disabled={busy || site.archived}
          onChange={(e) => toggle("show_on_dashboard", e.target.checked)}
        />
        <span><strong>Management dashboard</strong>: show this site to signed-in staff.</span>
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={site.show_on_public}
          disabled={busy || site.archived}
          onChange={(e) => toggle("show_on_public", e.target.checked)}
        />
        <span><strong>Public page</strong>: show this site's wait times to farmers. New sites start hidden.</span>
      </label>
      <p className={`small ${note?.kind === "error" ? "field-error" : "muted"}`} role="status">{note?.text ?? "\u00a0"}</p>
    </div>
  );
}

function ArchiveControls(props: { site: Site; onChange: (s: Site, text: string) => void; onError: (e: unknown) => void }) {
  const { site } = props;
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<Site>, text: string) => {
    setBusy(true);
    try {
      props.onChange(await fn(), text);
    } catch (e) {
      props.onError(e);
    } finally {
      setBusy(false);
    }
  };
  return site.archived ? (
    <div className="danger-row">
      <div>
        <strong>Restore</strong>
        <p className="muted small">Brings the site back on the management dashboard. Polling and the public page stay off until you turn them on.</p>
      </div>
      <button className="secondary" disabled={busy} onClick={() => run(() => api.restoreSite(site.id), "Site restored.")}>
        Restore site
      </button>
    </div>
  ) : (
    <div className="danger-row">
      <div>
        <strong>Archive</strong>
        <p className="muted small">Hides the site everywhere and stops polling. All history is kept and it can be restored.</p>
      </div>
      <button
        className="secondary"
        disabled={busy}
        onClick={() => {
          if (window.confirm(`Archive ${site.name}? It will be hidden everywhere and polling will stop. History is kept.`)) {
            run(() => api.archiveSite(site.id), "Site archived.");
          }
        }}
      >
        Archive site
      </button>
    </div>
  );
}

function DeleteSite(props: { site: Site; onDeleted: () => void; onError: (e: unknown) => void }) {
  const { site } = props;
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const matches = typed.trim() === site.name;
  return (
    <div className="danger-row column">
      <div>
        <strong>Delete permanently</strong>
        <p className="muted small">
          Removes the site, its saved connection and all of its history from GrainTime. This cannot be undone. Nothing
          is changed on the site's own SQL Server. To keep the history, archive the site instead.
        </p>
      </div>
      {!open ? (
        <button className="danger" onClick={() => setOpen(true)}>Delete site…</button>
      ) : (
        <div className="delete-confirm">
          <label className="field">
            <span className="field-label">Type the site name <code>{site.name}</code> to confirm</span>
            <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          </label>
          <div className="actions-inline">
            <button
              className="danger"
              disabled={!matches || busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await api.deleteSite(site.id, typed);
                  props.onDeleted();
                } catch (e) {
                  props.onError(e);
                  setBusy(false);
                }
              }}
            >
              Permanently delete {site.name} and its history
            </button>
            <button className="secondary" onClick={() => { setOpen(false); setTyped(""); }}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

function SettingsPage() {
  const [saved, setSaved] = useState(false);
  return (
    <>
      <h2>Defaults for all sites</h2>
      {saved ? <Notice kind="ok">Saved.</Notice> : null}
      <section className="card">
        <DefaultsForm onSaved={() => setSaved(true)} />
      </section>
    </>
  );
}
