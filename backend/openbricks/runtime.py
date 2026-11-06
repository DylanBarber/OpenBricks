"""One durable run queue and one persistent Spark interpreter for the host."""

from __future__ import annotations

import json
import os
import platform
import queue
import signal
import shutil
import subprocess
import sys
import threading
import time
import psutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .clusters import RuntimeBusy


def now():
    return datetime.now(timezone.utc).isoformat()


class SingleSessionRuntime:
    def __init__(self, store, coordinator=None, recover=True):
        self.store = store
        self.coordinator = coordinator
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
        self.idle_timeout = max(
            0, int(os.getenv("OPENBRICKS_IDLE_TIMEOUT_SECONDS", "900"))
        )
        self.last_activity = time.monotonic()
        self.log = None
        self._connection_snapshots = {}
        self._secret_snapshots = {}
        self._redaction_history = None
        self._running_run = None
        self._stderr_thread = None
        self.current_environment = None
        self.current_project = None
        for run in self.store.active_runs() if recover else []:
            if (
                run["status"] in ("queued", "running")
                and run.get("cluster_id", "local") == "local"
            ):
                run.update(
                    error_class=(
                        "unknown_outcome" if run["status"] == "running" else "restart"
                    ),
                    status="failed",
                    finished_at=now(),
                    error="Compute restarted before this run completed. Inspect any prior writes before starting a new attempt.",
                )
                self.store.put("runs", run)
        self.thread = threading.Thread(
            target=self._loop, daemon=True, name="openbricks-run-queue"
        )

    def start(self):
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
        session_id=None,
        notebook_secrets=None,
    ):
        with self.state_lock:
            if self.stopping:
                raise RuntimeBusy("Compute is stopping; try again after maintenance")
            if self.environment_busy and trigger != "schedule":
                raise RuntimeBusy(
                    "The shared Python environment is being updated. Try running again when the package operation finishes"
                )
            return self._submit(
                notebook,
                cell_id,
                trigger,
                job_id,
                cluster_id,
                run_id,
                connections,
                parameters,
                timeout_seconds,
                environment_id,
                environment,
                provenance,
                resource_options,
                session_id,
                notebook_secrets,
            )

    @contextmanager
    def environment_change(self):
        with self.state_lock:
            if self.environment_busy or self.active_id or self.store.has_active_runs():
                raise RuntimeBusy(
                    "Wait for queued and running notebooks before changing the shared Python environment"
                )
            self.environment_busy = True
            self.environment_ready.clear()
        try:
            yield
        finally:
            with self.state_lock:
                self.environment_busy = False
                self.environment_ready.set()

    def _submit(
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
        session_id=None,
        notebook_secrets=None,
    ):
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
        from .parameters import resolve_parameters
        from .environments import (
            capture_environment,
            get_environment,
            validate_manifest,
        )

        project_id = notebook.get("project_id", "default")
        values = resolve_parameters(
            notebook.get("parameter_definitions", []), parameters
        )
        seconds = self.timeout if timeout_seconds is None else timeout_seconds
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not 1 <= seconds <= 86_400
        ):
            raise ValueError("Run timeout must be between 1 and 86400 seconds")
        manifest = (
            validate_manifest(environment)
            if environment is not None
            else (
                get_environment(self.store, environment_id, project_id)
                if environment_id
                else capture_environment(self.store, project_id)
            )
        )
        from .execution import notebook_provenance

        if self.queue.qsize() >= 100:
            raise ValueError("Run queue is full; wait for a run to finish")
        from .profiling import new_profile

        run = {
            "id": run_id or uuid4().hex,
            "notebook_id": notebook["id"],
            "notebook_name": notebook["name"],
            "cluster_id": cluster_id or "local",
            "cluster_name": "Local compute",
            "status": "queued",
            "trigger": trigger,
            "job_id": job_id,
            "created_at": now(),
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "outputs": [],
            "error": None,
            "_cells": cells,
            "_reset_scope": not bool(cell_id),
            "_notebook": notebook,
            "_cell_id": cell_id,
            "project_id": project_id,
            "connection_ids": notebook.get("connection_ids", []),
            "data_execution_id": notebook.get("data_execution_id"),
            "parameters": values,
            "timeout_seconds": seconds,
            "environment_id": manifest["id"],
            "_environment": manifest,
            "resource_options": resource_options or {},
            "session_id": session_id,
            "owner_id": (resource_options or {}).get("owner_id"),
            "provenance": {
                **notebook_provenance(notebook),
                **(provenance or {}),
                "environment_id": manifest["id"],
            },
            "logs": [],
            "progress": {"total_cells": len(cells)},
            "peak_memory_bytes": 0,
            "error_class": None,
            "profile": new_profile(),
        }
        self.store.put("runs", run)
        if connections is not None:
            self._connection_snapshots[run["id"]] = connections
        if notebook_secrets is not None:
            self._secret_snapshots[run["id"]] = notebook_secrets
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
            run.update(
                status="cancelled",
                finished_at=now(),
                error=(
                    "Cancelled by user. The shared interpreter was reset."
                    if active
                    else "Cancelled before execution."
                ),
            )
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
                descendants = []
                try:
                    descendants = psutil.Process(process.pid).children(recursive=True)
                except psutil.Error:
                    pass
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            capture_output=True,
                            timeout=15,
                        )
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError, subprocess.SubprocessError):
                    process.kill()
                # Spark/Python child processes can create their own process
                # groups. Stop captured descendants as well as the kernel's
                # group, then reap them before releasing memory reservations.
                for child in reversed(descendants):
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
                if descendants:
                    psutil.wait_procs(descendants, timeout=3)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=1)
                self._stderr_thread = None
            if self.log:
                self.log.close()
                self.log = None
            if self.coordinator:
                temporary_root = self.store.directory / "compute-tmp"
                temporary = temporary_root / self.session_info["id"]
                if (
                    temporary.is_dir()
                    and not temporary.is_symlink()
                    and not temporary_root.is_symlink()
                    and temporary.resolve().is_relative_to(temporary_root.resolve())
                ):
                    try:
                        shutil.rmtree(temporary)
                    except OSError:
                        pass  # A later cleanup can retry an OS-held temp file.

    def _get_process(self, package_dir=None, environment_id=None, project_id="default"):
        with self.process_lock:
            if (
                self.current_environment != environment_id
                or self.current_project != project_id
            ):
                self.reset()
                self.current_environment = environment_id
                self.current_project = project_id
            if self.process and self.process.poll() is None:
                return self.process
            log_dir = self.store.directory / "logs"
            log_dir.mkdir(exist_ok=True)
            self.log = (log_dir / "worker.log").open("a", encoding="utf-8")
            from .secret_runtime import RedactionHistory

            self._redaction_history = RedactionHistory()
            python_paths = [str(package_dir or self.store.directory / "site-packages")]
            if os.getenv("PYTHONPATH"):
                python_paths.append(os.environ["PYTHONPATH"])
            env = {
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "DATA_DIR": str(self.store.directory),
                "SPARK_LOCAL_IP": "127.0.0.1",
                "PYTHONPATH": os.pathsep.join(python_paths),
            }
            if self.coordinator:
                env.update(
                    OPENBRICKS_SPARK_LEASE_PROTOCOL="1",
                    OPENBRICKS_WORKER_MEMORY_MB=str(self.session_info["memory_mb"]),
                )
                temporary_root = self.store.directory / "compute-tmp"
                temporary = temporary_root / self.session_info["id"]
                if (
                    temporary_root.is_symlink()
                    or temporary.is_symlink()
                    or not temporary.resolve().is_relative_to(
                        self.store.directory.resolve()
                    )
                ):
                    raise ValueError("Invalid worker temporary directory")
                temporary.mkdir(parents=True, exist_ok=True)
                env.update(
                    OPENBRICKS_WORKER_TMPDIR=str(temporary),
                    TMPDIR=str(temporary),
                    SPARK_LOCAL_DIRS=str(temporary / "spark"),
                )
                # Thread pools in small Python/DuckDB workers must not reserve
                # the host's full CPU/memory population before user code runs.
                env.update(
                    OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1"
                )
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "openbricks.worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
                start_new_session=os.name != "nt",
            )
            self._capture_native_stderr(self.process, self.log, self._redaction_history)
            return self.process

    def _capture_native_stderr(self, process, log, history):
        """Mask native/JVM output before writing it to the host log file."""
        from .execution import Redactor, StreamRedactor

        def capture():
            stream = StreamRedactor(Redactor())

            def write(value):
                if not value:
                    return
                try:
                    if log.tell() > 5 * 1024 * 1024:
                        log.seek(0)
                        log.truncate()
                    log.write(value)
                    log.flush()
                except (OSError, ValueError):
                    pass

            try:
                while True:
                    chunk = process.stderr.readline(4096)
                    if not chunk:
                        break
                    stream.redactor = Redactor(history.include())
                    write(stream.feed(chunk))
                stream.redactor = Redactor(history.include())
                write(stream.feed(final=True))
            except (OSError, ValueError):
                pass

        self._stderr_thread = threading.Thread(
            target=capture, daemon=True, name="openbricks-native-log-redactor"
        )
        self._stderr_thread.start()

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
                if self.coordinator and not self.coordinator.admit(self, run_id):
                    continue
                with self.state_lock:
                    run = self.store.get("runs", run_id)
                    if self.stopping or not run or run["status"] != "queued":
                        continue
                    self.active_id = run_id
                    self._running_run = run
                    from .profiling import new_profile, ResourceSampler

                    run.setdefault("profile", new_profile())
                    try:
                        queued_at = datetime.fromisoformat(run["created_at"])
                        run["profile"]["queue_wait_ms"] = max(
                            0,
                            round(
                                (datetime.now(timezone.utc) - queued_at).total_seconds()
                                * 1000
                            ),
                        )
                    except (ValueError, TypeError):
                        pass
                    run.update(status="running", started_at=now(), queued_reason=None)
                    self.store.put("runs", run)
                started = time.monotonic()
                expired = threading.Event()
                sample_stop = threading.Event()
                resource_sampler = ResourceSampler(started=started)

                def sample_memory(run=run, sample_stop=sample_stop, run_id=run_id):
                    last_saved = 0.0
                    while not sample_stop.wait(0.25):
                        process = self.process
                        if process:
                            try:
                                sample = resource_sampler.sample(process.pid)
                                if sample is None:
                                    continue
                                total = sample["total_rss_bytes"]
                                with self.state_lock:
                                    run["profile"][
                                        "resources"
                                    ] = resource_sampler.snapshot()
                                run["peak_memory_bytes"] = max(
                                    run.get("peak_memory_bytes", 0), total
                                )
                                budget = run.get("resource_options", {}).get(
                                    "memory_mb"
                                )
                                if budget and total > budget * 1024**2:
                                    with self.state_lock:
                                        if (
                                            self.active_id == run_id
                                            and run["status"] == "running"
                                        ):
                                            run["memory_limit_exceeded"] = True
                                            self.reset()
                                    return
                                if time.monotonic() - last_saved >= 1:
                                    with self.state_lock:
                                        if (
                                            self.active_id == run_id
                                            and run_id not in self.cancelled
                                            and run["status"] == "running"
                                        ):
                                            self.store.put("runs", run)
                                    last_saved = time.monotonic()
                            except psutil.Error:
                                pass

                sampler = threading.Thread(
                    target=sample_memory, daemon=True, name="openbricks-memory-sampler"
                )
                sampler.start()

                def timeout(target_id=run_id, target_run=run, expiry=expired):
                    with self.state_lock:
                        if (
                            self.active_id == target_id
                            and target_run["status"] == "running"
                            and target_id not in self.cancelled
                        ):
                            expiry.set()
                            self.reset()

                timer = threading.Timer(
                    run.get("timeout_seconds", self.timeout), timeout
                )
                timer.daemon = True
                timer.start()
                try:
                    from .environments import prepare_environment

                    package_dir = prepare_environment(
                        self.store.directory, run["_environment"]
                    )
                    with self.state_lock:
                        if (
                            self.stopping
                            or run_id in self.cancelled
                            or expired.is_set()
                        ):
                            raise RuntimeError("Cancelled before execution")
                        process = self._get_process(
                            package_dir, run["environment_id"], run["project_id"]
                        )
                        if run_id in self._secret_snapshots:
                            secret_values = self._secret_snapshots[run_id]
                        else:
                            from .notebook_secrets import secrets_for_runtime

                            secret_values = secrets_for_runtime(
                                self.store,
                                run["_notebook"],
                                owner_id=run.get("owner_id"),
                            )
                        request = {
                            "run_id": run_id,
                            "notebook_id": run["notebook_id"],
                            "cells": run["_cells"],
                            "reset_scope": run["_reset_scope"],
                            "data_dir": str(self.store.directory),
                            "package_dir": str(package_dir),
                            "repository_paths": [],
                            "parameters": run["parameters"],
                            "project_id": run["project_id"],
                            "notebook_secrets": secret_values,
                        }
                        if run_id in self._connection_snapshots:
                            request["connections"] = self._connection_snapshots[run_id]
                        else:
                            try:
                                from .connectors import connections_for_runtime
                            except ImportError:
                                request["connections"] = []
                            else:
                                request["connections"] = (
                                    connections_for_runtime(
                                        self.store,
                                        project_id=run["project_id"],
                                        connection_ids=run["connection_ids"],
                                    )
                                    if run["connection_ids"]
                                    else []
                                )
                        from .execution import Redactor

                        from .secret_runtime import RedactionHistory, SecretHistoryFull

                        if self._redaction_history is None:
                            self._redaction_history = RedactionHistory()
                        known_values = [
                            *secret_values,
                            *(
                                {"value": str(value)}
                                for item in request["connections"]
                                for value in item.get("secrets", {}).values()
                                if value is not None and str(value)
                            ),
                        ]
                        try:
                            historical = self._redaction_history.include(known_values)
                        except SecretHistoryFull:
                            self.reset()
                            process = self._get_process(
                                package_dir, run["environment_id"], run["project_id"]
                            )
                            self._redaction_history = RedactionHistory()
                            historical = self._redaction_history.include(known_values)
                        redactor = Redactor([*request["connections"], *historical])
                        resource_sampler.sample(process.pid, force=True)
                        run["profile"]["resources"] = resource_sampler.snapshot()
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
                        raw_event = event
                        event = redactor.event(event)
                        if raw_event.get("type") == "profile":
                            from .profiling import redact_spark_profile

                            event["spark"] = redact_spark_profile(
                                raw_event["spark"], redactor
                            )
                        if (
                            event.get("type") == "spark_lease_request"
                            and self.coordinator
                        ):
                            granted = self.coordinator.acquire_spark(self, run_id)
                            if not granted:
                                raise RuntimeError("Spark catalog lease was cancelled")
                            process.stdin.write(
                                json.dumps(
                                    {"type": "spark_lease_granted", "run_id": run_id}
                                )
                                + "\n"
                            )
                            process.stdin.flush()
                        elif event.get("type") == "profile":
                            with self.state_lock:
                                run["profile"]["spark"] = event["spark"]
                                self.store.put("runs", run)
                        elif event.get("type") == "cell":
                            run["outputs"].append(event["output"])
                            with self.state_lock:
                                if run_id in self.cancelled:
                                    run["status"] = "cancelled"
                                self.store.put("runs", run)
                        elif event.get("type") == "done":
                            run.update(status=event["status"], error=event.get("error"))
                            finished = True
                            break
                        else:
                            from .execution import apply_event

                            apply_event(self.store, run, event)
                            self.store.put("runs", run)
                    if not finished:
                        if run_id in self.cancelled:
                            run.update(
                                status="cancelled",
                                error="Cancelled by user. The shared interpreter was reset.",
                            )
                        elif expired.is_set():
                            run.update(
                                status="failed",
                                error=f"Run exceeded {run.get('timeout_seconds', self.timeout)} seconds. The shared interpreter was reset.",
                            )
                        else:
                            run.update(
                                status="failed",
                                error="Interpreter exited unexpectedly. See /data/logs/worker.log on the host.",
                            )
                except Exception as exc:
                    error = (
                        f"Run exceeded {run.get('timeout_seconds', self.timeout)} seconds. The shared interpreter was reset."
                        if expired.is_set()
                        else str(exc)
                    )
                    run.update(
                        status="cancelled" if run_id in self.cancelled else "failed",
                        error=error,
                    )
                    self.reset()
                finally:
                    timer.cancel()
                    sample_stop.set()
                    sampler.join(timeout=1)
                    if self.process is not None:
                        resource_sampler.sample(self.process.pid, force=True)
                    run["profile"]["resources"] = resource_sampler.snapshot()
                    if run_id in self.cancelled:
                        run.update(
                            status="cancelled",
                            error="Cancelled by user. The shared interpreter was reset.",
                        )
                    elif expired.is_set():
                        run.update(
                            status="failed",
                            error=f"Run exceeded {run.get('timeout_seconds', self.timeout)} seconds. The shared interpreter was reset.",
                        )
                    elif run.get("memory_limit_exceeded"):
                        run.update(
                            status="failed",
                            error=f"Worker exceeded its {run['resource_options']['memory_mb']} MB process-tree memory budget and was stopped.",
                        )
                    run.update(
                        finished_at=now(),
                        duration_ms=round((time.monotonic() - started) * 1000),
                    )
                    from .execution import classify_error

                    run["error_class"] = classify_error(
                        run.get("error"), self.process.poll() if self.process else None
                    )
                    if run.get("memory_limit_exceeded"):
                        run["error_class"] = "resource_limit"
                    self.store.put("runs", run)
                    try:
                        from .data_workspace import finalize_data_run
                    except ImportError:
                        pass
                    else:
                        finalize_data_run(self.store, run)
            finally:
                with self.state_lock:
                    self._connection_snapshots.pop(run_id, None)
                    self._secret_snapshots.pop(run_id, None)
                    self.cancelled.discard(run_id)
                    self.active_id = None
                    self._running_run = None
                    self.last_activity = time.monotonic()
                    self.queue.task_done()
                if self.coordinator:
                    self.coordinator.finished(self, run_id)

    def _expire_idle(self):
        # Submission, queue transitions and environment changes all use this
        # lock. Recheck here so a newly queued run never loses its interpreter.
        with self.state_lock:
            if self.coordinator:
                return False  # Aggregate supervisor also accounts for queued work.
            if (
                self.stopping
                or not self.idle_timeout
                or self.active_id
                or not self.queue.empty()
                or self.environment_busy
                or self.process is None
            ):
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
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def sync_wheel(self, path):
        """Embedded execution already sees the installed shared package directory."""
        return None


# The management runtime always routes by cluster. Without a configured compute
# endpoint it retains an embedded local engine for development and tests.
from .sessions import SessionRuntime


class LocalRuntime(SessionRuntime):
    def __init__(self, store):
        super().__init__(store, SingleSessionRuntime)


from .compute_client import ClusterRuntime


class Runtime(ClusterRuntime):
    def __init__(self, store):
        local = None if os.getenv("OPENBRICKS_COMPUTE_URL") else LocalRuntime(store)
        super().__init__(store, local_engine=local)
