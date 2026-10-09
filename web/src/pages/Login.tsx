import { FormEvent, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Field, Notice } from "../components/Field";

export function LoginForm({ onDone }: { onDone: () => void | Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [ldap, setLdap] = useState(false);
  useEffect(() => { api.authOptions().then((o) => setLdap(o.ldap)).catch(() => {}); }, []);

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      await onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="form narrow" onSubmit={submit}>
      <Field label="Username" hint={ldap ? "Your network (Windows) username, or a GrainTime account" : undefined}>
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus />
      </Field>
      <Field label="Password">
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
      </Field>
      {error ? <Notice kind="error">{error}</Notice> : null}
      <div className="actions"><button type="submit" disabled={busy}>Sign in</button></div>
    </form>
  );
}

export function LoginPage({ onDone }: { onDone: () => Promise<void> }) {
  return (
    <div className="setup">
      <header className="setup-header">
        <h1>GrainTime</h1>
        <p className="muted">Sign in</p>
      </header>
      <main className="card narrow-card">
        <LoginForm onDone={onDone} />
      </main>
    </div>
  );
}
