import type { QualityReport } from "./platform";
export type User = {
  id: string;
  username: string;
  role: string;
  is_service_account?: boolean;
};
export type Cell = {
  id: string;
  language: "python" | "sql" | "markdown";
  source: string;
};
export type ParameterDefinition = {
  name: string;
  type: "string" | "integer" | "number" | "boolean" | "date" | "json";
  default?: unknown;
  required?: boolean;
  choices?: unknown[];
};
export type Project = {
  id: string;
  name: string;
  description?: string;
  role?: string;
  connection_ids?: string[];
  cluster_ids?: string[];
  created_at?: string;
};
export type Notebook = {
  id: string;
  name: string;
  description?: string;
  language?: string;
  cells: Cell[];
  created_at?: string;
  updated_at?: string;
  owner?: string;
  repository_id?: string;
  path?: string;
  folder_id?: string | null;
  folder_path?: string;
  cluster_id?: string | null;
  project_id?: string;
  connection_ids?: string[];
  parameter_definitions?: ParameterDefinition[];
};
export type NotebookSummary = Omit<Notebook, "cells"> & { cells?: Cell[] };
export type Folder = {
  id: string;
  name: string;
  parent_id: string | null;
  path?: string;
  created_at?: string;
  updated_at?: string;
};
export type Page<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
};
export type WorkspacePage = Page<NotebookSummary> & {
  folders: Folder[];
  breadcrumbs: Folder[];
};
export type BasicSchedule = {
  frequency: "hourly" | "daily" | "weekly";
  interval: number;
  minute: number;
  time: string;
  days_of_week: number[];
};
export type Cluster = {
  id: string;
  name: string;
  url: string;
  verify_tls: boolean;
  allow_insecure: boolean;
  built_in: boolean;
  has_token: boolean;
  last_test_at?: string;
  last_status?: Compute;
  created_at?: string;
};
export type Connection = {
  id: string;
  name: string;
  type: "sqlserver" | "s3";
  project_ids?: string[];
  config: Record<string, unknown>;
  has_secrets?: boolean;
  secret_fields?: string[];
  created_at?: string;
  updated_at?: string;
  built_in?: boolean;
};
export type QueryResult = {
  columns: string[];
  rows: unknown[][];
  truncated?: boolean;
  row_count?: number;
  offset?: number;
  next_offset?: number | null;
};
export type NotebookHistory = {
  notebook_id: string;
  notebook_name: string;
  folder_path?: string;
  total_runs: number;
  succeeded: number;
  failed: number;
  last_run_at?: string;
  last_status?: string;
  deleted?: boolean;
};
export type CellOutput = {
  cell_id: string;
  status?: string;
  stdout?: string;
  stderr?: string;
  error?: string;
  result?: unknown;
  outputs?: unknown[];
  duration_ms?: number;
};
export type Run = {
  session_id?: string;
  queued_reason?: string;
  resource_options?: { mode?: string; memory_mb?: number; priority?: number };
  project_id?: string;
  id: string;
  notebook_id: string;
  notebook_name?: string;
  status: string;
  cancel_requested?: boolean;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  ended_at?: string;
  outputs?: CellOutput[];
  error?: string;
  trigger?: string;
  job_id?: string;
  cluster_id?: string;
  cluster_name?: string;
  parameters?: Record<string, unknown>;
  environment_id?: string;
  release_id?: string;
  provenance?: {
    commit?: string;
    release_id?: string;
    path?: string;
    source_sha256?: string;
    rerun_of?: string;
  };
  source_commit?: string;
  source_hash?: string;
  progress?: { cell_id?: string; cell_index?: number; total_cells?: number };
  logs?: { at?: string; cell_id?: string; stream?: string; text: string }[];
  peak_memory_bytes?: number;
  error_class?: string;
  quality_reports?: QualityReport[];
  lineage?: unknown[];
};
export type Job = {
  resource_options?: {
    mode?: "interactive" | "job";
    memory_mb?: number;
    priority?: number;
  };
  catchup_policy?: "skip" | "latest" | "all";
  catchup_window_hours?: number;
  catchup_max_runs?: number;
  schedule_date_parameter?: string | null;
  project_id?: string;
  workflow_id?: string;
  workflow_name?: string;
  parameters?: Record<string, unknown>;
  environment_id?: string | null;
  notification_ids?: string[];
  id: string;
  name: string;
  notebook_id: string;
  notebook_name?: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  next_run_at?: string;
  last_run_at?: string;
  schedule_mode?: "basic" | "advanced";
  basic_schedule?: BasicSchedule;
  cluster_id?: string | null;
};
export type Repository = {
  id: string;
  name: string;
  url: string;
  branch: string;
  commit?: string;
  last_sync?: string;
  updated_at?: string;
  notebooks?: string[];
};
export type Package = {
  id?: string;
  name: string;
  filename?: string;
  version?: string;
  installed_at?: string;
  size?: number;
  status?: string;
};
export type Bucket = {
  name: string;
  objects?: number;
  object_count?: number;
  size?: number;
  bytes?: number;
  created_at?: string;
};
export type StorageObject = {
  key: string;
  size: number;
  modified_at?: string;
  updated_at?: string;
};
export type Compute = {
  status?: string;
  hostname?: string;
  cpu_count?: number;
  cpu_percent?: number;
  memory_total?: number;
  memory_used?: number;
  memory_percent?: number;
  disk_total?: number;
  disk_used?: number;
  disk_percent?: number;
  spark_version?: string;
  python_version?: string;
  active_runs?: number;
  queued_runs?: number;
  uptime_seconds?: number;
  master?: string;
  idle_timeout_seconds?: number;
  [key: string]: unknown;
};
export type Overview = {
  notebooks: NotebookSummary[];
  runs: Run[];
  jobs: Job[];
  repositories: Repository[];
  packages: Package[];
  buckets: Bucket[];
  compute: Compute;
  metrics?: {
    notebooks: number;
    runs: number;
    jobs: number;
    active_jobs: number;
    succeeded: number;
    failed: number;
    running: number;
    queued: number;
  };
  folders?: Folder[];
};

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public details?: unknown,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      credentials: "same-origin",
      headers:
        init?.body && !(init.body instanceof FormData)
          ? { "Content-Type": "application/json", ...init.headers }
          : init?.headers,
    });
  } catch {
    throw new ApiError(
      "The runtime is unavailable. Check that the OpenBricks server is running.",
      503,
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail ?? body?.error;
    throw new ApiError(
      typeof detail === "string"
        ? detail
        : typeof detail?.message === "string"
          ? detail.message
          : Array.isArray(detail)
            ? detail.map((item: { msg?: string }) => item.msg).join("; ")
            : response.status >= 500
              ? "The runtime is unavailable. Check that the OpenBricks server is running."
              : `Request failed (${response.status}).`,
      response.status,
      detail,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export function json(method: string, body?: unknown): RequestInit {
  return {
    method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}
export function newCellId(): string {
  const values = globalThis.crypto.getRandomValues(new Uint32Array(4));
  return `cell-${Array.from(values, (value) => value.toString(16).padStart(8, "0")).join("")}`;
}
export function bytes(value?: number): string {
  if (value == null) return "—";
  if (value < 1024) return `${value} B`;
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** index).toFixed(index === 1 ? 0 : 1)} ${["B", "KB", "MB", "GB", "TB"][index]}`;
}
export function date(value?: string): string {
  return value
    ? new Date(value).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
    : "—";
}
export function relative(value?: string): string {
  if (!value) return "Just now";
  const minutes = Math.max(
    0,
    Math.floor((Date.now() - new Date(value).getTime()) / 60000),
  );
  return minutes < 1
    ? "Just now"
    : minutes < 60
      ? `${minutes}m ago`
      : minutes < 1440
        ? `${Math.floor(minutes / 60)}h ago`
        : `${Math.floor(minutes / 1440)}d ago`;
}
