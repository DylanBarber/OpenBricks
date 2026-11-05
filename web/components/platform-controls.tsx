"use client";
import { useEffect, useState } from "react";
import {
  api,
  type NotebookSummary,
  type Page,
  type Project,
  type QueryResult,
} from "@/lib/api";
import { Field, Pagination, Spinner } from "./ui";
export function ProjectSelect({
  projects,
  value,
  onChange,
}: {
  projects: Project[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="project-select">
      <span>Project</span>
      <select
        aria-label="Current project"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {projects.map((project) => (
          <option key={project.id} value={project.id}>
            {project.name}
          </option>
        ))}
      </select>
    </label>
  );
}
export function NotebookPicker({
  value,
  onChange,
  projectId = "default",
}: {
  value: string;
  onChange: (value: string) => void;
  projectId?: string;
}) {
  const [page, setPage] = useState<Page<NotebookSummary> | null>(null),
    [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0),
    [error, setError] = useState("");
  useEffect(() => {
    setOffset(0);
    setSearch("");
    setPage(null);
  }, [projectId]);
  useEffect(() => {
    let stopped = false;
    const timer = setTimeout(
      () =>
        api<Page<NotebookSummary>>(
          `/workspace?${new URLSearchParams({ all_folders: "true", project_id: projectId, search, limit: "20", offset: String(offset) })}`,
        )
          .then((result) => {
            if (!stopped) {
              setPage(result);
              setError("");
            }
          })
          .catch((error) => {
            if (!stopped) setError(error.message);
          }),
      search ? 200 : 0,
    );
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [projectId, search, offset]);
  return (
    <div className="notebook-picker">
      <Field label="Find notebook">
        <input
          value={search}
          placeholder="Search project notebooks…"
          onChange={(event) => {
            setSearch(event.target.value);
            setOffset(0);
          }}
        />
      </Field>
      <select
        aria-label="Select notebook"
        required
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Choose notebook</option>
        {value && !page?.items.some((item) => item.id === value) ? (
          <option value={value}>Selected notebook · {value.slice(0, 8)}</option>
        ) : null}
        {page?.items.map((item) => (
          <option
            key={item.id}
            value={item.id}
          >{`${(item.folder_path || "").replace(/\/+$/, "")}/${item.name}`}</option>
        ))}
      </select>
      {error ? <div className="inline-error">{error}</div> : null}
      <Pagination
        total={page?.total || 0}
        limit={20}
        offset={offset}
        onPage={setOffset}
      />
    </div>
  );
}
export function ResultTable({ result }: { result?: QueryResult }) {
  return result?.columns ? (
    <div className="table-scroll">
      <table className="result-table">
        <thead>
          <tr>
            {result.columns.map((column, index) => (
              <th key={index}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {result.rows?.map((row, index) => (
            <tr key={index}>
              {row.map((value, index) => (
                <td key={index}>
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
      <div className="output-count">
        {result.rows?.length || 0} rows
        {result.truncated ? " · Results truncated" : ""}
      </div>
    </div>
  ) : null;
}
export function LoadingPanel() {
  return (
    <div className="panel-loading">
      <Spinner />
      Loading…
    </div>
  );
}
