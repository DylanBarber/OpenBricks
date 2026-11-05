"use client";

import { useEffect, useState, type FormEvent } from "react";
import {
  CalendarRange,
  Clock3,
  Plus,
  RefreshCw,
  Workflow as WorkflowIcon,
  Zap,
} from "lucide-react";
import {
  api,
  date,
  json,
  type Page,
  type ParameterDefinition,
} from "@/lib/api";
import {
  Empty,
  Field,
  Modal,
  Pagination,
  SectionHead,
  Spinner,
  Status,
} from "./ui";
import { NotebookPicker } from "./platform-controls";
import { ParameterValues } from "./parameter-fields";

type Kind = "notebook" | "workflow";
type Target = {
  id: string;
  name: string;
  parameter_definitions: ParameterDefinition[];
};
type Backfill = {
  id: string;
  name: string;
  target_name: string;
  status: string;
  start_date: string;
  end_date: string;
  date_parameter: string;
  max_concurrency: number;
  stop_on_failure: boolean;
  created_at: string;
  progress: { completed: number; total: number };
  snapshot_id?: string;
  environment_ids?: string[];
  error?: string;
  items?: {
    date: string;
    status: string;
    run_id: string;
    run_available?: boolean;
    error?: string;
  }[];
};
type Trigger = {
  id: string;
  name: string;
  project_id: string;
  notebook_id?: string | null;
  workflow_id?: string | null;
  type: "file_arrival" | "table_change" | "workflow_completion";
  parameters: Record<string, unknown>;
  event_parameter?: string | null;
  enabled: boolean;
  interval_seconds: number;
  debounce_seconds: number;
  include_existing: boolean;
  source: {
    path?: string | null;
    connection_id?: string | null;
    workflow_id?: string | null;
    states?: string[];
    pattern?: string;
  };
  last_checked_at?: string;
  next_check_at?: string;
  last_error?: string;
};
type Dispatch = {
  id: string;
  name: string;
  source_kind: string;
  status: string;
  observed_at: string;
  run_id: string;
  run_available?: boolean;
  error?: string;
};
type Cursor = {
  id: string;
  name?: string;
  policy?: string;
  cursor: string;
  last_checked_at?: string;
  skipped?: number;
  remaining?: number;
  last_error?: string;
};
type WorkflowRun = {
  id: string;
  name: string;
  status: string;
  error?: string;
  tasks: {
    key: string;
    status: string;
    run_id?: string;
    error?: string;
    attempts: unknown[];
  }[];
};
type Section = "backfills" | "triggers" | "runs" | "catchup";
type Props = {
  projectId: string;
  role?: string;
  canEdit?: boolean;
  canRun?: boolean;
  reportError?: (message: string) => void;
  onError?: (message: string) => void;
  onOpenWorkflowRun?: (id: string) => void;
  onRun?: (id: string) => void;
};
const terminal = (status: string) =>
  ["succeeded", "failed", "cancelled"].includes(status);
const errorText = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The operation could not be completed.";

function WorkflowPicker({
  projectId,
  value,
  onChange,
  label = "Workflow",
}: {
  projectId: string;
  value: string;
  onChange: (id: string) => void;
  label?: string;
}) {
  const [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0),
    [page, setPage] = useState<Page<Target> | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    setOffset(0);
    setSearch("");
    setPage(null);
  }, [projectId]);
  useEffect(() => {
    let disposed = false;
    const timer = setTimeout(
      () =>
        api<Page<Target>>(
          `/workflows/page?${new URLSearchParams({ project_id: projectId, search, limit: "20", offset: String(offset) })}`,
        )
          .then((result) => {
            if (!disposed) {
              setPage(result);
              setError("");
            }
          })
          .catch((error) => {
            if (!disposed) setError(errorText(error));
          }),
      search ? 200 : 0,
    );
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [projectId, search, offset]);
  return (
    <div className="notebook-picker">
      <Field label={`Find ${label.toLowerCase()}`}>
        <input
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setOffset(0);
          }}
          placeholder="Search project workflows…"
        />
      </Field>
      <Field label={label}>
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          required
        >
          <option value="">Choose workflow</option>
          {value && !page?.items.some((item) => item.id === value) ? (
            <option value={value}>Selected workflow</option>
          ) : null}
          {page?.items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </Field>
      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}
      {page ? <Pagination {...page} onPage={setOffset} /> : null}
    </div>
  );
}

function TargetPicker({
  projectId,
  kind,
  id,
  onKind,
  onId,
}: {
  projectId: string;
  kind: Kind;
  id: string;
  onKind: (kind: Kind) => void;
  onId: (id: string) => void;
}) {
  return (
    <>
      <Field label="Run">
        <select
          value={kind}
          onChange={(event) => onKind(event.target.value as Kind)}
        >
          <option value="notebook">A notebook</option>
          <option value="workflow">A workflow</option>
        </select>
      </Field>
      {kind === "notebook" ? (
        <NotebookPicker projectId={projectId} value={id} onChange={onId} />
      ) : (
        <WorkflowPicker projectId={projectId} value={id} onChange={onId} />
      )}
    </>
  );
}

export default function AutomationPanel(props: Props) {
  const { projectId } = props;
  const canEdit =
    props.canEdit ?? ["editor", "owner"].includes(props.role || "viewer");
  const canRun = props.canRun ?? (!!props.role && props.role !== "viewer");
  const report = props.onError || props.reportError || (() => {});
  const [section, setSection] = useState<Section>("backfills"),
    [offset, setOffset] = useState(0),
    [version, setVersion] = useState(0);
  const [page, setPage] = useState<Page<
      Backfill | Trigger | Dispatch | Cursor
    > | null>(null),
    [loading, setLoading] = useState(true),
    [loadError, setLoadError] = useState("");
  const [form, setForm] = useState<"backfill" | "trigger" | null>(null),
    [editing, setEditing] = useState<Trigger | null>(null),
    [formError, setFormError] = useState(""),
    [busy, setBusy] = useState(false);
  const [kind, setKind] = useState<Kind>("notebook"),
    [targetId, setTargetId] = useState(""),
    [target, setTarget] = useState<Target | null>(null),
    [name, setName] = useState("");
  const [parameters, setParameters] = useState<Record<string, unknown>>({}),
    [dateParameter, setDateParameter] = useState(""),
    [startDate, setStartDate] = useState(""),
    [endDate, setEndDate] = useState(""),
    [concurrency, setConcurrency] = useState(1),
    [stopOnFailure, setStopOnFailure] = useState(true);
  const [preview, setPreview] = useState<{
    count: number;
    dates: string[];
    target_name: string;
  } | null>(null);
  const [triggerType, setTriggerType] =
      useState<Trigger["type"]>("file_arrival"),
    [path, setPath] = useState("blob://"),
    [connectionId, setConnectionId] = useState(""),
    [upstream, setUpstream] = useState(""),
    [eventParameter, setEventParameter] = useState(""),
    [pattern, setPattern] = useState("*"),
    [interval, setIntervalSeconds] = useState(30),
    [debounce, setDebounce] = useState(10),
    [includeExisting, setIncludeExisting] = useState(false),
    [states, setStates] = useState<string[]>(["succeeded"]);
  const [connections, setConnections] = useState<
      { id: string; name: string }[]
    >([]),
    [detailId, setDetailId] = useState<string | null>(null),
    [detail, setDetail] = useState<Backfill | null>(null),
    [workflowId, setWorkflowId] = useState<string | null>(null),
    [workflowRun, setWorkflowRun] = useState<WorkflowRun | null>(null),
    [events, setEvents] = useState<{
      trigger: Trigger;
      page: Page<{
        id: string;
        status: string;
        created_at: string;
        event: unknown;
      }>;
    } | null>(null),
    [eventOffset, setEventOffset] = useState(0);

  useEffect(() => {
    setOffset(0);
    setPage(null);
    setForm(null);
    setEditing(null);
    setDetailId(null);
    setDetail(null);
    setWorkflowId(null);
    setWorkflowRun(null);
    setEvents(null);
    setEventOffset(0);
  }, [projectId]);
  useEffect(() => {
    setOffset(0);
    setPage(null);
  }, [section]);
  useEffect(() => {
    let disposed = false;
    const endpoint =
      section === "runs"
        ? "/automation/runs"
        : section === "catchup"
          ? "/schedules/catchup"
          : `/${section}`;
    const load = () =>
      api<Page<Backfill | Trigger | Dispatch | Cursor>>(
        `${endpoint}?${new URLSearchParams({ project_id: projectId, limit: "25", offset: String(offset) })}`,
      )
        .then((result) => {
          if (!disposed) {
            setPage(result);
            setLoadError("");
            setLoading(false);
          }
        })
        .catch((error) => {
          if (!disposed) {
            setLoadError(errorText(error));
            setLoading(false);
          }
        });
    setLoading(true);
    void load();
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 5000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [projectId, section, offset, version]);
  useEffect(() => {
    if (!form || !targetId) {
      setTarget(null);
      return;
    }
    let disposed = false;
    api<Target>(`/automation/targets/${kind}/${encodeURIComponent(targetId)}`)
      .then((result) => {
        if (disposed) return;
        setTarget(result);
        if (form === "backfill")
          setDateParameter((current) =>
            result.parameter_definitions.some(
              (item) =>
                item.name === current && ["date", "string"].includes(item.type),
            )
              ? current
              : result.parameter_definitions.find(
                  (item) => item.type === "date",
                )?.name ||
                result.parameter_definitions.find(
                  (item) => item.type === "string",
                )?.name ||
                "",
          );
      })
      .catch((error) => {
        if (!disposed) setFormError(errorText(error));
      });
    return () => {
      disposed = true;
    };
  }, [projectId, form, kind, targetId]);
  useEffect(() => {
    setPreview(null);
  }, [
    targetId,
    kind,
    dateParameter,
    startDate,
    endDate,
    parameters,
    concurrency,
    stopOnFailure,
  ]);
  useEffect(() => {
    if (form !== "trigger") return;
    let disposed = false;
    api<{ id: string; name: string; type: string }[]>(
      `/connections?project_id=${encodeURIComponent(projectId)}`,
    )
      .then((result) => {
        if (!disposed)
          setConnections(result.filter((item) => item.type === "s3"));
      })
      .catch((error) => {
        if (!disposed) setFormError(errorText(error));
      });
    return () => {
      disposed = true;
    };
  }, [form, projectId]);
  useEffect(() => {
    if (!detailId) return;
    let disposed = false;
    const load = () =>
      api<Backfill>(`/backfills/${detailId}`)
        .then((result) => {
          if (!disposed) {
            setDetail(result);
            if (terminal(result.status)) clearInterval(timer);
          }
        })
        .catch((error) => {
          if (!disposed) report(errorText(error));
        });
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 2000);
    void load();
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [detailId, projectId]);
  useEffect(() => {
    if (!workflowId) return;
    let disposed = false;
    const load = () =>
      api<WorkflowRun>(`/workflow-runs/${workflowId}`)
        .then((result) => {
          if (!disposed) {
            setWorkflowRun(result);
            if (terminal(result.status)) clearInterval(timer);
          }
        })
        .catch((error) => {
          if (!disposed) report(errorText(error));
        });
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 2000);
    void load();
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [workflowId, projectId]);
  useEffect(() => {
    if (!events) return;
    let disposed = false;
    api<
      Page<{ id: string; status: string; created_at: string; event: unknown }>
    >(`/triggers/${events.trigger.id}/events?limit=25&offset=${eventOffset}`)
      .then((result) => {
        if (!disposed)
          setEvents((current) =>
            current ? { ...current, page: result } : null,
          );
      })
      .catch((error) => {
        if (!disposed) report(errorText(error));
      });
    return () => {
      disposed = true;
    };
  }, [events?.trigger.id, eventOffset, projectId]);

  function openForm(type: "backfill" | "trigger", trigger?: Trigger) {
    setForm(type);
    setEditing(trigger || null);
    setFormError("");
    setBusy(false);
    setPreview(null);
    setTarget(null);
    setKind(trigger?.workflow_id ? "workflow" : "notebook");
    setTargetId(trigger?.workflow_id || trigger?.notebook_id || "");
    setName(
      trigger?.name ||
        (type === "backfill" ? "Date range backfill" : "New event trigger"),
    );
    setParameters(trigger?.parameters || {});
    setDateParameter("");
    const today = new Date().toISOString().slice(0, 10);
    setStartDate(today);
    setEndDate(today);
    setConcurrency(1);
    setStopOnFailure(true);
    setTriggerType(trigger?.type || "file_arrival");
    setPath(trigger?.source.path || "blob://");
    setConnectionId(trigger?.source.connection_id || "");
    setUpstream(trigger?.source.workflow_id || "");
    setPattern(trigger?.source.pattern || "*");
    setStates(trigger?.source.states || ["succeeded"]);
    setEventParameter(trigger?.event_parameter || "");
    setIntervalSeconds(trigger?.interval_seconds || 30);
    setDebounce(trigger?.debounce_seconds ?? 10);
    setIncludeExisting(trigger?.include_existing || false);
  }
  function targetKind(next: Kind) {
    setKind(next);
    setTargetId("");
    setTarget(null);
    setParameters({});
    setDateParameter("");
    setEventParameter("");
  }
  function targetChoice(next: string) {
    setTargetId(next);
    setTarget(null);
    setParameters({});
    setEventParameter("");
    setDateParameter("");
    setPreview(null);
  }
  function backfillBody() {
    return {
      name,
      project_id: projectId,
      [`${kind}_id`]: targetId,
      date_parameter: dateParameter,
      start_date: startDate,
      end_date: endDate,
      parameters,
      max_concurrency: concurrency,
      stop_on_failure: stopOnFailure,
    };
  }
  async function previewBackfill() {
    setBusy(true);
    setFormError("");
    try {
      setPreview(await api("/backfills/preview", json("POST", backfillBody())));
    } catch (error) {
      setFormError(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError("");
    try {
      if (form === "backfill") {
        if (!preview)
          throw new Error(
            "Preview the date range before creating the backfill.",
          );
        const result = await api<Backfill>(
          "/backfills",
          json("POST", backfillBody()),
        );
        setDetailId(result.id);
        setDetail(result);
        setSection("backfills");
      } else {
        const source =
          triggerType === "workflow_completion"
            ? { workflow_id: upstream, states }
            : {
                path,
                connection_id: path.startsWith("s3://") ? connectionId : null,
                pattern,
              };
        await api(editing ? `/triggers/${editing.id}` : "/triggers", {
          ...json(editing ? "PUT" : "POST", {
            name,
            project_id: projectId,
            [`${kind}_id`]: targetId,
            type: triggerType,
            source,
            parameters,
            event_parameter: eventParameter || null,
            enabled: editing?.enabled ?? true,
            interval_seconds: interval,
            debounce_seconds: debounce,
            include_existing: includeExisting,
          }),
          method: editing ? "PUT" : "POST",
        });
        setSection("triggers");
      }
      setForm(null);
      setVersion((value) => value + 1);
    } catch (error) {
      setFormError(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  async function action(path: string, body?: unknown, method = "POST") {
    try {
      await api(path, json(method, body || {}));
      setVersion((value) => value + 1);
    } catch (error) {
      report(errorText(error));
    }
  }
  async function showEvents(trigger: Trigger) {
    try {
      const result = await api<
        Page<{ id: string; status: string; created_at: string; event: unknown }>
      >(`/triggers/${trigger.id}/events?limit=25&offset=0`);
      setEventOffset(0);
      setEvents({ trigger, page: result });
    } catch (error) {
      report(errorText(error));
    }
  }
  function openWorkflow(id: string) {
    if (props.onOpenWorkflowRun) props.onOpenWorkflowRun(id);
    else {
      setWorkflowId(id);
      setWorkflowRun(null);
    }
  }
  const dates =
    target?.parameter_definitions.filter((item) =>
      ["date", "string"].includes(item.type),
    ) || [];
  const editableParameters =
    target?.parameter_definitions.filter(
      (item) =>
        item.name !== (form === "backfill" ? dateParameter : eventParameter),
    ) || [];

  return (
    <>
      <SectionHead
        eyebrow="REPEATABLE WORK, ON YOUR TERMS"
        title="Automation"
        description="Reprocess a date range, recover missed schedules, and start work when new data arrives."
        actions={
          <>
            {canRun ? (
              <button className="button" onClick={() => openForm("backfill")}>
                <CalendarRange size={16} />
                New backfill
              </button>
            ) : null}
            {canEdit ? (
              <button
                className="button primary"
                onClick={() => openForm("trigger")}
              >
                <Plus size={16} />
                New trigger
              </button>
            ) : null}
          </>
        }
      />
      <div className="tabs" role="tablist" aria-label="Automation views">
        {(
          [
            ["backfills", "Backfills"],
            ["triggers", "Event triggers"],
            ["runs", "Automated runs"],
            ["catchup", "Missed schedules"],
          ] as [Section, string][]
        ).map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={section === value}
            className={section === value ? "active" : ""}
            onClick={() => setSection(value)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="panel">
        <div className="panel-head">
          <h3>
            {section === "backfills"
              ? "Date range backfills"
              : section === "triggers"
                ? "Event triggers"
                : section === "runs"
                  ? "Automated run history"
                  : "Schedule recovery"}
          </h3>
          <button
            className="button compact"
            onClick={() => setVersion((value) => value + 1)}
          >
            <RefreshCw size={14} />
            Refresh
          </button>
        </div>
        {loadError ? (
          <div className="inline-error" role="alert">
            {loadError}
          </div>
        ) : null}
        {loading && !page ? (
          <Spinner />
        ) : !page?.items.length ? (
          <Empty
            icon={
              section === "backfills" ? (
                <CalendarRange size={28} />
              ) : section === "triggers" ? (
                <Zap size={28} />
              ) : (
                <Clock3 size={28} />
              )
            }
            title={
              section === "backfills"
                ? "Reprocess a date range"
                : section === "triggers"
                  ? "Let new data start your pipeline"
                  : "No records yet"
            }
          >
            {section === "catchup"
              ? "Choose a missed-run policy when configuring a scheduled job. Recovery records appear after its first schedule check."
              : "Work stays in bounded queues and follows your project's compute permissions."}
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                {section === "backfills" ? (
                  <tr>
                    <th>Name / target</th>
                    <th>Date range</th>
                    <th>Progress</th>
                    <th>Status</th>
                    <th>Actions</th>
                  </tr>
                ) : section === "triggers" ? (
                  <tr>
                    <th>Trigger / source</th>
                    <th>Type</th>
                    <th>Last checked</th>
                    <th>Status</th>
                    <th>Actions</th>
                  </tr>
                ) : section === "runs" ? (
                  <tr>
                    <th>Run</th>
                    <th>Started by</th>
                    <th>Observed</th>
                    <th>Status</th>
                    <th>Actions</th>
                  </tr>
                ) : (
                  <tr>
                    <th>Schedule</th>
                    <th>Checked through</th>
                    <th>Remaining / skipped</th>
                    <th>Last checked</th>
                  </tr>
                )}
              </thead>
              <tbody>
                {page.items.map((row) => {
                  if (section === "backfills") {
                    const item = row as Backfill;
                    return (
                      <tr key={item.id}>
                        <td>
                          <button
                            className="text-button"
                            onClick={() => {
                              setDetailId(item.id);
                              setDetail(item);
                            }}
                          >
                            {item.name}
                          </button>
                          <small>{item.target_name}</small>
                        </td>
                        <td>
                          {item.start_date} → {item.end_date}
                        </td>
                        <td>
                          {item.progress.completed} / {item.progress.total}
                        </td>
                        <td>
                          <Status status={item.status} />
                        </td>
                        <td>
                          {canRun && !terminal(item.status) ? (
                            <button
                              className="button compact"
                              onClick={() =>
                                action(`/backfills/${item.id}/cancel`)
                              }
                            >
                              Cancel
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    );
                  }
                  if (section === "triggers") {
                    const item = row as Trigger;
                    return (
                      <tr key={item.id}>
                        <td>
                          <button
                            className="text-button"
                            disabled={!canEdit}
                            onClick={() => openForm("trigger", item)}
                          >
                            {item.name}
                          </button>
                          <small>
                            {item.source.path ||
                              `Workflow ${item.source.workflow_id?.slice(0, 10)}`}
                          </small>
                          {item.last_error ? (
                            <div className="inline-error">
                              {item.last_error}
                            </div>
                          ) : null}
                        </td>
                        <td>
                          {item.type === "file_arrival"
                            ? "File arrival"
                            : item.type === "table_change"
                              ? "Delta table change"
                              : "Workflow completion"}
                        </td>
                        <td>
                          {item.last_checked_at
                            ? date(item.last_checked_at)
                            : "Not checked yet"}
                        </td>
                        <td>
                          <Status
                            status={item.enabled ? "enabled" : "paused"}
                          />
                        </td>
                        <td>
                          <div className="row-actions">
                            {canRun && item.enabled ? (
                              <button
                                className="button compact"
                                onClick={() =>
                                  action(`/triggers/${item.id}/check`)
                                }
                              >
                                Check now
                              </button>
                            ) : null}
                            <button
                              className="button compact"
                              onClick={() => void showEvents(item)}
                            >
                              Events
                            </button>
                            {canEdit ? (
                              <>
                                <button
                                  className="button compact"
                                  onClick={() =>
                                    action(
                                      `/triggers/${item.id}`,
                                      { ...item, enabled: !item.enabled },
                                      "PUT",
                                    )
                                  }
                                >
                                  {item.enabled ? "Pause" : "Resume"}
                                </button>
                                <button
                                  className="button compact danger"
                                  onClick={() => {
                                    if (
                                      confirm(
                                        `Delete trigger “${item.name}”? Its pending events will be cancelled.`,
                                      )
                                    )
                                      void action(
                                        `/triggers/${item.id}`,
                                        {},
                                        "DELETE",
                                      );
                                  }}
                                >
                                  Delete
                                </button>
                              </>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    );
                  }
                  if (section === "runs") {
                    const item = row as Dispatch;
                    return (
                      <tr key={item.id}>
                        <td>
                          <button
                            className="text-button"
                            disabled={!item.run_available}
                            onClick={() => openWorkflow(item.run_id)}
                          >
                            {item.name}
                          </button>
                          {item.error ? <small>{item.error}</small> : null}
                        </td>
                        <td>
                          {item.source_kind === "schedule"
                            ? "Schedule"
                            : "Event trigger"}
                        </td>
                        <td>{date(item.observed_at)}</td>
                        <td>
                          <Status status={item.status} />
                        </td>
                        <td>
                          {canRun && !terminal(item.status) ? (
                            <button
                              className="button compact"
                              onClick={() =>
                                action(`/automation/runs/${item.id}/cancel`)
                              }
                            >
                              Cancel
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    );
                  }
                  const item = row as Cursor;
                  return (
                    <tr key={item.id}>
                      <td>
                        <span>{item.name || item.id.slice(0, 12)}</span>
                        <small>{item.policy || "skip"}</small>
                        {item.last_error ? (
                          <small>{item.last_error}</small>
                        ) : null}
                      </td>
                      <td>{date(item.cursor)}</td>
                      <td>
                        {item.remaining || 0} remaining / {item.skipped || 0}{" "}
                        skipped
                      </td>
                      <td>
                        {item.last_checked_at
                          ? date(item.last_checked_at)
                          : "Not checked yet"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {page ? (
          <Pagination
            {...page}
            offset={offset}
            onPage={setOffset}
            busy={loading}
          />
        ) : null}
        {section === "triggers" ? (
          <p className="muted">
            Checks can start the configured pipeline. Local sources require
            shared local compute; use an S3 connection for sources shared with
            remote hosts. Poll intervals are minimum intervals.
          </p>
        ) : null}
        {section === "catchup" ? (
          <p className="muted">
            Missed-run policies are configured in Jobs &amp; schedules. Use a
            declared date parameter to process each occurrence's date. Queue
            limits and the lookback window bound recovery work.
          </p>
        ) : null}
      </div>

      {form ? (
        <Modal
          title={
            form === "backfill"
              ? "Create a date range backfill"
              : editing
                ? "Edit event trigger"
                : "Create event trigger"
          }
          wide
          onClose={() => {
            if (!busy) setForm(null);
          }}
        >
          <form className="modal-form" onSubmit={save}>
            <Field label="Name">
              <input
                required
                autoFocus
                maxLength={200}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </Field>
            <TargetPicker
              projectId={projectId}
              kind={kind}
              id={targetId}
              onKind={targetKind}
              onId={targetChoice}
            />
            {form === "backfill" ? (
              <>
                <div className="form-grid">
                  <Field
                    label="Date parameter"
                    hint="The selected input receives each date in the range."
                  >
                    <select
                      required
                      value={dateParameter}
                      onChange={(event) => setDateParameter(event.target.value)}
                    >
                      <option value="">Choose an input</option>
                      {dates.map((item) => (
                        <option key={item.name} value={item.name}>
                          {item.name} ({item.type})
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Start date">
                    <input
                      type="date"
                      required
                      value={startDate}
                      onChange={(event) => setStartDate(event.target.value)}
                    />
                  </Field>
                  <Field label="End date">
                    <input
                      type="date"
                      required
                      min={startDate}
                      value={endDate}
                      onChange={(event) => setEndDate(event.target.value)}
                    />
                  </Field>
                  <Field
                    label="Maximum concurrent dates"
                    hint="Actual execution also follows project and host resource limits."
                  >
                    <input
                      type="number"
                      min={1}
                      max={8}
                      value={concurrency}
                      onChange={(event) =>
                        setConcurrency(Number(event.target.value))
                      }
                    />
                  </Field>
                </div>
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={stopOnFailure}
                    onChange={(event) => setStopOnFailure(event.target.checked)}
                  />
                  <span>Stop admitting later dates after a failure</span>
                </label>
                <p className="muted">
                  Up to 366 dates. Source and package environments are captured
                  once when created. Cancellation waits for running work to
                  acknowledge it.
                </p>
              </>
            ) : (
              <>
                <Field label="Start work when">
                  <select
                    value={triggerType}
                    onChange={(event) =>
                      setTriggerType(event.target.value as Trigger["type"])
                    }
                  >
                    <option value="file_arrival">
                      Files arrive or are updated
                    </option>
                    <option value="table_change">A Delta table changes</option>
                    <option value="workflow_completion">
                      Another workflow completes
                    </option>
                  </select>
                </Field>
                {triggerType === "workflow_completion" ? (
                  <>
                    <WorkflowPicker
                      projectId={projectId}
                      value={upstream}
                      onChange={setUpstream}
                      label="Upstream workflow"
                    />
                    <div className="form-grid">
                      {["succeeded", "failed", "cancelled"].map((state) => (
                        <label key={state} className="checkbox-field">
                          <input
                            type="checkbox"
                            checked={states.includes(state)}
                            onChange={(event) =>
                              setStates((previous) =>
                                event.target.checked
                                  ? [...previous, state]
                                  : previous.filter((item) => item !== state),
                              )
                            }
                          />
                          <span>{state}</span>
                        </label>
                      ))}
                    </div>
                  </>
                ) : (
                  <>
                    <Field
                      label={
                        triggerType === "table_change"
                          ? "Delta table location"
                          : "Input location"
                      }
                      hint={
                        triggerType === "table_change"
                          ? "Use the table root, not its _delta_log folder."
                          : "Use an input location separate from the pipeline's outputs."
                      }
                    >
                      <input
                        required
                        value={path}
                        onChange={(event) => setPath(event.target.value)}
                        placeholder="blob://incoming/orders/ or s3://bucket/prefix/"
                      />
                    </Field>
                    {path.startsWith("s3://") ? (
                      <Field label="S3 connection">
                        <select
                          required
                          value={connectionId}
                          onChange={(event) =>
                            setConnectionId(event.target.value)
                          }
                        >
                          <option value="">
                            Choose an assigned connection
                          </option>
                          {connections.map((item) => (
                            <option key={item.id} value={item.id}>
                              {item.name}
                            </option>
                          ))}
                        </select>
                      </Field>
                    ) : null}
                    {triggerType === "file_arrival" ? (
                      <Field
                        label="File pattern"
                        hint="For example *.csv or *.parquet. Matching uses object paths relative to the bucket."
                      >
                        <input
                          value={pattern}
                          onChange={(event) => setPattern(event.target.value)}
                        />
                      </Field>
                    ) : null}
                  </>
                )}
                <div className="form-grid">
                  <Field label="Minimum check interval (seconds)">
                    <input
                      type="number"
                      min={15}
                      max={86400}
                      value={interval}
                      onChange={(event) =>
                        setIntervalSeconds(Number(event.target.value))
                      }
                    />
                  </Field>
                  <Field label="Wait for stable input (seconds)">
                    <input
                      type="number"
                      min={0}
                      max={3600}
                      value={debounce}
                      onChange={(event) =>
                        setDebounce(Number(event.target.value))
                      }
                    />
                  </Field>
                  <Field
                    label="Pass event details to"
                    hint="Choose a JSON parameter to receive object or upstream-run metadata."
                  >
                    <select
                      value={eventParameter}
                      onChange={(event) =>
                        setEventParameter(event.target.value)
                      }
                    >
                      <option value="">No event parameter</option>
                      {target?.parameter_definitions
                        .filter((item) => item.type === "json")
                        .map((item) => (
                          <option key={item.name} value={item.name}>
                            {item.name}
                          </option>
                        ))}
                    </select>
                  </Field>
                </div>
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={includeExisting}
                    onChange={(event) =>
                      setIncludeExisting(event.target.checked)
                    }
                  />
                  <span>
                    Include existing data when this source is first activated
                  </span>
                </label>
              </>
            )}
            {editableParameters.length ? (
              <>
                <h3>Parameter overrides</h3>
                <ParameterValues
                  definitions={editableParameters}
                  values={parameters}
                  onChange={setParameters}
                />
              </>
            ) : null}
            {preview && form === "backfill" ? (
              <div className="inline-note" role="status">
                <strong>{preview.count} runs</strong> of {preview.target_name},
                from {preview.dates[0]} through {preview.dates.at(-1)}. Source
                and packages will stay fixed across this range.
              </div>
            ) : null}
            {formError ? (
              <div className="inline-error" role="alert">
                {formError}
              </div>
            ) : null}
            <div className="modal-footer">
              <button
                type="button"
                className="button"
                disabled={busy}
                onClick={() => setForm(null)}
              >
                Cancel
              </button>
              {form === "backfill" ? (
                <button
                  type="button"
                  className="button"
                  disabled={busy || !targetId || !dateParameter}
                  onClick={() => void previewBackfill()}
                >
                  {busy ? <Spinner /> : <CalendarRange size={15} />}Preview runs
                </button>
              ) : null}
              <button
                className="button primary"
                disabled={
                  busy || !targetId || (form === "backfill" && !preview)
                }
              >
                {busy ? <Spinner /> : <Plus size={15} />}
                {form === "backfill" ? "Create backfill" : "Save trigger"}
              </button>
            </div>
          </form>
        </Modal>
      ) : null}

      {detailId && detail ? (
        <Modal
          title={detail.name}
          wide
          onClose={() => {
            setDetailId(null);
            setDetail(null);
          }}
        >
          <div className="modal-form">
            <div className="row-actions">
              <Status status={detail.status} />
              <span>
                {detail.progress.completed} / {detail.progress.total} dates
                complete
              </span>
              {canRun && !terminal(detail.status) ? (
                <button
                  className="button compact"
                  onClick={() => action(`/backfills/${detail.id}/cancel`)}
                >
                  Cancel backfill
                </button>
              ) : null}
            </div>
            <p>
              {detail.target_name} · {detail.start_date} → {detail.end_date}
            </p>
            {detail.snapshot_id ? (
              <p className="muted">
                Pinned capture <code>{detail.snapshot_id.slice(0, 16)}</code> ·{" "}
                {detail.environment_ids?.length || 1} package environment(s)
              </p>
            ) : null}
            {detail.error ? (
              <div className="inline-error">{detail.error}</div>
            ) : null}
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Status</th>
                    <th>Run</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.items?.map((item) => (
                    <tr key={item.date}>
                      <td>{item.date}</td>
                      <td>
                        <Status status={item.status} />
                        {item.error ? <small>{item.error}</small> : null}
                      </td>
                      <td>
                        {item.run_available ? (
                          <button
                            className="text-button"
                            onClick={() => openWorkflow(item.run_id)}
                          >
                            <WorkflowIcon size={14} />
                            {item.run_id.slice(0, 12)}
                          </button>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </Modal>
      ) : null}
      {workflowId ? (
        <Modal
          title={workflowRun?.name || "Automated workflow run"}
          wide
          onClose={() => {
            setWorkflowId(null);
            setWorkflowRun(null);
          }}
        >
          <div className="modal-form">
            {workflowRun ? (
              <>
                <Status status={workflowRun.status} />
                {workflowRun.error ? (
                  <div className="inline-error">{workflowRun.error}</div>
                ) : null}
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Step</th>
                        <th>Status</th>
                        <th>Attempts</th>
                        <th>Notebook run</th>
                      </tr>
                    </thead>
                    <tbody>
                      {workflowRun.tasks.map((task) => (
                        <tr key={task.key}>
                          <td>{task.key}</td>
                          <td>
                            <Status status={task.status} />
                            {task.error ? <small>{task.error}</small> : null}
                          </td>
                          <td>{task.attempts.length}</td>
                          <td>
                            {task.run_id && props.onRun ? (
                              <button
                                className="text-button"
                                onClick={() => props.onRun?.(task.run_id!)}
                              >
                                Open logs
                              </button>
                            ) : (
                              <code>{task.run_id?.slice(0, 12) || "—"}</code>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              <Spinner />
            )}
          </div>
        </Modal>
      ) : null}
      {events ? (
        <Modal
          title={`Events · ${events.trigger.name}`}
          wide
          onClose={() => setEvents(null)}
        >
          <div className="modal-form">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Observed</th>
                    <th>Status</th>
                    <th>Event</th>
                  </tr>
                </thead>
                <tbody>
                  {events.page.items.map((item) => (
                    <tr key={item.id}>
                      <td>{date(item.created_at)}</td>
                      <td>
                        <Status status={item.status} />
                      </td>
                      <td>
                        <pre>{JSON.stringify(item.event, null, 2)}</pre>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              {...events.page}
              offset={eventOffset}
              onPage={setEventOffset}
            />
          </div>
        </Modal>
      ) : null}
    </>
  );
}
