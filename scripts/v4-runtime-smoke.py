"""Real HTTP proof for a disposable OpenBricks v4 workspace, through the UI proxy.

The script creates synthetic fixtures and never restores a valid workspace archive.
It leaves its fixtures and an encrypted archive available for a separate fresh-host
restore proof. Do not run it against a workspace that contains real user data.

Required environment:
  OPENBRICKS_SMOKE_PASSWORD (or OPENBRICKS_ADMIN_PASSWORD)
  OPENBRICKS_SMOKE_S3_ENDPOINT: endpoint reachable FROM the management container
  OPENBRICKS_SMOKE_S3_ACCESS_KEY / OPENBRICKS_SMOKE_S3_SECRET_KEY

Optional environment:
  OPENBRICKS_SMOKE_URL=http://127.0.0.1:3001
  OPENBRICKS_SMOKE_USERNAME=admin
  OPENBRICKS_SMOKE_CLUSTER_ID=local
  OPENBRICKS_SMOKE_RECEIPT=.test-data/v4-runtime-smoke.json

Example: python scripts/v4-runtime-smoke.py --section all
Only the explicitly disposable port 3001 is accepted. No Docker commands are run.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


class Proof:
    def __init__(self, args):
        self.url = args.base_url.rstrip("/")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.port != 3001
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
        ):
            raise ValueError(
                "This script accepts only a disposable UI origin on port 3001"
            )
        self.suffix = uuid4().hex[:10]
        self.cluster = os.getenv("OPENBRICKS_SMOKE_CLUSTER_ID", "local")
        self.output = Path(args.receipt).resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            base_url=self.url,
            timeout=httpx.Timeout(180, connect=10),
            follow_redirects=False,
        )
        self.state = {
            "format": "openbricks-v4-api-proof-v1",
            "base_url": self.url,
            "suffix": self.suffix,
            "cluster_id": self.cluster,
            "checks": [],
            "notebooks": {},
            "runs": [],
        }
        self.project = None

    def checkpoint(self):
        temporary = self.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.replace(self.output)

    def passed(self, name):
        self.state["checks"].append(name)
        self.checkpoint()
        print("PASS: " + name, flush=True)

    def request(self, method, path, *, expected=None, **kwargs):
        response = self.client.request(method, "/api" + path, **kwargs)
        if expected is not None:
            assert (
                response.status_code == expected
            ), f"{method} {path}: expected {expected}, received {response.status_code}: {response.text[:1500]}"
        else:
            assert (
                response.is_success
            ), f"{method} {path}: {response.status_code}: {response.text[:1500]}"
        return response.json() if response.content else None

    def wait(self, get, predicate, *, timeout=120, label="operation"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = get()
            if predicate(last):
                return last
            time.sleep(0.4)
        status = last.get("status") if isinstance(last, dict) else None
        raise AssertionError(f"Timed out waiting for {label}; last status={status!r}")

    def login(self):
        assert self.client.get("/").status_code == 200
        assert self.client.get("/api/notebooks").status_code == 401
        password = os.getenv("OPENBRICKS_SMOKE_PASSWORD") or os.getenv(
            "OPENBRICKS_ADMIN_PASSWORD"
        )
        if not password:
            raise ValueError(
                "Set OPENBRICKS_SMOKE_PASSWORD for the disposable QA account"
            )
        username = os.getenv(
            "OPENBRICKS_SMOKE_USERNAME", os.getenv("OPENBRICKS_ADMIN_USERNAME", "admin")
        )
        self.request(
            "POST", "/auth/login", json={"username": username, "password": password}
        )
        assert self.request("GET", "/auth/me")["role"] == "admin"
        project = self.request(
            "POST",
            "/projects",
            json={
                "name": "v4 API proof " + self.suffix,
                "description": "Disposable synthetic integration fixtures",
                "cluster_ids": [self.cluster],
            },
        )
        self.project = project["id"]
        self.state["project_id"] = self.project
        self.passed(
            "UI proxy, protected API, administrator cookie login, isolated project"
        )

    def resources(self):
        return self.request(
            "GET",
            "/resources/status",
            params={"project_id": self.project, "cluster_id": self.cluster},
        )

    @contextmanager
    def runtime_policy(self):
        old = self.request(
            "GET", "/resources/policy", params={"cluster_id": self.cluster}
        )
        budget = min(3072, old["memory_budget_mb"])
        assert budget >= 1024, "QA compute must allow two 512 MB workers"
        projects = {
            key: {
                **value,
                "max_concurrent": min(2, value.get("max_concurrent") or 2),
                "memory_budget_mb": min(
                    budget, value.get("memory_budget_mb") or budget
                ),
            }
            for key, value in old.get("project_limits", {}).items()
        }
        projects[self.project] = {
            "max_concurrent": 2,
            "memory_budget_mb": budget,
            "priority": 0,
        }
        configured = {
            **old,
            "memory_budget_mb": budget,
            "max_concurrent": 2,
            "max_sessions": 6,
            "default_memory_mb": 512,
            "max_memory_mb": 512,
            "idle_timeout_seconds": 600,
            "project_limits": projects,
        }
        self.request(
            "PUT",
            "/resources/policy",
            params={"cluster_id": self.cluster},
            json=configured,
        )
        self.state["resource_test_policy"] = configured
        try:
            yield
        finally:
            # A failure must not leave sleeping synthetic runs or a changed policy.
            for identifier in self.state["runs"]:
                value = self.request("GET", f"/runs/{identifier}")
                if value["status"] not in TERMINAL:
                    self.request("POST", f"/runs/{identifier}/cancel")
                    self.wait(
                        lambda identifier=identifier: self.request(
                            "GET", f"/runs/{identifier}"
                        ),
                        lambda value: value["status"] in TERMINAL,
                        label="cleanup cancellation",
                    )
            self.wait(
                self.resources,
                lambda value: value["active_runs"] == 0 and value["queued_runs"] == 0,
                timeout=60,
                label="compute release before policy restoration",
            )
            self.request(
                "PUT",
                "/resources/policy",
                params={"cluster_id": self.cluster},
                json=old,
            )

    def notebook(self, key, cells):
        result = self.request(
            "POST",
            "/notebooks",
            json={
                "name": f"v4 {key} {self.suffix}",
                "project_id": self.project,
                "cluster_id": self.cluster,
                "cells": [
                    {"id": name, "language": "python", "source": source}
                    for name, source in cells
                ],
            },
        )
        self.state["notebooks"][key] = result["id"]
        self.checkpoint()
        return result["id"]

    def submit(self, notebook, cell, *, mode="interactive", priority=0):
        value = self.request(
            "POST",
            f"/notebooks/{notebook}/run",
            json={
                "cell_id": cell,
                "cluster_id": self.cluster,
                "timeout_seconds": 90,
                "resource_options": {
                    "mode": mode,
                    "memory_mb": 512,
                    "priority": priority,
                },
            },
        )
        self.state["runs"].append(value["id"])
        return value["id"]

    def finished(self, identifier, *, expected="succeeded", timeout=120):
        value = self.wait(
            lambda: self.request("GET", f"/runs/{identifier}"),
            lambda item: item["status"] in TERMINAL,
            timeout=timeout,
            label="notebook run",
        )
        assert (
            value["status"] == expected
        ), f"Notebook ended {value['status']}: {value.get('error', '')[:1500]}"
        return value

    @staticmethod
    def stdout(run):
        return "".join(item.get("stdout", "") for item in run.get("outputs", []))

    def runtime(self):
        with self.runtime_policy():
            first = self.notebook(
                "state-alpha",
                [
                    ("initialize", "qa_value = 'alpha'\nprint('initialized alpha')"),
                    ("read", "print('state=' + qa_value)"),
                    ("fresh", "print('inherited=' + str('qa_value' in globals()))"),
                ],
            )
            second = self.notebook(
                "state-beta",
                [
                    ("initialize", "qa_value = 'beta'\nprint('initialized beta')"),
                    ("read", "print('state=' + qa_value)"),
                ],
            )
            a, b = self.submit(first, "initialize"), self.submit(second, "initialize")
            self.finished(a)
            self.finished(b)
            assert "state=alpha" in self.stdout(
                self.finished(self.submit(first, "read"))
            )
            assert "state=beta" in self.stdout(
                self.finished(self.submit(second, "read"))
            )
            for _ in range(2):
                assert "inherited=False" in self.stdout(
                    self.finished(self.submit(first, "fresh", mode="job"))
                )
            assert "state=alpha" in self.stdout(
                self.finished(self.submit(first, "read"))
            )
            sessions = self.resources()["sessions"]
            own = [item for item in sessions if item["notebook_id"] in (first, second)]
            assert len(own) == 2 and len({item["id"] for item in own}) == 2
            assert not any(item["has_spark"] for item in own)
            self.passed(
                "independent notebook Python state, targeted cell reuse, fresh one-shot jobs without Spark"
            )

            sleeper = self.notebook(
                "cancel-target",
                [
                    (
                        "sleep",
                        "import time\nprint('cancel-target-started', flush=True)\ntime.sleep(60)",
                    )
                ],
            )
            survivor = self.notebook(
                "cancel-survivor",
                [
                    (
                        "sleep",
                        "import time\nprint('survivor-started', flush=True)\ntime.sleep(12)\nprint('survivor-finished')",
                    )
                ],
            )
            queued = self.notebook(
                "queued-cancel",
                [("sleep", "print('this queued run must never execute')")],
            )
            target_run, survivor_run = self.submit(
                sleeper, "sleep", mode="job"
            ), self.submit(survivor, "sleep", mode="job")
            for identifier in (target_run, survivor_run):
                self.wait(
                    lambda identifier=identifier: self.request(
                        "GET", f"/runs/{identifier}"
                    ),
                    lambda item: item["status"] == "running",
                    label="parallel worker admission",
                )
            queued_run = self.submit(queued, "sleep", mode="job")
            queued_value = self.wait(
                lambda: self.request("GET", f"/runs/{queued_run}"),
                lambda item: item["status"] == "queued"
                and bool(item.get("queued_reason")),
                label="bounded concurrency queue",
            )
            assert not queued_value.get("started_at")
            resource = self.resources()
            assert resource["active_runs"] == 2 and resource["queued_runs"] >= 1
            assert (
                resource["memory_reserved_mb"]
                <= resource["policy"]["memory_budget_mb"]
                <= 3072
            )
            self.state["admission_snapshot"] = resource
            self.request("POST", f"/runs/{queued_run}/cancel")
            cancelled = self.finished(queued_run, expected="cancelled")
            assert "must never execute" not in self.stdout(cancelled)
            self.request("POST", f"/runs/{target_run}/cancel")
            self.finished(target_run, expected="cancelled")
            assert "survivor-finished" in self.stdout(self.finished(survivor_run))
            assert "state=alpha" in self.stdout(
                self.finished(self.submit(first, "read"))
            )
            self.passed(
                "two 512 MB workers, bounded admission queue, queued cancellation and isolated active cancellation"
            )

    def execution(self, body):
        started = self.request(
            "POST", "/data/execute", json={"project_id": self.project, **body}
        )
        value = self.wait(
            lambda: self.request("GET", f"/data/executions/{started['id']}"),
            lambda item: item["status"] in TERMINAL,
            label="DuckDB execution",
        )
        assert (
            value["status"] == "succeeded"
        ), f"DuckDB execution failed: {value.get('error', '')[:1500]}"
        assert value["result"]["engine"] == "duckdb"
        assert value["result"]["rows"] == [[42]], value["result"]
        return value

    def duckdb(self):
        bucket = "v4-local-" + self.suffix
        self.request(
            "POST", "/buckets", json={"name": bucket, "project_id": self.project}
        )
        self.request(
            "POST",
            f"/buckets/{bucket}/objects",
            data={"key": "input/values.csv"},
            files={"file": ("values.csv", b"value\n20\n22\n5\n", "text/csv")},
        )
        query = {
            "engine": "duckdb",
            "cluster_id": self.cluster,
            "sql": f"SELECT SUM(value) AS total FROM read_csv('blob://{bucket}/input/values.csv', header=true) WHERE value >= ?",
            "params": [20],
        }
        self.execution(query)
        saved = self.request(
            "POST",
            "/data/queries",
            json={
                "project_id": self.project,
                "name": "v4 DuckDB parameters " + self.suffix,
                **query,
            },
        )
        listed = self.request(
            "GET",
            "/data/queries",
            params={"project_id": self.project, "search": self.suffix},
        )
        loaded = next(item for item in listed["items"] if item["id"] == saved["id"])
        assert (
            loaded["engine"] == "duckdb"
            and loaded["params"] == [20]
            and loaded["sql"] == query["sql"]
        )
        # Only query_id is sent: this exercises the saved query's stored engine,
        # parameters, SQL, and compute target through the normal HTTP loader.
        self.execution({"query_id": loaded["id"]})
        assert not any(item["has_spark"] for item in self.resources()["sessions"])
        self.state.update(local_bucket=bucket, saved_query_id=saved["id"])
        self.passed(
            "parameterized local CSV DuckDB query, persisted saved query reload, no Spark JVM"
        )

    def remote_download(self, connection, bucket, key, destination):
        digest = hashlib.sha256()
        with self.client.stream(
            "GET",
            f"/api/connections/{connection}/download",
            params={"bucket": bucket, "key": key},
        ) as response:
            assert (
                response.is_success
            ), f"Remote download failed: {response.status_code}"
            with destination.open("wb") as target:
                for chunk in response.iter_bytes(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
        return digest.hexdigest()

    def workspace_healthy(self):
        assert self.client.get("/api/health").status_code == 200
        assert self.request("GET", "/auth/me")["role"] == "admin"
        assert any(
            item["id"] == self.project for item in self.request("GET", "/projects")
        )

    def backups(self):
        endpoint = os.environ.get("OPENBRICKS_SMOKE_S3_ENDPOINT")
        if not endpoint:
            raise ValueError(
                "Set OPENBRICKS_SMOKE_S3_ENDPOINT to storage reachable from the QA management container"
            )
        connection = self.request(
            "POST",
            "/connections",
            json={
                "name": "v4 backup storage " + self.suffix,
                "type": "s3",
                "project_ids": [self.project],
                "config": {
                    "endpoint_url": endpoint,
                    "region": "us-east-1",
                    "addressing_style": "path",
                    "verify_tls": True,
                },
                "secrets": {
                    "access_key": os.getenv(
                        "OPENBRICKS_SMOKE_S3_ACCESS_KEY", "qa-access-key"
                    ),
                    "secret_key": os.getenv(
                        "OPENBRICKS_SMOKE_S3_SECRET_KEY", "qa-secret-key"
                    ),
                },
            },
        )
        identifier = connection["id"]
        source_bucket, target_bucket = (
            "v4-source-" + self.suffix,
            "v4-recovery-" + self.suffix,
        )
        for bucket in (source_bucket, target_bucket):
            self.request(
                "POST", f"/connections/{identifier}/buckets", json={"name": bucket}
            )
        expected_object = (
            "OpenBricks synthetic recovery fixture " + self.suffix
        ).encode()
        for key, data in (
            ("selected/proof.txt", expected_object),
            ("excluded/not-selected.txt", b"not part of the backup"),
        ):
            self.request(
                "POST",
                f"/connections/{identifier}/objects",
                data={"bucket": source_bucket, "key": key},
                files={"file": ("proof.txt", data, "text/plain")},
            )
        phrase = "synthetic-v4-recovery-" + self.suffix
        policy = self.request(
            "POST",
            "/operations/backup-policies",
            json={
                "name": "v4 encrypted recovery " + self.suffix,
                "project_id": self.project,
                "connection_id": identifier,
                "bucket": target_bucket,
                "prefix": "snapshots",
                "cron": "0 2 * * *",
                "timezone": "UTC",
                "enabled": False,
                "passphrase": phrase,
                "include_data": True,
                "storage_sources": [
                    {
                        "connection_id": identifier,
                        "bucket": source_bucket,
                        "prefix": "selected/",
                    }
                ],
                "include_catalog": False,
                "verify_restore": True,
                "notification_ids": [],
                "keep_remote": 3,
            },
        )
        assert (
            "passphrase" not in policy
            and "_passphrase" not in policy
            and policy["has_passphrase"]
        )
        started = self.request(
            "POST", f"/operations/backup-policies/{policy['id']}/run"
        )
        result = self.wait(
            lambda: self.request("GET", f"/operations/backup-runs/{started['id']}"),
            lambda item: item["status"] in ("succeeded", "failed"),
            timeout=300,
            label="encrypted upload and isolated restore drill",
        )
        assert (
            result["status"] == "succeeded"
        ), f"Backup failed: {result.get('error')} / {result.get('verification_error')}"
        assert (
            result["verification_status"] == "succeeded"
            and result["restore_drill"]["passed"]
        )
        receipt = result["remote"]
        assert receipt["verified"] and receipt["encrypted"]
        archive = self.output.parent / f"v4-{self.suffix}.obbackup"
        digest = self.remote_download(
            identifier, target_bucket, receipt["key"], archive
        )
        assert digest == receipt["sha256"] and archive.stat().st_size == receipt["size"]
        with archive.open("rb") as stream:
            assert stream.read(6) == b"OBBK1\n"
        receipt_file = self.output.parent / f"v4-{self.suffix}.receipt.json"
        self.remote_download(
            identifier, target_bucket, receipt["key"] + ".receipt.json", receipt_file
        )
        assert json.loads(receipt_file.read_text()) == receipt
        self.state["backup"] = {
            "policy_id": policy["id"],
            "run_id": started["id"],
            "connection_id": identifier,
            "source_bucket": source_bucket,
            "target_bucket": target_bucket,
            "source_key": "selected/proof.txt",
            "source_expected_text": expected_object.decode(),
            "excluded_key": "excluded/not-selected.txt",
            "receipt": receipt,
            "archive": str(archive),
            "receipt_file": str(receipt_file),
            "passphrase": phrase,
            "endpoint_url": endpoint,
        }
        self.passed(
            "selected S3 source, encrypted off-host upload, immutable receipt and real isolated restore drill"
        )

        self.request("POST", f"/operations/backup-runs/{started['id']}/verify")
        verified = self.wait(
            lambda: self.request("GET", f"/operations/backup-runs/{started['id']}"),
            lambda item: item.get("verification_status") == "failed"
            or (
                item.get("verification_status") == "succeeded"
                and bool(item.get("verified_at"))
            ),
            timeout=180,
            label="downloaded remote restore drill",
        )
        assert verified["verification_status"] == "succeeded", verified.get(
            "verification_error"
        )
        self.passed(
            "known remote archive is downloaded, hashed and independently restore-checked"
        )

        self.request(
            "POST",
            f"/operations/backup-runs/{started['id']}/restore",
            expected=400,
            json={"passphrase": "incorrect-synthetic-passphrase"},
        )
        self.workspace_healthy()
        # A separate, deliberately corrupted copy exercises archive authentication.
        # The original immutable backup and its ownership metadata remain intact.
        damaged = self.output.parent / f"v4-{self.suffix}.tampered.obbackup"
        with archive.open("rb") as original, damaged.open("wb") as target:
            while chunk := original.read(1024 * 1024):
                target.write(chunk)
        with damaged.open("r+b") as target:
            target.seek(-1, 2)
            original_byte = target.read(1)
            target.seek(-1, 2)
            target.write(bytes([original_byte[0] ^ 1]))
        bad_key = "negative-tests/" + self.suffix + ".obbackup"
        with damaged.open("rb") as stream:
            self.request(
                "POST",
                f"/connections/{identifier}/objects",
                data={"bucket": target_bucket, "key": bad_key},
                files={"file": (damaged.name, stream, "application/octet-stream")},
            )
        self.request(
            "POST",
            "/operations/remote-backups/restore",
            expected=400,
            json={
                "project_id": self.project,
                "connection_id": identifier,
                "bucket": target_bucket,
                "key": bad_key,
                "passphrase": phrase,
            },
        )
        self.workspace_healthy()
        assert (
            self.request("GET", f"/operations/backup-runs/{started['id']}")["status"]
            == "succeeded"
        )
        self.passed(
            "wrong passphrase and tampered archive are rejected while the primary QA workspace stays available"
        )

    def close(self):
        self.checkpoint()
        self.client.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base-url", default=os.getenv("OPENBRICKS_SMOKE_URL", "http://127.0.0.1:3001")
    )
    parser.add_argument(
        "--receipt",
        default=os.getenv(
            "OPENBRICKS_SMOKE_RECEIPT",
            str(
                Path(__file__).resolve().parents[1]
                / ".test-data"
                / "v4-runtime-smoke.json"
            ),
        ),
    )
    parser.add_argument(
        "--section", choices=("all", "runtime", "duckdb", "backup"), default="all"
    )
    args = parser.parse_args()
    proof = Proof(args)
    try:
        proof.login()
        if args.section in ("all", "runtime"):
            proof.runtime()
        if args.section in ("all", "duckdb"):
            proof.duckdb()
        if args.section in ("all", "backup"):
            proof.backups()
        proof.state["passed"] = True
        print(
            f"PASS: proof complete; synthetic recovery receipt saved to {proof.output}",
            flush=True,
        )
    finally:
        proof.close()


if __name__ == "__main__":
    main()
