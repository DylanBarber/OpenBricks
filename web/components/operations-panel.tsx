"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArchiveRestore,
  Bell,
  Download,
  HardDrive,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  Upload,
} from "lucide-react";
import { api, bytes, date, json } from "@/lib/api";
import { Empty, Field, Modal, SectionHead, Spinner, Status } from "./ui";
type Retention = {
  output_days: number;
  run_days: number;
  max_output_mb: number;
  backup_keep: number;
};
type Backup = {
  id: string;
  name: string;
  status: string;
  created_at?: string;
  size?: number;
  encrypted?: boolean;
  error?: string;
  includes?: string[];
  excludes?: string[];
};
type Notification = {
  project_id?: string;
  id: string;
  name: string;
  type: "webhook" | "email";
  config: {
    url?: string;
    endpoint?: string;
    allow_insecure?: boolean;
    slow_after_seconds?: number;
    host?: string;
    port?: number;
    sender?: string;
    recipients?: string[];
    username?: string;
    starttls?: boolean;
  };
  enabled: boolean;
  events: string[];
  secret_fields?: string[];
};
type Operations = {
  management: {
    memory_used: number;
    memory_limit: number;
    cpu_percent: number;
  };
  disk: { total: number; used: number; free: number };
  counts: {
    runs: number;
    active_runs: number;
    queued_runs: number;
    backups: number;
  };
  clusters: {
    id: string;
    name: string;
    status: string;
    memory_used: number;
    memory_total: number;
    active_runs: number;
    queued_runs: number;
  }[];
  retention: Retention;
  last_cleanup?: unknown;
};
export default function OperationsPanel({
  projectId,
  admin,
  onError,
}: {
  projectId: string;
  admin: boolean;
  onError: (message: string) => void;
}) {
  const policyDirty = useRef(false);
  const [overview, setOverview] = useState<Operations | null>(null),
    [retention, setRetention] = useState<Retention>({
      output_days: 30,
      run_days: 90,
      max_output_mb: 500,
      backup_keep: 5,
    }),
    [preview, setPreview] = useState<{
      outputs_to_clear: number;
      runs_to_delete: number;
      bytes_to_reclaim: number;
    } | null>(null),
    [backups, setBackups] = useState<Backup[]>([]),
    [notifications, setNotifications] = useState<Notification[]>([]),
    [editing, setEditing] = useState<Notification | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [version, setVersion] = useState(0),
    [backupOpen, setBackupOpen] = useState(false),
    [restoreOpen, setRestoreOpen] = useState(false),
    [passphrase, setPassphrase] = useState(""),
    [secret, setSecret] = useState(""),
    [restoreFile, setRestoreFile] = useState<File | null>(null);
  useEffect(() => {
    if (!admin) return;
    let stopped = false;
    Promise.all([
      api<Operations>("/operations/overview"),
      api<Backup[]>("/backups"),
      api<Notification[]>(`/notifications?project_id=${projectId}`),
    ])
      .then(([overview, backups, notifications]) => {
        if (!stopped) {
          setOverview(overview);
          if (!policyDirty.current) setRetention(overview.retention);
          setBackups(backups);
          setNotifications(notifications);
        }
      })
      .catch((error) => {
        if (!stopped) onError(error.message);
      });
    return () => {
      stopped = true;
    };
  }, [admin, version, projectId, onError]);
  useEffect(() => {
    if (!admin) return;
    const timer = setInterval(() => setVersion((value) => value + 1), 15000);
    return () => clearInterval(timer);
  }, [admin]);
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
  function saveNotification(event: FormEvent) {
    event.preventDefault();
    if (!editing) return;
    void perform(async () => {
      await api(
        editing.id ? `/notifications/${editing.id}` : "/notifications",
        json(editing.id ? "PUT" : "POST", {
          ...editing,
          project_id: editing.project_id || projectId,
          secrets:
            editing.type === "webhook"
              ? { token: secret }
              : { password: secret },
        }),
      );
      setEditing(null);
      setSecret("");
    }, "Notification target saved.");
  }
  if (!admin)
    return (
      <section className="card">
        <Empty
          icon={<ShieldCheck size={26} />}
          title="Administrator access required"
        >
          Contact a workspace administrator to manage retention, backups, and
          notification targets.
        </Empty>
      </section>
    );
  return (
    <>
      <SectionHead
        eyebrow="KEEP YOUR WORKSPACE HEALTHY"
        title="Operations"
        description="Watch resource use, retain the history you need, and protect your local workspace."
        actions={
          <button
            className="button"
            onClick={() => setVersion((value) => value + 1)}
          >
            <RefreshCw size={14} />
            Refresh
          </button>
        }
      />
      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}
      {message ? (
        <div className="inline-success" role="status">
          {message}
        </div>
      ) : null}
      <div className="operations-stats">
        <section className="card">
          <span>Management memory</span>
          <strong>{bytes(overview?.management.memory_used)}</strong>
          <small>Limit {bytes(overview?.management.memory_limit)}</small>
        </section>
        <section className="card">
          <span>Local disk free</span>
          <strong>{bytes(overview?.disk.free)}</strong>
          <small>{bytes(overview?.disk.used)} used</small>
        </section>
        <section className="card">
          <span>Active / queued</span>
          <strong>
            {overview?.counts.active_runs ?? "—"} /{" "}
            {overview?.counts.queued_runs ?? "—"}
          </strong>
          <small>{overview?.counts.runs ?? 0} recorded runs</small>
        </section>
      </div>
      <section className="card">
        <div className="card-heading">
          <h3>Compute health</h3>
          <HardDrive size={18} />
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Target</th>
              <th>Status</th>
              <th>Memory</th>
              <th>Active / queued</th>
            </tr>
          </thead>
          <tbody>
            {overview?.clusters.map((cluster) => (
              <tr key={cluster.id}>
                <td>{cluster.name}</td>
                <td>
                  <Status status={cluster.status} />
                </td>
                <td>
                  {bytes(cluster.memory_used)} / {bytes(cluster.memory_total)}
                </td>
                <td>
                  {cluster.active_runs} / {cluster.queued_runs}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="form-note padded">
          Container memory limits and Spark driver sizing are configured on each
          host. Idle compute releases its interpreter memory according to the
          compute target's shutdown policy.
        </p>
      </section>
      <section className="card platform-form">
        <h3>Retention policy</h3>
        <p className="form-note">
          Preview cleanup before applying it. Active runs are preserved.
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void perform(async () => {
              await api("/operations/retention", json("PUT", retention));
              policyDirty.current = false;
              setPreview(null);
            }, "Retention policy saved.");
          }}
        >
          <div className="form-grid">
            {[
              ["output_days", "Keep cell outputs (days)"],
              ["run_days", "Keep run records (days)"],
              ["max_output_mb", "Output storage budget (MB)"],
              ["backup_keep", "Keep newest backups"],
            ].map(([key, label]) => (
              <Field key={key} label={label}>
                <input
                  type="number"
                  min={1}
                  required
                  value={retention[key as keyof Retention]}
                  onChange={(event) => {
                    policyDirty.current = true;
                    setRetention({
                      ...retention,
                      [key]: Number(event.target.value),
                    });
                    setPreview(null);
                  }}
                />
              </Field>
            ))}
          </div>
          <div className="row-actions">
            <button className="button primary" disabled={busy}>
              <Save size={14} />
              Save policy
            </button>
            <button
              type="button"
              className="button"
              disabled={busy}
              onClick={() =>
                void perform(async () =>
                  setPreview(await api("/operations/retention/preview")),
                )
              }
            >
              Preview cleanup
            </button>
          </div>
        </form>
        {preview ? (
          <div className="retention-preview">
            <strong>
              {preview.outputs_to_clear} outputs · {preview.runs_to_delete} run
              records · {bytes(preview.bytes_to_reclaim)} to reclaim
            </strong>
            <button
              className="button danger"
              disabled={busy}
              onClick={() => {
                if (
                  window.confirm(
                    "Apply the saved retention policy and permanently remove the previewed history?",
                  )
                )
                  void perform(async () => {
                    const result = await api(
                      "/operations/retention/run",
                      json("POST"),
                    );
                    setMessage(JSON.stringify(result));
                    setPreview(null);
                  });
              }}
            >
              Apply cleanup
            </button>
          </div>
        ) : null}
      </section>
      <section className="card">
        <div className="card-heading">
          <div>
            <h3>Workspace backups</h3>
            <p>
              Includes workspace metadata, local objects, packages, and local
              warehouse data.
            </p>
          </div>
          <div className="row-actions">
            <button
              className="button compact"
              onClick={() => {
                setRestoreOpen(true);
                setPassphrase("");
                setRestoreFile(null);
              }}
            >
              <Upload size={13} />
              Restore
            </button>
            <button
              className="button primary compact"
              onClick={() => {
                setBackupOpen(true);
                setPassphrase("");
              }}
            >
              <Plus size={13} />
              Create backup
            </button>
          </div>
        </div>
        {backups.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Backup</th>
                <th>Status</th>
                <th>Created</th>
                <th>Size</th>
                <th>Download</th>
              </tr>
            </thead>
            <tbody>
              {backups.map((backup) => (
                <tr key={backup.id}>
                  <td>
                    {backup.name}
                    <small className="table-subline">
                      {backup.encrypted
                        ? "Passphrase encrypted"
                        : "Unencrypted archive"}
                    </small>
                    {backup.error ? (
                      <small className="failure-text">{backup.error}</small>
                    ) : null}
                  </td>
                  <td>
                    <Status status={backup.status} />
                  </td>
                  <td>{date(backup.created_at)}</td>
                  <td>{bytes(backup.size)}</td>
                  <td>
                    {["complete", "completed", "succeeded", "ready"].includes(
                      backup.status,
                    ) ? (
                      <a
                        className="button compact"
                        href={`/api/backups/${backup.id}/download`}
                      >
                        <Download size={13} />
                        Download
                      </a>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty title="Protect your first workspace backup">
            Keep a backup outside this server before making major changes.
          </Empty>
        )}
        <p className="form-note padded">
          External S3 data and external catalog databases need their own
          backups. A Docker image export does not back up workspace volumes.
        </p>
      </section>
      <section className="card">
        <div className="card-heading">
          <div>
            <h3>Notifications</h3>
            <p>
              Choose webhook or email destinations, then attach them to jobs and
              workflows.
            </p>
          </div>
          <button
            className="button primary compact"
            onClick={() => {
              setEditing({
                project_id: projectId,
                id: "",
                name: "",
                type: "webhook",
                config: {},
                enabled: true,
                events: ["failed"],
              });
              setSecret("");
            }}
          >
            <Bell size={13} />
            Add target
          </button>
        </div>
        {notifications.map((item) => (
          <div className="notification-row" key={item.id}>
            <div>
              <button
                className="name-link"
                onClick={() => {
                  setEditing(item);
                  setSecret("");
                }}
              >
                {item.name}
              </button>
              <small>
                {item.type} · {item.events.join(", ")}
              </small>
            </div>
            <Status status={item.enabled ? "active" : "disabled"} />
            <button
              className="button compact"
              disabled={busy}
              onClick={() =>
                void perform(async () => {
                  await api(`/notifications/${item.id}/test`, json("POST"));
                }, "Test notification sent.")
              }
            >
              Send test
            </button>
            <button
              className="icon-button"
              disabled={busy}
              aria-label={`Delete notification ${item.name}`}
              onClick={() => {
                if (
                  window.confirm(`Delete notification target “${item.name}”?`)
                )
                  void perform(async () => {
                    await api(`/notifications/${item.id}`, json("DELETE"));
                  });
              }}
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
        {!notifications.length ? (
          <Empty title="Keep the right people informed">
            Add a destination for failed or slow-run alerts.
          </Empty>
        ) : null}
      </section>
      {backupOpen ? (
        <Modal
          title="Create a workspace backup"
          onClose={() => {
            if (!busy) setBackupOpen(false);
          }}
        >
          <form
            className="modal-form"
            onSubmit={(event) => {
              event.preventDefault();
              const values = new FormData(event.currentTarget);
              void perform(async () => {
                await api(
                  "/backups",
                  json("POST", {
                    name: values.get("name") || undefined,
                    passphrase: passphrase || undefined,
                    include_data: true,
                  }),
                );
                setBackupOpen(false);
                setPassphrase("");
              }, "Backup started. Its status will update here.");
            }}
          >
            <Field label="Backup name">
              <input name="name" placeholder="Before pipeline changes" />
            </Field>
            <Field
              label="Encryption passphrase (optional)"
              hint="Use at least 12 characters and keep it separately. It is required to restore an encrypted archive."
            >
              <input
                type="password"
                minLength={12}
                value={passphrase}
                onChange={(event) => setPassphrase(event.target.value)}
                autoComplete="new-password"
              />
            </Field>
            <p className="form-note">
              Backups include saved credentials and local data. Protect
              downloaded archives. External storage and catalog services are
              excluded.
            </p>
            <div className="modal-footer">
              <button className="button primary" disabled={busy}>
                {busy ? <Spinner /> : <ArchiveRestore size={14} />}Create backup
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {restoreOpen ? (
        <Modal
          title="Restore a workspace backup"
          onClose={() => {
            if (!busy) setRestoreOpen(false);
          }}
        >
          <form
            className="modal-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (
                !restoreFile ||
                !window.confirm(
                  "Replace the local workspace with this backup? This can overwrite current notebooks, settings, and local data. Ensure you have a current backup first.",
                )
              )
                return;
              void perform(async () => {
                const form = new FormData();
                form.append("file", restoreFile);
                if (passphrase) form.append("passphrase", passphrase);
                const result = await api<{
                  message: string;
                  restart_required: boolean;
                }>("/backups/restore", { method: "POST", body: form });
                setMessage(
                  result.message +
                    (result.restart_required
                      ? " Restart the services before continuing."
                      : ""),
                );
                setRestoreOpen(false);
                setPassphrase("");
              });
            }}
          >
            <div className="inline-error">
              Restoring replaces local workspace data. Stop other work and make
              a fresh backup first. External S3 storage and external catalogs
              are not restored.
            </div>
            <Field label="Backup archive">
              <input
                type="file"
                required
                onChange={(event) =>
                  setRestoreFile(event.target.files?.[0] || null)
                }
              />
            </Field>
            <Field label="Encryption passphrase">
              <input
                type="password"
                value={passphrase}
                onChange={(event) => setPassphrase(event.target.value)}
                autoComplete="off"
              />
            </Field>
            <div className="modal-footer">
              <button className="button danger" disabled={busy || !restoreFile}>
                Restore local workspace
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {editing ? (
        <Modal
          title={
            editing.id ? "Edit notification target" : "Add notification target"
          }
          onClose={() => setEditing(null)}
        >
          <form className="modal-form" onSubmit={saveNotification}>
            <Field label="Name">
              <input
                required
                value={editing.name}
                onChange={(event) =>
                  setEditing({ ...editing, name: event.target.value })
                }
              />
            </Field>
            <Field label="Delivery">
              <select
                disabled={!!editing.id}
                value={editing.type}
                onChange={(event) => {
                  setEditing({
                    ...editing,
                    type: event.target.value as Notification["type"],
                    config: {},
                  });
                  setSecret("");
                }}
              >
                <option value="webhook">Webhook</option>
                <option value="email">Email (SMTP)</option>
              </select>
            </Field>
            {editing.type === "webhook" ? (
              <>
                <Field label="Webhook URL">
                  <input
                    type="url"
                    required={!editing.id}
                    placeholder={
                      editing.id
                        ? "Leave blank to keep the stored URL"
                        : "https://…"
                    }
                    value={editing.config.url || ""}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        config: { ...editing.config, url: event.target.value },
                      })
                    }
                  />
                </Field>
                <p className="form-note">
                  {editing.config.endpoint
                    ? `Stored destination: ${editing.config.endpoint}. Full paths and query values are kept encrypted.`
                    : "Webhook URLs are encrypted when saved."}
                </p>
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={!!editing.config.allow_insecure}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        config: {
                          ...editing.config,
                          allow_insecure: event.target.checked,
                        },
                      })
                    }
                  />
                  Allow HTTP for a trusted local destination
                </label>
                <Field
                  label="Bearer token"
                  hint={
                    editing.secret_fields?.length
                      ? "Leave blank to preserve the stored token."
                      : "Optional."
                  }
                >
                  <input
                    type="password"
                    value={secret}
                    onChange={(event) => setSecret(event.target.value)}
                    autoComplete="new-password"
                  />
                </Field>
              </>
            ) : (
              <>
                <div className="form-grid">
                  <Field label="SMTP host">
                    <input
                      required
                      value={editing.config.host || ""}
                      onChange={(event) =>
                        setEditing({
                          ...editing,
                          config: {
                            ...editing.config,
                            host: event.target.value,
                          },
                        })
                      }
                    />
                  </Field>
                  <Field label="Port">
                    <input
                      type="number"
                      min={1}
                      max={65535}
                      value={editing.config.port || 587}
                      onChange={(event) =>
                        setEditing({
                          ...editing,
                          config: {
                            ...editing.config,
                            port: Number(event.target.value),
                          },
                        })
                      }
                    />
                  </Field>
                  <Field label="Sender">
                    <input
                      type="email"
                      required
                      value={editing.config.sender || ""}
                      onChange={(event) =>
                        setEditing({
                          ...editing,
                          config: {
                            ...editing.config,
                            sender: event.target.value,
                          },
                        })
                      }
                    />
                  </Field>
                  <Field label="Recipients (comma separated)">
                    <input
                      required
                      value={editing.config.recipients?.join(", ") || ""}
                      onChange={(event) =>
                        setEditing({
                          ...editing,
                          config: {
                            ...editing.config,
                            recipients: event.target.value
                              .split(",")
                              .map((value) => value.trim())
                              .filter(Boolean),
                          },
                        })
                      }
                    />
                  </Field>
                  <Field label="Username (optional)">
                    <input
                      value={editing.config.username || ""}
                      onChange={(event) =>
                        setEditing({
                          ...editing,
                          config: {
                            ...editing.config,
                            username: event.target.value,
                          },
                        })
                      }
                    />
                  </Field>
                  <Field
                    label="SMTP password"
                    hint={
                      editing.secret_fields?.length
                        ? "Leave blank to preserve the stored password."
                        : "Optional."
                    }
                  >
                    <input
                      type="password"
                      value={secret}
                      onChange={(event) => setSecret(event.target.value)}
                      autoComplete="new-password"
                    />
                  </Field>
                </div>
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={editing.config.starttls !== false}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        config: {
                          ...editing.config,
                          starttls: event.target.checked,
                        },
                      })
                    }
                  />
                  Use STARTTLS
                </label>
              </>
            )}
            <div className="connection-checklist">
              {[
                ["failed", "Failed runs"],
                ["slow", "Slow runs"],
                ["succeeded", "Successful runs"],
              ].map(([value, label]) => (
                <label key={value} className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={editing.events.includes(value)}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        events: event.target.checked
                          ? [...editing.events, value]
                          : editing.events.filter((item) => item !== value),
                      })
                    }
                  />
                  {label}
                </label>
              ))}
            </div>
            <Field label="Slow-run threshold (seconds)">
              <input
                type="number"
                min={30}
                max={86400}
                value={editing.config.slow_after_seconds || 300}
                onChange={(event) =>
                  setEditing({
                    ...editing,
                    config: {
                      ...editing.config,
                      slow_after_seconds: Number(event.target.value),
                    },
                  })
                }
              />
            </Field>
            <label className="checkbox-field">
              <input
                type="checkbox"
                checked={editing.enabled}
                onChange={(event) =>
                  setEditing({ ...editing, enabled: event.target.checked })
                }
              />
              Enable delivery
            </label>
            {error ? <div className="inline-error">{error}</div> : null}
            <div className="modal-footer">
              <button className="button primary" disabled={busy}>
                Save target
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
    </>
  );
}
