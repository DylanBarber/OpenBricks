import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from openbricks import compute_client
from openbricks.compute import create_compute_app
from openbricks.resources import normalize_policy
from openbricks.runtime import LocalRuntime
from openbricks.store import Store


def notebook(identifier, source, project="default"):
    return {
        "id": identifier,
        "name": identifier,
        "project_id": project,
        "connection_ids": [],
        "cells": [{"id": "one", "language": "python", "source": source}],
    }


def wait_for(function, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(0.025)
    pytest.fail("Expected execution state was not reached")


def done(store, identifier):
    value = store.get("runs", identifier)
    return value if value and value["status"] not in ("queued", "running") else None


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for name in (
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
        "OPENBRICKS_COMPUTE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path / "sessions")
    value = LocalRuntime(store)
    value.update_resource_policy(
        {
            "memory_budget_mb": 1024,
            "max_concurrent": 2,
            "max_sessions": 8,
            "default_memory_mb": 256,
            "max_memory_mb": 512,
            "idle_timeout_seconds": 900,
        }
    )
    value.start()
    try:
        yield value
    finally:
        value.close()


def test_interactive_notebooks_keep_independent_state_and_jobs_are_one_shot(runtime):
    store = runtime.store
    first = runtime.submit(
        notebook("first", "answer = 41"),
        cell_id="one",
        resource_options={"owner_id": "alice"},
    )
    assert wait_for(lambda: done(store, first["id"]))["status"] == "succeeded"
    second = runtime.submit(
        notebook("second", "answer = 100"),
        cell_id="one",
        resource_options={"owner_id": "bob"},
    )
    assert wait_for(lambda: done(store, second["id"]))["status"] == "succeeded"
    assert len(runtime.resource_status()["sessions"]) == 2
    job = runtime.submit(
        notebook("first", "print('answer' in globals())"),
        trigger="schedule",
        job_id="daily",
        resource_options={"owner_id": "alice"},
    )
    result = wait_for(lambda: done(store, job["id"]))
    assert result["outputs"][0]["stdout"].strip() == "False"
    wait_for(
        lambda: not any(
            item["mode"] == "job" for item in runtime.resource_status()["sessions"]
        )
    )
    again = runtime.submit(
        notebook("first", "print(answer + 1)"),
        cell_id="one",
        resource_options={"owner_id": "alice"},
    )
    assert (
        wait_for(lambda: done(store, again["id"]))["outputs"][0]["stdout"].strip()
        == "42"
    )
    other = runtime.submit(
        notebook("second", "print(answer + 1)"),
        cell_id="one",
        resource_options={"owner_id": "bob"},
    )
    assert (
        wait_for(lambda: done(store, other["id"]))["outputs"][0]["stdout"].strip()
        == "101"
    )


def test_parallel_jobs_cancel_only_the_target_worker(runtime):
    first = runtime.submit(
        notebook("first", "import time; time.sleep(2); print('first')"),
        trigger="workflow",
    )
    second = runtime.submit(
        notebook("second", "import time; time.sleep(1); print('second')"),
        trigger="workflow",
    )
    wait_for(
        lambda: runtime.active_count == 2
        and len(runtime.resource_status()["sessions"]) == 2
    )
    assert len({item["pid"] for item in runtime.resource_status()["sessions"]}) == 2
    runtime.cancel(first["id"])
    assert wait_for(lambda: done(runtime.store, first["id"]))["status"] == "cancelled"
    result = wait_for(lambda: done(runtime.store, second["id"]))
    assert (
        result["status"] == "succeeded"
        and result["outputs"][0]["stdout"].strip() == "second"
    )


def test_priority_and_project_admission_budgets(runtime):
    policy = runtime.resource_policy()
    policy.update(max_concurrent=1)
    runtime.update_resource_policy(policy)
    blocker = runtime.submit(
        notebook("blocker", "import time; time.sleep(1.2)"), trigger="workflow"
    )
    wait_for(lambda: runtime.active_id == blocker["id"])
    low = runtime.submit(
        notebook("low", "print('low')"),
        trigger="workflow",
        resource_options={"priority": -10},
    )
    high = runtime.submit(
        notebook("high", "print('high')"),
        trigger="workflow",
        resource_options={"priority": 20},
    )
    wait_for(lambda: len(runtime._waiting) == 2)
    result_high = wait_for(lambda: done(runtime.store, high["id"]))
    result_low = wait_for(lambda: done(runtime.store, low["id"]))
    assert result_high["started_at"] < result_low["started_at"]
    wait_for(lambda: not runtime._pending)
    policy.update(
        max_concurrent=2,
        project_limits={"private": {"max_concurrent": 1, "memory_budget_mb": 256}},
    )
    runtime.update_resource_policy(policy)
    one = runtime.submit(
        notebook("one", "import time; time.sleep(1)", "private"), trigger="workflow"
    )
    wait_for(lambda: runtime.active_id == one["id"])
    two = runtime.submit(
        notebook("two", "print('later')", "private"), trigger="workflow"
    )
    three = runtime.submit(
        notebook("three", "import time; time.sleep(.5)", "other"), trigger="workflow"
    )
    wait_for(lambda: runtime.active_count == 2)
    assert runtime.store.get("runs", two["id"])["status"] == "queued"
    assert wait_for(lambda: done(runtime.store, three["id"]))["status"] == "succeeded"
    assert wait_for(lambda: done(runtime.store, two["id"]))["status"] == "succeeded"


def test_memory_guard_stops_process_tree_and_other_sessions_survive(runtime):
    resident = runtime.submit(notebook("resident", "answer = 42"), cell_id="one")
    assert (
        wait_for(lambda: done(runtime.store, resident["id"]))["status"] == "succeeded"
    )
    over = runtime.submit(
        notebook(
            "over", "import time\nlarge = bytearray(220 * 1024 * 1024)\ntime.sleep(4)"
        ),
        trigger="workflow",
        resource_options={"memory_mb": 128},
    )
    result = wait_for(lambda: done(runtime.store, over["id"]))
    assert result["status"] == "failed" and result["error_class"] == "resource_limit"
    assert result["peak_memory_bytes"] > 128 * 1024**2 and "128 MB" in result["error"]
    again = runtime.submit(notebook("resident", "print(answer)"), cell_id="one")
    assert (
        wait_for(lambda: done(runtime.store, again["id"]))["outputs"][0][
            "stdout"
        ].strip()
        == "42"
    )


def test_memory_guard_also_stops_descendants_with_their_own_process_group(runtime):
    source = "import subprocess,sys,time\nchild = subprocess.Popen([sys.executable, '-c', 'import time; data=bytearray(220*1024*1024); time.sleep(8)'], start_new_session=True)\nprint(child.pid, flush=True)\ntime.sleep(8)"
    over = runtime.submit(
        notebook("child-allocation", source),
        trigger="workflow",
        resource_options={"memory_mb": 128},
    )
    result = wait_for(lambda: done(runtime.store, over["id"]))
    assert result["error_class"] == "resource_limit"
    import psutil

    text = "".join(item["text"] for item in result.get("logs", [])).strip()
    if text.isdigit():
        wait_for(lambda: not psutil.pid_exists(int(text)))


def test_idle_session_eviction_during_unrelated_active_work(runtime):
    runtime.idle_timeout = 1
    first = runtime.submit(notebook("idle", "answer=42"), cell_id="one")
    assert wait_for(lambda: done(runtime.store, first["id"]))["status"] == "succeeded"
    idle = runtime.sessions[first["session_id"]]
    second = runtime.submit(
        notebook("busy", "import time; time.sleep(1.2)"), trigger="workflow"
    )
    wait_for(lambda: runtime.active_id == second["id"])
    idle.last_activity = time.monotonic() - 10
    assert runtime._expire_idle() is True
    assert idle.process is None
    assert wait_for(lambda: done(runtime.store, second["id"]))["status"] == "succeeded"


def test_resource_policy_host_ceiling_and_project_inheritance():
    with pytest.raises(ValueError):
        normalize_policy({"memory_budget_mb": 4096}, ceiling=3072)
    policy = normalize_policy(
        {
            "memory_budget_mb": 1024,
            "default_memory_mb": 256,
            "max_memory_mb": 512,
            "max_concurrent": 2,
            "project_limits": {"p": {"priority": 3}},
        },
        ceiling=3072,
    )
    assert policy["project_limits"]["p"] == {
        "memory_budget_mb": 1024,
        "max_concurrent": 2,
        "priority": 3,
    }


def test_remote_control_dispatches_parallel_work_and_proxies_resource_policy(
    tmp_path, monkeypatch
):
    token = "resource-control-compute-test-token-123456789"
    with TestClient(create_compute_app(tmp_path / "compute", token=token)) as compute:
        headers = {"Authorization": "Bearer " + token}
        policy = {
            "memory_budget_mb": 1024,
            "default_memory_mb": 256,
            "max_memory_mb": 512,
            "max_concurrent": 2,
        }
        assert (
            compute.put("/resources/policy", headers=headers, json=policy).status_code
            == 200
        )

        def transport(request):
            response = compute.request(
                request.method,
                request.url.path,
                content=request.content,
                headers=headers,
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers={"Content-Type": "application/json"},
            )

        monkeypatch.setattr(
            compute_client,
            "client_for",
            lambda store, entry: httpx.Client(
                base_url="http://agent", transport=httpx.MockTransport(transport)
            ),
        )
        store = Store(tmp_path / "control")
        store.put(
            "clusters",
            {"id": "remote", "name": "Remote", "url": "http://agent", "type": "remote"},
        )
        runtime = compute_client.ClusterRuntime(store)
        runtime.start()
        try:
            assert runtime.resource_policy("remote")["default_memory_mb"] == 256
            first = runtime.submit(
                notebook("one", "import time; time.sleep(1)"),
                cluster_id="remote",
                trigger="workflow",
            )
            second = runtime.submit(
                notebook("two", "import time; time.sleep(1)"),
                cluster_id="remote",
                trigger="workflow",
            )
            wait_for(lambda: runtime.resource_status("remote")["active_runs"] == 2)
            for run in (first, second):
                result = wait_for(lambda: done(store, run["id"]))
                assert (
                    result["status"] == "succeeded"
                    and result["resource_options"]["memory_mb"] == 256
                )
        finally:
            runtime.close()


@pytest.mark.skipif(
    os.getenv("OPENBRICKS_TEST_SPARK") != "1",
    reason="Enable in the packaged Spark/Hive environment",
)
def test_real_parallel_spark_sessions_handoff_local_catalog_safely(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENBRICKS_CATALOG_URL", raising=False)
    monkeypatch.delenv("OPENBRICKS_S3_ENDPOINT", raising=False)
    monkeypatch.setenv("SPARK_MASTER", "local[1]")
    monkeypatch.setenv("SPARK_DRIVER_MEMORY", "512m")
    store = Store(tmp_path / "catalog-handoff")
    runtime = LocalRuntime(store)
    runtime.update_resource_policy(
        {
            "memory_budget_mb": 3072,
            "max_concurrent": 2,
            "default_memory_mb": 1536,
            "max_memory_mb": 1536,
        }
    )
    runtime.start()
    try:
        first = runtime.submit(
            notebook(
                "first",
                "import time\nspark.sql('CREATE DATABASE IF NOT EXISTS spark_catalog.shared_sessions')\nspark.sql('CREATE TABLE IF NOT EXISTS spark_catalog.shared_sessions.answers USING PARQUET AS SELECT 42 AS answer')\nprint('catalog ready', flush=True)\ntime.sleep(3)",
            ),
            cell_id="one",
        )
        wait_for(
            lambda: any(
                "catalog ready" in item["text"]
                for item in store.get("runs", first["id"]).get("logs", [])
            ),
            timeout=100,
        )
        second = runtime.submit(
            {
                "id": "second",
                "name": "Second session",
                "cells": [
                    {
                        "id": "one",
                        "language": "sql",
                        "source": "SELECT answer FROM spark_catalog.shared_sessions.answers",
                    }
                ],
            },
            trigger="workflow",
        )
        wait_for(lambda: runtime.active_count == 2)
        result_first = wait_for(lambda: done(store, first["id"]), timeout=100)
        result_second = wait_for(lambda: done(store, second["id"]), timeout=100)
        assert result_first["status"] == result_second["status"] == "succeeded", (
            result_first.get("error"),
            result_second.get("error"),
        )
        assert result_second["outputs"][0]["result"]["rows"] == [[42]]
        assert any(
            item["reason"] == "local_catalog_handoff"
            for item in runtime.resource_status()["recent_sessions"]
        )
    finally:
        runtime.close()
