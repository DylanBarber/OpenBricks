"use client";

import dynamic from "next/dynamic";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  BookOpen,
  Box,
  Braces,
  CalendarClock,
  Check,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clock3,
  Code2,
  Cpu,
  Database,
  ExternalLink,
  FileCode2,
  FileUp,
  Folder,
  FolderGit2,
  GitBranch,
  HardDrive,
  House,
  Layers3,
  LockKeyhole,
  LogOut,
  Menu,
  MoreHorizontal,
  Package as PackageIcon,
  Play,
  Plus,
  RefreshCw,
  Search,
  Server,
  Settings2,
  ShieldCheck,
  Sparkles,
  Terminal,
  Trash2,
  Upload,
  Users,
  X,
} from "lucide-react";
import {
  ApiError,
  api,
  bytes,
  date,
  json,
  newCellId,
  relative,
  type Bucket,
  type Cluster,
  type Connection,
  type Notebook,
  type NotebookSummary,
  type Overview,
  type Project,
  type Run,
  type StorageObject,
  type User,
} from "@/lib/api";
import {
  BrickMark,
  Crumb,
  Empty,
  Field,
  Modal,
  SectionHead,
  Spinner,
  Status,
} from "./ui";
import { NotebookTabs } from "./notebook-tabs";
import { ProjectSelect } from "./platform-controls";
import { RunActivity } from "./run-activity";
const WorkflowsPanel = dynamic(() => import("./workflows-panel"));
const DataWorkspace = dynamic(() => import("./data-workspace"));
const ProjectsPanel = dynamic(() => import("./projects-panel"));
const OperationsPanel = dynamic(() => import("./operations-panel"));
const ReleasesPanel = dynamic(() => import("./releases-panel"));
import { ThemeToggle } from "./theme-toggle";
import { WorkspaceBrowser } from "./workspace-browser";
import { RunBrowser } from "./run-browser";
import { SchedulesPanel } from "./schedules-panel";
import { ClustersPanel } from "./clusters-panel";
import { ConnectionsPanel, ConnectedStorage } from "./connections-panel";
import { CatalogPanel } from "./catalog-panel";

type View =
  | "home"
  | "workspace"
  | "jobs"
  | "runs"
  | "repositories"
  | "packages"
  | "storage"
  | "compute"
  | "connections"
  | "catalog"
  | "settings"
  | "workflows"
  | "data"
  | "projects"
  | "operations"
  | "releases";
const viewNames: Record<View, string> = {
  workflows: "Workflows",
  data: "Data workspace",
  projects: "Projects & access",
  operations: "Operations",
  releases: "Releases & environments",
  home: "Overview",
  workspace: "Workspace",
  jobs: "Jobs & schedules",
  runs: "Run history",
  repositories: "Git repositories",
  packages: "Packages",
  storage: "Object storage",
  compute: "Compute clusters",
  connections: "Connections",
  catalog: "Data catalog",
  settings: "Team & settings",
};
const navigation = [
  {
    group: "WORKSPACE",
    items: [
      { id: "home", label: "Overview", icon: House },
      { id: "workspace", label: "Notebooks", icon: BookOpen },
      { id: "workflows", label: "Workflows", icon: GitBranch },
      { id: "jobs", label: "Jobs & schedules", icon: CalendarClock },
      { id: "runs", label: "Run history", icon: Activity },
    ],
  },
  {
    group: "DEVELOP",
    items: [
      { id: "data", label: "Data workspace", icon: Database },
      { id: "repositories", label: "Git repositories", icon: FolderGit2 },
      { id: "releases", label: "Releases", icon: GitBranch },
      { id: "packages", label: "Packages", icon: Box },
      { id: "connections", label: "Connections", icon: Database },
      { id: "catalog", label: "Data catalog", icon: Layers3 },
    ],
  },
  {
    group: "INFRASTRUCTURE",
    items: [
      { id: "projects", label: "Projects & access", icon: Users },
      { id: "operations", label: "Operations", icon: Settings2 },
      { id: "storage", label: "Object storage", icon: Database },
      { id: "compute", label: "Compute clusters", icon: Cpu },
    ],
  },
];

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      onLogin(
        await api<User>(
          "/auth/login",
          json("POST", {
            username: form.get("username"),
            password: form.get("password"),
          }),
        ),
      );
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <div className="login-brand">
        <div className="brand">
          <BrickMark />
          <span>openbricks</span>
        </div>
        <div className="login-hero">
          <div className="overline">
            OPEN SOURCE. YOUR INFRASTRUCTURE. ALL YOURS.
          </div>
          <h1>
            Big ideas.
            <br />
            Your compute.
            <br />
            <span>Room to grow.</span>
          </h1>
          <p>
            Your notebooks, your Spark, your data.
            <br />
            An open data workspace across your servers.
          </p>
          <div className="login-chips">
            <span>
              <Code2 size={15} />
              Python + SQL
            </span>
            <span>
              <CalendarClock size={15} />
              Scheduled jobs
            </span>
            <span>
              <HardDrive size={15} />
              Shared storage
            </span>
          </div>
        </div>
        <div className="login-art" aria-hidden="true">
          <div className="art-orbit orbit-one" />
          <div className="art-orbit orbit-two" />
          <div className="art-cube">
            <BrickMark />
          </div>
          <div className="art-label">
            <span className="connected-dot" /> YOUR LOCAL DATA PLATFORM
          </div>
        </div>
        <div className="login-bottom">
          Built for the way you work. Hosted where you choose.
        </div>
      </div>
      <div className="login-form-side">
        <div className="login-theme">
          <ThemeToggle />
        </div>
        <div className="login-form">
          <div className="login-icon">
            <LockKeyhole size={23} />
          </div>
          <div className="eyebrow">YOUR WORKSPACE IS WAITING</div>
          <h2>Welcome to OpenBricks</h2>
          <p>Sign in to your local data workspace.</p>
          <form onSubmit={submit}>
            <Field label="Username">
              <input
                name="username"
                autoComplete="username"
                required
                placeholder="Your username"
                autoFocus
              />
            </Field>
            <Field label="Password">
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                required
                placeholder="Your password"
              />
            </Field>
            {error ? (
              <div className="inline-error" role="alert">
                {error}
              </div>
            ) : null}
            <button className="button primary login-submit" disabled={busy}>
              {busy ? <Spinner /> : null}Sign in to workspace
              <ArrowRight size={16} />
            </button>
          </form>
          <div className="login-note">
            <ShieldCheck size={17} />
            <span>
              Use the admin account configured on your server, or an account
              your workspace admin created.
            </span>
          </div>
        </div>
        <div className="login-footer">
          <BrickMark small />
          <span>An open home for your data work.</span>
          <span>v0.3 beta</span>
        </div>
      </div>
    </div>
  );
}

function NotebookTable({
  notebooks,
  onOpen,
  onDelete,
  limit,
}: {
  notebooks: NotebookSummary[];
  onOpen: (id: string) => void;
  onDelete?: (id: string) => void;
  limit?: number;
}) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Language</th>
            <th>Last modified</th>
            <th className="right">{onDelete ? "Actions" : ""}</th>
          </tr>
        </thead>
        <tbody>
          {notebooks.slice(0, limit).map((notebook) => (
            <tr key={notebook.id}>
              <td>
                <button
                  className="name-link"
                  onClick={() => onOpen(notebook.id)}
                >
                  <span
                    className={`file-icon ${notebook.language === "sql" ? "sql" : ""}`}
                  >
                    <FileCode2 size={17} />
                  </span>
                  <span>
                    {notebook.name}
                    <small>
                      {notebook.repository_id ? "Git repository" : "Workspace"}
                    </small>
                  </span>
                </button>
              </td>
              <td>
                <span
                  className={`language-tag ${notebook.language === "sql" ? "sql" : ""}`}
                >
                  {notebook.language === "sql" ? "SQL" : "Python"}
                </span>
              </td>
              <td className="muted">{relative(notebook.updated_at)}</td>
              <td className="right">
                {onDelete ? (
                  <button
                    className="icon-button"
                    aria-label={`Delete ${notebook.name}`}
                    onClick={() => onDelete(notebook.id)}
                  >
                    <Trash2 size={14} />
                  </button>
                ) : (
                  <button
                    className="icon-button"
                    aria-label={`Open ${notebook.name}`}
                    onClick={() => onOpen(notebook.id)}
                  >
                    <ArrowRight size={15} />
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunTable({
  runs,
  onSelect,
  onOpen,
}: {
  runs: Run[];
  onSelect: (run: Run) => void;
  onOpen: (id: string) => void;
}) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>Notebook</th>
            <th>Status</th>
            <th>Started</th>
            <th>Trigger</th>
            <th className="right">Details</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td>
                <button
                  className="name-link"
                  onClick={() => onOpen(run.notebook_id)}
                >
                  <span className="file-icon">
                    <FileCode2 size={16} />
                  </span>
                  <span>
                    {run.notebook_name || "Notebook"}
                    <small>{run.id.slice(0, 8)}</small>
                  </span>
                </button>
              </td>
              <td>
                <Status status={run.status} />
              </td>
              <td className="muted">
                {date(run.started_at || run.created_at)}
              </td>
              <td>
                <span className="subtle-tag">{run.trigger || "manual"}</span>
              </td>
              <td className="right">
                <button
                  className="icon-button"
                  aria-label="View run details"
                  onClick={() => onSelect(run)}
                >
                  <ChevronRight size={16} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ResourceBar({
  title,
  used,
  total,
  icon,
}: {
  title: string;
  used?: number;
  total?: number;
  icon: ReactNode;
}) {
  const percent = total
    ? Math.min(100, Math.round(((used || 0) / total) * 100))
    : 0;
  return (
    <div className="resource">
      <div className="resource-label">
        <span>
          {icon}
          {title}
        </span>
        <strong>{total ? `${percent}%` : "—"}</strong>
      </div>
      <div className="meter">
        <span style={{ width: `${percent}%` }} />
      </div>
      <small>
        {bytes(used)} <span>/ {bytes(total)}</span>
      </small>
    </div>
  );
}

export function OpenBricks() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [data, setData] = useState<Overview | null>(null);
  const [view, setView] = useState<View>("home");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("default");
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [folderId, setFolderId] = useState<string | null>(null);
  const [workspaceVersion, setWorkspaceVersion] = useState(0);
  const [storageTab, setStorageTab] = useState<"local" | "connected">("local");
  const [storageConnectionId, setStorageConnectionId] = useState<string | null>(
    null,
  );
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState<string | null>(null);
  const [selected, setSelected] = useState<Notebook | null>(null);
  const [editorDirty, setEditorDirty] = useState(false);
  const [selectedRun, setSelectedRun] = useState<Run | null>(null);
  const [bucket, setBucket] = useState<Bucket | null>(null);
  const [objects, setObjects] = useState<StorageObject[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [mobileOpen, setMobileOpen] = useState(false);
  const notebookUpload = useRef<HTMLInputElement>(null);
  const wheelUpload = useRef<HTMLInputElement>(null);
  const objectUpload = useRef<HTMLInputElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const notifyError = useCallback((message: string) => setError(message), []);
  const refresh = useCallback(async () => {
    try {
      const availableProjects = await api<Project[]>("/projects");
      setProjects(availableProjects);
      const activeProject = availableProjects.some(
        (project) => project.id === projectId,
      )
        ? projectId
        : availableProjects[0]?.id || "";
      if (activeProject !== projectId) setProjectId(activeProject);
      const [overview, computeClusters, savedConnections] = await Promise.all([
        api<Overview>(`/overview?project_id=${activeProject}`),
        api<Cluster[]>("/clusters"),
        api<Connection[]>("/connections"),
      ]);
      setData(overview);
      setClusters(computeClusters);
      setConnections(savedConnections);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) setUser(null);
      else setError((failure as Error).message);
    }
  }, [projectId]);
  useEffect(() => {
    let cancelled = false;
    api<User>("/auth/me")
      .then((value) => {
        if (!cancelled) setUser(value);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setChecking(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  useEffect(() => {
    if (!user) return;
    void refresh();
    const interval = setInterval(() => void refresh(), 15000);
    return () => clearInterval(interval);
  }, [user, refresh]);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "k") {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 5000);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    if (view === "settings" && user?.role === "admin")
      api<User[]>("/users")
        .then(setUsers)
        .catch((failure) => setError(failure.message));
  }, [view, user]);
  useEffect(() => {
    if (!selectedRun || !["queued", "running"].includes(selectedRun.status))
      return;
    let stopped = false;
    const interval = setInterval(
      () =>
        api<Run>(`/runs/${selectedRun.id}`)
          .then((run) => {
            if (!stopped) setSelectedRun(run);
          })
          .catch((failure) => {
            if (!stopped) setError(failure.message);
          }),
      1500,
    );
    return () => {
      stopped = true;
      clearInterval(interval);
    };
  }, [selectedRun?.id, selectedRun?.status]);
  function navigate(next: View) {
    if (
      editorDirty &&
      !window.confirm("Leave this notebook and discard unsaved changes?")
    )
      return;
    setEditorDirty(false);
    setView(next);
    setSelected(null);
    setSearch("");
    setMobileOpen(false);
  }
  async function action(work: () => Promise<void>, success?: string) {
    setBusy(true);
    setError("");
    try {
      await work();
      if (success) setToast(success);
      setWorkspaceVersion((value) => value + 1);
      await refresh();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const openNotebook = useCallback(async (id: string) => {
    try {
      const notebook = await api<Notebook>(`/notebooks/${id}`);
      setSelected(notebook);
      setFolderId(notebook.folder_id || null);
      setView("workspace");
    } catch (failure) {
      setError((failure as Error).message);
    }
  }, []);
  const openRun = useCallback(async (id: string) => {
    try {
      setSelectedRun(await api<Run>(`/runs/${id}`));
    } catch (failure) {
      setError((failure as Error).message);
    }
  }, []);
  async function upload(
    file: File | undefined,
    path: string,
    message: string,
    extras?: Record<string, string>,
  ) {
    if (!file) return;
    await action(async () => {
      const form = new FormData();
      form.append("file", file);
      if (path === "/notebooks/import") form.append("project_id", projectId);
      if (path === "/notebooks/import" && folderId)
        form.append("folder_id", folderId);
      Object.entries(extras || {}).forEach(([key, value]) =>
        form.append(key, value),
      );
      await api(path, { method: "POST", body: form });
      if (bucket)
        setObjects(
          await api<StorageObject[]>(
            `/buckets/${encodeURIComponent(bucket.name)}/objects`,
          ),
        );
    }, message);
  }
  async function openBucket(value: Bucket) {
    await action(async () => {
      setObjects(
        await api<StorageObject[]>(
          `/buckets/${encodeURIComponent(value.name)}/objects`,
        ),
      );
      setBucket(value);
    });
  }
  async function submitModal(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    await action(
      async () => {
        if (modal === "notebook") {
          if (
            editorDirty &&
            !window.confirm(
              "Create a new notebook and discard unsaved changes in the current notebook?",
            )
          )
            return;
          const language = values.language as "python" | "sql";
          const notebook = await api<Notebook>(
            "/notebooks",
            json("POST", {
              name: values.name,
              language,
              project_id: projectId,
              connection_ids: [],
              parameter_definitions: [],
              folder_id: folderId,
              cluster_id: values.cluster_id || null,
              cells: [
                {
                  id: newCellId(),
                  language,
                  source:
                    language === "sql"
                      ? "SELECT 'Hello, OpenBricks' AS message;"
                      : "# Your next big idea starts here\nprint('Hello, OpenBricks')",
                },
              ],
            }),
          );
          setEditorDirty(false);
          setSelected(notebook);
          setView("workspace");
        }
        if (modal === "job")
          await api("/jobs", json("POST", { ...values, enabled: true }));
        if (modal === "repository")
          await api(
            "/repositories",
            json("POST", { ...values, project_id: projectId }),
          );
        if (modal === "bucket") await api("/buckets", json("POST", values));
        if (modal === "build")
          await api("/packages/build", json("POST", values));
        if (modal === "user") {
          await api("/users", json("POST", values));
          setUsers(await api<User[]>("/users"));
        }
        setModal(null);
      },
      modal === "build"
        ? "Wheel built and installed in the local runtime."
        : "Saved to your workspace.",
    );
  }
  const notebooks = (data?.notebooks || []).filter((item) =>
    item.name.toLowerCase().includes(search.toLowerCase()),
  );
  const jobs = (data?.jobs || []).filter((item) =>
    item.name.toLowerCase().includes(search.toLowerCase()),
  );
  const repositories = (data?.repositories || []).filter((item) =>
    item.name.toLowerCase().includes(search.toLowerCase()),
  );
  const packages = (data?.packages || []).filter((item) =>
    `${item.name} ${item.filename}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const buckets = (data?.buckets || []).filter((item) =>
    item.name.toLowerCase().includes(search.toLowerCase()),
  );
  const filteredRuns = (data?.runs || []).filter((item) =>
    `${item.notebook_name} ${item.status}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const compute = data?.compute;
  const currentRole =
    user?.role === "admin"
      ? "owner"
      : projects.find((project) => project.id === projectId)?.role || "viewer";
  const canEditProject = ["editor", "owner"].includes(currentRole);
  const selectedRunRole =
    user?.role === "admin"
      ? "owner"
      : projects.find(
          (project) => project.id === (selectedRun?.project_id || projectId),
        )?.role || "viewer";
  const canOperateSelectedRun = ["operator", "editor", "owner"].includes(
    selectedRunRole,
  );
  if (checking)
    return (
      <div className="boot-screen">
        <BrickMark />
        <Spinner />
        <span>Connecting to your workspace…</span>
      </div>
    );
  if (!user)
    return (
      <Login
        onLogin={(value) => {
          setUser(value);
          setData(null);
          setError("");
        }}
      />
    );
  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileOpen ? "mobile-open" : ""}`}>
        <a
          className="brand"
          href="#"
          onClick={(event) => {
            event.preventDefault();
            navigate("home");
          }}
        >
          <BrickMark />
          <span>openbricks</span>
        </a>
        <button
          className="workspace-switch"
          onClick={() => navigate("compute")}
        >
          <span className="workspace-avatar">
            <Layers3 size={16} />
          </span>
          <span>
            My workspace<small>Open compute platform</small>
          </span>
          <ChevronDown size={14} />
        </button>
        <button
          className="button primary sidebar-create"
          disabled={!canEditProject}
          onClick={() => setModal("notebook")}
        >
          <Plus size={16} />
          New notebook
        </button>
        <nav aria-label="Main navigation">
          {navigation.map((group) => (
            <div className="nav-group" key={group.group}>
              <div className="nav-label">{group.group}</div>
              {group.items.map((item) => (
                <button
                  key={item.id}
                  className={`nav-item ${view === item.id ? "active" : ""}`}
                  onClick={() => navigate(item.id as View)}
                >
                  <item.icon size={17} />
                  <span>{item.label}</span>
                  {item.id === "jobs" &&
                  (data?.metrics?.jobs || data?.jobs.length) ? (
                    <span className="nav-count">
                      {data?.metrics?.jobs ?? data?.jobs.length}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-note">
            <span className="connected-dot" />
            <div>
              Your infrastructure
              <small>Local and remote. In your control.</small>
            </div>
            <ShieldCheck size={17} />
          </div>
          <button
            className={`nav-item ${view === "settings" ? "active" : ""}`}
            onClick={() => navigate("settings")}
          >
            <Settings2 size={17} />
            <span>Team & settings</span>
          </button>
          <div className="user-area">
            <span className="avatar">
              {user.username.slice(0, 2).toUpperCase()}
            </span>
            <div>
              {user.username}
              <small>
                {user.role === "admin" ? "Workspace admin" : "Team member"}
              </small>
            </div>
            <button
              className="icon-button"
              aria-label="Sign out"
              onClick={() => {
                if (
                  editorDirty &&
                  !window.confirm(
                    "Sign out and discard unsaved notebook changes?",
                  )
                )
                  return;
                void action(async () => {
                  await api("/auth/logout", json("POST"));
                  setUser(null);
                  setData(null);
                  setSelected(null);
                  setEditorDirty(false);
                });
              }}
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </aside>
      {mobileOpen ? (
        <button
          className="sidebar-scrim"
          aria-label="Close navigation"
          onClick={() => setMobileOpen(false)}
        />
      ) : null}
      <div className="main-shell">
        <header className="topbar">
          <button
            className="icon-button mobile-menu"
            aria-label="Open navigation"
            onClick={() => setMobileOpen(true)}
          >
            <Menu size={20} />
          </button>
          <div className="breadcrumbs">
            <span className="muted">Workspace</span>
            <Crumb>{viewNames[view]}</Crumb>
            {selected ? <Crumb>{selected.name}</Crumb> : null}
          </div>
          <div className="topbar-right">
            <ProjectSelect
              projects={projects}
              value={projectId}
              onChange={(id) => {
                if (
                  editorDirty &&
                  !window.confirm(
                    "Switch project and discard unsaved notebook edits?",
                  )
                )
                  return;
                setProjectId(id);
                setSelected(null);
                setEditorDirty(false);
                setFolderId(null);
                setSearch("");
                setWorkspaceVersion((value) => value + 1);
              }}
            />
            <ThemeToggle />
            <label className="global-search">
              <Search size={15} />
              <input
                ref={searchRef}
                aria-label="Search current view"
                placeholder="Search workspace…"
                value={search}
                onChange={(event) => {
                  setSearch(event.target.value);
                  if (view === "home") setView("workspace");
                }}
              />
              <kbd>⌘ K</kbd>
            </label>
            <button className="local-pill" onClick={() => navigate("compute")}>
              <Cpu size={12} />
              {clusters.length || 1} compute{" "}
              {clusters.length === 1 ? "target" : "targets"}
            </button>
            <button
              className="icon-button"
              title="Refresh workspace"
              aria-label="Refresh workspace"
              disabled={busy}
              onClick={() => {
                setWorkspaceVersion((value) => value + 1);
                void refresh();
              }}
            >
              <RefreshCw size={16} className={busy ? "spin" : ""} />
            </button>
          </div>
        </header>
        {error ? (
          <div className="global-error" role="alert">
            <span>{error}</span>
            <button
              className="icon-button"
              aria-label="Dismiss error"
              onClick={() => setError("")}
            >
              <X size={16} />
            </button>
          </div>
        ) : null}
        {toast ? (
          <div className="toast" role="status">
            <Check size={17} />
            {toast}
            <button
              aria-label="Dismiss notification"
              className="icon-button"
              onClick={() => setToast("")}
            >
              <X size={14} />
            </button>
          </div>
        ) : null}
        {!data ? (
          <div className="loading-content">
            <Spinner />
            <h2>Loading your workspace</h2>
            <p>Connecting to notebooks, compute, and local storage.</p>
            <button className="button" onClick={() => void refresh()}>
              <RefreshCw size={14} />
              Try again
            </button>
          </div>
        ) : selected ? (
          <NotebookTabs
            notebook={selected}
            projects={projects}
            connections={connections}
            admin={user.role === "admin"}
            onOpen={openNotebook}
            onBack={() => {
              setSelected(null);
              setEditorDirty(false);
            }}
            onChanged={refresh}
            onError={notifyError}
            onDirtyChange={setEditorDirty}
            clusters={clusters}
          />
        ) : (
          <main className="page-content">
            {view === "workflows" ? (
              <WorkflowsPanel
                role={
                  user.role === "admin"
                    ? "owner"
                    : projects.find((project) => project.id === projectId)
                        ?.role || "viewer"
                }
                projectId={projectId}
                projects={projects}
                clusters={clusters.filter((cluster) =>
                  projects
                    .find((project) => project.id === projectId)
                    ?.cluster_ids?.includes(cluster.id),
                )}
                onError={notifyError}
                onRun={openRun}
              />
            ) : null}
            {view === "data" ? (
              <DataWorkspace
                role={
                  user.role === "admin"
                    ? "owner"
                    : projects.find((project) => project.id === projectId)
                        ?.role || "viewer"
                }
                projectId={projectId}
                clusters={clusters.filter((cluster) =>
                  projects
                    .find((project) => project.id === projectId)
                    ?.cluster_ids?.includes(cluster.id),
                )}
                connections={connections.filter(
                  (connection) =>
                    projects
                      .find((project) => project.id === projectId)
                      ?.connection_ids?.includes(connection.id) ||
                    (
                      connection as Connection & { project_ids?: string[] }
                    ).project_ids?.includes(projectId),
                )}
                onError={notifyError}
              />
            ) : null}
            {view === "projects" ? (
              <ProjectsPanel
                projects={projects}
                clusters={clusters}
                connections={connections}
                user={user}
                onChanged={refresh}
                onError={notifyError}
              />
            ) : null}
            {view === "operations" ? (
              <OperationsPanel
                projectId={projectId}
                admin={user.role === "admin"}
                onError={notifyError}
              />
            ) : null}
            {view === "releases" ? (
              <ReleasesPanel
                role={
                  user.role === "admin"
                    ? "owner"
                    : projects.find((project) => project.id === projectId)
                        ?.role || "viewer"
                }
                clusters={clusters.filter((cluster) =>
                  projects
                    .find((project) => project.id === projectId)
                    ?.cluster_ids?.includes(cluster.id),
                )}
                onRun={openRun}
                projectId={projectId}
                repositories={data.repositories}
                connections={connections.filter(
                  (connection) =>
                    projects
                      .find((project) => project.id === projectId)
                      ?.connection_ids?.includes(connection.id) ||
                    (
                      connection as Connection & { project_ids?: string[] }
                    ).project_ids?.includes(projectId),
                )}
                onError={notifyError}
              />
            ) : null}
            {view === "home" ? (
              <>
                <SectionHead
                  eyebrow="YOUR LOCAL DATA PLATFORM"
                  title={`Good to see you, ${user.username}.`}
                  description="A little less infrastructure. A lot more possibility."
                  actions={
                    <>
                      <button
                        className="button"
                        onClick={() => notebookUpload.current?.click()}
                      >
                        <Upload size={15} />
                        Import notebook
                      </button>
                      <button
                        className="button primary"
                        disabled={!canEditProject}
                        onClick={() => setModal("notebook")}
                      >
                        <Plus size={16} />
                        New notebook
                      </button>
                    </>
                  }
                />
                <div className="overview-banner">
                  <div className="banner-symbol">
                    <Layers3 size={42} strokeWidth={1.25} />
                  </div>
                  <div>
                    <div className="banner-eyebrow">
                      YOUR WORKSPACE. YOUR ENTIRE WORKFLOW.
                    </div>
                    <h2>Make yourself at home.</h2>
                    <p>
                      From the first query to the scheduled pipeline, keep your
                      work connected.
                    </p>
                  </div>
                  <span className="banner-badge">
                    <span className="connected-dot" />
                    Self-hosted & ready
                  </span>
                  <div className="banner-lines" aria-hidden="true" />
                </div>
                <div className="stat-grid">
                  <button
                    className="stat-card"
                    onClick={() => navigate("workspace")}
                  >
                    <div>
                      <BookOpen size={18} />
                      <ArrowRight size={15} />
                    </div>
                    <strong>
                      {data.metrics?.notebooks ?? data.notebooks.length}
                      <span>Notebooks</span>
                    </strong>
                    <small>Ideas in your workspace</small>
                  </button>
                  <button
                    className="stat-card"
                    onClick={() => navigate("jobs")}
                  >
                    <div>
                      <CalendarClock size={18} />
                      <ArrowRight size={15} />
                    </div>
                    <strong>
                      {data.metrics?.active_jobs ??
                        data.jobs.filter((item) => item.enabled).length}
                      <span>Active schedules</span>
                    </strong>
                    <small>
                      {(data.metrics?.jobs ?? data.jobs.length)
                        ? `${data.metrics?.jobs ?? data.jobs.length} jobs in total`
                        : "Your workflows, on autopilot"}
                    </small>
                  </button>
                  <button
                    className="stat-card"
                    onClick={() => navigate("runs")}
                  >
                    <div>
                      <Activity size={18} />
                      <ArrowRight size={15} />
                    </div>
                    <strong>
                      {data.metrics?.succeeded ??
                        data.runs.filter((item) => item.status === "succeeded")
                          .length}
                      <span>Successful runs</span>
                    </strong>
                    <small>
                      {data.metrics?.runs ?? data.runs.length} recorded runs
                    </small>
                  </button>
                  <button
                    className="stat-card"
                    onClick={() => navigate("storage")}
                  >
                    <div>
                      <Database size={18} />
                      <ArrowRight size={15} />
                    </div>
                    <strong>
                      {data.buckets.length}
                      <span>Storage buckets</span>
                    </strong>
                    <small>On this machine. In your control.</small>
                  </button>
                </div>
                <div className="overview-columns">
                  <div className="overview-main">
                    <section className="card">
                      <div className="card-heading">
                        <div>
                          <h2>Pick up where you left off</h2>
                          <p>Your most recently updated notebooks.</p>
                        </div>
                        <button
                          className="text-button"
                          onClick={() => navigate("workspace")}
                        >
                          View all
                          <ArrowRight size={14} />
                        </button>
                      </div>
                      {data.notebooks.length ? (
                        <NotebookTable
                          notebooks={[...data.notebooks].sort((a, b) =>
                            (b.updated_at || "").localeCompare(
                              a.updated_at || "",
                            ),
                          )}
                          onOpen={openNotebook}
                          limit={5}
                        />
                      ) : (
                        <Empty
                          title="Your next idea starts here"
                          action={
                            <button
                              className="button primary"
                              disabled={!canEditProject}
                              onClick={() => setModal("notebook")}
                            >
                              <Plus size={15} />
                              Create a notebook
                            </button>
                          }
                        >
                          Write Python, query with Spark SQL, and turn your data
                          into something useful.
                        </Empty>
                      )}
                    </section>
                    <section className="card">
                      <div className="card-heading">
                        <div>
                          <h2>Recent activity</h2>
                          <p>Every run, from exploration to automation.</p>
                        </div>
                        <button
                          className="text-button"
                          onClick={() => navigate("runs")}
                        >
                          All runs
                          <ArrowRight size={14} />
                        </button>
                      </div>
                      {data.runs.length ? (
                        <div className="activity-list">
                          {data.runs.slice(0, 4).map((run) => (
                            <button
                              key={run.id}
                              onClick={() => void openRun(run.id)}
                            >
                              <span className={`activity-icon ${run.status}`}>
                                <Play size={13} />
                              </span>
                              <div>
                                <strong>
                                  {run.notebook_name || "Notebook run"}
                                </strong>
                                <small>
                                  {run.trigger === "schedule"
                                    ? "Scheduled run"
                                    : "Manual run"}{" "}
                                  · {relative(run.created_at)}
                                </small>
                              </div>
                              <Status status={run.status} />
                              <ChevronRight size={14} />
                            </button>
                          ))}
                        </div>
                      ) : (
                        <div className="quiet-empty">
                          <Activity size={22} />
                          <div>
                            <strong>A fresh start</strong>
                            <p>Your notebook runs will appear here.</p>
                          </div>
                        </div>
                      )}
                    </section>
                  </div>
                  <aside className="overview-side">
                    <section className="card compute-card">
                      <div className="card-heading">
                        <h2>
                          <Cpu size={17} />
                          Workspace runtime
                        </h2>
                        <Status status={compute?.status || "idle"} />
                      </div>
                      <div className="compute-machine">
                        <div className="machine-icon">
                          <Server size={25} />
                        </div>
                        <div>
                          <strong>
                            {compute?.hostname || "OpenBricks server"}
                          </strong>
                          <span>
                            Management · Python{" "}
                            {compute?.python_version || "runtime"}
                          </span>
                        </div>
                      </div>
                      <ResourceBar
                        title="Memory"
                        used={compute?.memory_used}
                        total={compute?.memory_total}
                        icon={<Cpu size={13} />}
                      />
                      <ResourceBar
                        title="Disk space"
                        used={compute?.disk_used}
                        total={compute?.disk_total}
                        icon={<HardDrive size={13} />}
                      />
                      <div className="compute-specs">
                        <span>
                          CPU cores<strong>{compute?.cpu_count || "—"}</strong>
                        </span>
                        <span>
                          Active runs
                          <strong>{compute?.active_runs || 0}</strong>
                        </span>
                      </div>
                      <button
                        className="button full"
                        onClick={() => navigate("compute")}
                      >
                        View compute
                        <ArrowRight size={14} />
                      </button>
                    </section>
                    <section className="quick-card">
                      <div className="eyebrow">MAKE IT YOURS</div>
                      <h3>
                        Your workflows.
                        <br />
                        Your infrastructure.
                      </h3>
                      <button onClick={() => navigate("repositories")}>
                        <FolderGit2 size={17} />
                        <span>
                          Bring your code<small>Connect a Git repository</small>
                        </span>
                        <ArrowRight size={14} />
                      </button>
                      <button onClick={() => navigate("packages")}>
                        <PackageIcon size={17} />
                        <span>
                          Build your toolkit<small>Install Python wheels</small>
                        </span>
                        <ArrowRight size={14} />
                      </button>
                      <button onClick={() => navigate("jobs")}>
                        <CalendarClock size={17} />
                        <span>
                          Put it on a schedule
                          <small>Automate your notebooks</small>
                        </span>
                        <ArrowRight size={14} />
                      </button>
                    </section>
                    <div className="open-note">
                      <BrickMark small />
                      <p>
                        Open by nature.
                        <br />
                        <strong>Your data. Your machine. Your rules.</strong>
                      </p>
                    </div>
                  </aside>
                </div>
              </>
            ) : null}
            {view === "workspace" ? (
              <WorkspaceBrowser
                canEdit={canEditProject}
                projectId={projectId}
                refreshKey={workspaceVersion}
                folderId={folderId}
                onFolderChange={setFolderId}
                search={search}
                onSearch={setSearch}
                onOpen={openNotebook}
                onNew={() => setModal("notebook")}
                onImport={() => notebookUpload.current?.click()}
                onChanged={refresh}
                onError={notifyError}
              />
            ) : null}
            {view === "jobs" ? (
              <SchedulesPanel
                role={currentRole}
                projectId={projectId}
                totalJobs={data.metrics?.jobs ?? data.jobs.length}
                activeJobs={
                  data.metrics?.active_jobs ??
                  data.jobs.filter((job) => job.enabled).length
                }
                clusters={clusters}
                onChanged={refresh}
                onOpen={openNotebook}
                onError={notifyError}
              />
            ) : null}
            {view === "runs" ? (
              <RunBrowser
                projectId={projectId}
                onSelect={openRun}
                onOpen={openNotebook}
                onError={notifyError}
              />
            ) : null}
            {view === "repositories" ? (
              <>
                <SectionHead
                  eyebrow="YOUR CODE, CONNECTED"
                  title="Git repositories"
                  description="Bring notebooks and Python projects into your local workspace."
                  actions={
                    <button
                      className="button primary"
                      onClick={() => setModal("repository")}
                    >
                      <Plus size={15} />
                      Clone repository
                    </button>
                  }
                />
                <div className="repository-grid">
                  {repositories.map((repo) => (
                    <section className="card repository-card" key={repo.id}>
                      <div className="repository-top">
                        <span className="repo-icon">
                          <FolderGit2 size={24} />
                        </span>
                        <span className="subtle-tag">Git repository</span>
                      </div>
                      <h2>{repo.name}</h2>
                      <p className="repository-url">{repo.url}</p>
                      <div className="repository-meta">
                        <span>
                          <GitBranch size={14} />
                          {repo.branch}
                        </span>
                        <code>{repo.commit?.slice(0, 7) || "—"}</code>
                      </div>
                      <div className="repository-updated">
                        Last synced {relative(repo.updated_at)}
                      </div>
                      <div className="repository-actions">
                        <button
                          className="button"
                          disabled={busy}
                          onClick={() =>
                            void action(async () => {
                              await api(
                                `/repositories/${repo.id}/pull`,
                                json("POST"),
                              );
                            }, "Repository updated.")
                          }
                        >
                          <RefreshCw size={14} />
                          Pull latest
                        </button>
                        <button
                          className="button primary soft"
                          disabled={busy}
                          onClick={() =>
                            void action(async () => {
                              const imported = await api<Notebook[]>(
                                `/repositories/${repo.id}/import`,
                                json("POST"),
                              );
                              setToast(
                                `${imported.length} notebooks imported.`,
                              );
                            })
                          }
                        >
                          <ArrowDownToLine size={14} />
                          Import notebooks
                        </button>
                      </div>
                    </section>
                  ))}
                </div>
                {!repositories.length ? (
                  <section className="card">
                    <Empty
                      icon={<FolderGit2 size={30} />}
                      title="Great work starts with a commit"
                      action={
                        <button
                          className="button primary"
                          onClick={() => setModal("repository")}
                        >
                          <Plus size={15} />
                          Connect a repository
                        </button>
                      }
                    >
                      Clone a Git repository, pull updates, and import .ipynb or
                      Databricks .py notebooks.
                    </Empty>
                  </section>
                ) : null}
                <div className="info-strip">
                  <GitBranch size={17} />
                  <span>
                    Repositories are cloned onto this server. Private repository
                    access uses the host&apos;s configured Git credentials.
                  </span>
                </div>
              </>
            ) : null}
            {view === "packages" ? (
              <>
                <SectionHead
                  eyebrow="BUILD YOUR OWN TOOLKIT"
                  title="Python packages"
                  description="Reusable code, ready to import in your notebooks."
                  actions={
                    <>
                      <button
                        className="button"
                        onClick={() => setModal("build")}
                        disabled={!data.repositories.length}
                      >
                        <Braces size={15} />
                        Build wheel
                      </button>
                      <button
                        className="button primary"
                        onClick={() => wheelUpload.current?.click()}
                      >
                        <Upload size={15} />
                        Upload wheel
                      </button>
                    </>
                  }
                />
                <div className="package-explainer">
                  <span className="package-graphic">
                    <Box size={40} strokeWidth={1.3} />
                  </span>
                  <div>
                    <h2>Write once. Import everywhere.</h2>
                    <p>
                      Upload a <code>.whl</code> file or build a package from a
                      connected Git project. Installed packages are available to
                      notebook sessions.
                    </p>
                  </div>
                </div>
                <section className="card">
                  {packages.length ? (
                    <div className="table-scroll">
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th>Package</th>
                            <th>Version</th>
                            <th>Installed</th>
                            <th>Size</th>
                            <th>Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {packages.map((pkg, index) => (
                            <tr key={pkg.id || index}>
                              <td>
                                <div className="name-link">
                                  <span className="file-icon violet">
                                    <Box size={17} />
                                  </span>
                                  <span>
                                    {pkg.name}
                                    <small>{pkg.filename}</small>
                                  </span>
                                </div>
                              </td>
                              <td>
                                <code className="subtle-tag">
                                  {pkg.version || "—"}
                                </code>
                              </td>
                              <td className="muted">
                                {date(pkg.installed_at)}
                              </td>
                              <td className="muted">{bytes(pkg.size)}</td>
                              <td>
                                <Status status={pkg.status || "installed"} />
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <Empty
                      icon={<Box size={28} />}
                      title="A home for your reusable code"
                      action={
                        <button
                          className="button"
                          onClick={() => wheelUpload.current?.click()}
                        >
                          <Upload size={14} />
                          Upload a .whl file
                        </button>
                      }
                    >
                      Install your Python libraries into the shared local
                      runtime.
                    </Empty>
                  )}
                </section>
              </>
            ) : null}
            {view === "storage" ? (
              <>
                <div
                  className="view-tabs storage-tabs"
                  role="tablist"
                  aria-label="Storage source"
                >
                  <button
                    role="tab"
                    aria-selected={storageTab === "local"}
                    className={storageTab === "local" ? "active" : ""}
                    onClick={() => setStorageTab("local")}
                  >
                    Local filesystem
                  </button>
                  <button
                    role="tab"
                    aria-selected={storageTab === "connected"}
                    className={storageTab === "connected" ? "active" : ""}
                    onClick={() => setStorageTab("connected")}
                  >
                    Connected object stores
                    <span>
                      {connections.filter((item) => item.type === "s3").length}
                    </span>
                  </button>
                </div>
                {storageTab === "connected" ? (
                  <>
                    <SectionHead
                      eyebrow="SHARED DATA, WHERE YOU CHOOSE"
                      title="Object storage"
                      description="Browse SeaweedFS and remote S3-compatible stores with the same familiar workflow."
                    />
                    <ConnectedStorage
                      connections={connections.filter(
                        (item) => item.type === "s3",
                      )}
                      initialConnectionId={storageConnectionId}
                      onError={notifyError}
                    />
                  </>
                ) : (
                  <>
                    <SectionHead
                      eyebrow="CLOUD-LIKE STORAGE. MINUS THE CLOUD."
                      title="Object storage"
                      description="Organize files in buckets. Keep every byte on this machine."
                      actions={
                        <button
                          className="button primary"
                          onClick={() => setModal("bucket")}
                        >
                          <Plus size={15} />
                          Create bucket
                        </button>
                      }
                    />
                    {bucket ? (
                      <section className="card">
                        <div className="card-heading">
                          <div className="bucket-heading">
                            <button
                              className="text-button"
                              onClick={() => setBucket(null)}
                            >
                              All buckets
                            </button>
                            <ChevronRight size={14} />
                            <h2>
                              <Database size={17} />
                              {bucket.name}
                            </h2>
                          </div>
                          <button
                            className="button primary"
                            onClick={() => objectUpload.current?.click()}
                          >
                            <Upload size={14} />
                            Upload file
                          </button>
                        </div>
                        {objects.length ? (
                          <div className="table-scroll">
                            <table className="data-table">
                              <thead>
                                <tr>
                                  <th>Object key</th>
                                  <th>Size</th>
                                  <th>Modified</th>
                                  <th className="right">Actions</th>
                                </tr>
                              </thead>
                              <tbody>
                                {objects
                                  .filter((object) =>
                                    object.key
                                      .toLowerCase()
                                      .includes(search.toLowerCase()),
                                  )
                                  .map((object) => (
                                    <tr key={object.key}>
                                      <td>
                                        <div className="name-link">
                                          <FileCode2
                                            size={17}
                                            className="muted"
                                          />
                                          <span>{object.key}</span>
                                        </div>
                                      </td>
                                      <td className="muted">
                                        {bytes(object.size)}
                                      </td>
                                      <td className="muted">
                                        {date(
                                          object.modified_at ||
                                            object.updated_at,
                                        )}
                                      </td>
                                      <td className="right">
                                        <div className="row-actions">
                                          <a
                                            className="icon-button"
                                            aria-label={`Download ${object.key}`}
                                            href={`/api/buckets/${encodeURIComponent(bucket.name)}/download?key=${encodeURIComponent(object.key)}`}
                                            download
                                          >
                                            <ArrowDownToLine size={15} />
                                          </a>
                                          <button
                                            className="icon-button"
                                            aria-label={`Delete ${object.key}`}
                                            onClick={() => {
                                              if (
                                                window.confirm(
                                                  `Delete object “${object.key}”?`,
                                                )
                                              )
                                                void action(async () => {
                                                  await api(
                                                    `/buckets/${encodeURIComponent(bucket.name)}/objects?key=${encodeURIComponent(object.key)}`,
                                                    json("DELETE"),
                                                  );
                                                  setObjects(
                                                    await api<StorageObject[]>(
                                                      `/buckets/${encodeURIComponent(bucket.name)}/objects`,
                                                    ),
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
                          </div>
                        ) : (
                          <Empty
                            icon={<Database size={28} />}
                            title="This bucket is ready for data"
                            action={
                              <button
                                className="button"
                                onClick={() => objectUpload.current?.click()}
                              >
                                <Upload size={14} />
                                Upload a file
                              </button>
                            }
                          >
                            Upload datasets, notebook inputs, or pipeline
                            outputs.
                          </Empty>
                        )}
                        <div className="bucket-code">
                          <Terminal size={15} />
                          <code>
                            blob.path(&quot;blob://{bucket.name}
                            /your-file.csv&quot;)
                          </code>
                          <span>Use in Python notebooks</span>
                        </div>
                      </section>
                    ) : (
                      <div className="bucket-grid">
                        {buckets.map((item) => (
                          <button
                            className="card bucket-card"
                            key={item.name}
                            onClick={() => void openBucket(item)}
                          >
                            <div>
                              <span className="bucket-icon">
                                <Database size={25} />
                              </span>
                              <ArrowRight size={17} />
                            </div>
                            <h2>{item.name}</h2>
                            <p>
                              {item.object_count || item.objects || 0} objects
                              <span>·</span>
                              {bytes(item.size || item.bytes || 0)}
                            </p>
                            <div className="bucket-location">
                              <HardDrive size={13} />
                              Stored locally
                            </div>
                          </button>
                        ))}
                      </div>
                    )}
                    {!bucket && !buckets.length ? (
                      <section className="card">
                        <Empty
                          icon={<Database size={30} />}
                          title="A place for every byte"
                          action={
                            <button
                              className="button primary"
                              onClick={() => setModal("bucket")}
                            >
                              <Plus size={15} />
                              Create your first bucket
                            </button>
                          }
                        >
                          Local object storage with familiar buckets and keys,
                          accessible from your notebooks.
                        </Empty>
                      </section>
                    ) : null}
                    <div className="info-strip">
                      <ShieldCheck size={17} />
                      <span>
                        Storage lives in the persistent Docker data volume.
                        Notebook code can read and write files with the built-in{" "}
                        <code>blob</code> helper.
                      </span>
                    </div>
                  </>
                )}
              </>
            ) : null}
            {view === "compute" ? (
              <ClustersPanel
                clusters={clusters}
                admin={user.role === "admin"}
                onChanged={refresh}
                onError={notifyError}
              />
            ) : null}
            {view === "connections" ? (
              <ConnectionsPanel
                connections={connections}
                admin={user.role === "admin"}
                onChanged={refresh}
                onError={notifyError}
                onStorage={(connectionId) => {
                  setStorageConnectionId(connectionId);
                  navigate("storage");
                  setStorageTab("connected");
                }}
              />
            ) : null}
            {view === "catalog" ? (
              <CatalogPanel
                projectId={projectId}
                admin={user.role === "admin"}
                onError={notifyError}
              />
            ) : null}
            {view === "settings" ? (
              <>
                <SectionHead
                  eyebrow="BETTER TOGETHER"
                  title="Team & settings"
                  description="A shared workspace for a small, trusted team."
                  actions={
                    user.role === "admin" ? (
                      <button
                        className="button primary"
                        onClick={() => setModal("user")}
                      >
                        <Plus size={15} />
                        Add team member
                      </button>
                    ) : null
                  }
                />
                <section className="card">
                  <div className="card-heading">
                    <div>
                      <h2>Workspace members</h2>
                      <p>
                        Members can write and run code on this shared server.
                      </p>
                    </div>
                    <Users size={20} className="muted" />
                  </div>
                  {user.role === "admin" ? (
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>Member</th>
                          <th>Role</th>
                          <th>Access</th>
                        </tr>
                      </thead>
                      <tbody>
                        {users.map((member) => (
                          <tr key={member.id}>
                            <td>
                              <div className="name-link">
                                <span className="avatar light">
                                  {member.username.slice(0, 2).toUpperCase()}
                                </span>
                                <span>
                                  {member.username}
                                  {member.id === user.id ? (
                                    <small>You</small>
                                  ) : null}
                                </span>
                              </div>
                            </td>
                            <td>
                              <span className="subtle-tag">{member.role}</span>
                            </td>
                            <td>
                              <Status status="active" />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <div className="settings-member">
                      <span className="avatar light">
                        {user.username.slice(0, 2).toUpperCase()}
                      </span>
                      <div>
                        <strong>{user.username}</strong>
                        <p>
                          Team member · Contact your admin to manage accounts.
                        </p>
                      </div>
                    </div>
                  )}
                </section>
                <div className="settings-grid">
                  <section className="card settings-card">
                    <ShieldCheck size={22} />
                    <h3>Trusted team workspace</h3>
                    <p>
                      Notebook execution, wheel installation, and Git builds run
                      code with the runtime&apos;s permissions. Add people you
                      trust to share this server.
                    </p>
                  </section>
                  <section className="card settings-card">
                    <HardDrive size={22} />
                    <h3>Portable by design</h3>
                    <p>
                      The management workspace and compute run independently.
                      Keep datasets on local storage or use a connected object
                      store to share files across clusters.
                    </p>
                  </section>
                </div>
              </>
            ) : null}
            <footer className="page-footer">
              <span>
                <BrickMark small />
                OPENBRICKS
              </span>
              <span>An open home for your data work.</span>
              <span>Open compute platform · v0.3 beta</span>
            </footer>
          </main>
        )}
      </div>
      <input
        type="file"
        className="hidden"
        ref={notebookUpload}
        accept=".ipynb,.py,.sql,.json"
        aria-label="Import notebook file"
        onChange={(event) => {
          void upload(
            event.target.files?.[0],
            "/notebooks/import",
            "Notebook imported.",
          );
          event.target.value = "";
        }}
      />
      <input
        type="file"
        className="hidden"
        ref={wheelUpload}
        accept=".whl"
        aria-label="Upload Python wheel"
        onChange={(event) => {
          void upload(
            event.target.files?.[0],
            "/packages/upload",
            "Wheel installed in the runtime.",
          );
          event.target.value = "";
        }}
      />
      <input
        type="file"
        className="hidden"
        ref={objectUpload}
        aria-label="Upload storage object"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (bucket && file) {
            const key = window.prompt(
              "Object key (folder paths are supported)",
              file.name,
            );
            if (key)
              void upload(
                file,
                `/buckets/${encodeURIComponent(bucket.name)}/objects`,
                "Object uploaded.",
                { key },
              );
          }
          event.target.value = "";
        }}
      />
      {modal ? (
        <Modal
          title={
            {
              notebook: "Create a notebook",
              job: "Create a scheduled job",
              repository: "Clone a Git repository",
              bucket: "Create a storage bucket",
              build: "Build a Python wheel",
              user: "Add a team member",
            }[modal] || "Create"
          }
          onClose={() => {
            if (!busy) setModal(null);
          }}
        >
          <form className="modal-form" onSubmit={submitModal}>
            {modal === "notebook" ? (
              <>
                <p className="modal-description">
                  A new home for your next idea. Mix Python, SQL, and Markdown
                  cells in one notebook.
                </p>
                <Field label="Notebook name">
                  <input
                    name="name"
                    required
                    maxLength={180}
                    autoFocus
                    placeholder="My first notebook"
                  />
                </Field>
                <Field label="Default language">
                  <select name="language">
                    <option value="python">Python</option>
                    <option value="sql">Spark SQL</option>
                  </select>
                </Field>
                <Field label="Compute cluster">
                  <select name="cluster_id">
                    <option value="">Default local compute</option>
                    {clusters.map((cluster) => (
                      <option key={cluster.id} value={cluster.id}>
                        {cluster.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <p className="form-note">
                  Create in{" "}
                  {folderId ? "the selected workspace folder" : "/Workspace"}.
                </p>
              </>
            ) : null}

            {modal === "repository" ? (
              <>
                <p className="modal-description">
                  Clone a repository to this server. Git uses the server&apos;s
                  configured credentials for private repositories.
                </p>
                <Field label="Display name">
                  <input
                    name="name"
                    required
                    autoFocus
                    placeholder="analytics-project"
                  />
                </Field>
                <Field label="Repository URL">
                  <input
                    name="url"
                    required
                    placeholder="https://github.com/your-team/analytics.git"
                  />
                </Field>
                <Field label="Branch">
                  <input name="branch" required defaultValue="main" />
                </Field>
              </>
            ) : null}
            {modal === "bucket" ? (
              <>
                <p className="modal-description">
                  Buckets organize local files using familiar object keys.
                </p>
                <Field
                  label="Bucket name"
                  hint="Use lowercase letters, numbers, and hyphens."
                >
                  <input
                    name="name"
                    pattern="[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
                    minLength={3}
                    maxLength={63}
                    required
                    autoFocus
                    placeholder="my-datasets"
                  />
                </Field>
              </>
            ) : null}
            {modal === "build" ? (
              <>
                <p className="modal-description">
                  Build and install a wheel from a Python project with a
                  pyproject.toml or setup.py in a cloned repository.
                </p>
                <Field label="Repository">
                  <select name="repository_id" required>
                    {data?.repositories.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field
                  label="Project directory"
                  hint="A path relative to the repository root. Use . for the root."
                >
                  <input name="path" required defaultValue="." />
                </Field>
                <div className="form-note">
                  The build executes trusted project code on this server.
                </div>
              </>
            ) : null}
            {modal === "user" ? (
              <>
                <p className="modal-description">
                  Create an account for someone you trust to share this runtime.
                </p>
                <Field label="Username">
                  <input
                    name="username"
                    required
                    autoFocus
                    autoComplete="off"
                    minLength={3}
                    placeholder="teammate"
                  />
                </Field>
                <Field label="Initial password" hint="At least 12 characters.">
                  <input
                    name="password"
                    type="password"
                    required
                    autoComplete="new-password"
                    minLength={12}
                  />
                </Field>
                <Field label="Role">
                  <select name="role">
                    <option value="member">
                      Member — notebooks, jobs, data, and packages
                    </option>
                    <option value="admin">
                      Admin — also manage team accounts
                    </option>
                  </select>
                </Field>
              </>
            ) : null}
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
                onClick={() => setModal(null)}
              >
                Cancel
              </button>
              <button type="submit" className="button primary" disabled={busy}>
                {busy ? (
                  <Spinner />
                ) : modal === "repository" ? (
                  <GitBranch size={15} />
                ) : (
                  <Plus size={15} />
                )}
                {busy
                  ? modal === "build"
                    ? "Building & installing…"
                    : "Working…"
                  : {
                      notebook: "Create notebook",
                      job: "Create job",
                      repository: "Clone repository",
                      bucket: "Create bucket",
                      build: "Build & install",
                      user: "Add member",
                    }[modal]}
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {selectedRun ? (
        <Modal title="Run details" onClose={() => setSelectedRun(null)} wide>
          <div className="run-detail">
            <div className="run-detail-heading">
              <div>
                <h3>{selectedRun.notebook_name || "Notebook execution"}</h3>
                <code>{selectedRun.id}</code>
              </div>
              <Status status={selectedRun.status} />
            </div>
            <RunActivity run={selectedRun} />
            <div className="run-detail-meta">
              <span>
                Started
                <strong>
                  {date(selectedRun.started_at || selectedRun.created_at)}
                </strong>
              </span>
              <span>
                Finished
                <strong>
                  {date(selectedRun.finished_at || selectedRun.ended_at)}
                </strong>
              </span>
              <span>
                Trigger<strong>{selectedRun.trigger || "manual"}</strong>
              </span>
            </div>
            {selectedRun.error ? (
              <pre className="run-error">{selectedRun.error}</pre>
            ) : null}
            {selectedRun.outputs?.length ? (
              selectedRun.outputs.map((output, index) => (
                <div className="run-output" key={`${output.cell_id}-${index}`}>
                  <div>
                    <strong>Cell {index + 1}</strong>
                    <Status status={output.status} />
                  </div>
                  {output.stdout ? <pre>{output.stdout}</pre> : null}
                  {output.stderr ? (
                    <pre className="stderr">{output.stderr}</pre>
                  ) : null}
                  {output.result ? (
                    <pre>{JSON.stringify(output.result, null, 2)}</pre>
                  ) : null}
                </div>
              ))
            ) : (
              <div className="quiet-empty">
                <Terminal size={22} />
                <p>
                  {["running", "queued"].includes(selectedRun.status)
                    ? "Waiting for notebook output…"
                    : "No cell output recorded."}
                </p>
              </div>
            )}
            <div className="modal-footer">
              {["queued", "running"].includes(selectedRun.status) ? (
                <button
                  className="button danger"
                  disabled={
                    busy ||
                    !canOperateSelectedRun ||
                    !!selectedRun.cancel_requested
                  }
                  onClick={() =>
                    void action(async () => {
                      setSelectedRun(
                        await api<Run>(
                          `/runs/${selectedRun.id}/cancel`,
                          json("POST"),
                        ),
                      );
                    })
                  }
                >
                  Cancel run
                </button>
              ) : null}
              <button
                className="button"
                disabled={
                  busy ||
                  !canOperateSelectedRun ||
                  ["queued", "running"].includes(selectedRun.status)
                }
                onClick={() =>
                  void action(async () => {
                    setSelectedRun(
                      await api<Run>(
                        `/runs/${selectedRun.id}/rerun`,
                        json("POST"),
                      ),
                    );
                  })
                }
              >
                Rerun captured source
              </button>
              <button
                className="button"
                disabled={
                  !!selectedRun.provenance?.release_id ||
                  selectedRun.notebook_id.startsWith("data-")
                }
                title={
                  selectedRun.provenance?.release_id
                    ? "This run uses an immutable release source"
                    : "Open workspace notebook"
                }
                onClick={() => {
                  void openNotebook(selectedRun.notebook_id);
                  setSelectedRun(null);
                }}
              >
                <BookOpen size={15} />
                Open notebook
              </button>
              <button
                className="button primary"
                onClick={() => setSelectedRun(null)}
              >
                Done
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
