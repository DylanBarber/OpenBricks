"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Cpu, RefreshCw, Save, Square } from "lucide-react";
import { api, json, type Cluster } from "@/lib/api";
import { Empty, Field, Modal, Spinner, Status } from "./ui";

export type ResourceRunOptions = {
  mode?: "interactive" | "job";
  memory_mb?: number;
  priority?: number;
};
type ProjectBudget = {
  max_concurrent: number;
  memory_budget_mb: number;
  priority: number;
};
type Policy = {
  memory_budget_mb: number;
  max_concurrent: number;
  max_sessions: number;
  default_memory_mb: number;
  max_memory_mb: number;
  idle_timeout_seconds: number;
  project_limits: Record<string, ProjectBudget>;
};
type Session = {
  id: string;
  notebook_id: string;
  notebook_name?: string;
  owner_id: string;
  project_id: string;
  mode: string;
  status: string;
  memory_mb: number;
  rss_mb?: number;
  has_spark?: boolean;
  active_run_id?: string;
  reason?: string;
};
type ResourceState = {
  policy: Policy;
  sessions: Session[];
  recent_sessions: Session[];
  active_runs: number;
  queued_runs: number;
  memory_reserved_mb: number;
};

export function ResourceRunFields({
  value,
  onChange,
  jobOnly = false,
}: {
  value: ResourceRunOptions;
  onChange: (value: ResourceRunOptions) => void;
  jobOnly?: boolean;
}) {
  return (
    <div className="form-grid">
      <Field
        label="Worker session"
        hint="Interactive sessions retain variables until eviction. Job workers start fresh and stop when the run finishes."
      >
        <select
          value={jobOnly ? "job" : value.mode || ""}
          disabled={jobOnly}
          onChange={(event) =>
            onChange({
              ...value,
              mode: event.target.value
                ? (event.target.value as "interactive" | "job")
                : undefined,
            })
          }
        >
          <option value="">Use run default</option>
          <option value="interactive">Interactive notebook session</option>
          <option value="job">Fresh job worker</option>
        </select>
      </Field>
      <Field
        label="Worker memory budget (MB)"
        hint="Leave blank to use the selected compute policy. The compute host enforces its own ceiling."
      >
        <input
          type="number"
          min={128}
          step={128}
          value={value.memory_mb ?? ""}
          onChange={(event) =>
            onChange({
              ...value,
              memory_mb: event.target.value
                ? Number(event.target.value)
                : undefined,
            })
          }
        />
      </Field>
      <Field
        label="Queue priority"
        hint="Higher values run first when capacity becomes available; active runs finish normally."
      >
        <input
          type="number"
          min={-20}
          max={20}
          value={value.priority ?? ""}
          placeholder="Use run default"
          onChange={(event) =>
            onChange({
              ...value,
              priority: event.target.value
                ? Number(event.target.value)
                : undefined,
            })
          }
        />
      </Field>
    </div>
  );
}

function PolicyForm({
  initial,
  projectId,
  clusterId,
  onSaved,
  onClose,
}: {
  initial: Policy;
  projectId: string;
  clusterId: string;
  onSaved: () => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState<Policy>(initial),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const project = value.project_limits[projectId];
  const update = (key: keyof Omit<Policy, "project_limits">, next: number) =>
    setValue((previous) => ({ ...previous, [key]: next }));
  const updateProject = (key: keyof ProjectBudget, next: number) =>
    setValue((previous) => ({
      ...previous,
      project_limits: {
        ...previous.project_limits,
        [projectId]: { ...previous.project_limits[projectId], [key]: next },
      },
    }));
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api(
        `/resources/policy?cluster_id=${encodeURIComponent(clusterId)}`,
        json("PUT", value),
      );
      onSaved();
      onClose();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title="Compute resource policy"
      onClose={() => {
        if (!busy) onClose();
      }}
      wide
    >
      <form className="modal-form" onSubmit={submit}>
        <p className="form-note">
          Wait for queued and running work to finish before changing budgets.
          Saving a policy releases idle workers and their temporary variables.
        </p>
        <div className="form-grid">
          <Field label="Total worker reservations (MB)">
            <input
              type="number"
              min={128}
              step={128}
              required
              value={value.memory_budget_mb}
              onChange={(event) =>
                update("memory_budget_mb", Number(event.target.value))
              }
            />
          </Field>
          <Field label="Concurrent runs">
            <input
              type="number"
              min={1}
              max={8}
              required
              value={value.max_concurrent}
              onChange={(event) =>
                update("max_concurrent", Number(event.target.value))
              }
            />
          </Field>
          <Field label="Default worker memory (MB)">
            <input
              type="number"
              min={128}
              step={128}
              required
              value={value.default_memory_mb}
              onChange={(event) =>
                update("default_memory_mb", Number(event.target.value))
              }
            />
          </Field>
          <Field label="Maximum worker memory (MB)">
            <input
              type="number"
              min={128}
              step={128}
              required
              value={value.max_memory_mb}
              onChange={(event) =>
                update("max_memory_mb", Number(event.target.value))
              }
            />
          </Field>
          <Field label="Maximum resident sessions">
            <input
              type="number"
              min={1}
              max={32}
              required
              value={value.max_sessions}
              onChange={(event) =>
                update("max_sessions", Number(event.target.value))
              }
            />
          </Field>
          <Field
            label="Idle shutdown (seconds)"
            hint="0 keeps an idle interactive worker until memory pressure or an explicit stop."
          >
            <input
              type="number"
              min={0}
              max={86400}
              required
              value={value.idle_timeout_seconds}
              onChange={(event) =>
                update("idle_timeout_seconds", Number(event.target.value))
              }
            />
          </Field>
        </div>
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={Boolean(project)}
            onChange={(event) =>
              setValue((previous) => {
                const limits = { ...previous.project_limits };
                if (event.target.checked)
                  limits[projectId] = {
                    max_concurrent: previous.max_concurrent,
                    memory_budget_mb: previous.memory_budget_mb,
                    priority: 0,
                  };
                else delete limits[projectId];
                return { ...previous, project_limits: limits };
              })
            }
          />
          <span>Customize the current project&apos;s budget</span>
        </label>
        {project ? (
          <div className="form-grid">
            <Field label="Project concurrent runs">
              <input
                type="number"
                min={1}
                max={value.max_concurrent}
                value={project.max_concurrent}
                required
                onChange={(event) =>
                  updateProject("max_concurrent", Number(event.target.value))
                }
              />
            </Field>
            <Field label="Project worker reservations (MB)">
              <input
                type="number"
                min={128}
                max={value.memory_budget_mb}
                step={128}
                value={project.memory_budget_mb}
                required
                onChange={(event) =>
                  updateProject("memory_budget_mb", Number(event.target.value))
                }
              />
            </Field>
            <Field label="Project queue priority adjustment">
              <input
                type="number"
                min={-20}
                max={20}
                value={project.priority}
                required
                onChange={(event) =>
                  updateProject("priority", Number(event.target.value))
                }
              />
            </Field>
          </div>
        ) : null}
        {error ? (
          <div className="inline-error" role="alert">
            {error}
          </div>
        ) : null}
        <div className="modal-footer">
          <button
            type="button"
            className="button"
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button className="button primary" disabled={busy}>
            {busy ? <Spinner /> : <Save size={15} />}Save policy
          </button>
        </div>
      </form>
    </Modal>
  );
}

export default function ResourceControls({
  projectId,
  clusters,
  admin,
  userId,
  canManage = false,
  onError,
}: {
  projectId: string;
  clusters: Cluster[];
  admin: boolean;
  userId: string;
  canManage?: boolean;
  onError: (message: string) => void;
}) {
  const [clusterId, setClusterId] = useState(clusters[0]?.id || ""),
    [state, setState] = useState<{ key: string; data: ResourceState } | null>(
      null,
    ),
    [policy, setPolicy] = useState<Policy | null>(null),
    [editing, setEditing] = useState(false),
    [stopping, setStopping] = useState<Session | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [refresh, setRefresh] = useState(0);
  const errorHandler = useRef(onError);
  useEffect(() => {
    errorHandler.current = onError;
  }, [onError]);
  const selected = clusters.some((cluster) => cluster.id === clusterId)
    ? clusterId
    : clusters[0]?.id || "";
  const requestKey = `${projectId}:${selected}`;
  const overview = state?.key === requestKey ? state.data : null;
  useEffect(() => {
    let cancelled = false,
      inFlight = false;
    setError("");
    setPolicy(null);
    setEditing(false);
    setStopping(null);
    if (!selected) return;
    const query = new URLSearchParams({
      project_id: projectId,
      cluster_id: selected,
    });
    async function load(initial: boolean) {
      if (inFlight) return;
      inFlight = true;
      try {
        const [data, nextPolicy] = await Promise.all([
          api<ResourceState>(`/resources/status?${query}`),
          initial && admin
            ? api<Policy>(
                `/resources/policy?cluster_id=${encodeURIComponent(selected)}`,
              )
            : Promise.resolve(null),
        ]);
        if (!cancelled) {
          setState({ key: requestKey, data });
          if (nextPolicy) setPolicy(nextPolicy);
          setError("");
        }
      } catch (failure) {
        if (!cancelled) setError((failure as Error).message);
      } finally {
        inFlight = false;
      }
    }
    void load(true);
    const interval = setInterval(() => {
      void load(false);
    }, 5000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [projectId, selected, requestKey, admin, refresh]);
  async function stop() {
    if (!stopping) return;
    setBusy(true);
    try {
      await api(
        `/resources/sessions/${stopping.id}/stop`,
        json("POST", { project_id: projectId, cluster_id: selected }),
      );
      setStopping(null);
      setRefresh((value) => value + 1);
    } catch (failure) {
      const message = (failure as Error).message;
      setError(message);
      errorHandler.current(message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="card platform-form">
      <div className="card-heading">
        <div>
          <h3>Sessions & resource budgets</h3>
          <p className="form-note">
            Independent workers keep notebook state separate and queue work
            until its compute and project budgets fit.
          </p>
        </div>
        <Cpu size={18} />
      </div>
      <div className="row-actions">
        <Field label="Compute target">
          <select
            value={selected}
            onChange={(event) => setClusterId(event.target.value)}
          >
            {clusters.length ? (
              clusters.map((cluster) => (
                <option value={cluster.id} key={cluster.id}>
                  {cluster.name}
                </option>
              ))
            ) : (
              <option value="">No assigned compute</option>
            )}
          </select>
        </Field>
        <button
          className="button"
          onClick={() => setRefresh((value) => value + 1)}
        >
          <RefreshCw size={15} />
          Refresh sessions
        </button>
        {admin ? (
          <button
            className="button"
            disabled={!policy}
            onClick={() => setEditing(true)}
          >
            Configure resource policy
          </button>
        ) : null}
      </div>
      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}
      {overview ? (
        <>
          <div className="operations-stats">
            <div>
              <span>Compute reservations</span>
              <strong>
                {overview.memory_reserved_mb.toLocaleString()} /{" "}
                {overview.policy.memory_budget_mb.toLocaleString()} MB
              </strong>
            </div>
            <div>
              <span>Running / queued</span>
              <strong>
                {overview.active_runs} / {overview.queued_runs}
              </strong>
            </div>
            <div>
              <span>Concurrency limit</span>
              <strong>{overview.policy.max_concurrent}</strong>
            </div>
          </div>
          {overview.sessions.length ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Notebook</th>
                  <th>Mode</th>
                  <th>State</th>
                  <th>Actual / budget</th>
                  <th>Owner</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {overview.sessions.map((session) => (
                  <tr key={session.id}>
                    <td>
                      {session.notebook_name ||
                        session.notebook_id.slice(0, 12)}
                    </td>
                    <td>
                      {session.mode}
                      {session.has_spark ? " · Spark" : ""}
                    </td>
                    <td>
                      <Status status={session.status} />
                    </td>
                    <td>
                      {session.rss_mb?.toFixed(1) ?? "—"} / {session.memory_mb}{" "}
                      MB
                    </td>
                    <td>
                      {session.owner_id === userId
                        ? "You"
                        : session.owner_id.slice(0, 12)}
                    </td>
                    <td>
                      {admin || canManage || session.owner_id === userId ? (
                        <button
                          className="button danger compact"
                          onClick={() => setStopping(session)}
                        >
                          <Square size={13} />
                          Stop session
                        </button>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty icon={<Cpu size={24} />} title="Workers start when needed">
              This project has no live sessions on the selected compute host.
            </Empty>
          )}
          <p className="form-note">
            Worker memory is monitored across Python and child processes. The
            host container also has a hard memory ceiling. Local Spark catalog
            ownership is shared by one JVM; handing it to another session can
            release an idle Spark interpreter.
          </p>
          {overview.recent_sessions.length ? (
            <details>
              <summary>Recent worker shutdowns</summary>
              <ul>
                {overview.recent_sessions
                  .slice(-8)
                  .reverse()
                  .map((session, index) => (
                    <li key={`${session.id}:${index}`}>
                      {session.notebook_name ||
                        session.notebook_id.slice(0, 12)}{" "}
                      · {(session.reason || "stopped").replaceAll("_", " ")}
                    </li>
                  ))}
              </ul>
            </details>
          ) : null}
        </>
      ) : selected && !error ? (
        <Spinner />
      ) : null}
      {editing && policy ? (
        <PolicyForm
          initial={policy}
          projectId={projectId}
          clusterId={selected}
          onSaved={() => setRefresh((value) => value + 1)}
          onClose={() => setEditing(false)}
        />
      ) : null}
      {stopping ? (
        <Modal
          title="Stop this session?"
          onClose={() => {
            if (!busy) setStopping(null);
          }}
        >
          <div className="modal-form">
            <p>
              Stopping releases this worker&apos;s memory and clears its
              temporary variables. An active run in this session will be
              cancelled.
            </p>
            <div className="modal-footer">
              <button
                className="button"
                disabled={busy}
                onClick={() => setStopping(null)}
              >
                Keep session
              </button>
              <button className="button danger" disabled={busy} onClick={stop}>
                {busy ? <Spinner /> : <Square size={15} />}Stop session
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
