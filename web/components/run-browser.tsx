"use client";

import { useEffect, useState } from "react";
import {
  Activity,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  FileCode2,
  RefreshCw,
  Search,
} from "lucide-react";
import {
  api,
  date,
  type NotebookHistory,
  type Page,
  type Run,
} from "@/lib/api";
import { Empty, Pagination, SectionHead, Spinner, Status } from "./ui";

export function RunBrowser({
  projectId,
  onSelect,
  onOpen,
  onError,
}: {
  projectId: string;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
  onError: (message: string) => void;
}) {
  const [selected, setSelected] = useState<NotebookHistory | null>(null);
  const [groups, setGroups] = useState<Page<NotebookHistory> | null>(null);
  const [runs, setRuns] = useState<Page<Run> | null>(null);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    setSelected(null);
    setGroups(null);
    setRuns(null);
    setStatus("");
    setSearch("");
    setOffset(0);
  }, [projectId]);
  useEffect(() => {
    setOffset(0);
  }, [selected?.notebook_id, search, status]);
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const timer = setTimeout(
      () => {
        setLoading(true);
        const query = new URLSearchParams({
          project_id: projectId,
          offset: String(offset),
          limit: "25",
        });
        if (selected) {
          query.set("notebook_id", selected.notebook_id);
          if (status) query.set("status", status);
        } else query.set("search", search);
        const request = selected
          ? api<Page<Run>>(`/history/runs?${query}`, {
              signal: controller.signal,
            }).then((result) => {
              if (!cancelled) setRuns(result);
            })
          : api<Page<NotebookHistory>>(`/history/notebooks?${query}`, {
              signal: controller.signal,
            }).then((result) => {
              if (!cancelled) setGroups(result);
            });
        request
          .catch((error) => {
            if (!cancelled) onError(error.message);
          })
          .finally(() => {
            if (!cancelled) setLoading(false);
          });
      },
      search && !selected ? 250 : 0,
    );
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [selected, search, status, offset, version, projectId, onError]);
  useEffect(() => {
    const timer = setInterval(() => setVersion((value) => value + 1), 15000);
    return () => clearInterval(timer);
  }, []);
  function pick(value: NotebookHistory | null) {
    setSelected(value);
    setOffset(0);
    setStatus("");
    setRuns(null);
  }
  return (
    <>
      <SectionHead
        eyebrow="EXPLORATION TO AUTOMATION"
        title={selected ? selected.notebook_name : "Run history"}
        description={
          selected
            ? `${selected.folder_path || "/Workspace"} · Execution history for this notebook.`
            : "Start with a notebook, then explore its runs. History loads one page at a time."
        }
        actions={
          <>
            {selected ? (
              <button
                className="button"
                disabled={selected.deleted}
                title={
                  selected.deleted
                    ? "The notebook was deleted; its history is retained."
                    : undefined
                }
                onClick={() => onOpen(selected.notebook_id)}
              >
                <BookOpen size={14} />
                Open notebook
              </button>
            ) : null}
            <button
              className="button"
              onClick={() => setVersion((value) => value + 1)}
              disabled={loading}
            >
              <RefreshCw size={14} />
              Refresh
            </button>
          </>
        }
      />
      {selected ? (
        <button className="text-button history-back" onClick={() => pick(null)}>
          <ArrowLeft size={14} />
          All notebooks
        </button>
      ) : null}
      <section className="card run-browser">
        <div className="browser-toolbar">
          {selected ? (
            <label className="filter-field">
              Status
              <select
                aria-label="Filter runs by status"
                value={status}
                onChange={(event) => setStatus(event.target.value)}
              >
                <option value="">All statuses</option>
                <option value="succeeded">Succeeded</option>
                <option value="failed">Failed</option>
                <option value="cancelled">Cancelled</option>
                <option value="running">Running</option>
                <option value="queued">Queued</option>
              </select>
            </label>
          ) : (
            <label className="list-search">
              <Search size={15} />
              <input
                aria-label="Search run history by notebook"
                placeholder="Find a notebook…"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
          )}
          <span className="subtle-tag">
            {selected
              ? `${(runs?.total || 0).toLocaleString()} runs`
              : `${(groups?.total || 0).toLocaleString()} notebooks`}
          </span>
          {loading ? <Spinner /> : null}
        </div>
        {selected ? (
          runs?.items.length ? (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Status</th>
                    <th>Started</th>
                    <th>Trigger</th>
                    <th>Compute</th>
                    <th className="right">Details</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.items.map((run) => (
                    <tr key={run.id}>
                      <td>
                        <button
                          className="name-link"
                          onClick={() => onSelect(run.id)}
                        >
                          <code>{run.id.slice(0, 10)}</code>
                        </button>
                      </td>
                      <td>
                        <Status status={run.status} />
                      </td>
                      <td className="muted">
                        {date(run.started_at || run.created_at)}
                      </td>
                      <td>
                        <span className="subtle-tag">
                          {run.trigger || "manual"}
                        </span>
                      </td>
                      <td className="muted">
                        {run.cluster_name || "Default compute"}
                      </td>
                      <td className="right">
                        <button
                          className="icon-button"
                          aria-label={`View run ${run.id.slice(0, 8)}`}
                          onClick={() => onSelect(run.id)}
                        >
                          <ArrowRight size={15} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : !loading ? (
            <Empty
              title="No runs match this filter"
              icon={<Activity size={27} />}
            >
              Try another status, or run this notebook to record its first
              result.
            </Empty>
          ) : (
            <div className="panel-loading">
              <Spinner />
              Loading runs…
            </div>
          )
        ) : groups?.items.length ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Notebook</th>
                  <th>Last run</th>
                  <th>Total runs</th>
                  <th>Succeeded</th>
                  <th>Failed</th>
                  <th className="right">History</th>
                </tr>
              </thead>
              <tbody>
                {groups.items.map((group) => (
                  <tr key={group.notebook_id}>
                    <td>
                      <button className="name-link" onClick={() => pick(group)}>
                        <span className="file-icon">
                          <FileCode2 size={17} />
                        </span>
                        <span>
                          {group.notebook_name}
                          <small>
                            {group.deleted
                              ? "Deleted notebook · History retained"
                              : group.folder_path || "/Workspace"}
                          </small>
                        </span>
                      </button>
                    </td>
                    <td>
                      <Status status={group.last_status} />
                      <small className="table-subline">
                        {date(group.last_run_at)}
                      </small>
                    </td>
                    <td>
                      <strong>{group.total_runs.toLocaleString()}</strong>
                    </td>
                    <td>
                      <span className="success-text">
                        {group.succeeded.toLocaleString()}
                      </span>
                    </td>
                    <td>
                      <span className={group.failed ? "failure-text" : "muted"}>
                        {group.failed.toLocaleString()}
                      </span>
                    </td>
                    <td className="right">
                      <button
                        className="button compact"
                        onClick={() => pick(group)}
                      >
                        View runs
                        <ArrowRight size={13} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : !loading ? (
          <Empty
            title={
              search ? "No matching notebooks" : "Every notebook has a story"
            }
            icon={<Activity size={28} />}
          >
            {search
              ? "Search by notebook name to find its history."
              : "Run a notebook to start recording results here."}
          </Empty>
        ) : (
          <div className="panel-loading">
            <Spinner />
            Loading notebook history…
          </div>
        )}
        <Pagination
          total={(selected ? runs : groups)?.total || 0}
          offset={offset}
          limit={25}
          onPage={setOffset}
          busy={loading}
        />
      </section>
      <div className="info-strip">
        <Activity size={17} />
        <span>
          Runs are grouped by notebook. Full logs and cell results load only
          when you open an individual run.
        </span>
      </div>
    </>
  );
}
