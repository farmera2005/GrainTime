// Thin client for the internal api. Every state-changing request carries the
// X-GrainTime header the api requires (CSRF guard); cookies are same-origin.

export type User = {
  id: number;
  username: string;
  display_name: string;
  role: "viewer" | "admin";
  auth_source: string;
};

export type SetupStatus = {
  setup_complete: boolean;
  admin_exists: boolean;
  admin_window_open: boolean;
  admin_window_closes_at: string | null;
  user: User | null;
  steps?: { admin: boolean; defaults: boolean; site: boolean; discovery: boolean };
};

export type Defaults = {
  poll_interval_s: number;
  threshold_green_max_min: number;
  threshold_yellow_max_min: number;
  recent_window_min: number;
  open_ticket_cutoff_hours: number;
  duration_ceiling_hours: number;
  backfill_default_days: number;
  public_stale_after_min: number;
  public_min_trucks: number;
};

export type Encrypt = "yes" | "no" | "strict";
export type AuthMethod = "sql" | "windows";

export type ConnectionFields = {
  host: string;
  port: number | null;
  instance_name?: string | null;
  auth_method?: AuthMethod;
  domain?: string | null;
  database: string;
  username: string;
  password?: string;
  encrypt: Encrypt;
  trust_server_certificate: boolean;
};

export type Site = {
  id: number;
  name: string;
  code: string;
  address: string | null;
  map_url: string | null;
  host: string;
  port: number;
  instance_name: string | null;
  database: string;
  auth_method: AuthMethod;
  domain: string | null;
  username: string;
  has_password: boolean;
  encrypt: Encrypt;
  trust_server_certificate: boolean;
  polling_enabled: boolean;
  show_on_dashboard: boolean;
  show_on_public: boolean;
  archived: boolean;
  mapping_profile_id: number | null;
  poll_interval_s: number | null;
  hours: Hours | null;
  last_success_at: string | null;
  last_error: JobError | null;
  stale: boolean;
  trucks_today: number;
};

export type SiteInput = Omit<ConnectionFields, "password"> & {
  name: string;
  code: string;
  address?: string | null;
  map_url?: string | null;
  password?: string;
};

export type DayHours = { open: string; close: string };
export type Hours = {
  weekly: (DayHours | null)[];
  overrides: { label: string; date_from: string; date_to: string; weekly: (DayHours | null)[] }[];
};

export type SiteSwitches = {
  polling_enabled: boolean;
  show_on_dashboard: boolean;
  show_on_public: boolean;
  mapping_profile_id: number | null;
  poll_interval_s: number | null;
  hours: Hours | null;
};

export type Lookup = {
  table: string;
  key_column: string;
  code_column: string;
  description_column?: string | null;
  direction_column?: string | null;
};

export type WeighSteps = {
  table: string;
  ticket_fk_column: string;
  time_column: string;
  weight_type_column: string;
  status_column?: string | null;
  status_done_value?: string | null;
};

export type TicketStatus = "open" | "completed" | "voided";

export type ProfileConfig = {
  ticket_table: string;
  id_column: string;
  high_water_column: string;
  ticket_number_column?: string | null;
  status_column: string;
  status_map: Record<string, TicketStatus>;
  void_column?: string | null;
  created_column: string;
  completed_column?: string | null;
  type_column?: string | null;
  product_column?: string | null;
  parent_column?: string | null;
  inbound_column?: string | null;
  outbound_column?: string | null;
  weigh_steps?: WeighSteps | null;
  type_lookup?: Lookup | null;
  direction_map: Record<string, "received" | "shipped">;
  included_type_codes: string[];
  product_lookup?: Lookup | null;
  filter_column?: string | null;
  filter_value?: string | null;
  lookback_hours: number;
  completed_lookback_hours: number;
  nightly_recheck_days: number;
};

export type MappingProfile = {
  id: number;
  name: string;
  description: string | null;
  config: ProfileConfig;
  sites: { id: number; name: string; polling_enabled: boolean }[];
  updated_at: string;
};

export type NormalizedTicket = {
  source_ticket_id: string;
  ticket_number: string | null;
  raw_status: string;
  status: string;
  direction: string;
  transaction_type: string | null;
  commodity: string | null;
  inbound_at: string | null;
  outbound_at: string | null;
  duration_s: number | null;
  single_weigh: boolean;
  source_created_at: string | null;
  included: boolean;
  note: string | null;
};

export type CollectionStatus = {
  polling_enabled: boolean;
  interval_s: number;
  stale: boolean;
  state: null | {
    high_water_mark: string | null;
    last_poll_at: string | null;
    last_success_at: string | null;
    next_poll_at: string | null;
    consecutive_failures: number;
    last_error: (JobError & { at?: string }) | null;
    rows_last_poll: number;
    rows_total: number;
    last_recheck_at: string | null;
  };
  tickets: { total: number; today: number; on_site_now: number };
  recent: Pick<NormalizedTicket, "source_ticket_id" | "ticket_number" | "status" | "commodity" |
    "inbound_at" | "outbound_at" | "duration_s" | "single_weigh">[];
  last_backfill: Job | null;
};

export type SqlInstance = { server: string | null; instance: string; version: string | null; tcp_port: number | null };

export type JobError = {
  code: string;
  cause: string;
  fix: string;
  docs?: string;
  detail?: string;
  instances?: SqlInstance[];
  checks?: { check: string; result: string; ok: boolean }[];
};

export type Job = {
  id: number;
  kind: "test_connection" | "discovery" | "preview" | "backfill";
  site_id: number | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  progress: { step: string; done: number; total: number; tickets_stored?: number } | null;
  error: JobError | null;
  result: Record<string, any> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type Level = "good" | "warning" | "critical" | null;

export type StatsOptions = {
  sites: { id: number; name: string; code: string }[];
  commodities: string[];
  first_date: string | null;
  today: string;
  thresholds: { green_max_min: number; yellow_max_min: number };
  ceiling_hours: number;
};

export type SiteOverview = {
  id: number; name: string; code: string;
  current: { minutes: number | null; trucks: number; basis: string; level: Level };
  on_site_now: number;
  oldest_on_site_min: number | null;
  trucks_today: number;
  completed_today: number;
  median_today_min: number | null;
  p90_today_min: number | null;
  typical_this_hour_min: number | null;
  last_update: string | null;
  stale: boolean;
  polling: boolean;
};

export type Summary = {
  completed: number; median_min: number | null; p90_min: number | null; mean_min: number | null; trucks: number;
  excluded: { voided: number; single_weigh: number; over_ceiling: number; unknown_status: number };
  ceiling_hours: number;
};
export type ByHour = { hour: number; trucks: number; completed: number; median_min: number | null; p90_min: number | null };
export type TrendDay = { date: string; completed: number; median_min: number | null; p90_min: number | null };
export type HeatCell = { dow: number; hour: number; completed: number; median_min: number | null };
export type CompareRow = { site_id: number; name: string; code: string; completed: number; median_min: number | null; p90_min: number | null };
export type DistBin = { from_min: number; to_min: number; count: number };

export type WidgetType = "kpi" | "sites_table" | "by_hour" | "trend" | "heatmap" | "compare" | "distribution" | "exclusions";
export type KpiMetric = "current" | "on_site_now" | "trucks_today" | "median" | "p90" | "completed";
export type Widget = {
  id: string;
  type: WidgetType;
  title: string | null;
  size: "S" | "M" | "L";
  settings: { site_id: number | null; metric: KpiMetric | null; show_p90: boolean };
};
export type RangeKey = "today" | "yesterday" | "7d" | "30d" | "90d" | "season" | "ytd" | "custom";
export type DashFilters = {
  range: RangeKey;
  date_from: string | null;
  date_to: string | null;
  site_ids: number[];
  direction: "received" | "shipped";
  commodity: string | null;
};
export type Dashboard = { id: number; name: string; position: number; filters: DashFilters; widgets: Widget[]; updated_at: string };
export type StatsQuery = { sites?: number[]; from?: string; to?: string; direction: string; commodity?: string | null };

export function statsQuery(q: StatsQuery, format?: "csv"): string {
  const p = new URLSearchParams();
  if (q.sites && q.sites.length) p.set("sites", q.sites.join(","));
  if (q.from) p.set("from", q.from);
  if (q.to) p.set("to", q.to);
  p.set("direction", q.direction);
  if (q.commodity) p.set("commodity", q.commodity);
  if (format) p.set("format", format);
  return p.toString();
}

export class ApiError extends Error {
  status: number;
  fields: Record<string, string>;
  constructor(status: number, message: string, fields: Record<string, string> = {}) {
    super(message);
    this.status = status;
    this.fields = fields;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-GrainTime"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) {
    const detail = data?.detail;
    if (Array.isArray(detail)) {
      // FastAPI/pydantic validation errors -> per-field messages
      const fields: Record<string, string> = {};
      for (const d of detail) {
        const loc = (d.loc || []).filter((x: unknown) => x !== "body");
        const key = loc.length ? String(loc[loc.length - 1]) : "_";
        fields[key] = String(d.msg || "Invalid value").replace(/^Value error, /, "");
      }
      throw new ApiError(res.status, Object.values(fields)[0] || "Invalid input", fields);
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : `Request failed (${res.status})`);
  }
  return data as T;
}

function safeJson(text: string): any {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export const api = {
  setupStatus: () => request<SetupStatus>("GET", "/api/setup/status"),
  createAdmin: (b: { username: string; display_name: string; password: string }) =>
    request<User>("POST", "/api/setup/admin", b),
  completeSetup: () => request<{ setup_complete: boolean }>("POST", "/api/setup/complete"),
  login: (username: string, password: string) =>
    request<User>("POST", "/api/auth/login", { username, password }),
  logout: () => request<{ ok: boolean }>("POST", "/api/auth/logout"),
  getDefaults: () => request<Defaults>("GET", "/api/settings/defaults"),
  putDefaults: (d: Defaults) => request<Defaults>("PUT", "/api/settings/defaults", d),
  listSites: () => request<Site[]>("GET", "/api/admin/sites"),
  getSite: (id: number) => request<Site>("GET", `/api/admin/sites/${id}`),
  createSite: (s: SiteInput) => request<Site>("POST", "/api/admin/sites", s),
  updateSite: (id: number, s: Partial<SiteInput & SiteSwitches>) => request<Site>("PATCH", `/api/admin/sites/${id}`, s),
  archiveSite: (id: number) => request<Site>("POST", `/api/admin/sites/${id}/archive`),
  restoreSite: (id: number) => request<Site>("POST", `/api/admin/sites/${id}/restore`),
  deleteSite: (id: number, confirmName: string) =>
    request<{ deleted: boolean }>("DELETE", `/api/admin/sites/${id}`, { confirm_name: confirmName }),
  testConnection: (b: { site_id?: number; connection?: ConnectionFields }) =>
    request<Job>("POST", "/api/admin/jobs/test-connection", b),
  startDiscovery: (siteId: number, b: { extra_tables: string[]; include_samples: boolean; mask: boolean }) =>
    request<Job>("POST", `/api/admin/sites/${siteId}/discovery`, b),
  siteJobs: (siteId: number, kind?: string) =>
    request<Job[]>("GET", `/api/admin/sites/${siteId}/jobs${kind ? `?kind=${kind}` : ""}`),
  getJob: (id: number) => request<Job>("GET", `/api/admin/jobs/${id}`),
  cancelJob: (id: number) => request<Job>("POST", `/api/admin/jobs/${id}/cancel`),
  listProfiles: () => request<MappingProfile[]>("GET", "/api/admin/mapping-profiles"),
  getProfile: (id: number) => request<MappingProfile>("GET", `/api/admin/mapping-profiles/${id}`),
  profileTemplate: () => request<{ config: ProfileConfig }>("GET", "/api/admin/mapping-profiles/template"),
  createProfile: (b: { name: string; description: string | null; config: ProfileConfig }) =>
    request<MappingProfile>("POST", "/api/admin/mapping-profiles", b),
  updateProfile: (id: number, b: { name: string; description: string | null; config: ProfileConfig; confirm_in_use?: boolean }) =>
    request<MappingProfile>("PUT", `/api/admin/mapping-profiles/${id}`, b),
  cloneProfile: (id: number) => request<MappingProfile>("POST", `/api/admin/mapping-profiles/${id}/clone`, {}),
  deleteProfile: (id: number) => request<{ deleted: boolean }>("DELETE", `/api/admin/mapping-profiles/${id}`),
  loginScript: async (id: number, database: string, login: string, windows: boolean) => {
    const q = new URLSearchParams({ database, login, windows: String(windows) });
    const res = await fetch(`/api/admin/mapping-profiles/${id}/login-script?${q}`, { credentials: "same-origin" });
    const text = await res.text();
    if (!res.ok) throw new ApiError(res.status, safeJson(text)?.detail || "Could not create the script");
    return text;
  },
  startPreview: (siteId: number, b: { profile_id?: number; limit?: number }) =>
    request<Job>("POST", `/api/admin/sites/${siteId}/preview`, b),
  startBackfill: (siteId: number, b: { date_from: string; date_to: string }) =>
    request<Job>("POST", `/api/admin/sites/${siteId}/backfill`, b),
  collection: (siteId: number) => request<CollectionStatus>("GET", `/api/admin/sites/${siteId}/collection`),
  reportJson: (id: number) => request<Record<string, any>>("GET", `/api/admin/jobs/${id}/report.json`),
  statsOptions: () => request<StatsOptions>("GET", "/api/stats/options"),
  stats: <T,>(kind: string, q: StatsQuery) => request<T>("GET", `/api/stats/${kind}?${statsQuery(q)}`),
  listDashboards: () => request<Dashboard[]>("GET", "/api/dashboards"),
  createDashboard: (b: { name: string; filters?: DashFilters; widgets?: Widget[] }) => request<Dashboard>("POST", "/api/dashboards", b),
  saveDashboard: (id: number, b: { name: string; filters: DashFilters; widgets: Widget[] }) => request<Dashboard>("PUT", `/api/dashboards/${id}`, b),
  resetDashboard: (id: number) => request<Dashboard>("POST", `/api/dashboards/${id}/reset`),
  deleteDashboard: (id: number) => request<{ deleted: boolean }>("DELETE", `/api/dashboards/${id}`),
};
