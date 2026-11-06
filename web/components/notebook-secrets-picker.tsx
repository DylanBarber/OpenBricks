"use client";

import { useEffect, useState } from "react";
import { KeyRound, RefreshCw, Save, X } from "lucide-react";
import { api, json, type Page } from "@/lib/api";
import { Field, Spinner } from "./ui";
import type { SecretScope } from "./secrets-panel";
import styles from "./security-panels.module.css";

type SecretRef = { scope: string; key: string };
type Selection = { id: string; secret_refs: SecretRef[]; version: number };
const identity = (value: SecretRef) => `${value.scope}/${value.key}`;

export function NotebookSecretsPicker({
  notebookId,
  projectId,
  canEdit,
  onChanged,
}: {
  notebookId: string;
  projectId: string;
  canEdit: boolean;
  onChanged?: () => void;
}) {
  const [scopes, setScopes] = useState<SecretScope[]>([]);
  const [selected, setSelected] = useState<SecretRef[]>([]);
  const [version, setVersion] = useState<number | null>(null);
  const [scopeName, setScopeName] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setNotice("");
    setVersion(null);
    setSelected([]);
    void Promise.all([
      api<Selection>(
        `/notebooks/${encodeURIComponent(notebookId)}/secret-refs`,
        { signal: controller.signal },
      ),
      api<Page<SecretScope>>(
        `/secret-scopes?project_id=${encodeURIComponent(projectId)}&limit=100`,
        { signal: controller.signal },
      ),
    ])
      .then(([selection, page]) => {
        if (controller.signal.aborted) return;
        setSelected(selection.secret_refs);
        setVersion(selection.version);
        setScopes(page.items);
        setScopeName(page.items[0]?.name || "");
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted)
          setError(
            failure instanceof Error
              ? failure.message
              : "Secret selection could not load.",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [notebookId, projectId, refresh]);
  const scope = scopes.find((item) => item.name === scopeName);
  function toggle(ref: SecretRef, checked: boolean) {
    setSelected((previous) =>
      checked
        ? previous.length < 32
          ? [
              ...previous.filter((item) => identity(item) !== identity(ref)),
              ref,
            ]
          : previous
        : previous.filter((item) => identity(item) !== identity(ref)),
    );
    setNotice("");
  }
  async function save() {
    if (version === null) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = await api<Selection>(
        `/notebooks/${encodeURIComponent(notebookId)}/secret-refs`,
        json("PUT", { secret_refs: selected, version }),
      );
      setVersion(value.version);
      setSelected(value.secret_refs);
      setNotice("Notebook secret selection saved.");
      onChanged?.();
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "Secret selection could not save.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section aria-label="Notebook secret selection" className={styles.card}>
      <div className={styles.cardHead}>
        <div>
          <h3>
            <KeyRound size={17} />
            Notebook secrets
          </h3>
          <p>
            Select the exact keys this notebook can use. Values stay hidden in
            settings.
          </p>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label="Reload notebook secret selection"
          disabled={busy || loading}
          onClick={() => setRefresh((previous) => previous + 1)}
        >
          <RefreshCw size={15} />
        </button>
      </div>
      {loading ? (
        <Spinner />
      ) : (
        <>
          <div className={styles.chips}>
            {selected.length ? (
              selected.map((item) => (
                <span className={styles.chip} key={identity(item)}>
                  <code>{identity(item)}</code>
                  {canEdit ? (
                    <button
                      className="icon-button"
                      type="button"
                      aria-label={`Remove secret ${identity(item)}`}
                      disabled={busy}
                      onClick={() => toggle(item, false)}
                    >
                      <X size={12} />
                    </button>
                  ) : null}
                </span>
              ))
            ) : (
              <small className="muted">
                No secrets selected. This notebook receives no notebook-secret
                values.
              </small>
            )}
          </div>
          {scopes.length ? (
            <>
              <div className={styles.toolbar}>
                <Field label="Secret scope">
                  <select
                    disabled={!canEdit || busy}
                    value={scopeName}
                    onChange={(event) => {
                      setScopeName(event.target.value);
                      setSearch("");
                    }}
                  >
                    {scopes.map((item) => (
                      <option key={item.id} value={item.name}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Filter keys">
                  <input
                    value={search}
                    maxLength={64}
                    onChange={(event) => setSearch(event.target.value)}
                  />
                </Field>
              </div>
              <div className={styles.checkList}>
                {scope?.keys
                  .filter((key) =>
                    key.name.toLowerCase().includes(search.toLowerCase()),
                  )
                  .map((key) => {
                    const ref = { scope: scope.name, key: key.name };
                    const checked = selected.some(
                      (item) => identity(item) === identity(ref),
                    );
                    return (
                      <label key={key.name}>
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={
                            !canEdit ||
                            busy ||
                            (!checked && selected.length >= 32)
                          }
                          onChange={(event) =>
                            toggle(ref, event.target.checked)
                          }
                        />
                        <code>{key.name}</code>
                      </label>
                    );
                  })}
              </div>
            </>
          ) : (
            <p className="muted">
              A project owner can add scopes under Notebook secrets.
            </p>
          )}
          <p className="muted">
            <small>
              {selected.length}/32 keys selected · Read with{" "}
              <code>
                dbutils.secrets.get(&quot;scope&quot;, &quot;key&quot;)
              </code>
              .
            </small>
          </p>
          {canEdit ? (
            <button
              className="button"
              type="button"
              disabled={busy || version === null}
              onClick={() => void save()}
            >
              {busy ? <Spinner /> : <Save size={14} />}Save secret selection
            </button>
          ) : null}
        </>
      )}
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
    </section>
  );
}
