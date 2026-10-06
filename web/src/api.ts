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

export type ConnectionFields = {
  host: string;
  port: number;
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
  database: string;
  username: string;
  has_password: boolean;
  encrypt: Encrypt;
  trust_server_certificate: boolean;
  polling_enabled: boolean;
  show_on_dashboard: boolean;
  show_on_public: boolean;
  archived: boolean;
};

export type SiteInput = Omit<ConnectionFields, "password"> & {
  name: string;
  code: string;
  address?: string | null;
  map_url?: string | null;
  password?: string;
};

export type JobError = { code: string; cause: string; fix: string; docs?: string; detail?: string };

export type Job = {
  id: number;
  kind: "test_connection" | "discovery";
  site_id: number | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  progress: { step: string; done: number; total: number } | null;
  error: JobError | null;
  result: Record<string, any> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

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
  updateSite: (id: number, s: Partial<SiteInput>) => request<Site>("PATCH", `/api/admin/sites/${id}`, s),
  testConnection: (b: { site_id?: number; connection?: ConnectionFields }) =>
    request<Job>("POST", "/api/admin/jobs/test-connection", b),
  startDiscovery: (siteId: number, b: { extra_tables: string[]; include_samples: boolean; mask: boolean }) =>
    request<Job>("POST", `/api/admin/sites/${siteId}/discovery`, b),
  siteJobs: (siteId: number, kind?: string) =>
    request<Job[]>("GET", `/api/admin/sites/${siteId}/jobs${kind ? `?kind=${kind}` : ""}`),
  getJob: (id: number) => request<Job>("GET", `/api/admin/jobs/${id}`),
  cancelJob: (id: number) => request<Job>("POST", `/api/admin/jobs/${id}/cancel`),
  reportJson: (id: number) => request<Record<string, any>>("GET", `/api/admin/jobs/${id}/report.json`),
};
