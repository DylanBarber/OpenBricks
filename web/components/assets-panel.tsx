"use client";

import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import {
  Database,
  GitBranch,
  Pencil,
  RefreshCw,
  Save,
  Search,
  UserRound,
} from "lucide-react";
import { api, date, json, type Page, type Project } from "@/lib/api";
import type { Asset, AssetGraph, AssetQuality } from "@/lib/v5-data";
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

const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed.";
const age = (seconds?: number | null) =>
  seconds == null
    ? "No recorded successful write"
    : seconds < 60
      ? "Less than a minute"
      : seconds < 3600
        ? `${Math.floor(seconds / 60)} minutes`
        : seconds < 86400
          ? `${Math.floor(seconds / 3600)} hours`
          : `${Math.floor(seconds / 86400)} days`;

function Health({ value }: { value: Asset["health"] }) {
  return (
    <span className={`v5-health v5-health-${value}`}>
      <span />
      {value}
    </span>
  );
}

function graphLayout(graph: AssetGraph, focus: string) {
  const levels = new Map<string, number>();
  const focusNode = graph.nodes.find((node) => node.dataset === focus);
  if (focusNode) {
    levels.set(focusNode.id, 0);
    for (let iteration = 0; iteration < 2; iteration++) {
      for (const edge of graph.edges) {
        if (levels.has(edge.source) && !levels.has(edge.target))
          levels.set(edge.target, (levels.get(edge.source) || 0) + 1);
        if (levels.has(edge.target) && !levels.has(edge.source))
          levels.set(edge.source, (levels.get(edge.target) || 0) - 1);
      }
    }
  } else {
    const targets = new Set(graph.edges.map((edge) => edge.target));
    const roots = graph.nodes.filter((node) => !targets.has(node.id));
    for (const node of roots.length ? roots : graph.nodes.slice(0, 1))
      levels.set(node.id, 0);
    for (let iteration = 0; iteration < 3; iteration++)
      for (const edge of graph.edges)
        if (levels.has(edge.source) && !levels.has(edge.target))
          levels.set(
            edge.target,
            Math.min(3, (levels.get(edge.source) || 0) + 1),
          );
  }
  for (const node of graph.nodes)
    if (!levels.has(node.id)) levels.set(node.id, 0);
  const columns = [...new Set(levels.values())].sort((a, b) => a - b);
  const groups = columns.map((level) =>
    graph.nodes
      .filter((node) => levels.get(node.id) === level)
      .sort((a, b) => a.dataset.localeCompare(b.dataset)),
  );
  const height = Math.max(
    240,
    Math.max(1, ...groups.map((group) => group.length)) * 90 + 45,
  );
  const width = Math.max(900, groups.length * 260 + 40);
  const positions = new Map<string, { x: number; y: number }>();
  groups.forEach((group, column) =>
    group.forEach((node, row) =>
      positions.set(node.id, {
        x: 20 + column * 260,
        y: 25 + row * 90 + (height - 45 - group.length * 90) / 2,
      }),
    ),
  );
  return { positions, width, height };
}

function LineageGraph({
  graph,
  focus,
  selected,
  onFocus,
  onRun,
}: {
  graph: AssetGraph;
  focus: string;
  selected?: string;
  onFocus: (asset: Asset) => void;
  onRun?: (id: string) => void;
}) {
  const marker = useId().replaceAll(":", "");
  const { positions, width, height } = useMemo(
    () => graphLayout(graph, focus),
    [graph, focus],
  );
  return (
    <>
      <div className="v5-graph-scroll">
        <svg
          className="v5-asset-graph"
          viewBox={`0 0 ${width} ${height}`}
          style={{ minWidth: Math.min(width, 1100), height }}
          role="group"
          aria-label="Observed dataset dependency graph"
        >
          <defs>
            <marker
              id={marker}
              markerWidth="8"
              markerHeight="8"
              refX="7"
              refY="3"
              orient="auto"
            >
              <path d="M0,0 L0,6 L8,3 z" fill="var(--muted)" />
            </marker>
          </defs>
          {graph.edges.map((edge, index) => {
            const source = positions.get(edge.source),
              target = positions.get(edge.target);
            if (!source || !target) return null;
            const x1 = source.x + 205,
              x2 = target.x,
              y1 = source.y + 30,
              y2 = target.y + 30;
            const middle = (x1 + x2) / 2;
            return (
              <path
                key={`${edge.source}:${edge.target}:${index}`}
                d={`M${x1},${y1} C${middle},${y1} ${middle},${y2} ${x2},${y2}`}
                fill="none"
                stroke="var(--muted)"
                strokeWidth="1.4"
                opacity=".55"
                markerEnd={`url(#${marker})`}
              >
                <title>
                  Observed in run {edge.run_id || "unknown"} ·{" "}
                  {edge.run_status || "unknown"}
                </title>
              </path>
            );
          })}
          {graph.nodes.map((node) => {
            const point = positions.get(node.id);
            if (!point) return null;
            const name =
              node.dataset.length > 28
                ? `…${node.dataset.slice(-27)}`
                : node.dataset;
            return (
              <g
                key={node.id}
                role="button"
                tabIndex={0}
                aria-label={`Focus dataset ${node.dataset}`}
                className={`v5-graph-node v5-graph-node-${node.health} ${selected === node.id ? "selected" : ""}`}
                onClick={() => onFocus(node)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onFocus(node);
                  }
                }}
              >
                <title>
                  {node.dataset} · {node.health}
                </title>
                <rect x={point.x} y={point.y} width="205" height="61" rx="9" />
                <circle cx={point.x + 14} cy={point.y + 19} r="3.5" />
                <text
                  x={point.x + 26}
                  y={point.y + 23}
                  className="v5-graph-label"
                >
                  {name}
                </text>
                <text
                  x={point.x + 14}
                  y={point.y + 45}
                  className="v5-graph-subtitle"
                >
                  {node.owner?.username || "Unassigned"} · {node.health}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <p className="muted v5-note">
        Edges infer a dependency from reads and writes recorded in the same run.
        Select a dataset to focus its neighborhood.
      </p>
      {graph.truncated ? (
        <div className="info-strip">
          This graph reached its evidence or size limit. Focus a dataset or
          reduce the depth to see a smaller neighborhood.
        </div>
      ) : null}
      {onRun && graph.edges.length ? (
        <details className="v5-edge-list">
          <summary>Observed dependency runs</summary>
          {graph.edges.slice(0, 30).map((edge, index) => (
            <div key={index}>
              <span>
                {graph.nodes.find((node) => node.id === edge.source)?.dataset} →{" "}
                {graph.nodes.find((node) => node.id === edge.target)?.dataset}
              </span>
              {edge.run_id ? (
                <button
                  className="text-button"
                  onClick={() => onRun(edge.run_id as string)}
                >
                  Open run · {edge.run_status || "unknown"}
                </button>
              ) : null}
            </div>
          ))}
        </details>
      ) : null}
    </>
  );
}

function AnnotationEditor({
  asset,
  onClose,
  onSaved,
}: {
  asset: Asset;
  onClose: () => void;
  onSaved: (asset: Asset) => void;
}) {
  const [description, setDescription] = useState(asset.description),
    [owner, setOwner] = useState(asset.owner_id || ""),
    [minutes, setMinutes] = useState(
      asset.freshness_seconds ? String(asset.freshness_seconds / 60) : "",
    );
  const [members, setMembers] = useState<
      { user_id: string; username: string }[]
    >([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    let stopped = false;
    const controller = new AbortController();
    api<{ user_id: string; username: string }[]>(
      `/projects/${asset.project_id}/members`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!stopped) setMembers(value);
      })
      .catch((error) => {
        if (!stopped) setError(message(error));
      });
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [asset.project_id]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const value = await api<Asset>(
        `/assets/${asset.id}`,
        json("PUT", {
          project_id: asset.project_id,
          dataset: asset.dataset,
          description,
          owner_id: owner || null,
          freshness_seconds: minutes ? Number(minutes) * 60 : null,
        }),
      );
      onSaved(value);
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Edit data asset" onClose={onClose}>
      <form onSubmit={submit}>
        <p className="v5-dataset-name">{asset.dataset}</p>
        <Field label="Description">
          <textarea
            rows={4}
            value={description}
            maxLength={4000}
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
        <Field label="Owner">
          <select
            value={owner}
            onChange={(event) => setOwner(event.target.value)}
          >
            <option value="">Unassigned</option>
            {members.map((member) => (
              <option key={member.user_id} value={member.user_id}>
                {member.username}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Freshness threshold (minutes)"
          hint="Based on the last recorded successful write. Leave blank to disable this threshold."
        >
          <input
            type="number"
            min={1}
            max={525600}
            value={minutes}
            onChange={(event) => setMinutes(event.target.value)}
          />
        </Field>
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
            {busy ? <Spinner /> : <Save size={14} />}Save asset details
          </button>
        </div>
      </form>
    </Modal>
  );
}

function QualityHistory({
  asset,
  onClose,
  onRun,
}: {
  asset: Asset;
  onClose: () => void;
  onRun?: (id: string) => void;
}) {
  const [offset, setOffset] = useState(0),
    [page, setPage] = useState<Page<AssetQuality> | null>(null),
    [selected, setSelected] = useState<AssetQuality | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    let stopped = false;
    const controller = new AbortController();
    api<Page<AssetQuality>>(
      `/assets/${asset.id}/quality?${new URLSearchParams({ project_id: asset.project_id, offset: String(offset), limit: "10" })}`,
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
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [asset.id, asset.project_id, offset]);
  return (
    <Modal title="Observed quality history" wide onClose={onClose}>
      <p className="v5-dataset-name">{asset.dataset}</p>
      <p className="muted v5-note">
        Recorded reports from the latest 500 project runs, bounded to 1,000
        matching reports. Opening this view does not run checks.
      </p>
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      {page ? (
        <>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Observed</th>
                  <th>Quality</th>
                  <th>Run</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((item) => (
                  <tr key={item.id}>
                    <td>{date(item.observed_at)}</td>
                    <td>
                      <Status
                        status={
                          item.passed
                            ? "passed"
                            : item.passed === false
                              ? "failed"
                              : "unknown"
                        }
                      />
                    </td>
                    <td>{item.run_status}</td>
                    <td>
                      <button
                        className="text-button"
                        onClick={() => setSelected(item)}
                      >
                        Inspect checks
                      </button>
                      {onRun ? (
                        <button
                          className="text-button"
                          onClick={() => onRun(item.run_id)}
                        >
                          Open run
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!page.total ? (
            <p className="v5-note">
              No quality reports are recorded in this evidence window.
            </p>
          ) : null}
          <Pagination
            total={page.total}
            offset={offset}
            limit={10}
            onPage={setOffset}
          />
        </>
      ) : !error ? (
        <Spinner />
      ) : null}
      {selected ? <QualityChecks report={selected} /> : null}
    </Modal>
  );
}

function QualityChecks({ report }: { report: NonNullable<Asset["quality"]> }) {
  return (
    <div className="v5-quality-checks">
      <div className="v5-subhead">
        <h3>Recorded checks</h3>
        <small>{date(report.observed_at)}</small>
      </div>
      <div className="table-scroll">
        <table className="result-table">
          <thead>
            <tr>
              <th>Check</th>
              <th>Result</th>
              <th>Failed rows</th>
              <th>Observed value</th>
            </tr>
          </thead>
          <tbody>
            {report.checks.map((check, index) => (
              <tr key={index}>
                <td>{check.name || check.type}</td>
                <td>
                  {check.passed
                    ? "Passed"
                    : check.passed === false
                      ? "Failed"
                      : "Unknown"}
                </td>
                <td>{check.failed_rows ?? "—"}</td>
                <td>
                  {check.actual == null
                    ? "—"
                    : typeof check.actual === "object"
                      ? JSON.stringify(check.actual)
                      : String(check.actual)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function AssetsPanel({
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
    [page, setPage] = useState<Page<Asset> | null>(null),
    [selected, setSelected] = useState<Asset | null>(null);
  const [focus, setFocus] = useState(""),
    [direction, setDirection] = useState("both"),
    [depth, setDepth] = useState(1),
    [graph, setGraph] = useState<AssetGraph | null>(null),
    [loading, setLoading] = useState(false),
    [graphLoading, setGraphLoading] = useState(false),
    [error, setError] = useState(""),
    [graphError, setGraphError] = useState(""),
    [revision, setRevision] = useState(0),
    [editing, setEditing] = useState(false),
    [history, setHistory] = useState(false);
  const role = projects.find((project) => project.id === projectId)?.role;
  const canEdit = role === "owner" || role === "editor";
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
        api<Page<Asset>>(
          `/assets?${new URLSearchParams({ project_id: projectId, search, offset: String(offset), limit: "20" })}`,
          { signal: controller.signal },
        )
          .then((value) => {
            if (!stopped) {
              setPage(value);
              setError("");
              setSelected((current) =>
                current?.project_id === projectId
                  ? value.items.find((item) => item.id === current.id) ||
                    current
                  : value.items[0] || null,
              );
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
    let stopped = false;
    const controller = new AbortController();
    setGraphLoading(true);
    setGraphError("");
    api<AssetGraph>(
      `/assets/graph?${new URLSearchParams({ project_id: projectId, dataset: focus, direction, depth: String(depth), max_nodes: "60" })}`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!stopped) {
          setGraph(value);
          setSelected((current) =>
            current
              ? value.nodes.find((node) => node.id === current.id) || current
              : current,
          );
        }
      })
      .catch((error) => {
        if (!stopped) setGraphError(message(error));
      })
      .finally(() => {
        if (!stopped) setGraphLoading(false);
      });
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [projectId, focus, direction, depth, revision]);
  function project(value: string) {
    setProjectId(value);
    setOffset(0);
    setSearch("");
    setSelected(null);
    setPage(null);
    setGraph(null);
    setFocus("");
    setEditing(false);
    setHistory(false);
  }
  function focusAsset(value: Asset) {
    setSelected(value);
    setFocus(value.dataset);
  }
  return (
    <div className="v5-data-page">
      <SectionHead
        eyebrow="Follow the data"
        title="Data assets"
        description="Owners, observed health, and dependencies built from recorded dataset activity."
        actions={
          <>
            <ProjectSelect
              projects={projects}
              value={projectId}
              onChange={project}
            />
            <button
              className="button"
              onClick={() => setRevision((value) => value + 1)}
            >
              <RefreshCw size={14} />
              Refresh evidence
            </button>
          </>
        }
      />
      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}
      <div className="v5-asset-layout">
        <section className="card v5-assets-list">
          <div className="v5-list-head">
            <Field label="Find data asset">
              <span className="v5-search">
                <Search size={14} />
                <input
                  value={search}
                  placeholder="Dataset name or location"
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setOffset(0);
                  }}
                />
              </span>
            </Field>
            {loading ? <Spinner /> : null}
          </div>
          {page?.items.map((asset) => (
            <button
              key={asset.id}
              className={`v5-asset-choice ${selected?.id === asset.id ? "selected" : ""}`}
              onClick={() => focusAsset(asset)}
            >
              <Database size={17} />
              <span>
                <strong>{asset.dataset}</strong>
                <small>{asset.owner?.username || "Unassigned owner"}</small>
              </span>
              <Health value={asset.health} />
            </button>
          ))}
          {page && !page.total ? (
            <Empty icon={<Database size={25} />} title="No observed assets">
              Run notebooks with data.read, data.write, or data.lineage to
              record datasets.
            </Empty>
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
        </section>
        <section className="card v5-graph-panel">
          <div className="v5-graph-head">
            <h2>
              <GitBranch size={17} />
              Dataset dependencies
            </h2>
            <div className="v5-graph-controls">
              <Field label="Direction">
                <select
                  value={direction}
                  onChange={(event) => setDirection(event.target.value)}
                >
                  <option value="both">Upstream and downstream</option>
                  <option value="upstream">Upstream</option>
                  <option value="downstream">Downstream</option>
                </select>
              </Field>
              <Field label="Depth">
                <select
                  value={depth}
                  onChange={(event) => setDepth(Number(event.target.value))}
                >
                  <option value={1}>One hop</option>
                  <option value={2}>Two hops</option>
                </select>
              </Field>
              {focus ? (
                <button className="button" onClick={() => setFocus("")}>
                  Show project graph
                </button>
              ) : null}
            </div>
          </div>
          {focus ? (
            <p className="v5-focus-label">
              Focused on <code>{focus}</code>
            </p>
          ) : null}
          {graphError ? (
            <div className="inline-error" role="alert">
              {graphError}
            </div>
          ) : null}
          {graphLoading ? (
            <div className="v5-loading" role="status">
              <Spinner />
              Loading observed dependencies…
            </div>
          ) : graph?.nodes.length ? (
            <LineageGraph
              graph={graph}
              focus={focus}
              selected={selected?.id}
              onFocus={focusAsset}
              onRun={onRun}
            />
          ) : !graphError ? (
            <Empty
              icon={<GitBranch size={25} />}
              title="No dependency observations"
            >
              Read/write events from notebook runs will build this graph.
            </Empty>
          ) : null}
        </section>
      </div>
      {selected ? (
        <section className="card v5-asset-detail">
          <div className="v5-asset-title">
            <div>
              <div className="eyebrow">Data asset</div>
              <h2>{selected.dataset}</h2>
              <p>
                {selected.description ||
                  "Add a description to help your team understand this dataset."}
              </p>
            </div>
            <div className="head-actions">
              <Health value={selected.health} />
              {canEdit ? (
                <button className="button" onClick={() => setEditing(true)}>
                  <Pencil size={14} />
                  Edit asset details
                </button>
              ) : null}
            </div>
          </div>
          <div className="v5-asset-facts">
            <div>
              <span>
                <UserRound size={14} />
                Owner
              </span>
              <strong>{selected.owner?.username || "Unassigned"}</strong>
            </div>
            <div>
              <span>Successful write age</span>
              <strong>{age(selected.freshness.age_seconds)}</strong>
              <small>
                {selected.freshness.threshold_seconds
                  ? `Threshold ${age(selected.freshness.threshold_seconds)}`
                  : "No freshness threshold"}
              </small>
            </div>
            <div>
              <span>Latest observed write</span>
              <strong>{date(selected.last_write_at || undefined)}</strong>
              <small>Run {selected.last_writer_status || "unknown"}</small>
            </div>
            <div>
              <span>Quality last observed</span>
              <strong>{date(selected.quality?.observed_at)}</strong>
              <small>
                {selected.quality?.row_count == null
                  ? "No row count recorded"
                  : `${selected.quality.row_count.toLocaleString()} rows in that report`}
              </small>
            </div>
          </div>
          <div className="info-strip">
            Health reflects recorded evidence, not a live table scan. Freshness
            uses the last recorded successful write; failed-run events remain
            observations.
          </div>
          <div className="v5-subhead">
            <h3>Quality evidence</h3>
            <div className="head-actions">
              <button className="button" onClick={() => setHistory(true)}>
                View quality history
              </button>
              {selected.quality?.run_id && onRun ? (
                <button
                  className="button"
                  onClick={() => onRun(selected.quality!.run_id)}
                >
                  Open observed run
                </button>
              ) : null}
            </div>
          </div>
          {selected.quality ? (
            <QualityChecks report={selected.quality} />
          ) : (
            <p className="muted v5-note">
              No quality report was recorded for this dataset in the latest 500
              project runs.
            </p>
          )}
        </section>
      ) : null}
      {editing && selected ? (
        <AnnotationEditor
          asset={selected}
          onClose={() => setEditing(false)}
          onSaved={(asset) => {
            setSelected(asset);
            setEditing(false);
            setRevision((value) => value + 1);
          }}
        />
      ) : null}
      {history && selected ? (
        <QualityHistory
          asset={selected}
          onClose={() => setHistory(false)}
          onRun={onRun}
        />
      ) : null}
    </div>
  );
}
