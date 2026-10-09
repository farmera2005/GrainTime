import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, Defaults } from "../api";
import { Field, Notice } from "./Field";

type Key = keyof Defaults;
const FIELDS: { key: Key; label: string; unit: string; hint?: string }[] = [
  { key: "threshold_green_max_min", label: "Green up to", unit: "minutes", hint: "Time on site at or below this is green" },
  { key: "threshold_yellow_max_min", label: "Yellow up to", unit: "minutes", hint: "Above this is red" },
  { key: "poll_interval_s", label: "Poll each site every", unit: "seconds" },
  { key: "recent_window_min", label: "“Current time on site” window", unit: "minutes", hint: "Trucks that weighed out within this window" },
  { key: "open_ticket_cutoff_hours", label: "Treat open tickets as abandoned after", unit: "hours" },
  { key: "duration_ceiling_hours", label: "Exclude visits longer than", unit: "hours", hint: "Usually a ticket left open, not a real wait" },
  { key: "backfill_default_days", label: "Default history to backfill", unit: "days" },
  { key: "public_stale_after_min", label: "Public page: data is stale after", unit: "minutes" },
  { key: "public_min_trucks", label: "Public page: minimum trucks to show a number", unit: "trucks" },
];

/** Global defaults. Every site can override these later in the admin panel. */
export function DefaultsForm({ onSaved, submitLabel, fields }: { onSaved: () => void; submitLabel?: string; fields?: Key[] }) {
  const [d, setD] = useState<Defaults | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getDefaults().then(setD).catch(() => setMessage("Could not load the defaults."));
  }, []);

  if (!d) return message ? <Notice kind="error">{message}</Notice> : <p className="muted">Loading…</p>;

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    setSaving(true);
    setErrors({});
    setMessage(null);
    try {
      await api.putDefaults(d);
      onSaved();
    } catch (e) {
      if (e instanceof ApiError) {
        setErrors(e.fields);
        setMessage(e.message);
      } else setMessage("Could not save.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="form" onSubmit={submit} noValidate>
      <div className="grid">
        {FIELDS.filter((f) => !fields || fields.includes(f.key)).map((f) => (
          <Field key={f.key} label={f.label} hint={f.hint} error={errors[f.key]}>
            <span className="with-unit">
              <input
                inputMode="numeric"
                value={String(d[f.key])}
                onChange={(e) => setD({ ...d, [f.key]: Number(e.target.value.replace(/\D/g, "")) || 0 })}
              />
              <span className="unit">{f.unit}</span>
            </span>
          </Field>
        ))}
      </div>
      {message ? <Notice kind="error">{message}</Notice> : null}
      <div className="actions">
        <button type="submit" disabled={saving}>{saving ? "Saving…" : submitLabel ?? "Save defaults"}</button>
      </div>
    </form>
  );
}
