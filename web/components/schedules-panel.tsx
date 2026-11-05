"use client";

import { useEffect, useState, type FormEvent } from "react";
import { CalendarClock, Clock3, Pause, Pencil, Play, Plus, Search, Trash2 } from "lucide-react";
import { api, date, json, type BasicSchedule, type Cluster, type Job, type NotebookSummary, type Page, type WorkspacePage } from "@/lib/api";
import { Empty, Field, Modal, Pagination, SectionHead, Spinner, Status } from "./ui";

const days = [{ day: 1, label: "Mon" }, { day: 2, label: "Tue" }, { day: 3, label: "Wed" }, { day: 4, label: "Thu" }, { day: 5, label: "Fri" }, { day: 6, label: "Sat" }, { day: 0, label: "Sun" }];
function basicCron(value: BasicSchedule): string { const [hour, minute] = value.time.split(":").map(Number); return value.frequency === "hourly" ? `${value.minute} */${value.interval} * * *` : `${minute} ${hour} * * ${value.frequency === "weekly" ? value.days_of_week.join(",") : "*"}`; }
const defaultBasic: BasicSchedule = { frequency: "daily", interval: 1, minute: 0, time: "08:00", days_of_week: [1, 2, 3, 4, 5] };

function ScheduleForm({ job, clusters, onSave, onClose }: { job: Job | null; clusters: Cluster[]; onSave: () => void; onClose: () => void }) {
  const [mode, setMode] = useState<"basic" | "advanced">(job ? job.schedule_mode || "advanced" : "basic");
  const [basic, setBasic] = useState<BasicSchedule>({ ...defaultBasic, ...job?.basic_schedule });
  const [cron, setCron] = useState(job?.cron || "0 8 * * *");
  const [notebookId, setNotebookId] = useState(job?.notebook_id || "");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [notebooks, setNotebooks] = useState<WorkspacePage | null>(null);
  const [selectedNotebook, setSelectedNotebook] = useState<NotebookSummary | null>(job ? { id: job.notebook_id, name: job.notebook_name || "Selected notebook" } : null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { setOffset(0); }, [search]);
  useEffect(() => {
    let stopped = false;
    const timer = setTimeout(() => {
      setLoading(true);
      const query = new URLSearchParams({ all_folders: "true", search, offset: String(offset), limit: "20" });
      api<WorkspacePage>(`/workspace?${query}`).then(result => { if (!stopped) setNotebooks(result); }).catch(failure => { if (!stopped) setError(failure.message); }).finally(() => { if (!stopped) setLoading(false); });
    }, search ? 250 : 0);
    return () => { stopped = true; clearTimeout(timer); };
  }, [search, offset]);
  function updateBasic(value: Partial<BasicSchedule>) { setBasic(previous => ({ ...previous, ...value })); }
  function toggleDay(day: number) { updateBasic({ days_of_week: basic.days_of_week.includes(day) ? basic.days_of_week.filter(value => value !== day) : [...basic.days_of_week, day] }); }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError("");
    if (!notebookId) { setError("Choose a notebook for this schedule."); return; }
    if (mode === "basic" && basic.frequency === "weekly" && !basic.days_of_week.length) { setError("Choose at least one weekday."); return; }
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      await api(job ? `/jobs/${job.id}` : "/jobs", json(job ? "PUT" : "POST", { name: form.get("name"), notebook_id: notebookId, schedule_mode: mode, basic_schedule: basic, cron, timezone: form.get("timezone"), cluster_id: form.get("cluster_id") || null, enabled: job?.enabled ?? true }));
      onSave(); onClose();
    } catch (failure) { setError((failure as Error).message); } finally { setBusy(false); }
  }
  const options = notebooks?.items || [];
  return <Modal title={job ? "Edit notebook schedule" : "Create a scheduled job"} onClose={() => { if (!busy) onClose(); }} wide><form className="modal-form schedule-form" onSubmit={submit}><div className="form-grid"><Field label="Job name"><input name="name" required maxLength={200} autoFocus defaultValue={job?.name || ""} placeholder="Daily warehouse refresh" /></Field><Field label="Compute cluster"><select name="cluster_id" defaultValue={job?.cluster_id || ""}><option value="">Notebook default compute</option>{clusters.map(cluster => <option key={cluster.id} value={cluster.id}>{cluster.name}</option>)}</select></Field></div>
    <div className="notebook-picker"><label className="field"><span>Notebook</span><div className="list-search"><Search size={14} /><input value={search} aria-label="Search notebooks for schedule" placeholder="Search notebooks in all folders…" onChange={event => setSearch(event.target.value)} /></div></label><select aria-label="Notebook for schedule" value={notebookId} onChange={event => { setNotebookId(event.target.value); setSelectedNotebook(options.find(item => item.id === event.target.value) || null); }} required><option value="">Choose a notebook…</option>{selectedNotebook && !options.some(item => item.id === selectedNotebook.id) ? <option value={selectedNotebook.id}>{selectedNotebook.name}</option> : null}{options.map(notebook => <option key={notebook.id} value={notebook.id}>{`${(notebook.folder_path || "/Workspace").replace(/\/+$/, "")}/${notebook.name}`}</option>)}</select>{loading ? <small className="muted">Loading notebooks…</small> : null}<Pagination total={notebooks?.total || 0} offset={offset} limit={20} onPage={setOffset} busy={loading} /></div>
    <div className="schedule-tabs" role="tablist" aria-label="Schedule mode"><button type="button" role="tab" id="schedule-basic-tab" aria-selected={mode === "basic"} aria-controls="schedule-basic-panel" className={mode === "basic" ? "active" : ""} onClick={() => setMode("basic")}><CalendarClock size={15} />Basic</button><button type="button" role="tab" id="schedule-advanced-tab" aria-selected={mode === "advanced"} aria-controls="schedule-advanced-panel" className={mode === "advanced" ? "active" : ""} onClick={() => { if (mode === "basic") setCron(basicCron(basic)); setMode("advanced"); }}><Clock3 size={15} />Advanced</button></div>
    {mode === "basic" ? <div role="tabpanel" id="schedule-basic-panel" aria-labelledby="schedule-basic-tab" className="schedule-panel"><Field label="Repeat"><select value={basic.frequency} onChange={event => updateBasic({ frequency: event.target.value as BasicSchedule["frequency"] })}><option value="hourly">Every few hours</option><option value="daily">Every day</option><option value="weekly">On selected weekdays</option></select></Field>{basic.frequency === "hourly" ? <div className="form-grid"><Field label="Every"><select aria-label="Hourly interval" value={basic.interval} onChange={event => updateBasic({ interval: Number(event.target.value) })}>{Array.from({ length: 23 }, (_, index) => index + 1).map(value => <option key={value} value={value}>{value} {value === 1 ? "hour" : "hours"}</option>)}</select></Field><Field label="Minute past the hour"><input type="number" min={0} max={59} required value={basic.minute} onChange={event => updateBasic({ minute: Number(event.target.value) })} /></Field></div> : <Field label="Run at"><input type="time" required value={basic.time} onChange={event => updateBasic({ time: event.target.value })} /></Field>}{basic.frequency === "weekly" ? <fieldset className="weekday-fieldset"><legend>Days of the week</legend><div className="weekday-buttons">{days.map(({ day, label }) => <label key={day} className={basic.days_of_week.includes(day) ? "selected" : ""}><input type="checkbox" checked={basic.days_of_week.includes(day)} onChange={() => toggleDay(day)} /><span>{label}</span></label>)}</div></fieldset> : null}<p className="schedule-summary"><CalendarClock size={15} />{basic.frequency === "hourly" ? `Runs every ${basic.interval} ${basic.interval === 1 ? "hour" : "hours"}, at minute ${String(basic.minute).padStart(2, "0")}.` : basic.frequency === "daily" ? `Runs every day at ${basic.time}.` : `Runs ${days.filter(({ day }) => basic.days_of_week.includes(day)).map(({ label }) => label).join(", ") || "on the days you select"} at ${basic.time}.`}</p></div> : <div role="tabpanel" id="schedule-advanced-panel" aria-labelledby="schedule-advanced-tab" className="schedule-panel"><Field label="Cron expression" hint="Five fields: minute, hour, day of month, month, day of week. Sunday is 0 or 7."><input className="monospace" required value={cron} onChange={event => setCron(event.target.value)} placeholder="0 8 * * 1-5" /></Field><div className="cron-reference"><code>0 8 * * 1-5</code><span>Weekdays at 08:00</span><code>*/15 * * * *</code><span>Every 15 minutes</span><code>0 0 1 * *</code><span>First day of each month</span></div></div>}
    <Field label="Time zone" hint="Schedules follow this IANA time zone, including daylight saving changes."><input name="timezone" required defaultValue={job?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"} placeholder="America/New_York" /></Field>{error ? <div className="inline-error" role="alert">{error}</div> : null}<div className="modal-footer"><button type="button" className="button" disabled={busy} onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}>{busy ? <Spinner /> : <CalendarClock size={15} />}{job ? "Save schedule" : "Create job"}</button></div></form></Modal>;
}

export function SchedulesPanel({ totalJobs, activeJobs, clusters, onChanged, onOpen, onError }: { totalJobs: number; activeJobs: number; clusters: Cluster[]; onChanged: () => void; onOpen: (id: string) => void; onError: (message: string) => void }) {
  const [editing, setEditing] = useState<Job | null | undefined>(undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [notice, setNotice] = useState("");
  const [jobPage, setJobPage] = useState<Page<Job> | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  useEffect(() => { setOffset(0); }, [search]);
  useEffect(() => { let stopped = false; const timer = setTimeout(() => { setLoading(true); api<Page<Job>>(`/jobs/page?${new URLSearchParams({ search, limit: "25", offset: String(offset) })}`).then(result => { if (!stopped) setJobPage(result); }).catch(error => { if (!stopped) onError(error.message); }).finally(() => { if (!stopped) setLoading(false); }); }, search ? 250 : 0); return () => { stopped = true; clearTimeout(timer); }; }, [search, offset, version, onError]);
  useEffect(() => { const timer = setInterval(() => setVersion(value => value + 1), 15000); return () => clearInterval(timer); }, []);
  async function action(job: Job, kind: "toggle" | "run" | "delete") {
    if (kind === "delete" && !window.confirm(`Delete schedule “${job.name}”?`)) return;
    setBusy(job.id); setNotice("");
    try { if (kind === "toggle") await api(`/jobs/${job.id}`, json("PUT", { ...job, enabled: !job.enabled })); else await api(`/jobs/${job.id}${kind === "run" ? "/run" : ""}`, json(kind === "delete" ? "DELETE" : "POST")); if (kind === "run") setNotice(`Queued “${job.name}”. View its progress in Run history.`); setVersion(value => value + 1); onChanged(); }
    catch (error) { onError((error as Error).message); } finally { setBusy(null); }
  }
  const filtered = jobPage?.items || [];
  return <><SectionHead eyebrow="LET YOUR WORK KEEP WORKING" title="Jobs & schedules" description="Choose a simple schedule or write precise cron rules for each notebook." actions={<button className="button primary" onClick={() => setEditing(null)}><Plus size={15} />Create job</button>} /><div className="metric-strip"><span><span className="connected-dot" />{activeJobs} active schedules</span><span>{totalJobs.toLocaleString()} total jobs</span></div>{notice ? <div className="inline-success" role="status">{notice}</div> : null}<section className="card"><div className="browser-toolbar"><label className="list-search"><Search size={15} /><input aria-label="Search scheduled jobs" placeholder="Find a job or notebook…" value={search} onChange={event => setSearch(event.target.value)} /></label>{loading ? <Spinner /> : null}</div>{filtered.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>Job</th><th>Schedule</th><th>Compute</th><th>Next run</th><th>Status</th><th className="right">Actions</th></tr></thead><tbody>{filtered.map(job => <tr key={job.id}><td><button className="name-link" onClick={() => onOpen(job.notebook_id)}><span className="file-icon"><CalendarClock size={17} /></span><span>{job.name}<small>{job.notebook_name}</small></span></button></td><td><span className="subtle-tag">{job.schedule_mode === "basic" ? "Basic" : "Advanced"}</span><code className="table-subline">{job.cron}</code><small className="table-subline">{job.timezone}</small></td><td className="muted">{clusters.find(cluster => cluster.id === job.cluster_id)?.name || "Notebook default"}</td><td className="muted">{date(job.next_run_at)}</td><td><Status status={job.enabled ? "active" : "paused"} /></td><td className="right"><div className="row-actions"><button className="icon-button" disabled={busy === job.id} aria-label={`Run job ${job.name}`} onClick={() => void action(job, "run")}><Play size={14} /></button><button className="icon-button" disabled={busy === job.id} aria-label={`${job.enabled ? "Pause" : "Resume"} job ${job.name}`} onClick={() => void action(job, "toggle")}>{job.enabled ? <Pause size={14} /> : <Play size={14} />}</button><button className="icon-button" disabled={busy === job.id} aria-label={`Edit job ${job.name}`} onClick={() => setEditing(job)}><Pencil size={14} /></button><button className="icon-button" disabled={busy === job.id} aria-label={`Delete job ${job.name}`} onClick={() => void action(job, "delete")}><Trash2 size={14} /></button></div></td></tr>)}</tbody></table></div> : <Empty icon={<CalendarClock size={28} />} title={search ? "No matching schedules" : "Your workflows, on autopilot"} action={!search ? <button className="button primary" onClick={() => setEditing(null)}><Plus size={15} />Create your first job</button> : undefined}>Run notebooks daily, weekly, every few hours, or on an advanced cron schedule.</Empty>}<Pagination total={jobPage?.total || 0} offset={offset} limit={25} onPage={setOffset} busy={loading} /></section><div className="info-strip"><Clock3 size={17} /><span>Each job has its own time zone and compute target. Scheduled and interactive work share that cluster&apos;s execution queue.</span></div>{editing !== undefined ? <ScheduleForm job={editing} clusters={clusters} onSave={() => { setVersion(value => value + 1); onChanged(); }} onClose={() => setEditing(undefined)} /> : null}</>;
}
