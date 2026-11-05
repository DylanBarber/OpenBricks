"""One durable run queue and one persistent Spark interpreter for the host."""
from __future__ import annotations

import json
import os
import platform
import queue
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .clusters import RuntimeBusy


def now():
    return datetime.now(timezone.utc).isoformat()


class LocalRuntime:
    def __init__(self, store):
        self.store = store
        self.queue = queue.Queue()
        self.process = None
        self.process_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.environment_busy = False
        self.environment_ready = threading.Event()
        self.environment_ready.set()
        self.active_id = None
        self.cancelled = set()
        self.stopping = False
        self.timeout = int(os.getenv("OPENBRICKS_RUN_TIMEOUT", "3600"))
        self.idle_timeout = max(0, int(os.getenv("OPENBRICKS_IDLE_TIMEOUT_SECONDS", "900")))
        self.last_activity = time.monotonic()
        self.log = None
        self._connection_snapshots = {}
        for run in self.store.active_runs():
            if run["status"] in ("queued", "running") and run.get("cluster_id", "local") == "local":
                run.update(status="failed", finished_at=now(), error="Server restarted before this run completed. Run it again to start a new session.")
                self.store.put("runs", run)
        self.thread = threading.Thread(target=self._loop, daemon=True, name="openbricks-run-queue")

    def start(self):
        self.thread.start()

    def submit(self, notebook, cell_id=None, trigger="manual", job_id=None, cluster_id=None, run_id=None, connections=None):
        with self.state_lock:
            if self.environment_busy and trigger != "schedule":
                raise RuntimeBusy("The shared Python environment is being updated. Try running again when the package operation finishes")
            return self._submit(notebook, cell_id, trigger, job_id, cluster_id, run_id, connections)

    @contextmanager
    def environment_change(self):
        with self.state_lock:
            if self.environment_busy or self.active_id or self.store.has_active_runs():
                raise RuntimeBusy("Wait for queued and running notebooks before changing the shared Python environment")
            self.environment_busy = True
            self.environment_ready.clear()
        try:
            yield
        finally:
            with self.state_lock:
                self.environment_busy = False
                self.environment_ready.set()

    def _submit(self, notebook, cell_id=None, trigger="manual", job_id=None, cluster_id=None, run_id=None, connections=None):
        if run_id:
            previous = self.store.get("runs", run_id)
            if previous:
                if previous["notebook_id"] != notebook["id"]:
                    raise ValueError("Run identifier belongs to another notebook")
                return previous
        if cell_id:
            cells = [cell for cell in notebook["cells"] if cell["id"] == cell_id]
            if not cells:
                raise ValueError("Cell not found")
        else:
            cells = notebook["cells"]
        if self.queue.qsize() >= 100:
            raise ValueError("Run queue is full; wait for a run to finish")
        run = {"id": run_id or uuid4().hex, "notebook_id": notebook["id"], "notebook_name": notebook["name"], "cluster_id": cluster_id or "local", "cluster_name": "Local compute", "status": "queued", "trigger": trigger,
               "job_id": job_id, "created_at": now(), "started_at": None, "finished_at": None, "duration_ms": None, "outputs": [], "error": None,
               "_cells": cells, "_reset_scope": not bool(cell_id)}
        self.store.put("runs", run)
        if connections is not None:
            self._connection_snapshots[run["id"]] = connections
        self.queue.put(run["id"])
        return run

    def cancel(self, run_id):
        with self.state_lock:
            run = self.store.get("runs", run_id)
            if not run:
                raise ValueError("Run not found")
            if run["status"] not in ("queued", "running"):
                return run
            self.cancelled.add(run_id)
            active = self.active_id == run_id
            run.update(status="cancelled", finished_at=now(), error="Cancelled by user. The shared interpreter was reset." if active else "Cancelled before execution.")
            self.store.put("runs", run)
            # Hold the queue transition lock so this reset cannot kill a later
            # run after the cancelled run exits.
            if active:
                self.reset()
        return self.store.get("runs", run_id)

    def reset(self):
        with self.process_lock:
            process = self.process
            self.process = None
            if process and process.poll() is None:
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError, subprocess.SubprocessError):
                    process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            if self.log:
                self.log.close()
                self.log = None

    def _get_process(self):
        with self.process_lock:
            if self.process and self.process.poll() is None:
                return self.process
            log_dir = self.store.directory / "logs"
            log_dir.mkdir(exist_ok=True)
            self.log = (log_dir / "worker.log").open("a", encoding="utf-8")
            python_paths = [str(self.store.directory / "site-packages"), *[str(self.store.directory / "repositories" / repo["id"]) for repo in self.store.list("repositories")]]
            if os.getenv("PYTHONPATH"):
                python_paths.append(os.environ["PYTHONPATH"])
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "DATA_DIR": str(self.store.directory), "SPARK_LOCAL_IP": "127.0.0.1", "PYTHONPATH": os.pathsep.join(python_paths)}
            self.process = subprocess.Popen([sys.executable, "-u", "-m", "openbricks.worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                            text=True, encoding="utf-8", bufsize=1, env=env, start_new_session=os.name != "nt")
            return self.process

    def _loop(self):
        while not self.stopping:
            try:
                run_id = self.queue.get(timeout=0.5)
            except queue.Empty:
                self._expire_idle()
                continue
            try:
                # Scheduled work remains durable while the shared package
                # environment is changing, then starts when installation ends.
                self.environment_ready.wait()
                with self.state_lock:
                    run = self.store.get("runs", run_id)
                    if self.stopping or not run or run["status"] != "queued":
                        continue
                    self.active_id = run_id
                    run.update(status="running", started_at=now())
                    self.store.put("runs", run)
                started = time.monotonic()
                expired = threading.Event()
                def timeout():
                    with self.state_lock:
                        if self.active_id == run_id:
                            expired.set()
                            self.reset()
                timer = threading.Timer(self.timeout, timeout)
                timer.daemon = True
                timer.start()
                try:
                    with self.state_lock:
                        if self.stopping or run_id in self.cancelled:
                            raise RuntimeError("Cancelled before execution")
                        process = self._get_process()
                        request = {"run_id": run_id, "notebook_id": run["notebook_id"], "cells": run["_cells"], "reset_scope": run["_reset_scope"],
                                   "data_dir": str(self.store.directory), "package_dir": str(self.store.directory / "site-packages"),
                                   "repository_paths": [str(self.store.directory / "repositories" / repo["id"]) for repo in self.store.list("repositories")]}
                        if run_id in self._connection_snapshots:
                            request["connections"] = self._connection_snapshots[run_id]
                        else:
                            try:
                                from .connectors import connections_for_runtime
                            except ImportError:
                                request["connections"] = []
                            else:
                                request["connections"] = connections_for_runtime(self.store)
                        process.stdin.write(json.dumps(request) + "\n")
                        process.stdin.flush()
                    finished = False
                    for line in process.stdout:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("run_id") != run_id:
                            continue
                        if event.get("type") == "cell":
                            run["outputs"].append(event["output"])
                            with self.state_lock:
                                if run_id in self.cancelled:
                                    run["status"] = "cancelled"
                                self.store.put("runs", run)
                        elif event.get("type") == "done":
                            run.update(status=event["status"], error=event.get("error"))
                            finished = True
                            break
                    if not finished:
                        if run_id in self.cancelled:
                            run.update(status="cancelled", error="Cancelled by user. The shared interpreter was reset.")
                        elif expired.is_set():
                            run.update(status="failed", error=f"Run exceeded {self.timeout} seconds. The shared interpreter was reset.")
                        else:
                            run.update(status="failed", error="Interpreter exited unexpectedly. See /data/logs/worker.log on the host.")
                except Exception as exc:
                    run.update(status="cancelled" if run_id in self.cancelled else "failed", error=str(exc))
                    self.reset()
                finally:
                    timer.cancel()
                    if run_id in self.cancelled:
                        run.update(status="cancelled", error="Cancelled by user. The shared interpreter was reset.")
                    run.update(finished_at=now(), duration_ms=round((time.monotonic() - started) * 1000))
                    self.store.put("runs", run)
            finally:
                with self.state_lock:
                    self._connection_snapshots.pop(run_id, None)
                    self.cancelled.discard(run_id)
                    self.active_id = None
                    self.last_activity = time.monotonic()
                    self.queue.task_done()

    def _expire_idle(self):
        # Submission, queue transitions and environment changes all use this
        # lock. Recheck here so a newly queued run never loses its interpreter.
        with self.state_lock:
            if (self.stopping or not self.idle_timeout or self.active_id or not self.queue.empty()
                    or self.environment_busy or self.process is None):
                return False
            if time.monotonic() - self.last_activity < self.idle_timeout:
                return False
            self.reset()
            return True

    def close(self):
        with self.state_lock:
            self.stopping = True
            self.environment_ready.set()
            self.reset()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def sync_wheel(self, path):
        """Embedded execution already sees the installed shared package directory."""
        return None


# The management runtime always routes by cluster. Without a configured compute
# endpoint it retains an embedded local engine for development and tests.
from .compute_client import ClusterRuntime


class Runtime(ClusterRuntime):
    def __init__(self, store):
        local = None if os.getenv("OPENBRICKS_COMPUTE_URL") else LocalRuntime(store)
        super().__init__(store, local_engine=local)
