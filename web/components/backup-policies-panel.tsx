"use client";

import { useEffect, useState, type FormEvent } from "react";
import {
  ArchiveRestore,
  CloudUpload,
  Edit3,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import {
  api,
  bytes,
  date,
  json,
  type Connection,
  type Page,
  type Project,
} from "@/lib/api";
import { Empty, Field, Modal, Pagination, Spinner, Status } from "./ui";
import CronScheduleFields from "./cron-schedule-fields";

type Source = { connection_id: string; bucket: string; prefix: string };
type Policy = {
  id: string;
  name: string;
  project_id: string;
  connection_id: string;
  bucket: string;
  prefix: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  has_passphrase?: boolean;
  passphrase?: string;
  include_data: boolean;
  storage_sources: Source[];
  include_catalog: boolean;
  verify_restore: boolean;
  notification_ids: string[];
  keep_remote: number;
  next_fire_at?: string;
};
type BackupRun = {
  id: string;
  name: string;
  status: string;
  created_at: string;
  error?: string;
  queued_reason?: string;
  verification_status?: string;
  verification_error?: string;
  retention_warning?: string;
  remote_expired?: boolean;
  remote?: {
    connection_id: string;
    bucket: string;
    key: string;
    size: number;
    sha256: string;
  };
  restore_drill?: {
    passed: boolean;
    management_boot?: boolean;
    authentication?: boolean;
  };
};
type Destination = {
  id: string;
  name: string;
  enabled: boolean;
  events: string[];
};
type RecoveryPlan = {
  plan_hash: string;
  objects: number;
  bytes: number;
  catalog_tables: number;
  mappings: Record<string, string>;
};
const active = new Set([
  "queued",
  "waiting",
  "running",
  "uploading",
  "verifying",
]);
const base = "/operations";

function fresh(projectId: string, connectionId: string): Policy {
  return {
    id: "",
    name: "Nightly recovery",
    project_id: projectId,
    connection_id: connectionId,
    bucket: "",
    prefix: "backups",
    cron: "0 2 * * *",
    timezone: "UTC",
    enabled: true,
    passphrase: "",
    include_data: true,
    storage_sources: [],
    include_catalog: false,
    verify_restore: true,
    notification_ids: [],
    keep_remote: 5,
  };
}

export default function BackupPoliciesPanel({
  projectId,
  admin,
  onError,
}: {
  projectId: string;
  admin: boolean;
  onError: (message: string) => void;
}) {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [notifications, setNotifications] = useState<Destination[]>([]);
  const [runs, setRuns] = useState<Page<BackupRun>>({
    items: [],
    total: 0,
    offset: 0,
    limit: 10,
  });
  const [offset, setOffset] = useState(0);
  const [version, setVersion] = useState(0);
  const [editing, setEditing] = useState<Policy | null>(null);
  const [restore, setRestore] = useState<BackupRun | "storage" | null>(null);
  const [source, setSource] = useState({
    connection_id: "",
    bucket: "",
    key: "",
    passphrase: "",
  });
  const [recoveryOpen, setRecoveryOpen] = useState(false);
  const [mappingText, setMappingText] = useState("{}");
  const [includeCatalog, setIncludeCatalog] = useState(false);
  const [plan, setPlan] = useState<RecoveryPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [restartRequired, setRestartRequired] = useState(false);

  useEffect(() => {
    setOffset(0);
    setEditing(null);
    setRestore(null);
    setRecoveryOpen(false);
  }, [projectId]);
  useEffect(() => {
    if (!admin || restartRequired) return;
    let stopped = false;
    const query = encodeURIComponent(projectId);
    Promise.all([
      api<Policy[]>(`${base}/backup-policies?project_id=${query}`),
      api<Page<BackupRun>>(
        `${base}/backup-runs?project_id=${query}&offset=${offset}&limit=10`,
      ),
      api<Connection[]>(`/connections?project_id=${query}`),
      api<Destination[]>(`/notifications?project_id=${query}`),
      api<Project[]>("/projects"),
    ])
      .then(([policies, runs, connections, notifications, projects]) => {
        if (stopped) return;
        setPolicies(policies);
        setRuns(runs);
        const assigned = new Set(
          projects.find((item) => item.id === projectId)?.connection_ids || [],
        );
        setConnections(
          connections.filter(
            (item) =>
              item.type === "s3" &&
              ((item.project_ids || ["default"]).includes(projectId) ||
                assigned.has(item.id)),
          ),
        );
        setNotifications(notifications);
        setLoading(false);
      })
      .catch((error) => {
        if (!stopped) {
          onError(error.message);
          setLoading(false);
        }
      });
    return () => {
      stopped = true;
    };
  }, [projectId, admin, offset, version, onError, restartRequired]);
  useEffect(() => {
    if (!admin || restartRequired) return;
    const timer = setInterval(() => setVersion((value) => value + 1), 8000);
    return () => clearInterval(timer);
  }, [admin, restartRequired]);

  async function perform(work: () => Promise<void>, success?: string) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await work();
      if (success) setMessage(success);
      setVersion((value) => value + 1);
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function save(event: FormEvent) {
    event.preventDefault();
    if (!editing) return;
    void perform(async () => {
      await api(
        editing.id
          ? `${base}/backup-policies/${editing.id}`
          : `${base}/backup-policies`,
        json(editing.id ? "PUT" : "POST", {
          ...editing,
          project_id: projectId,
        }),
      );
      setEditing(null);
    }, "Backup policy saved.");
  }
  function beginRestore(item: BackupRun | "storage") {
    setSource({
      connection_id:
        item === "storage"
          ? connections[0]?.id || ""
          : item.remote?.connection_id || "",
      bucket: item === "storage" ? "" : item.remote?.bucket || "",
      key: item === "storage" ? "" : item.remote?.key || "",
      passphrase: "",
    });
    setRestore(item);
    setError("");
  }
  function restoreWorkspace(event: FormEvent) {
    event.preventDefault();
    if (!restore) return;
    if (
      !window.confirm(
        "Replace this workspace with the selected backup? Existing notebook history and settings will be replaced. The server will need to restart.",
      )
    )
      return;
    void perform(async () => {
      const result = await api<{ restart_required?: boolean }>(
        restore === "storage"
          ? `${base}/remote-backups/restore`
          : `${base}/backup-runs/${restore.id}/restore`,
        json(
          "POST",
          restore === "storage"
            ? { ...source, project_id: projectId }
            : { passphrase: source.passphrase },
        ),
      );
      setSource((value) => ({ ...value, passphrase: "" }));
      setRestore(null);
      setRestartRequired(Boolean(result.restart_required));
    }, "Workspace restored. Restart the management container, then sign in using the restored accounts.");
  }
  function mappings(): Record<string, string> {
    const parsed = JSON.parse(mappingText);
    if (
      !parsed ||
      typeof parsed !== "object" ||
      Array.isArray(parsed) ||
      Object.values(parsed).some((value) => typeof value !== "string")
    )
      throw new Error(
        "Enter a JSON object that maps original connection IDs to current destination connection IDs.",
      );
    return parsed;
  }
  const connectionName = (id: string) =>
    connections.find((item) => item.id === id)?.name || id;
  if (!admin) return null;
  return (
    <section className="platform-section">
      <div className="section-head">
        <div>
          <h2>Off-host recovery</h2>
          <p>
            Encrypted snapshots, scheduled uploads, and restore checks for this
            project.
          </p>
        </div>
        <div className="head-actions">
          <button
            className="button"
            disabled={busy || restartRequired}
            onClick={() => beginRestore("storage")}
          >
            <ArchiveRestore size={15} />
            Restore from storage
          </button>
          <button
            className="button"
            disabled={busy || restartRequired}
            onClick={() => {
              setRecoveryOpen(true);
              setPlan(null);
            }}
          >
            <ShieldCheck size={15} />
            Recover attached data
          </button>
          <button
            className="button primary"
            disabled={busy || restartRequired || !connections.length}
            onClick={() =>
              setEditing(fresh(projectId, connections[0]?.id || ""))
            }
          >
            <Plus size={15} />
            Backup policy
          </button>
        </div>
      </div>
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      {message && (
        <div className="info-banner" role="status">
          {message}
        </div>
      )}
      {!connections.length && (
        <p className="info-banner">
          Add an S3-compatible connection to this project before configuring
          off-host backups. Use storage on another machine for protection
          against host failure.
        </p>
      )}
      {loading ? (
        <Spinner />
      ) : policies.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Policy</th>
                <th>Destination</th>
                <th>Schedule</th>
                <th>Recovery</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {policies.map((policy) => (
                <tr key={policy.id}>
                  <td>
                    <strong>{policy.name}</strong>
                    <div>
                      <Status status={policy.enabled ? "enabled" : "paused"} />
                    </div>
                  </td>
                  <td>
                    {connectionName(policy.connection_id)}
                    <small className="muted" style={{ display: "block" }}>
                      {policy.bucket}/{policy.prefix}
                    </small>
                  </td>
                  <td>
                    <code>{policy.cron}</code> · {policy.timezone}
                    <small className="muted" style={{ display: "block" }}>
                      Next: {date(policy.next_fire_at)}
                    </small>
                  </td>
                  <td>
                    Keep {policy.keep_remote}
                    <small className="muted" style={{ display: "block" }}>
                      {policy.verify_restore
                        ? "Restore drill enabled"
                        : "Upload verification only"}
                    </small>
                  </td>
                  <td>
                    <div className="row-actions">
                      <button
                        className="button"
                        disabled={busy || restartRequired}
                        onClick={() =>
                          void perform(async () => {
                            await api(
                              `${base}/backup-policies/${policy.id}/run`,
                              json("POST"),
                            );
                          }, "Backup queued. It will start when the workspace is idle.")
                        }
                      >
                        <CloudUpload size={14} />
                        Run
                      </button>
                      <button
                        className="icon-button"
                        aria-label={`Edit ${policy.name}`}
                        disabled={busy || restartRequired}
                        onClick={() =>
                          setEditing({
                            ...policy,
                            passphrase: "",
                            storage_sources: policy.storage_sources.map(
                              (item) => ({ ...item }),
                            ),
                          })
                        }
                      >
                        <Edit3 size={15} />
                      </button>
                      <button
                        className="icon-button"
                        aria-label={`Delete ${policy.name}`}
                        disabled={busy || restartRequired}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Delete backup policy "${policy.name}"? Existing remote archives will remain.`,
                            )
                          )
                            void perform(async () => {
                              await api(
                                `${base}/backup-policies/${policy.id}`,
                                json("DELETE"),
                              );
                            });
                        }}
                      >
                        <Trash2 size={15} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty title="No backup policies" icon={<CloudUpload size={26} />}>
          Create a policy to protect workspace state and selected data on
          S3-compatible storage.
        </Empty>
      )}
      <div className="section-head">
        <div>
          <h3>Backup history</h3>
          <p>
            One upload at a time. Work waits for active notebooks and jobs to
            finish.
          </p>
        </div>
        <button
          className="icon-button"
          aria-label="Refresh backup history"
          disabled={busy || restartRequired}
          onClick={() => setVersion((value) => value + 1)}
        >
          <RefreshCw size={15} />
        </button>
      </div>
      {runs.items.length ? (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Backup</th>
                  <th>Status</th>
                  <th>Archive</th>
                  <th>Restore check</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {runs.items.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <strong>{run.name}</strong>
                      <small className="muted" style={{ display: "block" }}>
                        {date(run.created_at)}
                      </small>
                    </td>
                    <td>
                      <Status status={run.status} />
                      {(run.error ||
                        run.queued_reason ||
                        run.retention_warning) && (
                        <small style={{ display: "block" }}>
                          {run.error ||
                            run.retention_warning ||
                            run.queued_reason}
                        </small>
                      )}
                    </td>
                    <td>
                      {run.remote ? (
                        <>
                          <span>
                            {bytes(run.remote.size)}
                            {run.remote_expired ? " · expired" : " · encrypted"}
                          </span>
                          <small
                            className="muted"
                            style={{
                              display: "block",
                              maxWidth: 300,
                              overflowWrap: "anywhere",
                            }}
                            title={run.remote.sha256}
                          >
                            {run.remote.bucket}/{run.remote.key}
                          </small>
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {run.verification_status ? (
                        <Status status={run.verification_status} />
                      ) : (
                        "Not requested"
                      )}
                      {run.restore_drill?.passed && (
                        <small className="muted" style={{ display: "block" }}>
                          Boot, sign-in, and files checked
                        </small>
                      )}
                      {run.verification_error && (
                        <small style={{ display: "block" }}>
                          {run.verification_error}
                        </small>
                      )}
                    </td>
                    <td>
                      <div className="row-actions">
                        <button
                          className="button"
                          disabled={
                            busy ||
                            restartRequired ||
                            active.has(run.status) ||
                            !run.remote ||
                            run.remote_expired ||
                            ["queued", "running"].includes(
                              run.verification_status || "",
                            )
                          }
                          onClick={() =>
                            void perform(async () => {
                              await api(
                                `${base}/backup-runs/${run.id}/verify`,
                                json("POST"),
                              );
                            }, "Remote restore drill queued.")
                          }
                        >
                          <ShieldCheck size={14} />
                          Verify
                        </button>
                        <button
                          className="button"
                          disabled={
                            busy ||
                            restartRequired ||
                            active.has(run.status) ||
                            !run.remote ||
                            run.remote_expired
                          }
                          onClick={() => beginRestore(run)}
                        >
                          Restore
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            total={runs.total}
            offset={offset}
            limit={10}
            onPage={setOffset}
            busy={busy}
          />
        </>
      ) : (
        <p className="muted">Completed and queued backups will appear here.</p>
      )}
      {editing && (
        <Modal
          title={editing.id ? "Edit backup policy" : "Create backup policy"}
          wide
          onClose={() => {
            if (!busy) setEditing(null);
          }}
        >
          <form className="platform-form" onSubmit={save}>
            <div className="form-grid">
              <Field label="Name">
                <input
                  required
                  maxLength={120}
                  value={editing.name}
                  onChange={(event) =>
                    setEditing({ ...editing, name: event.target.value })
                  }
                />
              </Field>
              <Field label="Destination connection">
                <select
                  required
                  value={editing.connection_id}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      connection_id: event.target.value,
                    })
                  }
                >
                  {connections.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Destination bucket">
                <input
                  required
                  value={editing.bucket}
                  onChange={(event) =>
                    setEditing({ ...editing, bucket: event.target.value })
                  }
                />
              </Field>
              <Field label="Destination prefix">
                <input
                  value={editing.prefix}
                  onChange={(event) =>
                    setEditing({ ...editing, prefix: event.target.value })
                  }
                />
              </Field>
              <CronScheduleFields
                key={editing.id || "new-policy"}
                cron={editing.cron}
                timezone={editing.timezone}
                disabled={busy}
                onCronChange={(cron) =>
                  setEditing((value) => (value ? { ...value, cron } : value))
                }
                onTimezoneChange={(timezone) =>
                  setEditing((value) =>
                    value ? { ...value, timezone } : value,
                  )
                }
              />
              <Field
                label="Encryption passphrase"
                hint={
                  editing.id
                    ? "Leave blank to keep the saved passphrase. Keep a separate copy for recovery on a new host."
                    : "At least 12 characters. Keep a separate copy for recovery on a new host."
                }
              >
                <input
                  type="password"
                  autoComplete="new-password"
                  required={!editing.id}
                  minLength={12}
                  maxLength={1024}
                  value={editing.passphrase || ""}
                  onChange={(event) =>
                    setEditing({ ...editing, passphrase: event.target.value })
                  }
                />
              </Field>
              <Field label="Remote backups to keep">
                <input
                  type="number"
                  min={1}
                  max={100}
                  required
                  value={editing.keep_remote}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      keep_remote: Number(event.target.value),
                    })
                  }
                />
              </Field>
            </div>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={editing.enabled}
                onChange={(event) =>
                  setEditing({ ...editing, enabled: event.target.checked })
                }
              />
              Enable schedule
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={editing.include_data}
                onChange={(event) =>
                  setEditing({ ...editing, include_data: event.target.checked })
                }
              />
              Include local blob data, tables, and notebook files
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={editing.verify_restore}
                onChange={(event) =>
                  setEditing({
                    ...editing,
                    verify_restore: event.target.checked,
                  })
                }
              />
              Boot an isolated restored workspace and check sign-in after each
              upload
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={editing.include_catalog}
                onChange={(event) =>
                  setEditing({
                    ...editing,
                    include_catalog: event.target.checked,
                  })
                }
              />
              Include Unity Catalog table and schema metadata
            </label>
            <p className="muted">
              Restore checks do not execute notebooks or contact external
              databases. SQL Server data needs its own database backup. Pause
              external object writers while taking a coordinated snapshot.
            </p>
            <h3>Attach selected storage data</h3>
            {editing.storage_sources.map((item, index) => (
              <div className="form-grid" key={index}>
                <Field label={`Source ${index + 1} connection`}>
                  <select
                    required
                    value={item.connection_id}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        storage_sources: editing.storage_sources.map(
                          (source, position) =>
                            position === index
                              ? { ...source, connection_id: event.target.value }
                              : source,
                        ),
                      })
                    }
                  >
                    {connections.map((connection) => (
                      <option key={connection.id} value={connection.id}>
                        {connection.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Source bucket">
                  <input
                    required
                    value={item.bucket}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        storage_sources: editing.storage_sources.map(
                          (source, position) =>
                            position === index
                              ? { ...source, bucket: event.target.value }
                              : source,
                        ),
                      })
                    }
                  />
                </Field>
                <Field label="Source prefix">
                  <input
                    value={item.prefix}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        storage_sources: editing.storage_sources.map(
                          (source, position) =>
                            position === index
                              ? { ...source, prefix: event.target.value }
                              : source,
                        ),
                      })
                    }
                  />
                </Field>
                <button
                  type="button"
                  className="button"
                  onClick={() =>
                    setEditing({
                      ...editing,
                      storage_sources: editing.storage_sources.filter(
                        (_, position) => position !== index,
                      ),
                    })
                  }
                >
                  Remove source
                </button>
              </div>
            ))}
            <button
              type="button"
              className="button"
              disabled={editing.storage_sources.length >= 20}
              onClick={() =>
                setEditing({
                  ...editing,
                  storage_sources: [
                    ...editing.storage_sources,
                    {
                      connection_id: connections[0]?.id || "",
                      bucket: "",
                      prefix: "",
                    },
                  ],
                })
              }
            >
              <Plus size={14} />
              Attach storage source
            </button>
            <p className="muted">
              Selected sources are copied into the encrypted archive. Maximum
              10,000 objects and 20 GiB. The backup destination must sit outside
              source prefixes.
            </p>
            <h3>Notifications</h3>
            {notifications.length ? (
              notifications.map((target) => (
                <label className="checkbox" key={target.id}>
                  <input
                    type="checkbox"
                    checked={editing.notification_ids.includes(target.id)}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        notification_ids: event.target.checked
                          ? [...editing.notification_ids, target.id]
                          : editing.notification_ids.filter(
                              (id) => id !== target.id,
                            ),
                      })
                    }
                  />
                  {target.name}
                  {!target.enabled ? " (disabled)" : ""}
                  <small className="muted">
                    {" "}
                    ·{" "}
                    {target.events
                      .filter((event) =>
                        ["failed", "succeeded"].includes(event),
                      )
                      .join(", ") ||
                      "Enable failed or succeeded events in notification settings"}
                  </small>
                </label>
              ))
            ) : (
              <p className="muted">
                Create a notification target in Operations to receive backup
                failure and success messages.
              </p>
            )}
            {error && (
              <p className="error-banner" role="alert">
                {error}
              </p>
            )}
            <div className="modal-actions">
              <button
                type="button"
                className="button"
                disabled={busy}
                onClick={() => setEditing(null)}
              >
                Cancel
              </button>
              <button className="button primary" disabled={busy}>
                {busy && <Spinner />}Save policy
              </button>
            </div>
          </form>
        </Modal>
      )}
      {restore && (
        <Modal
          title="Restore workspace from storage"
          onClose={() => {
            if (!busy) {
              setRestore(null);
              setSource((value) => ({ ...value, passphrase: "" }));
            }
          }}
        >
          <form className="platform-form" onSubmit={restoreWorkspace}>
            <p>
              OpenBricks downloads and validates the encrypted archive on the
              server. Restoring replaces the current workspace and requires a
              server restart.
            </p>
            <Field label="Storage connection">
              <select
                required
                disabled={restore !== "storage" || busy}
                value={source.connection_id}
                onChange={(event) =>
                  setSource({ ...source, connection_id: event.target.value })
                }
              >
                <option value="">Choose storage</option>
                {connections.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Bucket">
              <input
                required
                disabled={restore !== "storage" || busy}
                value={source.bucket}
                onChange={(event) =>
                  setSource({ ...source, bucket: event.target.value })
                }
              />
            </Field>
            <Field
              label="Archive object key"
              hint="The .obbackup object key from your backup receipt. No prior run history is required."
            >
              <input
                required
                disabled={restore !== "storage" || busy}
                value={source.key}
                onChange={(event) =>
                  setSource({ ...source, key: event.target.value })
                }
              />
            </Field>
            <Field label="Backup passphrase">
              <input
                type="password"
                required
                minLength={12}
                disabled={busy}
                autoComplete="off"
                value={source.passphrase}
                onChange={(event) =>
                  setSource({ ...source, passphrase: event.target.value })
                }
              />
            </Field>
            {error && (
              <p className="error-banner" role="alert">
                {error}
              </p>
            )}
            <div className="modal-actions">
              <button
                type="button"
                className="button"
                disabled={busy}
                onClick={() => {
                  setRestore(null);
                  setSource((value) => ({ ...value, passphrase: "" }));
                }}
              >
                Cancel
              </button>
              <button className="button danger" disabled={busy}>
                {busy ? <Spinner /> : <ArchiveRestore size={15} />}Replace
                workspace
              </button>
            </div>
          </form>
        </Modal>
      )}
      {recoveryOpen && (
        <Modal
          title="Recover attached storage and catalog"
          wide
          onClose={() => {
            if (!busy) setRecoveryOpen(false);
          }}
        >
          <form
            className="platform-form"
            onSubmit={(event) => {
              event.preventDefault();
              void perform(async () => {
                setPlan(
                  await api<RecoveryPlan>(
                    `${base}/recovery/plan`,
                    json("POST", {
                      project_id: projectId,
                      mappings: mappings(),
                      include_catalog: includeCatalog,
                    }),
                  ),
                );
              });
            }}
          >
            <p>
              After restoring a workspace archive, preview its attached data and
              map each original storage connection to a current destination.
              Create empty destination buckets first. Existing objects and
              catalog entries are preserved.
            </p>
            <Field
              label="Connection mapping (JSON)"
              hint={
                'Example: {"original-storage-id": "new-storage-id"}. Use {} if the archive contains only catalog metadata.'
              }
            >
              <textarea
                rows={4}
                spellCheck={false}
                value={mappingText}
                onChange={(event) => {
                  setMappingText(event.target.value);
                  setPlan(null);
                }}
              />
            </Field>
            <p className="muted">
              Current storage IDs:{" "}
              {connections
                .map((item) => `${item.name}: ${item.id}`)
                .join("; ") || "No storage connections"}
            </p>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={includeCatalog}
                onChange={(event) => {
                  setIncludeCatalog(event.target.checked);
                  setPlan(null);
                }}
              />
              Recover Unity Catalog metadata to the configured catalog service
            </label>
            {plan && (
              <div className="info-banner">
                Ready to recover {plan.objects.toLocaleString()} objects (
                {bytes(plan.bytes)}) and {plan.catalog_tables.toLocaleString()}{" "}
                catalog tables. Buckets and object keys retain their original
                names.
              </div>
            )}
            {error && (
              <p className="error-banner" role="alert">
                {error}
              </p>
            )}
            <div className="modal-actions">
              <button
                type="button"
                className="button"
                disabled={busy}
                onClick={() => setRecoveryOpen(false)}
              >
                Close
              </button>
              <button className="button" disabled={busy}>
                {busy && <Spinner />}Preview recovery
              </button>
              <button
                type="button"
                className="button primary"
                disabled={busy || !plan}
                onClick={() => {
                  if (
                    !plan ||
                    !window.confirm(
                      "Recover the selected attached data and catalog metadata now? This operation requires an idle workspace.",
                    )
                  )
                    return;
                  void perform(async () => {
                    const result = await api<{
                      objects_restored: number;
                      catalog_objects_created: number;
                    }>(
                      `${base}/recovery/apply`,
                      json("POST", {
                        project_id: projectId,
                        mappings: mappings(),
                        include_catalog: includeCatalog,
                        plan_hash: plan.plan_hash,
                      }),
                    );
                    setRecoveryOpen(false);
                    setMessage(
                      `Recovered ${result.objects_restored} storage objects and ${result.catalog_objects_created} catalog objects.`,
                    );
                  });
                }}
              >
                Recover attachments
              </button>
            </div>
          </form>
        </Modal>
      )}
    </section>
  );
}
