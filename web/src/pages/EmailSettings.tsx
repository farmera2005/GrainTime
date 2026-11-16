// Configuration -> Email: how GrainTime sends scheduled reports.
import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, EmailSettings } from "../api";
import { Field, Notice } from "../components/Field";

export function EmailSettingsPage() {
  const [s, setS] = useState<EmailSettings | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [testTo, setTestTo] = useState("");
  const [test, setTest] = useState<{ ok: boolean; message: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.getEmail().then((v) => setS({ ...v, client_secret: "", smtp_password: "",
      link_base_url: v.link_base_url || window.location.origin })).catch((e) => setMsg({ kind: "error", text: e.message }));
  }, []);
  if (!s) return msg ? <Notice kind="error">{msg.text}</Notice> : <p className="muted">Loading…</p>;
  const set = (p: Partial<EmailSettings>) => setS({ ...s, ...p });

  const save = async (ev: FormEvent) => {
    ev.preventDefault();
    setBusy(true); setErrors({}); setMsg(null);
    try {
      const v = await api.putEmail(s);
      setS({ ...v, client_secret: "", smtp_password: "" });
      setMsg({ kind: "ok", text: v.configured ? "Saved. Scheduled reports will be sent with these settings." : "Saved, but email isn't complete yet." });
    } catch (e) {
      if (e instanceof ApiError) { setErrors(e.fields); setMsg({ kind: "error", text: e.message }); } else setMsg({ kind: "error", text: "Could not save." });
    } finally { setBusy(false); window.scrollTo({ top: 0 }); }
  };
  const sendTest = async () => {
    setBusy(true); setTest(null);
    try { setTest(await api.testEmail({ settings: s, to: testTo })); }
    catch (e) { setTest({ ok: false, message: e instanceof Error ? e.message : "Test failed" }); }
    finally { setBusy(false); }
  };

  return (
    <form className="form" onSubmit={save} noValidate>
      <h2>Email</h2>
      <p className="muted">How GrainTime sends scheduled reports. Secrets are stored encrypted and never shown again.</p>
      {msg ? <Notice kind={msg.kind}>{msg.text}</Notice> : null}

      <section className="card">
        <h3>Send with</h3>
        <div className="radio-row">
          <label className="check"><input type="radio" checked={s.method === "graph"} onChange={() => set({ method: "graph" })} />
            <span><strong>Microsoft 365 (Microsoft Graph)</strong>: recommended. Sends from a Microsoft 365 mailbox through an app registration. (Microsoft has been retiring password sign-in for SMTP.)</span></label>
          <label className="check"><input type="radio" checked={s.method === "smtp"} onChange={() => set({ method: "smtp" })} />
            <span><strong>SMTP</strong>: an Exchange / Microsoft 365 relay connector, an internal relay, or another mail server.</span></label>
        </div>
        <div className="row">
          <Field label="Send from (mailbox address)" error={errors.from_address}><input value={s.from_address} placeholder="graintime@mercerlandmark.com" onChange={(e) => set({ from_address: e.target.value })} /></Field>
          <Field label="Sender name"><input value={s.from_name} onChange={(e) => set({ from_name: e.target.value })} /></Field>
        </div>
      </section>

      {s.method === "graph" ? (
        <section className="card">
          <h3>Microsoft 365 app registration</h3>
          <details>
            <summary>How to set it up (about 5 minutes, needs a Microsoft 365 admin)</summary>
            <ol className="small">
              <li>In the <strong>Microsoft Entra admin center</strong>: App registrations → New registration, name it <em>GrainTime</em>, single tenant, no redirect URI.</li>
              <li>API permissions → Add → <strong>Microsoft Graph</strong> → <strong>Application permissions</strong> → <strong>Mail.Send</strong>, then <strong>Grant admin consent</strong>.</li>
              <li>Certificates &amp; secrets → New client secret. Copy its <em>Value</em> (shown once) into Client secret below, and note when it expires.</li>
              <li>Copy the <em>Directory (tenant) ID</em> and <em>Application (client) ID</em> from the app's Overview.</li>
              <li>Recommended: limit the app to the sending mailbox only (an Exchange application access policy or RBAC for Applications), so it can't send as anyone else.</li>
            </ol>
          </details>
          <div className="row">
            <Field label="Directory (tenant) ID" error={errors.tenant_id}><input value={s.tenant_id} onChange={(e) => set({ tenant_id: e.target.value })} spellCheck={false} /></Field>
            <Field label="Application (client) ID" error={errors.client_id}><input value={s.client_id} onChange={(e) => set({ client_id: e.target.value })} spellCheck={false} /></Field>
          </div>
          <Field label="Client secret" hint={s.has_client_secret ? "A secret is saved. Leave blank to keep it." : "Stored encrypted; never shown again."} error={errors.client_secret}>
            <input type="password" autoComplete="new-password" value={s.client_secret ?? ""} onChange={(e) => set({ client_secret: e.target.value })} />
          </Field>
        </section>
      ) : (
        <section className="card">
          <h3>SMTP server</h3>
          <div className="row">
            <Field label="Server" error={errors.smtp_host}><input value={s.smtp_host} placeholder="mercerlandmark-com.mail.protection.outlook.com" onChange={(e) => set({ smtp_host: e.target.value })} spellCheck={false} /></Field>
            <Field label="Port" error={errors.smtp_port}><input inputMode="numeric" value={String(s.smtp_port)} onChange={(e) => set({ smtp_port: Number(e.target.value.replace(/\D/g, "")) || 0 })} /></Field>
            <Field label="Encryption"><select value={s.smtp_security} onChange={(e) => set({ smtp_security: e.target.value as EmailSettings["smtp_security"] })}>
              <option value="starttls">STARTTLS (usually port 587 or 25)</option><option value="ssl">SSL/TLS (usually port 465)</option><option value="none">None (internal relay only)</option></select></Field>
          </div>
          <div className="row">
            <Field label="Username (optional)" hint="Leave blank for a relay that accepts mail from this server's address."><input value={s.smtp_username} onChange={(e) => set({ smtp_username: e.target.value })} autoComplete="off" /></Field>
            <Field label="Password" hint={s.has_smtp_password ? "A password is saved. Leave blank to keep it." : ""}><input type="password" autoComplete="new-password" value={s.smtp_password ?? ""} onChange={(e) => set({ smtp_password: e.target.value })} /></Field>
          </div>
        </section>
      )}

      <section className="card">
        <h3>Links and certificates</h3>
        <Field label="GrainTime's address (for links in emails)" hint="As people open it, e.g. http://graintime:8080" error={errors.link_base_url}>
          <input value={s.link_base_url} onChange={(e) => set({ link_base_url: e.target.value })} spellCheck={false} />
        </Field>
        <Field label="Extra CA certificate (optional)" hint="Only if your network inspects HTTPS/TLS with its own certificate authority and the test reports an untrusted certificate. Verification always stays on." error={errors.ca_cert_pem}>
          <textarea rows={3} className="mono" value={s.ca_cert_pem ?? ""} placeholder="-----BEGIN CERTIFICATE-----" onChange={(e) => set({ ca_cert_pem: e.target.value || null })} spellCheck={false} />
        </Field>
      </section>

      <section className="card">
        <h3>Test</h3>
        <p className="muted small">Sends a short message with the settings above, without saving them.</p>
        <div className="actions-inline">
          <input className="inline-input" type="email" value={testTo} placeholder="you@mercerlandmark.com" onChange={(e) => setTestTo(e.target.value)} aria-label="Send the test to" />
          <button type="button" className="secondary" disabled={busy || !testTo} onClick={sendTest}>{busy ? "Sending…" : "Send test email"}</button>
        </div>
        {test ? <Notice kind={test.ok ? "ok" : "error"}>{test.message}</Notice> : null}
      </section>
      <div className="actions"><button type="submit" disabled={busy}>Save</button></div>
    </form>
  );
}
