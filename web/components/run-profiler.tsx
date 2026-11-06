"use client";

import { useEffect, useId, useState } from "react";
import { Activity, ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { api, bytes, date, type Page, type Run } from "@/lib/api";
import styles from "./run-profiler.module.css";

type Sample = {
  elapsed_ms: number;
  python_rss_bytes: number;
  jvm_rss_bytes: number;
  other_rss_bytes: number;
  total_rss_bytes: number;
  cpu_percent: number | null;
  container_current_bytes: number | null;
  container_limit_bytes: number | null;
};
type Stage = {
  id: number;
  attempt: number;
  name: string;
  status: string;
  submitted_at_ms?: number | null;
  completed_at_ms?: number | null;
  num_tasks: number;
  completed_tasks: number;
  failed_tasks: number;
  executor_run_ms: number;
  executor_cpu_ns: number;
  shuffle_read_bytes: number;
  shuffle_write_bytes: number;
  memory_spilled_bytes: number;
  disk_spilled_bytes: number;
};
type Profile = {
  version: number;
  queue_wait_ms: number;
  spark_wait_ms: number;
  wait_reason?: string | null;
  resources: {
    status: string;
    samples: Sample[];
    peak: Partial<Record<keyof Sample, number>>;
    truncated: boolean;
    sample_interval_ms: number;
  };
  spark: {
    status: string;
    application_id?: string;
    unavailable_reason?: string;
    truncated: boolean;
    jobs: { id: number; status: string; stage_ids: number[] }[];
    stages: Stage[];
    plans: {
      cell_id: string;
      execution_id?: number;
      plan: string;
      truncated: boolean;
    }[];
  };
};
type Metric = {
  current: number | null;
  previous: number | null;
  delta: number | null;
};
type ProfileResponse = {
  run_id: string;
  status: string;
  duration_ms?: number | null;
  profile: Profile | null;
  comparison: { run_id: string; metrics: Record<string, Metric> } | null;
};
type Series = {
  key: keyof Sample;
  label: string;
  color: string;
  dashed?: boolean;
};
const MEMORY_SERIES: Series[] = [
  { key: "python_rss_bytes", label: "Python", color: "#329d81" },
  { key: "jvm_rss_bytes", label: "Spark JVM", color: "#db654b" },
  { key: "other_rss_bytes", label: "Other processes", color: "#8c78ba" },
  {
    key: "container_current_bytes",
    label: "Whole container",
    color: "#7789a5",
    dashed: true,
  },
];
const CPU_SERIES: Series[] = [
  { key: "cpu_percent", label: "Worker CPU", color: "#7288d5" },
];

function duration(value?: number | null) {
  return value == null
    ? "—"
    : value < 1000
      ? `${Math.round(value)} ms`
      : `${(value / 1000).toFixed(1)} s`;
}

function Timeline({
  samples,
  series,
  cpu = false,
}: {
  samples: Sample[];
  series: Series[];
  cpu?: boolean;
}) {
  const labelId = useId();
  const width = 640,
    height = 155,
    left = 62,
    top = 12,
    bottom = 28;
  const end = Math.max(1, ...samples.map((sample) => sample.elapsed_ms));
  const numericValues = samples.flatMap((sample) =>
    series
      .map(({ key }) => sample[key])
      .filter(
        (value): value is number =>
          typeof value === "number" && Number.isFinite(value),
      ),
  );
  const maximum = Math.max(cpu ? 100 : 1, ...numericValues);
  const chartHeight = height - top - bottom;
  const valueLabel = (value: number) =>
    cpu ? `${value.toFixed(0)}%` : bytes(Math.round(value));
  return (
    <figure className={styles.chart}>
      <figcaption id={labelId}>
        {cpu ? "CPU over time" : "Memory over time"}
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-labelledby={labelId}
      >
        {[0, 0.5, 1].map((part) => {
          const y = top + chartHeight * (1 - part);
          return (
            <g key={part}>
              <line
                x1={left}
                y1={y}
                x2={width - 10}
                y2={y}
                className={styles.grid}
              />
              <text x={left - 7} y={y + 4} textAnchor="end">
                {valueLabel(maximum * part)}
              </text>
            </g>
          );
        })}
        {series.map(({ key, label, color, dashed }) => {
          const points = samples
            .filter(
              (sample) =>
                typeof sample[key] === "number" && Number.isFinite(sample[key]),
            )
            .map(
              (sample) =>
                `${(left + (sample.elapsed_ms / end) * (width - left - 10)).toFixed(1)},${(top + chartHeight * (1 - (sample[key] as number) / maximum)).toFixed(1)}`,
            )
            .join(" ");
          return (
            <polyline
              key={key}
              points={points}
              fill="none"
              stroke={color}
              strokeWidth="2"
              strokeDasharray={dashed ? "5 4" : undefined}
            >
              <title>{label}</title>
            </polyline>
          );
        })}
        <text x={left} y={height - 6}>
          0 s
        </text>
        <text x={width - 10} y={height - 6} textAnchor="end">
          {(end / 1000).toFixed(1)} s
        </text>
      </svg>
      <div className={styles.legend}>
        {series.map(({ key, label, color }) => (
          <span key={key}>
            <i style={{ background: color }} />
            {label}
          </span>
        ))}
      </div>
    </figure>
  );
}

function ProfilerContent({ run }: { run: Run }) {
  const [result, setResult] = useState<ProfileResponse | null>(null);
  const [error, setError] = useState("");
  const [comparisonId, setComparisonId] = useState("");
  const [history, setHistory] = useState<Page<Run> | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const active = run.status === "running" || run.status === "queued";

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const suffix = comparisonId
          ? `?compare_run_id=${encodeURIComponent(comparisonId)}`
          : "";
        const value = await api<ProfileResponse>(
          `/runs/${encodeURIComponent(run.id)}/profile${suffix}`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted) {
          setResult(value);
          setError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error
              ? cause.message
              : "Run profile is unavailable.",
          );
      } finally {
        if (active && !controller.signal.aborted)
          timer = setTimeout(load, 2000);
      }
    }
    void load();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [run.id, active, comparisonId, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    void api<Page<Run>>(
      `/history/runs?notebook_id=${encodeURIComponent(run.notebook_id)}&limit=25&offset=${offset}`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted) {
          setHistory(value);
          setHistoryError("");
        }
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted)
          setHistoryError(
            cause instanceof Error
              ? cause.message
              : "Run history is unavailable.",
          );
      });
    return () => controller.abort();
  }, [run.notebook_id, offset, refresh]);

  const profile = result?.profile;
  const samples = profile?.resources.samples ?? [];
  const alternatives =
    history?.items.filter((item) => item.id !== run.id) ?? [];
  return (
    <div className={styles.content}>
      <div className={styles.toolbar}>
        <label>
          Compare with
          <select
            aria-label="Compare run profile"
            value={comparisonId}
            onChange={(event) => setComparisonId(event.target.value)}
          >
            <option value="">No comparison</option>
            {comparisonId &&
            !alternatives.some((item) => item.id === comparisonId) ? (
              <option value={comparisonId}>
                Selected run · {comparisonId.slice(0, 10)}
              </option>
            ) : null}
            {alternatives.map((item) => (
              <option key={item.id} value={item.id}>
                {date(item.created_at)} · {item.status} · {item.id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
        {history && history.total > 25 ? (
          <div className={styles.paging}>
            <button
              className="icon-button"
              disabled={offset === 0}
              aria-label="Newer comparison runs"
              onClick={() => setOffset(Math.max(0, offset - 25))}
            >
              <ChevronLeft size={15} />
            </button>
            <small>
              {offset + 1}–{Math.min(offset + 25, history.total)} /{" "}
              {history.total}
            </small>
            <button
              className="icon-button"
              disabled={offset + 25 >= history.total}
              aria-label="Older comparison runs"
              onClick={() => setOffset(offset + 25)}
            >
              <ChevronRight size={15} />
            </button>
          </div>
        ) : null}
        <button
          className="button"
          onClick={() => setRefresh((value) => value + 1)}
        >
          <RefreshCw size={13} />
          Refresh
        </button>
      </div>
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      {historyError ? (
        <p className="form-note" role="status">
          Comparison history: {historyError}
        </p>
      ) : null}
      {!result && !error ? (
        <p className="form-note" role="status">
          Loading run diagnostics…
        </p>
      ) : null}
      {result && !profile ? (
        <p className="form-note">
          No profile was captured for this run. Profiles are available for runs
          started on a compatible runtime; retention can also expire older
          diagnostics.
        </p>
      ) : null}
      {profile ? (
        <>
          <div className={styles.metrics}>
            <span>
              Duration<strong>{duration(result?.duration_ms)}</strong>
            </span>
            <span>
              Queued<strong>{duration(profile.queue_wait_ms)}</strong>
            </span>
            <span>
              Catalog wait<strong>{duration(profile.spark_wait_ms)}</strong>
            </span>
            <span>
              Peak worker memory
              <strong>{bytes(profile.resources.peak.total_rss_bytes)}</strong>
            </span>
          </div>
          {profile.wait_reason ? (
            <p className="form-note">{profile.wait_reason}</p>
          ) : null}
          {result?.comparison ? (
            <div className={styles.comparison} aria-label="Run comparison">
              {Object.entries(result.comparison.metrics).map(
                ([key, metric]) => (
                  <span key={key}>
                    {key.replaceAll("_", " ")}
                    <strong>
                      {key.includes("memory")
                        ? bytes(metric.current ?? undefined)
                        : duration(metric.current)}{" "}
                      <small>
                        vs{" "}
                        {key.includes("memory")
                          ? bytes(metric.previous ?? undefined)
                          : duration(metric.previous)}{" "}
                        ·{" "}
                        {metric.delta == null
                          ? "unavailable"
                          : `${metric.delta > 0 ? "+" : metric.delta < 0 ? "−" : ""}${key.includes("memory") ? bytes(Math.abs(metric.delta)) : duration(Math.abs(metric.delta))}`}
                      </small>
                    </strong>
                  </span>
                ),
              )}
            </div>
          ) : null}
          {samples.length ? (
            <div className={styles.charts}>
              <Timeline samples={samples} series={MEMORY_SERIES} />
              <Timeline samples={samples} series={CPU_SERIES} cpu />
            </div>
          ) : (
            <p className="form-note">
              Memory and CPU samples are not available yet.
            </p>
          )}
          <p className={styles.note}>
            Worker memory covers this interpreter and its child processes.
            Whole-container memory includes other work and filesystem cache on
            this compute service. CPU at 100% represents one fully used core.
            {profile.resources.truncated
              ? " Older points were downsampled; exact sampled peaks are preserved."
              : ""}
          </p>
          <h4>Spark execution</h4>
          {profile.spark.status === "not_started" ? (
            <p className="form-note">
              Spark was not started for this run. Python and DuckDB work can run
              without a JVM.
            </p>
          ) : profile.spark.status === "unavailable" ? (
            <p className="form-note">
              {profile.spark.unavailable_reason ||
                "Spark metrics are unavailable."}{" "}
              Completed resource samples remain available.
            </p>
          ) : (
            <p className="form-note">
              {profile.spark.jobs.length} jobs · {profile.spark.stages.length}{" "}
              stages
              {profile.spark.application_id
                ? ` · ${profile.spark.application_id}`
                : ""}
            </p>
          )}
          {profile.spark.stages.length ? (
            <div className="table-scroll">
              <table className={`data-table ${styles.stages}`}>
                <caption className={styles.srOnly}>
                  Spark stage timings and data movement
                </caption>
                <thead>
                  <tr>
                    <th>Stage</th>
                    <th>Status</th>
                    <th>Duration</th>
                    <th>Tasks</th>
                    <th>Shuffle read</th>
                    <th>Shuffle write</th>
                    <th>Spilled</th>
                    <th>Task CPU</th>
                  </tr>
                </thead>
                <tbody>
                  {profile.spark.stages.map((stage) => (
                    <tr key={`${stage.id}-${stage.attempt}`}>
                      <td title={stage.name}>
                        {stage.id}.{stage.attempt}
                      </td>
                      <td>{stage.status}</td>
                      <td>
                        {duration(
                          stage.submitted_at_ms != null &&
                            stage.completed_at_ms != null
                            ? stage.completed_at_ms - stage.submitted_at_ms
                            : null,
                        )}
                      </td>
                      <td>
                        {stage.completed_tasks}/{stage.num_tasks}
                        {stage.failed_tasks
                          ? ` · ${stage.failed_tasks} failed`
                          : ""}
                      </td>
                      <td>{bytes(stage.shuffle_read_bytes)}</td>
                      <td>{bytes(stage.shuffle_write_bytes)}</td>
                      <td>
                        {bytes(
                          stage.memory_spilled_bytes + stage.disk_spilled_bytes,
                        )}
                      </td>
                      <td>{duration(stage.executor_cpu_ns / 1_000_000)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {profile.spark.truncated ? (
            <p className="form-note">
              This view is bounded to retained jobs, stages, and query plans.
              Large runs may have additional Spark activity.
            </p>
          ) : null}
          {profile.spark.plans.length ? (
            <div className={styles.plans}>
              <h4>Query plans</h4>
              {profile.spark.plans.map((plan, index) => (
                <details key={`${plan.execution_id ?? "display"}-${index}`}>
                  <summary>
                    Cell {plan.cell_id || "unknown"}
                    {plan.execution_id != null
                      ? ` · SQL execution ${plan.execution_id}`
                      : ""}
                    {plan.truncated ? " · truncated" : ""}
                  </summary>
                  <pre>{plan.plan}</pre>
                </details>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

export function RunProfiler({ run }: { run: Run }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className={styles.root}
      data-testid="run-profiler"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <Activity size={14} />
        Run profiler
      </summary>
      {open ? <ProfilerContent key={run.id} run={run} /> : null}
    </details>
  );
}
