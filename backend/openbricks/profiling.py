"""Bounded run diagnostics without a public Spark UI or raw event-log archive.

The Spark adapter targets the version packaged in the runtime (Spark 4.0.1).
Missing platform counters and unavailable Spark status entries are explicit;
profiling must never turn a successful notebook into a failed execution.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import threading
import time

import psutil

MAX_SAMPLES = 240
MAX_JOBS = 64
MAX_STAGES = 128
MAX_PLANS = 16
MAX_PLAN_LENGTH = 16_384


def redact_spark_profile(value, redactor):
    """Keep fixed telemetry keys usable even if a one-character secret matches.

    Only known schema keys and enums survive. User-controlled plan/stage text is
    still masked; arbitrary output dictionaries use the generic key redactor.
    """
    status = value.get("status")
    safe = {
        "status": (
            status
            if status in ("not_started", "measured", "unavailable")
            else "unavailable"
        ),
        "truncated": bool(value.get("truncated")),
        "jobs": [],
        "stages": [],
        "plans": [],
    }
    if "application_id" in value:
        safe["application_id"] = redactor.text(str(value["application_id"])[:160])
    if status == "unavailable":
        safe["unavailable_reason"] = (
            "Spark status metrics are unavailable for this sample"
        )
    for item in value.get("jobs", [])[:MAX_JOBS]:
        row = {
            key: item[key]
            for key in ("id", "submitted_at_ms", "completed_at_ms")
            if key in item and isinstance(item.get(key), (int, type(None)))
        }
        row["status"] = (
            item.get("status")
            if item.get("status") in ("RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN")
            else "UNKNOWN"
        )
        row["stage_ids"] = [
            identifier
            for identifier in item.get("stage_ids", [])[:MAX_STAGES]
            if isinstance(identifier, int)
        ]
        safe["jobs"].append(row)
    for item in value.get("stages", [])[:MAX_STAGES]:
        row = {
            key: item[key]
            for key in (
                "id",
                "attempt",
                "submitted_at_ms",
                "completed_at_ms",
                "num_tasks",
                "completed_tasks",
                "failed_tasks",
                "executor_run_ms",
                "executor_cpu_ns",
                "input_bytes",
                "output_bytes",
                "shuffle_read_bytes",
                "shuffle_write_bytes",
                "memory_spilled_bytes",
                "disk_spilled_bytes",
            )
            if key in item and isinstance(item.get(key), (int, type(None)))
        }
        row["status"] = (
            item.get("status")
            if item.get("status")
            in ("ACTIVE", "COMPLETE", "FAILED", "PENDING", "SKIPPED")
            else "UNKNOWN"
        )
        row["name"] = redactor.text(str(item.get("name", "")))[:200]
        safe["stages"].append(row)
    for item in value.get("plans", [])[:MAX_PLANS]:
        row = {
            key: item[key]
            for key in ("execution_id", "submitted_at_ms", "completed_at_ms")
            if key in item and isinstance(item.get(key), (int, type(None)))
        }
        row["cell_id"] = str(item.get("cell_id", ""))[:80]
        row["plan"] = redactor.text(str(item.get("plan", "")))[:MAX_PLAN_LENGTH]
        row["truncated"] = bool(item.get("truncated"))
        safe["plans"].append(row)
    return safe


def new_profile():
    return {
        "version": 1,
        "queue_wait_ms": 0,
        "spark_wait_ms": 0,
        "resources": {
            "status": "unavailable",
            "samples": [],
            "peak": {},
            "sample_interval_ms": 1000,
            "truncated": False,
        },
        "spark": {
            "status": "not_started",
            "jobs": [],
            "stages": [],
            "plans": [],
            "truncated": False,
        },
    }


def cgroup_memory(root=Path("/sys/fs/cgroup")):
    """Return the current container, not host/WSL-wide, cgroup counters."""
    try:
        current = int((root / "memory.current").read_text().strip())
        raw = (root / "memory.max").read_text().strip()
        limit = int(raw) if raw != "max" else None
        return {"container_current_bytes": current, "container_limit_bytes": limit}
    except (OSError, ValueError):
        try:
            current = int((root / "memory/memory.usage_in_bytes").read_text().strip())
            limit = int((root / "memory/memory.limit_in_bytes").read_text().strip())
            return {
                "container_current_bytes": current,
                "container_limit_bytes": limit if limit < 2**60 else None,
            }
        except (OSError, ValueError):
            return {"container_current_bytes": None, "container_limit_bytes": None}


class ResourceSampler:
    def __init__(
        self, started=None, process_factory=psutil.Process, clock=time.monotonic
    ):
        self.clock, self.process_factory = clock, process_factory
        self.started = clock() if started is None else started
        self.previous = {}
        self.last_time = None
        self.last_saved = None
        self.interval = 1.0
        self.value = new_profile()["resources"]
        self.lock = threading.RLock()

    def sample(self, pid, *, force=False):
        with self.lock:
            try:
                parent = self.process_factory(pid)
                processes = [parent, *parent.children(recursive=True)]
            except (psutil.Error, OSError):
                return None
            moment = self.clock()
            sample = {
                "elapsed_ms": max(0, round((moment - self.started) * 1000)),
                "python_rss_bytes": 0,
                "jvm_rss_bytes": 0,
                "other_rss_bytes": 0,
                "total_rss_bytes": 0,
            }
            current_cpu, cpu_delta = {}, 0.0
            for process in processes:
                try:
                    identity = (process.pid, process.create_time())
                    rss = process.memory_info().rss
                    name = process.name().lower()
                    bucket = (
                        "jvm_rss_bytes"
                        if name in ("java", "java.exe")
                        else (
                            "python_rss_bytes"
                            if "python" in name
                            else "other_rss_bytes"
                        )
                    )
                    sample[bucket] += rss
                    sample["total_rss_bytes"] += rss
                    values = process.cpu_times()
                    consumed = values.user + values.system
                    current_cpu[identity] = consumed
                    if identity in self.previous:
                        cpu_delta += max(0, consumed - self.previous[identity])
                except (psutil.Error, OSError):
                    continue
            elapsed = moment - self.last_time if self.last_time is not None else 0
            sample["cpu_percent"] = (
                round(cpu_delta * 100 / elapsed, 2) if elapsed > 0 else None
            )
            sample.update(cgroup_memory())
            self.previous, self.last_time = current_cpu, moment
            self.value["status"] = "measured"
            for key, value in sample.items():
                if key != "elapsed_ms" and isinstance(value, (int, float)):
                    self.value["peak"][key] = max(value, self.value["peak"].get(key, 0))
            if (
                force
                or self.last_saved is None
                or moment - self.last_saved >= self.interval
            ):
                samples = self.value["samples"]
                samples.append(sample)
                self.last_saved = moment
                if len(samples) > MAX_SAMPLES:
                    # Downsample older points while preserving the endpoints.
                    # Exact per-counter peaks are maintained separately above.
                    self.value["samples"] = [samples[0], *samples[2:-1:2], samples[-1]]
                    self.interval *= 2
                    self.value["sample_interval_ms"] = round(self.interval * 1000)
                    self.value["truncated"] = True
            return sample

    def snapshot(self):
        with self.lock:
            return deepcopy(self.value)


def _option(value, transform=lambda value: value):
    return transform(value.get()) if value.isDefined() else None


def _date(value):
    return _option(value, lambda date: int(date.getTime()))


def _sequence(value, maximum):
    size = value.size()
    return [value.apply(index) for index in range(min(size, maximum))], size > maximum


class SparkProfiler:
    """Read retained status metrics for this run's Spark job group only."""

    def __init__(self, run_id, spark_getter):
        self.run_id, self.spark_getter = run_id, spark_getter
        self.value = new_profile()["spark"]
        self.cell_id = None
        self.lock = threading.RLock()

    def bind(self, spark):
        # Spark local properties are thread-local: call from the executing cell
        # thread, not the background metrics collector.
        spark.sparkContext.setJobGroup(
            self.run_id, "OpenBricks notebook run", interruptOnCancel=True
        )
        if self.cell_id:
            spark.sparkContext.setJobDescription("OpenBricks cell " + self.cell_id)

    def cell(self, identifier):
        self.cell_id = identifier
        spark = self.spark_getter()
        if spark is not None:
            self.bind(spark)

    def plan(self, frame):
        try:
            if not type(frame).__module__.startswith("pyspark") or not hasattr(
                frame, "_jdf"
            ):
                return
            plan = str(frame._jdf.queryExecution().toString())
        except Exception:
            return
        with self.lock:
            plans = self.value["plans"]
            if any(
                value.get("cell_id") == self.cell_id
                and value.get("plan") == plan[:MAX_PLAN_LENGTH]
                for value in plans
            ):
                return
            if len(plans) < MAX_PLANS:
                plans.append(
                    {
                        "cell_id": self.cell_id,
                        "plan": plan[:MAX_PLAN_LENGTH],
                        "truncated": len(plan) > MAX_PLAN_LENGTH,
                    }
                )
            else:
                self.value["truncated"] = True

    def collect(self):
        spark = self.spark_getter()
        if spark is None:
            return self.snapshot()
        try:
            context = spark.sparkContext
            identifiers = sorted(context.statusTracker().getJobIdsForGroup(self.run_id))
            store = context._jsc.sc().statusStore()
            jobs, stages, stage_ids = [], [], set()
            truncated = len(identifiers) > MAX_JOBS
            for identifier in identifiers[-MAX_JOBS:]:
                job = store.job(int(identifier))
                values, limited = _sequence(job.stageIds(), MAX_STAGES)
                ids = [int(value) for value in values]
                stage_ids.update(ids)
                truncated |= limited
                jobs.append(
                    {
                        "id": int(identifier),
                        "status": str(job.status()),
                        "stage_ids": ids,
                        "submitted_at_ms": _date(job.submissionTime()),
                        "completed_at_ms": _date(job.completionTime()),
                    }
                )
                # SQLAppStatusStore lives in the same in-memory status store,
                # even with the web UI disabled. It includes executed writes
                # and actions that do not return a displayed DataFrame.
                try:
                    associated = store.jobWithAssociatedSql(int(identifier))._2()
                    if associated.isDefined():
                        execution_id = int(associated.get())
                        sql = (
                            spark._jsparkSession.sharedState()
                            .statusStore()
                            .execution(execution_id)
                        )
                        if sql.isDefined():
                            query = sql.get()
                            with self.lock:
                                plans = self.value["plans"]
                                if not any(
                                    item.get("execution_id") == execution_id
                                    for item in plans
                                ):
                                    if len(plans) < MAX_PLANS:
                                        text = str(query.physicalPlanDescription())
                                        plans.append(
                                            {
                                                "execution_id": execution_id,
                                                "cell_id": self.cell_id,
                                                "plan": text[:MAX_PLAN_LENGTH],
                                                "truncated": len(text)
                                                > MAX_PLAN_LENGTH,
                                                "submitted_at_ms": int(
                                                    query.submissionTime()
                                                ),
                                                "completed_at_ms": _date(
                                                    query.completionTime()
                                                ),
                                            }
                                        )
                                    else:
                                        truncated = True
                except Exception:
                    pass  # Optional SQL metadata never hides measured stage metrics.
            truncated |= len(stage_ids) > MAX_STAGES
            for identifier in sorted(stage_ids)[-MAX_STAGES:]:
                stage = store.lastStageAttempt(identifier)
                row = {
                    "id": identifier,
                    "attempt": int(stage.attemptId()),
                    "status": str(stage.status()),
                    "name": str(stage.name())[:200],
                    "submitted_at_ms": _date(stage.submissionTime()),
                    "completed_at_ms": _date(stage.completionTime()),
                }
                for target, source in (
                    ("num_tasks", "numTasks"),
                    ("completed_tasks", "numCompleteTasks"),
                    ("failed_tasks", "numFailedTasks"),
                    ("executor_run_ms", "executorRunTime"),
                    ("executor_cpu_ns", "executorCpuTime"),
                    ("input_bytes", "inputBytes"),
                    ("output_bytes", "outputBytes"),
                    ("shuffle_read_bytes", "shuffleReadBytes"),
                    ("shuffle_write_bytes", "shuffleWriteBytes"),
                    ("memory_spilled_bytes", "memoryBytesSpilled"),
                    ("disk_spilled_bytes", "diskBytesSpilled"),
                ):
                    row[target] = int(getattr(stage, source)())
                stages.append(row)
            with self.lock:
                self.value.update(
                    status="measured",
                    application_id=context.applicationId,
                    jobs=jobs,
                    stages=stages,
                    truncated=self.value["truncated"] or truncated,
                )
                self.value.pop("unavailable_reason", None)
        except Exception:
            with self.lock:
                # Do not persist exception strings: JDBC URLs/credentials can
                # appear in Spark exceptions. Partial successful metrics remain.
                self.value["status"] = "unavailable"
                self.value["unavailable_reason"] = (
                    "Spark status metrics are unavailable for this sample"
                )
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            return deepcopy(self.value)


def compare_profiles(current, previous):
    result = {}
    for key in ("duration_ms", "peak_memory_bytes"):
        left, right = current.get(key), previous.get(key)
        result[key] = {
            "current": left,
            "previous": right,
            "delta": (
                left - right
                if isinstance(left, (int, float)) and isinstance(right, (int, float))
                else None
            ),
        }
    for key in ("queue_wait_ms", "spark_wait_ms"):
        left, right = current.get("profile", {}).get(key), previous.get(
            "profile", {}
        ).get(key)
        result[key] = {
            "current": left,
            "previous": right,
            "delta": (
                left - right
                if isinstance(left, (int, float)) and isinstance(right, (int, float))
                else None
            ),
        }
    return result


def register_profile_routes(app, authenticated, access):
    from .profile_api import register_profile_routes as register

    return register(app, authenticated, access)
