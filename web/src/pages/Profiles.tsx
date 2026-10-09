import { FormEvent, ReactNode, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, Lookup, MappingProfile, ProfileConfig, TicketStatus, WeighSteps } from "../api";
import { Field, Notice } from "../components/Field";
import { fmtDateTime } from "../format";

export function ProfileList() {
  const [profiles, setProfiles] = useState<MappingProfile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const nav = useNavigate();
  const load = () => api.listProfiles().then(setProfiles).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  return (
    <>
      <div className="page-head">
        <h2>Mapping profiles</h2>
        <Link className="button" to="/config/profiles/new">New profile</Link>
      </div>
      <p className="muted">
        A mapping profile tells GrainTime where tickets and weigh times are in a site's scale database. Sites on the
        same CompuWeigh version share one profile. GrainTime builds every query from the profile itself: read-only,
        bounded, never free-form SQL.
      </p>
      {error ? <Notice kind="error">{error}</Notice> : null}
      {profiles?.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Name</th><th>Used by</th><th>Updated</th><th></th></tr></thead>
            <tbody>
              {profiles.map((p) => (
                <tr key={p.id}>
                  <td><Link to={`/config/profiles/${p.id}`}>{p.name}</Link></td>
                  <td>{p.sites.length ? p.sites.map((s) => s.name).join(", ") : <span className="muted">no sites</span>}</td>
                  <td>{fmtDateTime(p.updated_at)}</td>
                  <td className="row-actions">
                    <Link className="button secondary small-button" to={`/config/profiles/${p.id}`}>Edit</Link>
                    <button className="secondary small-button" onClick={async () => {
                      try { const c = await api.cloneProfile(p.id); nav(`/config/profiles/${c.id}`); }
                      catch (e) { setError(e instanceof Error ? e.message : "Could not clone"); }
                    }}>Clone</button>
                    {!p.sites.length ? (
                      <button className="link" onClick={async () => {
                        if (!window.confirm(`Delete the mapping profile "${p.name}"?`)) return;
                        try { await api.deleteProfile(p.id); load(); }
                        catch (e) { setError(e instanceof Error ? e.message : "Could not delete"); }
                      }}>Delete</button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : profiles ? <p className="muted">No profiles yet.</p> : <p className="muted">Loading…</p>}
    </>
  );
}

type Errors = Record<string, string>;

export function ProfileEdit() {
  const params = useParams();
  const isNew = params.id === "new";
  const id = isNew ? null : Number(params.id);
  const nav = useNavigate();
  const [profile, setProfile] = useState<MappingProfile | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [cfg, setCfg] = useState<ProfileConfig | null>(null);
  const [errors, setErrors] = useState<Errors>({});
  const [message, setMessage] = useState<{ kind: "ok" | "error" | "warn"; text: string } | null>(null);
  const [confirmInUse, setConfirmInUse] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (isNew) {
      api.profileTemplate().then((t) => { setCfg(t.config); setName(""); });
    } else if (id) {
      api.getProfile(id).then((p) => {
        setProfile(p); setCfg(p.config); setName(p.name); setDescription(p.description ?? "");
      }).catch((e) => setMessage({ kind: "error", text: e.message }));
    }
  }, [id, isNew]);

  if (!cfg) return message ? <Notice kind={message.kind}>{message.text}</Notice> : <p className="muted">Loading…</p>;

  const set = <K extends keyof ProfileConfig>(k: K, v: ProfileConfig[K]) => setCfg({ ...cfg, [k]: v });
  const txt = (k: keyof ProfileConfig) => (cfg[k] as string | null | undefined) ?? "";
  const setTxt = (k: keyof ProfileConfig) => (v: string) => set(k, (v.trim() ? v : null) as never);
  const weighMode = cfg.weigh_steps ? "steps" : "columns";

  const save = async (ev?: FormEvent, confirm = false) => {
    ev?.preventDefault();
    setSaving(true); setErrors({}); setMessage(null);
    try {
      if (isNew) {
        const p = await api.createProfile({ name, description: description || null, config: cfg });
        nav(`/config/profiles/${p.id}`, { replace: true });
        setMessage({ kind: "ok", text: "Profile created." });
      } else if (id) {
        const p = await api.updateProfile(id, { name, description: description || null, config: cfg, confirm_in_use: confirm });
        setProfile(p); setConfirmInUse(false);
        setMessage({ kind: "ok", text: p.sites.length ? `Saved. ${p.sites.length} site(s) use it from their next poll.` : "Saved." });
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.message.includes("Confirm")) {
        setConfirmInUse(true);
        setMessage({ kind: "warn", text: e.message });
      } else if (e instanceof ApiError) {
        setErrors(e.fields);
        setMessage({ kind: "error", text: e.message });
      } else setMessage({ kind: "error", text: "Could not save." });
    } finally {
      setSaving(false);
      window.scrollTo({ top: 0 });
    }
  };

  return (
    <>
      <div className="page-head">
        <h2>{isNew ? "New mapping profile" : name || "Mapping profile"}</h2>
        <Link to="/config/profiles">All profiles</Link>
      </div>
      {message ? (
        <Notice kind={message.kind}>
          {message.text}
          {confirmInUse ? (
            <div className="actions-inline">
              <button onClick={() => save(undefined, true)} disabled={saving}>Apply to those sites</button>
              <button className="secondary" onClick={() => { setConfirmInUse(false); setMessage(null); }}>Cancel</button>
            </div>
          ) : null}
        </Notice>
      ) : null}
      {profile?.sites.length ? (
        <p className="muted small">Used by: {profile.sites.map((s) => s.name).join(", ")}. Test changes with <strong>Preview data</strong> on a site before saving.</p>
      ) : null}

      <form className="form" onSubmit={save} noValidate>
        <section className="card">
          <div className="row">
            <Field label="Profile name" error={errors.name}>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="CompuWeigh GMS" />
            </Field>
            <Field label="Description (optional)">
              <input value={description} onChange={(e) => setDescription(e.target.value)} />
            </Field>
          </div>
        </section>

        <Section title="Ticket table" hint="One row per truck transaction.">
          <div className="grid">
            <Text label="Ticket table" hint="schema.Table, e.g. dbo.TransactionID" value={cfg.ticket_table} onChange={(v) => set("ticket_table", v)} error={errors.ticket_table} />
            <Text label="Ticket ID column" hint="Unique, never changes" value={cfg.id_column} onChange={(v) => set("id_column", v)} error={errors.id_column} />
            <Text label="High-water mark column" hint="Always increases for new tickets (identity)" value={cfg.high_water_column} onChange={(v) => set("high_water_column", v)} error={errors.high_water_column} />
            <Text label="Ticket number column (optional)" hint="Printed number; shown in Preview only" value={txt("ticket_number_column")} onChange={setTxt("ticket_number_column")} />
            <Text label="Created time column" hint="Indexed; used for look-back and backfill" value={cfg.created_column} onChange={(v) => set("created_column", v)} error={errors.created_column} />
            <Text label="Completed time column (optional)" hint="Indexed; catches recent completions" value={txt("completed_column")} onChange={setTxt("completed_column")} />
            <Text label="Linked (split) ticket column (optional)" hint="Tickets pointing to a parent count once" value={txt("parent_column")} onChange={setTxt("parent_column")} />
          </div>
        </Section>

        <Section title="Status" hint="Translate each status value. Only completed tickets count toward time on site; voided ones are excluded and counted.">
          <div className="grid">
            <Text label="Status column" value={cfg.status_column} onChange={(v) => set("status_column", v)} error={errors.status_column} />
            <Text label="Void flag column (optional)" hint="If set and true, the ticket is voided" value={txt("void_column")} onChange={setTxt("void_column")} />
          </div>
          <MapEditor<TicketStatus>
            label="Status values"
            value={cfg.status_map}
            options={["open", "completed", "voided"]}
            onChange={(m) => set("status_map", m)}
            error={errors.status_map}
          />
        </Section>

        <Section title="Weigh times" hint="Time on site = outbound weigh − inbound weigh.">
          <div className="radio-row">
            <label className="check"><input type="radio" checked={weighMode === "steps"} onChange={() => setCfg({ ...cfg, inbound_column: null, outbound_column: null, weigh_steps: cfg.weigh_steps ?? { table: "", ticket_fk_column: "", time_column: "", weight_type_column: "" } })} /> From a table of weigh steps (CompuWeigh GMS: TransactionLog)</label>
            <label className="check"><input type="radio" checked={weighMode === "columns"} onChange={() => setCfg({ ...cfg, weigh_steps: null })} /> From inbound and outbound time columns on the ticket table</label>
          </div>
          {cfg.weigh_steps ? (
            <WeighStepsEditor value={cfg.weigh_steps} onChange={(w) => set("weigh_steps", w)} />
          ) : (
            <div className="grid">
              <Text label="Inbound weigh time column" value={txt("inbound_column")} onChange={setTxt("inbound_column")} />
              <Text label="Outbound weigh time column" value={txt("outbound_column")} onChange={setTxt("outbound_column")} />
            </div>
          )}
          {errors.config ? <Notice kind="error">{errors.config}</Notice> : null}
        </Section>

        <Section title="Transaction type and direction" hint="Which ticket types to track, and which are received vs shipped.">
          <div className="grid">
            <Text label="Type column on the ticket table" value={txt("type_column")} onChange={setTxt("type_column")} />
          </div>
          <LookupEditor label="Type lookup table" value={cfg.type_lookup ?? null} withDirection onChange={(l) => set("type_lookup", l)} />
          <MapEditor<"received" | "shipped">
            label="Direction values"
            value={cfg.direction_map}
            options={["received", "shipped"]}
            onChange={(m) => set("direction_map", m)}
          />
          <Text
            label="Track only these type codes"
            hint="Comma-separated, e.g. TRUCKIN. Leave empty to track every type."
            value={cfg.included_type_codes.join(", ")}
            onChange={(v) => set("included_type_codes", v.split(",").map((x) => x.trim()).filter(Boolean))}
          />
        </Section>

        <Section title="Commodity" hint="Product names come from a lookup table.">
          <div className="grid">
            <Text label="Product column on the ticket table" value={txt("product_column")} onChange={setTxt("product_column")} />
          </div>
          <LookupEditor label="Product lookup table" value={cfg.product_lookup ?? null} onChange={(l) => set("product_lookup", l)} />
        </Section>

        <Section title="Filter and polling" hint="Optional filter for databases shared by several locations; look-back windows catch edits and voids.">
          <div className="grid">
            <Text label="Filter column (optional)" value={txt("filter_column")} onChange={setTxt("filter_column")} />
            <Text label="Filter value" value={txt("filter_value")} onChange={setTxt("filter_value")} />
            <Num label="Re-read tickets created in the last" unit="hours" value={cfg.lookback_hours} onChange={(v) => set("lookback_hours", v)} />
            <Num label="Re-read tickets completed in the last" unit="hours" value={cfg.completed_lookback_hours} onChange={(v) => set("completed_lookback_hours", v)} />
            <Num label="Nightly re-check of the last" unit="days" value={cfg.nightly_recheck_days} onChange={(v) => set("nightly_recheck_days", v)} />
          </div>
        </Section>

        <div className="actions">
          <button type="submit" disabled={saving}>{saving ? "Saving…" : isNew ? "Create profile" : "Save profile"}</button>
        </div>
      </form>

      {!isNew && id ? <LoginScript profileId={id} /> : null}
    </>
  );
}

function Section(props: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="card">
      <h3>{props.title}</h3>
      {props.hint ? <p className="muted small">{props.hint}</p> : null}
      {props.children}
    </section>
  );
}

function Text(props: { label: string; hint?: string; value: string; onChange: (v: string) => void; error?: string }) {
  return (
    <Field label={props.label} hint={props.hint} error={props.error}>
      <input value={props.value} onChange={(e) => props.onChange(e.target.value)} spellCheck={false} />
    </Field>
  );
}

function Num(props: { label: string; unit: string; value: number; onChange: (v: number) => void }) {
  return (
    <Field label={props.label}>
      <span className="with-unit">
        <input inputMode="numeric" value={String(props.value)} onChange={(e) => props.onChange(Number(e.target.value.replace(/\D/g, "")) || 0)} />
        <span className="unit">{props.unit}</span>
      </span>
    </Field>
  );
}

function MapEditor<T extends string>(props: { label: string; value: Record<string, T>; options: T[]; onChange: (m: Record<string, T>) => void; error?: string }) {
  const entries = Object.entries(props.value);
  const [newKey, setNewKey] = useState("");
  return (
    <div className="map-editor">
      <div className="field-label">{props.label}</div>
      <table>
        <thead><tr><th>Value in the scale database</th><th>Means</th><th></th></tr></thead>
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k}>
              <td><code>{k}</code></td>
              <td>
                <select aria-label={`${props.label} ${k}`} value={v} onChange={(e) => props.onChange({ ...props.value, [k]: e.target.value as T })}>
                  {props.options.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </td>
              <td><button type="button" className="link" onClick={() => { const m = { ...props.value }; delete m[k]; props.onChange(m); }}>Remove</button></td>
            </tr>
          ))}
          <tr>
            <td><input aria-label={`New ${props.label} value`} value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="value" /></td>
            <td colSpan={2}>
              <button type="button" className="secondary small-button" disabled={!newKey.trim()} onClick={() => {
                props.onChange({ ...props.value, [newKey.trim()]: props.options[0] }); setNewKey("");
              }}>Add value</button>
            </td>
          </tr>
        </tbody>
      </table>
      {props.error ? <span className="field-error">{props.error}</span> : null}
    </div>
  );
}

function WeighStepsEditor(props: { value: WeighSteps; onChange: (w: WeighSteps) => void }) {
  const v = props.value;
  const set = (k: keyof WeighSteps) => (x: string) => props.onChange({ ...v, [k]: x.trim() ? x : (k === "table" || k === "ticket_fk_column" || k === "time_column" || k === "weight_type_column" ? "" : null) });
  return (
    <div className="grid">
      <Text label="Weigh steps table" hint="e.g. dbo.TransactionLog" value={v.table} onChange={set("table")} />
      <Text label="Ticket link column" hint="Points to the ticket ID" value={v.ticket_fk_column} onChange={set("ticket_fk_column")} />
      <Text label="Weigh time column" hint="When the weight was recorded" value={v.time_column} onChange={set("time_column")} />
      <Text label="Weight type column" hint="Gross/tare; empty on non-weigh steps" value={v.weight_type_column} onChange={set("weight_type_column")} />
      <Text label="Step status column (optional)" value={v.status_column ?? ""} onChange={set("status_column")} />
      <Text label="Status value for a finished step" value={v.status_done_value ?? ""} onChange={set("status_done_value")} />
    </div>
  );
}

function LookupEditor(props: { label: string; value: Lookup | null; withDirection?: boolean; onChange: (l: Lookup | null) => void }) {
  const v = props.value;
  if (!v) {
    return (
      <p className="small">
        No {props.label.toLowerCase()}.{" "}
        <button type="button" className="link" onClick={() => props.onChange({ table: "", key_column: "", code_column: "" })}>Add one</button>
      </p>
    );
  }
  const set = (k: keyof Lookup) => (x: string) => props.onChange({ ...v, [k]: x.trim() ? x : (k === "description_column" || k === "direction_column" ? null : "") });
  return (
    <div>
      <div className="field-label">{props.label} <button type="button" className="link" onClick={() => props.onChange(null)}>Remove</button></div>
      <div className="grid">
        <Text label="Table" value={v.table} onChange={set("table")} />
        <Text label="Key column" value={v.key_column} onChange={set("key_column")} />
        <Text label="Code column" value={v.code_column} onChange={set("code_column")} />
        <Text label="Description column (optional)" value={v.description_column ?? ""} onChange={set("description_column")} />
        {props.withDirection ? <Text label="Direction column (optional)" value={v.direction_column ?? ""} onChange={set("direction_column")} /> : null}
      </div>
    </div>
  );
}

function LoginScript({ profileId }: { profileId: number }) {
  const [database, setDatabase] = useState("GMS");
  const [login, setLogin] = useState("graintime");
  const [windows, setWindows] = useState(false);
  const [script, setScript] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const make = async () => {
    setError(null);
    try { setScript(await api.loginScript(profileId, database, login, windows)); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not create the script"); }
  };
  return (
    <section className="card">
      <h3>Read-only login script</h3>
      <p className="muted small">
        T-SQL for the site's SQL Server that creates GrainTime's login with SELECT on only the columns this profile reads.
        It can't read customer, driver, plate, weight or price columns. Run it in SSMS as a sysadmin, then set the password.
      </p>
      <div className="row">
        <Field label="Database name"><input value={database} onChange={(e) => setDatabase(e.target.value)} /></Field>
        <Field label="Login name" hint={windows ? "DOMAIN\\user" : "SQL login"}><input value={login} onChange={(e) => setLogin(e.target.value)} /></Field>
      </div>
      <label className="check"><input type="checkbox" checked={windows} onChange={(e) => setWindows(e.target.checked)} /> Windows (domain) account instead of a SQL login</label>
      <div className="actions-inline">
        <button type="button" className="secondary" onClick={make}>Create script</button>
        {script ? (
          <>
            <button type="button" className="secondary" onClick={() => navigator.clipboard?.writeText(script)}>Copy</button>
            <a className="button secondary" download={`graintime-login-${database}.sql`} href={`data:text/plain;charset=utf-8,${encodeURIComponent(script)}`}>Download .sql</a>
          </>
        ) : null}
      </div>
      {error ? <Notice kind="error">{error}</Notice> : null}
      {script ? <pre className="script">{script}</pre> : null}
    </section>
  );
}
