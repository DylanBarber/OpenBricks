"use client";

import { useEffect, useState } from "react";
import { Download, Search } from "lucide-react";
import { api, date, type Page, type Project } from "@/lib/api";
import { Empty, Field, Pagination, SectionHead, Spinner } from "./ui";

type AuditEvent = {
  id: string;
  project_id: string;
  actor_id?: string;
  actor_name?: string;
  action: string;
  resource_id?: string;
  created_at: string;
};

export default function AuditPanel({
  projects,
  onError,
}: {
  projects: Project[];
  onError: (message: string) => void;
}) {
  const [project, setProject] = useState("");
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<Page<AuditEvent> | null>(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState({ action: "", actor: "" });
  const owners = projects.filter((item) => item.role === "owner");
  const params = new URLSearchParams({
    limit: "25",
    offset: String(offset),
    ...(project ? { project_id: project } : {}),
    ...(query.action ? { action: query.action } : {}),
    ...(query.actor ? { actor_id: query.actor } : {}),
  });
  const exportParams = new URLSearchParams(params);
  exportParams.delete("offset");
  exportParams.set("limit", "10000");
  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery({ action: action.trim(), actor: actor.trim() });
      setOffset(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [action, actor]);
  useEffect(() => {
    let stopped = false;
    setBusy(true);
    api<Page<AuditEvent>>(
      `/audit?${new URLSearchParams({ limit: "25", offset: String(offset), ...(project ? { project_id: project } : {}), ...(query.action ? { action: query.action } : {}), ...(query.actor ? { actor_id: query.actor } : {}) })}`,
    )
      .then((value) => {
        if (!stopped) setPage(value);
      })
      .catch((value) => {
        if (!stopped) onError(value.message);
      })
      .finally(() => {
        if (!stopped) setBusy(false);
      });
    return () => {
      stopped = true;
    };
  }, [project, query.action, query.actor, offset, onError]);
  return (
    <>
      <SectionHead
        eyebrow="TEAM ACTIVITY"
        title="Audit history"
        description="Inspect project actions, account activity, and changes to notebook and data configuration."
        actions={
          <a className="button" href={`/api/audit/export?${exportParams}`}>
            <Download size={15} />
            Export CSV
          </a>
        }
      />
      <div
        className="filter-bar"
        style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 20 }}
      >
        <Field label="Project">
          <select
            value={project}
            onChange={(event) => {
              setProject(event.target.value);
              setOffset(0);
            }}
          >
            <option value="">All owned projects</option>
            {owners.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Action"
          hint="Exact action, such as notebook.run or secret.rotate"
        >
          <input
            value={action}
            maxLength={120}
            onChange={(event) => setAction(event.target.value)}
            placeholder="All actions"
          />
        </Field>
        <Field label="Actor account ID">
          <input
            value={actor}
            maxLength={80}
            onChange={(event) => setActor(event.target.value)}
            placeholder="All actors"
          />
        </Field>
      </div>
      {busy ? <Spinner /> : null}
      {!busy && page?.items.length === 0 ? (
        <Empty icon={<Search size={26} />} title="No matching actions">
          Choose another project or clear the filters.
        </Empty>
      ) : null}
      {page?.items.length ? (
        <div className="table-scroll">
          <table className="result-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Project</th>
                <th>Resource</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr key={item.id}>
                  <td>{date(item.created_at)}</td>
                  <td>
                    <strong>{item.actor_name || "Deleted account"}</strong>
                    <small style={{ display: "block" }}>{item.actor_id}</small>
                  </td>
                  <td>
                    <code>{item.action}</code>
                  </td>
                  <td>
                    {projects.find((project) => project.id === item.project_id)
                      ?.name || item.project_id}
                  </td>
                  <td>
                    <code>{item.resource_id || "—"}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <Pagination
        total={page?.total || 0}
        limit={25}
        offset={offset}
        onPage={setOffset}
        busy={busy}
      />
      <p className="form-note">
        Audit records follow the configured run-history retention. Exports
        contain up to 10,000 matching events; refine filters to export a smaller
        set.
      </p>
    </>
  );
}
