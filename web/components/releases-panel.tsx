"use client";
import { useEffect, useState, type FormEvent } from "react";
import {
  Box,
  GitBranch,
  GitCompareArrows,
  Plus,
  RefreshCw,
} from "lucide-react";
import {
  api,
  date,
  json,
  type Cluster,
  type Connection,
  type Repository,
} from "@/lib/api";
import type { Environment, Release } from "@/lib/platform";
import { ParameterValues } from "./parameter-fields";
import { Empty, Field, Modal, SectionHead, Spinner } from "./ui";
type Comparison = {
  added: string[];
  removed: string[];
  changed: string[];
  environment_changed: boolean;
};
export default function ReleasesPanel({
  role,
  projectId,
  repositories,
  connections,
  clusters,
  onRun,
  onError,
}: {
  role: string;
  clusters: Cluster[];
  onRun: (id: string) => void;
  projectId: string;
  repositories: Repository[];
  connections: Connection[];
  onError: (message: string) => void;
}) {
  const [runRelease, setRunRelease] = useState<Release | null>(null),
    [runPath, setRunPath] = useState(""),
    [runCluster, setRunCluster] = useState(""),
    [parameters, setParameters] = useState<Record<string, unknown>>({});
  const [environments, setEnvironments] = useState<Environment[]>([]),
    [releases, setReleases] = useState<Release[]>([]),
    [version, setVersion] = useState(0),
    [busy, setBusy] = useState(false),
    [modal, setModal] = useState<"environment" | "release" | null>(null),
    [repositoryId, setRepositoryId] = useState(repositories[0]?.id || ""),
    [selected, setSelected] = useState<Release | null>(null),
    [otherId, setOtherId] = useState(""),
    [comparison, setComparison] = useState<Comparison | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    let stopped = false;
    Promise.all([
      api<Environment[]>(`/environments?project_id=${projectId}`),
      api<Release[]>(`/releases?project_id=${projectId}`),
    ])
      .then(([environments, releases]) => {
        if (!stopped) {
          setEnvironments(environments);
          setReleases(releases);
        }
      })
      .catch((error) => {
        if (!stopped) onError(error.message);
      });
    return () => {
      stopped = true;
    };
  }, [projectId, version, onError]);
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
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void perform(async () => {
      if (modal === "environment")
        await api(
          "/environments",
          json("POST", { project_id: projectId, name: form.get("name") }),
        );
      else
        await api(
          "/releases",
          json("POST", {
            project_id: projectId,
            name: form.get("name"),
            repository_id: repositoryId,
            commit: form.get("commit"),
            environment_id: form.get("environment_id") || null,
            paths: String(form.get("paths") || "").trim()
              ? String(form.get("paths"))
                  .split("\n")
                  .map((value) => value.trim())
                  .filter(Boolean)
              : undefined,
            connection_ids: form.getAll("connection_ids"),
            parameter_definitions: JSON.parse(
              String(form.get("parameter_definitions") || "{}"),
            ),
          }),
        );
      setModal(null);
    });
  }
  return (
    <>
      <SectionHead
        eyebrow="MAKE EVERY RUN REPRODUCIBLE"
        title="Releases & environments"
        description="Capture Git sources and a pinned wheel manifest, compare releases, and select them in workflow steps."
        actions={
          <>
            <button
              className="button"
              disabled={!["editor", "owner"].includes(role)}
              onClick={() => setModal("environment")}
            >
              <Box size={14} />
              Capture environment
            </button>
            <button
              className="button primary"
              disabled={
                !repositories.length || !["editor", "owner"].includes(role)
              }
              onClick={() => {
                setModal("release");
                setRepositoryId(repositories[0]?.id || "");
              }}
            >
              <Plus size={14} />
              Create release
            </button>
          </>
        }
      />
      {error ? <div className="inline-error">{error}</div> : null}
      <section className="card">
        <div className="card-heading">
          <h3>Immutable Git releases</h3>
          <button
            className="icon-button"
            aria-label="Refresh releases"
            onClick={() => setVersion((value) => value + 1)}
          >
            <RefreshCw size={15} />
          </button>
        </div>
        {releases.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Release</th>
                <th>Source commit</th>
                <th>Package environment</th>
                <th>Created</th>
                <th>Run</th>
              </tr>
            </thead>
            <tbody>
              {releases.map((release) => (
                <tr key={release.id}>
                  <td>
                    <button
                      className="name-link"
                      onClick={() =>
                        void perform(async () => {
                          setSelected(
                            await api(
                              `/releases/${release.id}?project_id=${projectId}`,
                            ),
                          );
                          setComparison(null);
                          setOtherId("");
                        })
                      }
                    >
                      <GitBranch size={15} />
                      {release.name}
                    </button>
                  </td>
                  <td>
                    <code>{release.commit?.slice(0, 12)}</code>
                  </td>
                  <td>
                    {environments.find(
                      (item) => item.id === release.environment_id,
                    )?.name ||
                      release.environment_id?.slice(0, 10) ||
                      "Captured at creation"}
                  </td>
                  <td>{date(release.created_at)}</td>
                  <td>
                    <button
                      className="button compact"
                      disabled={role === "viewer"}
                      onClick={() =>
                        void perform(async () => {
                          const item = await api<Release>(
                            `/releases/${release.id}`,
                          );
                          setRunRelease(item);
                          setRunPath(item.notebooks?.[0]?.path || "");
                          setParameters({});
                          setRunCluster("");
                        })
                      }
                    >
                      Run release
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty title="Capture your first release">
            A release freezes the selected Git notebook sources so later edits
            do not change a scheduled pipeline.
          </Empty>
        )}
      </section>
      <section className="card">
        <div className="card-heading">
          <h3>Pinned wheel environments</h3>
        </div>
        {environments.map((environment) => (
          <details className="environment-record" key={environment.id}>
            <summary>
              <Box size={15} />
              <strong>{environment.name}</strong>
              <span>{environment.wheels?.length || 0} wheels</span>
              <code>{environment.id.slice(0, 12)}</code>
            </summary>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Wheel artifact</th>
                  <th>SHA-256</th>
                </tr>
              </thead>
              <tbody>
                {environment.wheels?.map((wheel, index) => (
                  <tr key={index}>
                    <td>{wheel.filename || wheel.name}</td>
                    <td>
                      <code className="break-code">{wheel.sha256}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!environment.wheels?.length ? (
              <p className="form-note padded">
                This environment captures an empty installed-wheel set.
              </p>
            ) : null}
            <div className="padded form-note">
              Runtime identity:{" "}
              <code>{JSON.stringify(environment.runtime)}</code>
            </div>
          </details>
        ))}
        {!environments.length ? (
          <Empty title="Keep package versions together">
            Capture the currently installed wheels before a production run.
          </Empty>
        ) : null}
      </section>
      <div className="info-strip">
        <GitBranch size={16} />
        <span>
          Releases are immutable. A workflow repair or captured-source rerun
          uses the original sources and environment, rather than the latest
          workspace edits.
        </span>
      </div>
      {modal ? (
        <Modal
          title={
            modal === "environment"
              ? "Capture package environment"
              : "Create Git release"
          }
          onClose={() => {
            if (!busy) setModal(null);
          }}
        >
          <form className="modal-form" onSubmit={submit}>
            <Field label="Name">
              <input
                name="name"
                required
                autoFocus
                placeholder={
                  modal === "environment"
                    ? "Validated packages · November"
                    : "Orders pipeline v1"
                }
              />
            </Field>
            {modal === "release" ? (
              <>
                <Field label="Repository">
                  <select
                    value={repositoryId}
                    onChange={(event) => setRepositoryId(event.target.value)}
                  >
                    {repositories.map((repository) => (
                      <option key={repository.id} value={repository.id}>
                        {repository.name} · {repository.branch}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field
                  label="Exact Git commit"
                  hint="Use the full 40-character commit hash; a branch name can change."
                >
                  <input
                    name="commit"
                    key={repositoryId}
                    required
                    pattern="[a-fA-F0-9]{40}"
                    defaultValue={String(
                      (
                        repositories.find(
                          (repository) => repository.id === repositoryId,
                        ) as Repository & { commit_full?: string }
                      )?.commit_full || "",
                    )}
                    placeholder="40-character SHA"
                  />
                </Field>
                <Field
                  label="Notebook paths (one per line)"
                  hint="Leave empty to capture all supported notebooks at this commit."
                >
                  <textarea
                    name="paths"
                    rows={4}
                    placeholder="notebooks/00_run_pipeline.py"
                  />
                </Field>
                <Field label="Pinned environment">
                  <select name="environment_id">
                    <option value="">Capture current wheels</option>
                    {environments.map((environment) => (
                      <option key={environment.id} value={environment.id}>
                        {environment.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Parameter definitions by notebook path (JSON, optional)">
                  <textarea
                    name="parameter_definitions"
                    rows={3}
                    defaultValue="{}"
                    placeholder='{"notebooks/report.py":[{"name":"date","type":"date","required":true}]}'
                  />
                </Field>
                <Field label="Allowed saved connections">
                  <div className="connection-checklist">
                    {connections.map((connection) => (
                      <label className="checkbox-field" key={connection.id}>
                        <input
                          type="checkbox"
                          name="connection_ids"
                          value={connection.id}
                        />
                        {connection.name}
                      </label>
                    ))}
                  </div>
                </Field>
              </>
            ) : (
              <p className="form-note">
                This captures immutable hashes of the currently installed wheel
                artifacts. It does not copy arbitrary files from the compute
                host.
              </p>
            )}
            {error ? <div className="inline-error">{error}</div> : null}
            <div className="modal-footer">
              <button className="button primary" disabled={busy}>
                {busy ? <Spinner /> : <Plus size={14} />}Create {modal}
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
      {runRelease ? (
        <Modal
          title={`Run ${runRelease.name}`}
          onClose={() => setRunRelease(null)}
        >
          <div className="modal-form">
            <Field label="Captured notebook">
              <select
                value={runPath}
                onChange={(event) => {
                  setRunPath(event.target.value);
                  setParameters({});
                }}
              >
                {runRelease.notebooks?.map((item) => (
                  <option key={item.path} value={item.path}>
                    {item.path}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Compute">
              <select
                value={runCluster}
                onChange={(event) => setRunCluster(event.target.value)}
              >
                <option value="">Default compute</option>
                {clusters.map((cluster) => (
                  <option key={cluster.id} value={cluster.id}>
                    {cluster.name}
                  </option>
                ))}
              </select>
            </Field>
            <ParameterValues
              definitions={
                runRelease.notebooks?.find((item) => item.path === runPath)
                  ?.notebook?.parameter_definitions || []
              }
              values={parameters}
              onChange={setParameters}
            />
            <p className="form-note">
              Uses this release’s captured source and pinned package
              environment.
            </p>
            {error ? <div className="inline-error">{error}</div> : null}
            <div className="modal-footer">
              <button
                className="button primary"
                disabled={busy || !runPath}
                onClick={() =>
                  void perform(async () => {
                    const run = await api<{ id: string }>(
                      `/releases/${runRelease.id}/run`,
                      json("POST", {
                        path: runPath,
                        parameters,
                        cluster_id: runCluster || null,
                      }),
                    );
                    setRunRelease(null);
                    onRun(run.id);
                  })
                }
              >
                Run captured source
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
      {selected ? (
        <Modal title={selected.name} wide onClose={() => setSelected(null)}>
          <div className="modal-form">
            <div className="release-provenance">
              <code>{selected.commit}</code>
              <span>
                Environment{" "}
                {selected.environment_id?.slice(0, 12) || "Captured"}
              </span>
            </div>
            <h3>Captured notebooks</h3>
            <ul className="release-paths">
              {selected.notebooks?.map((item) => (
                <li key={item.path}>
                  <code>{item.path}</code>
                </li>
              ))}
            </ul>
            <p className="form-note">
              Choose this release and a captured notebook path in a workflow
              step to execute the pinned source.
            </p>
            <div className="form-grid">
              <Field label="Compare against another release">
                <select
                  value={otherId}
                  onChange={(event) => setOtherId(event.target.value)}
                >
                  <option value="">Choose release</option>
                  {releases
                    .filter((release) => release.id !== selected.id)
                    .map((release) => (
                      <option key={release.id} value={release.id}>
                        {release.name}
                      </option>
                    ))}
                </select>
              </Field>
              <button
                className="button"
                disabled={!otherId || busy}
                onClick={() =>
                  void perform(async () =>
                    setComparison(
                      await api(
                        `/releases/${selected.id}/compare?other_id=${otherId}&project_id=${projectId}`,
                      ),
                    ),
                  )
                }
              >
                <GitCompareArrows size={15} />
                Compare releases
              </button>
            </div>
            {comparison ? (
              <div className="release-comparison">
                <p>
                  Package environment{" "}
                  {comparison.environment_changed ? "changed" : "unchanged"}
                </p>
                {(["added", "removed", "changed"] as const).map((key) => (
                  <div key={key}>
                    <h4>
                      {key} · {comparison[key].length}
                    </h4>
                    {comparison[key].length ? (
                      comparison[key].map((path) => (
                        <code key={path}>{path}</code>
                      ))
                    ) : (
                      <span className="muted">None</span>
                    )}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </Modal>
      ) : null}
    </>
  );
}
