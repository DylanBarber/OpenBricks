"use client";
import { useEffect, useState } from "react";
import { ArrowUpRight, PackageCheck, Plus, RefreshCw } from "lucide-react";
import { api, date, json, type Page } from "@/lib/api";
import { Empty, Field, Modal, Pagination, SectionHead, Spinner } from "./ui";

type Change = { kind: string; key: string; id: string; action: string };
type Plan = {
  name: string;
  target: string;
  project_id: string;
  plan_hash: string;
  environment_id: string;
  counts: Record<string, number>;
  changes: Change[];
  warnings: string[];
};
type Deployment = {
  id: string;
  name: string;
  target: string;
  created_at: string;
  environment_id: string;
  counts: Record<string, number>;
  resources?: { kind: string; key: string; id: string }[];
};
const example = (projectId: string) =>
  JSON.stringify(
    {
      format_version: 1,
      name: "hello-pipeline",
      targets: { dev: { project_id: projectId } },
      environment: { wheels: [] },
      notebooks: [
        {
          key: "hello",
          path: "notebooks/hello.py",
          source:
            "# Databricks notebook source\nprint('Hello from a deployed project')\n",
        },
      ],
      workflows: [
        { key: "daily", tasks: [{ key: "hello", notebook: "hello" }] },
      ],
      schedules: [],
    },
    null,
    2,
  );

export default function DeploymentsPanel({
  projectId,
  role,
  onError,
  onChanged,
}: {
  projectId: string;
  role: string;
  onError: (message: string) => void;
  onChanged?: () => void;
}) {
  const [page, setPage] = useState<Page<Deployment> | null>(null),
    [offset, setOffset] = useState(0),
    [version, setVersion] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [message, setMessage] = useState("");
  const [create, setCreate] = useState(false),
    [definition, setDefinition] = useState(() => example(projectId)),
    [target, setTarget] = useState("dev"),
    [plan, setPlan] = useState<Plan | null>(null),
    [planOffset, setPlanOffset] = useState(0),
    [selected, setSelected] = useState<Deployment | null>(null),
    [promotionTarget, setPromotionTarget] = useState("prod");
  const canEdit = ["admin", "owner", "editor"].includes(role);
  useEffect(() => {
    setPage(null);
    setOffset(0);
    setDefinition(example(projectId));
    setTarget("dev");
    setPlan(null);
    setSelected(null);
    setCreate(false);
    setError("");
    setMessage("");
  }, [projectId]);
  useEffect(() => {
    let stopped = false;
    api<Page<Deployment>>(
      `/deployments?${new URLSearchParams({ project_id: projectId, limit: "25", offset: String(offset) })}`,
    )
      .then((value) => {
        if (!stopped) setPage(value);
      })
      .catch((failure: Error) => {
        if (!stopped) setError(failure.message);
      });
    return () => {
      stopped = true;
    };
  }, [projectId, offset, version]);
  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (failure) {
      const text = (failure as Error).message;
      setError(text);
      onError(text);
    } finally {
      setBusy(false);
    }
  }
  const changed = () => {
    setVersion((value) => value + 1);
    onChanged?.();
  };
  async function preview() {
    await perform(async () => {
      const value =
        !create && selected
          ? await api<Plan>(
              `/deployments/${selected.id}/promotion-plan`,
              json("POST", { target: promotionTarget }),
            )
          : await api<Plan>(
              "/deployments/plan",
              json("POST", { bundle: JSON.parse(definition), target }),
            );
      setPlan(value);
      setPlanOffset(0);
    });
  }
  async function apply() {
    if (!plan) return;
    await perform(async () => {
      const value =
        !create && selected
          ? await api<Deployment>(
              `/deployments/${selected.id}/promote`,
              json("POST", {
                target: promotionTarget,
                expected_plan_hash: plan.plan_hash,
              }),
            )
          : await api<Deployment>(
              "/deployments",
              json("POST", {
                bundle: JSON.parse(definition),
                target,
                expected_plan_hash: plan.plan_hash,
              }),
            );
      setCreate(false);
      setSelected(value);
      setPlan(null);
      setMessage(`Deployed ${value.name} to ${value.target}.`);
      changed();
    });
  }
  async function exportBundle(id: string) {
    await perform(async () => {
      const value = await api<unknown>(`/deployments/${id}/export`);
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(value, null, 2)], {
          type: "application/json",
        }),
      );
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "openbricks-bundle.json";
      anchor.click();
      URL.revokeObjectURL(url);
    });
  }
  return (
    <div className="platform-stack">
      <SectionHead
        eyebrow="FROM SOURCE TO RUNNING PIPELINES"
        title="Project deployments"
        description="Validate and deploy notebooks, workflows, schedules, and pinned wheel environments together. Promote the same captured project between targets."
        actions={
          <>
            <button
              className="button"
              onClick={() => setVersion((value) => value + 1)}
              disabled={busy}
            >
              <RefreshCw size={15} />
              Refresh
            </button>
            {canEdit ? (
              <button
                className="button primary"
                onClick={() => {
                  setCreate(true);
                  setSelected(null);
                  setPlan(null);
                }}
              >
                <Plus size={15} />
                Deploy project
              </button>
            ) : null}
          </>
        }
      />
      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}
      {message ? (
        <div className="inline-success" role="status">
          {message}
        </div>
      ) : null}
      <section className="card platform-form">
        <h3>Git and CI delivery</h3>
        <p className="form-note">
          Store your bundle beside its notebooks. Use the CLI to pack local
          files, validate workspace references, and deploy with a scoped
          service-account token.
        </p>
        <pre>
          openbricks plan openbricks.yml --target dev{"\n"}openbricks deploy
          openbricks.yml --target dev
        </pre>
        <p className="form-note">
          Declarations contain connection references, never saved connection
          passwords. Existing resources omitted from a bundle are preserved.
        </p>
      </section>
      <section className="card">
        {page?.items.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Deployment</th>
                <th>Target</th>
                <th>Resources</th>
                <th>Created</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <button
                      className="text-button"
                      onClick={() =>
                        void perform(async () => {
                          setSelected(
                            await api<Deployment>(`/deployments/${item.id}`),
                          );
                          setPlan(null);
                        })
                      }
                    >
                      {item.name}
                    </button>
                  </td>
                  <td>{item.target}</td>
                  <td>
                    {item.counts.notebooks || 0} notebooks ·{" "}
                    {item.counts.workflows || 0} workflows ·{" "}
                    {item.counts.jobs || 0} schedules
                  </td>
                  <td>{date(item.created_at)}</td>
                  <td>
                    <button
                      className="button compact"
                      onClick={() => void exportBundle(item.id)}
                      disabled={busy}
                    >
                      Export bundle
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : page ? (
          <Empty
            icon={<PackageCheck size={28} />}
            title="Ship a complete project"
          >
            Preview a bundle to create a consistent set of notebooks and
            workflows.
          </Empty>
        ) : (
          <Spinner />
        )}
        {page ? (
          <Pagination
            total={page.total}
            offset={offset}
            limit={25}
            onPage={setOffset}
            busy={busy}
          />
        ) : null}
      </section>
      {create || selected ? (
        <Modal
          title={
            create
              ? "Deploy a project bundle"
              : `${selected?.name} · ${selected?.target}`
          }
          wide
          onClose={() => {
            if (!busy) {
              setCreate(false);
              setSelected(null);
              setPlan(null);
            }
          }}
        >
          <div className="modal-form">
            {create ? (
              <>
                <Field
                  label="Packed bundle (JSON)"
                  hint="Paste the output of openbricks pack, or start with this inline example."
                >
                  <textarea
                    aria-label="Project bundle JSON"
                    rows={20}
                    value={definition}
                    onChange={(event) => {
                      setDefinition(event.target.value);
                      setPlan(null);
                    }}
                    disabled={busy}
                  />
                </Field>
                <Field label="Target">
                  <input
                    value={target}
                    onChange={(event) => {
                      setTarget(event.target.value);
                      setPlan(null);
                    }}
                    disabled={busy}
                  />
                </Field>
              </>
            ) : selected ? (
              <>
                <p className="form-note">
                  Environment <code>{selected.environment_id}</code>. Promotion
                  uses this captured source and package manifest, including the
                  target bindings saved with the bundle.
                </p>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Resource</th>
                      <th>Key</th>
                      <th>ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selected.resources?.slice(0, 100).map((item) => (
                      <tr key={`${item.kind}:${item.id}`}>
                        <td>{item.kind}</td>
                        <td>{item.key}</td>
                        <td>
                          <code>{item.id}</code>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {(selected.resources?.length || 0) > 100 ? (
                  <p className="form-note">
                    Showing the first 100 resource references. Export the bundle
                    for the complete project.
                  </p>
                ) : null}
                {canEdit ? (
                  <Field label="Promote to declared target">
                    <input
                      placeholder="prod"
                      value={promotionTarget}
                      onChange={(event) => {
                        setPromotionTarget(event.target.value);
                        setPlan(null);
                      }}
                      disabled={busy}
                    />
                  </Field>
                ) : null}
              </>
            ) : null}
            {plan ? (
              <section className="card platform-form">
                <h3>Deployment plan</h3>
                <p>
                  {plan.name} → {plan.target} · {plan.counts.notebooks}{" "}
                  notebooks, {plan.counts.workflows} workflows,{" "}
                  {plan.counts.jobs} schedules
                </p>
                <p className="form-note">
                  Plan <code>{plan.plan_hash.slice(0, 16)}</code> · environment{" "}
                  <code>{plan.environment_id.slice(0, 16)}</code>
                </p>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Action</th>
                      <th>Resource</th>
                      <th>Key</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.changes
                      .slice(planOffset, planOffset + 25)
                      .map((item) => (
                        <tr key={`${item.kind}:${item.id}`}>
                          <td>{item.action}</td>
                          <td>{item.kind}</td>
                          <td>{item.key}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
                <Pagination
                  total={plan.changes.length}
                  offset={planOffset}
                  limit={25}
                  onPage={setPlanOffset}
                />
                {plan.warnings.map((value) => (
                  <p key={value} className="form-note">
                    {value}
                  </p>
                ))}
              </section>
            ) : null}
            {error ? (
              <div className="inline-error" role="alert">
                {error}
              </div>
            ) : null}
            <div className="modal-footer">
              <button
                className="button"
                disabled={busy}
                onClick={() => {
                  setCreate(false);
                  setSelected(null);
                  setPlan(null);
                }}
              >
                Close
              </button>
              {canEdit ? (
                <>
                  <button
                    className="button"
                    disabled={busy || (!create && !promotionTarget)}
                    onClick={() => void preview()}
                  >
                    {busy ? <Spinner /> : null}Preview{" "}
                    {create ? "deployment" : "promotion"}
                  </button>
                  {plan ? (
                    <button
                      className="button primary"
                      disabled={busy}
                      onClick={() => void apply()}
                    >
                      <ArrowUpRight size={15} />
                      Apply reviewed plan
                    </button>
                  ) : null}
                </>
              ) : null}
            </div>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
