// Times are stored in UTC and always shown in the sites' time zone.
const TZ = "America/New_York";

const dt = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
});
const tm = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit", second: "2-digit" });

export function fmtDateTime(iso: string | null | undefined): string {
  return iso ? dt.format(new Date(iso)) : "";
}

export function fmtTime(iso: string | null | undefined): string {
  return iso ? tm.format(new Date(iso)) : "";
}

export function fmtDuration(s: number | null | undefined): string {
  if (s == null) return "";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

export function localDateISO(d: Date): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
  return parts; // YYYY-MM-DD
}
