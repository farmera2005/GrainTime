import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, ConnectionFields, Encrypt, Site, SiteInput } from "../api";
import { useJob } from "../useJob";
import { Field, Notice } from "./Field";
import { TestConnectionOutcome } from "./JobOutcome";

type FormState = {
  name: string;
  code: string;
  address: string;
  map_url: string;
  host: string;
  port: string;
  instance_name: string;
  database: string;
  username: string;
  password: string;
  encrypt: Encrypt;
  trust_server_certificate: boolean;
};

function fromSite(s?: Site): FormState {
  return {
    name: s?.name ?? "",
    code: s?.code ?? "",
    address: s?.address ?? "",
    map_url: s?.map_url ?? "",
    host: s?.host ?? "",
    port: s ? String(s.port) : "1433",
    instance_name: s?.instance_name ?? "",
    database: s?.database ?? "",
    username: s?.username ?? "",
    password: "",
    encrypt: s?.encrypt ?? "yes",
    // Most SQL Server Express installs use a self-signed certificate.
    trust_server_certificate: s?.trust_server_certificate ?? true,
  };
}

/**
 * Add or edit a site. Used by both the setup wizard and the admin panel, so
 * the first site is registered through exactly the same path as every other.
 */
export function SiteForm(props: { site?: Site; onSaved: (s: Site) => void; submitLabel?: string }) {
  const { site } = props;
  const [f, setF] = useState<FormState>(() => fromSite(site));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testJobId, setTestJobId] = useState<number | null>(null);
  const testJob = useJob(testJobId);
  const testing = !!testJob && (testJob.status === "queued" || testJob.status === "running");

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => {
    setF((prev) => ({ ...prev, [k]: v }));
    setErrors((e) => ({ ...e, [k === "database" ? "database" : k]: "" }));
  };

  // Typing SERVER\INSTANCE (as Windows clients use) splits it into the two fields.
  // With an instance name, a still-default port is cleared so it gets looked up.
  const setHost = (value: string) => {
    const i = value.indexOf("\\");
    if (i < 0) return set("host", value);
    setF((prev) => ({
      ...prev,
      host: value.slice(0, i),
      instance_name: value.slice(i + 1),
      port: !site && prev.port === "1433" ? "" : prev.port,
    }));
  };
  const setInstance = (value: string) =>
    setF((prev) => ({
      ...prev,
      instance_name: value,
      port: !site && value.trim() && prev.port === "1433" ? "" : prev.port,
    }));

  // A port looked up from the instance name is filled into the form.
  useEffect(() => {
    const p = testJob?.status === "succeeded" ? testJob.result?.resolved_port : undefined;
    if (p) setF((prev) => ({ ...prev, port: String(p) }));
  }, [testJob?.status, testJob?.result?.resolved_port]);

  const connection = (): ConnectionFields => ({
    host: f.host.trim(),
    port: f.port ? Number(f.port) : null,
    instance_name: f.instance_name.trim() || null,
    database: f.database.trim(),
    username: f.username.trim(),
    password: f.password || undefined,
    encrypt: f.encrypt,
    trust_server_certificate: f.trust_server_certificate,
  });

  const test = async () => {
    setMessage(null);
    setErrors({});
    try {
      // Saved site with no new password: the collector uses the stored one.
      const body = site ? { site_id: site.id, connection: connection() } : { connection: connection() };
      const job = await api.testConnection(body);
      setTestJobId(job.id);
    } catch (e) {
      handleError(e);
    }
  };

  const handleError = (e: unknown) => {
    if (e instanceof ApiError) {
      setErrors(e.fields);
      setMessage(e.message);
    } else {
      setMessage("Could not reach the server. Check your network and try again.");
    }
  };

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    if (!f.port) {
      setErrors({ port: "Enter the port, or click Test connection to look it up from the instance name." });
      setMessage("Please fix the highlighted fields.");
      return;
    }
    setSaving(true);
    setMessage(null);
    setErrors({});
    const body: SiteInput = {
      name: f.name.trim(),
      code: f.code.trim(),
      address: f.address.trim() || null,
      map_url: f.map_url.trim() || null,
      ...connection(),
    };
    try {
      const saved = site ? await api.updateSite(site.id, body) : await api.createSite(body);
      setF((prev) => ({ ...prev, password: "" }));
      props.onSaved(saved);
    } catch (e) {
      handleError(e);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="form" onSubmit={submit} noValidate>
      <fieldset>
        <legend>Site</legend>
        <div className="row">
          <Field label="Name" error={errors.name}>
            <input value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="Celina" required />
          </Field>
          <Field label="Short code" error={errors.code} hint="Letters and digits, used in links (e.g. CELINA)">
            <input value={f.code} onChange={(e) => set("code", e.target.value.toUpperCase())} maxLength={20} required />
          </Field>
        </div>
        <div className="row">
          <Field label="Address (optional)" error={errors.address} hint="Shown on the public page">
            <input value={f.address} onChange={(e) => set("address", e.target.value)} />
          </Field>
          <Field label="Map link (optional)" error={errors.map_url}>
            <input value={f.map_url} onChange={(e) => set("map_url", e.target.value)} placeholder="https://maps…" />
          </Field>
        </div>
      </fieldset>

      <fieldset>
        <legend>Scale database connection (CompuWeigh SQL Server)</legend>
        <div className="row">
          <Field label="Server name or IP address" error={errors.host} hint="You can paste SERVER\INSTANCE here">
            <input value={f.host} onChange={(e) => setHost(e.target.value)} placeholder="10.1.2.3" required />
          </Field>
          <Field label="Instance name (optional)" error={errors.instance_name} hint="e.g. SQLEXPRESS. Blank for the default instance">
            <input value={f.instance_name} onChange={(e) => setInstance(e.target.value)} placeholder="SQLEXPRESS" />
          </Field>
        </div>
        <div className="row">
          <Field
            label="TCP port"
            error={errors.port}
            hint={f.instance_name.trim() ? "Leave blank and click Test connection to look it up" : "Usually 1433 for the default instance"}
          >
            <input value={f.port} onChange={(e) => set("port", e.target.value.replace(/\D/g, ""))} inputMode="numeric" />
          </Field>
        </div>
        <div className="row">
          <Field label="Database name" error={errors.database}>
            <input value={f.database} onChange={(e) => set("database", e.target.value)} required />
          </Field>
          <Field label="SQL login" error={errors.username} hint="Read-only SQL authentication login">
            <input value={f.username} onChange={(e) => set("username", e.target.value)} autoComplete="off" required />
          </Field>
        </div>
        <Field
          label="Password"
          error={errors.password}
          hint={site?.has_password ? "A password is saved. Leave blank to keep it." : "Stored encrypted. It is never shown again."}
        >
          <input type="password" value={f.password} onChange={(e) => set("password", e.target.value)} autoComplete="new-password" />
        </Field>
        <div className="row">
          <Field label="Encryption" error={errors.encrypt} hint="ODBC Driver 18 encrypts by default">
            <select value={f.encrypt} onChange={(e) => set("encrypt", e.target.value as Encrypt)}>
              <option value="yes">Encrypt (recommended)</option>
              <option value="strict">Strict (TDS 8)</option>
              <option value="no">Do not require encryption</option>
            </select>
          </Field>
          <label className="check">
            <input
              type="checkbox"
              checked={f.trust_server_certificate}
              onChange={(e) => set("trust_server_certificate", e.target.checked)}
            />
            Trust the server certificate (needed for self-signed certificates, typical on SQL Express)
          </label>
        </div>
        <div className="actions-inline">
          <button type="button" className="secondary" onClick={test} disabled={testing}>
            {testing ? "Testing…" : "Test connection"}
          </button>
          <span className="muted small">Runs from the collector, read-only, 5 s connect timeout.</span>
        </div>
        <TestConnectionOutcome job={testJob} onUsePort={(p) => set("port", String(p))} />
      </fieldset>

      {message && !Object.values(errors).some(Boolean) ? <Notice kind="error">{message}</Notice> : null}
      {message && Object.values(errors).some(Boolean) ? <Notice kind="error">Please fix the highlighted fields.</Notice> : null}
      <div className="actions">
        <button type="submit" disabled={saving}>{saving ? "Saving…" : props.submitLabel ?? (site ? "Save changes" : "Save site")}</button>
      </div>
    </form>
  );
}
