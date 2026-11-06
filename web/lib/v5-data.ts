import type { ParameterDefinition, QueryResult } from "./api";

export type DashboardTile = {
  id: string;
  title: string;
  query_id: string;
  chart: "table" | "bar" | "line" | "number";
  x?: string | null;
  y?: string | null;
  width: number;
  height: number;
  parameter_bindings: Record<string, string>;
};
export type Dashboard = {
  id: string;
  project_id: string;
  name: string;
  description: string;
  parameters: ParameterDefinition[];
  tiles: DashboardTile[];
  generation: string;
  cron?: string | null;
  timezone: string;
  enabled: boolean;
  cache_ttl_seconds: number;
  run_as_user_id?: string;
  last_success_at?: string;
  last_schedule_error?: string | null;
  last_refresh?: DashboardRefresh | null;
  updated_at?: string;
};
export type DashboardSummary = Omit<Dashboard, "tiles" | "parameters">;
export type DashboardRefresh = {
  id: string;
  dashboard_id: string;
  project_id: string;
  status: string;
  parameters: Record<string, unknown>;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  error?: string;
  source?: string;
  tiles: {
    id: string;
    query_id: string;
    status: string;
    query_hash: string;
    run_id?: string;
    execution_id?: string;
    error?: string;
  }[];
};
export type DashboardSnapshot = {
  id: string;
  dashboard_id: string;
  project_id: string;
  parameters: Record<string, unknown>;
  refreshed_at: string;
  stale: boolean;
  age_seconds: number;
  refresh_id: string;
  latest_refresh?: DashboardRefresh | null;
  tiles: (DashboardTile & {
    result: QueryResult;
    run_id: string;
    execution_id: string;
    query_hash: string;
    query_updated_at?: string;
  })[];
};
export type QueryOption = {
  id: string;
  name: string;
  engine: string;
};
export type Asset = {
  id: string;
  project_id: string;
  dataset: string;
  description: string;
  owner_id?: string | null;
  owner?: { id: string; username: string } | null;
  freshness_seconds?: number | null;
  health: "healthy" | "warning" | "failed" | "unknown";
  quality?: {
    passed?: boolean | null;
    row_count?: number | null;
    run_id: string;
    run_status: string;
    observed_at: string;
    live: false;
    checks: {
      name?: string;
      type?: string;
      column?: string;
      passed?: boolean;
      failed_rows?: number;
      actual?: unknown;
    }[];
  } | null;
  freshness: {
    status: "fresh" | "stale" | "unknown";
    age_seconds?: number | null;
    threshold_seconds?: number | null;
    observed_at?: string | null;
    basis: string;
    live: false;
  };
  last_read_at?: string | null;
  last_write_at?: string | null;
  last_writer_run_id?: string | null;
  last_writer_status?: string | null;
};
export type AssetGraph = {
  nodes: Asset[];
  edges: {
    source: string;
    target: string;
    run_id?: string;
    run_status?: string | null;
    last_observed_at: string;
    inferred: boolean;
    evidence: string;
  }[];
  truncated: boolean;
  depth: number;
  direction: string;
  evidence: {
    quality_run_window: number;
    quality_report_limit: number;
    edge_limit: number;
    live_queries: boolean;
    meaning: string;
  };
};
export type AssetQuality = NonNullable<Asset["quality"]> & { id: string };
