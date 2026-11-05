"use client";
import { useEffect, useState, type FormEvent } from "react";
import { Bot, Copy, KeyRound, Plus, RefreshCw, Trash2 } from "lucide-react";
import { api, date, json, type Project } from "@/lib/api";
import { Empty, Field, Modal, SectionHead, Spinner } from "./ui";

type Account = {
  id: string;
  username: string;
  description?: string;
  grants: { project_id: string; role: string }[];
};
type Token = {
  id: string;
  name: string;
  prefix: string;
  actions: string[];
  project_ids: string[];
  expires_at: string;
  last_used_at?: string;
  revoked_at?: string;
};
export default function ServiceAccountsPanel({
  projects,
  projectId = "default",
  isAdmin,
  onError,
}: {
  projects: Project[];
  projectId?: string;
  isAdmin: boolean;
  onError: (message: string) => void;
}) {
  const [accounts, setAccounts] = useState<Account[] | null>(null),
    [version, setVersion] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [create, setCreate] = useState(false),
    [selected, setSelected] = useState<Account | null>(null),
    [tokens, setTokens] = useState<Token[]>([]),
    [issued, setIssued] = useState("");
  const [name, setName] = useState(""),
    [description, setDescription] = useState(""),
    [grants, setGrants] = useState<Record<string, string>>({
      [projectId]: "operator",
    }),
    [tokenName, setTokenName] = useState(""),
    [tokenProjects, setTokenProjects] = useState<string[]>([]),
    [permission, setPermission] = useState("run"),
    [days, setDays] = useState(30);
  useEffect(() => {
    if (!isAdmin) return;
    let stopped = false;
    api<Account[]>("/service-accounts")
      .then((value) => {
        if (!stopped) setAccounts(value);
      })
      .catch((failure: Error) => {
        if (!stopped) setError(failure.message);
      });
    return () => {
      stopped = true;
    };
  }, [isAdmin, version]);
  useEffect(() => {
    if (!selected) return;
    let stopped = false;
    api<Token[]>(`/service-accounts/${selected.id}/tokens`)
      .then((value) => {
        if (!stopped) setTokens(value);
      })
      .catch((failure: Error) => {
        if (!stopped) setError(failure.message);
      });
    return () => {
      stopped = true;
    };
  }, [selected, version]);
  useEffect(() => {
    setSelected(null);
    setIssued("");
    setCreate(false);
    setError("");
  }, [projectId]);
  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await work();
      setVersion((value) => value + 1);
    } catch (failure) {
      const value = (failure as Error).message;
      setError(value);
      onError(value);
    } finally {
      setBusy(false);
    }
  }
  const projectName = (id: string) =>
    projects.find((item) => item.id === id)?.name || id;
  async function save(event: FormEvent) {
    event.preventDefault();
    await perform(async () => {
      await api(
        "/service-accounts",
        json("POST", {
          name,
          description,
          grants: Object.entries(grants).map(([project_id, role]) => ({
            project_id,
            role,
          })),
        }),
      );
      setCreate(false);
    });
  }
  async function issue(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    await perform(async () => {
      const value = await api<Token & { token: string }>(
        `/service-accounts/${selected.id}/tokens`,
        json("POST", {
          name: tokenName,
          project_ids: tokenProjects,
          actions: [permission],
          expires_in_days: days,
        }),
      );
      setIssued(value.token);
      setTokenName("");
    });
  }
  if (!isAdmin)
    return (
      <Empty title="Administrator access required">
        Workspace administrators manage automation identities and their project
        grants.
      </Empty>
    );
  return (
    <div className="platform-stack">
      <SectionHead
        eyebrow="AUTOMATE WITH A DEDICATED IDENTITY"
        title="Service accounts"
        description="Give CI and external tools scoped, expiring access without sharing a person's login password."
        actions={
          <>
            <button
              className="button"
              disabled={busy}
              onClick={() => setVersion((value) => value + 1)}
            >
              <RefreshCw size={15} />
              Refresh
            </button>
            <button
              className="button primary"
              onClick={() => {
                setName("");
                setDescription("");
                setGrants({ [projectId]: "operator" });
                setError("");
                setCreate(true);
              }}
            >
              <Plus size={15} />
              New service account
            </button>
          </>
        }
      />
      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}
      <section className="card">
        {accounts?.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Account</th>
                <th>Project grants</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((account) => (
                <tr key={account.id}>
                  <td>
                    <strong>{account.username}</strong>
                    <div className="muted">{account.description}</div>
                  </td>
                  <td>
                    {account.grants.map((grant) => (
                      <div key={grant.project_id}>
                        {projectName(grant.project_id)} · {grant.role}
                      </div>
                    ))}
                  </td>
                  <td>
                    <div className="row-actions">
                      <button
                        className="button compact"
                        onClick={() => {
                          setSelected(account);
                          setTokens([]);
                          setIssued("");
                          setTokenName("");
                          setTokenProjects(
                            account.grants.map((grant) => grant.project_id),
                          );
                          setPermission(
                            account.grants.some(
                              (grant) => grant.role === "viewer",
                            )
                              ? "read"
                              : "run",
                          );
                        }}
                      >
                        <KeyRound size={14} />
                        API tokens
                      </button>
                      <button
                        className="icon-button"
                        aria-label={`Delete service account ${account.username}`}
                        disabled={busy}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Delete ${account.username}? Its tokens are revoked and future job admissions will fail. Running work may finish.`,
                            )
                          )
                            void perform(async () => {
                              await api(
                                `/service-accounts/${account.id}`,
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
        ) : accounts ? (
          <Empty
            icon={<Bot size={28} />}
            title="Create your first automation identity"
          >
            Assign only the projects and permissions that the automation needs.
          </Empty>
        ) : (
          <Spinner />
        )}
      </section>
      {create ? (
        <Modal
          title="Create service account"
          onClose={() => {
            if (!busy) setCreate(false);
          }}
        >
          <form className="modal-form" onSubmit={save}>
            <Field label="Account name">
              <input
                required
                autoFocus
                pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,59}"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="sales-ci"
              />
            </Field>
            <Field label="Description">
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </Field>
            <h3>Project grants</h3>
            {projects.map((project) => (
              <div className="form-grid" key={project.id}>
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={project.id in grants}
                    onChange={(event) =>
                      setGrants((previous) => {
                        const next = { ...previous };
                        if (event.target.checked) next[project.id] = "operator";
                        else delete next[project.id];
                        return next;
                      })
                    }
                  />
                  <span>{project.name}</span>
                </label>
                <select
                  aria-label={`Role in ${project.name}`}
                  disabled={!(project.id in grants)}
                  value={grants[project.id] || "operator"}
                  onChange={(event) =>
                    setGrants((value) => ({
                      ...value,
                      [project.id]: event.target.value,
                    }))
                  }
                >
                  <option value="viewer">Viewer · read metadata/source</option>
                  <option value="operator">Operator · read and run</option>
                  <option value="editor">Editor · deploy, edit, and run</option>
                </select>
              </div>
            ))}
            <p className="form-note">
              Service accounts cannot sign in with a password or become
              workspace administrators. Existing schedules run as this identity
              using its current project membership.
            </p>
            {error ? <div className="inline-error">{error}</div> : null}
            <div className="modal-footer">
              <button
                type="button"
                className="button"
                disabled={busy}
                onClick={() => setCreate(false)}
              >
                Cancel
              </button>
              <button
                className="button primary"
                disabled={busy || !Object.keys(grants).length}
              >
                {busy ? <Spinner /> : null}Create account
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {selected ? (
        <Modal
          title={`API tokens · ${selected.username}`}
          wide
          onClose={() => {
            if (!busy) {
              setSelected(null);
              setIssued("");
            }
          }}
        >
          <div className="modal-form">
            {issued ? (
              <section className="card platform-form">
                <h3>Copy this token now</h3>
                <p className="form-note">
                  It is shown once. Keep it in your CI secret store; it cannot
                  be retrieved after closing this dialog.
                </p>
                <Field label="New automation token">
                  <textarea readOnly rows={2} value={issued} />
                </Field>
                <button
                  className="button"
                  onClick={() =>
                    void navigator.clipboard
                      .writeText(issued)
                      .catch(() =>
                        setError(
                          "Select the token above and copy it manually.",
                        ),
                      )
                  }
                >
                  <Copy size={14} />
                  Copy token
                </button>
              </section>
            ) : null}
            <form onSubmit={issue}>
              <div className="form-grid">
                <Field label="Token name">
                  <input
                    required
                    value={tokenName}
                    onChange={(event) => setTokenName(event.target.value)}
                    placeholder="GitHub Actions"
                  />
                </Field>
                <Field label="Expires after (days)">
                  <input
                    type="number"
                    required
                    min={1}
                    max={365}
                    value={days}
                    onChange={(event) => setDays(Number(event.target.value))}
                  />
                </Field>
                <Field label="Permissions">
                  <select
                    value={permission}
                    onChange={(event) => setPermission(event.target.value)}
                  >
                    <option value="read">Read</option>
                    <option
                      value="run"
                      disabled={selected.grants.some(
                        (grant) =>
                          tokenProjects.includes(grant.project_id) &&
                          grant.role === "viewer",
                      )}
                    >
                      Read and run
                    </option>
                    <option
                      value="edit"
                      disabled={selected.grants.some(
                        (grant) =>
                          tokenProjects.includes(grant.project_id) &&
                          grant.role !== "editor",
                      )}
                    >
                      Read, run, edit, and deploy
                    </option>
                  </select>
                </Field>
              </div>
              <h4>Limit this token to projects</h4>
              {selected.grants.map((grant) => (
                <label className="checkbox-field" key={grant.project_id}>
                  <input
                    type="checkbox"
                    checked={tokenProjects.includes(grant.project_id)}
                    onChange={(event) => {
                      setTokenProjects((values) =>
                        event.target.checked
                          ? [...values, grant.project_id]
                          : values.filter(
                              (value) => value !== grant.project_id,
                            ),
                      );
                      setPermission("read");
                    }}
                  />
                  <span>
                    {projectName(grant.project_id)} · account role {grant.role}
                  </span>
                </label>
              ))}
              <button
                className="button primary"
                disabled={busy || !tokenProjects.length}
              >
                {busy ? <Spinner /> : null}Issue scoped token
              </button>
            </form>
            <h3>Issued tokens</h3>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Scope</th>
                  <th>Expires</th>
                  <th>Last used</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((token) => (
                  <tr key={token.id}>
                    <td>
                      {token.name}
                      <div className="muted">{token.prefix}…</div>
                    </td>
                    <td>
                      {token.actions.join(", ")}
                      <div className="muted">
                        {token.project_ids.map(projectName).join(", ")}
                      </div>
                    </td>
                    <td>{date(token.expires_at)}</td>
                    <td>{date(token.last_used_at)}</td>
                    <td>
                      {token.revoked_at ? (
                        "Revoked"
                      ) : (
                        <button
                          className="button compact"
                          disabled={busy}
                          onClick={() =>
                            void perform(async () => {
                              await api(
                                `/service-accounts/${selected.id}/tokens/${token.id}`,
                                json("DELETE"),
                              );
                            })
                          }
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="form-note">
              Revoking a token blocks API access. It does not delete schedules
              already owned by the service account; remove its project grant or
              account to stop future admissions.
            </p>
            {error ? (
              <div className="inline-error" role="alert">
                {error}
              </div>
            ) : null}
            <div className="modal-footer">
              <button
                className="button"
                disabled={busy}
                onClick={() => {
                  setSelected(null);
                  setIssued("");
                }}
              >
                Close
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
