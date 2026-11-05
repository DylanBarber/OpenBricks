"use client";
import dynamic from "next/dynamic";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  GitBranch,
  GitCommitHorizontal,
  GitPullRequest,
  RefreshCw,
  Upload,
} from "lucide-react";
import { api, json } from "@/lib/api";
import { Empty, Field, Modal, Spinner } from "./ui";
import { NotebookPicker } from "./platform-controls";

const CodeEditor = dynamic(() => import("./code-editor"), { ssr: false });
type GitStatus = {
  branch: string | null;
  commit: string;
  branches: string[];
  changes: { path: string; status: string }[];
  workspace_changes: {
    id: string;
    name: string;
    path: string;
    version: number;
    modified: boolean;
  }[];
  truncated: boolean;
  has_github_token: boolean;
};
type OpenFile = { path: string; source: string; sha256: string };
type EditorFile = OpenFile & { savedSource: string };

export default function GitWorkspacePanel({
  repositoryId,
  projectId,
  role,
  isAdmin = false,
  onChanged,
  onError,
}: {
  repositoryId: string;
  projectId: string;
  role: string;
  isAdmin?: boolean;
  onChanged?: () => void;
  onError: (message: string) => void;
}) {
  const [loaded, setLoaded] = useState<{ id: string; value: GitStatus } | null>(
      null,
    ),
    [version, setVersion] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [message, setMessage] = useState("");
  const [modal, setModal] = useState<
      "branch" | "commit" | "pr" | "credential" | "link" | null
    >(null),
    [selectedPaths, setSelectedPaths] = useState<string[]>([]),
    [selectedNotebooks, setSelectedNotebooks] = useState<string[]>([]),
    [file, setFile] = useState<EditorFile | null>(null),
    [diff, setDiff] = useState<string>(""),
    [newPath, setNewPath] = useState(""),
    [linkNotebook, setLinkNotebook] = useState(""),
    [linkPath, setLinkPath] = useState("");
  const [branch, setBranch] = useState(""),
    [commitMessage, setCommitMessage] = useState(""),
    [authorName, setAuthorName] = useState(""),
    [authorEmail, setAuthorEmail] = useState(""),
    [prTitle, setPrTitle] = useState(""),
    [prBody, setPrBody] = useState(""),
    [prBase, setPrBase] = useState("main"),
    [token, setToken] = useState("");
  const canEdit = ["admin", "owner", "editor"].includes(role),
    prefix = `/repositories/${encodeURIComponent(repositoryId)}/git`,
    status = loaded?.id === repositoryId ? loaded.value : null;
  const previousHead = useRef<string | null>(null);
  const invalidateFilePreview = useCallback(() => {
    setDiff("");
    setFile((current) =>
      current && current.source !== current.savedSource ? current : null,
    );
  }, []);
  useEffect(() => {
    if (
      previousHead.current &&
      status?.commit &&
      previousHead.current !== status.commit
    ) {
      invalidateFilePreview();
    }
    previousHead.current = status?.commit || null;
  }, [status?.commit, invalidateFilePreview]);
  useEffect(() => {
    let stopped = false;
    api<GitStatus>(`${prefix}/status`)
      .then((value) => {
        if (!stopped) setLoaded({ id: repositoryId, value });
      })
      .catch((failure: Error) => {
        if (!stopped) setError(failure.message);
      });
    return () => {
      stopped = true;
    };
  }, [prefix, repositoryId, version]);
  useEffect(() => {
    setLoaded(null);
    setSelectedPaths([]);
    setSelectedNotebooks([]);
    setFile(null);
    setDiff("");
    setError("");
    setMessage("");
    setModal(null);
    setToken("");
  }, [repositoryId, projectId]);
  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await work();
      setVersion((value) => value + 1);
      onChanged?.();
    } catch (failure) {
      const value = (failure as Error).message;
      setError(value);
      onError(value);
    } finally {
      setBusy(false);
    }
  }
  const toggle = (items: string[], value: string) =>
    items.includes(value)
      ? items.filter((item) => item !== value)
      : [...items, value];
  function close() {
    if (!busy) {
      setModal(null);
      setToken("");
    }
  }
  async function openFile(path: string) {
    await perform(async () => {
      const [opened, patch] = await Promise.all([
        api<OpenFile>(`${prefix}/file?path=${encodeURIComponent(path)}`),
        api<{ text: string; truncated: boolean }>(
          `${prefix}/diff?path=${encodeURIComponent(path)}`,
        ),
      ]);
      setFile({ ...opened, savedSource: opened.source });
      setDiff(
        patch.text + (patch.truncated ? "\n… diff preview truncated" : ""),
      );
    });
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    await perform(async () => {
      if (modal === "branch") {
        await api(`${prefix}/branch`, json("POST", { name: branch }));
        invalidateFilePreview();
      } else if (modal === "commit") {
        await api(
          `${prefix}/commit`,
          json("POST", {
            message: commitMessage,
            paths: selectedPaths,
            author_name: authorName || undefined,
            author_email: authorEmail || undefined,
          }),
        );
        invalidateFilePreview();
      } else if (modal === "credential") {
        await api(`${prefix}/github-credential`, json("PUT", { token }));
        setToken("");
      } else if (modal === "link") {
        const notebook = await api<{ version: number }>(
          `/notebooks/${linkNotebook}`,
        );
        await api(
          `${prefix}/link-notebook`,
          json("POST", {
            notebook_id: linkNotebook,
            path: linkPath,
            version: notebook.version,
          }),
        );
        setMessage(
          "Notebook connected. Select it below to export and stage its saved source.",
        );
      } else if (modal === "pr") {
        const value = await api<{ url: string }>(
          `${prefix}/pull-request`,
          json("POST", { title: prTitle, body: prBody, base: prBase }),
        );
        setMessage(`Pull request created: ${value.url}`);
      }
      setModal(null);
      setSelectedPaths([]);
    });
  }
  return (
    <div className="platform-stack">
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
      <section className="card platform-form">
        <div className="card-heading">
          <div>
            <h3>Git working tree</h3>
            <p className="form-note">
              Review source changes before committing. Import conflicts preserve
              workspace edits.
            </p>
          </div>
          <button
            className="button compact"
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                setDiff("");
                const latest = await api<GitStatus>(`${prefix}/status`);
                setLoaded({ id: repositoryId, value: latest });
                if (file) {
                  const patch = await api<{ text: string; truncated: boolean }>(
                    `${prefix}/diff?path=${encodeURIComponent(file.path)}`,
                  );
                  setDiff(
                    patch.text +
                      (patch.truncated ? "\n… diff preview truncated" : ""),
                  );
                }
              })
            }
          >
            <RefreshCw size={14} />
            Refresh
          </button>
        </div>
        {status ? (
          <>
            <div className="form-grid">
              <Field label="Current branch">
                <select
                  aria-label="Current Git branch"
                  disabled={busy || !canEdit}
                  value={status.branch || ""}
                  onChange={(event) =>
                    void perform(async () => {
                      await api(
                        `${prefix}/branch`,
                        json("POST", {
                          name: event.target.value,
                          create: false,
                        }),
                      );
                      invalidateFilePreview();
                    })
                  }
                >
                  {!status.branch ? (
                    <option value="">Detached commit</option>
                  ) : null}
                  {status.branches.map((name) => (
                    <option key={name}>{name}</option>
                  ))}
                </select>
              </Field>
              <Field label="Exact commit">
                <input value={status.commit} readOnly />
              </Field>
            </div>
            <div className="row-actions">
              <button
                className="button"
                disabled={busy || !canEdit}
                onClick={() => {
                  setBranch("");
                  setModal("branch");
                }}
              >
                <GitBranch size={15} />
                New branch
              </button>
              <button
                className="button"
                disabled={busy || !canEdit || !selectedPaths.length}
                onClick={() => setModal("commit")}
              >
                <GitCommitHorizontal size={15} />
                Commit selected
              </button>
              <button
                className="button"
                disabled={busy || !canEdit || !status.branch}
                onClick={() =>
                  void perform(async () => {
                    await api(`${prefix}/push`, json("POST", {}));
                    setMessage("Branch pushed successfully.");
                  })
                }
              >
                <Upload size={15} />
                Push branch
              </button>
              <button
                className="button"
                disabled={busy || !canEdit || !status.branch}
                onClick={() => setModal("pr")}
              >
                <GitPullRequest size={15} />
                Create pull request
              </button>
              {isAdmin ? (
                <button
                  className="button"
                  disabled={busy}
                  onClick={() => {
                    setToken("");
                    setModal("credential");
                  }}
                >
                  {status.has_github_token
                    ? "Rotate GitHub credential"
                    : "Configure GitHub credential"}
                </button>
              ) : null}
            </div>
          </>
        ) : (
          <Spinner />
        )}
      </section>
      <section className="card platform-form">
        <div className="card-heading">
          <h3>Workspace notebook changes</h3>
          <button
            className="button compact"
            disabled={busy || !canEdit}
            onClick={() => {
              setLinkNotebook("");
              setLinkPath("");
              setModal("link");
            }}
          >
            Connect a workspace notebook
          </button>
        </div>
        <p className="form-note">
          Export selected saved notebook versions into this checkout, then
          review and commit their diffs.
        </p>
        {status?.workspace_changes.length ? (
          <>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Select</th>
                  <th>Notebook</th>
                  <th>Source path</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {status.workspace_changes.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Export ${item.name}`}
                        disabled={busy || !canEdit}
                        checked={selectedNotebooks.includes(item.id)}
                        onChange={() =>
                          setSelectedNotebooks((values) =>
                            toggle(values, item.id),
                          )
                        }
                      />
                    </td>
                    <td>{item.name}</td>
                    <td>
                      <code>{item.path}</code>
                    </td>
                    <td>
                      {item.modified
                        ? "Workspace edited"
                        : "Matches imported source"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button
              className="button"
              disabled={busy || !canEdit || !selectedNotebooks.length}
              onClick={() =>
                void perform(async () => {
                  const versions = Object.fromEntries(
                    status.workspace_changes
                      .filter((item) => selectedNotebooks.includes(item.id))
                      .map((item) => [item.id, item.version]),
                  );
                  const result = await api<GitStatus>(
                    `${prefix}/stage-notebooks`,
                    json("POST", { notebook_ids: selectedNotebooks, versions }),
                  );
                  setSelectedPaths(result.changes.map((item) => item.path));
                  setSelectedNotebooks([]);
                  setMessage(
                    "Notebook source exported and staged. Review the diff before committing.",
                  );
                })
              }
            >
              Export and stage notebooks
            </button>
          </>
        ) : (
          <p className="form-note">
            Import notebooks from this repository to connect workspace copies.
          </p>
        )}
      </section>
      <section className="card platform-form">
        <h3>Changed files</h3>
        {status?.changes.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Select</th>
                <th>Path</th>
                <th>Git state</th>
              </tr>
            </thead>
            <tbody>
              {status.changes.map((item) => (
                <tr key={item.path}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Commit ${item.path}`}
                      disabled={busy || !canEdit}
                      checked={selectedPaths.includes(item.path)}
                      onChange={() =>
                        setSelectedPaths((values) => toggle(values, item.path))
                      }
                    />
                  </td>
                  <td>
                    <button
                      className="text-button"
                      onClick={() => void openFile(item.path)}
                      disabled={busy}
                    >
                      {item.path}
                    </button>
                  </td>
                  <td>
                    <code>{item.status}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty title="Working tree is clean">
            Changes appear here after editing a file or exporting workspace
            notebooks.
          </Empty>
        )}
        {status?.truncated ? (
          <p className="form-note">
            This is a bounded preview. Use a local Git client for larger working
            trees.
          </p>
        ) : null}
        <div className="form-grid">
          <Field label="Open a repository file">
            <input
              value={newPath}
              onChange={(event) => setNewPath(event.target.value)}
              placeholder="notebooks/bronze.py"
            />
          </Field>
          <div className="row-actions">
            <button
              className="button"
              disabled={busy || !newPath}
              onClick={() => void openFile(newPath)}
            >
              Open source and diff
            </button>
            <button
              className="button"
              disabled={busy || !canEdit || !newPath}
              onClick={() =>
                void perform(async () => {
                  const source = newPath.endsWith(".py")
                    ? "# Databricks notebook source\nprint('Hello, OpenBricks')\n"
                    : newPath.endsWith(".sql")
                      ? "SELECT 1;\n"
                      : "";
                  const saved = await api<{ sha256: string }>(
                    `${prefix}/file`,
                    json("PUT", {
                      path: newPath,
                      source,
                      expected_sha256:
                        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    }),
                  );
                  setFile({
                    path: newPath,
                    source,
                    savedSource: source,
                    sha256: saved.sha256,
                  });
                  setDiff("");
                  setSelectedPaths((values) =>
                    values.includes(newPath) ? values : [...values, newPath],
                  );
                })
              }
            >
              Create new file
            </button>
          </div>
        </div>
      </section>
      {file ? (
        <section className="card platform-form">
          <div className="card-heading">
            <h3>{file.path}</h3>
            <button
              className="button"
              disabled={busy || !canEdit}
              onClick={() =>
                void perform(async () => {
                  const saved = await api<{ sha256: string }>(
                    `${prefix}/file`,
                    json("PUT", {
                      path: file.path,
                      source: file.source,
                      expected_sha256: file.sha256,
                    }),
                  );
                  setFile({
                    ...file,
                    savedSource: file.source,
                    sha256: saved.sha256,
                  });
                  const result = await api<{ text: string }>(
                    `${prefix}/diff?path=${encodeURIComponent(file.path)}`,
                  );
                  setDiff(result.text);
                  setSelectedPaths((values) =>
                    values.includes(file.path)
                      ? values
                      : [...values, file.path],
                  );
                  setMessage("Source saved with conflict protection.");
                })
              }
            >
              Save working-tree file
            </button>
          </div>
          <CodeEditor
            label={`Git source ${file.path}`}
            language={
              file.path.endsWith(".sql")
                ? "sql"
                : file.path.endsWith(".md")
                  ? "markdown"
                  : "python"
            }
            value={file.source}
            onChange={(source) => setFile({ ...file, source })}
            disabled={busy || !canEdit}
          />
          <h4>Diff against HEAD</h4>
          <pre
            style={{ maxHeight: 360, overflow: "auto", whiteSpace: "pre-wrap" }}
          >
            {diff || "No tracked changes against HEAD."}
          </pre>
        </section>
      ) : null}
      {modal ? (
        <Modal
          title={
            modal === "branch"
              ? "Create a feature branch"
              : modal === "commit"
                ? "Commit selected source"
                : modal === "pr"
                  ? "Create GitHub pull request"
                  : modal === "link"
                    ? "Connect notebook to Git"
                    : "GitHub repository credential"
          }
          onClose={close}
        >
          <form className="modal-form" onSubmit={submit}>
            {modal === "link" ? (
              <>
                <NotebookPicker
                  value={linkNotebook}
                  onChange={setLinkNotebook}
                  projectId={projectId}
                />
                <Field
                  label="New repository path"
                  hint="Choose a new .py, .sql, or .ipynb path. Existing repository files are preserved."
                >
                  <input
                    required
                    value={linkPath}
                    onChange={(event) => setLinkPath(event.target.value)}
                    placeholder="notebooks/new_analysis.py"
                  />
                </Field>
              </>
            ) : modal === "branch" ? (
              <Field label="Branch name">
                <input
                  autoFocus
                  required
                  value={branch}
                  onChange={(event) => setBranch(event.target.value)}
                  placeholder="feature/daily-orders"
                />
              </Field>
            ) : modal === "commit" ? (
              <>
                <Field label="Commit message">
                  <textarea
                    autoFocus
                    required
                    value={commitMessage}
                    onChange={(event) => setCommitMessage(event.target.value)}
                  />
                </Field>
                <div className="form-grid">
                  <Field label="Author name (optional)">
                    <input
                      value={authorName}
                      onChange={(event) => setAuthorName(event.target.value)}
                    />
                  </Field>
                  <Field label="Author email (optional)">
                    <input
                      type="email"
                      value={authorEmail}
                      onChange={(event) => setAuthorEmail(event.target.value)}
                    />
                  </Field>
                </div>
                <p className="form-note">
                  {selectedPaths.length} selected files. Blank author fields use
                  your workspace username and an openbricks.local address.
                </p>
              </>
            ) : modal === "credential" ? (
              <>
                <Field
                  label="GitHub token"
                  hint="Repository-scoped token for HTTPS pushes and pull requests. Leave blank to keep the saved credential."
                >
                  <input
                    type="password"
                    autoComplete="off"
                    value={token}
                    onChange={(event) => setToken(event.target.value)}
                  />
                </Field>
                <p className="form-note">
                  The token is encrypted on the server and is never returned.
                  SSH pushes use your server&apos;s configured SSH identity.
                </p>
              </>
            ) : (
              <>
                <Field label="Pull request title">
                  <input
                    required
                    autoFocus
                    value={prTitle}
                    onChange={(event) => setPrTitle(event.target.value)}
                  />
                </Field>
                <Field label="Base branch">
                  <input
                    required
                    value={prBase}
                    onChange={(event) => setPrBase(event.target.value)}
                  />
                </Field>
                <Field label="Description">
                  <textarea
                    rows={6}
                    value={prBody}
                    onChange={(event) => setPrBody(event.target.value)}
                  />
                </Field>
                <p className="form-note">
                  Push the current branch before creating the pull request. This
                  sends the title and description to GitHub.
                </p>
              </>
            )}
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
                onClick={close}
              >
                Cancel
              </button>
              <button className="button primary" disabled={busy}>
                {busy ? <Spinner /> : null}
                {modal === "branch"
                  ? "Create branch"
                  : modal === "commit"
                    ? "Commit files"
                    : modal === "pr"
                      ? "Create pull request"
                      : modal === "link"
                        ? "Connect notebook"
                        : "Save credential"}
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
    </div>
  );
}
