"""Real Spark profiling, Hive lease, and Unity worker proof on disposable port3001.

No Docker commands are executed. Configure the separate remote compute service
with OPENBRICKS_SPARK_CATALOG_MODE=unity, a reachable UC URL/catalog, local[1],
a512m driver heap, and at least2304MiB container memory (3GiB recommended).
Only the explicit loopback QA UI origin http://127.0.0.1:3001 is accepted.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

TERMINAL = {"succeeded", "failed", "cancelled"}


def disposable_url(value):
    parsed = urlsplit(value)
    if (
        (parsed.scheme, parsed.hostname, parsed.port, parsed.path)
        not in {("http", "127.0.0.1", 3001, ""), ("http", "127.0.0.1", 3001, "/")}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use only the disposable UI proxy http://127.0.0.1:3001")
    return value.rstrip("/")


def spark_active(run):
    spark = (run.get("profile") or {}).get("spark", {})
    return (
        spark.get("status") == "measured"
        and any(job.get("status") == "RUNNING" for job in spark.get("jobs", []))
        and any(stage.get("status") == "ACTIVE" for stage in spark.get("stages", []))
    )


class Proof:
    def __init__(self, args):
        self.args, self.url = args, disposable_url(args.base_url)
        self.client = httpx.Client(
            base_url=self.url,
            timeout=httpx.Timeout(180, connect=10),
            follow_redirects=False,
        )
        self.suffix = uuid4().hex[:10]
        self.path = Path(args.receipt).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = {
            "format": "openbricks-v5-compute-proof-v1",
            "checks": [],
            "runs": [],
            "suffix": self.suffix,
            "passed": False,
        }
        self.redactions = []

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def check(self, label):
        self.state["checks"].append(label)
        self.save()
        print("PASS: " + label, flush=True)

    def api(self, method, path, **kwargs):
        response = self.client.request(method, "/api" + path, **kwargs)
        if not response.is_success:
            text = response.text[:1500]
            for secret in self.redactions:
                text = text.replace(secret, "[REDACTED]")
            raise AssertionError(
                f"{method} {path}: HTTP {response.status_code}: {text}"
            )
        return response.json() if response.content else None

    def wait(self, function, predicate, label, seconds=240):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = function()
            if predicate(value):
                return value
            time.sleep(0.35)
        raise AssertionError("Timed out waiting for " + label)

    def setup(self):
        password = os.getenv("OPENBRICKS_SMOKE_PASSWORD") or os.getenv(
            "OPENBRICKS_ADMIN_PASSWORD"
        )
        if not password:
            raise ValueError(
                "Set OPENBRICKS_SMOKE_PASSWORD for the disposable workspace"
            )
        self.redactions.append(password)
        self.api(
            "POST",
            "/auth/login",
            json={
                "username": os.getenv("OPENBRICKS_SMOKE_USERNAME", "admin"),
                "password": password,
            },
        )
        assert self.api("GET", "/auth/me")["role"] == "admin"
        self.project = self.api(
            "POST",
            "/projects",
            json={"name": "v5 compute " + self.suffix, "cluster_ids": ["local"]},
        )["id"]
        self.state["project_id"] = self.project

    def resources(self, cluster):
        return self.api(
            "GET",
            "/resources/status",
            params={"cluster_id": cluster, "project_id": self.project},
        )

    @contextmanager
    def policy(self, cluster, concurrent=2):
        self.wait(
            lambda: self.resources(cluster),
            lambda value: value["active_runs"] == value["queued_runs"] == 0,
            "idle compute",
        )
        old = self.api("GET", "/resources/policy", params={"cluster_id": cluster})
        budget = old["memory_budget_mb"]
        assert (
            budget >= 2048
        ), "Two real Spark workers require at least2048MiB admission capacity"
        projects = {
            key: {
                **value,
                "max_concurrent": min(
                    concurrent, value.get("max_concurrent") or concurrent
                ),
                "memory_budget_mb": min(
                    budget, value.get("memory_budget_mb") or budget
                ),
            }
            for key, value in old.get("project_limits", {}).items()
        }
        projects[self.project] = {
            "max_concurrent": concurrent,
            "memory_budget_mb": budget,
            "priority": 0,
        }
        body = {
            **old,
            "max_concurrent": concurrent,
            "default_memory_mb": 1024,
            "max_memory_mb": min(budget, 1536),
            "max_sessions": 4,
            "project_limits": projects,
        }
        self.api("PUT", "/resources/policy", params={"cluster_id": cluster}, json=body)
        try:
            yield
        finally:
            for identifier in self.state["runs"]:
                run = self.api("GET", f"/runs/{identifier}")
                if run["status"] not in TERMINAL:
                    self.api("POST", f"/runs/{identifier}/cancel", json={})
            self.wait(
                lambda: self.resources(cluster),
                lambda value: value["active_runs"] == value["queued_runs"] == 0,
                "fixture cleanup",
            )
            self.api(
                "PUT", "/resources/policy", params={"cluster_id": cluster}, json=old
            )

    def notebook(self, name, source, cluster="local"):
        return self.api(
            "POST",
            "/notebooks",
            json={
                "name": name + " " + self.suffix,
                "project_id": self.project,
                "cluster_id": cluster,
                "cells": [
                    {"id": "compute-proof", "language": "python", "source": source}
                ],
            },
        )

    def submit(self, notebook, memory=1024):
        result = self.api(
            "POST",
            f"/notebooks/{notebook['id']}/run",
            json={
                "timeout_seconds": 240,
                "resource_options": {"mode": "job", "memory_mb": memory},
            },
        )
        self.state["runs"].append(result["id"])
        self.save()
        return result["id"]

    def run(self, identifier):
        return self.api("GET", f"/runs/{identifier}")

    def done(self, identifier):
        return self.wait(
            lambda: self.run(identifier),
            lambda value: value["status"] in TERMINAL,
            "completed Spark run",
        )

    def profiling(self):
        with self.policy("local"):
            target = self.notebook(
                "Profile aggregate",
                # Force a real exchange even when the worker uses local[1].
                "display(spark.range(2000, numPartitions=2).selectExpr('id % 7 AS grouping').groupBy('grouping').count())",
            )
            first = self.done(self.submit(target, memory=1536))
            assert first["status"] == "succeeded", first.get("error")
            second = self.done(self.submit(target, memory=1536))
            assert second["status"] == "succeeded", second.get("error")
            for run in (first, second):
                result = self.api("GET", f"/runs/{run['id']}/profile")
                profile = result["profile"]
                assert profile["resources"]["status"] == "measured"
                assert 0 < len(profile["resources"]["samples"]) <= 240
                assert profile["resources"]["peak"]["python_rss_bytes"] > 0
                assert profile["resources"]["peak"]["jvm_rss_bytes"] > 0
                assert profile["spark"]["status"] == "measured", profile["spark"].get(
                    "unavailable_reason"
                )
                assert (
                    profile["spark"]["jobs"]
                    and profile["spark"]["stages"]
                    and profile["spark"]["plans"]
                )
                assert (
                    sum(
                        stage["shuffle_write_bytes"]
                        for stage in profile["spark"]["stages"]
                    )
                    > 0
                )
            compared = self.api(
                "GET",
                f"/runs/{second['id']}/profile",
                params={"compare_run_id": first["id"]},
            )
            assert (
                compared["comparison"]["metrics"]["duration_ms"]["delta"]
                == second["duration_ms"] - first["duration_ms"]
            )
            self.check(
                "real Spark plans, stages, shuffle and Python/JVM memory profiles with captured-run comparison"
            )

    def slow_sql(self, marker, rows):
        function = "pause_" + self.suffix
        return f"""import time
from pyspark.sql.types import LongType
spark.createDataFrame([({marker},)], ['marker']).createOrReplaceTempView('isolation_fixture')
print('ISOLATION_MARKER', spark.sql('SELECT marker FROM isolation_fixture').first().marker, flush=True)
def hold(value):
    time.sleep(.06)
    return int(value)
spark.udf.register({function!r}, hold, LongType())
display(spark.sql('SELECT SUM({function}(id)) AS total FROM range({rows})'))
print('FINAL_MARKER', spark.sql('SELECT marker FROM isolation_fixture').first().marker, flush=True)
"""

    def hive(self):
        with self.policy("local"):
            assert self.resources("local")["spark_catalog_mode"] == "hive"
            targets = [
                self.notebook("Hive lease " + str(index), self.slow_sql(index, 160))
                for index in (1, 2)
            ]
            identifiers = [self.submit(target) for target in targets]
            pair = self.wait(
                lambda: [self.run(identifier) for identifier in identifiers],
                lambda values: any(spark_active(value) for value in values)
                and any(
                    (value.get("profile") or {}).get("wait_reason")
                    == "Waiting for the local Hive catalog owner"
                    for value in values
                ),
                "one Spark SQL worker and one visible Hive lease waiter",
            )
            assert not all(spark_active(value) for value in pair)
            active = next(value for value in pair if spark_active(value))
            waiting = next(value for value in pair if value["id"] != active["id"])
            self.api("POST", f"/runs/{active['id']}/cancel", json={})
            assert self.done(active["id"])["status"] == "cancelled"
            result = self.done(waiting["id"])
            assert result["status"] == "succeeded", result.get("error")
            assert result["profile"]["spark_wait_ms"] > 0
            self.check(
                "default Hive keeps one Spark owner, reports catalog wait and safely hands off after cancellation"
            )

    def unity(self):
        # The legacy catalog proof creates a dedicated schema; Unity's default
        # namespace is a separate prerequisite for unqualified SQL/temp views.
        for path, body in (
            ("/catalog/catalogs", {"project_id": self.project, "name": "unity"}),
            (
                "/catalog/schemas",
                {
                    "project_id": self.project,
                    "catalog_name": "unity",
                    "name": "default",
                },
            ),
        ):
            response = self.client.post("/api" + path, json=body)
            assert response.status_code in (
                201,
                409,
            ), f"Catalog setup failed: HTTP {response.status_code}"
        cluster = os.getenv("OPENBRICKS_SMOKE_UNITY_CLUSTER_ID")
        if not cluster:
            endpoint = os.getenv("OPENBRICKS_SMOKE_REMOTE_URL", "http://remote:8001")
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in ("remote", "remote-compute")
                or parsed.port != 8001
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "Use only the isolated remote:8001 service for this proof"
                )
            token = os.environ["OPENBRICKS_SMOKE_REMOTE_TOKEN"]
            self.redactions.append(token)
            cluster = self.api(
                "POST",
                "/clusters",
                json={
                    "name": "v5 unity " + self.suffix,
                    "url": endpoint,
                    "token": token,
                    "allow_insecure": True,
                },
            )["id"]
        project = next(
            item for item in self.api("GET", "/projects") if item["id"] == self.project
        )
        self.api(
            "PUT",
            f"/projects/{self.project}",
            json={**project, "cluster_ids": ["local", cluster]},
        )
        self.state["unity_cluster_id"] = cluster
        with self.policy(cluster):
            resources = self.resources(cluster)
            assert resources["spark_catalog_mode"] == "unity"
            assert resources["local_spark_catalog_concurrency"] >= 2
            targets = [
                self.notebook(
                    "Unity isolation " + str(marker),
                    self.slow_sql(marker, 400),
                    cluster,
                )
                for marker in (111, 222)
            ]
            identifiers = [self.submit(target) for target in targets]
            pair = self.wait(
                lambda: [self.run(identifier) for identifier in identifiers],
                lambda values: all(spark_active(value) for value in values),
                "simultaneous real Spark SQL stages in two Unity-only JVMs",
            )
            applications = [
                value["profile"]["spark"]["application_id"] for value in pair
            ]
            assert len(set(applications)) == 2
            for run, marker in zip(pair, (111, 222)):
                text = "".join(item.get("text", "") for item in run.get("logs", []))
                assert "ISOLATION_MARKER " + str(marker) in text
            self.state["overlapping_spark_applications"] = applications
            self.api("POST", f"/runs/{identifiers[0]}/cancel", json={})
            assert self.done(identifiers[0])["status"] == "cancelled"
            survivor = self.done(identifiers[1])
            assert survivor["status"] == "succeeded", survivor.get("error")
            assert "FINAL_MARKER 222" in "".join(
                output.get("stdout", "") for output in survivor["outputs"]
            )
            assert survivor["outputs"][0]["result"]["rows"] == [[79800]]
            self.check(
                "two real Unity Spark SQL stages overlap with distinct application IDs and isolated views; cancelling one preserves the other"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.getenv("OPENBRICKS_SMOKE_URL", "http://127.0.0.1:3001")
    )
    parser.add_argument("--receipt", default=".test-data/v5-compute-smoke.json")
    parser.add_argument(
        "--section", choices=("all", "profile", "hive", "unity"), default="all"
    )
    args = parser.parse_args()
    proof = Proof(args)
    try:
        proof.setup()
        for name in (
            ("profile", "hive", "unity") if args.section == "all" else (args.section,)
        ):
            {"profile": proof.profiling, "hive": proof.hive, "unity": proof.unity}[
                name
            ]()
        proof.state["passed"] = True
        proof.save()
    finally:
        proof.client.close()


if __name__ == "__main__":
    main()
