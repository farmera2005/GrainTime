import { useEffect, useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, Site, User } from "../api";
import { DefaultsForm } from "../components/DefaultsForm";
import { DiscoveryPanel } from "../components/Discovery";
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
          <Route path="*" element={<Navigate to="/admin/sites" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function SiteList() {
  const [sites, setSites] = useState<Site[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.listSites().then(setSites).catch((e) => setError(e.message));
  }, []);
  return (
    <>
      <div className="page-head">
        <h2>Sites</h2>
        <Link className="button" to="/admin/sites/new">Add site</Link>
      </div>
      {error ? <Notice kind="error">{error}</Notice> : null}
      {sites && !sites.length ? <p className="muted">No sites yet.</p> : null}
      {sites?.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Name</th><th>Code</th><th>Server</th><th>Database</th><th>Polling</th><th>Public</th></tr>
            </thead>
            <tbody>
              {sites.map((s) => (
                <tr key={s.id}>
                  <td><Link to={`/admin/sites/${s.id}`}>{s.name}</Link></td>
                  <td>{s.code}</td>
                  <td>{s.host}:{s.port}</td>
                  <td>{s.database}</td>
                  <td>{s.polling_enabled ? "On" : "Off"}</td>
                  <td>{s.show_on_public ? "Shown" : "Hidden"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
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
  const [site, setSite] = useState<Site | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.getSite(id).then(setSite).catch((e) => setError(e.message));
  }, [id]);
  if (error) return <Notice kind="error">{error}</Notice>;
  if (!site) return <p className="muted">Loading…</p>;
  return (
    <>
      <div className="page-head">
        <h2>{site.name} <span className="muted">({site.code})</span></h2>
        <Link to="/admin/sites">All sites</Link>
      </div>
      {saved ? <Notice kind="ok">Saved.</Notice> : null}
      <section className="card">
        <SiteForm key={site.id} site={site} onSaved={(s) => { setSite(s); setSaved(true); }} />
      </section>
      <section className="card">
        <h3>Discovery</h3>
        <DiscoveryPanel siteId={site.id} />
      </section>
    </>
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
