"use client";
import { useEffect, useState, type FormEvent } from "react";
import { FolderKey, Plus, Save, Trash2, UserPlus } from "lucide-react";
import {
  api,
  json,
  type Cluster,
  type Connection,
  type Project,
  type User,
} from "@/lib/api";
import { Empty, Field, Modal, SectionHead, Spinner } from "./ui";
type Member = {
  user_id: string;
  username: string;
  role: string;
  global_admin?: boolean;
  is_service_account?: boolean;
};
export default function ProjectsPanel({
  projects,
  clusters,
  connections,
  user,
  onChanged,
  onError,
}: {
  projects: Project[];
  clusters: Cluster[];
  connections: Connection[];
  user: User;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const [editing, setEditing] = useState<Project | null>(null),
    [members, setMembers] = useState<Member[]>([]),
    [available, setAvailable] = useState<User[]>([]),
    [memberProject, setMemberProject] = useState<Project | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [version, setVersion] = useState(0),
    [newUser, setNewUser] = useState(""),
    [newRole, setNewRole] = useState("viewer");
  useEffect(() => {
    if (!memberProject) return;
    let stopped = false;
    Promise.all([
      api<Member[]>(`/projects/${memberProject.id}/members`),
      api<User[]>(`/projects/${memberProject.id}/available-users`),
    ])
      .then(([members, users]) => {
        if (!stopped) {
          setMembers(members);
          setAvailable(users);
        }
      })
      .catch((error) => {
        if (!stopped) setError(error.message);
      });
    return () => {
      stopped = true;
    };
  }, [memberProject?.id, version]);
  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await work();
      onChanged();
      setVersion((value) => value + 1);
    } catch (error) {
      setError((error as Error).message);
      onError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function save(event: FormEvent) {
    event.preventDefault();
    if (editing)
      void perform(async () => {
        await api(
          editing.id ? `/projects/${editing.id}` : "/projects",
          json(editing.id ? "PUT" : "POST", editing),
        );
        setEditing(null);
      });
  }
  return (
    <>
      <SectionHead
        eyebrow="WORK TOGETHER, WITH BOUNDARIES"
        title="Projects & access"
        description="Group notebooks and data work, assign team roles, and allow only the connections and compute each project needs."
        actions={
          user.role === "admin" ? (
            <button
              className="button primary"
              onClick={() =>
                setEditing({
                  id: "",
                  name: "",
                  description: "",
                  connection_ids: [],
                  cluster_ids: ["local"],
                })
              }
            >
              <Plus size={15} />
              New project
            </button>
          ) : null
        }
      />
      <div className="project-grid">
        {projects.map((project) => (
          <section className="card project-card" key={project.id}>
            <FolderKey size={24} className="accent" />
            <h2>{project.name}</h2>
            <p>
              {project.description ||
                "A shared space for notebooks and data work."}
            </p>
            <div className="project-facts">
              <span>
                Your role
                <strong>
                  {user.role === "admin" ? "Administrator" : project.role}
                </strong>
              </span>
              <span>
                Connections
                <strong>{project.connection_ids?.length || 0}</strong>
              </span>
              <span>
                Compute targets
                <strong>{project.cluster_ids?.length || 0}</strong>
              </span>
            </div>
            {user.role === "admin" || project.role === "owner" ? (
              <div className="row-actions">
                <button
                  className="button compact"
                  onClick={() => {
                    setEditing(project);
                    setError("");
                  }}
                >
                  Settings
                </button>
                <button
                  className="button compact"
                  onClick={() => {
                    setMemberProject(project);
                    setError("");
                  }}
                >
                  <UserPlus size={13} />
                  Members
                </button>
                {user.role === "admin" && project.id !== "default" ? (
                  <button
                    className="icon-button"
                    aria-label={`Delete project ${project.name}`}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Delete empty project “${project.name}”? Move or remove its resources first.`,
                        )
                      )
                        void perform(async () => {
                          await api(`/projects/${project.id}`, json("DELETE"));
                        });
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                ) : null}
              </div>
            ) : null}
          </section>
        ))}
      </div>
      <section className="card platform-form">
        <h3>Roles at a glance</h3>
        <table className="data-table">
          <thead>
            <tr>
              <th>Role</th>
              <th>Allowed work</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Viewer</td>
              <td>Browse project resources and results.</td>
            </tr>
            <tr>
              <td>Editor</td>
              <td>
                Browse, run, and edit notebooks, queries, and project resources.
              </td>
            </tr>
            <tr>
              <td>Operator</td>
              <td>Browse and execute work, without editing its source.</td>
            </tr>
            <tr>
              <td>Owner</td>
              <td>Also manage project access and settings.</td>
            </tr>
          </tbody>
        </table>
        <p className="form-note">
          Project permissions control application actions for trusted teams.
          Notebook code still runs with the compute service's permissions.
        </p>
      </section>
      {editing ? (
        <Modal
          title={editing.id ? "Project settings" : "Create project"}
          wide
          onClose={() => {
            if (!busy) setEditing(null);
          }}
        >
          <form className="modal-form" onSubmit={save}>
            <Field label="Project name">
              <input
                required
                autoFocus
                value={editing.name}
                onChange={(event) =>
                  setEditing({ ...editing, name: event.target.value })
                }
              />
            </Field>
            <Field label="Description">
              <textarea
                value={editing.description || ""}
                onChange={(event) =>
                  setEditing({ ...editing, description: event.target.value })
                }
                rows={2}
              />
            </Field>
            <h3>Allowed connections</h3>
            <p className="form-note">
              Notebooks must also select their connections individually. An
              empty notebook selection exposes no saved credentials.
            </p>
            <div className="connection-checklist">
              {connections.map((item) => (
                <label className="checkbox-field" key={item.id}>
                  <input
                    type="checkbox"
                    disabled={user.role !== "admin"}
                    checked={editing.connection_ids?.includes(item.id) || false}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        connection_ids: event.target.checked
                          ? [...(editing.connection_ids || []), item.id]
                          : (editing.connection_ids || []).filter(
                              (id) => id !== item.id,
                            ),
                      })
                    }
                  />
                  {item.name}
                  <span className="muted">{item.type}</span>
                </label>
              ))}
            </div>
            <h3>Allowed compute</h3>
            <div className="connection-checklist">
              {clusters.map((item) => (
                <label className="checkbox-field" key={item.id}>
                  <input
                    type="checkbox"
                    disabled={user.role !== "admin"}
                    checked={editing.cluster_ids?.includes(item.id) || false}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        cluster_ids: event.target.checked
                          ? [...(editing.cluster_ids || []), item.id]
                          : (editing.cluster_ids || []).filter(
                              (id) => id !== item.id,
                            ),
                      })
                    }
                  />
                  {item.name}
                </label>
              ))}
            </div>
            {error ? <div className="inline-error">{error}</div> : null}
            <div className="modal-footer">
              <button className="button primary" disabled={busy}>
                {busy ? <Spinner /> : <Save size={14} />}Save project
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {memberProject ? (
        <Modal
          title={`${memberProject.name} · Members`}
          wide
          onClose={() => setMemberProject(null)}
        >
          <div className="modal-form">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Team member</th>
                  <th>Project role</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {members.map((member) => (
                  <tr key={member.user_id}>
                    <td>
                      {member.username}
                      {member.global_admin ? (
                        <small className="table-subline">
                          Workspace administrator
                        </small>
                      ) : null}
                    </td>
                    <td>
                      <select
                        aria-label={`${member.username} project role`}
                        value={member.role}
                        title={
                          member.global_admin
                            ? "Workspace administrators have implicit access to every project"
                            : undefined
                        }
                        disabled={busy || !!member.global_admin}
                        onChange={(event) =>
                          void perform(async () => {
                            await api(
                              `/projects/${memberProject.id}/members/${member.user_id}`,
                              json("PUT", { role: event.target.value }),
                            );
                          })
                        }
                      >
                        {["viewer", "editor", "operator", "owner"].map(
                          (role) => (
                            <option
                              key={role}
                              disabled={
                                role === "owner" && member.is_service_account
                              }
                            >
                              {role}
                            </option>
                          ),
                        )}
                      </select>
                    </td>
                    <td>
                      <button
                        className="icon-button"
                        aria-label={`Remove ${member.username} from project`}
                        disabled={busy || !!member.global_admin}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Remove ${member.username} from this project?`,
                            )
                          )
                            void perform(async () => {
                              await api(
                                `/projects/${memberProject.id}/members/${member.user_id}`,
                                json("DELETE"),
                              );
                            });
                        }}
                      >
                        <Trash2 size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!members.length ? (
              <Empty title="No explicit project members" />
            ) : null}
            <h3>Add a member</h3>
            <div className="form-grid">
              <Field label="Team account">
                <select
                  value={newUser}
                  onChange={(event) => setNewUser(event.target.value)}
                >
                  <option value="">Choose account</option>
                  {available
                    .filter(
                      (user) =>
                        !members.some((member) => member.user_id === user.id),
                    )
                    .map((user) => (
                      <option key={user.id} value={user.id}>
                        {user.username}
                      </option>
                    ))}
                </select>
              </Field>
              <Field label="Role">
                <select
                  value={newRole}
                  onChange={(event) => setNewRole(event.target.value)}
                >
                  {["viewer", "editor", "operator", "owner"].map((role) => (
                    <option
                      key={role}
                      disabled={
                        role === "owner" &&
                        available.find((account) => account.id === newUser)
                          ?.is_service_account
                      }
                    >
                      {role}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            {error ? <div className="inline-error">{error}</div> : null}
            <button
              className="button primary"
              disabled={!newUser || busy}
              onClick={() =>
                void perform(async () => {
                  await api(
                    `/projects/${memberProject.id}/members/${newUser}`,
                    json("PUT", { role: newRole }),
                  );
                  setNewUser("");
                })
              }
            >
              <UserPlus size={14} />
              Add member
            </button>
          </div>
        </Modal>
      ) : null}
    </>
  );
}
