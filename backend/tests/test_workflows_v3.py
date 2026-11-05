import time

import pytest

from openbricks.store import Store
from openbricks.workflows import WorkflowManager


class FakeRuntime:
    def __init__(self, store):
        self.store, self.calls, self.failures = store, [], {}

    def submit(self, notebook, **options):
        identifier = options["run_id"]
        old = self.store.get("runs", identifier)
        if old:
            return old
        self.calls.append((notebook, options))
        remaining = self.failures.get(notebook["id"], 0)
        self.failures[notebook["id"]] = max(0, remaining - 1)
        return self.store.put("runs", {"id": identifier, "notebook_id": notebook["id"], "status": "failed" if remaining else "succeeded",
            "error": "deliberate failure" if remaining else None, "error_class": "execution", "finished_at": "2026-11-05T00:00:00+00:00"})

    def cancel(self, identifier):
        return self.store.put("runs", {**self.store.get("runs", identifier), "status": "cancelled"})


def setup(tmp_path):
    store = Store(tmp_path)
    for name in ("bronze", "silver", "gold"):
        store.put("notebooks", {"id": name, "name": name, "project_id": "default", "connection_ids": [],
            "parameter_definitions": [{"name": "day", "type": "string", "required": True}],
            "cells": [{"id": "one", "language": "python", "source": "print('original')"}]})
    runtime = FakeRuntime(store)
    manager = WorkflowManager(store, runtime)
    workflow = {"id": "pipeline", "name": "Pipeline", "project_id": "default", "parameter_definitions": [{"name": "day", "type": "string", "required": True}],
        "tasks": [{"key": name, "notebook_id": name, "depends_on": [] if i == 0 else [("bronze", "silver")[i-1]]} for i, name in enumerate(("bronze", "silver", "gold"))]}
    workflow = {**manager.validate(workflow), "id": "pipeline"}
    return store, runtime, manager, workflow


def drain(manager, identifier):
    for _ in range(40):
        value = manager.store.get("workflow_runs", identifier)
        if value["status"] not in ("queued", "running"):
            return value
        for task in value["tasks"]:
            if task["status"] == "waiting_retry":
                task["next_retry_at"] = "2000-01-01T00:00:00+00:00"
        manager.store.put("workflow_runs", value)
        manager._tick(value)
    pytest.fail("Workflow did not finish")


def test_dag_captured_parameters_sources_and_replay(tmp_path):
    store, runtime, manager, workflow = setup(tmp_path)
    run = manager.submit(workflow, {"day": "yesterday"})
    notebook = store.get("notebooks", "silver")
    notebook["cells"][0]["source"] = "print('edited later')"
    store.put("notebooks", notebook)
    done = drain(manager, run["id"])
    assert done["status"] == "succeeded"
    assert [item[0]["id"] for item in runtime.calls] == ["bronze", "silver", "gold"]
    assert runtime.calls[1][0]["cells"][0]["source"] == "print('original')"
    assert runtime.calls[1][1]["parameters"] == {"day": "yesterday"}
    rerun = manager.replay(run["id"])
    assert drain(manager, rerun["id"])["status"] == "succeeded"
    assert runtime.calls[-2][0]["cells"][0]["source"] == "print('original')"


def test_failure_skips_downstream_repair_preserves_successful_steps(tmp_path):
    store, runtime, manager, workflow = setup(tmp_path)
    runtime.failures["silver"] = 1
    run = manager.submit(workflow, {"day": "today"})
    failed = drain(manager, run["id"])
    assert [task["status"] for task in failed["tasks"]] == ["succeeded", "failed", "skipped"]
    repaired = manager.replay(run["id"], repair=True)
    assert drain(manager, repaired["id"])["status"] == "succeeded"
    assert [item[0]["id"] for item in runtime.calls] == ["bronze", "silver", "silver", "gold"]


def test_opt_in_retries_and_restart_admission_identity(tmp_path):
    store, runtime, manager, workflow = setup(tmp_path)
    workflow["tasks"][0].update(max_retries=1, retry_safe=True, retry_delay_seconds=1)
    runtime.failures["bronze"] = 1
    run = manager.submit(workflow, {"day": "today"})
    done = drain(manager, run["id"])
    assert done["status"] == "succeeded" and len(done["tasks"][0]["attempts"]) == 2
    run = manager.submit(workflow, {"day": "tomorrow"})
    task = run["tasks"][0]
    task.update(status="queued", run_id="persisted-before-admission", attempts=[{"number": 1, "run_id": "persisted-before-admission", "status": "queued", "created_at": "now"}])
    store.put("workflow_runs", run)
    recovered = WorkflowManager(store, runtime)
    assert drain(recovered, run["id"])["status"] == "succeeded"
    assert runtime.calls[-3][1]["run_id"] == "persisted-before-admission"


def test_cycle_retry_validation_and_cancel_before_admission(tmp_path):
    store, runtime, manager, workflow = setup(tmp_path)
    workflow["tasks"][0]["depends_on"] = ["gold"]
    with pytest.raises(ValueError, match="cycle"):
        manager.validate(workflow)
    workflow["tasks"][0]["depends_on"] = []
    workflow["tasks"][0]["max_retries"] = 2
    with pytest.raises(ValueError, match="retry_safe"):
        manager.validate(workflow)
    workflow["tasks"][0]["max_retries"] = 0
    run = manager.submit(workflow, {"day": "today"})
    assert manager.cancel(run["id"])["status"] == "cancelled"
    assert not runtime.calls


def test_unknown_outcome_never_retries_even_when_task_opted_in(tmp_path):
    store, runtime, manager, workflow = setup(tmp_path)
    workflow["tasks"][0].update(max_retries=5, retry_safe=True)
    original = runtime.submit
    def unknown(notebook, **options):
        result = original(notebook, **options)
        result.update(status="failed", error="Compute outcome is unknown", error_class="unknown_outcome")
        return store.put("runs", result)
    runtime.submit = unknown
    done = drain(manager, manager.submit(workflow, {"day": "today"})["id"])
    assert done["status"] == "failed"
    assert len(done["tasks"][0]["attempts"]) == 1 and len(runtime.calls) == 1


def test_cancel_requires_acknowledgment_and_does_not_start_dependents(tmp_path):
    store, runtime, manager, workflow = setup(tmp_path)
    original = runtime.submit
    def running(notebook, **options):
        return store.put("runs", {**original(notebook, **options), "status": "running"})
    runtime.submit = running
    original_cancel = runtime.cancel
    def unavailable(identifier):
        raise ValueError("Cannot confirm cancellation")
    runtime.cancel = unavailable
    run = manager.submit(workflow, {"day": "today"})
    manager._tick(run)
    pending = manager.cancel(run["id"])
    assert pending["status"] == "running" and pending["cancel_requested"]
    assert "acknowledgment" in pending["error"] and len(runtime.calls) == 1
    runtime.cancel = original_cancel
    done = drain(manager, run["id"])
    assert done["status"] == "cancelled" and len(runtime.calls) == 1


def test_workflow_schedule_compute_default_preserves_explicit_task_target(tmp_path):
    store, runtime, manager, workflow = setup(tmp_path)
    workflow["tasks"][1]["cluster_id"] = "specific"
    store.put("workflows", workflow)
    job = {"id": "scheduled", "workflow_id": workflow["id"], "cluster_id": "job-default", "parameters": {"day": "today"}}
    run = manager.submit_job(job)
    assert drain(manager, run["id"])["status"] == "succeeded"
    assert [options["cluster_id"] for _, options in runtime.calls] == ["job-default", "specific", "job-default"]


def test_attempt_timestamps_use_child_clock_not_later_control_admission(tmp_path):
    store, runtime, manager, workflow = setup(tmp_path)
    original = runtime.submit
    def clocked(notebook, **options):
        return store.put("runs", {**original(notebook, **options), "created_at": "2026-11-05T06:17:04+00:00",
            "compute_created_at": "2026-11-05T06:17:02+00:00", "started_at": "2026-11-05T06:17:03+00:00", "finished_at": "2026-11-05T06:17:03.700+00:00"})
    runtime.submit = clocked
    done = drain(manager, manager.submit(workflow, {"day": "today"})["id"])
    attempt = done["tasks"][0]["attempts"][0]
    assert attempt["created_at"] == "2026-11-05T06:17:02+00:00"
    assert attempt["started_at"] == "2026-11-05T06:17:03+00:00"
    assert attempt["finished_at"] == "2026-11-05T06:17:03.700+00:00"
    assert attempt["admitted_at"] != attempt["created_at"]
