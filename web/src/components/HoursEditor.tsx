// Operating hours for a site: a weekly schedule plus date-range overrides
// (e.g. harvest hours). The public page uses them to say open or closed.
import { useState } from "react";
import { api, DayHours, Hours, Site } from "../api";
import { Notice } from "./Field";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const STANDARD: (DayHours | null)[] = [...Array(5).fill({ open: "07:00", close: "17:00" }), { open: "08:00", close: "12:00" }, null];

function Week(props: { value: (DayHours | null)[]; onChange: (v: (DayHours | null)[]) => void; idPrefix: string }) {
  const set = (i: number, d: DayHours | null) => props.onChange(props.value.map((x, j) => (j === i ? d : x)));
  return (
    <table className="hours-table">
      <tbody>
        {DAYS.map((name, i) => {
          const d = props.value[i];
          return (
            <tr key={name}>
              <th scope="row">{name}</th>
              <td>
                <label className="check inline">
                  <input type="checkbox" checked={!d} onChange={(e) => set(i, e.target.checked ? null : { open: "07:00", close: "17:00" })} /> Closed
                </label>
              </td>
              <td>
                {d ? (
                  <span className="hours-times">
                    <input type="time" aria-label={`${name} opens`} id={`${props.idPrefix}-${i}-open`} value={d.open} onChange={(e) => set(i, { ...d, open: e.target.value })} />
                    <span className="muted">to</span>
                    <input type="time" aria-label={`${name} closes`} value={d.close} onChange={(e) => set(i, { ...d, close: e.target.value })} />
                  </span>
                ) : null}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function HoursEditor(props: { site: Site; onChange: (s: Site) => void }) {
  const [hours, setHours] = useState<Hours | null>(props.site.hours);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const save = async (h: Hours | null) => {
    setBusy(true);
    setNote(null);
    try {
      const s = await api.updateSite(props.site.id, { hours: h });
      props.onChange(s);
      setHours(s.hours);
      setNote({ kind: "ok", text: h ? "Hours saved." : "Hours cleared. The public page won't say whether this site is open." });
    } catch (e) {
      setNote({ kind: "error", text: e instanceof Error ? e.message : "Could not save the hours." });
    } finally {
      setBusy(false);
    }
  };

  if (!hours) {
    return (
      <>
        <p className="muted small">No hours set. The public page shows wait times but not whether the site is open.</p>
        <button className="secondary" onClick={() => setHours({ weekly: STANDARD, overrides: [] })}>Set operating hours</button>
        {note ? <Notice kind={note.kind}>{note.text}</Notice> : null}
      </>
    );
  }
  const year = new Date().getFullYear();
  return (
    <>
      <p className="muted small">Eastern time. Closing must be after opening on the same day.</p>
      <h4>Regular hours</h4>
      <Week idPrefix="weekly" value={hours.weekly} onChange={(weekly) => setHours({ ...hours, weekly })} />

      <h4>Date-range overrides</h4>
      <p className="muted small">For harvest or holiday hours. Inside its dates an override replaces the regular hours; if several overlap, the last one wins.</p>
      {hours.overrides.map((o, k) => {
        const setO = (patch: Partial<typeof o>) => setHours({ ...hours, overrides: hours.overrides.map((x, j) => (j === k ? { ...x, ...patch } : x)) });
        return (
          <fieldset key={k} className="override">
            <div className="override-head">
              <label>Name <input value={o.label} maxLength={60} placeholder="Harvest" onChange={(e) => setO({ label: e.target.value })} /></label>
              <label>From <input type="date" value={o.date_from} onChange={(e) => setO({ date_from: e.target.value })} /></label>
              <label>To <input type="date" value={o.date_to} onChange={(e) => setO({ date_to: e.target.value })} /></label>
              <button className="link danger-text" onClick={() => setHours({ ...hours, overrides: hours.overrides.filter((_, j) => j !== k) })}>Remove</button>
            </div>
            <Week idPrefix={`ov${k}`} value={o.weekly} onChange={(weekly) => setO({ weekly })} />
          </fieldset>
        );
      })}
      <button className="secondary small-button" disabled={hours.overrides.length >= 20}
        onClick={() => setHours({ ...hours, overrides: [...hours.overrides, { label: "Harvest", date_from: `${year}-09-15`, date_to: `${year}-11-30`, weekly: Array(7).fill({ open: "06:00", close: "20:00" }) }] })}>
        Add override
      </button>

      <div className="actions">
        <button className="secondary" disabled={busy} onClick={() => save(null)}>Clear hours</button>
        <button disabled={busy} onClick={() => save(hours)}>Save hours</button>
      </div>
      {note ? <Notice kind={note.kind}>{note.text}</Notice> : null}
    </>
  );
}
