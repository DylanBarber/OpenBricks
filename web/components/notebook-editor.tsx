"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";

import dynamic from "next/dynamic";

import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Check,
  Code2,
  Download,
  FileCode2,
  Play,
  Plus,
  Save,
  Square,
  Trash2,
  Search,
  Settings2,
  History,
  Wand2,
  Users,
} from "lucide-react";

import {
  api,
  date,
  json,
  newCellId,
  type Cell,
  type CellOutput,
  type Cluster,
  type Connection,
  type Notebook,
  type Page,
  type Project,
  type Run,
} from "@/lib/api";

import { Field, Modal, Pagination, Spinner, Status } from "./ui";

import {
  ParameterDefinitions,
  ParameterValues,
  parameterDefaults,
} from "./parameter-fields";

import { RunActivity } from "./run-activity";
import {
  ResourceRunFields,
  type ResourceRunOptions,
} from "./resource-controls";

const CodeEditor = dynamic(() => import("./code-editor"), { ssr: false });
const CollaborationPanel = dynamic(() => import("./collaboration-panel"), {
  ssr: false,
});
const NotebookSecretsPicker = dynamic(() =>
  import("./notebook-secrets-picker").then(
    (module) => module.NotebookSecretsPicker,
  ),
);

function Output({ output }: { output?: CellOutput }) {
  if (!output) return null;

  const result = output.result as
    { columns?: string[]; rows?: unknown[][] } | undefined;

  return (
    <div className="cell-output">
      <div className="output-label">
        <Status status={output.status} />
        <span>
          {output.duration_ms != null
            ? `${(output.duration_ms / 1000).toFixed(2)}s`
            : ""}
        </span>
      </div>
      {output.stdout ? <pre>{output.stdout}</pre> : null}
      {output.stderr ? <pre className="stderr">{output.stderr}</pre> : null}
      {output.error ? <pre className="stderr">{output.error}</pre> : null}
      {result?.columns ? (
        <div className="table-scroll">
          <table className="result-table">
            <thead>
              <tr>
                {result.columns.map((column, index) => (
                  <th key={`${column}-${index}`}>{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(result.rows || []).map((row, index) => (
                <tr key={index}>
                  {row.map((value, column) => (
                    <td key={column}>
                      {value === null ? (
                        <em className="muted">null</em>
                      ) : typeof value === "object" ? (
                        JSON.stringify(value)
                      ) : (
                        String(value)
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="output-count">
            {result.rows?.length || 0} rows · Results limited by runtime
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function NotebookEditor({
  notebook: initial,
  onBack,
  onChanged,
  onError,
  onDirtyChange,
  clusters,
  projects,
  connections,
  canEdit: permissionToEdit = true,
  canRun = true,
}: {
  notebook: Notebook;
  onBack: () => void;
  onChanged: () => void;
  onError: (message: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  clusters: Cluster[];
  projects: Project[];
  connections: Connection[];
  canEdit?: boolean;
  canRun?: boolean;
}) {
  const [notebook, setNotebook] = useState(initial);
  const canEdit = permissionToEdit && !notebook.collaboration_enabled;
  const [collaborating, setCollaborating] = useState(false);
  const [secretSelection, setSecretSelection] = useState(false);

  const [dirty, setDirty] = useState(false);

  const [busy, setBusy] = useState(false);

  const [run, setRun] = useState<Run | null>(null);

  const [history, setHistory] = useState<Run[]>([]);

  const [showHistory, setShowHistory] = useState(false);

  const [historyOffset, setHistoryOffset] = useState(0);

  const [historyTotal, setHistoryTotal] = useState(0);

  const [settings, setSettings] = useState(false);

  const [runOptions, setRunOptions] = useState(false);

  const [parameters, setParameters] = useState<Record<string, unknown>>({});

  const [runClusterId, setRunClusterId] = useState("");
  const [resourceOptions, setResourceOptions] = useState<ResourceRunOptions>({
    mode: "interactive",
  });
  const [environmentId, setEnvironmentId] = useState("");

  const [environments, setEnvironments] = useState<
    { id: string; name: string }[]
  >([]);

  const [find, setFind] = useState("");

  const [replace, setReplace] = useState("");

  const [revisions, setRevisions] = useState<Page<{
    id: string;
    created_at: string;
    reason?: string;
    version: number;
  }> | null>(null);

  const [revisionOffset, setRevisionOffset] = useState(0);

  const [showRevisions, setShowRevisions] = useState(false);

  const [revision, setRevision] = useState<{
    id: string;
    notebook: Notebook;
  } | null>(null);

  useEffect(() => {
    if (!runOptions) return;
    let stopped = false;
    api<{ id: string; name: string }[]>(
      `/environments?project_id=${notebook.project_id || "default"}`,
    )
      .then((items) => {
        if (!stopped) setEnvironments(items);
      })
      .catch((error) => {
        if (!stopped) onError(error.message);
      });
    return () => {
      stopped = true;
    };
  }, [runOptions, onError]);

  useEffect(() => {
    if (!showRevisions) return;
    let stopped = false;
    api<
      Page<{ id: string; created_at: string; reason?: string; version: number }>
    >(`/notebooks/${notebook.id}/revisions?limit=20&offset=${revisionOffset}`)
      .then((value) => {
        if (!stopped) setRevisions(value);
      })
      .catch((error) => {
        if (!stopped) onError(error.message);
      });
    return () => {
      stopped = true;
    };
  }, [showRevisions, revisionOffset, notebook.id, onError]);

  const active = run?.status === "queued" || run?.status === "running";

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!active || !run) return;

    let stopped = false;

    const timer = setInterval(async () => {
      try {
        const updated = await api<Run>(`/runs/${run.id}`);
        if (!stopped) {
          setRun(updated);
          if (!["queued", "running"].includes(updated.status)) onChanged();
        }
      } catch (error) {
        if (!stopped) onError((error as Error).message);
      }
    }, 1200);

    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [active, run?.id, onChanged, onError]);

  useEffect(() => {
    const prevent = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault();
    };

    window.addEventListener("beforeunload", prevent);

    return () => window.removeEventListener("beforeunload", prevent);
  }, [dirty]);

  useEffect(() => {
    if (!showHistory) return;
    let stopped = false;
    api<Page<Run>>(
      `/history/runs?${new URLSearchParams({ notebook_id: notebook.id, limit: "20", offset: String(historyOffset) })}`,
    )
      .then((result) => {
        if (!stopped) {
          setHistory(result.items);
          setHistoryTotal(result.total);
        }
      })
      .catch((error) => {
        if (!stopped) onError(error.message);
      });
    return () => {
      stopped = true;
    };
  }, [showHistory, historyOffset, notebook.id, onError]);

  function change(update: Partial<Notebook>) {
    if (busy || !canEdit) return;
    setNotebook((previous) => ({ ...previous, ...update }));
    setDirty(true);
  }

  function changeCell(id: string, update: Partial<Cell>) {
    change({
      cells: notebook.cells.map((cell) =>
        cell.id === id ? { ...cell, ...update } : cell,
      ),
    });
  }

  function addCell(language: Cell["language"] = "python", after?: number) {
    const cells = [...notebook.cells];
    cells.splice(after == null ? cells.length : after + 1, 0, {
      id: newCellId(),
      language,
      source: "",
    });
    change({ cells });
  }

  function moveCell(index: number, direction: number) {
    const cells = [...notebook.cells];
    [cells[index], cells[index + direction]] = [
      cells[index + direction],
      cells[index],
    ];
    change({ cells });
  }

  async function save() {
    const saved = await api<Notebook>(
      `/notebooks/${notebook.id}`,
      json("PUT", notebook),
    );
    if (alive.current) {
      setNotebook(saved);
      setDirty(false);
    }
    onChanged();
    return saved;
  }

  async function perform(action: () => Promise<void>) {
    setBusy(true);
    try {
      await action();
    } catch (error) {
      onError((error as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  function start(cellId?: string) {
    void perform(async () => {
      if (dirty) await save();
      const started = await api<Run>(
        `/notebooks/${notebook.id}/run`,
        json("POST", {
          ...(cellId ? { cell_id: cellId } : {}),
          parameters,
          ...(runClusterId ? { cluster_id: runClusterId } : {}),
          environment_id: environmentId || null,
          resource_options: resourceOptions,
        }),
      );
      setRun(started);
    });
  }

  async function formatCell(cell: Cell) {
    void perform(async () => {
      let source = cell.source;
      if (cell.language === "sql") {
        const formatter = await import("sql-formatter");
        source = formatter.format(source, { language: "spark" });
      } else if (cell.language === "markdown") {
        const [formatter, parser] = await Promise.all([
          import("prettier/standalone"),
          import("prettier/plugins/markdown"),
        ]);
        source = await formatter.format(source, {
          parser: "markdown",
          plugins: [parser],
        });
      } else {
        source = (
          await api<{ source: string }>(
            "/editor/format",
            json("POST", {
              language: cell.language,
              source,
              project_id: notebook.project_id || "default",
            }),
          )
        ).source;
      }
      setNotebook((previous) => ({
        ...previous,
        cells: previous.cells.map((item) =>
          item.id === cell.id ? { ...item, source } : item,
        ),
      }));
      setDirty(true);
    });
  }

  function download() {
    const content = {
      nbformat: 4,
      nbformat_minor: 5,
      metadata: {
        kernelspec: {
          display_name: "Python 3 (OpenBricks)",
          language: "python",
          name: "python3",
        },
      },
      cells: notebook.cells.map((cell) => ({
        cell_type: cell.language === "markdown" ? "markdown" : "code",
        id: cell.id,
        metadata: {},
        ...(cell.language === "markdown"
          ? {}
          : { execution_count: null, outputs: [] }),
        source: (cell.language === "sql"
          ? `%%sql\n${cell.source}`
          : cell.source
        ).split(/(?<=\n)/),
      })),
    };

    const url = URL.createObjectURL(
      new Blob([JSON.stringify(content, null, 2)], {
        type: "application/x-ipynb+json",
      }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${notebook.name.replace(/[^a-z0-9-_ ]/gi, "_")}.ipynb`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="notebook-page">
      <div className="notebook-header">
        <button
          className="icon-button"
          onClick={() => {
            if (
              !dirty ||
              window.confirm("Leave this notebook and discard unsaved changes?")
            )
              onBack();
          }}
          aria-label="Back to workspace"
        >
          <ArrowLeft size={20} />
        </button>
        <FileCode2 size={23} className="accent" />
        <div className="notebook-title">
          <input
            aria-label="Notebook name"
            disabled={busy || !canEdit}
            value={notebook.name}
            onChange={(event) => change({ name: event.target.value })}
          />
          <span>
            {dirty ? (
              <>
                <span className="unsaved-dot" />
                Unsaved changes
              </>
            ) : (
              <>
                <Check size={12} /> All changes saved
              </>
            )}
          </span>
        </div>
        <div className="notebook-actions">
          <button
            className="button"
            onClick={() =>
              void perform(async () => {
                await save();
              })
            }
            disabled={busy || !dirty || !canEdit}
          >
            <Save size={15} />
            Save
          </button>
          <button
            className="icon-button"
            aria-label="Download notebook"
            title="Download .ipynb"
            onClick={download}
          >
            <Download size={17} />
          </button>
          <button
            className="icon-button"
            title="Notebook settings"
            aria-label="Notebook settings"
            onClick={() => setSettings(true)}
          >
            <Settings2 size={17} />
          </button>
          <button className="button" onClick={() => setRunOptions(true)}>
            Run options
          </button>
          <button
            className="button primary"
            disabled={busy || active || !canRun}
            onClick={() => start()}
          >
            {busy ? <Spinner /> : <Play size={15} fill="currentColor" />}Run all
          </button>
        </div>
      </div>
      <div className="notebook-toolbar">
        <div>
          <span className="connected-dot" />
          <label className="notebook-compute">
            <select
              aria-label="Notebook compute cluster"
              disabled={busy || active || !canEdit}
              value={notebook.cluster_id || ""}
              onChange={(event) =>
                change({ cluster_id: event.target.value || null })
              }
            >
              <option value="">Default local compute</option>
              {clusters.map((cluster) => (
                <option key={cluster.id} value={cluster.id}>
                  {cluster.name}
                </option>
              ))}
            </select>
          </label>
          <span className="toolbar-divider" />
          Python + Spark SQL
        </div>
        <div>
          {run ? <Status status={run.status} /> : null}
          <button
            className="text-button"
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                if (dirty) await save();
                setCollaborating(true);
              })
            }
          >
            <Users size={13} />
            Collaborate
          </button>
          <button
            className="text-button"
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                if (dirty) await save();
                setSecretSelection(true);
              })
            }
          >
            Notebook secrets
          </button>
          {active ? (
            <button
              className="text-button"
              disabled={busy || !canRun || !!run?.cancel_requested}
              onClick={() =>
                void perform(async () => {
                  setRun(
                    await api<Run>(`/runs/${run?.id}/cancel`, json("POST")),
                  );
                })
              }
            >
              <Square size={12} /> Cancel run
            </button>
          ) : null}
          <button
            className="text-button"
            onClick={() => setShowRevisions(true)}
          >
            <History size={13} />
            Revisions
          </button>
          <button
            className="text-button"
            onClick={() => {
              setHistoryOffset(0);
              setShowHistory(!showHistory);
            }}
          >
            Run history
          </button>
        </div>
      </div>

      <div className="notebook-search">
        <Search size={14} />
        <input
          aria-label="Find across notebook cells"
          placeholder="Find across all cells…"
          value={find}
          onChange={(event) => setFind(event.target.value)}
        />
        {find ? (
          <>
            <span>
              {
                notebook.cells.filter((cell) =>
                  cell.source.toLowerCase().includes(find.toLowerCase()),
                ).length
              }{" "}
              matching cells
            </span>
            <input
              aria-label="Replace matching text"
              placeholder="Replace with…"
              value={replace}
              onChange={(event) => setReplace(event.target.value)}
            />
            <button
              className="button compact"
              disabled={busy}
              onClick={() =>
                change({
                  cells: notebook.cells.map((cell) => ({
                    ...cell,
                    source: cell.source.split(find).join(replace),
                  })),
                })
              }
            >
              Replace all (case sensitive)
            </button>
            {notebook.cells.map((cell, index) =>
              cell.source.toLowerCase().includes(find.toLowerCase()) ? (
                <button
                  key={cell.id}
                  className="text-button"
                  onClick={() =>
                    document
                      .getElementById(`cell-${cell.id}`)
                      ?.scrollIntoView({ behavior: "smooth", block: "center" })
                  }
                >
                  Cell {index + 1}
                </button>
              ) : null,
            )}
          </>
        ) : null}
      </div>

      {run ? <RunActivity run={run} /> : null}

      {showHistory ? (
        <div className="history-panel">
          <div className="card-heading">
            <h3>Notebook runs</h3>
            <button
              className="text-button"
              onClick={() => setShowHistory(false)}
            >
              Close
            </button>
          </div>
          {history.length ? (
            history.map((item) => (
              <button
                className="history-row"
                key={item.id}
                onClick={() =>
                  void perform(async () => {
                    setRun(await api<Run>(`/runs/${item.id}`));
                    setShowHistory(false);
                  })
                }
              >
                <Status status={item.status} />
                <code>{item.id.slice(0, 8)}</code>
                <span>{date(item.created_at)}</span>
              </button>
            ))
          ) : (
            <p className="muted">No runs yet.</p>
          )}
          <Pagination
            total={historyTotal}
            offset={historyOffset}
            limit={20}
            onPage={setHistoryOffset}
            busy={busy}
          />
        </div>
      ) : null}

      {run?.error ? (
        <div className="error-banner" role="status">
          <strong>
            {active
              ? "Compute connection interrupted"
              : run.status === "cancelled"
                ? "Run cancelled"
                : "Run failed"}
          </strong>
          <pre>{run.error}</pre>
        </div>
      ) : null}

      <div className="notebook-content">
        <div className="notebook-intro">
          <span className="eyebrow">NOTEBOOK</span>
          <h2>{notebook.name}</h2>
          <p>Explore, transform, and build on your selected compute.</p>
        </div>
        {notebook.cells.map((cell, index) => (
          <section
            id={`cell-${cell.id}`}
            className={`notebook-cell ${cell.language === "markdown" ? "markdown-cell" : ""}`}
            key={cell.id}
          >
            <div className="cell-header">
              <div>
                <span className="cell-number">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <Code2 size={14} />
                <select
                  aria-label={`Cell ${index + 1} language`}
                  disabled={busy || !canEdit}
                  value={cell.language}
                  onChange={(event) =>
                    changeCell(cell.id, {
                      language: event.target.value as Cell["language"],
                    })
                  }
                >
                  <option value="python">Python</option>
                  <option value="sql">SQL</option>
                  <option value="markdown">Markdown</option>
                </select>
              </div>
              <div>
                <button
                  className="icon-button tiny"
                  disabled={busy || !canEdit}
                  aria-label={`Format cell ${index + 1}`}
                  title="Format cell"
                  onClick={() => void formatCell(cell)}
                >
                  <Wand2 size={13} />
                </button>
                <button
                  className="icon-button tiny"
                  disabled={index === 0 || !canEdit}
                  aria-label={`Move cell ${index + 1} up`}
                  onClick={() => moveCell(index, -1)}
                >
                  <ArrowUp size={13} />
                </button>
                <button
                  className="icon-button tiny"
                  disabled={index === notebook.cells.length - 1 || !canEdit}
                  aria-label={`Move cell ${index + 1} down`}
                  onClick={() => moveCell(index, 1)}
                >
                  <ArrowDown size={13} />
                </button>
                <button
                  className="icon-button tiny"
                  aria-label={`Delete cell ${index + 1}`}
                  onClick={() =>
                    change({
                      cells: notebook.cells.filter(
                        (item) => item.id !== cell.id,
                      ),
                    })
                  }
                >
                  <Trash2 size={13} />
                </button>
                {cell.language !== "markdown" ? (
                  <button
                    className="cell-run"
                    disabled={active || busy || !canRun}
                    onClick={() => start(cell.id)}
                    title="Save notebook and run this cell"
                  >
                    <Play size={12} fill="currentColor" /> Run cell
                  </button>
                ) : null}
              </div>
            </div>
            <CodeEditor
              value={cell.source}
              language={cell.language}
              disabled={busy || !canEdit}
              label={`Cell ${index + 1} code`}
              onChange={(source) => changeCell(cell.id, { source })}
              onRun={!active && !busy ? () => start(cell.id) : undefined}
            />
            <Output
              output={run?.outputs?.find(
                (output) => output.cell_id === cell.id,
              )}
            />
            <button
              className="insert-cell"
              onClick={() => addCell("python", index)}
              aria-label={`Insert cell after cell ${index + 1}`}
            >
              <Plus size={12} />
            </button>
          </section>
        ))}
        <div className="add-cells">
          <button className="button" onClick={() => addCell("python")}>
            <Plus size={14} />
            Python
          </button>
          <button className="button" onClick={() => addCell("sql")}>
            <Plus size={14} />
            SQL
          </button>
          <button className="text-button" onClick={() => addCell("markdown")}>
            <Plus size={14} />
            Markdown
          </button>
        </div>
        <div className="notebook-footnote">
          Shift + Enter to run · Ctrl/Cmd + ] to indent <span>·</span> Cell
          state resets when the interpreter shuts down.
        </div>
      </div>

      {settings ? (
        <Modal
          title="Notebook settings"
          wide
          onClose={() => setSettings(false)}
        >
          <form
            className="modal-form"
            onSubmit={(event: FormEvent) => {
              event.preventDefault();
              setSettings(false);
            }}
          >
            <fieldset className="plain-fieldset" disabled={!canEdit}>
              <div className="form-grid">
                <Field label="Project">
                  <select
                    value={notebook.project_id || "default"}
                    onChange={(event) => {
                      setRunClusterId("");
                      setEnvironmentId("");
                      change({
                        project_id: event.target.value,
                        connection_ids: [],
                        folder_id: null,
                        cluster_id: null,
                      });
                    }}
                  >
                    {projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Description">
                  <input
                    value={notebook.description || ""}
                    onChange={(event) =>
                      change({ description: event.target.value })
                    }
                  />
                </Field>
              </div>
              <h3>Notebook connections</h3>
              <p className="form-note">
                Only selected connections are exposed to this notebook. Project
                permissions also apply.
              </p>
              <div className="connection-checklist">
                {connections
                  .filter((item) => {
                    const project = projects.find(
                      (project) =>
                        project.id === (notebook.project_id || "default"),
                    );
                    return (
                      project?.connection_ids?.includes(item.id) ||
                      (
                        item as Connection & { project_ids?: string[] }
                      ).project_ids?.includes(notebook.project_id || "default")
                    );
                  })
                  .map((item) => (
                    <label className="checkbox-field" key={item.id}>
                      <input
                        type="checkbox"
                        checked={
                          notebook.connection_ids?.includes(item.id) || false
                        }
                        onChange={(event) =>
                          change({
                            connection_ids: event.target.checked
                              ? [...(notebook.connection_ids || []), item.id]
                              : (notebook.connection_ids || []).filter(
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
              <h3>Parameters</h3>
              <ParameterDefinitions
                value={notebook.parameter_definitions || []}
                onChange={(value) => change({ parameter_definitions: value })}
              />
            </fieldset>
            <div className="modal-footer">
              <button className="button primary">
                Done · save notebook to apply
              </button>
            </div>
          </form>
        </Modal>
      ) : null}

      {runOptions ? (
        <Modal title="Run options" onClose={() => setRunOptions(false)}>
          <div className="modal-form">
            <p className="form-note">
              Override parameter defaults for this run. Values are validated by
              the runtime.
            </p>
            <ParameterValues
              definitions={notebook.parameter_definitions || []}
              values={parameters}
              onChange={setParameters}
            />
            {!(notebook.parameter_definitions || []).length ? (
              <p className="muted">
                Define typed parameters in notebook settings first.
              </p>
            ) : null}
            <Field label="Run on compute">
              <select
                value={runClusterId}
                onChange={(event) => setRunClusterId(event.target.value)}
              >
                <option value="">Notebook default compute</option>
                {clusters
                  .filter((cluster) =>
                    projects
                      .find(
                        (project) =>
                          project.id === (notebook.project_id || "default"),
                      )
                      ?.cluster_ids?.includes(cluster.id),
                  )
                  .map((cluster) => (
                    <option key={cluster.id} value={cluster.id}>
                      {cluster.name}
                    </option>
                  ))}
              </select>
            </Field>
            <ResourceRunFields
              value={resourceOptions}
              onChange={setResourceOptions}
            />
            <Field label="Pinned package environment">
              <select
                value={environmentId}
                onChange={(event) => setEnvironmentId(event.target.value)}
              >
                <option value="">Notebook default package environment</option>
                {environments.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </Field>
            <div className="modal-footer">
              <button
                className="button"
                onClick={() => {
                  setParameters({});
                  setEnvironmentId("");
                }}
              >
                Reset defaults
              </button>
              <button
                className="button primary"
                disabled={active || busy || !canRun}
                onClick={() => {
                  setRunOptions(false);
                  start();
                }}
              >
                <Play size={14} />
                Run notebook
              </button>
            </div>
          </div>
        </Modal>
      ) : null}

      {collaborating ? (
        <CollaborationPanel
          notebookId={notebook.id}
          canEdit={permissionToEdit}
          onChanged={onChanged}
          onClose={() => {
            setCollaborating(false);
            void api<Notebook>(`/notebooks/${notebook.id}`)
              .then((value) => {
                if (alive.current) {
                  setNotebook(value);
                  setDirty(false);
                }
              })
              .catch((error) => onError(error.message));
            onChanged();
          }}
        />
      ) : null}
      {secretSelection ? (
        <Modal
          title="Notebook secrets"
          onClose={() => setSecretSelection(false)}
          wide
        >
          <div className="modal-form">
            <NotebookSecretsPicker
              notebookId={notebook.id}
              projectId={notebook.project_id || "default"}
              canEdit={permissionToEdit}
              onChanged={() => {
                void api<Notebook>(`/notebooks/${notebook.id}`)
                  .then((value) => {
                    if (alive.current) {
                      setNotebook(value);
                      setDirty(false);
                    }
                  })
                  .catch((error) => onError(error.message));
                onChanged();
              }}
            />
          </div>
        </Modal>
      ) : null}
      {showRevisions ? (
        <Modal
          title="Notebook revision history"
          wide
          onClose={() => {
            setShowRevisions(false);
            setRevision(null);
          }}
        >
          <div className="modal-form">
            {revision ? (
              <>
                <button
                  className="text-button"
                  onClick={() => setRevision(null)}
                >
                  Back to revisions
                </button>
                <div className="revision-comparison">
                  <div>
                    <h3>Saved revision</h3>
                    <pre>
                      {revision.notebook.cells
                        .map((cell) => `# ${cell.language}\n${cell.source}`)
                        .join("\n\n")}
                    </pre>
                  </div>
                  <div>
                    <h3>Current notebook</h3>
                    <pre>
                      {notebook.cells
                        .map((cell) => `# ${cell.language}\n${cell.source}`)
                        .join("\n\n")}
                    </pre>
                  </div>
                </div>
                <button
                  className="button primary"
                  disabled={busy || !canEdit}
                  onClick={() => {
                    if (
                      window.confirm(
                        "Restore this revision? Current unsaved edits will be discarded; a new revision is created.",
                      )
                    )
                      void perform(async () => {
                        setNotebook(
                          await api<Notebook>(
                            `/notebooks/${notebook.id}/revisions/${revision.id}/restore`,
                            json("POST"),
                          ),
                        );
                        setDirty(false);
                        setRevision(null);
                        setShowRevisions(false);
                        onChanged();
                      });
                  }}
                >
                  Restore revision
                </button>
              </>
            ) : (
              <>
                {revisions?.items.map((item) => (
                  <button
                    className="history-row"
                    key={item.id}
                    onClick={() =>
                      void perform(async () =>
                        setRevision(
                          await api(
                            `/notebooks/${notebook.id}/revisions/${item.id}`,
                          ),
                        ),
                      )
                    }
                  >
                    <strong>Version {item.version}</strong>
                    <span>{date(item.created_at)}</span>
                    <span>{item.reason || "Saved"}</span>
                  </button>
                ))}
                <Pagination
                  total={revisions?.total || 0}
                  offset={revisionOffset}
                  limit={20}
                  onPage={setRevisionOffset}
                />
              </>
            )}
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
