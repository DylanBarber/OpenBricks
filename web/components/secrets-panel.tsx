"use client";

import { useEffect, useState, type FormEvent } from "react";
import { KeyRound, Plus, RefreshCw, Settings2, Trash2 } from "lucide-react";
import { api, date, json, type Page, type Project } from "@/lib/api";
import { Empty, Field, Modal, Pagination, SectionHead, Spinner } from "./ui";
import styles from "./security-panels.module.css";

export type SecretScope = {
  id: string;
  name: string;
  project_id: string;
  description: string;
  allowed_roles: string[];
  keys: { name: string; version: number; updated_at: string }[];
};
type ScopeEditor = {
  id?: string;
  name: string;
  description: string;
  allowed_roles: string[];
};
type KeyEditor = { scope: SecretScope; key: string; existing: boolean };
type Removal = { scope: SecretScope; key?: string };
const validName = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const htmlNamePattern = "[A-Za-z0-9][A-Za-z0-9_.\\-]{0,63}";
const message = (error: unknown) =>
  error instanceof Error ? error.message : "The request could not complete.";

export function SecretsPanel({
  projects,
  onError,
}: {
  projects: Project[];
  onError: (message: string) => void;
}) {
  const [selectedProject, setSelectedProject] = useState("");
  const project =
    projects.find((item) => item.id === selectedProject) || projects[0];
  const projectId = project?.id || "";
  const canManage = project?.role === "owner";
  const [page, setPage] = useState<Page<SecretScope>>({
    items: [],
    total: 0,
    limit: 25,
    offset: 0,
  });
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [scopeEditor, setScopeEditor] = useState<ScopeEditor | null>(null);
  const [keyEditor, setKeyEditor] = useState<KeyEditor | null>(null);
  const [value, setValue] = useState("");
  const [removal, setRemoval] = useState<Removal | null>(null);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void api<Page<SecretScope>>(
      `/secret-scopes?project_id=${encodeURIComponent(projectId)}&limit=25&offset=${offset}`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (!controller.signal.aborted) setPage(result);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(message(failure));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [projectId, offset, refresh]);

  function failed(failure: unknown) {
    const text = message(failure);
    setError(text);
    onError(text);
  }
  function closeKey() {
    if (!busy) {
      setKeyEditor(null);
      setValue("");
    }
  }
  async function saveScope(event: FormEvent) {
    event.preventDefault();
    if (!scopeEditor || !projectId) return;
    setBusy(true);
    setError("");
    try {
      const { id, ...body } = scopeEditor;
      await api(
        id ? `/secret-scopes/${id}` : "/secret-scopes",
        json(
          id ? "PATCH" : "POST",
          id
            ? {
                description: body.description,
                allowed_roles: body.allowed_roles,
              }
            : { ...body, project_id: projectId },
        ),
      );
      setScopeEditor(null);
      setRefresh((previous) => previous + 1);
      setNotice(id ? "Scope access updated." : "Secret scope created.");
    } catch (failure) {
      failed(failure);
    } finally {
      setBusy(false);
    }
  }
  async function saveKey(event: FormEvent) {
    event.preventDefault();
    if (!keyEditor) return;
    if (new TextEncoder().encode(value).length > 4096) {
      setError("A secret is limited to 4 KB.");
      return;
    }
    const credential = value;
    setValue("");
    setBusy(true);
    setError("");
    try {
      await api(
        `/secret-scopes/${keyEditor.scope.id}/keys/${encodeURIComponent(keyEditor.key)}`,
        json("PUT", { value: credential }),
      );
      setKeyEditor(null);
      setRefresh((previous) => previous + 1);
      setNotice("Secret saved. Future runs use the new value.");
    } catch (failure) {
      failed(failure);
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!removal) return;
    setBusy(true);
    setError("");
    try {
      await api(
        `/secret-scopes/${removal.scope.id}${removal.key ? `/keys/${encodeURIComponent(removal.key)}` : ""}`,
        json("DELETE"),
      );
      setRemoval(null);
      setRefresh((previous) => previous + 1);
      setNotice("Secret access removed.");
    } catch (failure) {
      failed(failure);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <SectionHead
        eyebrow="Workspace security"
        title="Notebook secrets"
        description="Store credentials once and select only the keys each notebook needs."
        actions={
          <button
            className="button primary"
            disabled={!canManage || busy}
            onClick={() => {
              setError("");
              setScopeEditor({
                name: "",
                description: "",
                allowed_roles: ["operator", "editor", "owner"],
              });
            }}
          >
            <Plus size={15} />
            New scope
          </button>
        }
      />
      <div className={styles.toolbar}>
        <Field label="Project">
          <select
            value={projectId}
            disabled={busy}
            onChange={(event) => {
              setSelectedProject(event.target.value);
              setOffset(0);
              setScopeEditor(null);
              setKeyEditor(null);
              setValue("");
              setRemoval(null);
              setNotice("");
            }}
          >
            {projects.map((item) => (
              <option value={item.id} key={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </Field>
        <button
          className="button"
          disabled={loading}
          onClick={() => setRefresh((previous) => previous + 1)}
        >
          <RefreshCw size={15} />
          Refresh
        </button>
      </div>
      <p className={styles.note}>
        Values are encrypted and hidden after saving. Project owners manage
        scopes and keys. Notebook editors select keys in Notebook settings, then
        code uses <code>dbutils.secrets.get(scope, key)</code>.
      </p>
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p className={styles.success} role="status">
          {notice}
        </p>
      ) : null}
      {loading ? (
        <Spinner />
      ) : page.items.length ? (
        <div className={styles.grid}>
          {page.items.map((scope) => (
            <article className={styles.card} key={scope.id}>
              <div className={styles.cardHead}>
                <div>
                  <h3>
                    <KeyRound size={17} />
                    {scope.name}
                  </h3>
                  <p>{scope.description || "Notebook credential scope"}</p>
                </div>
                {canManage ? (
                  <div className={styles.actions}>
                    <button
                      className="icon-button"
                      aria-label={`Configure scope ${scope.name}`}
                      onClick={() =>
                        setScopeEditor({
                          id: scope.id,
                          name: scope.name,
                          description: scope.description,
                          allowed_roles: scope.allowed_roles,
                        })
                      }
                    >
                      <Settings2 size={15} />
                    </button>
                    <button
                      className="icon-button"
                      aria-label={`Delete scope ${scope.name}`}
                      onClick={() => setRemoval({ scope })}
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                ) : null}
              </div>
              <small className="muted">
                Available to {scope.allowed_roles.join(", ")}
              </small>
              <ul className={styles.keyList}>
                {scope.keys.map((key) => (
                  <li key={key.name}>
                    <span>
                      <code>{key.name}</code>
                      <small>
                        Version {key.version} · Updated {date(key.updated_at)}
                      </small>
                    </span>
                    {canManage ? (
                      <div className={styles.actions}>
                        <button
                          className="button"
                          onClick={() => {
                            setValue("");
                            setKeyEditor({
                              scope,
                              key: key.name,
                              existing: true,
                            });
                          }}
                        >
                          Rotate
                        </button>
                        <button
                          className="icon-button"
                          aria-label={`Delete secret ${scope.name}/${key.name}`}
                          onClick={() => setRemoval({ scope, key: key.name })}
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
              {!scope.keys.length ? (
                <p className={styles.emptyKeys}>No keys in this scope yet.</p>
              ) : null}
              {canManage ? (
                <button
                  className="button"
                  onClick={() => {
                    setValue("");
                    setKeyEditor({ scope, key: "", existing: false });
                  }}
                >
                  <Plus size={14} />
                  Add key
                </button>
              ) : null}
            </article>
          ))}
        </div>
      ) : (
        <Empty icon={<KeyRound size={28} />} title="No secret scopes yet">
          {canManage
            ? "Create a scope, then add the credentials your notebooks need."
            : "A project owner can create secret scopes for your notebooks."}
        </Empty>
      )}
      <Pagination
        total={page.total}
        offset={offset}
        limit={25}
        busy={loading}
        onPage={setOffset}
      />
      {scopeEditor ? (
        <Modal
          title={
            scopeEditor.id
              ? `Configure ${scopeEditor.name}`
              : "New secret scope"
          }
          onClose={() => {
            if (!busy) setScopeEditor(null);
          }}
        >
          <form
            className={styles.form}
            onSubmit={(event) => void saveScope(event)}
          >
            <Field label="Scope name">
              <input
                required
                maxLength={64}
                pattern={htmlNamePattern}
                value={scopeEditor.name}
                disabled={!!scopeEditor.id || busy}
                onChange={(event) =>
                  setScopeEditor({ ...scopeEditor, name: event.target.value })
                }
              />
            </Field>
            <Field label="Description">
              <input
                maxLength={2000}
                disabled={busy}
                value={scopeEditor.description}
                onChange={(event) =>
                  setScopeEditor({
                    ...scopeEditor,
                    description: event.target.value,
                  })
                }
              />
            </Field>
            <fieldset className={styles.roles}>
              <legend>Project roles allowed to use these secrets</legend>
              {["operator", "editor", "owner"].map((role) => (
                <label key={role}>
                  <input
                    type="checkbox"
                    checked={scopeEditor.allowed_roles.includes(role)}
                    disabled={role === "owner" || busy}
                    onChange={(event) =>
                      setScopeEditor({
                        ...scopeEditor,
                        allowed_roles: event.target.checked
                          ? [...scopeEditor.allowed_roles, role]
                          : scopeEditor.allowed_roles.filter(
                              (value) => value !== role,
                            ),
                      })
                    }
                  />
                  {role}
                </label>
              ))}
            </fieldset>
            {error ? (
              <p className={styles.error} role="alert">
                {error}
              </p>
            ) : null}
            <div className={styles.footer}>
              <button
                className="button"
                type="button"
                disabled={busy}
                onClick={() => setScopeEditor(null)}
              >
                Cancel
              </button>
              <button
                className="button primary"
                disabled={busy || !validName.test(scopeEditor.name)}
              >
                {busy ? <Spinner /> : null}Save scope
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {keyEditor ? (
        <Modal
          title={
            keyEditor.existing
              ? `Rotate ${keyEditor.scope.name}/${keyEditor.key}`
              : `Add key to ${keyEditor.scope.name}`
          }
          onClose={closeKey}
        >
          <form
            className={styles.form}
            onSubmit={(event) => void saveKey(event)}
          >
            <Field label="Key name">
              <input
                required
                maxLength={64}
                pattern={htmlNamePattern}
                value={keyEditor.key}
                disabled={keyEditor.existing || busy}
                onChange={(event) =>
                  setKeyEditor({ ...keyEditor, key: event.target.value })
                }
              />
            </Field>
            <Field
              label="Secret value"
              hint="The value is hidden after saving. Rotation applies to future dispatches."
            >
              <input
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                required
                maxLength={4096}
                value={value}
                disabled={busy}
                onChange={(event) => setValue(event.target.value)}
              />
            </Field>
            {error ? (
              <p className={styles.error} role="alert">
                {error}
              </p>
            ) : null}
            <div className={styles.footer}>
              <button
                className="button"
                type="button"
                disabled={busy}
                onClick={closeKey}
              >
                Cancel
              </button>
              <button
                className="button primary"
                disabled={busy || !validName.test(keyEditor.key) || !value}
              >
                {busy ? <Spinner /> : null}Save secret
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {removal ? (
        <Modal
          title={removal.key ? "Delete notebook secret" : "Delete secret scope"}
          onClose={() => {
            if (!busy) setRemoval(null);
          }}
        >
          <p>
            Delete{" "}
            <strong>
              {removal.scope.name}
              {removal.key ? `/${removal.key}` : " and all its keys"}
            </strong>
            ? Future runs selecting these keys will fail until their selection
            is updated.
          </p>
          {error ? (
            <p className={styles.error} role="alert">
              {error}
            </p>
          ) : null}
          <div className={styles.footer}>
            <button
              className="button"
              disabled={busy}
              onClick={() => setRemoval(null)}
            >
              Cancel
            </button>
            <button
              className="button danger"
              disabled={busy}
              onClick={() => void remove()}
            >
              {busy ? <Spinner /> : <Trash2 size={14} />}Delete
            </button>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
