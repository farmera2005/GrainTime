// Configuration -> Users: local accounts and the directory (LDAP) users who
// have signed in. Directory users' roles come from their groups.
import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, User, UserAdmin } from "../api";
import { Field, Notice } from "../components/Field";
import { ago } from "../format";

export function UsersPage({ me }: { me: User }) {
  const [users, setUsers] = useState<UserAdmin[] | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [resetFor, setResetFor] = useState<UserAdmin | null>(null);
  const load = () => api.listUsers().then(setUsers).catch((e) => setMsg({ kind: "error", text: e.message }));
  useEffect(() => { load(); }, []);

  const change = async (u: UserAdmin, patch: Parameters<typeof api.updateUser>[1], text: string) => {
    setMsg(null);
    try {
      await api.updateUser(u.id, patch);
      setMsg({ kind: "ok", text });
      load();
    } catch (e) {
      setMsg({ kind: "error", text: e instanceof Error ? e.message : "Could not save." });
    }
  };

  return (
    <>
      <div className="page-head">
        <h2>Users</h2>
        <button onClick={() => setAdding(!adding)}>{adding ? "Cancel" : "Add local user"}</button>
      </div>
      <p className="muted">
        <strong>Local</strong> accounts sign in with a GrainTime password; keep at least one local administrator for
        when the directory is unreachable. <strong>Directory</strong> users appear here after their first sign-in; their
        role follows their groups (see <Link to="/config/sign-in">Sign-in & LDAP</Link>) and is updated at every sign-in.
        Disabling a user signs them out at once.
      </p>
      {msg ? <Notice kind={msg.kind}>{msg.text}</Notice> : null}
      {adding ? <AddUser onDone={(text) => { setAdding(false); setMsg({ kind: "ok", text }); load(); }} /> : null}
      {resetFor ? <ResetPassword user={resetFor} onDone={(text) => { setResetFor(null); if (text) setMsg({ kind: "ok", text }); }} /> : null}
      {users ? (
        <div className="table-wrap card">
          <table>
            <thead>
              <tr><th>User</th><th>Signs in with</th><th>Role</th><th>Last sign-in</th><th>Status</th><th></th></tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className={u.is_active ? "" : "archived"}>
                  <td><strong>{u.display_name}</strong><div className="muted small">{u.username}{u.id === me.id ? " (you)" : ""}</div></td>
                  <td>{u.auth_source === "ldap" ? "Directory (LDAP)" : "Local password"}</td>
                  <td>
                    {u.auth_source === "local" ? (
                      <select aria-label={`Role for ${u.username}`} value={u.role} disabled={u.id === me.id}
                        onChange={(e) => change(u, { role: e.target.value as "viewer" | "admin" }, `${u.username} is now ${e.target.value === "admin" ? "an administrator" : "a viewer"}.`)}>
                        <option value="viewer">Viewer</option>
                        <option value="admin">Administrator</option>
                      </select>
                    ) : (
                      <span title="From directory groups">{u.role === "admin" ? "Administrator" : "Viewer"} <span className="muted small">(from groups)</span></span>
                    )}
                  </td>
                  <td>{u.last_login_at ? ago(u.last_login_at) : <span className="muted">never</span>}</td>
                  <td>{u.is_active ? <span className="badge badge-ok">Active</span> : <span className="badge badge-muted">Disabled</span>}</td>
                  <td><div className="row-actions">
                    {u.auth_source === "local" ? <button className="link" onClick={() => setResetFor(u)}>Set password</button> : null}
                    {u.id !== me.id ? (
                      <button className="link" onClick={() => change(u, { is_active: !u.is_active }, `${u.username} ${u.is_active ? "disabled and signed out" : "enabled"}.`)}>
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                    ) : null}
                  </div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">Loading…</p>}
    </>
  );
}

function AddUser({ onDone }: { onDone: (text: string) => void }) {
  const [f, setF] = useState({ username: "", display_name: "", password: "", role: "viewer" as "viewer" | "admin" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    setErrors({});
    setError(null);
    try {
      const u = await api.createUser(f);
      onDone(`Local user ${u.username} added.`);
    } catch (e) {
      if (e instanceof ApiError) { setErrors(e.fields); setError(e.message); } else setError("Could not add the user.");
    }
  };
  return (
    <form className="form card" onSubmit={submit} noValidate>
      <h3>Add a local user</h3>
      <div className="row">
        <Field label="Username" error={errors.username}><input value={f.username} autoComplete="off" onChange={(e) => setF({ ...f, username: e.target.value })} /></Field>
        <Field label="Name" error={errors.display_name}><input value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} /></Field>
        <Field label="Password" hint="At least 12 characters" error={errors.password}><input type="password" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
        <Field label="Role">
          <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value as "viewer" | "admin" })}>
            <option value="viewer">Viewer (dashboard only)</option>
            <option value="admin">Administrator</option>
          </select>
        </Field>
      </div>
      {error ? <Notice kind="error">{error}</Notice> : null}
      <div className="actions"><button type="submit">Add user</button></div>
    </form>
  );
}

function ResetPassword({ user, onDone }: { user: UserAdmin; onDone: (text: string | null) => void }) {
  const [pw, setPw] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    try {
      await api.updateUser(user.id, { password: pw });
      onDone(`New password set for ${user.username}. They have been signed out everywhere.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not set the password.");
    }
  };
  return (
    <form className="form card narrow-card" onSubmit={submit}>
      <h3>Set a new password for {user.username}</h3>
      <Field label="New password" hint="At least 12 characters"><input type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} autoFocus /></Field>
      {error ? <Notice kind="error">{error}</Notice> : null}
      <div className="actions">
        <button type="button" className="secondary" onClick={() => onDone(null)}>Cancel</button>
        <button type="submit">Set password</button>
      </div>
    </form>
  );
}
