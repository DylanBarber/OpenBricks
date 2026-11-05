"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from "react";
import {
  ChevronDown,
  ChevronRight,
  FileCode2,
  Folder as FolderIcon,
  FolderPlus,
  Home,
  MoveRight,
  Pencil,
  Plus,
  Search,
  Trash2,
  Upload,
} from "lucide-react";
import {
  api,
  json,
  relative,
  type Folder,
  type Notebook,
  type NotebookSummary,
  type WorkspacePage,
} from "@/lib/api";
import { Empty, Field, Modal, Pagination, SectionHead, Spinner } from "./ui";

type Props = {
  canEdit: boolean;
  projectId: string;
  refreshKey?: number;
  folderId: string | null;
  onFolderChange: (id: string | null) => void;
  search: string;
  onSearch: (value: string) => void;
  onOpen: (id: string) => void;
  onNew: () => void;
  onImport: () => void;
  onChanged: () => void;
  onError: (message: string) => void;
};
type EditTarget = {
  kind: "folder" | "notebook";
  id: string;
  name: string;
  parent_id?: string | null;
};

export function WorkspaceBrowser({
  canEdit,
  projectId,
  refreshKey,
  folderId,
  onFolderChange,
  search,
  onSearch,
  onOpen,
  onNew,
  onImport,
  onChanged,
  onError,
}: Props) {
  const [page, setPage] = useState<WorkspacePage | null>(null);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [offset, setOffset] = useState(0);
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<"create" | "rename" | "move" | null>(
    null,
  );
  const [target, setTarget] = useState<EditTarget | null>(null);
  const [formError, setFormError] = useState("");
  useEffect(() => {
    setOffset(0);
  }, [folderId, search, projectId]);
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const timer = setTimeout(
      () => {
        setLoading(true);
        const query = new URLSearchParams({
          project_id: projectId,
          limit: "50",
          offset: String(offset),
          search,
        });
        if (folderId) query.set("folder_id", folderId);
        Promise.all([
          api<WorkspacePage>(`/workspace?${query}`, {
            signal: controller.signal,
          }),
          api<Folder[]>(`/folders?project_id=${projectId}`, {
            signal: controller.signal,
          }),
        ])
          .then(([result, tree]) => {
            if (!cancelled) {
              setPage(result);
              setFolders(tree);
            }
          })
          .catch((error) => {
            if (!cancelled) onError(error.message);
          })
          .finally(() => {
            if (!cancelled) setLoading(false);
          });
      },
      search ? 250 : 0,
    );
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [folderId, search, offset, version, refreshKey, projectId, onError]);
  useEffect(() => {
    if (!folderId) return;
    setExpanded((previous) => {
      const next = new Set(previous);
      let current = folders.find((folder) => folder.id === folderId);
      const seen = new Set<string>();
      while (current && !seen.has(current.id)) {
        seen.add(current.id);
        next.add(current.id);
        current = folders.find((folder) => folder.id === current?.parent_id);
      }
      return next;
    });
  }, [folderId, folders]);
  const reload = useCallback(() => {
    setVersion((previous) => previous + 1);
    onChanged();
  }, [onChanged]);
  const children = useMemo(() => {
    const map = new Map<string | null, Folder[]>();
    for (const folder of folders)
      map.set(folder.parent_id || null, [
        ...(map.get(folder.parent_id || null) || []),
        folder,
      ]);
    for (const group of map.values())
      group.sort((a, b) => a.name.localeCompare(b.name));
    return map;
  }, [folders]);
  function toggle(id: string) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function tree(parent: string | null, depth = 0): React.ReactNode {
    if (depth > 40) return null;
    return children.get(parent)?.map((folder) => (
      <div key={folder.id}>
        <div
          className={`folder-tree-row ${folderId === folder.id ? "selected" : ""}`}
          style={{ paddingLeft: 10 + depth * 14 }}
        >
          <button
            className="tree-expander"
            aria-label={`${expanded.has(folder.id) ? "Collapse" : "Expand"} ${folder.name}`}
            aria-expanded={expanded.has(folder.id)}
            onClick={() => toggle(folder.id)}
          >
            {children.has(folder.id) ? (
              expanded.has(folder.id) ? (
                <ChevronDown size={12} />
              ) : (
                <ChevronRight size={12} />
              )
            ) : null}
          </button>
          <button
            className="folder-tree-link"
            onClick={() => {
              onFolderChange(folder.id);
              onSearch("");
            }}
          >
            <FolderIcon size={14} />
            <span>{folder.name}</span>
          </button>
        </div>
        {expanded.has(folder.id) ? tree(folder.id, depth + 1) : null}
      </div>
    ));
  }
  function begin(kind: "create" | "rename" | "move", value?: EditTarget) {
    setTarget(value || null);
    setDialog(kind);
    setFormError("");
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setFormError("");
    const values = new FormData(event.currentTarget);
    try {
      if (dialog === "create")
        await api(
          "/folders",
          json("POST", {
            name: values.get("name"),
            parent_id: folderId,
            project_id: projectId,
          }),
        );
      if (target && dialog === "rename")
        await api(
          `/folders/${target.id}`,
          json("PUT", { name: values.get("name") }),
        );
      if (target && dialog === "move") {
        const parent = values.get("parent_id") || null;
        if (target.kind === "folder")
          await api(
            `/folders/${target.id}`,
            json("PUT", { name: target.name, parent_id: parent }),
          );
        else
          await api(
            `/notebooks/${target.id}/location`,
            json("PATCH", { folder_id: parent }),
          );
      }
      setDialog(null);
      reload();
    } catch (error) {
      setFormError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function remove(value: EditTarget) {
    if (
      !window.confirm(
        `Delete ${value.kind} “${value.name}”?${value.kind === "folder" ? " Only empty folders can be deleted." : " This cannot be undone."}`,
      )
    )
      return;
    setBusy(true);
    try {
      await api(
        `/${value.kind === "folder" ? "folders" : "notebooks"}/${value.id}`,
        json("DELETE"),
      );
      reload();
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const forbidden = useMemo(() => {
    const ids = new Set<string>();
    if (target?.kind !== "folder") return ids;
    const add = (id: string) => {
      if (ids.has(id)) return;
      ids.add(id);
      children.get(id)?.forEach((folder) => add(folder.id));
    };
    add(target.id);
    return ids;
  }, [target, children]);
  const notebookRows = page?.items || [];
  return (
    <>
      <SectionHead
        eyebrow="A PLACE FOR EVERY IDEA"
        title="Workspace"
        description="Organize notebooks in folders, then pick up exactly where you left off."
        actions={
          <>
            <button className="button" disabled={!canEdit} onClick={onImport}>
              <Upload size={15} />
              Import
            </button>
            <button
              className="button"
              disabled={!canEdit}
              onClick={() => begin("create")}
            >
              <FolderPlus size={15} />
              New folder
            </button>
            <button
              className="button primary"
              disabled={!canEdit}
              onClick={onNew}
            >
              <Plus size={15} />
              New notebook
            </button>
          </>
        }
      />
      <div className="workspace-layout">
        <aside className="card folder-tree" aria-label="Notebook folders">
          <div className="folder-tree-heading">WORKSPACE</div>
          <button
            className={`folder-root ${!folderId ? "selected" : ""}`}
            onClick={() => {
              onFolderChange(null);
              onSearch("");
            }}
          >
            <Home size={14} />
            All root notebooks
          </button>
          {tree(null)}
          {!folders.length ? (
            <p className="tree-hint">
              Create folders to organize projects and pipelines.
            </p>
          ) : null}
        </aside>
        <section className="card workspace-files">
          <div className="workspace-path">
            <button
              onClick={() => {
                onFolderChange(null);
                onSearch("");
              }}
            >
              <Home size={13} />
              Workspace
            </button>
            {page?.breadcrumbs?.map((folder) => (
              <span key={folder.id}>
                <ChevronRight size={12} />
                <button
                  onClick={() => {
                    onFolderChange(folder.id);
                    onSearch("");
                  }}
                >
                  {folder.name}
                </button>
              </span>
            ))}
          </div>
          <div className="browser-toolbar">
            <label className="list-search">
              <Search size={15} />
              <input
                aria-label="Search notebooks in all folders"
                value={search}
                placeholder="Search notebooks across all folders…"
                onChange={(event) => onSearch(event.target.value)}
              />
            </label>
            {loading ? (
              <Spinner />
            ) : (
              <span className="muted">{page?.total || 0} notebooks</span>
            )}
          </div>
          {page?.folders?.length && !search ? (
            <div className="folder-list">
              {page.folders.map((folder) => (
                <div className="folder-list-item" key={folder.id}>
                  <button
                    className="name-link"
                    onClick={() => onFolderChange(folder.id)}
                  >
                    <FolderIcon size={20} />
                    <span>
                      {folder.name}
                      <small>Folder</small>
                    </span>
                  </button>
                  <div className="row-actions">
                    <button
                      className="icon-button"
                      disabled={!canEdit}
                      aria-label={`Rename folder ${folder.name}`}
                      onClick={() =>
                        begin("rename", { kind: "folder", ...folder })
                      }
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      className="icon-button"
                      disabled={!canEdit}
                      aria-label={`Move folder ${folder.name}`}
                      onClick={() =>
                        begin("move", { kind: "folder", ...folder })
                      }
                    >
                      <MoveRight size={14} />
                    </button>
                    <button
                      className="icon-button"
                      disabled={busy || !canEdit}
                      aria-label={`Delete folder ${folder.name}`}
                      onClick={() => void remove({ kind: "folder", ...folder })}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : null}
          {notebookRows.length ? (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Notebook</th>
                    <th>Language</th>
                    <th>Updated</th>
                    <th className="right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {notebookRows.map((notebook: NotebookSummary) => (
                    <tr key={notebook.id}>
                      <td>
                        <button
                          className="name-link"
                          onClick={() => onOpen(notebook.id)}
                        >
                          <span className="file-icon">
                            <FileCode2 size={17} />
                          </span>
                          <span>
                            {notebook.name}
                            <small>
                              {search
                                ? notebook.folder_path || "/Workspace"
                                : notebook.repository_id
                                  ? "Git repository"
                                  : "Notebook"}
                            </small>
                          </span>
                        </button>
                      </td>
                      <td>
                        <span className="language-tag">
                          {notebook.language === "sql" ? "SQL" : "Python"}
                        </span>
                      </td>
                      <td className="muted">{relative(notebook.updated_at)}</td>
                      <td className="right">
                        <div className="row-actions">
                          <button
                            className="icon-button"
                            disabled={!canEdit}
                            aria-label={`Move notebook ${notebook.name}`}
                            onClick={() =>
                              begin("move", {
                                kind: "notebook",
                                id: notebook.id,
                                name: notebook.name,
                                parent_id: notebook.folder_id,
                              })
                            }
                          >
                            <MoveRight size={14} />
                          </button>
                          <button
                            className="icon-button"
                            disabled={busy || !canEdit}
                            aria-label={`Delete notebook ${notebook.name}`}
                            onClick={() =>
                              void remove({
                                kind: "notebook",
                                id: notebook.id,
                                name: notebook.name,
                              })
                            }
                          >
                            <Trash2 size={13} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : !loading ? (
            <Empty
              icon={<FolderIcon size={28} />}
              title={
                search ? "No matching notebooks" : "Room for your next idea"
              }
            >
              {search
                ? "Try a different name or search term."
                : "Create a notebook here, import one, or add another folder."}
            </Empty>
          ) : (
            <div className="panel-loading">
              <Spinner />
              Loading notebooks…
            </div>
          )}
          <Pagination
            total={page?.total || 0}
            limit={50}
            offset={offset}
            onPage={setOffset}
            busy={loading}
          />
        </section>
      </div>
      {dialog ? (
        <Modal
          title={
            dialog === "create"
              ? "Create a folder"
              : `${dialog === "rename" ? "Rename" : "Move"} ${target?.name}`
          }
          onClose={() => {
            if (!busy) setDialog(null);
          }}
        >
          <form className="modal-form" onSubmit={submit}>
            {dialog === "move" ? (
              <Field label="Destination folder">
                <select name="parent_id" defaultValue={target?.parent_id || ""}>
                  <option value="">/Workspace</option>
                  {folders
                    .filter((folder) => !forbidden.has(folder.id))
                    .map((folder) => (
                      <option key={folder.id} value={folder.id}>
                        {folder.path || folder.name}
                      </option>
                    ))}
                </select>
              </Field>
            ) : (
              <Field label="Folder name">
                <input
                  name="name"
                  required
                  autoFocus
                  maxLength={180}
                  defaultValue={target?.name || ""}
                  placeholder="e.g. Finance analytics"
                />
              </Field>
            )}
            {formError ? (
              <div className="inline-error" role="alert">
                {formError}
              </div>
            ) : null}
            <div className="modal-footer">
              <button
                className="button"
                type="button"
                disabled={busy || !canEdit}
                onClick={() => setDialog(null)}
              >
                Cancel
              </button>
              <button className="button primary" disabled={busy || !canEdit}>
                {busy ? <Spinner /> : <FolderIcon size={14} />}
                {dialog === "create" ? "Create folder" : "Save changes"}
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
    </>
  );
}
