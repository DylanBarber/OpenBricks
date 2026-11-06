import json
import os
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from openbricks.execution import Redactor
from openbricks.profiling import (
    MAX_SAMPLES,
    ResourceSampler,
    SparkProfiler,
    cgroup_memory,
    new_profile,
    redact_spark_profile,
    register_profile_routes,
)
from openbricks.runtime import LocalRuntime
from openbricks.spark_catalog import execution_catalog
from openbricks.store import Store


class Process:
    def __init__(self, pid, name, rss, cpu=0):
        self.pid, self.label, self.rss, self.cpu = pid, name, rss, cpu
        self.descendants = []

    def create_time(self):
        return self.pid * 10

    def name(self):
        return self.label

    def memory_info(self):
        return SimpleNamespace(rss=self.rss)

    def cpu_times(self):
        return SimpleNamespace(user=self.cpu, system=0)

    def children(self, recursive=True):
        return self.descendants


def test_resource_samples_separate_python_jvm_container_and_cpu(tmp_path, monkeypatch):
    from openbricks import profiling

    current = [10.0]
    parent = Process(1, "python.exe", 40)
    java = Process(2, "java", 100)
    other = Process(3, "native", 15)
    parent.descendants = [java, other]
    monkeypatch.setattr(
        profiling,
        "cgroup_memory",
        lambda: {"container_current_bytes": 200, "container_limit_bytes": 1024},
    )
    sampler = ResourceSampler(
        process_factory=lambda pid: parent, clock=lambda: current[0]
    )
    first = sampler.sample(1)
    assert first["cpu_percent"] is None and first["total_rss_bytes"] == 155
    current[0] += 1
    parent.cpu, java.cpu = 0.2, 0.3
    second = sampler.sample(1)
    assert second["cpu_percent"] == 50
    assert second["python_rss_bytes"] == 40 and second["jvm_rss_bytes"] == 100
    assert second["container_current_bytes"] == 200
    assert sampler.snapshot()["peak"]["total_rss_bytes"] == 155


def test_long_profiles_are_bounded_but_preserve_exact_peak():
    current = [0.0]
    parent = Process(1, "python", 10)
    sampler = ResourceSampler(
        process_factory=lambda pid: parent, clock=lambda: current[0]
    )
    for index in range(1000):
        current[0] += 1
        parent.rss = 9999 if index == 401 else 10
        sampler.sample(1, force=True)
    value = sampler.snapshot()
    assert len(value["samples"]) <= MAX_SAMPLES and value["truncated"]
    assert value["samples"][0]["elapsed_ms"] == 1000
    assert value["samples"][-1]["elapsed_ms"] == 1_000_000
    assert value["peak"]["python_rss_bytes"] == 9999


def test_cgroup_counters_never_substitute_host_ram(tmp_path):
    assert cgroup_memory(tmp_path) == {
        "container_current_bytes": None,
        "container_limit_bytes": None,
    }
    (tmp_path / "memory.current").write_text("512")
    (tmp_path / "memory.max").write_text("max")
    assert cgroup_memory(tmp_path) == {
        "container_current_bytes": 512,
        "container_limit_bytes": None,
    }
    (tmp_path / "memory.max").write_text("1024")
    assert cgroup_memory(tmp_path)["container_limit_bytes"] == 1024


def test_spark_diagnostics_redact_user_text_without_corrupting_schema():
    redactor = Redactor([{"secrets": {"one": "status", "two": "private-password"}}])
    value = {
        "status": "measured",
        "jobs": [{"id": 1, "status": "SUCCEEDED", "stage_ids": [2]}],
        "stages": [
            {
                "id": 2,
                "status": "COMPLETE",
                "name": "private-password",
                "input_bytes": 20,
            }
        ],
        "plans": [
            {"cell_id": "status", "plan": "Filter private-password", "truncated": False}
        ],
        "private-password": "must not retain unknown keys",
    }
    safe = redact_spark_profile(value, redactor)
    assert safe["status"] == "measured" and safe["jobs"][0]["status"] == "SUCCEEDED"
    assert safe["plans"][0]["cell_id"] == "status"
    assert safe["stages"][0]["name"] == "[REDACTED]"
    assert "private-password" not in json.dumps(safe)


def test_no_spark_reports_not_started_and_failed_collection_is_explicit():
    profiler = SparkProfiler("run", lambda: None)
    assert profiler.collect()["status"] == "not_started"
    profiler = SparkProfiler("run", lambda: object())
    assert profiler.collect()["status"] == "unavailable"


def test_catalog_concurrency_is_explicit_and_rejects_unsafe_configuration(monkeypatch):
    monkeypatch.delenv("OPENBRICKS_SPARK_CATALOG_MODE", raising=False)
    assert execution_catalog()["shared_derby"] is True
    monkeypatch.setenv("OPENBRICKS_SPARK_CATALOG_MODE", "unity")
    monkeypatch.delenv("OPENBRICKS_CATALOG_URL", raising=False)
    with pytest.raises(ValueError, match="requires OPENBRICKS_CATALOG_URL"):
        execution_catalog()

    monkeypatch.setenv("OPENBRICKS_CATALOG_URL", "http://catalog:8080")
    monkeypatch.setenv("OPENBRICKS_CATALOG_NAMES", "shared,analytics")
    assert execution_catalog() == {
        "mode": "unity",
        "default_catalog": "shared",
        "shared_derby": False,
    }
    monkeypatch.setenv("OPENBRICKS_SPARK_DEFAULT_CATALOG", "spark_catalog")
    with pytest.raises(ValueError):
        execution_catalog()
    monkeypatch.setenv("OPENBRICKS_SPARK_DEFAULT_CATALOG", "other")
    with pytest.raises(ValueError, match="must be in"):
        execution_catalog()


def test_unity_catalog_workers_do_not_take_the_shared_derby_lease(
    tmp_path, monkeypatch
):
    from openbricks.sessions import SessionRuntime

    monkeypatch.setenv("OPENBRICKS_SPARK_CATALOG_MODE", "unity")
    monkeypatch.setenv("OPENBRICKS_CATALOG_URL", "http://catalog:8080")
    monkeypatch.setenv("OPENBRICKS_CATALOG_NAMES", "unity")
    monkeypatch.delenv("OPENBRICKS_SPARK_DEFAULT_CATALOG", raising=False)
    runtime = SessionRuntime(Store(tmp_path), lambda *args, **kwargs: None)
    kernels = [
        SimpleNamespace(
            stopping=False,
            process=SimpleNamespace(poll=lambda: None),
            has_spark=False,
            session_info={"id": str(index)},
            state_lock=threading.RLock(),
        )
        for index in range(2)
    ]
    runtime._active = {str(index): {"session_id": str(index)} for index in range(2)}
    assert runtime.acquire_spark(kernels[0], "0")
    assert runtime.acquire_spark(kernels[1], "1")
    assert all(kernel.has_spark for kernel in kernels)
    assert runtime._spark_owner is None


def test_profile_api_authorizes_both_runs_and_limits_comparison(tmp_path):
    store = Store(tmp_path)
    for id, notebook, project in (
        ("one", "nb", "default"),
        ("two", "nb", "default"),
        ("other", "nb", "private"),
        ("different", "else", "default"),
    ):
        store.put(
            "runs",
            {
                "id": id,
                "notebook_id": notebook,
                "project_id": project,
                "status": "succeeded",
                "profile": new_profile(),
                "duration_ms": 200 if id == "one" else 100,
            },
        )
    app = FastAPI()
    app.state.store = store

    class Access:
        def require_project(self, user, project, action):
            if project != "default":
                raise HTTPException(403)

    register_profile_routes(app, lambda: {"id": "viewer"}, Access())
    with TestClient(app) as client:
        result = client.get("/api/runs/one/profile?compare_run_id=two")
        assert result.status_code == 200
        assert result.json()["comparison"]["metrics"]["duration_ms"]["delta"] == 100
        assert (
            client.get("/api/runs/one/profile?compare_run_id=other").status_code == 403
        )
        assert (
            client.get("/api/runs/one/profile?compare_run_id=different").status_code
            == 400
        )
        assert client.get("/api/runs/missing/profile").status_code == 404


def test_real_python_worker_persists_bounded_memory_profile_and_redacted_native_log(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    monkeypatch.delenv("OPENBRICKS_SPARK_CATALOG_MODE", raising=False)
    store = Store(tmp_path)
    runtime = LocalRuntime(store)
    runtime.start()
    secret = "synthetic-native-profile-password"
    notebook = {
        "id": "profile",
        "name": "Profile",
        "cells": [
            {
                "id": "cell",
                "language": "python",
                "source": "import os,time\nvalue=notebook_secrets.get('scope','password')\nos.write(2, ('native '+value+'\\n').encode())\nlarge=bytearray(10*1024*1024)\ntime.sleep(.7)\nprint(value)",
            }
        ],
    }
    try:
        run = runtime.submit(
            notebook,
            notebook_secrets=[{"scope": "scope", "key": "password", "value": secret}],
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = store.get("runs", run["id"])
            if result["status"] not in ("running", "queued"):
                break
            time.sleep(0.03)
        assert result["status"] == "succeeded", result.get("error")
        assert result["profile"]["resources"]["status"] == "measured"
        assert len(result["profile"]["resources"]["samples"]) >= 2
        assert (
            result["profile"]["resources"]["peak"]["python_rss_bytes"] >= 10 * 1024**2
        )
        assert result["profile"]["spark"]["status"] == "not_started"
        assert secret not in json.dumps(result)
        runtime.reset()
        assert secret not in (tmp_path / "logs/worker.log").read_text()
        assert "[REDACTED]" in (tmp_path / "logs/worker.log").read_text()
    finally:
        runtime.close()


def test_rotated_and_unselected_secrets_stay_redacted_in_warm_python_and_native_logs(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENBRICKS_SPARK_CATALOG_MODE", raising=False)
    runtime = LocalRuntime(Store(tmp_path))
    runtime.start()
    first, second = "old-synthetic-secret-rotation", "new-synthetic-secret-rotation"

    def execute(source, values):
        run = runtime.submit(
            {
                "id": "warm-secrets",
                "name": "Warm secrets",
                "cells": [{"id": "cell", "language": "python", "source": source}],
            },
            cell_id="cell",
            notebook_secrets=values,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = runtime.store.get("runs", run["id"])
            if value["status"] not in ("queued", "running"):
                return value
            time.sleep(0.03)
        pytest.fail("Warm secret run did not complete")

    try:
        assert (
            execute(
                "previous = dbutils.secrets.get('scope','key')",
                [{"scope": "scope", "key": "key", "value": first}],
            )["status"]
            == "succeeded"
        )
        rotated = execute(
            "import os\nprint(previous, dbutils.secrets.get('scope','key'))\nos.write(2, (previous+'\\n').encode())",
            [{"scope": "scope", "key": "key", "value": second}],
        )
        revoked = execute("print(notebook_secrets.listScopes())\nprint(previous)", [])
        assert rotated["status"] == revoked["status"] == "succeeded"
        assert first not in json.dumps(rotated) + json.dumps(revoked)
        assert second not in json.dumps(rotated)
        assert "[]" in revoked["outputs"][0]["stdout"]
        runtime.reset()
        assert first not in (tmp_path / "logs/worker.log").read_text()
    finally:
        runtime.close()


def test_compute_rejects_secret_envelopes_without_echoing_input(tmp_path, monkeypatch):
    from openbricks.compute import create_compute_app

    monkeypatch.delenv("OPENBRICKS_SPARK_CATALOG_MODE", raising=False)
    password = "rejected-secret-must-not-appear"
    notebook = {"id": "input", "name": "Input", "cells": []}
    with TestClient(create_compute_app(tmp_path, token="t" * 32)) as client:
        response = client.post(
            "/runs",
            headers={"Authorization": "Bearer " + "t" * 32},
            json={
                "run_id": "input",
                "notebook": notebook,
                "notebook_secrets": [
                    {"scope": "scope", "key": "key", "value": password}
                ]
                * 33,
            },
        )
        assert response.status_code == 422 and password not in response.text
        response = client.post(
            "/runs",
            headers={"Authorization": "Bearer " + "t" * 32},
            json={
                "run_id": "input",
                "notebook": notebook,
                "notebook_secrets": [
                    {"scope": "scope", "key": "key", "value": password * 300}
                ],
            },
        )
        assert response.status_code == 400 and password not in response.text


@pytest.mark.skipif(
    os.getenv("OPENBRICKS_TEST_SPARK") != "1",
    reason="Enable real Java/Spark integration checks",
)
def test_real_spark_status_store_reports_stages_shuffle_and_plans(
    tmp_path, owned_spark_gateway
):
    from pyspark.sql import SparkSession

    spark = (
        SparkSession.builder.master("local[1]")
        .appName("OpenBricks profile verification")
        .config("spark.ui.enabled", "false")
        .config("spark.driver.memory", "512m")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.sql.warehouse.dir", (tmp_path / "warehouse").as_uri())
        .getOrCreate()
    )
    try:
        profiler = SparkProfiler("run-profile", lambda: spark)
        profiler.cell("aggregate")
        frame = (
            # A single-partition range can satisfy the aggregate without an
            # exchange, even with shuffle partitions configured. Force input
            # partitions so the assertion below verifies real shuffle metrics.
            spark.range(1000, numPartitions=2)
            .selectExpr("id % 5 AS grouping")
            .groupBy("grouping")
            .count()
        )
        profiler.plan(frame)
        assert len(frame.collect()) == 5
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = profiler.collect()
            if (
                result["status"] == "measured"
                and result["stages"]
                and all(item["status"] == "SUCCEEDED" for item in result["jobs"])
            ):
                break
            time.sleep(0.1)
        assert result["status"] == "measured", result
        assert result["jobs"] and result["stages"] and result["plans"]
        assert sum(item["shuffle_write_bytes"] for item in result["stages"]) > 0
        assert sum(item["executor_run_ms"] for item in result["stages"]) >= 0
        assert all(item["cell_id"] == "aggregate" for item in result["plans"])
    finally:
        spark.stop()
