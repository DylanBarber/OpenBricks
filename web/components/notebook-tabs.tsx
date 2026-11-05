"use client";
import { useCallback, useEffect, useState } from "react";
import { Plus, X } from "lucide-react";
import {
  type Cluster,
  type Connection,
  type Notebook,
  type Project,
} from "@/lib/api";
import { Modal } from "./ui";
import { NotebookEditor } from "./notebook-editor";
import { NotebookPicker } from "./platform-controls";
export function NotebookTabs({
  notebook,
  clusters,
  projects,
  connections,
  onBack,
  onChanged,
  onError,
  onDirtyChange,
  onOpen,
  admin,
}: {
  notebook: Notebook;
  clusters: Cluster[];
  projects: Project[];
  connections: Connection[];
  onBack: () => void;
  onChanged: () => void;
  onError: (message: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  onOpen: (id: string) => void;
  admin: boolean;
}) {
  const [tabs, setTabs] = useState<Notebook[]>([notebook]),
    [active, setActive] = useState(notebook.id),
    [dirty, setDirty] = useState<Record<string, boolean>>({}),
    [picker, setPicker] = useState(false),
    [pick, setPick] = useState("");
  useEffect(() => {
    setTabs((previous) =>
      previous.some((item) => item.id === notebook.id)
        ? previous
        : [...previous, notebook],
    );
    setActive(notebook.id);
  }, [notebook]);
  useEffect(
    () => onDirtyChange(Object.values(dirty).some(Boolean)),
    [dirty, onDirtyChange],
  );
  const track = useCallback(
    (id: string, value: boolean) =>
      setDirty((previous) =>
        previous[id] === value ? previous : { ...previous, [id]: value },
      ),
    [],
  );
  function close(id: string) {
    if (
      dirty[id] &&
      !window.confirm("Discard unsaved changes in this notebook tab?")
    )
      return;
    const next = tabs.filter((item) => item.id !== id);
    setTabs(next);
    setDirty((previous) => ({ ...previous, [id]: false }));
    if (!next.length) onBack();
    else if (active === id) setActive(next[next.length - 1].id);
  }
  return (
    <>
      <div className="notebook-tabs" role="tablist" aria-label="Open notebooks">
        {tabs.map((item) => (
          <div key={item.id} className={active === item.id ? "active" : ""}>
            <button
              role="tab"
              aria-selected={active === item.id}
              onClick={() => setActive(item.id)}
            >
              {dirty[item.id] ? "● " : ""}
              {item.name}
            </button>
            <button
              aria-label={`Close ${item.name}`}
              onClick={() => close(item.id)}
            >
              <X size={12} />
            </button>
          </div>
        ))}
        <button
          className="icon-button"
          aria-label="Open another notebook"
          onClick={() => setPicker(true)}
        >
          <Plus size={15} />
        </button>
      </div>
      {tabs.map((item) => (
        <div hidden={active !== item.id} key={item.id}>
          <NotebookTab
            canEdit={
              admin ||
              ["editor", "owner"].includes(
                projects.find(
                  (project) => project.id === (item.project_id || "default"),
                )?.role || "",
              )
            }
            canRun={
              admin ||
              ["editor", "operator", "owner"].includes(
                projects.find(
                  (project) => project.id === (item.project_id || "default"),
                )?.role || "",
              )
            }
            notebook={item}
            clusters={clusters}
            projects={projects}
            connections={connections}
            onBack={() => {
              if (
                Object.values(dirty).some(Boolean) &&
                !window.confirm(
                  "Close all notebook tabs and discard unsaved edits?",
                )
              )
                return;
              onBack();
            }}
            onChanged={onChanged}
            onError={onError}
            onDirty={track}
          />
        </div>
      ))}
      {picker ? (
        <Modal title="Open a notebook" onClose={() => setPicker(false)}>
          <div className="modal-form">
            <NotebookPicker
              value={pick}
              onChange={setPick}
              projectId={notebook.project_id || "default"}
            />
            <div className="modal-footer">
              <button
                className="button primary"
                disabled={!pick}
                onClick={() => {
                  onOpen(pick);
                  setPicker(false);
                }}
              >
                Open notebook
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </>
  );
}
function NotebookTab({
  onDirty,
  ...props
}: {
  notebook: Notebook;
  clusters: Cluster[];
  projects: Project[];
  connections: Connection[];
  onBack: () => void;
  onChanged: () => void;
  onError: (message: string) => void;
  onDirty: (id: string, dirty: boolean) => void;
  canEdit: boolean;
  canRun: boolean;
}) {
  const change = useCallback(
    (dirty: boolean) => onDirty(props.notebook.id, dirty),
    [onDirty, props.notebook.id],
  );
  return <NotebookEditor {...props} onDirtyChange={change} />;
}
