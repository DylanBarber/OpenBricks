"""Lazy independent kernels with bounded admission and idle eviction."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import threading
import time

import psutil

from .clusters import RuntimeBusy
from .resources import load_policy, normalize_policy, run_options


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class QueueView:
    def __init__(self, runtime):
        self.runtime = runtime

    def qsize(self):
        with self.runtime.store.connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind='runs' AND status='queued' AND COALESCE(json_extract(body,'$.cluster_id'),'local')='local'"
            ).fetchone()[0]

    def empty(self):
        return self.qsize() == 0


class SessionRuntime:
    """The compute service owns process groups, never the Docker daemon.

    Admission reservations and a process-tree RSS guard complement the hard
    outer container ceiling. RSS sampling can have a short transient overshoot.
    Spark's local Derby catalog is leased by one initialized JVM at a time.
    """

    def __init__(self, store, kernel_factory):
        self.store, self.kernel_factory = store, kernel_factory
        self.policy = load_policy(store)
        self.state_lock = threading.RLock()
        self.condition = threading.Condition(self.state_lock)
        self.environment_busy = False
        self.environment_ready = threading.Event()
        self.environment_ready.set()
        self.stopping = False
        self.started = False
        self.sessions = {}
        self._active = {}
        self._pending = {}
        self._waiting = {}
        self._sequence = 0
        self._latest = None
        self._legacy_process = None
        self._last_activity = time.monotonic()
        self._spark_owner = None
        from .spark_catalog import execution_catalog

        self.execution_catalog = execution_catalog()
        self.recent = deque(maxlen=32)
        self.timeout = 3600
        import os

        self.timeout = int(os.getenv("OPENBRICKS_RUN_TIMEOUT", "3600"))
        self.idle_timeout = self.policy["idle_timeout_seconds"]
        self.queue = QueueView(self)
        for run in store.active_runs():
            if run.get("cluster_id", "local") == "local":
                run.update(
                    error_class=(
                        "unknown_outcome" if run["status"] == "running" else "restart"
                    ),
                    status="failed",
                    finished_at=timestamp(),
                    error="Compute restarted before this run completed. Inspect any prior writes before starting a new attempt.",
                )
                store.put("runs", run)
        self.thread = threading.Thread(
            target=self._sweep, daemon=True, name="openbricks-session-supervisor"
        )

    @property
    def active_id(self):
        with self.state_lock:
            return next(iter(self._active), None)

    @property
    def active_count(self):
        with self.state_lock:
            return len(self._active)

    @property
    def queued_count(self):
        return self.queue.qsize()

    @property
    def process(self):
        with self.state_lock:
            return self._latest.process if self._latest else self._legacy_process

    @process.setter
    def process(self, value):
        self._legacy_process = value

    @property
    def last_activity(self):
        return self._latest.last_activity if self._latest else self._last_activity

    @last_activity.setter
    def last_activity(self, value):
        self._last_activity = value
        if self._latest:
            self._latest.last_activity = value

    def start(self):
        with self.state_lock:
            self.started = True
            for kernel in self.sessions.values():
                if not kernel.thread.is_alive():
                    kernel.start()
            self.thread.start()

    def submit(
        self,
        notebook,
        cell_id=None,
        trigger="manual",
        job_id=None,
        cluster_id=None,
        run_id=None,
        connections=None,
        parameters=None,
        timeout_seconds=None,
        environment_id=None,
        environment=None,
        provenance=None,
        resource_options=None,
        notebook_secrets=None,
    ):
        from .environments import (
            capture_environment,
            get_environment,
            validate_manifest,
        )

        with self.condition:
            if self.stopping:
                raise RuntimeBusy("Compute is stopping; try again after maintenance")
            if run_id:
                previous = self.store.get("runs", run_id)
                if previous:
                    if previous["notebook_id"] != notebook["id"]:
                        raise ValueError("Run identifier belongs to another notebook")
                    return previous
            if self.environment_busy and trigger != "schedule":
                raise RuntimeBusy(
                    "Python packages are being updated; wait for maintenance to finish"
                )
            if len(self._pending) >= 100:
                raise RuntimeBusy("Compute's bounded run queue is full")
            options = run_options(
                resource_options, notebook, self.policy, trigger=trigger, job_id=job_id
            )
            project = notebook.get("project_id", "default")
            manifest = (
                validate_manifest(environment)
                if environment is not None
                else (
                    get_environment(self.store, environment_id, project)
                    if environment_id
                    else capture_environment(self.store, project)
                )
            )
            # Jobs get one-shot kernels; interactive cells in the same notebook
            # keep a session until its environment changes, eviction, or reset.
            identity = [
                project,
                options["owner_id"],
                notebook["id"],
                manifest["id"],
                options["memory_mb"],
            ]
            if options["mode"] == "job":
                from uuid import uuid4

                identity.append(run_id or uuid4().hex)
            session_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
            kernel = self.sessions.get(session_id)
            if kernel is None:
                kernel = self.kernel_factory(
                    self.store, coordinator=self, recover=False
                )
                kernel.session_info = {
                    "id": session_id,
                    "project_id": project,
                    "owner_id": options["owner_id"],
                    "notebook_id": notebook["id"],
                    "notebook_name": notebook["name"],
                    "environment_id": manifest["id"],
                    "mode": options["mode"],
                    "memory_mb": options["memory_mb"],
                    "created_at": timestamp(),
                }
                kernel.has_spark = False
                self.sessions[session_id] = kernel
            kernel.timeout = self.timeout
            result = kernel.submit(
                notebook,
                cell_id,
                trigger,
                job_id,
                cluster_id=cluster_id,
                run_id=run_id,
                connections=connections,
                parameters=parameters,
                timeout_seconds=timeout_seconds,
                environment=manifest,
                provenance=provenance,
                resource_options=options,
                session_id=session_id,
                notebook_secrets=notebook_secrets,
            )
            self._sequence += 1
            self._pending[result["id"]] = {
                "id": result["id"],
                "session_id": session_id,
                "project_id": project,
                "options": options,
                "sequence": self._sequence,
            }
            if self.started and not kernel.thread.is_alive():
                kernel.start()
            self.condition.notify_all()
            return result

    def _can_admit(self, item):
        if self.environment_busy or len(self._active) >= self.policy["max_concurrent"]:
            return False, "Waiting for compute capacity"
        project = item["project_id"]
        limits = self.policy["project_limits"].get(project, {})
        active = list(self._active.values())
        project_active = [value for value in active if value["project_id"] == project]
        if len(project_active) >= limits.get(
            "max_concurrent", self.policy["max_concurrent"]
        ):
            return False, "Waiting for the project's concurrency budget"
        required = item["options"]["memory_mb"]
        if (
            required + sum(value["options"]["memory_mb"] for value in active)
            > self.policy["memory_budget_mb"]
        ):
            return False, "Waiting for the compute memory budget"
        if required + sum(
            value["options"]["memory_mb"] for value in project_active
        ) > limits.get("memory_budget_mb", self.policy["memory_budget_mb"]):
            return False, "Waiting for the project's memory budget"
        return True, None

    def admit(self, kernel, run_id):
        with self.condition:
            self._waiting[run_id] = self._pending.get(run_id)
            reason_saved = None
            while not self.stopping and not kernel.stopping:
                with self.store.connect() as db:
                    row = db.execute(
                        "SELECT status FROM documents WHERE kind='runs' AND id=?",
                        (run_id,),
                    ).fetchone()
                if not row or row[0] != "queued":
                    return False
                item = self._pending.get(run_id)
                if item is None:
                    return False

                def order(value):
                    bonus = (
                        self.policy["project_limits"]
                        .get(value["project_id"], {})
                        .get("priority", 0)
                    )
                    return (-value["options"]["priority"] - bonus, value["sequence"])

                candidates = sorted(
                    (value for value in self._waiting.values() if value), key=order
                )
                selected = next(
                    (value for value in candidates if self._can_admit(value)[0]), None
                )
                if selected and selected["id"] == run_id:
                    self._evict_for(kernel)
                    self._active[run_id] = item
                    self._waiting.pop(run_id, None)
                    self._latest = kernel
                    return True
                _, reason = self._can_admit(item)
                reason = reason or "Waiting behind higher-priority work"
                if reason_saved != reason:
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE documents SET body=json_set(body,'$.queued_reason',?) WHERE kind='runs' AND id=? AND status='queued'",
                            (reason, run_id),
                        )
                    reason_saved = reason
                self.condition.wait(0.2)
            return False

    def _evict_for(self, target):
        resident = [
            kernel
            for kernel in self.sessions.values()
            if kernel.process is not None and kernel.process.poll() is None
        ]
        while True:
            total = sum(kernel.session_info["memory_mb"] for kernel in resident)
            extra = 0 if target in resident else target.session_info["memory_mb"]
            same = [
                kernel
                for kernel in resident
                if kernel.session_info["project_id"]
                == target.session_info["project_id"]
            ]
            project_budget = (
                self.policy["project_limits"]
                .get(target.session_info["project_id"], {})
                .get("memory_budget_mb", self.policy["memory_budget_mb"])
            )
            project_total = sum(kernel.session_info["memory_mb"] for kernel in same)
            if (
                total + extra <= self.policy["memory_budget_mb"]
                and project_total + extra <= project_budget
                and len(resident) + (target not in resident)
                <= self.policy["max_sessions"]
            ):
                return
            active_sessions = {item["session_id"] for item in self._active.values()}
            choices = [
                kernel
                for kernel in resident
                if kernel is not target
                and kernel.session_info["id"] not in active_sessions
            ]
            if not choices:
                return  # Active reservations were checked by _can_admit.
            candidate = min(choices, key=lambda value: value.last_activity)
            self._evict(candidate, "memory_pressure")
            resident.remove(candidate)

    def _evict(self, kernel, reason):
        if self._spark_owner is kernel:
            self._spark_owner = None
        kernel.reset()
        kernel.has_spark = False
        self.recent.append(
            {
                **kernel.session_info,
                "status": "stopped",
                "reason": reason,
                "finished_at": timestamp(),
            }
        )

    def acquire_spark(self, kernel, run_id):
        began, waiting = time.monotonic(), False
        with self.condition:
            while not self.stopping and not kernel.stopping:
                if (
                    run_id not in self._active
                    or kernel.process is None
                    or kernel.process.poll() is not None
                ):
                    return False
                if not self.execution_catalog["shared_derby"]:
                    kernel.has_spark = True
                    return True
                owner = self._spark_owner
                if owner is None or owner is kernel:
                    self._spark_owner = kernel
                    kernel.has_spark = True
                    if waiting:
                        self._spark_wait(kernel, run_id, began, finished=True)
                    return True
                owner_active = any(
                    item["session_id"] == owner.session_info["id"]
                    for item in self._active.values()
                )
                if not owner_active:
                    self._evict(owner, "local_catalog_handoff")
                    self._spark_owner = kernel
                    kernel.has_spark = True
                    if waiting:
                        self._spark_wait(kernel, run_id, began, finished=True)
                    return True
                if not waiting:
                    self._spark_wait(kernel, run_id, began)
                    waiting = True
                self.condition.wait(0.2)
            return False

    def _spark_wait(self, kernel, run_id, began, finished=False):
        with kernel.state_lock:
            run = getattr(kernel, "_running_run", None)
            if run is not None and run["id"] == run_id:
                run["profile"]["spark_wait_ms"] = round(
                    (time.monotonic() - began) * 1000
                )
                run["profile"]["wait_reason"] = (
                    None if finished else "Waiting for the local Hive catalog owner"
                )
                self.store.put("runs", run)

    def finished(self, kernel, run_id):
        with self.condition:
            self._active.pop(run_id, None)
            self._pending.pop(run_id, None)
            self._waiting.pop(run_id, None)
            if kernel.session_info["mode"] == "job":
                self._evict(kernel, "job_completed")
                kernel.stopping = True
                self.sessions.pop(kernel.session_info["id"], None)
            if not kernel.has_spark and self._spark_owner is kernel:
                self._spark_owner = None
            self.condition.notify_all()

    def cancel(self, run_id):
        with self.condition:
            item = self._pending.get(run_id)
            if item:
                kernel = self.sessions.get(item["session_id"])
                if kernel:
                    result = kernel.cancel(run_id)
                    self.condition.notify_all()
                    return result
            run = self.store.get("runs", run_id)
            if not run:
                raise ValueError("Run not found")
            return run

    @contextmanager
    def environment_change(self):
        with self.condition:
            if self.environment_busy or self._pending or self.store.has_active_runs():
                raise RuntimeBusy(
                    "Wait for queued and running notebooks before changing packages or workspace data"
                )
            self.environment_busy = True
            self.environment_ready.clear()
        try:
            yield
        finally:
            with self.condition:
                self.environment_busy = False
                self.environment_ready.set()
                self.condition.notify_all()

    def reset(self):
        with self.condition:
            for kernel in list(self.sessions.values()):
                self._evict(kernel, "reset")
            self._legacy_process = None
            self.condition.notify_all()

    def _expire_idle(self):
        with self.condition:
            if self.stopping or not self.idle_timeout or self.environment_busy:
                return False
            expired = False
            pending_sessions = {item["session_id"] for item in self._pending.values()}
            for kernel in self.sessions.values():
                if (
                    kernel.process is not None
                    and kernel.session_info["id"] not in pending_sessions
                    and time.monotonic() - kernel.last_activity >= self.idle_timeout
                ):
                    self._evict(kernel, "idle_timeout")
                    expired = True
            if (
                self._legacy_process is not None
                and not self._pending
                and time.monotonic() - self._last_activity >= self.idle_timeout
            ):
                self.reset()
                expired = True
            return expired

    def _sweep(self):
        while not self.stopping:
            self._expire_idle()
            with self.condition:
                pending = {item["session_id"] for item in self._pending.values()}
                obsolete = [
                    kernel
                    for kernel in self.sessions.values()
                    if kernel.process is None
                    and kernel.session_info["id"] not in pending
                ]
                # Retire cold kernel supervisors too; only active/queued work
                # may temporarily create up to the bounded queue's 100 threads.
                for kernel in obsolete[: -self.policy["max_sessions"]]:
                    kernel.stopping = True
                    self.sessions.pop(kernel.session_info["id"], None)
            time.sleep(0.25)

    def resource_policy(self):
        return {
            **self.policy,
            "project_limits": {
                key: dict(value) for key, value in self.policy["project_limits"].items()
            },
        }

    def update_resource_policy(self, value):
        with self.condition:
            if self._pending or self.environment_busy:
                raise RuntimeBusy(
                    "Wait for queued and running work before changing resource budgets"
                )
            policy = normalize_policy(value)
            self.reset()
            self.policy = policy
            self.idle_timeout = policy["idle_timeout_seconds"]
            self.store.put(
                "settings",
                {
                    "id": "compute_resource_policy",
                    "policy": policy,
                    "updated_at": timestamp(),
                },
            )
            return self.resource_policy()

    def resource_status(self):
        with self.condition:
            sessions = []
            for kernel in self.sessions.values():
                process = kernel.process
                if process is None or process.poll() is not None:
                    continue
                rss = 0
                try:
                    parent = psutil.Process(process.pid)
                    rss = parent.memory_info().rss + sum(
                        child.memory_info().rss
                        for child in parent.children(recursive=True)
                        if child.is_running()
                    )
                except psutil.Error:
                    pass
                sessions.append(
                    {
                        **kernel.session_info,
                        "status": "running" if kernel.active_id else "idle",
                        "active_run_id": kernel.active_id,
                        "pid": process.pid,
                        "rss_mb": round(rss / 1024**2, 2),
                        "has_spark": kernel.has_spark,
                        "idle_seconds": round(
                            max(0, time.monotonic() - kernel.last_activity), 1
                        ),
                    }
                )
            return {
                "policy": self.resource_policy(),
                "sessions": sessions,
                "recent_sessions": list(self.recent),
                "active_runs": len(self._active),
                "queued_runs": self.queue.qsize(),
                "memory_reserved_mb": sum(value["memory_mb"] for value in sessions),
                "memory_enforcement": "process-tree-rss-guard-and-container-ceiling",
                "local_spark_catalog_concurrency": (
                    1
                    if self.execution_catalog["shared_derby"]
                    else self.policy["max_concurrent"]
                ),
                "spark_catalog_mode": self.execution_catalog["mode"],
                "spark_default_catalog": self.execution_catalog["default_catalog"],
            }

    def stop_session(self, identifier):
        with self.condition:
            kernel = self.sessions.get(identifier)
            if not kernel:
                raise ValueError("Session not found")
            for run_id, value in list(self._pending.items()):
                if value["session_id"] == identifier:
                    kernel.cancel(run_id)
            self._evict(kernel, "user_stopped")
            self.condition.notify_all()
            return {"ok": True}

    def close(self):
        with self.condition:
            self.stopping = True
            self.environment_ready.set()
            kernels = list(self.sessions.values())
            self.condition.notify_all()
        for kernel in kernels:
            kernel.close()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def sync_wheel(self, path):
        return None
