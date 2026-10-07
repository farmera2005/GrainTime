import { useCallback, useEffect, useState } from "react";
import { BrowserRouter } from "react-router-dom";
import { api, SetupStatus } from "./api";
import { Notice } from "./components/Field";
import { AdminApp } from "./pages/Admin";
import { DashboardPage } from "./pages/Dashboard";
import { LoginPage } from "./pages/Login";
import { SetupWizard } from "./pages/SetupWizard";

export function App() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.setupStatus());
      setError(null);
    } catch {
      setError("The GrainTime server is not responding yet. It may still be starting; this page retries automatically.");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Keep retrying while the api is unreachable (e.g. the stack is still starting).
  useEffect(() => {
    if (!error) return;
    const t = window.setTimeout(refresh, 3000);
    return () => window.clearTimeout(t);
  }, [error, refresh]);

  if (error && !status) return <div className="setup"><main className="card"><Notice kind="warn">{error}</Notice></main></div>;
  if (!status) return <div className="setup"><p className="muted">Loading…</p></div>;

  if (!status.setup_complete) return <SetupWizard status={status} refresh={refresh} />;
  if (!status.user) return <LoginPage onDone={refresh} />;

  const logout = async () => {
    await api.logout();
    await refresh();
  };

  if (status.user.role !== "admin") {
    return (
      <div className="app">
        <header className="topbar">
          <span className="brand">GrainTime</span>
          <nav><a href="/public/" target="_blank" rel="noopener">Public page ↗</a></nav>
          <span className="spacer" />
          <span className="muted">{status.user.display_name}</span>
          <button className="link" onClick={logout}>Sign out</button>
        </header>
        <main className="content content-wide"><DashboardPage /></main>
      </div>
    );
  }
  return (
    <BrowserRouter>
      <AdminApp user={status.user} onLogout={logout} />
    </BrowserRouter>
  );
}
