"use client";
import { useEffect, useState, type FormEvent } from "react";
import dynamic from "next/dynamic";
import {
  BarChart3,
  Database,
  Play,
  Plus,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";
import {
  api,
  date,
  json,
  type Cluster,
  type Connection,
  type Page,
  type QueryResult,
} from "@/lib/api";
import type { DataExecution, SavedQuery } from "@/lib/platform";
import {
  Empty,
  Field,
  Modal,
  Pagination,
  SectionHead,
  Spinner,
  Status,
} from "./ui";
import { ResultTable } from "./platform-controls";
const CodeEditor = dynamic(() => import("./code-editor"), { ssr: false });
type Check = {
  type: string;
  column?: string;
  min?: number;
  max?: number;
  max_age_seconds?: number;
};
type Ingestion = {
  mode?: "watermark" | "change_tracking";
  id: string;
  name: string;
  project_id: string;
  connection_id: string;
  cluster_id?: string;
  source: { schema: string; table: string };
  sink: { path?: string; table?: string };
  keys: string[];
  watermark: { column: string; type: string; initial?: unknown };
  jdbc?: {
    fetch_size?: number;
    partition_column?: string;
    lower_bound?: number;
    upper_bound?: number;
    num_partitions?: number;
  };
  watermark_value?: unknown;
  last_run_id?: string;
};
function SmallChart({ result }: { result: QueryResult }) {
  const [x, setX] = useState(0),
    [y, setY] = useState(Math.min(1, result.columns.length - 1)),
    [kind, setKind] = useState("bar");
  const rows = (result.rows || [])
      .filter((row) => Number.isFinite(Number(row[y])))
      .slice(0, 40),
    min = Math.min(0, ...rows.map((row) => Number(row[y]))),
    max = Math.max(1, ...rows.map((row) => Number(row[y]))),
    range = max - min,
    points = rows.map((row, index) => ({
      x: 40 + ((index + 0.5) * 700) / Math.max(1, rows.length),
      y: 200 - ((Number(row[y]) - min) / range) * 170,
      label: String(row[x]),
      value: Number(row[y]),
    })),
    zero = 200 - ((0 - min) / range) * 170;
  return (
    <section className="chart-panel">
      <div className="chart-controls">
        <BarChart3 size={15} />
        <Field label="Chart">
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value)}
          >
            <option value="bar">Bar</option>
            <option value="line">Line</option>
          </select>
        </Field>
        <Field label="Labels">
          <select
            value={x}
            onChange={(event) => setX(Number(event.target.value))}
          >
            {result.columns.map((column, index) => (
              <option key={index} value={index}>
                {column}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Values">
          <select
            value={y}
            onChange={(event) => setY(Number(event.target.value))}
          >
            {result.columns.map((column, index) => (
              <option key={index} value={index}>
                {column}
              </option>
            ))}
          </select>
        </Field>
      </div>
      {points.length ? (
        <svg
          viewBox="0 0 780 255"
          role="img"
          aria-label={`${kind} chart of ${result.columns[y]} by ${result.columns[x]}`}
        >
          <line x1="35" y1={zero} x2="755" y2={zero} stroke="var(--line)" />
          {kind === "line" ? (
            <polyline
              points={points.map((point) => `${point.x},${point.y}`).join(" ")}
              fill="none"
              stroke="var(--coral)"
              strokeWidth="3"
            />
          ) : null}
          {points.map((point, index) => (
            <g key={index}>
              {kind === "bar" ? (
                <rect
                  x={point.x - 250 / points.length}
                  y={Math.min(zero, point.y)}
                  width={500 / points.length}
                  height={Math.max(1, Math.abs(zero - point.y))}
                  fill="var(--coral)"
                />
              ) : (
                <circle cx={point.x} cy={point.y} r="4" fill="var(--coral)" />
              )}
              <title>
                {point.label}: {point.value}
              </title>
              {points.length <= 12 ? (
                <text
                  x={point.x}
                  y="225"
                  textAnchor="middle"
                  fill="var(--muted)"
                  fontSize="10"
                >
                  {point.label.slice(0, 15)}
                </text>
              ) : null}
            </g>
          ))}
        </svg>
      ) : (
        <p className="form-note">
          Choose a numeric value column to draw the chart.
        </p>
      )}
      <p className="form-note">
        Charts use up to 40 rows from this result preview.
      </p>
    </section>
  );
}
export default function DataWorkspace({
  role,
  projectId,
  clusters,
  connections,
  onError,
}: {
  role: string;
  projectId: string;
  clusters: Cluster[];
  connections: Connection[];
  onError: (message: string) => void;
}) {
  const canEdit = ["editor", "owner"].includes(role),
    canRun = role !== "viewer",
    canManage = role === "owner";
  const [tab, setTab] = useState("sql"),
    [engine, setEngine] = useState<"spark" | "sqlserver" | "duckdb">("spark"),
    [duckSources, setDuckSources] = useState<
      { name: string; path: string; format?: string; connection_id?: string }[]
    >([]),
    [connectionId, setConnectionId] = useState(""),
    [clusterId, setClusterId] = useState(""),
    [sql, setSql] = useState("SELECT * FROM range(10)"),
    [params, setParams] = useState("[]"),
    [execution, setExecution] = useState<DataExecution | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [version, setVersion] = useState(0),
    [queries, setQueries] = useState<Page<SavedQuery> | null>(null),
    [queryOffset, setQueryOffset] = useState(0),
    [selectedQuery, setSelectedQuery] = useState<SavedQuery | null>(null),
    [saveOpen, setSaveOpen] = useState(false),
    [queryName, setQueryName] = useState(""),
    [history, setHistory] = useState<Page<DataExecution> | null>(null),
    [historyOffset, setHistoryOffset] = useState(0),
    [showChart, setShowChart] = useState(false);
  const [catalog, setCatalog] = useState(""),
    [schema, setSchema] = useState(""),
    [table, setTable] = useState(""),
    [path, setPath] = useState(""),
    [format, setFormat] = useState("parquet"),
    [checks, setChecks] = useState<Check[]>([{ type: "row_count", min: 1 }]),
    [lineage, setLineage] = useState<Page<{
      id: string;
      dataset: string;
      action: string;
      run_id?: string;
      created_at?: string;
      details?: unknown;
    }> | null>(null),
    [lineageOffset, setLineageOffset] = useState(0),
    [deltaVersion, setDeltaVersion] = useState(0),
    [retentionHours, setRetentionHours] = useState(168),
    [ingestions, setIngestions] = useState<Page<Ingestion> | null>(null),
    [ingestionOffset, setIngestionOffset] = useState(0),
    [ingestion, setIngestion] = useState<Ingestion | null>(null),
    [keyText, setKeyText] = useState("id");
  const active = execution && ["queued", "running"].includes(execution.status);
  useEffect(() => {
    setExecution(null);
    setConnectionId("");
    setClusterId("");
    setSelectedQuery(null);
    setQueryOffset(0);
    setHistoryOffset(0);
    setLineageOffset(0);
    setIngestionOffset(0);
    setDuckSources([]);
  }, [projectId]);
  const computeIds = clusters.map((cluster) => cluster.id).join(",");
  useEffect(() => {
    setClusterId((previous) =>
      clusters.some((cluster) => cluster.id === previous)
        ? previous
        : clusters.some((cluster) => cluster.id === "local")
          ? ""
          : clusters[0]?.id || "",
    );
  }, [projectId, computeIds]);
  useEffect(() => {
    let stopped = false;
    Promise.all([
      api<Page<SavedQuery>>(
        `/data/queries?project_id=${projectId}&limit=20&offset=${queryOffset}`,
      ),
      api<Page<DataExecution>>(
        `/data/executions?project_id=${projectId}&limit=15&offset=${historyOffset}`,
      ),
    ])
      .then(([queries, history]) => {
        if (!stopped) {
          setQueries(queries);
          setHistory(history);
        }
      })
      .catch((error) => {
        if (!stopped) onError(error.message);
      });
    return () => {
      stopped = true;
    };
  }, [projectId, queryOffset, historyOffset, version, onError]);
  useEffect(() => {
    if (!["quality", "ingestion"].includes(tab)) return;
    let stopped = false;
    const endpoint =
      tab === "quality"
        ? `/data/lineage?project_id=${projectId}&limit=20&offset=${lineageOffset}`
        : `/data/ingestions?project_id=${projectId}&limit=20&offset=${ingestionOffset}`;
    api<Page<never>>(endpoint)
      .then((result) => {
        if (!stopped) {
          if (tab === "quality") setLineage(result);
          else setIngestions(result);
        }
      })
      .catch((error) => {
        if (!stopped) onError(error.message);
      });
    return () => {
      stopped = true;
    };
  }, [tab, projectId, lineageOffset, ingestionOffset, version, onError]);
  useEffect(() => {
    if (!active || !execution) return;
    let stopped = false;
    const timer = setInterval(
      () =>
        api<DataExecution>(`/data/executions/${execution.id}`)
          .then((result) => {
            if (!stopped) {
              setExecution(result);
              if (!["queued", "running"].includes(result.status))
                setVersion((value) => value + 1);
            }
          })
          .catch((error) => {
            if (!stopped) setError(error.message);
          }),
      1200,
    );
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [active, execution?.id]);
  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await work();
      setVersion((value) => value + 1);
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function execute(
    operation: string,
    payload: Record<string, unknown> = {},
    overrides: Record<string, unknown> = {},
  ) {
    if (!canRun) {
      setError("Your project role can view results but cannot execute work.");
      return;
    }
    await perform(async () => {
      const values = operation === "query" ? JSON.parse(params) : [];
      if (!Array.isArray(values))
        throw Error("Query parameters must be a JSON array.");
      setExecution(
        await api<DataExecution>(
          "/data/execute",
          json("POST", {
            project_id: projectId,
            engine,
            connection_id: connectionId || null,
            cluster_id: clusterId || null,
            query_id: operation === "query" ? selectedQuery?.id || null : null,
            operation,
            sql,
            params: values,
            catalog: catalog || null,
            schema: schema || null,
            table: table || null,
            limit: 100,
            payload:
              engine === "duckdb"
                ? { ...payload, sources: duckSources }
                : payload,
            ...overrides,
          }),
        ),
      );
    });
  }
  async function saveQuery(event: FormEvent) {
    event.preventDefault();
    await perform(async () => {
      setSelectedQuery(
        await api<SavedQuery>(
          selectedQuery ? `/data/queries/${selectedQuery.id}` : "/data/queries",
          json(selectedQuery ? "PUT" : "POST", {
            project_id: projectId,
            name: queryName,
            engine,
            sql,
            connection_id: connectionId || null,
            cluster_id: clusterId || null,
            params: JSON.parse(params),
            payload: engine === "duckdb" ? { sources: duckSources } : {},
          }),
        ),
      );
      setSaveOpen(false);
    });
  }
  function loadQuery(query: SavedQuery) {
    setSelectedQuery(query);
    setSql(query.sql);
    setEngine(query.engine);
    setConnectionId(query.connection_id || "");
    setClusterId(query.cluster_id || "");
    setParams(JSON.stringify(query.params || [], null, 2));
    setQueryName(query.name);
    setDuckSources(query.payload?.sources || []);
    setTab("sql");
  }
  function dataset() {
    return path
      ? { path, format }
      : {
          table: table.includes(".")
            ? table
            : [catalog, schema, table].filter(Boolean).join("."),
        };
  }
  const sqlConnections = connections.filter(
    (item) => item.type === "sqlserver",
  );
  return (
    <>
      <SectionHead
        eyebrow="MAKE SENSE OF YOUR DATA"
        title="Data workspace"
        description="Explore schemas, write SQL, save useful queries, and check the data your pipelines produce."
      />
      <div className="schedule-tabs data-tabs" role="tablist">
        {[
          ["sql", "SQL workspace"],
          ["explore", "Data explorer"],
          ["quality", "Quality & lineage"],
          ["delta", "Delta tables"],
          ["ingestion", "SQL ingestion"],
        ].map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? "active" : ""}
            onClick={() => {
              setTab(key);
              setExecution(null);
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="data-workspace-layout">
        <aside className="card saved-query-list">
          <div className="card-heading">
            <h3>Saved queries</h3>
            <button
              className="icon-button"
              aria-label="New query"
              onClick={() => {
                setSelectedQuery(null);
                setSql("SELECT * FROM range(10)");
                setQueryName("");
                setTab("sql");
              }}
            >
              <Plus size={15} />
            </button>
          </div>
          {queries?.items.map((query) => (
            <button
              key={query.id}
              className={selectedQuery?.id === query.id ? "selected" : ""}
              onClick={() => loadQuery(query)}
            >
              <Database size={13} />
              <span>
                {query.name}
                <small>{query.engine}</small>
              </span>
            </button>
          ))}
          {!queries?.items.length ? (
            <p className="form-note">Save a query to return to it later.</p>
          ) : null}
          <Pagination
            total={queries?.total || 0}
            limit={20}
            offset={queryOffset}
            onPage={setQueryOffset}
          />
        </aside>
        <div className="data-main">
          <section className="card data-source-fields">
            <div className="form-grid">
              <Field label="Query engine">
                <select
                  value={engine}
                  onChange={(event) => {
                    setEngine(
                      event.target.value as "spark" | "sqlserver" | "duckdb",
                    );
                    setExecution(null);
                  }}
                >
                  <option value="spark">Spark SQL</option>
                  <option value="sqlserver">Microsoft SQL Server</option>
                  <option value="duckdb">DuckDB · quick file queries</option>
                </select>
              </Field>

              <Field label="Compute">
                <select
                  value={clusterId}
                  onChange={(event) => setClusterId(event.target.value)}
                >
                  <option
                    value=""
                    disabled={
                      !clusters.some((cluster) => cluster.id === "local")
                    }
                  >
                    {clusters.some((cluster) => cluster.id === "local")
                      ? "Default local compute"
                      : "Choose compute"}
                  </option>
                  {clusters.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </Field>

              {engine === "sqlserver" ? (
                <Field label="Connection">
                  <select
                    value={connectionId}
                    onChange={(event) => setConnectionId(event.target.value)}
                  >
                    <option value="">Choose connection</option>
                    {sqlConnections.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </Field>
              ) : null}
            </div>
          </section>
          {tab === "sql" ? (
            <section className="card sql-workspace">
              {engine === "duckdb" ? (
                <div className="modal-form">
                  <div className="card-heading">
                    <h3>Files for this query</h3>
                    <button
                      className="button compact"
                      onClick={() =>
                        setDuckSources([
                          ...duckSources,
                          {
                            name: `dataset_${duckSources.length + 1}`,
                            path: "",
                            format: "parquet",
                          },
                        ])
                      }
                      disabled={!canRun || duckSources.length >= 32}
                    >
                      <Plus size={13} />
                      Add file source
                    </button>
                  </div>
                  <p className="form-note">
                    Query CSV, Parquet, or JSON files as named tables. This
                    engine runs without starting Spark.
                  </p>
                  {duckSources.map((source, index) => (
                    <div className="form-grid" key={index}>
                      <Field label="Table alias">
                        <input
                          value={source.name}
                          onChange={(event) =>
                            setDuckSources(
                              duckSources.map((item, position) =>
                                position === index
                                  ? { ...item, name: event.target.value }
                                  : item,
                              ),
                            )
                          }
                        />
                      </Field>
                      <Field label="File path">
                        <input
                          value={source.path}
                          placeholder="blob://workspace/orders.parquet"
                          onChange={(event) =>
                            setDuckSources(
                              duckSources.map((item, position) =>
                                position === index
                                  ? { ...item, path: event.target.value }
                                  : item,
                              ),
                            )
                          }
                        />
                      </Field>
                      <Field label="File format">
                        <select
                          value={source.format || "parquet"}
                          onChange={(event) =>
                            setDuckSources(
                              duckSources.map((item, position) =>
                                position === index
                                  ? { ...item, format: event.target.value }
                                  : item,
                              ),
                            )
                          }
                        >
                          <option value="parquet">Parquet</option>
                          <option value="csv">CSV</option>
                          <option value="json">JSON</option>
                        </select>
                      </Field>
                      <Field label="Remote storage connection">
                        <select
                          value={source.connection_id || ""}
                          onChange={(event) =>
                            setDuckSources(
                              duckSources.map((item, position) =>
                                position === index
                                  ? {
                                      ...item,
                                      connection_id:
                                        event.target.value || undefined,
                                    }
                                  : item,
                              ),
                            )
                          }
                        >
                          <option value="">Local file</option>
                          {connections
                            .filter((item) => item.type === "s3")
                            .map((item) => (
                              <option key={item.id} value={item.id}>
                                {item.name}
                              </option>
                            ))}
                        </select>
                      </Field>
                      <button
                        className="button compact"
                        onClick={() =>
                          setDuckSources(
                            duckSources.filter(
                              (_, position) => position !== index,
                            ),
                          )
                        }
                      >
                        Remove source
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}
              <div className="card-heading">
                <h3>{selectedQuery?.name || "Untitled query"}</h3>
                <div className="row-actions">
                  <button
                    className="button compact"
                    disabled={!canEdit}
                    onClick={() => {
                      setQueryName(selectedQuery?.name || "");
                      setSaveOpen(true);
                    }}
                  >
                    <Save size={13} />
                    Save query
                  </button>
                  {selectedQuery ? (
                    <button
                      className="icon-button"
                      aria-label="Delete saved query"
                      disabled={!canEdit}
                      onClick={() => {
                        if (window.confirm("Delete this saved query?"))
                          void perform(async () => {
                            await api(
                              `/data/queries/${selectedQuery.id}`,
                              json("DELETE"),
                            );
                            setSelectedQuery(null);
                          });
                      }}
                    >
                      <Trash2 size={14} />
                    </button>
                  ) : null}
                  <button
                    className="button primary compact"
                    disabled={
                      !canRun ||
                      busy ||
                      !!active ||
                      (engine === "sqlserver" && !connectionId)
                    }
                    onClick={() => void execute("query")}
                  >
                    <Play size={13} />
                    Run query
                  </button>
                </div>
              </div>
              <CodeEditor
                language="sql"
                value={sql}
                onChange={setSql}
                onRun={!active ? () => void execute("query") : undefined}
                label="SQL query"
              />
              <div className="data-query-params">
                <Field
                  label={
                    engine === "sqlserver"
                      ? "Positional parameters (JSON array; use ? placeholders)"
                      : "Query parameters (JSON array)"
                  }
                >
                  <textarea
                    value={params}
                    onChange={(event) => setParams(event.target.value)}
                    rows={2}
                  />
                </Field>
              </div>
            </section>
          ) : null}
          {tab === "explore" ? (
            <section className="card platform-form">
              <h3>Browse source metadata</h3>
              <div className="form-grid">
                <Field label="Catalog (Spark, optional)">
                  <input
                    value={catalog}
                    onChange={(event) => setCatalog(event.target.value)}
                    placeholder="spark_catalog"
                  />
                </Field>
                <Field label="Schema / database">
                  <input
                    value={schema}
                    onChange={(event) => setSchema(event.target.value)}
                    placeholder={engine === "sqlserver" ? "dbo" : "default"}
                  />
                </Field>
                <Field label="Table">
                  <input
                    value={table}
                    onChange={(event) => setTable(event.target.value)}
                  />
                </Field>
              </div>
              <div className="row-actions">
                {engine === "spark" ? (
                  <button
                    className="button"
                    disabled={!canRun || busy || !!active}
                    onClick={() => void execute("catalogs")}
                  >
                    List catalogs
                  </button>
                ) : null}
                <button
                  className="button"
                  disabled={!canRun || busy || !!active}
                  onClick={() => void execute("schemas")}
                >
                  List schemas
                </button>
                <button
                  className="button"
                  disabled={!canRun || busy || !!active}
                  onClick={() => void execute("tables")}
                >
                  List tables
                </button>
                <button
                  className="button"
                  disabled={!canRun || busy || !!active || !table}
                  onClick={() => void execute("columns")}
                >
                  Columns
                </button>
                <button
                  className="button primary"
                  disabled={!canRun || busy || !!active || !table}
                  onClick={() => void execute("preview")}
                >
                  Preview 100 rows
                </button>
              </div>
              <p className="form-note">
                Spark metadata and previews run on the selected compute. SQL
                Server uses the selected saved connection.
              </p>
            </section>
          ) : null}
          {tab === "quality" ? (
            <section className="card platform-form">
              <h3>Data quality checks</h3>
              <p className="form-note">
                Checks run on Spark. Set a table or local blob path visible to
                the selected compute.
              </p>
              <div className="form-grid">
                <Field label="Table (full name)">
                  <input
                    value={table}
                    onChange={(event) => setTable(event.target.value)}
                    placeholder="spark_catalog.default.sales"
                  />
                </Field>
                <Field label="Or dataset path">
                  <input
                    value={path}
                    onChange={(event) => setPath(event.target.value)}
                    placeholder="blob://silver/sales"
                  />
                </Field>
                <Field label="File format">
                  <select
                    value={format}
                    onChange={(event) => setFormat(event.target.value)}
                  >
                    <option>parquet</option>
                    <option>delta</option>
                  </select>
                </Field>
              </div>
              {checks.map((check, index) => (
                <div className="quality-check-row" key={index}>
                  <Field label="Check">
                    <select
                      value={check.type}
                      onChange={(event) =>
                        setChecks((previous) =>
                          previous.map((item, i) =>
                            i === index
                              ? { ...item, type: event.target.value }
                              : item,
                          ),
                        )
                      }
                    >
                      {[
                        "not_null",
                        "unique",
                        "range",
                        "row_count",
                        "freshness",
                      ].map((type) => (
                        <option key={type}>{type}</option>
                      ))}
                    </select>
                  </Field>
                  {check.type !== "row_count" ? (
                    <Field label="Column">
                      <input
                        value={check.column || ""}
                        onChange={(event) =>
                          setChecks((previous) =>
                            previous.map((item, i) =>
                              i === index
                                ? { ...item, column: event.target.value }
                                : item,
                            ),
                          )
                        }
                      />
                    </Field>
                  ) : null}
                  {["range", "row_count"].includes(check.type) ? (
                    <>
                      <Field label="Minimum">
                        <input
                          type="number"
                          value={check.min ?? ""}
                          onChange={(event) =>
                            setChecks((previous) =>
                              previous.map((item, i) =>
                                i === index
                                  ? {
                                      ...item,
                                      min:
                                        event.target.value === ""
                                          ? undefined
                                          : Number(event.target.value),
                                    }
                                  : item,
                              ),
                            )
                          }
                        />
                      </Field>
                      <Field label="Maximum">
                        <input
                          type="number"
                          value={check.max ?? ""}
                          onChange={(event) =>
                            setChecks((previous) =>
                              previous.map((item, i) =>
                                i === index
                                  ? {
                                      ...item,
                                      max:
                                        event.target.value === ""
                                          ? undefined
                                          : Number(event.target.value),
                                    }
                                  : item,
                              ),
                            )
                          }
                        />
                      </Field>
                    </>
                  ) : null}
                  {check.type === "freshness" ? (
                    <Field label="Maximum age (seconds)">
                      <input
                        type="number"
                        min={1}
                        value={check.max_age_seconds || 3600}
                        onChange={(event) =>
                          setChecks((previous) =>
                            previous.map((item, i) =>
                              i === index
                                ? {
                                    ...item,
                                    max_age_seconds: Number(event.target.value),
                                  }
                                : item,
                            ),
                          )
                        }
                      />
                    </Field>
                  ) : null}
                  <button
                    className="icon-button"
                    aria-label={`Remove check ${index + 1}`}
                    onClick={() =>
                      setChecks(checks.filter((_, i) => i !== index))
                    }
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
              <div className="row-actions">
                <button
                  className="button"
                  onClick={() =>
                    setChecks([...checks, { type: "not_null", column: "" }])
                  }
                >
                  <Plus size={13} />
                  Add check
                </button>
                <button
                  className="button primary"
                  disabled={
                    !canRun ||
                    busy ||
                    !!active ||
                    (!path && !table) ||
                    !checks.length
                  }
                  onClick={() =>
                    void execute(
                      "quality",
                      { dataset: dataset(), checks, fail_on_error: true },
                      { engine: "spark" },
                    )
                  }
                >
                  Run checks
                </button>
              </div>
            </section>
          ) : null}
          {tab === "delta" ? (
            <section className="card platform-form">
              <h3>Inspect and maintain Delta tables</h3>
              <div className="form-grid">
                <Field label="Table (full name)">
                  <input
                    value={table}
                    onChange={(event) => setTable(event.target.value)}
                  />
                </Field>
                <Field label="Or local blob path">
                  <input
                    value={path}
                    onChange={(event) => setPath(event.target.value)}
                    placeholder="blob://silver/orders"
                  />
                </Field>
                <Field label="Restore version">
                  <input
                    type="number"
                    min={0}
                    value={deltaVersion}
                    onChange={(event) =>
                      setDeltaVersion(Number(event.target.value))
                    }
                  />
                </Field>
                <Field label="Vacuum retention (hours)">
                  <input
                    type="number"
                    min={168}
                    value={retentionHours}
                    onChange={(event) =>
                      setRetentionHours(Number(event.target.value))
                    }
                  />
                </Field>
              </div>
              <div className="row-actions">
                {[
                  ["delta_history", "History"],
                  ["delta_detail", "Details"],
                  ["delta_preview", "Preview version"],
                  ["delta_vacuum", "Preview vacuum"],
                ].map(([operation, label]) => (
                  <button
                    className="button"
                    key={operation}
                    disabled={!canRun || busy || !!active || (!table && !path)}
                    onClick={() =>
                      void execute(
                        operation,
                        {
                          ...dataset(),
                          version: deltaVersion,
                          retention_hours: retentionHours,
                          dry_run: true,
                        },
                        { engine: "spark" },
                      )
                    }
                  >
                    {label}
                  </button>
                ))}
                <button
                  className="button"
                  disabled={!canManage || busy || !!active || (!table && !path)}
                  onClick={() => {
                    if (
                      window.confirm(
                        `Restore this Delta table to version ${deltaVersion}? This creates a new table version.`,
                      )
                    )
                      void execute(
                        "delta_restore",
                        { ...dataset(), version: deltaVersion },
                        { engine: "spark" },
                      );
                  }}
                >
                  Restore version
                </button>
                <button
                  className="button danger"
                  disabled={!canManage || busy || !!active || (!table && !path)}
                  onClick={() => {
                    if (
                      window.confirm(
                        "Permanently remove files outside the retention window? Older versions may no longer be readable. Review the vacuum preview first.",
                      )
                    )
                      void execute(
                        "delta_vacuum",
                        {
                          ...dataset(),
                          retention_hours: retentionHours,
                          dry_run: false,
                        },
                        { engine: "spark" },
                      );
                  }}
                >
                  Run vacuum
                </button>
              </div>
              <p className="form-note">
                Delta operations require compatible Delta libraries on the
                selected compute. Use a preview before removing historical
                files.
              </p>
            </section>
          ) : null}
          {tab === "ingestion" ? (
            <section className="card">
              <div className="card-heading">
                <div>
                  <h3>Incremental SQL Server ingestion</h3>
                  <p>
                    Merge source rows into Delta by key and checkpoint
                    successful watermarks.
                  </p>
                </div>
                <button
                  className="button primary compact"
                  disabled={!canEdit}
                  onClick={() => {
                    setIngestion({
                      id: "",
                      name: "New ingestion",
                      mode: "change_tracking",
                      project_id: projectId,
                      connection_id: sqlConnections[0]?.id || "",
                      source: { schema: "dbo", table: "" },
                      sink: { path: "blob://bronze/orders" },
                      keys: ["id"],
                      watermark: { column: "updated_at", type: "timestamp" },
                      jdbc: { fetch_size: 10000 },
                    });
                    setKeyText("id");
                  }}
                >
                  <Plus size={13} />
                  New ingestion
                </button>
              </div>
              {ingestions?.items.map((item) => (
                <div className="ingestion-row" key={item.id}>
                  <button
                    className="name-link"
                    onClick={() => {
                      setIngestion(item);
                      setKeyText(item.keys.join(", "));
                    }}
                  >
                    {item.name}
                    <small>
                      {item.source.schema}.{item.source.table} →{" "}
                      {item.sink.table || item.sink.path}
                    </small>
                  </button>
                  <span className="muted">
                    {item.mode === "change_tracking"
                      ? "Change Tracking"
                      : "Watermark"}{" "}
                    {JSON.stringify(
                      item.mode === "change_tracking" &&
                        item.watermark_value &&
                        typeof item.watermark_value === "object"
                        ? (item.watermark_value as { version?: number }).version
                        : (item.watermark_value ?? "Not started"),
                    )}
                  </span>
                  <button
                    className="button compact"
                    disabled={!canRun || busy || !!active}
                    onClick={() =>
                      void perform(async () =>
                        setExecution(
                          await api(
                            `/data/ingestions/${item.id}/run`,
                            json("POST", { cluster_id: clusterId || null }),
                          ),
                        ),
                      )
                    }
                  >
                    <Play size={12} />
                    Run
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`Delete ingestion ${item.name}`}
                    disabled={!canEdit}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Delete ingestion “${item.name}” and its saved checkpoint? The destination data is retained.`,
                        )
                      )
                        void perform(async () => {
                          await api(
                            `/data/ingestions/${item.id}`,
                            json("DELETE"),
                          );
                        });
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
              {!ingestions?.items.length ? (
                <Empty title="Bring SQL Server data into your lake">
                  Create a key-based incremental ingestion using a saved SQL
                  Server connection.
                </Empty>
              ) : null}
              <Pagination
                total={ingestions?.total || 0}
                offset={ingestionOffset}
                limit={20}
                onPage={setIngestionOffset}
              />
              <p className="form-note padded">
                Change Tracking captures inserts, updates, and deletions by
                primary key. Watermark mode reads only rows above the saved
                watermark and does not capture deletions. Both modes use a
                dedicated Delta sink and advance checkpoints after success.
              </p>
            </section>
          ) : null}
          {error ? (
            <div className="inline-error" role="alert">
              {error}
            </div>
          ) : null}
          {execution ? (
            <section className="card execution-result">
              <div className="card-heading">
                <h3>Result</h3>
                <div className="row-actions">
                  <Status status={execution.status} />
                  {active ? <Spinner /> : null}
                  {execution.run_id ? (
                    <code>{execution.run_id.slice(0, 10)}</code>
                  ) : null}
                  {execution.result?.columns?.length ? (
                    <button
                      className="button compact"
                      onClick={() => setShowChart(!showChart)}
                    >
                      <BarChart3 size={13} />
                      {showChart ? "Hide chart" : "Chart"}
                    </button>
                  ) : null}
                  {active && execution.run_id ? (
                    <button
                      className="text-button"
                      onClick={() =>
                        void perform(async () => {
                          await api(
                            `/runs/${execution.run_id}/cancel`,
                            json("POST"),
                          );
                        })
                      }
                    >
                      Cancel query
                    </button>
                  ) : null}
                </div>
              </div>
              {execution.error ? (
                <pre className="run-error">{execution.error}</pre>
              ) : null}
              {tab === "explore" &&
              ["catalogs", "schemas", "tables"].includes(
                execution.operation || "",
              ) &&
              execution.result?.rows?.length ? (
                <div className="explorer-choices">
                  {execution.result.rows.map((row, index) => {
                    const columns = execution.result!.columns;
                    const tableIndex = columns.findIndex((column) =>
                      ["table_name", "tablename"].includes(
                        column.toLowerCase(),
                      ),
                    );
                    const value = String(row[tableIndex >= 0 ? tableIndex : 0]);
                    return (
                      <button
                        className="button compact"
                        key={index}
                        onClick={() => {
                          if (execution.operation === "catalogs") {
                            setCatalog(value);
                            setSchema("");
                            setTable("");
                            void execute(
                              "schemas",
                              {},
                              { catalog: value, schema: null, table: null },
                            );
                          } else if (execution.operation === "schemas") {
                            setSchema(value);
                            setTable("");
                            void execute(
                              "tables",
                              {},
                              { schema: value, table: null },
                            );
                          } else {
                            setTable(value);
                            void execute("preview", {}, { table: value });
                          }
                        }}
                      >
                        <Database size={12} />
                        {value}
                      </button>
                    );
                  })}
                </div>
              ) : null}
              <ResultTable result={execution.result} />
              {tab === "explore" &&
              execution.result &&
              (execution.result.offset ||
                execution.result.next_offset != null) ? (
                <div className="pagination">
                  <span>
                    Metadata page · {(execution.result.offset || 0) + 1} onward
                  </span>
                  <div>
                    <button
                      className="button"
                      disabled={!!active || !(execution.result.offset || 0)}
                      onClick={() =>
                        void execute(
                          execution.operation || "tables",
                          {},
                          {
                            offset: Math.max(
                              0,
                              (execution.result?.offset || 0) - 100,
                            ),
                          },
                        )
                      }
                    >
                      Previous
                    </button>
                    <button
                      className="button"
                      disabled={
                        !!active || execution.result.next_offset == null
                      }
                      onClick={() =>
                        void execute(
                          execution.operation || "tables",
                          {},
                          { offset: execution.result?.next_offset },
                        )
                      }
                    >
                      Next
                    </button>
                  </div>
                </div>
              ) : null}
              {showChart && execution.result ? (
                <SmallChart key={execution.id} result={execution.result} />
              ) : null}
              {execution.quality_reports?.length ? (
                <div className="quality-reports">
                  {execution.quality_reports.map((report, index) => (
                    <div key={index}>
                      <Status
                        status={
                          report.passed === true
                            ? "succeeded"
                            : report.passed === false
                              ? "failed"
                              : report.status
                        }
                      />
                      <strong>
                        {report.name || report.type || `Check ${index + 1}`}
                      </strong>
                      <pre>{JSON.stringify(report, null, 2)}</pre>
                    </div>
                  ))}
                </div>
              ) : null}
              {active ? (
                <p className="form-note padded">Waiting for compute results…</p>
              ) : null}
            </section>
          ) : null}
          {tab === "quality" ? (
            <section className="card">
              <div className="card-heading">
                <h3>Recorded data lineage</h3>
                <button
                  className="icon-button"
                  aria-label="Refresh lineage"
                  onClick={() => setVersion((value) => value + 1)}
                >
                  <RefreshCw size={14} />
                </button>
              </div>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Dataset</th>
                    <th>Action</th>
                    <th>Run</th>
                    <th>Recorded</th>
                  </tr>
                </thead>
                <tbody>
                  {lineage?.items.map((item) => (
                    <tr key={item.id}>
                      <td>{item.dataset}</td>
                      <td>{item.action}</td>
                      <td>
                        <code>{item.run_id?.slice(0, 10) || "—"}</code>
                      </td>
                      <td>{date(item.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!lineage?.items.length ? (
                <p className="form-note padded">
                  No lineage events recorded for this project.
                </p>
              ) : null}
              <Pagination
                total={lineage?.total || 0}
                offset={lineageOffset}
                limit={20}
                onPage={setLineageOffset}
              />
            </section>
          ) : null}
          <section className="card">
            <div className="card-heading">
              <h3>Query & data operation history</h3>
            </div>
            {history?.items.map((item) => (
              <button
                className="history-row"
                key={item.id}
                onClick={() =>
                  void perform(async () =>
                    setExecution(await api(`/data/executions/${item.id}`)),
                  )
                }
              >
                <Status status={item.status} />
                <span>{item.operation || "query"}</span>
                <code>{item.sql?.slice(0, 65) || item.id.slice(0, 10)}</code>
                <span>{date(item.created_at)}</span>
              </button>
            ))}
            <Pagination
              total={history?.total || 0}
              limit={15}
              offset={historyOffset}
              onPage={setHistoryOffset}
            />
          </section>
        </div>
      </div>
      {saveOpen ? (
        <Modal
          title={selectedQuery ? "Update saved query" : "Save query"}
          onClose={() => setSaveOpen(false)}
        >
          <form className="modal-form" onSubmit={saveQuery}>
            <Field label="Query name">
              <input
                required
                value={queryName}
                onChange={(event) => setQueryName(event.target.value)}
                autoFocus
              />
            </Field>
            {error ? <div className="inline-error">{error}</div> : null}
            <div className="modal-footer">
              <button className="button primary" disabled={busy}>
                <Save size={14} />
                Save query
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {ingestion ? (
        <Modal
          title={ingestion.id ? "Edit ingestion" : "Create ingestion"}
          wide
          onClose={() => setIngestion(null)}
        >
          <form
            className="modal-form"
            onSubmit={(event) => {
              event.preventDefault();
              void perform(async () => {
                await api(
                  ingestion.id
                    ? `/data/ingestions/${ingestion.id}`
                    : "/data/ingestions",
                  json(ingestion.id ? "PUT" : "POST", {
                    ...ingestion,
                    keys: keyText
                      .split(",")
                      .map((key) => key.trim())
                      .filter(Boolean),
                  }),
                );
                setIngestion(null);
              });
            }}
          >
            <fieldset className="plain-fieldset" disabled={!canEdit}>
              <div className="form-grid">
                <Field label="Name">
                  <input
                    required
                    value={ingestion.name}
                    onChange={(event) =>
                      setIngestion({ ...ingestion, name: event.target.value })
                    }
                  />
                </Field>
                <Field label="SQL Server connection">
                  <select
                    required
                    value={ingestion.connection_id}
                    onChange={(event) =>
                      setIngestion({
                        ...ingestion,
                        connection_id: event.target.value,
                      })
                    }
                  >
                    <option value="">Choose connection</option>
                    {sqlConnections.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Source schema">
                  <input
                    required
                    value={ingestion.source.schema}
                    onChange={(event) =>
                      setIngestion({
                        ...ingestion,
                        source: {
                          ...ingestion.source,
                          schema: event.target.value,
                        },
                      })
                    }
                  />
                </Field>
                <Field label="Source table">
                  <input
                    required
                    value={ingestion.source.table}
                    onChange={(event) =>
                      setIngestion({
                        ...ingestion,
                        source: {
                          ...ingestion.source,
                          table: event.target.value,
                        },
                      })
                    }
                  />
                </Field>
                <Field label="Delta sink path">
                  <input
                    value={ingestion.sink.path || ""}
                    onChange={(event) =>
                      setIngestion({
                        ...ingestion,
                        sink: { path: event.target.value },
                      })
                    }
                  />
                </Field>
                <Field label="Or Delta table">
                  <input
                    value={ingestion.sink.table || ""}
                    onChange={(event) =>
                      setIngestion({
                        ...ingestion,
                        sink: { table: event.target.value },
                      })
                    }
                  />
                </Field>
                <Field label="Unique merge keys (comma separated)">
                  <input
                    required
                    value={keyText}
                    onChange={(event) => setKeyText(event.target.value)}
                  />
                </Field>
                <Field
                  label="Synchronization mode"
                  hint="Change Tracking captures updates and deletions using a consistent source snapshot."
                >
                  <select
                    value={ingestion.mode || "watermark"}
                    onChange={(event) =>
                      setIngestion({
                        ...ingestion,
                        mode: event.target.value as
                          "watermark" | "change_tracking",
                        jdbc: {
                          fetch_size: ingestion.jdbc?.fetch_size || 4096,
                        },
                      })
                    }
                  >
                    <option value="change_tracking">
                      Change Tracking · insert, update, delete
                    </option>
                    <option value="watermark">
                      Watermark · rows after a timestamp or value
                    </option>
                  </select>
                </Field>
                {(ingestion.mode || "watermark") === "watermark" ? (
                  <>
                    <Field label="Watermark column">
                      <input
                        required
                        value={ingestion.watermark.column}
                        onChange={(event) =>
                          setIngestion({
                            ...ingestion,
                            watermark: {
                              ...ingestion.watermark,
                              column: event.target.value,
                            },
                          })
                        }
                      />
                    </Field>
                    <Field label="Watermark type">
                      <select
                        value={ingestion.watermark.type}
                        onChange={(event) =>
                          setIngestion({
                            ...ingestion,
                            watermark: {
                              ...ingestion.watermark,
                              type: event.target.value,
                            },
                          })
                        }
                      >
                        {["timestamp", "date", "number", "string"].map(
                          (type) => (
                            <option key={type}>{type}</option>
                          ),
                        )}
                      </select>
                    </Field>
                    <Field label="Initial watermark (optional)">
                      <input
                        value={String(ingestion.watermark.initial ?? "")}
                        onChange={(event) =>
                          setIngestion({
                            ...ingestion,
                            watermark: {
                              ...ingestion.watermark,
                              initial:
                                event.target.value === ""
                                  ? undefined
                                  : ingestion.watermark.type === "number"
                                    ? Number(event.target.value)
                                    : event.target.value,
                            },
                          })
                        }
                      />
                    </Field>
                  </>
                ) : null}
                <Field label="Fetch size">
                  <input
                    type="number"
                    min={1}
                    value={ingestion.jdbc?.fetch_size || 10000}
                    onChange={(event) =>
                      setIngestion({
                        ...ingestion,
                        jdbc: {
                          ...ingestion.jdbc,
                          fetch_size: Number(event.target.value),
                        },
                      })
                    }
                  />
                </Field>
                <Field label="Compute">
                  <select
                    value={ingestion.cluster_id || ""}
                    onChange={(event) =>
                      setIngestion({
                        ...ingestion,
                        cluster_id: event.target.value,
                      })
                    }
                  >
                    <option value="">Default compute</option>
                    {clusters.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              {(ingestion.mode || "watermark") === "watermark" ? (
                <details>
                  <summary>Parallel JDBC reads (optional)</summary>
                  <div className="form-grid">
                    {[
                      ["partition_column", "Partition column"],
                      ["lower_bound", "Lower bound"],
                      ["upper_bound", "Upper bound"],
                      ["num_partitions", "Partitions"],
                    ].map(([key, label]) => (
                      <Field key={key} label={label}>
                        <input
                          value={String(
                            (
                              ingestion.jdbc as
                                Record<string, unknown> | undefined
                            )?.[key] ?? "",
                          )}
                          onChange={(event) =>
                            setIngestion({
                              ...ingestion,
                              jdbc: {
                                ...ingestion.jdbc,
                                [key]:
                                  key === "partition_column"
                                    ? event.target.value
                                    : Number(event.target.value),
                              },
                            })
                          }
                        />
                      </Field>
                    ))}
                  </div>
                </details>
              ) : null}
              <p className="form-note">
                {ingestion.mode === "change_tracking"
                  ? "Enable Change Tracking and snapshot isolation on the source. Merge keys must match its primary key. Use a new, dedicated Delta sink. Schema changes or expired checkpoints stop the ingestion for review; successful runs capture inserts, updates, and deletions."
                  : "Successful runs advance the watermark. This mode does not replicate source deletions or late arrivals below that watermark."}
              </p>
              {error ? <div className="inline-error">{error}</div> : null}
            </fieldset>
            <div className="modal-footer">
              <button className="button primary" disabled={busy || !canEdit}>
                Save ingestion
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
    </>
  );
}
