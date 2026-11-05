import time

import pytest
from fastapi.testclient import TestClient

from openbricks.compute import create_compute_app
from openbricks.runtime import LocalRuntime
from openbricks.store import Store


def notebook(source, cell_id="one"):
    return {"id": "idle-notebook", "name": "Idle memory check", "cells": [{"id": cell_id, "language": "python", "source": source}]}


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    pytest.fail("Runtime condition did not complete")


def finished(store, run_id):
    value = store.get("runs", run_id)
    return value if value and value["status"] not in ("queued", "running") else None


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_IDLE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("OPENBRICKS_RUN_TIMEOUT", "15")
    monkeypatch.delenv("OPENBRICKS_CATALOG_URL", raising=False)
    for key in ("OPENBRICKS_S3_ENDPOINT", "OPENBRICKS_S3_ACCESS_KEY", "OPENBRICKS_S3_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    value = LocalRuntime(Store(tmp_path / "data"))
    value.start()
    try:
        yield value
    finally:
        value.close()


def test_idle_interpreter_exits_and_next_run_restarts_without_touching_saved_data(runtime):
    store = runtime.store
    store.put("notebooks", notebook("saved source remains"))
    run = runtime.submit(notebook("answer = 41\nblob.write_text('blob://workspace/kept.txt', 'persistent data')\nprint('ready')"), cell_id="one")
    assert wait_for(lambda: finished(store, run["id"]))["status"] == "succeeded"
    process = runtime.process
    assert process is not None and process.poll() is None
    wait_for(lambda: runtime.active_id is None)
    # Force the elapsed interval without making this test wait a full idle period.
    with runtime.state_lock:
        runtime.last_activity = time.monotonic() - 2
    # reset() clears its reference before waiting for the OS process tree to
    # exit; observe the process exit as well as the cleared runtime reference.
    wait_for(lambda: runtime.process is None and process.poll() is not None)
    assert store.get("notebooks", "idle-notebook")["cells"][0]["source"] == "saved source remains"
    assert (store.directory / "buckets" / "workspace" / "kept.txt").read_text() == "persistent data"
    next_run = runtime.submit(notebook("print('fresh interpreter')\nprint('answer' in globals())", "two"), cell_id="two")
    result = wait_for(lambda: finished(store, next_run["id"]))
    assert result["status"] == "succeeded" and result["outputs"][0]["stdout"].strip().splitlines() == ["fresh interpreter", "False"]
    assert runtime.process is not process


def test_active_and_queued_work_are_not_killed_by_idle_expiration(runtime):
    first = runtime.submit(notebook("import time\ntime.sleep(1.3)\nprint('active work survived')"))
    wait_for(lambda: runtime.active_id == first["id"] and runtime.process is not None)
    process = runtime.process
    with runtime.state_lock:
        runtime.last_activity = time.monotonic() - 10
    assert runtime._expire_idle() is False
    second = runtime.submit(notebook("print('queued work survived')"))
    assert runtime._expire_idle() is False
    first_result = wait_for(lambda: finished(runtime.store, first["id"]))
    second_result = wait_for(lambda: finished(runtime.store, second["id"]))
    assert first_result["status"] == second_result["status"] == "succeeded"
    assert runtime.process is process and process.poll() is None
    assert time.monotonic() - runtime.last_activity < runtime.idle_timeout


def test_queue_and_environment_change_rechecked_before_idle_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_IDLE_TIMEOUT_SECONDS", "1")
    runtime = LocalRuntime(Store(tmp_path / "data"))
    resets = []
    runtime.process = object()
    monkeypatch.setattr(runtime, "reset", lambda: resets.append(True))
    runtime.last_activity = time.monotonic() - 10
    with runtime.environment_change():
        assert runtime._expire_idle() is False
    queued = runtime.submit(notebook("print('queued')"))
    assert runtime._expire_idle() is False
    assert runtime.store.get("runs", queued["id"])["status"] == "queued"
    assert resets == []


def test_zero_timeout_disables_automatic_interpreter_shutdown(runtime):
    runtime.idle_timeout = 0
    run = runtime.submit(notebook("answer = 41\nprint('warm')"), cell_id="one")
    assert wait_for(lambda: finished(runtime.store, run["id"]))["status"] == "succeeded"
    process = runtime.process
    wait_for(lambda: runtime.active_id is None)
    with runtime.state_lock:
        runtime.last_activity = time.monotonic() - 100000
    assert runtime._expire_idle() is False
    time.sleep(0.65)  # Allow the actual queue-loop idle check to run as well.
    assert runtime.process is process and process.poll() is None
    second = runtime.submit(notebook("print(answer + 1)", "two"), cell_id="two")
    result = wait_for(lambda: finished(runtime.store, second["id"]))
    assert result["status"] == "succeeded" and result["outputs"][0]["stdout"].strip() == "42"


def test_compute_status_exposes_idle_timeout_and_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_IDLE_TIMEOUT_SECONDS", "37")
    token = "test-compute-authentication-token-123456789"
    with TestClient(create_compute_app(tmp_path / "compute", token=token)) as client:
        response = client.get("/status", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200 and response.json()["idle_timeout_seconds"] == 37
    monkeypatch.delenv("OPENBRICKS_IDLE_TIMEOUT_SECONDS")
    assert LocalRuntime(Store(tmp_path / "default")).idle_timeout == 900
