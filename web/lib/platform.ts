import type { Notebook, ParameterDefinition, QueryResult } from "./api";
export type WorkflowTask = {
  key: string;
  notebook_id?: string;
  release_id?: string;
  path?: string;
  depends_on: string[];
  cluster_id?: string | null;
  parameters: Record<string, unknown>;
  timeout_seconds: number;
  max_retries: number;
  retry_safe?: boolean;
  retry_delay_seconds: number;
};
export type Workflow = {
  id: string;
  name: string;
  description?: string;
  notification_ids?: string[];
  project_id: string;
  parameter_definitions: ParameterDefinition[];
  parameters: Record<string, unknown>;
  tasks: WorkflowTask[];
  cron?: string;
  timezone?: string;
  enabled?: boolean;
};
export type WorkflowRun = {
  id: string;
  workflow_id: string;
  name: string;
  project_id: string;
  status: string;
  cancel_requested?: boolean;
  parameters: Record<string, unknown>;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  error?: string;
  progress?: { completed: number; total: number };
  tasks: {
    key: string;
    status: string;
    depends_on: string[];
    run_id?: string;
    error?: string;
    attempts: {
      number: number;
      run_id: string;
      status: string;
      error?: string;
    }[];
  }[];
};
export type SavedQuery = {
  id: string;
  name: string;
  project_id: string;
  engine: "spark" | "sqlserver";
  sql: string;
  connection_id?: string;
  cluster_id?: string;
  params?: unknown[];
  description?: string;
};
export type QualityReport = {
  name?: string;
  type?: string;
  column?: string;
  passed?: boolean;
  status?: string;
  message?: string;
  [key: string]: unknown;
};
export type DataExecution = {
  id: string;
  run_id?: string;
  project_id: string;
  status: string;
  operation?: string;
  engine?: string;
  sql?: string;
  created_at?: string;
  error?: string;
  result?: QueryResult;
  quality_reports?: QualityReport[];
  lineage?: unknown[];
};
export type Environment = {
  id: string;
  name: string;
  description?: string;
  wheels?: {
    name?: string;
    version?: string;
    filename?: string;
    sha256?: string;
  }[];
  created_at?: string;
  [key: string]: unknown;
};
export type Release = {
  id: string;
  name: string;
  repository_id: string;
  commit: string;
  environment_id?: string;
  created_at?: string;
  notebooks?: { path: string; name?: string; notebook?: Notebook }[];
  [key: string]: unknown;
};
