"use client";

import { useEffect, useState, type CSSProperties, type FormEvent } from "react";
import {
  ArrowDown,
  ArrowUp,
  BarChart3,
  Clock3,
  LayoutDashboard,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";
import {
  api,
  ApiError,
  date,
  json,
  type Page,
  type Project,
  type QueryResult,
} from "@/lib/api";
import type {
  Dashboard,
  DashboardRefresh,
  DashboardSnapshot,
  DashboardSummary,
  DashboardTile,
  QueryOption,
} from "@/lib/v5-data";
import CronScheduleFields from "./cron-schedule-fields";
import {
  parameterDefaults,
  ParameterDefinitions,
  ParameterValues,
} from "./parameter-fields";
import { ProjectSelect } from "./platform-controls";
import {
  Empty,
  Field,
  Modal,
  Pagination,
  SectionHead,
  Spinner,
  Status,
} from "./ui";
import "./v5-data.css";

const terminal = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "unknown",
]);
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed.";
type DashboardDraft = Omit<Dashboard, "id" | "generation">;

export function CachedRows({ result }: { result: QueryResult }) {
  const [offset, setOffset] = useState(0),
    [columnOffset, setColumnOffset] = useState(0);
  const columns = result.columns.slice(columnOffset, columnOffset + 10);
  const rows = result.rows.slice(offset, offset + 20);
  return (
    <div className="v5-cached-rows">
      <div className="table-scroll">
        <table className="result-table">
          <thead>
            <tr>
              {columns.map((column, index) => (
                <th key={index}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                {row
                  .slice(columnOffset, columnOffset + 10)
                  .map((value, cell) => (
                    <td key={cell}>
                      {value == null ? (
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
      </div>
      {!result.rows.length ? (
        <p className="muted v5-note">The query returned no rows.</p>
      ) : null}
      {result.columns.length > 10 ? (
        <div className="v5-column-pages">
          <span>
            Columns {columnOffset + 1}–
            {Math.min(columnOffset + 10, result.columns.length)} of{" "}
            {result.columns.length}
          </span>
          <button
            className="text-button"
            disabled={!columnOffset}
            onClick={() => setColumnOffset(Math.max(0, columnOffset - 10))}
          >
            Previous columns
          </button>
          <button
            className="text-button"
            disabled={columnOffset + 10 >= result.columns.length}
            onClick={() => setColumnOffset(columnOffset + 10)}
          >
            Next columns
          </button>
        </div>
      ) : null}
      {result.rows.length > 20 ? (
        <Pagination
          total={result.rows.length}
          offset={offset}
          limit={20}
          onPage={setOffset}
        />
      ) : null}
      {result.truncated ? (
        <p className="muted v5-note">
          Bounded cached preview. Aggregate in SQL for complete totals.
        </p>
      ) : null}
    </div>
  );
}

function numeric(value: unknown): number | null {
  if ((typeof value !== "string" && typeof value !== "number") || value === "")
    return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function TileResult({ tile }: { tile: DashboardSnapshot["tiles"][number] }) {
  const result = tile.result;
  if (tile.chart === "table") return <CachedRows result={result} />;
  const x = tile.x ? result.columns.indexOf(tile.x) : 0;
  const y = tile.y
    ? result.columns.indexOf(tile.y)
    : result.columns.findIndex((_, index) =>
        result.rows.some((row) => numeric(row[index]) !== null),
      );
  if (y < 0 || (tile.chart !== "number" && x < 0))
    return (
      <p className="v5-note">
        The configured chart columns are unavailable in this cached result. Edit
        the tile or refresh its query.
      </p>
    );
  if (tile.chart === "number")
    return (
      <>
        <div className="v5-number-result">
          {result.rows[0]?.[y] == null ? "—" : String(result.rows[0][y])}
        </div>
        <p className="muted v5-note">{result.columns[y]} · first cached row</p>
        <details>
          <summary>View cached rows</summary>
          <CachedRows result={result} />
        </details>
      </>
    );
  const rows = result.rows
    .filter((row) => numeric(row[y]) !== null)
    .slice(0, 40);
  const values = rows.map((row) => numeric(row[y]) as number);
  const min = Math.min(0, ...values),
    max = Math.max(1, ...values),
    range = max - min;
  const points = rows.map((row, index) => ({
    x: 48 + ((index + 0.5) * 600) / Math.max(1, rows.length),
    y: 180 - ((values[index] - min) / range) * 145,
    value: values[index],
    label: String(row[x]),
  }));
  const zero = 180 - ((0 - min) / range) * 145;
  return (
    <>
      {points.length ? (
        <svg
          className="v5-tile-chart"
          viewBox="0 0 700 230"
          role="img"
          aria-label={`${tile.title || "Saved query"}: ${result.columns[y]} by ${result.columns[x]}`}
        >
          <line x1="40" x2="665" y1={zero} y2={zero} stroke="var(--line)" />
          {tile.chart === "line" ? (
            <polyline
              points={points.map((point) => `${point.x},${point.y}`).join(" ")}
              fill="none"
              stroke="var(--coral)"
              strokeWidth="3"
            />
          ) : null}
          {points.map((point, index) => (
            <g key={index}>
              {tile.chart === "bar" ? (
                <rect
                  x={point.x - 200 / points.length}
                  y={Math.min(zero, point.y)}
                  width={400 / points.length}
                  height={Math.abs(zero - point.y)}
                  fill="var(--coral)"
                />
              ) : (
                <circle cx={point.x} cy={point.y} r="4" fill="var(--coral)" />
              )}
              <title>
                {point.label}: {point.value}
              </title>
              {index % Math.max(1, Math.ceil(points.length / 8)) === 0 ? (
                <text
                  x={point.x}
                  y="211"
                  textAnchor="middle"
                  fill="var(--muted)"
                  fontSize="11"
                >
                  {point.label.slice(0, 12)}
                </text>
              ) : null}
            </g>
          ))}
          <text x="7" y="30" fill="var(--muted)" fontSize="11">
            {max.toLocaleString()}
          </text>
          <text x="7" y="185" fill="var(--muted)" fontSize="11">
            {min.toLocaleString()}
          </text>
        </svg>
      ) : (
        <p className="v5-note">No numeric rows to chart.</p>
      )}
      {result.rows.length > 40 ? (
        <p className="muted v5-note">
          Chart shows the first 40 numeric cached rows.
        </p>
      ) : null}
      <details>
        <summary>View cached rows</summary>
        <CachedRows result={result} />
      </details>
    </>
  );
}

function QueryPicker({
  projectId,
  value,
  onChange,
}: {
  projectId: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0),
    [page, setPage] = useState<Page<QueryOption> | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let stopped = false;
    const timer = setTimeout(
      () => {
        api<Page<QueryOption>>(
          `/dashboards/query-options?${new URLSearchParams({ project_id: projectId, search, limit: "20", offset: String(offset) })}`,
          { signal: controller.signal },
        )
          .then((value) => {
            if (!stopped) {
              setPage(value);
              setError("");
            }
          })
          .catch((error) => {
            if (!stopped) setError(message(error));
          });
      },
      search ? 200 : 0,
    );
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [projectId, search, offset]);
  return (
    <div className="v5-query-picker">
      <Field label="Find saved query">
        <input
          value={search}
          placeholder="Search this project's saved queries"
          onChange={(event) => {
            setSearch(event.target.value);
            setOffset(0);
          }}
        />
      </Field>
      <Field label="Saved query">
        <select
          required
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">Choose a saved SELECT query</option>
          {value && !page?.items.some((item) => item.id === value) ? (
            <option value={value}>Selected query · {value.slice(0, 12)}</option>
          ) : null}
          {page?.items.map((item) => (
            <option value={item.id} key={item.id}>
              {item.name} · {item.engine}
            </option>
          ))}
        </select>
      </Field>
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      {page && page.total > 20 ? (
        <Pagination
          total={page.total}
          offset={offset}
          limit={20}
          onPage={setOffset}
        />
      ) : null}
    </div>
  );
}

function Editor({
  initial,
  projectId,
  onClose,
  onSaved,
}: {
  initial: Dashboard | null;
  projectId: string;
  onClose: () => void;
  onSaved: (value: Dashboard) => void;
}) {
  const [draft, setDraft] = useState<DashboardDraft>(
    () =>
      initial || {
        name: "",
        description: "",
        project_id: projectId,
        tiles: [
          {
            id: "tile-1",
            title: "",
            query_id: "",
            chart: "table",
            width: 6,
            height: 4,
            parameter_bindings: {},
          },
        ],
        parameters: [],
        cron: null,
        timezone: "UTC",
        enabled: true,
        cache_ttl_seconds: 3600,
      },
  );
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  function updateTile(index: number, value: Partial<DashboardTile>) {
    setDraft((current) => ({
      ...current,
      tiles: current.tiles.map((tile, position) =>
        position === index ? { ...tile, ...value } : tile,
      ),
    }));
  }
  function reorder(index: number, step: number) {
    setDraft((current) => {
      const tiles = [...current.tiles];
      [tiles[index], tiles[index + step]] = [tiles[index + step], tiles[index]];
      return { ...current, tiles };
    });
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const value = await api<Dashboard>(
        initial ? `/dashboards/${initial.id}` : "/dashboards",
        json(initial ? "PUT" : "POST", draft),
      );
      onSaved(value);
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={initial ? "Edit dashboard" : "Create dashboard"}
      wide
      onClose={onClose}
    >
      <form onSubmit={submit} className="v5-dashboard-form">
        <div className="form-grid">
          <Field label="Name">
            <input
              required
              maxLength={200}
              value={draft.name}
              onChange={(event) =>
                setDraft({ ...draft, name: event.target.value })
              }
            />
          </Field>
          <Field
            label="Cache freshness (minutes)"
            hint="Older results remain available with a stale badge."
          >
            <input
              type="number"
              min={1}
              max={43200}
              required
              value={draft.cache_ttl_seconds / 60}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  cache_ttl_seconds: Number(event.target.value) * 60,
                })
              }
            />
          </Field>
        </div>
        <Field label="Description">
          <textarea
            maxLength={4000}
            rows={2}
            value={draft.description}
            onChange={(event) =>
              setDraft({ ...draft, description: event.target.value })
            }
          />
        </Field>
        <h3>Filters</h3>
        <p className="muted v5-note">
          Declare typed filters, then bind them to positional query parameters
          below.
        </p>
        <ParameterDefinitions
          value={draft.parameters}
          onChange={(parameters) => setDraft({ ...draft, parameters })}
        />
        <div className="v5-subhead">
          <h3>Tiles</h3>
          <button
            type="button"
            className="button"
            disabled={draft.tiles.length >= 12}
            onClick={() =>
              setDraft({
                ...draft,
                tiles: [
                  ...draft.tiles,
                  {
                    id: `tile-${crypto.randomUUID().slice(0, 12)}`,
                    title: "",
                    query_id: "",
                    chart: "table",
                    width: 6,
                    height: 4,
                    parameter_bindings: {},
                  },
                ],
              })
            }
          >
            <Plus size={14} />
            Add tile
          </button>
        </div>
        {draft.tiles.map((tile, index) => (
          <fieldset className="v5-tile-editor" key={tile.id}>
            <legend>
              Tile {index + 1}
              {tile.title ? ` · ${tile.title}` : ""}
            </legend>
            <div className="v5-tile-editor-actions">
              <button
                type="button"
                className="icon-button"
                disabled={!index}
                aria-label={`Move tile ${index + 1} up`}
                onClick={() => reorder(index, -1)}
              >
                <ArrowUp size={14} />
              </button>
              <button
                type="button"
                className="icon-button"
                disabled={index === draft.tiles.length - 1}
                aria-label={`Move tile ${index + 1} down`}
                onClick={() => reorder(index, 1)}
              >
                <ArrowDown size={14} />
              </button>
              <button
                type="button"
                className="icon-button"
                disabled={draft.tiles.length === 1}
                aria-label={`Remove tile ${index + 1}`}
                onClick={() =>
                  setDraft({
                    ...draft,
                    tiles: draft.tiles.filter((item) => item.id !== tile.id),
                  })
                }
              >
                <Trash2 size={14} />
              </button>
            </div>
            <div className="form-grid">
              <Field label="Title">
                <input
                  maxLength={200}
                  value={tile.title}
                  onChange={(event) =>
                    updateTile(index, { title: event.target.value })
                  }
                />
              </Field>
              <Field label="Visualization">
                <select
                  value={tile.chart}
                  onChange={(event) =>
                    updateTile(index, {
                      chart: event.target.value as DashboardTile["chart"],
                    })
                  }
                >
                  {["table", "bar", "line", "number"].map((kind) => (
                    <option key={kind} value={kind}>
                      {kind}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <QueryPicker
              projectId={projectId}
              value={tile.query_id}
              onChange={(query_id) => updateTile(index, { query_id })}
            />
            <div className="form-grid">
              <Field
                label="Label column"
                hint="Leave blank to use the first column."
              >
                <input
                  value={tile.x || ""}
                  maxLength={200}
                  onChange={(event) =>
                    updateTile(index, { x: event.target.value || null })
                  }
                />
              </Field>
              <Field
                label="Value column"
                hint="Leave blank to use the first numeric column."
              >
                <input
                  value={tile.y || ""}
                  maxLength={200}
                  onChange={(event) =>
                    updateTile(index, { y: event.target.value || null })
                  }
                />
              </Field>
              <Field label="Width">
                <select
                  value={tile.width}
                  onChange={(event) =>
                    updateTile(index, { width: Number(event.target.value) })
                  }
                >
                  {[
                    3,
                    4,
                    6,
                    8,
                    12,
                    ...([3, 4, 6, 8, 12].includes(tile.width)
                      ? []
                      : [tile.width]),
                  ].map((width) => (
                    <option value={width} key={width}>
                      {width} / 12 columns
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Height">
                <select
                  value={tile.height}
                  onChange={(event) =>
                    updateTile(index, { height: Number(event.target.value) })
                  }
                >
                  {Array.from({ length: 12 }, (_, value) => (
                    <option value={value + 1} key={value}>
                      {value + 1} units
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            {Object.entries(tile.parameter_bindings).map(([position, name]) => (
              <div className="v5-binding" key={position}>
                <Field label="SQL parameter position (zero-based)">
                  <input
                    type="number"
                    min={0}
                    max={999}
                    defaultValue={position}
                    onBlur={(event) => {
                      const next = event.target.value;
                      if (next === position) return;
                      if (
                        !/^(0|[1-9][0-9]{0,2})$/.test(next) ||
                        next in tile.parameter_bindings
                      ) {
                        setError(
                          "Use a unique SQL parameter position between 0 and 999.",
                        );
                        event.target.value = position;
                        return;
                      }
                      const bindings = { ...tile.parameter_bindings };
                      delete bindings[position];
                      bindings[next] = name;
                      updateTile(index, { parameter_bindings: bindings });
                    }}
                  />
                </Field>
                <Field label="Dashboard filter">
                  <select
                    value={name}
                    required
                    onChange={(event) =>
                      updateTile(index, {
                        parameter_bindings: {
                          ...tile.parameter_bindings,
                          [position]: event.target.value,
                        },
                      })
                    }
                  >
                    {draft.parameters.map((parameter) => (
                      <option value={parameter.name} key={parameter.name}>
                        {parameter.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`Remove parameter binding ${position}`}
                  onClick={() => {
                    const bindings = { ...tile.parameter_bindings };
                    delete bindings[position];
                    updateTile(index, { parameter_bindings: bindings });
                  }}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
            <button
              type="button"
              className="text-button"
              disabled={
                !draft.parameters.length ||
                Object.keys(tile.parameter_bindings).length >= 100
              }
              onClick={() => {
                let position = 0;
                while (String(position) in tile.parameter_bindings) position++;
                updateTile(index, {
                  parameter_bindings: {
                    ...tile.parameter_bindings,
                    [position]: draft.parameters[0].name,
                  },
                });
              }}
            >
              <Plus size={13} />
              Bind a filter to this query
            </button>
          </fieldset>
        ))}
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={!!draft.cron && draft.enabled}
            onChange={(event) =>
              setDraft({
                ...draft,
                enabled: true,
                cron: event.target.checked ? "0 8 * * *" : null,
              })
            }
          />
          Refresh on a schedule
        </label>
        {draft.cron ? (
          <CronScheduleFields
            cron={draft.cron}
            timezone={draft.timezone}
            onCronChange={(cron) =>
              setDraft((current) => ({ ...current, cron }))
            }
            onTimezoneChange={(timezone) =>
              setDraft((current) => ({ ...current, timezone }))
            }
          />
        ) : null}
        {error ? (
          <p className="inline-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="modal-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button className="button primary" disabled={busy}>
            {busy ? <Spinner /> : <Save size={14} />}Save dashboard
          </button>
        </div>
      </form>
    </Modal>
  );
}

export default function DashboardsPanel({
  projects,
  onError,
  onRun,
}: {
  projects: Project[];
  onError: (error: string) => void;
  onRun?: (id: string) => void;
}) {
  const [projectId, setProjectId] = useState(projects[0]?.id || "default"),
    [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0),
    [page, setPage] = useState<Page<DashboardSummary> | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null),
    [board, setBoard] = useState<Dashboard | null>(null),
    [filters, setFilters] = useState<Record<string, unknown>>({}),
    [applied, setApplied] = useState<Record<string, unknown>>({});
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null),
    [snapshotError, setSnapshotError] = useState(""),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [cacheLoading, setCacheLoading] = useState(false),
    [revision, setRevision] = useState(0),
    [cacheRevision, setCacheRevision] = useState(0);
  const [editor, setEditor] = useState<Dashboard | "new" | null>(null),
    [deleting, setDeleting] = useState(false),
    [busy, setBusy] = useState(false),
    [active, setActive] = useState<DashboardRefresh | null>(null),
    [historyOpen, setHistoryOpen] = useState(false),
    [historyOffset, setHistoryOffset] = useState(0),
    [history, setHistory] = useState<Page<DashboardRefresh> | null>(null),
    [detail, setDetail] = useState<DashboardRefresh | null>(null);
  const role = projects.find((project) => project.id === projectId)?.role;
  const canEdit = role === "editor" || role === "owner",
    canRun = canEdit || role === "operator";
  const appliedKey = JSON.stringify(applied);

  useEffect(() => {
    if (
      projects.length &&
      !projects.some((project) => project.id === projectId)
    )
      setProjectId(projects[0].id);
  }, [projects, projectId]);
  useEffect(() => {
    let stopped = false;
    const controller = new AbortController();
    setLoading(true);
    const timer = setTimeout(
      () =>
        api<Page<DashboardSummary>>(
          `/dashboards?${new URLSearchParams({ project_id: projectId, search, offset: String(offset), limit: "20" })}`,
          { signal: controller.signal },
        )
          .then((value) => {
            if (!stopped) {
              setPage(value);
              setError("");
              setSelectedId((current) => current || value.items[0]?.id || null);
            }
          })
          .catch((error) => {
            if (!stopped) setError(message(error));
          })
          .finally(() => {
            if (!stopped) setLoading(false);
          }),
      search ? 200 : 0,
    );
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [projectId, search, offset, revision]);
  useEffect(() => {
    if (!selectedId) return;
    let stopped = false;
    const controller = new AbortController();
    setBoard(null);
    setSnapshot(null);
    setActive(null);
    setDetail(null);
    setHistory(null);
    api<Dashboard>(`/dashboards/${selectedId}`, { signal: controller.signal })
      .then((value) => {
        if (!stopped) {
          setBoard(value);
          const defaults = parameterDefaults(value.parameters);
          setFilters(defaults);
          setApplied(defaults);
          setActive(
            value.last_refresh && !terminal.has(value.last_refresh.status)
              ? value.last_refresh
              : null,
          );
          setError("");
        }
      })
      .catch((error) => {
        if (!stopped) setError(message(error));
      });
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [selectedId, revision]);
  useEffect(() => {
    if (!board) return;
    let stopped = false;
    const controller = new AbortController();
    setCacheLoading(true);
    setSnapshot(null);
    setSnapshotError("");
    api<DashboardSnapshot>(
      `/dashboards/${board.id}/snapshot?${new URLSearchParams({ parameters: appliedKey })}`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!stopped) setSnapshot(value);
      })
      .catch((error) => {
        if (!stopped)
          setSnapshotError(
            error instanceof ApiError && error.status === 404
              ? "There is no saved result for these filters yet. An operator can refresh the dashboard."
              : message(error),
          );
      })
      .finally(() => {
        if (!stopped) setCacheLoading(false);
      });
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [board?.id, board?.generation, appliedKey, cacheRevision]);
  useEffect(() => {
    if (!active || terminal.has(active.status)) return;
    let stopped = false,
      polling = false;
    const controller = new AbortController();
    const timer = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        const value = await api<DashboardRefresh>(
          `/dashboards/${active.dashboard_id}/refreshes/${active.id}`,
          { signal: controller.signal },
        );
        if (!stopped) {
          setActive(value);
          if (terminal.has(value.status)) {
            setCacheRevision((value) => value + 1);
            setBoard((current) =>
              current ? { ...current, last_refresh: value } : null,
            );
          }
        }
      } catch (error) {
        if (!stopped) setError(message(error));
      } finally {
        polling = false;
      }
    }, 1200);
    return () => {
      stopped = true;
      controller.abort();
      clearInterval(timer);
    };
  }, [active?.id, active?.status]);
  useEffect(() => {
    if (!board || !historyOpen) return;
    let stopped = false;
    const controller = new AbortController();
    api<Page<DashboardRefresh>>(
      `/dashboards/${board.id}/refreshes?offset=${historyOffset}&limit=10`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!stopped) setHistory(value);
      })
      .catch((error) => {
        if (!stopped) setError(message(error));
      });
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [board?.id, historyOpen, historyOffset, cacheRevision]);

  async function refresh() {
    if (!board) return;
    setBusy(true);
    try {
      const value = await api<DashboardRefresh>(
        `/dashboards/${board.id}/refresh`,
        json("POST", { parameters: filters }),
      );
      setApplied({ ...filters });
      setActive(value);
      setError("");
    } catch (error) {
      setError(message(error));
      onError(message(error));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!board) return;
    setBusy(true);
    try {
      await api(`/dashboards/${board.id}`, json("DELETE"));
      setDeleting(false);
      setBoard(null);
      setSelectedId(null);
      setRevision((value) => value + 1);
    } catch (error) {
      setError(message(error));
      onError(message(error));
    } finally {
      setBusy(false);
    }
  }
  function selectProject(value: string) {
    setProjectId(value);
    setSelectedId(null);
    setBoard(null);
    setPage(null);
    setOffset(0);
    setSearch("");
    setHistoryOpen(false);
  }

  return (
    <div className="v5-data-page">
      <SectionHead
        eyebrow="Query once, share the result"
        title="Dashboards"
        description="Saved SQL, shared charts, and visible freshness. Opening a dashboard uses cached results."
        actions={
          <>
            <ProjectSelect
              projects={projects}
              value={projectId}
              onChange={selectProject}
            />
            <button
              className="button primary"
              disabled={!canEdit}
              onClick={() => setEditor("new")}
            >
              <Plus size={15} />
              New dashboard
            </button>
          </>
        }
      />
      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}
      <div className="v5-dashboard-workspace">
        <aside className="card v5-dashboard-list">
          <div className="v5-list-head">
            <Field label="Find dashboard">
              <input
                value={search}
                placeholder="Search dashboards"
                onChange={(event) => {
                  setSearch(event.target.value);
                  setOffset(0);
                }}
              />
            </Field>
            {loading ? <Spinner /> : null}
          </div>
          {page?.items.map((item) => (
            <button
              key={item.id}
              className={`v5-dashboard-choice ${selectedId === item.id ? "selected" : ""}`}
              onClick={() => {
                setSelectedId(item.id);
                setHistoryOffset(0);
              }}
            >
              <LayoutDashboard size={17} />
              <span>
                <strong>{item.name}</strong>
                <small>
                  {item.last_success_at
                    ? `Updated ${date(item.last_success_at)}`
                    : "No successful refresh yet"}
                </small>
              </span>
            </button>
          ))}
          {page && !page.total ? (
            <p className="muted v5-note">
              No dashboards match this project and search.
            </p>
          ) : null}
          {page ? (
            <Pagination
              total={page.total}
              offset={offset}
              limit={20}
              onPage={setOffset}
              busy={loading}
            />
          ) : null}
        </aside>
        <main className="v5-dashboard-main">
          {board ? (
            <>
              <div className="v5-dashboard-title">
                <div>
                  <h2>{board.name}</h2>
                  <p className="muted">
                    {board.description ||
                      "A shared view of saved query results."}
                  </p>
                </div>
                <div className="head-actions">
                  <button
                    className="button"
                    disabled={
                      !canRun ||
                      busy ||
                      (!!active && !terminal.has(active.status))
                    }
                    onClick={() => void refresh()}
                  >
                    {busy ? <Spinner /> : <RefreshCw size={14} />}Refresh data
                  </button>
                  {canEdit ? (
                    <>
                      <button
                        className="icon-button"
                        aria-label="Edit dashboard"
                        onClick={() => setEditor(board)}
                      >
                        <Pencil size={15} />
                      </button>
                      <button
                        className="icon-button"
                        aria-label="Delete dashboard"
                        onClick={() => setDeleting(true)}
                      >
                        <Trash2 size={15} />
                      </button>
                    </>
                  ) : null}
                </div>
              </div>
              {board.parameters.length ? (
                <section className="card v5-dashboard-filters">
                  <h3>Filters</h3>
                  <ParameterValues
                    definitions={board.parameters}
                    values={filters}
                    onChange={setFilters}
                  />
                  <button
                    className="button"
                    disabled={cacheLoading}
                    onClick={() => {
                      setApplied({ ...filters });
                      setCacheRevision((value) => value + 1);
                    }}
                  >
                    View cached results
                  </button>
                  <p className="muted v5-note">
                    Apply filters to view an existing cache. Refresh data to run
                    the queries for these values.
                  </p>
                </section>
              ) : null}
              {active && !terminal.has(active.status) ? (
                <div className="info-strip" role="status">
                  <Spinner />
                  Refreshing{" "}
                  {
                    active.tiles.filter((tile) => tile.status === "succeeded")
                      .length
                  }{" "}
                  of {active.tiles.length} tiles · existing saved results remain
                  available.
                </div>
              ) : active?.error ? (
                <div className="inline-error" role="alert">
                  {active.error}
                </div>
              ) : null}
              {board.last_schedule_error ? (
                <div className="inline-error" role="alert">
                  {board.last_schedule_error}
                </div>
              ) : null}
              {snapshot ? (
                <>
                  <div className="v5-cache-stamp">
                    <Clock3 size={14} />
                    <span>Cached {date(snapshot.refreshed_at)}</span>
                    <Status status={snapshot.stale ? "stale" : "fresh"} />
                    <span>
                      {board.cron && board.enabled
                        ? `${board.cron} · ${board.timezone}`
                        : "Manual refresh"}
                    </span>
                  </div>
                  {snapshot.latest_refresh?.error ? (
                    <p className="inline-error" role="alert">
                      {snapshot.latest_refresh.error}
                    </p>
                  ) : null}
                  <div className="v5-dashboard-grid">
                    {snapshot.tiles.map((tile) => (
                      <section
                        className="card v5-dashboard-tile"
                        key={`${snapshot.refresh_id}:${tile.id}`}
                        style={
                          {
                            "--tile-width": tile.width,
                            "--tile-height": tile.height,
                          } as CSSProperties
                        }
                      >
                        <header>
                          <h3>{tile.title || "Saved query"}</h3>
                          <BarChart3 size={15} />
                        </header>
                        <TileResult tile={tile} />
                        <footer>
                          <span>Source {tile.query_hash.slice(0, 8)}</span>
                          {onRun ? (
                            <button
                              className="text-button"
                              onClick={() => onRun(tile.run_id)}
                            >
                              Open query run
                            </button>
                          ) : (
                            <span>Run {tile.run_id.slice(0, 10)}</span>
                          )}
                        </footer>
                      </section>
                    ))}
                  </div>
                </>
              ) : cacheLoading ? (
                <div className="v5-loading" role="status">
                  <Spinner />
                  Loading saved results…
                </div>
              ) : (
                <Empty
                  icon={<LayoutDashboard size={28} />}
                  title="No cached result"
                >
                  {snapshotError ||
                    "Refresh this dashboard to publish its first result."}
                </Empty>
              )}
              <details
                className="card v5-refresh-history"
                onToggle={(event) => setHistoryOpen(event.currentTarget.open)}
              >
                <summary>Recent refreshes</summary>
                {history ? (
                  <>
                    <div className="table-scroll">
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th>Started</th>
                            <th>Status</th>
                            <th>Source</th>
                            <th>Details</th>
                          </tr>
                        </thead>
                        <tbody>
                          {history.items.map((item) => (
                            <tr key={item.id}>
                              <td>{date(item.created_at)}</td>
                              <td>
                                <Status status={item.status} />
                              </td>
                              <td>{item.source}</td>
                              <td>
                                <button
                                  className="text-button"
                                  onClick={() => setDetail(item)}
                                >
                                  Inspect refresh
                                </button>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <Pagination
                      total={history.total}
                      offset={historyOffset}
                      limit={10}
                      onPage={setHistoryOffset}
                    />
                  </>
                ) : historyOpen ? (
                  <Spinner />
                ) : null}
              </details>
            </>
          ) : error ? (
            <Empty
              icon={<LayoutDashboard size={30} />}
              title="Dashboard unavailable"
              action={
                <button
                  className="button"
                  onClick={() => setRevision((value) => value + 1)}
                >
                  Try again
                </button>
              }
            >
              {error}
            </Empty>
          ) : loading || selectedId ? (
            <div className="v5-loading">
              <Spinner />
              Loading dashboard…
            </div>
          ) : (
            <Empty
              icon={<LayoutDashboard size={30} />}
              title="Build a shared view"
            >
              Save queries in Data workspace, then arrange them into a
              dashboard.
            </Empty>
          )}
        </main>
      </div>
      {editor ? (
        <Editor
          key={editor === "new" ? "new" : editor.id}
          initial={editor === "new" ? null : editor}
          projectId={projectId}
          onClose={() => setEditor(null)}
          onSaved={(value) => {
            setEditor(null);
            setSelectedId(value.id);
            setRevision((value) => value + 1);
          }}
        />
      ) : null}
      {deleting && board ? (
        <Modal title="Delete dashboard?" onClose={() => setDeleting(false)}>
          <p>
            This removes {board.name}, its cached results, and its refresh
            history. Saved SQL queries and underlying data remain available.
          </p>
          <div className="modal-actions">
            <button className="button" onClick={() => setDeleting(false)}>
              Cancel
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={() => void remove()}
            >
              {busy ? <Spinner /> : <Trash2 size={14} />}Delete dashboard
            </button>
          </div>
        </Modal>
      ) : null}
      {detail ? (
        <Modal title="Refresh details" wide onClose={() => setDetail(null)}>
          <div className="v5-refresh-detail">
            <Status status={detail.status} />
            <p>
              Requested {date(detail.created_at)} · {detail.source}
            </p>
            <pre>{JSON.stringify(detail.parameters, null, 2)}</pre>
            {detail.error ? (
              <p className="inline-error">{detail.error}</p>
            ) : null}
            {detail.tiles.map((tile) => (
              <div className="v5-refresh-tile" key={tile.id}>
                <strong>{tile.id}</strong>
                <Status status={tile.status} />
                <small>Query {tile.query_hash.slice(0, 12)}</small>
                {tile.run_id && onRun ? (
                  <button
                    className="text-button"
                    onClick={() => {
                      onRun(tile.run_id as string);
                      setDetail(null);
                    }}
                  >
                    Open run
                  </button>
                ) : tile.run_id ? (
                  <code>{tile.run_id}</code>
                ) : null}
                {tile.error ? <p>{tile.error}</p> : null}
              </div>
            ))}
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
