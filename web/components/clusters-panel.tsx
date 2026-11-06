"use client";

import { useState, type FormEvent } from "react";
import { CheckCircle2, Clock3, Cpu, Globe2, Layers3, Pencil, Plus, RefreshCw, Server, ShieldCheck, Trash2 } from "lucide-react";
import { api, bytes, date, json, type Cluster, type Compute } from "@/lib/api";
import { Empty, Field, Modal, SectionHead, Spinner, Status } from "./ui";

function idleDuration(seconds: number): string { if (seconds % 3600 === 0) return `${seconds / 3600} ${seconds === 3600 ? "hour" : "hours"}`; if (seconds % 60 === 0) return `${seconds / 60} ${seconds === 60 ? "minute" : "minutes"}`; return `${seconds} ${seconds === 1 ? "second" : "seconds"}`; }

type ClusterTest = { ok: boolean; cluster_id: string; status: Compute & { storage_scope?: string; capabilities?: string[] } };

function ClusterForm({ cluster, onSaved, onClose }: { cluster: Cluster | null; onSaved: () => void; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const values = new FormData(event.currentTarget); setBusy(true); setError("");
    try { await api(cluster ? `/clusters/${cluster.id}` : "/clusters", json(cluster ? "PUT" : "POST", { name: values.get("name"), url: values.get("url"), token: values.get("token") || "", verify_tls: values.has("verify_tls"), allow_insecure: values.has("allow_insecure") })); onSaved(); onClose(); }
    catch (failure) { setError((failure as Error).message); } finally { setBusy(false); }
  }
  return <Modal title={cluster ? "Configure remote cluster" : "Add a remote compute cluster"} onClose={() => { if (!busy) onClose(); }}><form className="modal-form" onSubmit={submit}><p className="modal-description">Connect an OpenBricks compute service running on another server. Notebooks and jobs can target this cluster independently.</p><Field label="Cluster name"><input name="name" required autoFocus maxLength={100} defaultValue={cluster?.name || ""} placeholder="Analytics compute" /></Field><Field label="Compute service URL" hint="The HTTPS address of an OpenBricks compute service, rather than a Spark master URL."><input type="url" name="url" required defaultValue={cluster?.url || ""} placeholder="https://compute.example.com" /></Field><Field label="Compute access token" hint={cluster ? "Leave blank to keep the saved token." : "Use the access token configured on the compute server."}><input name="token" type="password" autoComplete="new-password" required={!cluster} placeholder={cluster?.has_token ? "Saved token is hidden" : "Access token"} /></Field><label className="checkbox-field"><input name="verify_tls" type="checkbox" defaultChecked={cluster?.verify_tls ?? true} /><span>Verify the server&apos;s TLS certificate</span></label><label className="checkbox-field"><input name="allow_insecure" type="checkbox" defaultChecked={cluster?.allow_insecure ?? false} /><span>Allow HTTP on a trusted private network</span></label>{error ? <div className="inline-error" role="alert">{error}</div> : null}<div className="modal-footer"><button className="button" type="button" disabled={busy} onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}>{busy ? <Spinner /> : <Server size={15} />}{cluster ? "Save cluster" : "Add cluster"}</button></div></form></Modal>;
}

export function ClustersPanel({ clusters, admin, onChanged, onError }: { clusters: Cluster[]; admin: boolean; onChanged: () => void; onError: (message: string) => void }) {
  const [editing, setEditing] = useState<Cluster | null | undefined>(undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [tests, setTests] = useState<Record<string, ClusterTest>>( {} );
  const [failures, setFailures] = useState<Record<string, string>>({});
  async function test(cluster: Cluster) {
    setBusy(cluster.id); setFailures(previous => { const next = { ...previous }; delete next[cluster.id]; return next; });
    try { const result = await api<ClusterTest>(`/clusters/${cluster.id}/test`, json("POST")); setTests(previous => ({ ...previous, [cluster.id]: result })); onChanged(); }
    catch (error) { setFailures(previous => ({ ...previous, [cluster.id]: (error as Error).message })); } finally { setBusy(null); }
  }
  async function remove(cluster: Cluster) {
    if (!window.confirm(`Remove cluster “${cluster.name}” from this workspace? Its server and data will remain intact.`)) return;
    setBusy(cluster.id); try { await api(`/clusters/${cluster.id}`, json("DELETE")); onChanged(); } catch (error) { onError((error as Error).message); } finally { setBusy(null); }
  }
  return <><SectionHead eyebrow="COMPUTE, ON YOUR TERMS" title="Compute clusters" description="Keep the management workspace light. Run notebooks on dedicated local or remote compute." actions={admin ? <button className="button primary" onClick={() => setEditing(null)}><Plus size={15} />Add remote cluster</button> : undefined} />
    <div className="cluster-architecture"><span><Layers3 size={18} />Management workspace</span><span className="architecture-link" /><span><Cpu size={18} />Local compute container</span><span className="architecture-link" /><span><Globe2 size={18} />Remote clusters</span></div>
    <div className="cluster-grid">{clusters.map(cluster => {
      const current = tests[cluster.id]?.status || cluster.last_status;
      const cpuCapacity = current?.cpu_capacity;
      const capacityKnown = typeof cpuCapacity === "number" && Number.isFinite(cpuCapacity) && cpuCapacity > 0;
      const cpuValue = capacityKnown ? cpuCapacity.toLocaleString(undefined, { maximumSignificantDigits: 3 }) : current?.cpu_count ?? "—";
      const idleKnown = typeof current?.idle_timeout_seconds === "number";
      const idleTimeout = idleKnown ? Math.max(0, current!.idle_timeout_seconds!) : 900;
      return <section className="card cluster-card" key={cluster.id}><div className="cluster-card-top"><span className={`cluster-icon ${cluster.built_in ? "local" : "remote"}`}>{cluster.built_in ? <Server size={26} /> : <Globe2 size={26} />}</span><span className="subtle-tag">{cluster.built_in ? "Built-in local" : "Remote compute"}</span></div><div className="cluster-title"><h2>{cluster.name}</h2>{current ? <Status status={current.status} /> : <span className="subtle-tag">Not tested</span>}</div><p className="cluster-url">{cluster.url}</p>{current ? <><div className="cluster-metrics"><span>{capacityKnown ? "CPU capacity" : "Host CPU cores"}<strong>{cpuValue}</strong></span><span>Memory in use<strong>{bytes(current.memory_used)}</strong></span><span>Active / queued<strong>{current.active_runs || 0} / {current.queued_runs || 0}</strong></span></div><div className="cluster-runtime"><span>Apache Spark {current.spark_version || "—"}</span><span>Python {current.python_version || "—"}</span><code>{current.master || "—"}</code></div></> : <p className="cluster-untested">Test the connection to check runtime availability and resources.</p>}{tests[cluster.id]?.ok ? <div className="inline-success"><CheckCircle2 size={14} />Connection verified</div> : cluster.last_test_at ? <small className="muted">Last verified {date(cluster.last_test_at)}</small> : null}{failures[cluster.id] ? <div className="inline-error" role="alert">{failures[cluster.id]}</div> : null}<div className="cluster-idle-note"><Clock3 size={14} /><p>{idleTimeout === 0 ? "Idle shutdown is disabled for this cluster." : `${idleKnown ? "" : "Default: "}Notebook memory is released after ${idleDuration(idleTimeout)} idle. In-memory variables reset; saved notebooks and data persist.`}</p></div><div className="cluster-actions"><button className="button" disabled={!admin || busy === cluster.id} title={!admin ? "An administrator can test this cluster" : undefined} onClick={() => void test(cluster)}>{busy === cluster.id ? <Spinner /> : <RefreshCw size={14} />}Test connection</button>{admin && !cluster.built_in ? <><button className="icon-button" aria-label={`Configure cluster ${cluster.name}`} onClick={() => setEditing(cluster)}><Pencil size={15} /></button><button className="icon-button" disabled={busy === cluster.id} aria-label={`Remove cluster ${cluster.name}`} onClick={() => void remove(cluster)}><Trash2 size={15} /></button></> : null}</div></section>;
    })}</div>{!clusters.length ? <section className="card"><Empty icon={<Server size={30} />} title="Connect your compute">The built-in compute service will appear when configured on the server.</Empty></section> : null}<div className="info-strip"><ShieldCheck size={17} /><span>Choose a compute target in a notebook or schedule. Remote clusters execute on their own host; use connected object storage for shared datasets.</span></div>{editing !== undefined ? <ClusterForm cluster={editing} onSaved={() => { if (editing) setTests(previous => { const next = { ...previous }; delete next[editing.id]; return next; }); onChanged(); }} onClose={() => setEditing(undefined)} /> : null}</>;
}
