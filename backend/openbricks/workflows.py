"""Durable notebook DAGs using the existing bounded compute execution queues."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .clusters import RuntimeBusy, timestamp as now
from .environments import capture_environment, get_environment
from .parameters import normalize_definitions, resolve_parameters
from .releases import public

TERMINAL = {"succeeded", "failed", "cancelled", "skipped"}
REFERENCE = re.compile(r"^\$\{params\.([A-Za-z_][A-Za-z0-9_]*)\}$")


class TaskInput(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
    notebook_id: str | None = None
    release_id: str | None = None
    path: str | None = None
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    cluster_id: str | None = None
    environment_id: str | None = None
    connection_ids: list[str] | None = Field(default=None, max_length=256)
    parameters: dict = Field(default_factory=dict)
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    max_retries: int = Field(default=0, ge=0, le=5)
    retry_delay_seconds: int = Field(default=30, ge=1, le=86400)
    retry_safe: bool = False


class WorkflowInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    project_id: str = "default"
    parameter_definitions: list[dict] = Field(default_factory=list, max_length=100)
    parameters: dict = Field(default_factory=dict)
    notification_ids: list[str] = Field(default_factory=list, max_length=20)
    tasks: list[TaskInput] = Field(min_length=1, max_length=100)


class StartInput(BaseModel):
    parameters: dict = Field(default_factory=dict)


class RepairInput(BaseModel):
    task_keys: list[str] | None = Field(default=None, max_length=100)


def substitute(value, parameters):
    if isinstance(value, str):
        match = REFERENCE.fullmatch(value)
        if match:
            if match[1] not in parameters:
                raise ValueError(f"Workflow parameter {match[1]} is not provided")
            return copy.deepcopy(parameters[match[1]])
    elif isinstance(value, dict):
        return {key: substitute(item, parameters) for key, item in value.items()}
    elif isinstance(value, list):
        return [substitute(item, parameters) for item in value]
    return value


class WorkflowManager:
    def __init__(self, store, runtime, access=None):
        self.store, self.runtime, self.access = store, runtime, access
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="openbricks-workflows")
        self.on_terminal = None

    def start(self):
        if not self.thread.is_alive():
            self.thread.start()

    def close(self):
        self.stopped.set()
        # Drain any API/scheduler admission that was already inside capture.
        # Release the lock before joining the coordinator thread.
        with self.lock:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def _authorize(self, user, project_id, action):
        if self.access is not None:
            if not user:
                raise ValueError("A current run-as account is required")
            self.access.require_project(user, project_id, action)

    def _source(self, task, project_id, user):
        if bool(task.get("notebook_id")) == bool(task.get("release_id")):
            raise ValueError("Each task needs either notebook_id or release_id and path")
        if task.get("release_id"):
            release = self.store.get("releases", task["release_id"])
            if not release or release.get("project_id") != project_id:
                raise ValueError("Task release was not found in this project")
            found = next((item["notebook"] for item in release["notebooks"] if item["path"] == task.get("path")), None)
            if not found:
                raise ValueError("Task notebook path was not found in its release")
            notebook = copy.deepcopy(found)
            environment = release["_environment"]
            provenance = {"release_id": release["id"], "repository_id": release["repository_id"], "commit": release["commit"], "path": task["path"], "source_sha256": release["source_sha256"]}
        else:
            notebook = self.store.get("notebooks", task["notebook_id"])
            if not notebook or notebook.get("project_id", "default") != project_id:
                raise ValueError("Task notebook was not found in this project")
            environment = None
            provenance = {"notebook_id": notebook["id"], "source_sha256": hashlib.sha256(json.dumps(notebook["cells"], sort_keys=True).encode()).hexdigest()}
        if task.get("connection_ids") is not None:
            notebook["connection_ids"] = task["connection_ids"]
        if self.access is not None:
            self.access.require_notebook(user, notebook, "run")
            if hasattr(self.access, "require_cluster"):
                self.access.require_cluster(user, task.get("cluster_id") or notebook.get("cluster_id"), project_id, "run")
            for identifier in notebook.get("connection_ids", []):
                self.access.require_connection(user, identifier, project_id, "run")
        if task.get("environment_id"):
            environment = get_environment(self.store, task["environment_id"], project_id)
        return notebook, environment, provenance

    def validate(self, value, user=None):
        value = WorkflowInput.model_validate(value).model_dump()
        project_id = value["project_id"]
        self._authorize(user, project_id, "edit")
        value["parameter_definitions"] = normalize_definitions(value["parameter_definitions"])
        # Saving a required input without a default is valid; it is requested
        # when the workflow starts rather than invented at definition time.
        partial = [{**item, "required": False} for item in value["parameter_definitions"]]
        resolve_parameters(partial, value["parameters"])
        for identifier in value["notification_ids"]:
            target = self.store.get("notification_targets", identifier)
            if not target or target.get("project_id", "default") != project_id:
                raise ValueError("Notification destination was not found in this project")
        tasks = {task["key"]: task for task in value["tasks"]}
        if len(tasks) != len(value["tasks"]):
            raise ValueError("Workflow task keys must be unique")
        for key, task in tasks.items():
            if key in task["depends_on"] or set(task["depends_on"]) - tasks.keys():
                raise ValueError("Task dependencies must reference other workflow tasks")
            if len(set(task["depends_on"])) != len(task["depends_on"]):
                raise ValueError("Task dependencies must be unique")
            if task["max_retries"] and not task["retry_safe"]:
                raise ValueError("Retries require retry_safe=true for a task with idempotent writes")
            self._source(task, project_id, user)
        visited, visiting = set(), set()
        def visit(key):
            if key in visiting:
                raise ValueError("Workflow dependencies contain a cycle")
            if key in visited:
                return
            visiting.add(key)
            for parent in tasks[key]["depends_on"]:
                visit(parent)
            visiting.remove(key)
            visited.add(key)
        for key in tasks:
            visit(key)
        return value

    def submit(self, workflow, parameters=None, user=None, trigger="manual", job_id=None, default_cluster_id=None):
        with self.lock:
            if self.stopped.is_set():
                raise RuntimeBusy("Workflow coordination is stopping; try again after maintenance")
            project_id = workflow.get("project_id", "default")
            self._authorize(user, project_id, "run")
            # Validation also checks graph and source access, while a run-only
            # account need not have permission to edit its saved definition.
            values = resolve_parameters(workflow.get("parameter_definitions", []), parameters, workflow.get("parameters", {}))
            specs = {}
            captured = None
            for original in workflow["tasks"]:
                task = copy.deepcopy(original)
                if default_cluster_id and not task.get("cluster_id"):
                    task["cluster_id"] = default_cluster_id
                notebook, environment, provenance = self._source(task, project_id, user)
                if environment is None:
                    if captured is None:
                        captured = capture_environment(self.store, project_id)
                    environment = captured
                inherited = {item["name"]: values[item["name"]] for item in notebook.get("parameter_definitions", []) if item["name"] in values}
                task_parameters = resolve_parameters(notebook.get("parameter_definitions", []), substitute(task.get("parameters", {}), values), inherited)
                specs[task["key"]] = {"task": copy.deepcopy(task), "notebook": notebook, "environment": environment,
                    "parameters": task_parameters, "provenance": provenance}
            if len(json.dumps(specs, allow_nan=False)) > 20_000_000:
                raise ValueError("Captured workflow source exceeds 20 MB")
            return self._new_run(workflow, values, specs, user, trigger, job_id)

    def _new_run(self, workflow, parameters, specs, user, trigger, job_id=None, previous=None, preserve=None):
        if self.stopped.is_set():
            raise RuntimeBusy("Workflow coordination is stopping; try again after maintenance")
        with self.store.connect() as db:
            active = db.execute("SELECT COUNT(*) FROM documents WHERE kind='workflow_runs' AND status IN ('queued','running')").fetchone()[0]
        if active >= 100:
            raise RuntimeBusy("Workflow queue is full")
        tasks = []
        for key, spec in specs.items():
            old = next((task for task in (previous or {}).get("tasks", []) if task["key"] == key), None)
            tasks.append(copy.deepcopy(old) if old and key in (preserve or set()) else {"key": key, "status": "pending", "depends_on": spec["task"].get("depends_on", []), "run_id": None, "attempts": [], "error": None})
        value = {"id": uuid4().hex, "workflow_id": workflow["id"], "job_id": job_id, "name": workflow["name"],
            "project_id": workflow.get("project_id", "default"), "status": "queued", "trigger": trigger, "parameters": parameters,
            "created_at": now(), "started_at": None, "finished_at": None, "error": None, "tasks": tasks,
            "progress": {"completed": sum(task["status"] in TERMINAL for task in tasks), "total": len(tasks)},
            "_specs": copy.deepcopy(specs), "_actor": {key: user[key] for key in ("id", "username", "role") if user and key in user},
            "repair_of": previous["id"] if previous else None}
        if self.stopped.is_set():
            raise RuntimeBusy("Workflow coordination is stopping; try again after maintenance")
        return self.store.put("workflow_runs", value)

    def submit_job(self, job, parameters=None, user=None, trigger="manual"):
        if self.stopped.is_set():
            raise RuntimeBusy("Workflow coordination is stopping; try again after maintenance")
        if user is None and job.get("run_as_user_id"):
            user = self.store.get("users", job["run_as_user_id"])
        values = {**job.get("parameters", {}), **(parameters or {})}
        if job.get("workflow_id"):
            workflow = self.store.get("workflows", job["workflow_id"])
            if not workflow:
                raise ValueError("Scheduled workflow no longer exists")
            return self.submit(workflow, values, user, trigger, job["id"], default_cluster_id=job.get("cluster_id"))
        notebook = self.store.get("notebooks", job.get("notebook_id", ""))
        if not notebook:
            raise ValueError("Scheduled notebook no longer exists")
        self._authorize(user, notebook.get("project_id", "default"), "run")
        if self.access is not None:
            self.access.require_notebook(user, notebook, "run")
            if hasattr(self.access, "require_cluster"):
                self.access.require_cluster(user, job.get("cluster_id") or notebook.get("cluster_id"), notebook.get("project_id", "default"), "run")
            for identifier in notebook.get("connection_ids", []):
                self.access.require_connection(user, identifier, notebook.get("project_id", "default"), "run")
        return self.runtime.submit(notebook, trigger=trigger, job_id=job["id"], cluster_id=job.get("cluster_id") or notebook.get("cluster_id"), parameters=values,
            timeout_seconds=job.get("timeout_seconds"), environment_id=job.get("environment_id"))

    def _actor(self, run):
        actor = run.get("_actor", {})
        if self.access is None:
            return actor
        current = self.store.get("users", actor.get("id", ""))
        if not current:
            raise ValueError("The run-as account is no longer available")
        self._authorize(current, run["project_id"], "run")
        return current

    def _admit(self, run, task):
        spec = run["_specs"][task["key"]]
        actor = self._actor(run)
        if self.access is not None:
            self.access.require_notebook(actor, spec["notebook"], "run")
            if hasattr(self.access, "require_cluster"):
                self.access.require_cluster(actor, spec["task"].get("cluster_id") or spec["notebook"].get("cluster_id"), run["project_id"], "run")
            for identifier in spec["notebook"].get("connection_ids", []):
                self.access.require_connection(actor, identifier, run["project_id"], "run")
        if not task.get("run_id"):
            task["run_id"] = uuid4().hex
            task["attempts"].append({"number": len(task["attempts"]) + 1, "run_id": task["run_id"], "status": "queued", "created_at": now()})
        task["status"] = "queued"
        self.store.put("workflow_runs", run)  # Persist admission ID before RPC.
        return self.runtime.submit(spec["notebook"], trigger="workflow", job_id=run.get("job_id"), cluster_id=spec["task"].get("cluster_id"),
            parameters=spec["parameters"], timeout_seconds=spec["task"].get("timeout_seconds", 3600), environment=spec["environment"],
            provenance={**spec["provenance"], "workflow_run_id": run["id"], "task_key": task["key"], "attempt": len(task["attempts"])}, run_id=task["run_id"])

    def _tick(self, run):
        dirty = False
        if run["status"] == "queued":
            run.update(status="running", started_at=now())
            dirty = True
        by_key = {task["key"]: task for task in run["tasks"]}
        for task in run["tasks"]:
            if task["status"] in TERMINAL:
                continue
            child = self.store.get("runs", task["run_id"]) if task.get("run_id") else None
            if run.get("_cancel_requested"):
                if child and child["status"] not in TERMINAL:
                    try:
                        child = self.runtime.cancel(child["id"])
                    except (ValueError, RuntimeBusy):
                        if run.get("error") != "Cancellation is pending acknowledgment from compute":
                            run["error"] = "Cancellation is pending acknowledgment from compute"
                            dirty = True
                        continue
                task.update(status="cancelled", error="Workflow cancelled")
                if task["attempts"]:
                    task["attempts"][-1].update(status=child["status"] if child else "cancelled", finished_at=now())
                dirty = True
                continue
            if child:
                attempt = task["attempts"][-1]
                attempt.setdefault("admitted_at", attempt["created_at"])
                timestamps = {"created_at": child.get("compute_created_at") or child.get("created_at"), "started_at": child.get("started_at")}
                for field, value in timestamps.items():
                    if value is not None and attempt.get(field) != value:
                        attempt[field] = value
                        dirty = True
                if child["status"] not in TERMINAL:
                    if task["status"] != child["status"]:
                        task["status"] = child["status"]
                        task["attempts"][-1]["status"] = child["status"]
                        dirty = True
                    continue
                if attempt.get("finished_at") is None:
                    attempt.update(status=child["status"], finished_at=child.get("finished_at") or now(), error=child.get("error"))
                    task["error"] = child.get("error")
                    spec = run["_specs"][task["key"]]["task"]
                    retryable = child["status"] == "failed" and child.get("error_class") not in ("unknown_outcome", "configuration", "permissions", "cancelled")
                    if retryable and spec.get("retry_safe") and len(task["attempts"]) <= spec.get("max_retries", 0):
                        task.update(status="waiting_retry", next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=spec.get("retry_delay_seconds", 30))).isoformat())
                    else:
                        task["status"] = child["status"]
                    dirty = True
                if task["status"] != "waiting_retry":
                    continue
                if datetime.now(timezone.utc) < datetime.fromisoformat(task["next_retry_at"]):
                    continue
                task.update(status="pending", run_id=None, error=None)
                task.pop("next_retry_at", None)
                dirty = True
            dependencies = [by_key[key]["status"] for key in task.get("depends_on", [])]
            if any(status in ("failed", "skipped", "cancelled") for status in dependencies):
                task.update(status="skipped", error="An upstream task did not succeed")
                dirty = True
                continue
            if not all(status == "succeeded" for status in dependencies):
                continue
            try:
                self._admit(run, task)
                dirty = True
            except RuntimeBusy:
                pass  # Retry admission with the same ID, not a new attempt.
            except Exception:
                # An admission that created a durable child record is recovered
                # by that record. Do not infer whether remote code has run.
                if task.get("run_id") and self.store.get("runs", task["run_id"]):
                    continue
                task.update(status="failed", error="Task admission failed; check permissions, source, and pinned artifacts")
                if task["attempts"]:
                    task["attempts"][-1].update(status="failed", finished_at=now(), error=task["error"])
                dirty = True
        complete = sum(task["status"] in TERMINAL for task in run["tasks"])
        run["progress"] = {"completed": complete, "total": len(run["tasks"])}
        if complete == len(run["tasks"]):
            status = "cancelled" if run.get("_cancel_requested") else ("succeeded" if all(task["status"] == "succeeded" for task in run["tasks"]) else "failed")
            run.update(status=status, finished_at=now(), error=None if status == "succeeded" else run.get("error") or "One or more workflow tasks did not succeed")
            dirty = True
        if dirty:
            self.store.put("workflow_runs", run)
            if run["status"] in TERMINAL and self.on_terminal:
                self.on_terminal(public(run))

    def _loop(self):
        while not self.stopped.wait(0.25):
            with self.lock:
                with self.store.connect() as db:
                    rows = db.execute("SELECT body FROM documents WHERE kind='workflow_runs' AND status IN ('queued','running') ORDER BY created_at,id").fetchall()
                for row in rows:
                    run = json.loads(row[0])
                    try:
                        self._tick(run)
                    except Exception:
                        # Preserve the durable state for recovery; a coordinator
                        # error must never trigger another execution attempt.
                        continue

    def cancel(self, identifier, user=None):
        with self.lock:
            run = self.store.get("workflow_runs", identifier)
            if not run:
                raise ValueError("Workflow run not found")
            self._authorize(user, run["project_id"], "run")
            if run["status"] not in TERMINAL:
                run["_cancel_requested"] = True
                run["cancel_requested"] = True
                self.store.put("workflow_runs", run)
                self._tick(run)
            return self.store.get("workflow_runs", identifier)

    def replay(self, identifier, user=None, repair=False, task_keys=None):
        with self.lock:
            old = self.store.get("workflow_runs", identifier)
            if not old or old["status"] not in TERMINAL:
                raise ValueError("Only a finished workflow run can be repaired or rerun")
            self._authorize(user, old["project_id"], "run")
            selected = set(task_keys or [task["key"] for task in old["tasks"] if task["status"] != "succeeded"])
            if selected - old["_specs"].keys():
                raise ValueError("Unknown workflow task key")
            changed = True
            while changed:
                previous = len(selected)
                selected.update(task["key"] for task in old["tasks"] if selected.intersection(task.get("depends_on", [])))
                changed = len(selected) != previous
            preserve = {task["key"] for task in old["tasks"] if task["status"] == "succeeded" and task["key"] not in selected} if repair else set()
            if repair and not selected:
                raise ValueError("There are no failed tasks to repair")
            workflow = {"id": old["workflow_id"], "name": old["name"], "project_id": old["project_id"]}
            return self._new_run(workflow, old["parameters"], old["_specs"], user, "repair" if repair else "rerun", old.get("job_id"), old, preserve)


def register_workflow_routes(app, authenticated, access):
    def lookup(request, kind, identifier, user, action="read"):
        value = request.app.state.store.get(kind, identifier)
        if not value:
            raise HTTPException(404, "Workflow or run not found")
        access.require_project(user, value.get("project_id", "default"), action)
        return value

    @app.get("/api/workflows")
    def listing(request: Request, project_id: str = "default", user=Depends(authenticated)):
        access.require_project(user, project_id, "read")
        return public([item for item in request.app.state.store.list("workflows") if item.get("project_id", "default") == project_id])

    @app.post("/api/workflows", status_code=201)
    def create(body: WorkflowInput, request: Request, user=Depends(authenticated)):
        value = request.app.state.workflows.validate(body.model_dump(), user)
        return public(request.app.state.store.put("workflows", {**value, "id": uuid4().hex, "created_at": now(), "updated_at": now()}))

    @app.get("/api/workflows/page")
    def page(request: Request, project_id: str = "default", search: str = "", limit: int = 25, offset: int = 0, user=Depends(authenticated)):
        access.require_project(user, project_id, "read")
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        where = "kind='workflows' AND COALESCE(json_extract(body,'$.project_id'),'default')=? AND instr(lower(name),lower(?))>0"
        params = (project_id, search)
        with request.app.state.store.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM documents WHERE " + where, params).fetchone()[0]
            rows = db.execute("SELECT body FROM documents WHERE " + where + " ORDER BY updated_at DESC,id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {"items": [public(json.loads(row[0])) for row in rows], "total": total, "limit": limit, "offset": offset}

    @app.get("/api/workflows/{id}")
    def detail(id: str, request: Request, user=Depends(authenticated)):
        return public(lookup(request, "workflows", id, user))

    @app.put("/api/workflows/{id}")
    def update(id: str, body: WorkflowInput, request: Request, user=Depends(authenticated)):
        old = lookup(request, "workflows", id, user, "edit")
        if body.project_id != old.get("project_id", "default"):
            raise ValueError("Move workflow projects through a new definition")
        value = request.app.state.workflows.validate(body.model_dump(), user)
        return public(request.app.state.store.put("workflows", {**value, "id": id, "created_at": old["created_at"], "updated_at": now()}))

    @app.delete("/api/workflows/{id}")
    def delete(id: str, request: Request, user=Depends(authenticated)):
        lookup(request, "workflows", id, user, "edit")
        store = request.app.state.store
        with store.connect() as db:
            linked = db.execute("SELECT 1 FROM documents WHERE (kind='jobs' AND json_extract(body,'$.workflow_id')=?) OR (kind='workflow_runs' AND json_extract(body,'$.workflow_id')=? AND status IN ('queued','running')) LIMIT 1", (id, id)).fetchone()
        if linked:
            raise HTTPException(409, "Remove schedules and finish active runs before deleting this workflow")
        store.delete("workflows", id)
        return {"ok": True}

    @app.post("/api/workflows/{id}/run", status_code=202)
    def start(id: str, body: StartInput, request: Request, user=Depends(authenticated)):
        value = lookup(request, "workflows", id, user, "run")
        return public(request.app.state.workflows.submit(value, body.parameters, user))

    @app.get("/api/workflow-runs")
    def runs(request: Request, workflow_id: str | None = None, project_id: str = "default", limit: int = 25, offset: int = 0, user=Depends(authenticated)):
        access.require_project(user, project_id, "read")
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        where, params = "kind='workflow_runs' AND COALESCE(json_extract(body,'$.project_id'),'default')=?", [project_id]
        if workflow_id:
            lookup(request, "workflows", workflow_id, user)
            where += " AND json_extract(body,'$.workflow_id')=?"
            params.append(workflow_id)
        with request.app.state.store.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM documents WHERE " + where, params).fetchone()[0]
            rows = db.execute("SELECT json_remove(body,'$._specs','$._actor') FROM documents WHERE " + where + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {"items": [public(json.loads(row[0])) for row in rows], "total": total, "limit": limit, "offset": offset}

    @app.get("/api/workflow-runs/{id}")
    def run_detail(id: str, request: Request, user=Depends(authenticated)):
        return public(lookup(request, "workflow_runs", id, user))

    @app.post("/api/workflow-runs/{id}/cancel")
    def cancel(id: str, request: Request, user=Depends(authenticated)):
        return public(request.app.state.workflows.cancel(id, user))

    @app.post("/api/workflow-runs/{id}/repair", status_code=202)
    def repair(id: str, body: RepairInput, request: Request, user=Depends(authenticated)):
        return public(request.app.state.workflows.replay(id, user, repair=True, task_keys=body.task_keys))

    @app.post("/api/workflow-runs/{id}/rerun", status_code=202)
    def rerun(id: str, request: Request, user=Depends(authenticated)):
        return public(request.app.state.workflows.replay(id, user))
