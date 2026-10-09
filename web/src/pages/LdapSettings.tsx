// Configuration -> Sign-in & LDAP: directory sign-in for staff. Local accounts
// always keep working, so an administrator can never be locked out by it.
import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, LdapSettings, LdapTestResult } from "../api";
import { Field, Notice } from "../components/Field";

const lines = (v: string) => v.split("\n").map((x) => x.trim()).filter(Boolean);

const STEP_LABEL: Record<string, string> = {
  input: "Username", connect: "Connect", tls: "Encryption", service_bind: "Service account",
  search: "Find user", user_bind: "Check password", groups: "Groups and role", error: "Result",
};

export function LdapSettingsPage() {
  const [s, setS] = useState<LdapSettings | null>(null);
  const [text, setText] = useState({ servers: "", admin: "", viewer: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState({ username: "", password: "" });
  const [result, setResult] = useState<LdapTestResult | null>(null);

  useEffect(() => {
    api.getLdap().then((v) => {
      setS({ ...v, bind_password: "" });
      setText({ servers: v.servers.join("\n"), admin: v.admin_groups.join("\n"), viewer: v.viewer_groups.join("\n") });
    }).catch((e) => setMsg({ kind: "error", text: e.message }));
  }, []);
  if (!s) return msg ? <Notice kind="error">{msg.text}</Notice> : <p className="muted">Loading…</p>;

  const current = (): LdapSettings => ({ ...s, servers: lines(text.servers), admin_groups: lines(text.admin), viewer_groups: lines(text.viewer) });
  const set = (patch: Partial<LdapSettings>) => setS({ ...s, ...patch });

  const save = async (ev: FormEvent) => {
    ev.preventDefault();
    setBusy(true); setErrors({}); setMsg(null);
    try {
      const v = await api.putLdap(current());
      setS({ ...v, bind_password: "" });
      setMsg({ kind: "ok", text: v.enabled ? "Saved. Staff can now sign in with their directory account." : "Saved. Directory sign-in is off." });
    } catch (e) {
      if (e instanceof ApiError) { setErrors(e.fields); setMsg({ kind: "error", text: e.message }); }
      else setMsg({ kind: "error", text: "Could not save." });
    } finally { setBusy(false); }
    window.scrollTo({ top: 0 });
  };

  const runTest = async () => {
    setBusy(true); setResult(null);
    try {
      setResult(await api.testLdap({ settings: { ...current(), enabled: true }, username: test.username || undefined, password: test.password || undefined }));
    } catch (e) {
      setResult({ ok: false, steps: [{ step: "error", ok: false, detail: e instanceof Error ? e.message : "Test failed" }], user: null });
    } finally { setBusy(false); }
  };

  return (
    <form className="form" onSubmit={save} noValidate>
      <h2>Sign-in & LDAP</h2>
      <p className="muted">
        Let staff sign in with their network (Active Directory or other LDAP) username and password. Local GrainTime
        accounts always keep working and are checked first, so keep at least one local administrator under Users.
      </p>
      {msg ? <Notice kind={msg.kind}>{msg.text}</Notice> : null}

      <section className="card">
        <label className="check">
          <input type="checkbox" checked={s.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          <span><strong>Allow directory sign-in</strong></span>
        </label>
      </section>

      <section className="card">
        <h3>Directory servers</h3>
        <Field label="Servers" hint="One per line, tried in order, e.g. dc1.corp.example or ldaps://dc1.corp.example:636" error={errors.servers}>
          <textarea rows={3} value={text.servers} onChange={(e) => setText({ ...text, servers: e.target.value })} spellCheck={false} />
        </Field>
        <fieldset>
          <legend>Connection security</legend>
          <div className="radio-row">
            <label className="check"><input type="radio" checked={s.security === "ldaps"} onChange={() => set({ security: "ldaps" })} /> LDAPS (encrypted, port 636). Recommended.</label>
            <label className="check"><input type="radio" checked={s.security === "starttls"} onChange={() => set({ security: "starttls" })} /> StartTLS (port 389, then encrypted)</label>
            <label className="check"><input type="radio" checked={s.security === "none"} onChange={() => set({ security: "none" })} /> None (port 389)</label>
          </div>
          {s.security === "none" ? <Notice kind="warn">Passwords would cross the network unencrypted. Use this only to test, and only on a trusted network.</Notice> : null}
        </fieldset>
        {s.security !== "none" ? (
          <>
            <Field label="Directory CA certificate (optional)" hint="Only needed if the server's certificate comes from your own certificate authority (for example AD Certificate Services) and is not trusted yet. Paste it in PEM form." error={errors.ca_cert_pem}>
              <textarea rows={4} className="mono" value={s.ca_cert_pem ?? ""} placeholder="-----BEGIN CERTIFICATE-----" onChange={(e) => set({ ca_cert_pem: e.target.value || null })} spellCheck={false} />
            </Field>
            <label className="check">
              <input type="checkbox" checked={!s.verify_cert} onChange={(e) => set({ verify_cert: !e.target.checked })} />
              <span>Don't check the server's certificate</span>
            </label>
            {!s.verify_cert ? <Notice kind="warn">Not recommended. The connection is encrypted but the server isn't verified, so a machine pretending to be your domain controller could collect passwords. Prefer pasting the CA certificate above.</Notice> : null}
          </>
        ) : null}
      </section>

      <section className="card">
        <h3>Finding users</h3>
        <div className="radio-row">
          <label className="check"><input type="radio" checked={s.bind_mode === "service"} onChange={() => set({ bind_mode: "service" })} /> Use a read-only service account to look users up (recommended)</label>
          <label className="check"><input type="radio" checked={s.bind_mode === "direct"} onChange={() => set({ bind_mode: "direct" })} /> Sign in to the directory directly as each user</label>
        </div>
        {s.bind_mode === "service" ? (
          <div className="row">
            <Field label="Service account (bind DN or user@domain)" hint="e.g. CN=svc-graintime,OU=Service Accounts,DC=corp,DC=example" error={errors.bind_dn}>
              <input value={s.bind_dn ?? ""} onChange={(e) => set({ bind_dn: e.target.value })} spellCheck={false} />
            </Field>
            <Field label="Service account password" hint={s.has_bind_password ? "A password is saved. Leave blank to keep it." : "Stored encrypted; never shown again."} error={errors.bind_password}>
              <input type="password" autoComplete="new-password" value={s.bind_password ?? ""} onChange={(e) => set({ bind_password: e.target.value })} />
            </Field>
          </div>
        ) : (
          <Field label="Sign-in name template" hint="{username} is replaced by what the user types, e.g. {username}@corp.example or CORP\{username}" error={errors.direct_bind_template}>
            <input value={s.direct_bind_template ?? ""} onChange={(e) => set({ direct_bind_template: e.target.value })} spellCheck={false} />
          </Field>
        )}
        <div className="row">
          <Field label="Search base (base DN)" hint="Where users are, e.g. DC=corp,DC=example" error={errors.base_dn}>
            <input value={s.base_dn} onChange={(e) => set({ base_dn: e.target.value })} spellCheck={false} />
          </Field>
          <Field label="Display name attribute" error={errors.display_name_attr}>
            <input value={s.display_name_attr} onChange={(e) => set({ display_name_attr: e.target.value })} spellCheck={false} />
          </Field>
        </div>
        <Field label="User filter" hint={<>{"{username}"} is replaced by the (escaped) username. <button type="button" className="link" onClick={() => set({ user_filter: "(&(objectClass=user)(sAMAccountName={username}))", display_name_attr: "displayName" })}>Active Directory</button> <button type="button" className="link" onClick={() => set({ user_filter: "(&(objectClass=inetOrgPerson)(uid={username}))", display_name_attr: "cn" })}>OpenLDAP</button></>} error={errors.user_filter}>
          <input className="mono" value={s.user_filter} onChange={(e) => set({ user_filter: e.target.value })} spellCheck={false} />
        </Field>
        <p className="muted small">Users may type <code>adamf</code>, <code>CORP\adamf</code> or <code>adamf@corp.example</code>; all mean <code>adamf</code>.</p>
      </section>

      <section className="card">
        <h3>Who may sign in, and as what</h3>
        <div className="row">
          <Field label="Administrator groups" hint="Group DNs, one per line. Members can change everything under Configuration." error={errors.admin_groups}>
            <textarea rows={3} className="mono" value={text.admin} onChange={(e) => setText({ ...text, admin: e.target.value })} spellCheck={false} placeholder="CN=GrainTime Admins,OU=Groups,DC=corp,DC=example" />
          </Field>
          <Field label="Viewer groups" hint="Members can see the dashboard." error={errors.viewer_groups}>
            <textarea rows={3} className="mono" value={text.viewer} onChange={(e) => setText({ ...text, viewer: e.target.value })} spellCheck={false} placeholder="CN=GrainTime Viewers,OU=Groups,DC=corp,DC=example" />
          </Field>
        </div>
        <label className="check">
          <input type="checkbox" checked={s.nested_groups} onChange={(e) => set({ nested_groups: e.target.checked })} />
          <span>Include nested groups (members of groups inside these groups). <span className="muted">Active Directory only; turn off for other directories.</span></span>
        </label>
        <label className="check">
          <input type="checkbox" checked={s.allow_any_user} onChange={(e) => set({ allow_any_user: e.target.checked })} />
          <span>Let any directory user found by the filter sign in as a viewer, even if not in a viewer group</span>
        </label>
        <p className="muted small">Roles are worked out from the groups at every sign-in. Anyone in neither kind of group is refused.</p>
      </section>

      <section className="card">
        <h3>Test</h3>
        <p className="muted small">Tries the settings above without saving them. Leave the user blank to check only the connection and service account. Nothing you type here is stored.</p>
        <div className="row">
          <Field label="Test username"><input value={test.username} autoComplete="off" onChange={(e) => setTest({ ...test, username: e.target.value })} /></Field>
          <Field label="Test password"><input type="password" autoComplete="new-password" value={test.password} onChange={(e) => setTest({ ...test, password: e.target.value })} /></Field>
        </div>
        <button type="button" className="secondary" disabled={busy} onClick={runTest}>{busy ? "Testing…" : "Test settings"}</button>
        {result ? (
          <div className={`notice ${result.ok ? "notice-ok" : "notice-error"}`} role="status">
            <strong>{result.ok ? (result.user ? `Sign-in works: ${result.user.display_name} would be ${result.user.role === "admin" ? "an administrator" : "a viewer"}.` : "Connection works.") : "The test failed."}</strong>
            <table className="checks"><tbody>
              {result.steps.map((st, i) => (
                <tr key={i}><td className={st.ok ? "check-ok" : "check-bad"}>{st.ok ? "✓" : "✕"}</td><td>{STEP_LABEL[st.step] ?? st.step}</td><td>{st.detail}</td></tr>
              ))}
            </tbody></table>
          </div>
        ) : null}
      </section>

      <div className="actions"><button type="submit" disabled={busy}>Save</button></div>
    </form>
  );
}
