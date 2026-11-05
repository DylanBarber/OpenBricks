"use client";
import { Activity, AlertTriangle, FileCode2 } from "lucide-react";
import { bytes, date, type Run } from "@/lib/api";
export function RunActivity({ run }: { run: Run }) {
  const commit = run.provenance?.commit || run.source_commit,
    releaseId = run.provenance?.release_id || run.release_id;
  const completed =
      run.outputs?.filter((output) => output.status === "succeeded").length ||
      0,
    total = run.progress?.total_cells || 0;
  return (
    <section className="run-activity">
      {run.status === "queued" && run.queued_reason ? (
        <p className="form-note">{run.queued_reason}</p>
      ) : null}
      {run.resource_options ? (
        <p className="form-note">
          {run.resource_options.mode === "interactive"
            ? "Interactive worker"
            : "Fresh job worker"}
          {run.resource_options.memory_mb
            ? ` · ${run.resource_options.memory_mb.toLocaleString()} MB budget`
            : ""}
          {run.session_id ? ` · Session ${run.session_id.slice(0, 10)}` : ""}
        </p>
      ) : null}
      {run.cancel_requested ? (
        <p className="form-note">
          Cancellation requested. Waiting for the compute target to confirm that
          execution has stopped.
        </p>
      ) : null}
      <div className="run-metrics">
        <span>
          <Activity size={14} />
          Progress
          <strong>
            {total
              ? `${completed} / ${total} cells`
              : run.status.replaceAll("_", " ")}
          </strong>
        </span>
        <span>
          Peak interpreter memory<strong>{bytes(run.peak_memory_bytes)}</strong>
        </span>
        <span>
          Current cell
          <strong>
            {run.progress?.cell_index != null
              ? run.progress.cell_index + 1
              : "—"}
          </strong>
        </span>
      </div>
      {total ? (
        <progress
          max={total}
          value={completed}
          aria-label="Completed notebook cells"
        />
      ) : null}
      {run.error_class ? (
        <div className="inline-error">
          <AlertTriangle size={14} />
          {run.error_class}: {run.error}
        </div>
      ) : null}
      {commit || releaseId || run.environment_id ? (
        <div className="release-provenance">
          <FileCode2 size={14} />
          {commit ? <code>Commit {commit.slice(0, 12)}</code> : null}
          {releaseId ? <span>Release {releaseId.slice(0, 10)}</span> : null}
          {run.environment_id ? (
            <span>Environment {run.environment_id.slice(0, 10)}</span>
          ) : null}
        </div>
      ) : null}
      {run.logs?.length ? (
        <details open>
          <summary>Live logs · {run.logs.length} entries</summary>
          <div className="live-logs" role="log" aria-label="Run logs">
            {run.logs.map((line, index) => (
              <div
                key={index}
                className={line.stream === "stderr" ? "stderr" : ""}
              >
                <time>{date(line.at)}</time>
                <code>{line.text}</code>
              </div>
            ))}
          </div>
        </details>
      ) : null}
      {run.quality_reports?.length ? (
        <details open>
          <summary>Data quality</summary>
          <pre>{JSON.stringify(run.quality_reports, null, 2)}</pre>
        </details>
      ) : null}
    </section>
  );
}
