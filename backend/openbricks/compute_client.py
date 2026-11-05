"""Durable control-plane routing to one queue per compute service."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx

from .clusters import RuntimeBusy, client_for, cluster_record, ensure_local_cluster, timestamp as now


def connection_snapshot(store):
    from .connectors import connections_for_runtime
    return connections_for_runtime(store)


def pending_runs(store, cluster_id=None):
    sql = "SELECT body FROM documents WHERE kind='runs' AND status IN ('queued','running')"
    params = []
    if cluster_id is not None:
        sql += " AND COALESCE(json_extract(body,'$.cluster_id'),'local')=?"
        params.append(cluster_id)
    sql += " ORDER BY rowid"
    with store.connect() as db:
        return [json.loads(row[0]) for row in db.execute(sql, params).fetchall()]


class QueueView:
    def __init__(self, runtime):
        self.runtime = runtime

    def qsize(self):
        return self.runtime.queued_count

    def empty(self):
        return self.qsize() == 0


class ClusterRuntime:
    def __init__(self, store, local_engine=None):
        self.store = store
        self.local_engine = local_engine
        self.state_lock = threading.RLock()
        self.environment_busy = False
        self.environment_ready = threading.Event()
        self.environment_ready.set()
        self.stopped = threading.Event()
        self.queues = {}
        self.threads = {}
        self._active = {}
        self._run_locks = {}
        self.queue = QueueView(self)
        self.started = False
        self._timeout = int(os.getenv("OPENBRICKS_RUN_TIMEOUT", "3600"))
        ensure_local_cluster(store)

    @property
    def timeout(self):
        return self.local_engine.timeout if self.local_engine else self._timeout

    @timeout.setter
    def timeout(self, value):
        self._timeout = value
        if self.local_engine:
            self.local_engine.timeout = value

    @property
    def active_id(self):
        if self.local_engine and self.local_engine.active_id:
            return self.local_engine.active_id
        with self.state_lock:
            return next(iter(self._active.values()), None)

    @property
    def active_count(self):
        with self.state_lock:
            return len(self._active) + int(bool(self.local_engine and self.local_engine.active_id))

    @property
    def queued_count(self):
        with self.store.connect() as db:
            return db.execute("SELECT COUNT(*) FROM documents WHERE kind='runs' AND status='queued'").fetchone()[0]

    @property
    def process(self):
        return self.local_engine.process if self.local_engine else None

    def start(self):
        self.started = True
        if self.local_engine:
            self.local_engine.start()
        # Reattach to idempotent compute run IDs. Compute can continue running
        # while the management container is restarted.
        for run in pending_runs(self.store):
            if self.local_engine and run.get("cluster_id", "local") == "local":
                continue
            if not run.get("_notebook"):
                run.update(status="failed", finished_at=now(), error="Legacy run cannot be resumed after the compute architecture changed")
                self.store.put("runs", run)
                continue
            self._enqueue(run)

    def submit(self, notebook, cell_id=None, trigger="manual", job_id=None, cluster_id=None):
        selected = cluster_id or notebook.get("cluster_id") or "local"
        with self.state_lock:
            if self.environment_busy and trigger != "schedule":
                raise RuntimeBusy("The shared Python environment is being updated. Try again when the package operation finishes")
            if self.local_engine and selected == "local":
                return self.local_engine.submit(notebook, cell_id, trigger, job_id, cluster_id="local")
            entry = cluster_record(self.store, selected)
            if not entry.get("url"):
                raise ValueError("The selected compute cluster has no service endpoint")
            if cell_id and not any(cell["id"] == cell_id for cell in notebook["cells"]):
                raise ValueError("Cell not found")
            if len(pending_runs(self.store, selected)) >= 100:
                raise RuntimeBusy("The selected cluster's run queue is full")
            run = {"id": uuid4().hex, "notebook_id": notebook["id"], "notebook_name": notebook["name"],
                   "cluster_id": selected, "cluster_name": entry["name"], "trigger": trigger, "job_id": job_id,
                   "status": "queued", "created_at": now(), "started_at": None, "finished_at": None,
                   "duration_ms": None, "outputs": [], "error": None,
                   "_notebook": {key: value for key, value in notebook.items() if not key.startswith("_")},
                   "_cell_id": cell_id}
            self.store.put("runs", run)
            self._enqueue(run)
            return run

    def _enqueue(self, run):
        selected = run.get("cluster_id", "local")
        with self.state_lock:
            if selected not in self.queues:
                self.queues[selected] = queue.Queue()
            self.queues[selected].put(run["id"])
            if self.started and (selected not in self.threads or not self.threads[selected].is_alive()):
                thread = threading.Thread(target=self._loop, args=(selected,), name=f"openbricks-cluster-{selected}", daemon=True)
                self.threads[selected] = thread
                thread.start()

    @contextmanager
    def environment_change(self):
        with self.state_lock:
            if self.environment_busy or pending_runs(self.store):
                raise RuntimeBusy("Wait for queued and running notebooks before changing the shared Python environment")
            self.environment_busy = True
            self.environment_ready.clear()
        try:
            if self.local_engine:
                with self.local_engine.environment_change():
                    yield
            else:
                yield
        finally:
            with self.state_lock:
                self.environment_busy = False
                self.environment_ready.set()

    def reset(self):
        if self.local_engine:
            self.local_engine.reset()
            return
        entry = cluster_record(self.store, "local")
        try:
            with client_for(self.store, entry) as client:
                response = client.post("/environment/reset")
                if response.status_code == 409:
                    raise RuntimeBusy("The compute service is busy; wait before changing packages")
                response.raise_for_status()
        except httpx.HTTPError:
            raise ValueError("The bundled compute service is unavailable; package changes were not applied")

    def sync_wheel(self, path):
        # The bundled deployment mounts the same artifact directory. Remote
        # agents synchronize installed wheels lazily before each notebook run.
        if self.local_engine:
            return
        entry = cluster_record(self.store, "local")
        if not entry.get("_shared_data"):
            self._sync_packages(entry)

    def _installed_wheels(self):
        wheels = self.store.directory / "wheels"
        for package in self.store.list("packages"):
            filename = package.get("filename")
            if not filename or Path(filename).name != filename:
                continue
            matches = list(wheels.rglob(filename))
            if not matches:
                raise ValueError("An installed wheel artifact is missing; upload or rebuild that package before using remote compute")
            path = max(matches, key=lambda item: item.stat().st_mtime)
            if path.is_symlink() or not path.resolve().is_relative_to(wheels.resolve()):
                raise ValueError("Invalid package artifact path")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            yield path, digest.hexdigest()

    def _sync_packages(self, entry):
        if entry.get("_shared_data"):
            return
        wheels = list(self._installed_wheels())
        if not wheels:
            return
        with client_for(self.store, entry) as client:
            response = client.get("/environment/wheels")
            response.raise_for_status()
            installed = {item["filename"]: item["sha256"] for item in response.json().get("items", [])}
            for path, digest in wheels:
                if installed.get(path.name) == digest:
                    continue
                with path.open("rb") as stream:
                    response = client.post("/environment/wheels", files={"file": (path.name, stream, "application/octet-stream")}, timeout=180)
                if response.status_code == 409:
                    raise RuntimeBusy("Compute is busy preparing its Python package environment")
                response.raise_for_status()

    def _loop(self, selected):
        work = self.queues[selected]
        while not self.stopped.is_set():
            try:
                run_id = work.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                while not self.environment_ready.wait(0.5):
                    if self.stopped.is_set():
                        return
                if self.stopped.is_set():
                    return
                run = self.store.get("runs", run_id)
                if not run or run["status"] not in ("queued", "running"):
                    continue
                self._execute(run, selected)
            finally:
                with self.state_lock:
                    self._active.pop(selected, None)
                work.task_done()

    def _execute(self, run, selected):
        run_id = run["id"]
        try:
            entry = cluster_record(self.store, selected)
            # A restarted control plane must reattach without attempting to
            # alter an environment while the agent's old run is still active.
            if not run.get("_remote_submitted"):
                self._sync_packages(entry)
            connections = connection_snapshot(self.store)
            payload = {"run_id": run_id, "notebook": run["_notebook"], "cell_id": run.get("_cell_id"),
                       "trigger": run["trigger"], "job_id": run.get("job_id"), "connections": connections}
            with client_for(self.store, entry) as client:
                with self.state_lock:
                    run_lock = self._run_locks.setdefault(run_id, threading.RLock())
                with run_lock:
                    with self.state_lock:
                        latest = self.store.get("runs", run_id)
                        if not latest or latest["status"] == "cancelled":
                            return
                    if run.get("_remote_submitted"):
                        # An acknowledged run is recovered by ID rather than
                        # resubmitted after a management restart.
                        response = None
                    else:
                        uncertain = False
                        for attempt in range(3):
                            try:
                                response = client.post("/runs", json=payload)
                                response.raise_for_status()
                                break
                            except httpx.ConnectError:
                                if not uncertain:
                                    raise
                            except httpx.TransportError:
                                # A lost response may follow successful code
                                # execution. Reuse the same idempotency ID.
                                uncertain = True
                            if self.stopped.wait(0.3 * (attempt + 1)):
                                return
                    with self.state_lock:
                        run["_remote_submitted"] = True
                        self.store.put("runs", run)
                # No connection secrets are retained in durable run metadata.
                payload.clear()
                connections = None
                deadline = time.monotonic() + self._timeout + 120
                while not self.stopped.is_set():
                    try:
                        response = client.get(f"/runs/{run_id}")
                        if response.status_code == 404:
                            raise ValueError("Compute has no durable record of this run. It was not automatically re-executed")
                        response.raise_for_status()
                    except httpx.HTTPError:
                        # Leave execution pending while disconnected. Marking
                        # it failed could encourage a duplicate SQL write.
                        with self.state_lock:
                            latest = self.store.get("runs", run_id)
                            if not latest or latest["status"] == "cancelled":
                                return
                            message = "Compute connection interrupted; execution state is being reconciled. Do not start a replacement run until its outcome is known"
                            if latest.get("error") != message:
                                run["error"] = message
                                self.store.put("runs", run)
                        self.stopped.wait(2)
                        continue
                    remote = response.json()
                    if remote.get("id") != run_id or remote.get("status") not in ("queued", "running", "succeeded", "failed", "cancelled"):
                        raise ValueError("Invalid compute response")
                    with self.state_lock:
                        latest = self.store.get("runs", run_id)
                        if not latest or latest["status"] == "cancelled":
                            return
                        for field in ("status", "started_at", "finished_at", "duration_ms", "outputs", "error"):
                            if field in remote:
                                run[field] = remote[field]
                        run["error"] = remote.get("error")
                        if run["status"] == "running":
                            self._active[selected] = run_id
                        self.store.put("runs", run)
                    if run["status"] not in ("queued", "running"):
                        return
                    if time.monotonic() > deadline:
                        try:
                            cancelled = client.post(f"/runs/{run_id}/cancel")
                            cancelled.raise_for_status()
                        except httpx.HTTPError:
                            self.stopped.wait(2)
                            continue
                    self.stopped.wait(0.7)
        except Exception as exc:
            if self.stopped.is_set():
                return
            with self.state_lock:
                latest = self.store.get("runs", run_id)
                if latest and latest["status"] == "cancelled":
                    return
                message = str(exc) if isinstance(exc, (RuntimeBusy, ValueError)) and not isinstance(exc, httpx.HTTPError) else "Compute service could not complete the request. Check cluster connectivity and authentication, then retry the notebook"
                run.update(status="failed", finished_at=now(), error=message)
                self.store.put("runs", run)

    def cancel(self, run_id):
        with self.state_lock:
            run_lock = self._run_locks.setdefault(run_id, threading.RLock())
        with run_lock:
            run = self.store.get("runs", run_id)
            if not run:
                raise ValueError("Run not found")
            if self.local_engine and run.get("cluster_id", "local") == "local":
                return self.local_engine.cancel(run_id)
            if run["status"] not in ("queued", "running"):
                return run
            if run.get("_remote_submitted"):
                try:
                    with client_for(self.store, cluster_record(self.store, run["cluster_id"])) as client:
                        response = client.post(f"/runs/{run_id}/cancel")
                        response.raise_for_status()
                        remote = response.json()
                except httpx.HTTPError:
                    # Do not claim remote code stopped when its service cannot
                    # acknowledge cancellation.
                    raise ValueError("Cannot confirm cancellation because the compute service is unavailable; the run may still be executing")
                if remote.get("status") in ("succeeded", "failed"):
                    with self.state_lock:
                        latest = self.store.get("runs", run_id) or run
                        for field in ("status", "started_at", "finished_at", "duration_ms", "outputs", "error"):
                            if field in remote:
                                latest[field] = remote[field]
                        self.store.put("runs", latest)
                        return latest
            run.update(status="cancelled", finished_at=now(), error="Cancelled by user")
            with self.state_lock:
                self.store.put("runs", run)
            return run

    def close(self):
        self.stopped.set()
        self.environment_ready.set()
        if self.local_engine:
            self.local_engine.close()
        for thread in self.threads.values():
            if thread.is_alive():
                thread.join(timeout=2)
