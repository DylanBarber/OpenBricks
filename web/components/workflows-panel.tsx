"use client";
import { useEffect, useState, type FormEvent } from "react";
import {
  GitBranch,
  Play,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import {
  api,
  date,
  json,
  type Cluster,
  type Page,
  type Project,
} from "@/lib/api";
import type {
  Release,
  Workflow,
  WorkflowRun,
  WorkflowTask,
} from "@/lib/platform";
import {
  Empty,
  Field,
  Modal,
  Pagination,
  SectionHead,
  Spinner,
  Status,
} from "./ui";
import { ScheduleForm } from "./schedules-panel";
import { NotebookPicker } from "./platform-controls";
import { ResourceRunFields } from "./resource-controls";
import {
  ParameterDefinitions,
  ParameterValues,
  parameterDefaults,
} from "./parameter-fields";
type Target = { id: string; name: string };
const freshTask = (index: number): WorkflowTask => ({
  key: `step_${index + 1}`,
  notebook_id: "",
  depends_on: [],
  parameters: {},
  timeout_seconds: 3600,
  max_retries: 0,
  retry_delay_seconds: 30,
});
function TaskGraph({
  tasks,
  statuses,
  onSelect,
}: {
  tasks: WorkflowTask[];
  statuses?: Record<string, string>;
  onSelect?: (key: string) => void;
}) {
  const levels = new Map<string, number>();
  for (let pass = 0; pass < tasks.length; pass++)
    for (const task of tasks)
      levels.set(
        task.key,
        Math.min(
          tasks.length,
          Math.max(
            0,
            ...task.depends_on.map((key) => (levels.get(key) || 0) + 1),
          ),
        ),
      );
  const rows = new Map<number, number>(),
    positions = new Map<string, { x: number; y: number }>();
  for (const task of tasks) {
    const level = levels.get(task.key) || 0,
      row = rows.get(level) || 0;
    positions.set(task.key, { x: level * 230 + 15, y: row * 105 + 15 });
    rows.set(level, row + 1);
  }
  const width = Math.max(
      260,
      ...Array.from(positions.values()).map((point) => point.x + 215),
    ),
    height = Math.max(
      120,
      ...Array.from(positions.values()).map((point) => point.y + 90),
    );
  return (
    <div className="workflow-graph-scroll">
      <div className="workflow-graph" style={{ width, height }}>
        <svg width={width} height={height} aria-hidden="true">
          <defs>
            <marker
              id="workflow-arrow"
              markerWidth="8"
              markerHeight="8"
              refX="7"
              refY="4"
              orient="auto"
            >
              <path d="M0 0L8 4L0 8" fill="currentColor" />
            </marker>
          </defs>
          {tasks.flatMap((task) =>
            task.depends_on.map((key) => {
              const a = positions.get(key),
                b = positions.get(task.key);
              return a && b ? (
                <path
                  key={`${key}-${task.key}`}
                  d={`M${a.x + 195} ${a.y + 34}C${a.x + 220} ${a.y + 34} ${b.x - 25} ${b.y + 34} ${b.x} ${b.y + 34}`}
                  fill="none"
                  stroke="currentColor"
                  markerEnd="url(#workflow-arrow)"
                />
              ) : null;
            }),
          )}
        </svg>
        {tasks.map((task) => {
          const point = positions.get(task.key)!;
          return (
            <button
              type="button"
              key={task.key}
              className="workflow-node"
              style={{ left: point.x, top: point.y }}
              onClick={() => onSelect?.(task.key)}
            >
              <strong>
                <GitBranch size={13} />
                {task.key}
              </strong>
              <span>
                {task.depends_on.length
                  ? `After ${task.depends_on.join(", ")}`
                  : "Starts immediately"}
              </span>
              {statuses?.[task.key] ? (
                <Status status={statuses[task.key]} />
              ) : (
                <small>
                  {task.max_retries} retries · {task.timeout_seconds}s timeout
                </small>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
export default function WorkflowsPanel({
  role,
  projectId,
  projects,
  clusters,
  onError,
  onRun,
}: {
  role: string;
  projectId: string;
  projects: Project[];
  clusters: Cluster[];
  onError: (message: string) => void;
  onRun: (id: string) => void;
}) {
  const canEdit = ["editor", "owner"].includes(role),
    canRun = role !== "viewer";
  const [schedule, setSchedule] = useState<Workflow | null>(null);
  const [page, setPage] = useState<Page<Workflow> | null>(null),
    [offset, setOffset] = useState(0),
    [search, setSearch] = useState(""),
    [version, setVersion] = useState(0),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Workflow | null>(null),
    [activeTask, setActiveTask] = useState(0),
    [taskParams, setTaskParams] = useState("{}"),
    [formError, setFormError] = useState(""),
    [runTarget, setRunTarget] = useState<Workflow | null>(null),
    [parameters, setParameters] = useState<Record<string, unknown>>({}),
    [selectedRun, setSelectedRun] = useState<WorkflowRun | null>(null),
    [history, setHistory] = useState<Page<WorkflowRun> | null>(null),
    [historyTarget, setHistoryTarget] = useState<Workflow | null>(null),
    [historyOffset, setHistoryOffset] = useState(0),
    [releases, setReleases] = useState<Release[]>([]),
    [notifications, setNotifications] = useState<Target[]>([]);
  useEffect(() => {
    setOffset(0);
  }, [projectId, search]);
  useEffect(() => {
    let stopped = false;
    setLoading(true);
    api<Page<Workflow> | Workflow[]>(
      `/workflows/page?${new URLSearchParams({ project_id: projectId, search, limit: "25", offset: String(offset) })}`,
    )
      .then((result) => {
        if (!stopped)
          setPage(
            Array.isArray(result)
              ? { items: result, total: result.length, limit: 25, offset: 0 }
              : result,
          );
      })
      .catch((error) => {
        if (!stopped) onError(error.message);
      })
      .finally(() => {
        if (!stopped) setLoading(false);
      });
    return () => {
      stopped = true;
    };
  }, [projectId, search, offset, version, onError]);
  useEffect(() => {
    if (!editing) return;
    let stopped = false;
    Promise.allSettled([
      api<Release[]>(`/releases?project_id=${editing.project_id}`),
      api<Target[]>(`/notifications?project_id=${editing.project_id}`),
    ]).then(([releases, notifications]) => {
      if (!stopped) {
        if (releases.status === "fulfilled") setReleases(releases.value);
        else onError(releases.reason.message);
        if (notifications.status === "fulfilled")
          setNotifications(notifications.value);
        else setNotifications([]);
      }
    });
    return () => {
      stopped = true;
    };
  }, [!!editing, editing?.project_id, onError]);
  useEffect(() => {
    if (!historyTarget) return;
    let stopped = false;
    api<Page<WorkflowRun>>(
      `/workflow-runs?project_id=${projectId}&workflow_id=${historyTarget.id}&limit=20&offset=${historyOffset}`,
    )
      .then((result) => {
        if (!stopped) setHistory(result);
      })
      .catch((error) => {
        if (!stopped) onError(error.message);
      });
    return () => {
      stopped = true;
    };
  }, [historyTarget?.id, historyOffset, version, projectId, onError]);
  useEffect(() => {
    if (!selectedRun || !["queued", "running"].includes(selectedRun.status))
      return;
    let stopped = false;
    const timer = setInterval(
      () =>
        api<WorkflowRun>(`/workflow-runs/${selectedRun.id}`)
          .then((result) => {
            if (!stopped) setSelectedRun(result);
          })
          .catch((error) => {
            if (!stopped) onError(error.message);
          }),
      1500,
    );
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [selectedRun?.id, selectedRun?.status, onError]);
  function edit(value: Workflow) {
    setEditing(value);
    setActiveTask(0);
    setTaskParams(JSON.stringify(value.tasks[0]?.parameters || {}, null, 2));
    setFormError("");
  }
  function update(patch: Partial<Workflow>) {
    setEditing((previous) => (previous ? { ...previous, ...patch } : previous));
  }
  function taskUpdate(patch: Partial<WorkflowTask>) {
    if (editing)
      update({
        tasks: editing.tasks.map((task, index) =>
          index === activeTask ? { ...task, ...patch } : task,
        ),
      });
  }
  function selectTask(index: number) {
    if (!editing) return;
    try {
      const parsed = JSON.parse(taskParams);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object")
        throw Error("Task parameters must be a JSON object.");
      update({
        tasks: editing.tasks.map((task, i) =>
          i === activeTask ? { ...task, parameters: parsed } : task,
        ),
      });
      setActiveTask(index);
      setTaskParams(
        JSON.stringify(editing.tasks[index]?.parameters || {}, null, 2),
      );
      setFormError("");
    } catch (error) {
      setFormError((error as Error).message);
    }
  }
  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setFormError("");
    try {
      await work();
      setVersion((value) => value + 1);
    } catch (error) {
      setFormError((error as Error).message);
      onError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editing) return;
    await perform(async () => {
      const parsed = JSON.parse(taskParams);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object")
        throw Error("Task parameters must be a JSON object.");
      const value = {
        ...editing,
        tasks: editing.tasks.map((task, index) =>
          index === activeTask ? { ...task, parameters: parsed } : task,
        ),
      };
      await api(
        editing.id ? `/workflows/${editing.id}` : "/workflows",
        json(editing.id ? "PUT" : "POST", value),
      );
      setEditing(null);
    });
  }
  const task = editing?.tasks[activeTask];
  return (
    <>
      <SectionHead
        eyebrow="BUILD CONNECTED WORK"
        title="Workflows"
        description="Connect notebooks into dependable pipelines with dependencies, retries, and step-level recovery."
        actions={
          <button
            className="button primary"
            disabled={!canEdit}
            onClick={() =>
              edit({
                id: "",
                name: "New workflow",
                project_id: projectId,
                parameter_definitions: [],
                parameters: {},
                tasks: [freshTask(0)],
              })
            }
          >
            <Plus size={15} />
            New workflow
          </button>
        }
      />
      <section className="card">
        <div className="browser-toolbar">
          <input
            className="platform-search"
            aria-label="Search workflows"
            placeholder="Find a workflow…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <button
            className="icon-button"
            aria-label="Refresh workflows"
            onClick={() => setVersion((value) => value + 1)}
          >
            <RefreshCw size={15} />
          </button>
          {loading ? <Spinner /> : null}
        </div>
        {page?.items.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Workflow</th>
                <th>Steps</th>
                <th>Schedule</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((workflow) => (
                <tr key={workflow.id}>
                  <td>
                    <button
                      className="name-link"
                      onClick={() => edit(workflow)}
                    >
                      {workflow.name}
                    </button>
                  </td>
                  <td>{workflow.tasks.length}</td>
                  <td>
                    <button
                      className="text-button"
                      disabled={!canEdit}
                      onClick={() => setSchedule(workflow)}
                    >
                      Add schedule
                    </button>
                  </td>
                  <td>
                    <div className="row-actions">
                      <button
                        className="button compact"
                        disabled={!canRun}
                        onClick={() => {
                          setRunTarget(workflow);
                          setParameters({
                            ...parameterDefaults(
                              workflow.parameter_definitions || [],
                            ),
                            ...workflow.parameters,
                          });
                        }}
                      >
                        <Play size={12} />
                        Run
                      </button>
                      <button
                        className="text-button"
                        onClick={() => {
                          setHistoryTarget(workflow);
                          setHistoryOffset(0);
                        }}
                      >
                        History
                      </button>
                      <button
                        className="icon-button"
                        aria-label={`Delete ${workflow.name}`}
                        disabled={!canEdit}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Delete workflow “${workflow.name}”?`,
                            )
                          )
                            void perform(async () => {
                              await api(
                                `/workflows/${workflow.id}`,
                                json("DELETE"),
                              );
                            });
                        }}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : !loading ? (
          <Empty title="Your first pipeline starts here">
            Add notebooks as steps and choose which steps depend on others.
          </Empty>
        ) : null}
        <Pagination
          total={page?.total || 0}
          offset={offset}
          limit={25}
          onPage={setOffset}
          busy={loading}
        />
      </section>
      {schedule ? (
        <ScheduleForm
          projectId={projectId}
          workflow={schedule}
          job={null}
          clusters={clusters}
          onSave={() => setVersion((value) => value + 1)}
          onClose={() => setSchedule(null)}
        />
      ) : null}
      {editing ? (
        <Modal
          title={editing.id ? "Edit workflow" : "Create workflow"}
          wide
          onClose={() => {
            if (!busy) setEditing(null);
          }}
        >
          <form className="modal-form workflow-form" onSubmit={save}>
            <fieldset className="plain-fieldset" disabled={!canEdit}>
              <div className="form-grid">
                <Field label="Name">
                  <input
                    value={editing.name}
                    required
                    onChange={(event) => update({ name: event.target.value })}
                  />
                </Field>
                <Field label="Project">
                  <select
                    disabled={!!editing.id}
                    value={editing.project_id}
                    onChange={(event) =>
                      update({ project_id: event.target.value })
                    }
                  >
                    {projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              <TaskGraph
                tasks={editing.tasks}
                onSelect={(key) =>
                  selectTask(
                    editing.tasks.findIndex((task) => task.key === key),
                  )
                }
              />
              <div className="task-tabs">
                {editing.tasks.map((task, index) => (
                  <button
                    type="button"
                    key={index}
                    className={activeTask === index ? "active" : ""}
                    onClick={() => selectTask(index)}
                  >
                    {task.key}
                  </button>
                ))}
                <button
                  type="button"
                  onClick={() => {
                    try {
                      const parameters = JSON.parse(taskParams);
                      if (
                        !parameters ||
                        Array.isArray(parameters) ||
                        typeof parameters !== "object"
                      )
                        throw Error("Step parameters must be a JSON object.");
                      const tasks = [
                        ...editing.tasks.map((task, index) =>
                          index === activeTask ? { ...task, parameters } : task,
                        ),
                        freshTask(editing.tasks.length),
                      ];
                      update({ tasks });
                      setActiveTask(tasks.length - 1);
                      setTaskParams("{}");
                    } catch (error) {
                      setFormError((error as Error).message);
                    }
                  }}
                >
                  <Plus size={13} />
                  Add step
                </button>
              </div>
              {task ? (
                <div className="task-editor">
                  <div className="form-grid">
                    <Field label="Step key">
                      <input
                        value={task.key}
                        required
                        pattern="[A-Za-z_][A-Za-z0-9_-]*"
                        onChange={(event) => {
                          const old = task.key,
                            key = event.target.value;
                          update({
                            tasks: editing.tasks.map((item, index) =>
                              index === activeTask
                                ? { ...item, key }
                                : {
                                    ...item,
                                    depends_on: item.depends_on.map((value) =>
                                      value === old ? key : value,
                                    ),
                                  },
                            ),
                          });
                        }}
                      />
                    </Field>
                    <Field label="Source">
                      <select
                        value={task.release_id ? "release" : "notebook"}
                        onChange={(event) =>
                          taskUpdate(
                            event.target.value === "release"
                              ? {
                                  release_id: releases[0]?.id || "",
                                  notebook_id: undefined,
                                  path: "",
                                }
                              : {
                                  release_id: undefined,
                                  path: undefined,
                                  notebook_id: "",
                                },
                          )
                        }
                      >
                        <option value="notebook">Workspace notebook</option>
                        <option value="release" disabled={!releases.length}>
                          Immutable Git release
                        </option>
                      </select>
                    </Field>
                  </div>
                  {task.release_id ? (
                    <div className="form-grid">
                      <Field label="Release">
                        <select
                          value={task.release_id}
                          onChange={(event) =>
                            taskUpdate({ release_id: event.target.value })
                          }
                        >
                          {releases.map((item) => (
                            <option key={item.id} value={item.id}>
                              {item.name} · {item.commit?.slice(0, 8)}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label="Notebook path">
                        <input
                          required
                          value={task.path || ""}
                          onChange={(event) =>
                            taskUpdate({ path: event.target.value })
                          }
                        />
                      </Field>
                    </div>
                  ) : (
                    <NotebookPicker
                      projectId={editing.project_id}
                      value={task.notebook_id || ""}
                      onChange={(notebook_id) => taskUpdate({ notebook_id })}
                    />
                  )}
                  <Field label="Runs after">
                    <div className="connection-checklist">
                      {editing.tasks
                        .filter((_, index) => index !== activeTask)
                        .map((item) => (
                          <label key={item.key} className="checkbox-field">
                            <input
                              type="checkbox"
                              checked={task.depends_on.includes(item.key)}
                              onChange={(event) =>
                                taskUpdate({
                                  depends_on: event.target.checked
                                    ? [...task.depends_on, item.key]
                                    : task.depends_on.filter(
                                        (key) => key !== item.key,
                                      ),
                                })
                              }
                            />
                            {item.key}
                          </label>
                        ))}
                    </div>
                  </Field>
                  <div className="form-grid">
                    <Field label="Compute">
                      <select
                        value={task.cluster_id || ""}
                        onChange={(event) =>
                          taskUpdate({ cluster_id: event.target.value || null })
                        }
                      >
                        <option value="">Notebook default</option>
                        {clusters.map((cluster) => (
                          <option key={cluster.id} value={cluster.id}>
                            {cluster.name}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <Field label="Timeout (seconds)">
                      <input
                        type="number"
                        required
                        min={1}
                        value={task.timeout_seconds}
                        onChange={(event) =>
                          taskUpdate({
                            timeout_seconds: Number(event.target.value),
                          })
                        }
                      />
                    </Field>
                    <Field label="Retries">
                      <input
                        type="number"
                        min={0}
                        max={5}
                        value={task.max_retries}
                        onChange={(event) =>
                          taskUpdate({
                            max_retries: Number(event.target.value),
                          })
                        }
                      />
                    </Field>
                    <Field label="Retry delay (seconds)">
                      <input
                        type="number"
                        min={1}
                        value={task.retry_delay_seconds}
                        onChange={(event) =>
                          taskUpdate({
                            retry_delay_seconds: Number(event.target.value),
                          })
                        }
                      />
                    </Field>
                  </div>
                  <ResourceRunFields
                    jobOnly
                    value={task.resource_options || { mode: "job" }}
                    onChange={(resource_options) =>
                      taskUpdate({ resource_options })
                    }
                  />
                  <label className="checkbox-field">
                    <input
                      type="checkbox"
                      checked={!!task.retry_safe}
                      onChange={(event) =>
                        taskUpdate({ retry_safe: event.target.checked })
                      }
                    />
                    This step is safe to retry (writes are idempotent)
                  </label>
                  <Field
                    label="Step parameter overrides (JSON object)"
                    hint="Use an exact value like ${params.date} to pass a workflow parameter into a step."
                  >
                    <textarea
                      rows={4}
                      value={taskParams}
                      onChange={(event) => setTaskParams(event.target.value)}
                    />
                  </Field>
                  <button
                    type="button"
                    className="text-button danger-text"
                    disabled={editing.tasks.length === 1}
                    onClick={() => {
                      update({
                        tasks: editing.tasks
                          .filter((_, index) => index !== activeTask)
                          .map((item) => ({
                            ...item,
                            depends_on: item.depends_on.filter(
                              (key) => key !== task.key,
                            ),
                          })),
                      });
                      setActiveTask(0);
                      setTaskParams(
                        JSON.stringify(
                          editing.tasks[activeTask === 0 ? 1 : 0]?.parameters ||
                            {},
                          null,
                          2,
                        ),
                      );
                    }}
                  >
                    <Trash2 size={13} />
                    Remove step
                  </button>
                </div>
              ) : null}
              <h3>Workflow parameters</h3>
              <ParameterDefinitions
                value={editing.parameter_definitions || []}
                onChange={(parameter_definitions) =>
                  update({ parameter_definitions })
                }
              />
              {notifications.length ? (
                <Field label="Notify on failure or slow runs">
                  <select
                    multiple
                    name="notification_ids"
                    defaultValue={
                      (editing as Workflow & { notification_ids?: string[] })
                        .notification_ids || []
                    }
                    onChange={(event) =>
                      update({
                        notification_ids: Array.from(
                          event.target.selectedOptions,
                          (option) => option.value,
                        ),
                      } as Partial<Workflow>)
                    }
                  >
                    {notifications.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </Field>
              ) : null}
              {formError ? (
                <div className="inline-error">{formError}</div>
              ) : null}
            </fieldset>
            <div className="modal-footer">
              <button
                type="button"
                className="button"
                onClick={() => setEditing(null)}
                disabled={busy}
              >
                Cancel
              </button>
              <button className="button primary" disabled={busy || !canEdit}>
                {busy ? <Spinner /> : <Save size={14} />}Save workflow
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {runTarget ? (
        <Modal
          title={`Run ${runTarget.name}`}
          onClose={() => setRunTarget(null)}
        >
          <div className="modal-form">
            <ParameterValues
              definitions={runTarget.parameter_definitions || []}
              values={parameters}
              onChange={setParameters}
            />
            <p className="form-note">
              Each step captures its notebook source and package environment
              when the workflow starts.
            </p>
            <div className="modal-footer">
              <button
                className="button primary"
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    setSelectedRun(
                      await api<WorkflowRun>(
                        `/workflows/${runTarget.id}/run`,
                        json("POST", { parameters }),
                      ),
                    );
                    setRunTarget(null);
                  })
                }
              >
                <Play size={14} />
                Run workflow
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
      {historyTarget ? (
        <Modal
          title={`${historyTarget.name} · Run history`}
          wide
          onClose={() => setHistoryTarget(null)}
        >
          <div className="modal-form">
            {history?.items.map((run) => (
              <button
                className="history-row"
                key={run.id}
                onClick={() =>
                  void perform(async () =>
                    setSelectedRun(await api(`/workflow-runs/${run.id}`)),
                  )
                }
              >
                <Status status={run.status} />
                <code>{run.id.slice(0, 10)}</code>
                <span>{date(run.created_at)}</span>
              </button>
            ))}
            {!history?.items.length ? (
              <Empty title="No workflow runs yet" />
            ) : null}
            <Pagination
              total={history?.total || 0}
              offset={historyOffset}
              limit={20}
              onPage={setHistoryOffset}
            />
          </div>
        </Modal>
      ) : null}
      {selectedRun ? (
        <Modal
          title={selectedRun.name || "Workflow run"}
          wide
          onClose={() => setSelectedRun(null)}
        >
          <div className="modal-form">
            <div className="run-detail-heading">
              <Status status={selectedRun.status} />
              <span>
                {selectedRun.progress?.completed || 0} /{" "}
                {selectedRun.progress?.total || selectedRun.tasks.length} steps
                complete
              </span>
            </div>
            <TaskGraph
              tasks={selectedRun.tasks.map((task) => ({
                ...freshTask(0),
                ...task,
                parameters: {},
              }))}
              statuses={Object.fromEntries(
                selectedRun.tasks.map((task) => [task.key, task.status]),
              )}
              onSelect={(key) => {
                const task = selectedRun.tasks.find((task) => task.key === key);
                if (task?.run_id) onRun(task.run_id);
              }}
            />
            {selectedRun.tasks.map((task) => (
              <div className="workflow-step-result" key={task.key}>
                <strong>{task.key}</strong>
                <Status status={task.status} />
                <span>{task.attempts.length} attempts</span>
                {task.run_id ? (
                  <button
                    className="text-button"
                    onClick={() => onRun(task.run_id!)}
                  >
                    Open notebook logs
                  </button>
                ) : null}
                {task.error ? <pre className="stderr">{task.error}</pre> : null}
              </div>
            ))}
            {selectedRun.error ? (
              <div className="inline-error">{selectedRun.error}</div>
            ) : null}
            <div className="modal-footer">
              {["queued", "running"].includes(selectedRun.status) ? (
                <button
                  className="button danger"
                  disabled={busy || !canRun || !!selectedRun.cancel_requested}
                  onClick={() =>
                    void perform(async () =>
                      setSelectedRun(
                        await api(
                          `/workflow-runs/${selectedRun.id}/cancel`,
                          json("POST"),
                        ),
                      ),
                    )
                  }
                >
                  {selectedRun.cancel_requested
                    ? "Cancellation requested"
                    : "Cancel"}
                </button>
              ) : null}
              {selectedRun.status === "failed" ? (
                <button
                  className="button primary"
                  disabled={busy || !canRun}
                  onClick={() =>
                    void perform(async () =>
                      setSelectedRun(
                        await api(
                          `/workflow-runs/${selectedRun.id}/repair`,
                          json("POST"),
                        ),
                      ),
                    )
                  }
                >
                  Restart failed steps
                </button>
              ) : null}
              {!["queued", "running"].includes(selectedRun.status) ? (
                <button
                  className="button"
                  disabled={busy || !canRun}
                  onClick={() =>
                    void perform(async () =>
                      setSelectedRun(
                        await api(
                          `/workflow-runs/${selectedRun.id}/rerun`,
                          json("POST"),
                        ),
                      ),
                    )
                  }
                >
                  Rerun captured sources
                </button>
              ) : null}
            </div>
          </div>
        </Modal>
      ) : null}
    </>
  );
}
