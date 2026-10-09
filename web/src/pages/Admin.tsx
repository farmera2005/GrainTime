import { useEffect, useState } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, Site, User } from "../api";
import { DefaultsForm } from "../components/DefaultsForm";
import { DataCollection } from "../components/DataCollection";
import { DiscoveryPanel } from "../components/Discovery";
import { ago } from "../format";
import { DashboardPage } from "./Dashboard";
import { ProfileEdit, ProfileList } from "./Profiles";
import { Notice } from "../components/Field";
import { SiteForm } from "../components/SiteForm";
import { HoursEditor } from "../components/HoursEditor";
import { UsersPage } from "./Users";
import { LdapSettingsPage } from "./LdapSettings";

/** The signed-in app for administrators: the dashboard plus Configuration. */
export function AdminApp({ user, onLogout }: { user: User; onLogout: () => Promise<void> }) {
  const path = useLocation().pathname;
  const wide = path.startsWith("/dashboard");
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/dashboard" className="brand">GrainTime</Link>
        <nav>
          <NavLink to="/dashboard">Dashboard</NavLink>
          <NavLink to="/config">Configuration</NavLink>
          <a href="/public/" target="_blank" rel="noopener">Public page ↗</a>
        </nav>
        <span className="spacer" />
        <span className="muted">{user.display_name}</span>
        <button className="link" onClick={onLogout}>Sign out</button>
      </header>
      <main className={wide ? "content content-wide" : "content"}>
        <Routes>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/config/*" element={<ConfigArea user={user} />} />
          {/* Old addresses (bookmarks) move to Configuration. */}
          <Route path="/admin/settings" element={<Navigate to="/config/collection" replace />} />
          <Route path="/admin/*" element={<Navigate to={path.replace(/^\/admin/, "/config")} replace />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  );
}

const CONFIG_SECTIONS: { group: string; items: { to: string; label: string }[] }[] = [
  { group: "Scale sites", items: [
    { to: "/config/sites", label: "Sites" },
    { to: "/config/profiles", label: "Mapping profiles" },
  ] },
  { group: "Statistics", items: [
    { to: "/config/collection", label: "Collection & metrics" },
    { to: "/config/public", label: "Public page" },
  ] },
  { group: "Access", items: [
    { to: "/config/users", label: "Users" },
    { to: "/config/sign-in", label: "Sign-in & LDAP" },
  ] },
];

/** Every setting in one place, with a side menu. */
function ConfigArea({ user }: { user: User }) {
  return (
    <div className="config">
      <nav className="config-nav" aria-label="Configuration">
        <h2 className="config-title">Configuration</h2>
        {CONFIG_SECTIONS.map((g) => (
          <div key={g.group} className="config-group">
            <div className="config-group-label">{g.group}</div>
            {g.items.map((i) => <NavLink key={i.to} to={i.to}>{i.label}</NavLink>)}
          </div>
        ))}
      </nav>
      <div className="config-body">
        <Routes>
          <Route path="sites" element={<SiteList />} />
          <Route path="sites/new" element={<NewSite />} />
          <Route path="sites/:id" element={<SiteDetail />} />
          <Route path="profiles" element={<ProfileList />} />
          <Route path="profiles/:id" element={<ProfileEdit />} />
          <Route path="collection" element={<CollectionSettings />} />
          <Route path="public" element={<PublicSettings />} />
          <Route path="users" element={<UsersPage me={user} />} />
          <Route path="sign-in" element={<LdapSettingsPage />} />
          <Route path="*" element={<Navigate to="/config/sites" replace />} />
        </Routes>
      </div>
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
        <Link className="button" to="/config/sites/new">Add site</Link>
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
                  <td><Link to={`/config/sites/${s.id}`}>{s.name}</Link></td>
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
                  <td><Link className="button secondary small-button" to={`/config/sites/${s.id}`}>Edit</Link></td>
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
      <SiteForm onSaved={(s) => nav(`/config/sites/${s.id}`)} />
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
        <Link to="/config/sites">All sites</Link>
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
        <h3>Operating hours</h3>
        <HoursEditor key={`${site.id}-hours`} site={site} onChange={setSite} />
      </section>

      <section className="card">
        <h3>Discovery</h3>
        <DiscoveryPanel siteId={site.id} />
      </section>

      <section className="card danger-zone">
        <h3>Archive or delete</h3>
        <ArchiveControls site={site} onChange={(s, text) => updated(s, text)} onError={failed} />
        <DeleteSite site={site} onDeleted={() => nav("/config/sites")} onError={failed} />
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
      {site.show_on_public ? (
        <p className="small muted">
          Shown on the <a href="/public/" target="_blank" rel="noopener">public page</a> within a minute. It lists the
          current time on site (only when enough trucks back it), trucks on site now, open or closed, and when the data
          was last updated. Nothing about individual trucks or customers.
        </p>
      ) : null}
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

const COLLECTION_FIELDS = ["threshold_green_max_min", "threshold_yellow_max_min", "poll_interval_s", "recent_window_min",
  "open_ticket_cutoff_hours", "duration_ceiling_hours", "backfill_default_days"] as const;

function CollectionSettings() {
  const [saved, setSaved] = useState(false);
  return (
    <>
      <h2>Collection & metrics</h2>
      <p className="muted">Defaults for every site: how often sites are polled, the time-on-site colour limits, and the
        rules the statistics use.</p>
      {saved ? <Notice kind="ok">Saved.</Notice> : null}
      <section className="card">
        <DefaultsForm fields={[...COLLECTION_FIELDS]} onSaved={() => setSaved(true)} />
      </section>
    </>
  );
}

function PublicSettings() {
  const [saved, setSaved] = useState(false);
  const [sites, setSites] = useState<Site[] | null>(null);
  useEffect(() => { api.listSites().then(setSites).catch(() => setSites([])); }, []);
  const shown = (sites ?? []).filter((s) => s.show_on_public && !s.archived);
  return (
    <>
      <div className="page-head">
        <h2>Public page</h2>
        <a className="button secondary" href="/public/" target="_blank" rel="noopener">Open the public page ↗</a>
      </div>
      <p className="muted">
        The farmer-facing wait-times page and its JSON feed. It is reachable inside your network at
        <code>/public/</code>; publishing it to the internet is a separate step (see the README).
      </p>
      {saved ? <Notice kind="ok">Saved.</Notice> : null}
      <section className="card">
        <h3>When to hold back a number</h3>
        <DefaultsForm fields={["public_min_trucks", "public_stale_after_min"]} onSaved={() => setSaved(true)} />
      </section>
      <section className="card">
        <h3>Sites on the public page</h3>
        {sites == null ? <p className="muted">Loading…</p> : shown.length ? (
          <ul>
            {shown.map((s) => (
              <li key={s.id}><Link to={`/config/sites/${s.id}`}>{s.name}</Link>{s.hours ? "" : <span className="muted"> (no operating hours set)</span>}</li>
            ))}
          </ul>
        ) : <p className="muted">No sites yet. Turn on <strong>Public page</strong> on a site's page under Sites.</p>}
      </section>
    </>
  );
}
