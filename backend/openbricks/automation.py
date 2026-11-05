"""Durable backfills, missed schedules, and bounded metadata event polling.

The queue persists intent before source checkpoints and before compute RPCs.
Only one management owner should coordinate a workspace database.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .clusters import RuntimeBusy, cluster_record
from .credentials import SecretBox
from .parameters import resolve_parameters
from .permissions import DEFAULT_PROJECT, now
from .releases import public
from .security import bucket_name, confined_path
from .workflows import TERMINAL, substitute


class BackfillInput(BaseModel):
    name: str = Field(default="Date range backfill", min_length=1, max_length=200)
    project_id: str = DEFAULT_PROJECT
    notebook_id: str | None = None
    workflow_id: str | None = None
    date_parameter: str = Field(min_length=1, max_length=64)
    start_date: date
    end_date: date
    parameters: dict = Field(default_factory=dict, max_length=100)
    max_concurrency: int = Field(default=1, ge=1, le=8)
    stop_on_failure: bool = True


class TriggerSource(BaseModel):
    path: str | None = Field(default=None, max_length=2048)
    connection_id: str | None = None
    workflow_id: str | None = None
    states: list[str] = Field(
        default_factory=lambda: ["succeeded"], min_length=1, max_length=3
    )
    pattern: str = Field(default="*", min_length=1, max_length=256)


class TriggerInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    project_id: str = DEFAULT_PROJECT
    notebook_id: str | None = None
    workflow_id: str | None = None
    type: str
    source: TriggerSource
    parameters: dict = Field(default_factory=dict, max_length=100)
    event_parameter: str | None = None
    enabled: bool = True
    interval_seconds: int = Field(default=30, ge=15, le=86400)
    debounce_seconds: int = Field(default=10, ge=0, le=3600)
    include_existing: bool = False


def instant(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def identity(*parts):
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_uri(value):
    parsed = urlsplit(value or "")
    if (
        parsed.scheme not in ("blob", "s3")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Use a blob://bucket/path or s3://bucket/path source without credentials"
        )
    bucket_name(parsed.netloc)
    if (
        any(part in (".", "..") for part in parsed.path.split("/"))
        or "\\" in parsed.path
    ):
        raise ValueError("Trigger source paths cannot contain traversal segments")
    return parsed.scheme, parsed.netloc, parsed.path.lstrip("/")


class AutomationManager:
    maximum_pending = 1000
    scan_batch = 500

    def __init__(self, app, cron_factory=None):
        self.app, self.store = app, app.state.store
        self.workflows, self.access = app.state.workflows, app.state.access
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.paused = 0
        self.thread = threading.Thread(
            target=self._loop, daemon=True, name="openbricks-automation"
        )
        self.cron_factory = cron_factory
        self._scans = {}
        self._job_offset = self._trigger_offset = self._backfill_offset = 0
        self._next_schedules = 0
        with self.store.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS automation_objects (
                trigger_id TEXT NOT NULL,generation TEXT NOT NULL,object_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,PRIMARY KEY(trigger_id,generation,object_key))"""
            )
            db.execute("""CREATE TABLE IF NOT EXISTS automation_events (
                id TEXT PRIMARY KEY,trigger_id TEXT NOT NULL,generation TEXT NOT NULL,
                project_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,
                created_at TEXT NOT NULL,ready_at TEXT NOT NULL,dispatch_id TEXT)""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS automation_event_due ON automation_events(trigger_id,status,ready_at,id)"
            )

    def start(self):
        if not self.thread.is_alive():
            self.thread.start()

    def close(self):
        self.stopped.set()
        with self.lock:
            self._scans.clear()
        if self.thread.is_alive():
            self.thread.join(timeout=6)

    def pause(self):
        with self.lock:
            self.paused += 1

    def resume(self):
        with self.lock:
            self.paused = max(0, self.paused - 1)

    @contextmanager
    def maintenance(self):
        self.pause()
        try:
            yield
        finally:
            self.resume()

    def _ready(self):
        if (
            self.stopped.is_set()
            or self.paused
            or getattr(self.app.state, "maintenance", None)
        ):
            raise RuntimeBusy("Automation is paused for workspace maintenance")

    def _actor(self, identifier, project_id):
        user = self.store.get("users", identifier)
        if not user or user.get("disabled") or user.get("enabled") is False:
            raise ValueError("The automation run-as account is unavailable")
        self.access.require_project(user, project_id, "run")
        return user

    def _definition(self, values, user):
        project_id = values.get("project_id", DEFAULT_PROJECT)
        self.access.require_project(user, project_id, "run")
        if bool(values.get("notebook_id")) == bool(values.get("workflow_id")):
            raise ValueError("Choose exactly one notebook or workflow")
        if values.get("workflow_id"):
            workflow = self.store.get("workflows", values["workflow_id"])
        else:
            notebook = self.store.get("notebooks", values["notebook_id"])
            workflow = (
                None
                if not notebook
                else {
                    "id": "notebook:" + notebook["id"],
                    "name": notebook["name"],
                    "project_id": notebook.get("project_id", DEFAULT_PROJECT),
                    "parameter_definitions": notebook.get("parameter_definitions", []),
                    "parameters": {},
                    "tasks": [
                        {
                            "key": "notebook",
                            "notebook_id": notebook["id"],
                            "depends_on": [],
                            "cluster_id": notebook.get("cluster_id"),
                            "environment_id": notebook.get("environment_id"),
                        }
                    ],
                }
            )
        if not workflow or workflow.get("project_id", DEFAULT_PROJECT) != project_id:
            raise ValueError(
                "Automation target was not found in its configured project"
            )
        return workflow

    def preview_backfill(self, body, user):
        values = (
            body.model_dump()
            if isinstance(body, BackfillInput)
            else BackfillInput.model_validate(body).model_dump()
        )
        start, end = values["start_date"], values["end_date"]
        count = (end - start).days + 1
        if not 1 <= count <= 366:
            raise ValueError("Choose an inclusive date range of 1 to 366 days")
        workflow = self._definition(values, user)
        definition = next(
            (
                item
                for item in workflow.get("parameter_definitions", [])
                if item["name"] == values["date_parameter"]
            ),
            None,
        )
        if not definition or definition.get("type", "string") not in ("date", "string"):
            raise ValueError(
                "Choose a declared date or string parameter for the backfill date"
            )
        dates = [
            (start + timedelta(days=offset)).isoformat() for offset in range(count)
        ]
        # Validate every combination without capturing artifacts or persisting
        # an environment during a preview.
        specs = [
            (
                task,
                self.workflows._source(task, values["project_id"], user)[0].get(
                    "parameter_definitions", []
                ),
            )
            for task in workflow["tasks"]
        ]
        for day in dates:
            parameters = resolve_parameters(
                workflow.get("parameter_definitions", []),
                {**values["parameters"], values["date_parameter"]: day},
                workflow.get("parameters", {}),
            )
            for task, definitions in specs:
                inherited = {
                    item["name"]: parameters[item["name"]]
                    for item in definitions
                    if item["name"] in parameters
                }
                resolve_parameters(
                    definitions,
                    substitute(task.get("parameters", {}), parameters),
                    inherited,
                )
        return {
            "dates": dates,
            "count": count,
            "date_parameter": values["date_parameter"],
            "target_name": workflow["name"],
            "max_concurrency": values["max_concurrency"],
            "stop_on_failure": values["stop_on_failure"],
            "capture": "Source and wheel environments are pinned when the backfill is created",
        }

    def create_backfill(self, body, user):
        values = (
            body.model_dump()
            if isinstance(body, BackfillInput)
            else BackfillInput.model_validate(body).model_dump()
        )
        with self.lock, self.workflows.lock:
            self._ready()
            preview = self.preview_backfill(values, user)
            with self.store.connect() as db:
                active = db.execute(
                    "SELECT COUNT(*) FROM documents WHERE kind='backfills' AND status IN ('queued','running')"
                ).fetchone()[0]
            if active >= 25:
                raise RuntimeBusy(
                    "Wait for an active backfill to finish before creating another"
                )
            workflow = self._definition(values, user)
            parameters = {
                **values["parameters"],
                values["date_parameter"]: preview["dates"][0],
            }
            captured = self.workflows.capture(workflow, parameters, user)
            identifier = uuid4().hex
            template_id = uuid4().hex
            self.store.put(
                "automation_templates",
                {
                    "id": template_id,
                    "project_id": values["project_id"],
                    "created_at": now(),
                    "_capture": captured,
                },
            )
            entry = {
                **values,
                "start_date": values["start_date"].isoformat(),
                "end_date": values["end_date"].isoformat(),
                "id": identifier,
                "created_at": now(),
                "status": "queued",
                "run_as_user_id": user["id"],
                "target_name": workflow["name"],
                "_template_id": template_id,
                "items": [
                    {
                        "date": day,
                        "status": "pending",
                        "run_id": identity("backfill", identifier, day),
                    }
                    for day in preview["dates"]
                ],
                "progress": {"completed": 0, "total": preview["count"]},
                "error": None,
                "snapshot_id": identity(
                    captured["workflow"],
                    {
                        key: {
                            "source": spec["provenance"],
                            "environment": spec["environment"]["id"],
                        }
                        for key, spec in captured["specs"].items()
                    },
                ),
                "environment_ids": sorted(
                    {spec["environment"]["id"] for spec in captured["specs"].values()}
                ),
            }
            self._ready()
            return self.store.put("backfills", entry)

    def _tick_backfill(self, entry):
        active = 0
        failure = False
        for item in entry["items"]:
            child = (
                self.store.get("workflow_runs", item["run_id"])
                if item["status"] != "pending"
                else None
            )
            if child:
                item["run_available"] = True
                item["status"] = child["status"]
                item["error"] = child.get("error")
                if child["status"] in TERMINAL:
                    item["finished_at"] = child.get("finished_at")
            elif item["status"] in ("queued", "running"):
                # The process may have stopped after persisting its admission
                # ID but before creating the durable workflow. Reuse that ID.
                item["status"] = "pending"
            if item["status"] in ("queued", "running"):
                active += 1
            failure |= item["status"] == "failed"
        if entry.get("cancel_requested") or (failure and entry.get("stop_on_failure")):
            for item in entry["items"]:
                if item["status"] == "pending":
                    item["status"] = (
                        "cancelled" if entry.get("cancel_requested") else "skipped"
                    )
                elif item["status"] in ("queued", "running") and entry.get(
                    "cancel_requested"
                ):
                    try:
                        actor = self._actor(
                            entry.get("_cancel_actor_id") or entry["run_as_user_id"],
                            entry["project_id"],
                        )
                        self.workflows.cancel(item["run_id"], actor)
                    except (ValueError, HTTPException, RuntimeBusy):
                        entry["error"] = (
                            "Cancellation is awaiting workflow acknowledgment"
                        )
        elif active < entry["max_concurrency"]:
            template = None
            for item in entry["items"]:
                if item["status"] != "pending" or active >= entry["max_concurrency"]:
                    continue
                try:
                    self._ready()
                    actor = self._actor(entry["run_as_user_id"], entry["project_id"])
                    template = template or self.store.get(
                        "automation_templates", entry["_template_id"]
                    )
                    if not template:
                        raise ValueError("Pinned backfill source is unavailable")
                    item["status"] = "queued"
                    self.store.put("backfills", entry)
                    parameters = {
                        **entry["parameters"],
                        entry["date_parameter"]: item["date"],
                    }
                    self.workflows.submit_captured(
                        template["_capture"],
                        parameters,
                        actor,
                        "backfill",
                        run_id=item["run_id"],
                    )
                    item["run_available"] = True
                    active += 1
                except RuntimeBusy:
                    item["status"] = "pending"
                    break
                except Exception:
                    if self.store.get("workflow_runs", item["run_id"]):
                        item["status"] = "queued"
                        item["run_available"] = True
                        active += 1
                    else:
                        item.update(
                            status="failed",
                            error="Backfill admission failed; check current access and pinned artifacts",
                        )
                        entry["error"] = item["error"]
                    break
        completed = sum(item["status"] in TERMINAL for item in entry["items"])
        entry["progress"] = {"completed": completed, "total": len(entry["items"])}
        if completed == len(entry["items"]):
            entry["status"] = (
                "cancelled"
                if entry.get("cancel_requested")
                else (
                    "succeeded"
                    if all(item["status"] == "succeeded" for item in entry["items"])
                    else "failed"
                )
            )
            entry["finished_at"] = now()
        else:
            entry["status"] = "running"
        self.store.put("backfills", entry)

    def cancel_backfill(self, identifier, user):
        with self.lock:
            value = self.store.get("backfills", identifier)
            if not value:
                raise HTTPException(404, "Backfill not found")
            self.access.require_project(user, value["project_id"], "run")
            if value["status"] not in TERMINAL:
                value["cancel_requested"] = True
                value["_cancel_actor_id"] = user["id"]
                self.store.put("backfills", value)
                self._tick_backfill(value)
            return value

    def _signature(self, job):
        return identity(
            job.get("project_id", DEFAULT_PROJECT),
            job.get("cron"),
            job.get("timezone", "UTC"),
            job.get("enabled", True),
            job.get("schedule_date_parameter"),
        )

    def validate_job(self, job, user):
        if job.get("catchup_policy", "skip") not in ("skip", "latest", "all"):
            raise ValueError("Choose skip, latest, or all for missed schedules")
        if (
            not 1 <= int(job.get("catchup_window_hours", 24)) <= 168
            or not 1 <= int(job.get("catchup_max_runs", 24)) <= 366
        ):
            raise ValueError(
                "Use a lookback of 1 to 168 hours and a catch-up queue cap of 1 to 366 runs"
            )
        if job.get("schedule_date_parameter"):
            workflow = self._definition(job, user)
            parameter = next(
                (
                    item
                    for item in workflow.get("parameter_definitions", [])
                    if item["name"] == job["schedule_date_parameter"]
                ),
                None,
            )
            if not parameter or parameter.get("type", "string") not in (
                "date",
                "string",
            ):
                raise ValueError(
                    "The scheduled date parameter must be a declared date or string input"
                )
        return job

    def track_job(self, job):
        with self.lock:
            state = self.store.get("schedule_cursors", job["id"])
            signature = self._signature(job)
            details = {
                "name": job.get("name", "Scheduled job"),
                "policy": job.get("catchup_policy", "skip"),
            }
            if state and state["signature"] == signature:
                if any(state.get(key) != value for key, value in details.items()):
                    state.update(details)
                    self.store.put("schedule_cursors", state)
                return state
            baseline = (
                now()
                if state
                else job.get("last_run_at") or job.get("created_at") or now()
            )
            return self.store.put(
                "schedule_cursors",
                {
                    "id": job["id"],
                    "project_id": job.get("project_id", DEFAULT_PROJECT),
                    "signature": signature,
                    **details,
                    "cursor": baseline,
                    "last_checked_at": None,
                    "skipped": 0,
                },
            )

    def _cron(self, job):
        if self.cron_factory:
            return self.cron_factory(job["cron"], job.get("timezone", "UTC"))
        from .main import cron_trigger

        return cron_trigger(job["cron"], job.get("timezone", "UTC"))

    def reconcile_job(self, job, clock=None):
        clock = instant(clock)
        state = self.track_job(job)
        if not job.get("enabled", True):
            return state
        start = max(
            instant(state["cursor"]),
            clock - timedelta(hours=int(job.get("catchup_window_hours", 24))),
        )
        if start >= clock:
            return state
        trigger = self._cron(job)
        candidate = trigger.get_next_fire_time(None, start + timedelta(microseconds=1))
        due = []
        for _ in range(11000):
            if candidate is None or instant(candidate) > clock:
                break
            due.append(instant(candidate))
            candidate = trigger.get_next_fire_time(candidate, candidate)
        else:
            raise ValueError("Schedule catch-up window contains too many occurrences")
        policy = job.get("catchup_policy", "skip")
        if policy not in ("skip", "latest", "all"):
            raise ValueError("Choose skip, latest, or all for missed schedules")
        cap = max(1, min(int(job.get("catchup_max_runs", 24)), 366))
        if policy == "skip":
            chosen = due[-1:] if due and (clock - due[-1]).total_seconds() < 60 else []
            state["skipped"] += len(due) - len(chosen)
        elif policy == "latest":
            chosen = due[-1:]
            state["skipped"] += max(0, len(due) - 1)
        else:
            with self.store.connect() as db:
                pending = db.execute(
                    "SELECT COUNT(*) FROM documents WHERE kind='automation_dispatches' AND job_id=? AND status IN ('pending','submitted')",
                    (job["id"],),
                ).fetchone()[0]
            chosen = due[: max(0, cap - pending)]
        for scheduled in chosen:
            self._ready()
            identifier = identity(
                "schedule", job["id"], state["signature"], scheduled.isoformat()
            )
            if not self.store.get("automation_dispatches", identifier):
                snapshot = copy.deepcopy(job)
                if job.get("schedule_date_parameter"):
                    from zoneinfo import ZoneInfo

                    snapshot["parameters"] = {
                        **snapshot.get("parameters", {}),
                        job["schedule_date_parameter"]: scheduled.astimezone(
                            ZoneInfo(job.get("timezone", "UTC"))
                        )
                        .date()
                        .isoformat(),
                    }
                self._put_dispatch(
                    identifier,
                    snapshot,
                    "schedule",
                    job["id"],
                    scheduled.isoformat(),
                    job_id=job["id"],
                )
        if policy == "all" and len(chosen) < len(due):
            state["cursor"] = chosen[-1].isoformat() if chosen else state["cursor"]
        else:
            state["cursor"] = clock.isoformat()
        state["last_checked_at"] = clock.isoformat()
        state["window_start"] = (
            clock - timedelta(hours=int(job.get("catchup_window_hours", 24)))
        ).isoformat()
        state["remaining"] = len(due) - len(chosen) if policy == "all" else 0
        return self.store.put("schedule_cursors", state)

    def dispatch_schedule(self, job_id, scheduled_for=None):
        with self.lock:
            self._ready()
            job = self.store.get("jobs", job_id)
            if not job or not job.get("enabled", True):
                return None
            self.reconcile_job(job, scheduled_for)
            self._dispatch_pending(job_id=job_id)
            return self.store.get("schedule_cursors", job_id)

    def record_scheduled_fire(self, job_id, scheduled_for):
        """Durably admit the scheduler's exact occurrence, including reschedules.

        Scheduler submission events can include occurrences beyond its execution
        grace period; leave those to the persisted cron catch-up policy.
        """
        with self.lock:
            self._ready()
            job = self.store.get("jobs", job_id)
            if not job or not job.get("enabled", True):
                return None
            scheduled = instant(scheduled_for)
            if (instant() - scheduled).total_seconds() > 60:
                return self.dispatch_schedule(job_id)
            state = self.track_job(job)
            identifier = identity(
                "schedule", job["id"], state["signature"], scheduled.isoformat()
            )
            if not self.store.get("automation_dispatches", identifier):
                snapshot = copy.deepcopy(job)
                if job.get("schedule_date_parameter"):
                    from zoneinfo import ZoneInfo

                    snapshot["parameters"] = {
                        **(snapshot.get("parameters") or {}),
                        job["schedule_date_parameter"]: scheduled.astimezone(
                            ZoneInfo(job.get("timezone", "UTC"))
                        )
                        .date()
                        .isoformat(),
                    }
                self._put_dispatch(
                    identifier,
                    snapshot,
                    "schedule",
                    job["id"],
                    scheduled.isoformat(),
                    job_id=job["id"],
                )
            self._dispatch_pending(job_id=job_id)
            return self.store.get("automation_dispatches", identifier)

    def _put_dispatch(
        self,
        identifier,
        job,
        source_kind,
        source_id,
        observed_at,
        job_id=None,
        event_ids=None,
    ):
        with self.store.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind='automation_dispatches' AND status IN ('pending','submitted')"
            ).fetchone()[0]
        if count >= self.maximum_pending:
            raise RuntimeBusy(
                "Automation queue is full; source checkpoints are retained for a later check"
            )
        return self.store.put(
            "automation_dispatches",
            {
                "id": identifier,
                "project_id": job.get("project_id", DEFAULT_PROJECT),
                "name": job.get("name", "Automated run"),
                "source_kind": source_kind,
                "source_id": source_id,
                "job_id": job_id,
                "status": "pending",
                "created_at": now(),
                "observed_at": observed_at,
                "run_id": identity("automation-child", identifier),
                "run_as_user_id": job.get("run_as_user_id"),
                "_job": job,
                "_event_ids": event_ids or [],
            },
        )

    def _dispatch_pending(self, job_id=None):
        where, arguments = (
            "kind='automation_dispatches' AND status IN ('pending','submitted')",
            [],
        )
        if job_id:
            where += " AND job_id=?"
            arguments.append(job_id)
        with self.store.connect() as db:
            # Reconcile accepted work even while the pending queue is full.
            # Otherwise pending records can starve terminal acknowledgments.
            rows = []
            for status in ("submitted", "pending"):
                rows.extend(
                    db.execute(
                        "SELECT body FROM documents WHERE "
                        + where
                        + " AND status=? ORDER BY created_at,id LIMIT 16",
                        (*arguments, status),
                    ).fetchall()
                )
        for row in rows:
            self._ready()
            entry = json.loads(row[0])
            child = self.store.get("workflow_runs", entry["run_id"])
            if child:
                entry["run_available"] = True
                entry["status"] = (
                    child["status"] if child["status"] in TERMINAL else "submitted"
                )
                entry["error"] = child.get("error")
                entry["finished_at"] = child.get("finished_at")
                self.store.put("automation_dispatches", entry)
                self._mark_schedule_admission(entry, child)
                if entry["status"] in TERMINAL:
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE automation_events SET status=? WHERE dispatch_id=?",
                            (entry["status"], entry["id"]),
                        )
                continue
            # A schedule is never allowed to overlap its prior execution.
            if entry.get("job_id") and self.store.has_active_job(entry["job_id"]):
                continue
            if entry["source_kind"] == "trigger":
                trigger = self.store.get("triggers", entry["source_id"])
                if not trigger or trigger.get("generation") != entry["_job"].get(
                    "_generation"
                ):
                    entry.update(
                        status="cancelled",
                        error="Trigger was disabled or reconfigured",
                        finished_at=now(),
                    )
                    self.store.put("automation_dispatches", entry)
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE automation_events SET status='cancelled' WHERE dispatch_id=?",
                            (entry["id"],),
                        )
                    continue
                if not trigger.get("enabled"):
                    continue
            try:
                actor = self._actor(entry["run_as_user_id"], entry["project_id"])
                if entry["source_kind"] == "schedule":
                    current = self.store.get("jobs", entry["source_id"])
                    if not current or not current.get("enabled", True):
                        entry.update(
                            status="cancelled",
                            finished_at=now(),
                            error="Schedule is disabled or removed",
                        )
                        self.store.put("automation_dispatches", entry)
                        continue
                if not entry.get("_template_id"):
                    captured = self.workflows.capture_job(entry["_job"], actor)
                    template_id = uuid4().hex
                    self.store.put(
                        "automation_templates",
                        {
                            "id": template_id,
                            "project_id": entry["project_id"],
                            "created_at": now(),
                            "_capture": captured,
                        },
                    )
                    entry["_template_id"] = template_id
                    self.store.put("automation_dispatches", entry)
                template = self.store.get("automation_templates", entry["_template_id"])
                self._ready()
                self.workflows.submit_captured(
                    template["_capture"],
                    user=actor,
                    trigger=entry["source_kind"],
                    job_id=entry.get("job_id"),
                    run_id=entry["run_id"],
                )
                entry["run_available"] = True
                entry.update(status="submitted", submitted_at=now(), error=None)
            except RuntimeBusy:
                continue
            except Exception:
                if self.store.get("workflow_runs", entry["run_id"]):
                    entry["status"] = "submitted"
                    entry["run_available"] = True
                else:
                    entry.update(
                        status="failed",
                        finished_at=now(),
                        error="Automation admission failed; check target project, current access, parameters, and pinned artifacts",
                    )
            self.store.put("automation_dispatches", entry)
            self._mark_schedule_admission(entry)
            if entry["status"] in TERMINAL:
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE automation_events SET status=? WHERE dispatch_id=?",
                        (entry["status"], entry["id"]),
                    )

    def _mark_schedule_admission(self, entry, child=None):
        if entry.get("source_kind") != "schedule" or not entry.get("job_id"):
            return
        with self.store.lock:
            job = self.store.get("jobs", entry["job_id"])
            if not job:
                return
            if child or entry["status"] == "submitted":
                admitted = (
                    (child or {}).get("created_at")
                    or entry.get("submitted_at")
                    or now()
                )
                if job.get("last_run_at") and instant(job["last_run_at"]) >= instant(
                    admitted
                ):
                    return
                job["last_run_at"] = admitted
                job.pop("last_error", None)
            elif entry["status"] == "failed":
                job["last_error"] = entry.get("error")
            else:
                return
            self.store.put("jobs", job)

    def validate_trigger(self, body, user, previous=None):
        values = (
            body.model_dump()
            if isinstance(body, TriggerInput)
            else TriggerInput.model_validate(body).model_dump()
        )
        self.access.require_project(user, values["project_id"], "edit")
        if previous and previous["project_id"] != values["project_id"]:
            raise ValueError("Create a new trigger to change its project")
        workflow = self._definition(values, user)
        if values["type"] not in (
            "file_arrival",
            "table_change",
            "workflow_completion",
        ):
            raise ValueError(
                "Choose file arrival, Delta table change, or upstream workflow completion"
            )
        if values.get("event_parameter"):
            definition = next(
                (
                    item
                    for item in workflow.get("parameter_definitions", [])
                    if item["name"] == values["event_parameter"]
                ),
                None,
            )
            if not definition or definition.get("type") != "json":
                raise ValueError("The event parameter must be declared with type json")
        parameters = {**values["parameters"]}
        if values.get("event_parameter"):
            parameters[values["event_parameter"]] = {
                "type": values["type"],
                "objects": [],
            }
        resolved = resolve_parameters(
            workflow.get("parameter_definitions", []),
            parameters,
            workflow.get("parameters", {}),
        )
        for task in workflow["tasks"]:
            notebook, _, _ = self.workflows._source(task, values["project_id"], user)
            inherited = {
                item["name"]: resolved[item["name"]]
                for item in notebook.get("parameter_definitions", [])
                if item["name"] in resolved
            }
            resolve_parameters(
                notebook.get("parameter_definitions", []),
                substitute(task.get("parameters", {}), resolved),
                inherited,
            )
        source = values["source"]
        if values["type"] == "workflow_completion":
            upstream = self.store.get("workflows", source.get("workflow_id") or "")
            if (
                not upstream
                or upstream.get("project_id", DEFAULT_PROJECT) != values["project_id"]
            ):
                raise ValueError("Choose an upstream workflow in this project")
            if any(
                state not in ("succeeded", "failed", "cancelled")
                for state in source["states"]
            ):
                raise ValueError("Choose terminal upstream workflow states")
            if values.get("workflow_id"):
                edges = {}
                for trigger in self.store.page(
                    "triggers", project_ids=[values["project_id"]], limit=1000
                )["items"]:
                    if (
                        trigger["id"] == (previous or {}).get("id")
                        or trigger["type"] != "workflow_completion"
                        or not trigger.get("workflow_id")
                    ):
                        continue
                    edges.setdefault(trigger["source"]["workflow_id"], set()).add(
                        trigger["workflow_id"]
                    )
                edges.setdefault(source["workflow_id"], set()).add(
                    values["workflow_id"]
                )
                pending, seen = [values["workflow_id"]], set()
                while pending:
                    node = pending.pop()
                    if node == source["workflow_id"]:
                        raise ValueError(
                            "Workflow completion triggers cannot create a dependency cycle"
                        )
                    if node not in seen:
                        pending.extend(edges.get(node, ()))
                        seen.add(node)
                if values["workflow_id"] == source["workflow_id"]:
                    raise ValueError(
                        "Workflow completion triggers cannot create a dependency cycle"
                    )
        else:
            scheme, bucket, _ = source_uri(source.get("path"))
            if scheme == "s3":
                item = self.access.require_connection(
                    user, source.get("connection_id"), values["project_id"], "run"
                )
                if item["type"] != "s3":
                    raise ValueError("Choose an S3 connection for this source")
            else:
                record = self.store.get("buckets", bucket)
                if (
                    not record
                    or record.get("project_id", DEFAULT_PROJECT) != values["project_id"]
                ):
                    raise ValueError("Choose a local bucket registered in this project")
                for task in workflow["tasks"]:
                    notebook, _, _ = self.workflows._source(
                        task, values["project_id"], user
                    )
                    selected = (
                        task.get("cluster_id") or notebook.get("cluster_id") or "local"
                    )
                    if selected != "local" or not cluster_record(
                        self.store, selected
                    ).get("_shared_data", True):
                        raise ValueError(
                            "Local bucket triggers require shared local compute; use S3 for remote targets"
                        )
        return values

    def save_trigger(self, body, user, previous=None):
        with self.lock:
            self._ready()
            values = self.validate_trigger(body, user, previous)
            if not previous:
                with self.store.connect() as db:
                    count = db.execute(
                        "SELECT COUNT(*) FROM documents WHERE kind='triggers'"
                    ).fetchone()[0]
                if count >= 1000:
                    raise RuntimeBusy(
                        "The workspace supports at most 1000 event triggers"
                    )
            same_source = (
                previous
                and previous["type"] == values["type"]
                and previous["source"] == values["source"]
            )
            entry = {
                **values,
                "id": (previous or {}).get("id", uuid4().hex),
                "run_as_user_id": (previous or {}).get("run_as_user_id", user["id"]),
                "created_at": (previous or {}).get("created_at", now()),
                "updated_at": now(),
                "activated_at": previous["activated_at"] if same_source else now(),
                "generation": previous["generation"] if same_source else uuid4().hex,
                "last_checked_at": (
                    previous.get("last_checked_at") if same_source else None
                ),
                "last_event_at": previous.get("last_event_at") if same_source else None,
                "last_error": None,
                "_state": (
                    copy.deepcopy(previous.get("_state", {})) if same_source else {}
                ),
            }
            if not same_source:
                self._scans.pop(entry["id"], None)
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE automation_events SET status='cancelled' WHERE trigger_id=? AND generation<>? AND status='pending'",
                        (entry["id"], entry["generation"]),
                    )
            return self.store.put("triggers", entry)

    def _s3(self, trigger, user):
        import boto3
        from botocore.config import Config

        record = self.access.require_connection(
            user, trigger["source"].get("connection_id"), trigger["project_id"], "run"
        )
        if record["type"] != "s3":
            raise ValueError("Trigger source connection is not S3")
        secrets = json.loads(
            SecretBox(self.store.directory).decrypt(record["_secrets"])
        )
        config = record["config"]
        return boto3.client(
            "s3",
            endpoint_url=config.get("endpoint_url") or None,
            region_name=config.get("region", "us-east-1"),
            aws_access_key_id=secrets["access_key"],
            aws_secret_access_key=secrets["secret_key"],
            aws_session_token=secrets.get("session_token"),
            verify=config.get("verify_tls", True),
            config=Config(
                signature_version="s3v4",
                connect_timeout=2,
                read_timeout=2,
                retries={"max_attempts": 0},
                s3={"addressing_style": config.get("addressing_style", "path")},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def _local_objects(self, root):
        # Scandir iterators remain in memory between polls. A
        # management restart rescans, while durable fingerprints deduplicate.
        scans = [os.scandir(root)]
        try:
            while scans:
                try:
                    entry = next(scans[-1])
                except StopIteration:
                    scans.pop().close()
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if len(scans) >= 64:
                            raise ValueError(
                                "Trigger sources support at most 64 nested directories"
                            )
                        scans.append(os.scandir(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        yield {
                            "key": Path(entry.path).relative_to(root).as_posix(),
                            "size": stat.st_size,
                            "modified_at": datetime.fromtimestamp(
                                stat.st_mtime, timezone.utc
                            ).isoformat(),
                            "fingerprint": f"{stat.st_mtime_ns}:{stat.st_size}",
                        }
                except FileNotFoundError:
                    continue
        finally:
            for scan in scans:
                scan.close()

    def _objects(self, trigger, user):
        scheme, bucket, prefix = source_uri(trigger["source"]["path"])
        delta = trigger["type"] == "table_change"
        if delta:
            prefix = (prefix.rstrip("/") + "/" if prefix else "") + "_delta_log/"
        if scheme == "blob":
            record = self.store.get("buckets", bucket)
            if (
                not record
                or record.get("project_id", DEFAULT_PROJECT) != trigger["project_id"]
            ):
                raise ValueError(
                    "The local source bucket is no longer assigned to this project"
                )
            root = confined_path(
                self.store.directory / "buckets", bucket + "/" + prefix, allow_root=True
            )
            if not root.is_dir():
                return [], True
            scan = self._scans.setdefault(
                trigger["id"], iter(self._local_objects(root))
            )
            items, complete = [], False
            for _ in range(self.scan_batch):
                try:
                    item = next(scan)
                except StopIteration:
                    self._scans.pop(trigger["id"], None)
                    complete = True
                    break
                item["key"] = (prefix.rstrip("/") + "/" + item["key"]).lstrip("/")
                items.append(item)
            return items, complete
        client = self._s3(trigger, user)
        arguments = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": self.scan_batch}
        token = trigger.get("_state", {}).get("continuation_token")
        if token:
            arguments["ContinuationToken"] = token
        try:
            result = client.list_objects_v2(**arguments)
        finally:
            client.close()
        trigger.setdefault("_state", {})["continuation_token"] = result.get(
            "NextContinuationToken"
        )
        return [
            {
                "key": item["Key"],
                "size": item.get("Size", 0),
                "modified_at": instant(item["LastModified"]).isoformat(),
                "fingerprint": str(item.get("ETag", ""))
                + ":"
                + str(item.get("Size", 0))
                + ":"
                + instant(item["LastModified"]).isoformat(),
            }
            for item in result.get("Contents", [])
        ], not bool(result.get("NextContinuationToken"))

    def _enqueue_event(self, db, trigger, key, fingerprint, payload, ready_at):
        event_id = identity(trigger["id"], trigger["generation"], key, fingerprint)
        exists = db.execute(
            "SELECT 1 FROM automation_events WHERE id=?", (event_id,)
        ).fetchone()
        if exists:
            return True
        pending = db.execute(
            "SELECT COUNT(*) FROM automation_events WHERE status='pending'"
        ).fetchone()[0]
        if pending >= self.maximum_pending:
            return False
        db.execute(
            "INSERT INTO automation_events(id,trigger_id,generation,project_id,payload,status,created_at,ready_at) VALUES(?,?,?,?,?,'pending',?,?)",
            (
                event_id,
                trigger["id"],
                trigger["generation"],
                trigger["project_id"],
                json.dumps(payload, allow_nan=False),
                now(),
                ready_at,
            ),
        )
        return True

    def _poll_objects(self, trigger, user, clock):
        items, complete = self._objects(trigger, user)
        delta = trigger["type"] == "table_change"
        for item in items:
            if delta:
                if not re.fullmatch(r"\d{20}\.json", Path(item["key"]).name):
                    continue
                item["version"] = int(Path(item["key"]).stem)
            elif not fnmatch.fnmatchcase(
                item["key"], trigger["source"].get("pattern", "*")
            ):
                continue
            if instant(item["modified_at"]) > clock - timedelta(
                seconds=trigger["debounce_seconds"]
            ):
                continue
            with self.store.connect() as db:
                previous = db.execute(
                    "SELECT fingerprint FROM automation_objects WHERE trigger_id=? AND generation=? AND object_key=?",
                    (trigger["id"], trigger["generation"], item["key"]),
                ).fetchone()
                if previous and previous[0] == item["fingerprint"]:
                    continue
                baseline = (
                    not trigger["include_existing"]
                    and not trigger.get("_state", {}).get("baseline_complete")
                    and previous is None
                    and instant(item["modified_at"]) < instant(trigger["activated_at"])
                )
                if not baseline and not self._enqueue_event(
                    db,
                    trigger,
                    item["key"],
                    item["fingerprint"],
                    item,
                    clock.isoformat(),
                ):
                    trigger["last_error"] = (
                        "Event queue is full; changed objects will be checked again"
                    )
                    continue
                # The event and observation commit atomically. A failed RPC
                # cannot advance this checkpoint without retaining its intent.
                db.execute(
                    "INSERT INTO automation_objects(trigger_id,generation,object_key,fingerprint) VALUES(?,?,?,?) ON CONFLICT(trigger_id,generation,object_key) DO UPDATE SET fingerprint=excluded.fingerprint",
                    (
                        trigger["id"],
                        trigger["generation"],
                        item["key"],
                        item["fingerprint"],
                    ),
                )
                if not baseline:
                    trigger["last_event_at"] = now()
        trigger.setdefault("_state", {})["scan_complete"] = complete
        if complete:
            trigger["_state"]["baseline_complete"] = True

    def _poll_workflow(self, trigger, clock):
        source = trigger["source"]
        upstream = self.store.get("workflows", source["workflow_id"])
        if (
            not upstream
            or upstream.get("project_id", DEFAULT_PROJECT) != trigger["project_id"]
        ):
            raise ValueError("The upstream workflow no longer belongs to this project")
        cursor = trigger.setdefault("_state", {}).get(
            "completion_cursor",
            ["" if trigger["include_existing"] else trigger["activated_at"], ""],
        )
        with self.store.connect() as db:
            states = source.get("states", ["succeeded"])
            rows = db.execute(
                "SELECT id,status,finished_at,name FROM documents WHERE kind='workflow_runs' AND project_id=? AND json_extract(body,'$.workflow_id')=? AND status IN ("
                + ",".join("?" for _ in states)
                + ") AND (finished_at>? OR (finished_at=? AND id>?)) ORDER BY finished_at,id LIMIT ?",
                (
                    trigger["project_id"],
                    source["workflow_id"],
                    *states,
                    cursor[0],
                    cursor[0],
                    cursor[1],
                    self.scan_batch,
                ),
            ).fetchall()
            for identifier, status, finished, name in rows:
                payload = {
                    "workflow_id": source["workflow_id"],
                    "workflow_run_id": identifier,
                    "status": status,
                    "finished_at": finished,
                    "name": name,
                }
                if not self._enqueue_event(
                    db,
                    trigger,
                    identifier,
                    status + ":" + finished,
                    payload,
                    (
                        instant(finished)
                        + timedelta(seconds=trigger["debounce_seconds"])
                    ).isoformat(),
                ):
                    trigger["last_error"] = (
                        "Event queue is full; upstream completions will be checked again"
                    )
                    break
                trigger["_state"]["completion_cursor"] = [finished, identifier]
                trigger["last_event_at"] = now()

    def _make_trigger_dispatch(self, trigger, clock=None):
        clock = instant(clock)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id,payload FROM automation_events WHERE trigger_id=? AND generation=? AND status='pending' AND ready_at<=? ORDER BY created_at,id LIMIT 100",
                (trigger["id"], trigger["generation"], clock.isoformat()),
            ).fetchall()
        if not rows:
            return
        events, selected = [], []
        for identifier, raw in rows:
            candidate = json.loads(raw)
            if events and len(json.dumps(events + [candidate])) > 12000:
                break
            events.append(candidate)
            selected.append(identifier)
        identifier = identity("trigger-batch", trigger["id"], sorted(selected))
        existing = self.store.get("automation_dispatches", identifier)
        if not existing:
            parameters = copy.deepcopy(trigger.get("parameters", {}))
            if trigger.get("event_parameter"):
                parameters[trigger["event_parameter"]] = {
                    "type": trigger["type"],
                    "source": trigger["source"].get("path"),
                    (
                        "objects"
                        if trigger["type"] != "workflow_completion"
                        else "upstream_runs"
                    ): events,
                    "event_count": len(events),
                }
            job = {
                "name": trigger["name"],
                "project_id": trigger["project_id"],
                "notebook_id": trigger.get("notebook_id"),
                "workflow_id": trigger.get("workflow_id"),
                "parameters": parameters,
                "run_as_user_id": trigger["run_as_user_id"],
                "_generation": trigger["generation"],
            }
            existing = self._put_dispatch(
                identifier, job, "trigger", trigger["id"], now(), event_ids=selected
            )
        with self.store.connect() as db:
            for event_id in selected:
                db.execute(
                    "UPDATE automation_events SET status='admitted',dispatch_id=? WHERE id=? AND status='pending'",
                    (identifier, event_id),
                )
        trigger["last_dispatch_id"] = existing["id"]

    def check_trigger(self, identifier, user=None, clock=None):
        with self.lock:
            self._ready()
            trigger = self.store.get("triggers", identifier)
            if not trigger:
                raise HTTPException(404, "Trigger not found")
            if user:
                self.access.require_project(user, trigger["project_id"], "run")
            if not trigger.get("enabled"):
                raise ValueError("Enable the trigger before checking for events")
            clock = instant(clock)
            trigger["last_error"] = None
            try:
                actor = self._actor(trigger["run_as_user_id"], trigger["project_id"])
                self._definition(trigger, actor)
                if trigger["type"] == "workflow_completion":
                    self._poll_workflow(trigger, clock)
                else:
                    self._poll_objects(trigger, actor, clock)
                self._ready()
                self._make_trigger_dispatch(trigger, clock)
            except RuntimeBusy:
                raise
            except Exception as exc:
                trigger["last_error"] = (
                    "Trigger check failed ("
                    + type(exc).__name__
                    + "); check current access and source configuration"
                )
            trigger["last_checked_at"] = clock.isoformat()
            trigger["next_check_at"] = (
                clock + timedelta(seconds=trigger["interval_seconds"])
            ).isoformat()
            return self.store.put("triggers", trigger)

    def cancel_dispatch(self, identifier, user):
        with self.lock:
            entry = self.store.get("automation_dispatches", identifier)
            if not entry:
                raise HTTPException(404, "Automated run not found")
            self.access.require_project(user, entry["project_id"], "run")
            child = self.store.get("workflow_runs", entry["run_id"])
            if child and child["status"] not in TERMINAL:
                self.workflows.cancel(child["id"], user)
                entry["cancel_requested"] = True
            elif not child and entry["status"] not in TERMINAL:
                entry.update(status="cancelled", finished_at=now())
            self.store.put("automation_dispatches", entry)
            if entry["status"] in TERMINAL:
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE automation_events SET status=? WHERE dispatch_id=?",
                        (entry["status"], entry["id"]),
                    )
            return entry

    def _page_records(self, kind, offset, limit, where="", params=()):
        with self.store.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind=? " + where, (kind, *params)
            ).fetchone()[0]
            if offset >= total:
                offset = 0
            rows = db.execute(
                "SELECT body FROM documents WHERE kind=? "
                + where
                + " ORDER BY id LIMIT ? OFFSET ?",
                (kind, *params, limit, offset),
            ).fetchall()
        return [json.loads(row[0]) for row in rows], offset + len(rows)

    def tick(self):
        with self.lock:
            self._ready()
            backfills, self._backfill_offset = self._page_records(
                "backfills",
                self._backfill_offset,
                4,
                "AND status IN ('queued','running')",
            )
            for entry in backfills:
                self._ready()
                self._tick_backfill(entry)
            self._dispatch_pending()
            if time.monotonic() >= self._next_schedules:
                jobs, self._job_offset = self._page_records(
                    "jobs", self._job_offset, 16, "AND json_extract(body,'$.enabled')=1"
                )
                for job in jobs:
                    self._ready()
                    try:
                        self.reconcile_job(job)
                    except RuntimeBusy:
                        break
                    except Exception as exc:
                        state = self.track_job(job)
                        state["last_error"] = (
                            "Schedule reconciliation failed ("
                            + type(exc).__name__
                            + ")"
                        )
                        self.store.put("schedule_cursors", state)
                self._next_schedules = time.monotonic() + 5
            triggers, self._trigger_offset = self._page_records(
                "triggers",
                self._trigger_offset,
                2,
                "AND json_extract(body,'$.enabled')=1",
            )
            for trigger in triggers:
                if trigger.get("next_check_at", "") <= now():
                    self.check_trigger(trigger["id"])

    def _loop(self):
        while not self.stopped.wait(1):
            try:
                self.tick()
            except RuntimeBusy:
                continue
            except Exception:
                # State is durable. An internal error never creates a fresh
                # admission identifier or assumes an uncertain write failed.
                continue

    def cleanup(self, cutoff):
        with self.lock, self.store.connect() as db:
            db.execute(
                "DELETE FROM automation_events WHERE status IN ('succeeded','failed','cancelled') AND created_at<?",
                (cutoff,),
            )
            db.execute(
                "DELETE FROM documents WHERE kind IN ('backfills','automation_dispatches') AND status IN ('succeeded','failed','cancelled') AND created_at<?",
                (cutoff,),
            )
            db.execute(
                "DELETE FROM documents WHERE kind='automation_templates' AND id NOT IN (SELECT json_extract(body,'$._template_id') FROM documents WHERE kind IN ('backfills','automation_dispatches') AND json_extract(body,'$._template_id') IS NOT NULL)"
            )
            db.execute(
                "DELETE FROM automation_objects WHERE NOT EXISTS (SELECT 1 FROM documents WHERE kind='triggers' AND id=automation_objects.trigger_id AND json_extract(body,'$.generation')=automation_objects.generation)"
            )


def register_automation_routes(app, authenticated, access):
    def item(request, kind, identifier, user, action="read"):
        entry = request.app.state.store.get(kind, identifier)
        if not entry:
            raise HTTPException(404, "Automation record not found")
        access.require_project(user, entry.get("project_id", DEFAULT_PROJECT), action)
        return entry

    @app.get("/api/automation/targets/{kind}/{id}")
    def target_metadata(
        kind: str, id: str, request: Request, user=Depends(authenticated)
    ):
        if kind not in ("notebook", "workflow"):
            raise HTTPException(404, "Choose a notebook or workflow target")
        with request.app.state.store.connect() as db:
            row = db.execute(
                "SELECT project_id,name,json_extract(body,'$.parameter_definitions') FROM documents WHERE kind=? AND id=?",
                (kind + "s", id),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Automation target not found")
        project_id = row[0] or DEFAULT_PROJECT
        access.require_project(user, project_id)
        return {
            "id": id,
            "project_id": project_id,
            "name": row[1],
            "parameter_definitions": json.loads(row[2]) if row[2] else [],
        }

    @app.post("/api/backfills/preview")
    def preview(body: BackfillInput, request: Request, user=Depends(authenticated)):
        return request.app.state.automation.preview_backfill(body, user)

    @app.post("/api/backfills", status_code=201)
    def create(body: BackfillInput, request: Request, user=Depends(authenticated)):
        return public(request.app.state.automation.create_backfill(body, user))

    @app.get("/api/backfills")
    def backfills(
        request: Request,
        project_id: str = DEFAULT_PROJECT,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return request.app.state.store.page(
            "backfills",
            [project_id],
            limit=limit,
            offset=offset,
            omit=("_template_id", "items"),
        )

    @app.get("/api/backfills/{id}")
    def backfill(id: str, request: Request, user=Depends(authenticated)):
        return public(item(request, "backfills", id, user))

    @app.post("/api/backfills/{id}/cancel")
    def cancel(id: str, request: Request, user=Depends(authenticated)):
        return public(request.app.state.automation.cancel_backfill(id, user))

    @app.get("/api/triggers")
    def triggers(
        request: Request,
        project_id: str = DEFAULT_PROJECT,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        page = request.app.state.store.page(
            "triggers", [project_id], limit=limit, offset=offset, omit=("_state",)
        )
        page["items"] = public(page["items"])
        return page

    @app.post("/api/triggers", status_code=201)
    def create_trigger(
        body: TriggerInput, request: Request, user=Depends(authenticated)
    ):
        return public(request.app.state.automation.save_trigger(body, user))

    @app.put("/api/triggers/{id}")
    def update_trigger(
        id: str, body: TriggerInput, request: Request, user=Depends(authenticated)
    ):
        old = item(request, "triggers", id, user, "edit")
        return public(request.app.state.automation.save_trigger(body, user, old))

    @app.delete("/api/triggers/{id}")
    def delete_trigger(id: str, request: Request, user=Depends(authenticated)):
        item(request, "triggers", id, user, "edit")
        manager = request.app.state.automation
        with manager.lock:
            request.app.state.store.delete("triggers", id)
            manager._scans.pop(id, None)
            with manager.store.connect() as db:
                db.execute(
                    "UPDATE automation_events SET status='cancelled' WHERE trigger_id=? AND status='pending'",
                    (id,),
                )
                db.execute("DELETE FROM automation_objects WHERE trigger_id=?", (id,))
        return {"ok": True}

    @app.post("/api/triggers/{id}/check")
    def check_trigger(id: str, request: Request, user=Depends(authenticated)):
        return public(request.app.state.automation.check_trigger(id, user))

    @app.get("/api/triggers/{id}/events")
    def events(
        id: str,
        request: Request,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        item(request, "triggers", id, user)
        with request.app.state.store.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM automation_events WHERE trigger_id=?", (id,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT id,status,created_at,payload,dispatch_id FROM automation_events WHERE trigger_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (id, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    "id": row[0],
                    "status": row[1],
                    "created_at": row[2],
                    "event": json.loads(row[3]),
                    "dispatch_id": row[4],
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/automation/runs")
    def automated_runs(
        request: Request,
        project_id: str = DEFAULT_PROJECT,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return request.app.state.store.page(
            "automation_dispatches",
            [project_id],
            limit=limit,
            offset=offset,
            omit=("_job", "_event_ids", "_template_id"),
        )

    @app.post("/api/automation/runs/{id}/cancel")
    def cancel_automated(id: str, request: Request, user=Depends(authenticated)):
        return public(request.app.state.automation.cancel_dispatch(id, user))

    @app.get("/api/schedules/catchup")
    def catchup(
        request: Request,
        project_id: str = DEFAULT_PROJECT,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return request.app.state.store.page(
            "schedule_cursors", [project_id], limit=limit, offset=offset
        )
