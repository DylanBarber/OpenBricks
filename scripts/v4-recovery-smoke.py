"""Disposable source-to-fresh-host recovery proof through the two UI proxies.

1. --phase prepare-restore: register synthetic catalog metadata, take an encrypted
   catalog-inclusive snapshot on 127.0.0.1:3001, and restore 127.0.0.1:3002.
2. Restart the fresh management service externally; this script never runs Docker.
3. --phase recover: sign in with the restored source account, recover selected S3
   attachments into separate storage, recover catalog metadata, and repeat safely.

Required: OPENBRICKS_RECOVERY_BOOTSTRAP_PASSWORD for the initial fresh account.
Source password: OPENBRICKS_RECOVERY_SOURCE_PASSWORD, defaulting to the synthetic
QA-only account. All output artifacts stay under the ignored .test-data directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "http://127.0.0.1:3001"
FRESH_URL = "http://127.0.0.1:3002"
SOURCE_STORAGE = "http://seaweedfs:8333"
RECOVERY_STORAGE = "http://recovery-storage:8333"
SOURCE_PASSWORD = "OpenBricks_v4_QA_Only_2026!"
STORAGE_ACCESS = "openbricks-v4-qa"
STORAGE_SECRET = "openbricks-v4-qa-secret-2026"


class API:
    def __init__(self, url, port):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != port
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Recovery proof accepts only the fixed disposable 127.0.0.1 QA origins"
            )
        self.client = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=httpx.Timeout(180, connect=8),
            follow_redirects=False,
        )

    def request(self, method, path, *, expected=None, **kwargs):
        response = self.client.request(method, "/api" + path, **kwargs)
        if expected is not None:
            assert (
                response.status_code in expected
            ), f"{method} {path}: {response.status_code}: {response.text[:1000]}"
        else:
            assert (
                response.is_success
            ), f"{method} {path}: {response.status_code}: {response.text[:1000]}"
        return response.json() if response.content else None

    def login(self, password, username=None):
        self.request(
            "POST",
            "/auth/login",
            json={
                "username": username
                or os.getenv("OPENBRICKS_RECOVERY_USERNAME", "admin"),
                "password": password,
            },
        )
        assert self.request("GET", "/auth/me")["role"] == "admin"

    def download(self, connection, bucket, key, path):
        digest = hashlib.sha256()
        with self.client.stream(
            "GET",
            f"/api/connections/{connection}/download",
            params={"bucket": bucket, "key": key},
        ) as response:
            assert (
                response.is_success
            ), f"S3 download failed: HTTP {response.status_code}"
            with path.open("wb") as target:
                for chunk in response.iter_bytes(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
        return digest.hexdigest()

    def object_bytes(self, connection, bucket, key):
        response = self.client.get(
            f"/api/connections/{connection}/download",
            params={"bucket": bucket, "key": key},
        )
        assert (
            response.is_success
        ), f"Restored object download failed: HTTP {response.status_code}"
        return response.content

    def close(self):
        self.client.close()


class RecoveryProof:
    def __init__(self, args):
        self.path = Path(args.receipt).resolve()
        if not self.path.is_relative_to((ROOT / ".test-data").resolve()):
            raise ValueError(
                "Recovery receipts must remain inside the ignored .test-data directory"
            )
        self.state = json.loads(self.path.read_text(encoding="utf-8"))
        assert self.state["base_url"] == SOURCE_URL and self.state.get(
            "backup"
        ), "Use the source QA backup smoke receipt"
        self.source, self.fresh = API(args.source_url, 3001), API(args.fresh_url, 3002)
        self.recovery = self.state.setdefault("recovery_proof", {"checks": []})
        self.backup = self.state["backup"]
        self.project = self.state["project_id"]

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def passed(self, name):
        if name not in self.recovery["checks"]:
            self.recovery["checks"].append(name)
        self.save()
        print("PASS: " + name, flush=True)

    @staticmethod
    def wait(get, predicate, label, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = get()
            if predicate(value):
                return value
            time.sleep(0.6)
        raise AssertionError("Timed out waiting for " + label)

    @staticmethod
    def source_password():
        return os.getenv("OPENBRICKS_RECOVERY_SOURCE_PASSWORD", SOURCE_PASSWORD)

    def connection(self, api, project, endpoint, name):
        return api.request(
            "POST",
            "/connections",
            json={
                "name": name,
                "type": "s3",
                "project_ids": [project],
                "config": {
                    "endpoint_url": endpoint,
                    "region": "us-east-1",
                    "addressing_style": "path",
                    "verify_tls": True,
                },
                "secrets": {
                    "access_key": os.getenv(
                        "OPENBRICKS_RECOVERY_S3_ACCESS_KEY", STORAGE_ACCESS
                    ),
                    "secret_key": os.getenv(
                        "OPENBRICKS_RECOVERY_S3_SECRET_KEY", STORAGE_SECRET
                    ),
                },
            },
        )["id"]

    def notebook_run(self, api, name, source):
        notebook = api.request(
            "POST",
            "/notebooks",
            json={
                "name": name + " " + self.state["suffix"],
                "project_id": self.project,
                "cluster_id": "local",
                "cells": [{"id": "main", "language": "python", "source": source}],
            },
        )
        started = api.request(
            "POST",
            f"/notebooks/{notebook['id']}/run",
            json={"resource_options": {"mode": "job", "memory_mb": 1536}},
        )
        result = self.wait(
            lambda: api.request("GET", "/runs/" + started["id"]),
            lambda value: value["status"]
            in ("succeeded", "failed", "cancelled", "timed_out"),
            name,
            timeout=240,
        )
        assert result["status"] == "succeeded", "Recovery notebook failed: " + str(
            result.get("error_class") or result["status"]
        )
        return notebook, result

    def prepare_local_execution(self):
        # These paths belong only to this proof. The fresh host reads them;
        # it never reruns a source notebook that could repeat prior writes.
        value = self.recovery.setdefault(
            "local_execution",
            {
                "bucket": "v4-recovery-local-" + self.state["suffix"],
                "text": "Exact restored local bytes — " + self.state["suffix"] + "\n",
                "rows": [[42, "restored Delta 雪"], [43, "second exact row"]],
                "schema": "struct<id:bigint,note:string>",
            },
        )
        value["text_uri"] = "blob://" + value["bucket"] + "/selected/message.txt"
        value["delta_uri"] = "blob://" + value["bucket"] + "/delta/rows"
        value["sha256"] = hashlib.sha256(value["text"].encode()).hexdigest()
        self.source.request(
            "POST",
            "/buckets",
            expected={201, 409},
            json={"name": value["bucket"], "project_id": self.project},
        )
        code = (
            f"blob.write_text({value['text_uri']!r}, {value['text']!r})\n"
            f"rows = {value['rows']!r}\n"
            "frame = spark.createDataFrame(rows, 'id long, note string')\n"
            f"frame.write.format('delta').mode('overwrite').save(blob.path({value['delta_uri']!r}))\n"
            f"assert blob.read_text({value['text_uri']!r}) == {value['text']!r}\n"
            "print('SOURCE_LOCAL_RECOVERY_FIXTURE_COMMITTED')"
        )
        notebook, result = self.notebook_run(
            self.source, "Source recovery data fixture", code
        )
        value.update(
            source_notebook_id=notebook["id"],
            source_run_id=result["id"],
            source_code_sha256=hashlib.sha256(code.encode()).hexdigest(),
        )
        self.passed(
            "source local object bytes and a real Delta table commit before the workspace snapshot"
        )

    def verify_local_execution(self):
        value = self.recovery.get("local_execution")
        assert (
            value
        ), "Prepare a new recovery archive with the local execution fixture first"
        restored = self.fresh.request(
            "GET", "/notebooks/" + value["source_notebook_id"]
        )
        assert restored.get("project_id") == self.project
        assert (
            hashlib.sha256(restored["cells"][0]["source"].encode()).hexdigest()
            == value["source_code_sha256"]
        ), "Restored notebook source differs from the captured source"
        code = (
            "import hashlib\n"
            f"assert hashlib.sha256(blob.read_bytes({value['text_uri']!r})).hexdigest() == {value['sha256']!r}\n"
            f"frame = spark.read.format('delta').load(blob.path({value['delta_uri']!r}))\n"
            f"assert frame.schema.simpleString() == {value['schema']!r}\n"
            f"assert [list(row) for row in frame.orderBy('id').collect()] == {value['rows']!r}\n"
            "assert spark.sql('SELECT 6 * 7 AS answer').first().answer == 42\n"
            "print('FRESH_HOST_RESTORED_BLOB_DELTA_SPARK_OK')"
        )
        notebook, result = self.notebook_run(
            self.fresh, "Fresh host restored data execution", code
        )
        assert (
            result.get("cluster_id") == "local"
            and result["resource_options"]["mode"] == "job"
        )
        assert any(
            "FRESH_HOST_RESTORED_BLOB_DELTA_SPARK_OK" in cell.get("stdout", "")
            for cell in result.get("outputs", [])
        ), "Fresh notebook did not confirm restored data reads"
        value["fresh_execution"] = {
            "notebook_id": notebook["id"],
            "run_id": result["id"],
            "session_id": result.get("session_id"),
            "passed": True,
        }
        self.passed(
            "a new fresh-host job reads exact restored blob bytes and Delta rows/schema and executes real Spark SQL"
        )

    def prepare(self):
        self.source.login(self.source_password())
        assert self.source.request(
            "GET", "/catalog/status", params={"project_id": self.project}
        )["available"]
        if "catalog" not in self.recovery:
            self.recovery["catalog"] = {
                "name": "v4_recovery_" + self.state["suffix"] + "_" + uuid4().hex[:6],
                "schema": "selected_data",
                "table": "recovery_rows",
            }
            self.save()
        catalog = self.recovery["catalog"]
        full_name = ".".join((catalog["name"], catalog["schema"], catalog["table"]))
        csv_key, csv_data = (
            "selected/catalog_rows.csv",
            b"id,note\n42,restored metadata and data\n",
        )
        self.source.request(
            "POST",
            f"/connections/{self.backup['connection_id']}/objects",
            data={"bucket": self.backup["source_bucket"], "key": csv_key},
            files={"file": ("catalog_rows.csv", csv_data, "text/csv")},
        )
        self.source.request(
            "POST",
            "/catalog/catalogs",
            expected={201, 409},
            json={
                "project_id": self.project,
                "name": catalog["name"],
                "comment": "Synthetic fresh-host recovery proof",
            },
        )
        self.source.request(
            "POST",
            "/catalog/schemas",
            expected={201, 409},
            json={
                "project_id": self.project,
                "catalog_name": catalog["name"],
                "name": catalog["schema"],
                "comment": "Selected recovery attachments",
            },
        )
        properties = {
            "openbricks.qa": "recovery-proof",
            "openbricks.proof": self.state["suffix"],
        }
        table_body = {
            "project_id": self.project,
            "catalog_name": catalog["name"],
            "schema_name": catalog["schema"],
            "name": catalog["table"],
            "storage_location": f"s3://{self.backup['source_bucket']}/{csv_key}",
            "data_source_format": "CSV",
            "comment": "Synthetic catalog metadata must survive the recovery archive",
            "properties": properties,
            "columns": [
                {
                    "name": "id",
                    "type_name": "LONG",
                    "type_text": "bigint",
                    "position": 0,
                    "nullable": False,
                },
                {
                    "name": "note",
                    "type_name": "STRING",
                    "type_text": "string",
                    "position": 1,
                    "nullable": True,
                },
            ],
        }
        self.source.request(
            "POST", "/catalog/tables", expected={201, 409}, json=table_body
        )
        detail = self.source.request(
            "GET", "/catalog/tables/" + full_name, params={"project_id": self.project}
        )
        assert (
            detail.get("properties") == properties
        ), "Source management API must preserve explicitly supplied table properties"
        assert [
            (column["name"], column["nullable"]) for column in detail["columns"]
        ] == [("id", False), ("note", True)]
        catalog.update(
            full_name=full_name,
            expected={
                key: detail.get(key)
                for key in (
                    "name",
                    "catalog_name",
                    "schema_name",
                    "columns",
                    "properties",
                    "storage_location",
                    "data_source_format",
                    "comment",
                    "table_type",
                )
            },
            csv_key=csv_key,
            csv_text=csv_data.decode(),
            csv_sha256=hashlib.sha256(csv_data).hexdigest(),
        )
        self.passed(
            "source external table has explicit properties, typed ordered columns and selected CSV data"
        )
        self.prepare_local_execution()

        policies = self.source.request(
            "GET", "/operations/backup-policies", params={"project_id": self.project}
        )
        policy = next(
            item for item in policies if item["id"] == self.backup["policy_id"]
        )
        keys = (
            "name",
            "project_id",
            "connection_id",
            "bucket",
            "prefix",
            "cron",
            "timezone",
            "enabled",
            "include_data",
            "storage_sources",
            "include_catalog",
            "verify_restore",
            "notification_ids",
            "keep_remote",
        )
        body = {key: policy[key] for key in keys}
        body.update(
            passphrase="",
            include_catalog=True,
            include_data=True,
            verify_restore=True,
            enabled=False,
        )
        self.source.request(
            "PUT", f"/operations/backup-policies/{policy['id']}", json=body
        )
        run = self.source.request(
            "POST", f"/operations/backup-policies/{policy['id']}/run"
        )
        result = self.wait(
            lambda: self.source.request("GET", f"/operations/backup-runs/{run['id']}"),
            lambda value: value["status"] in ("succeeded", "failed"),
            "catalog-inclusive encrypted backup",
        )
        assert result["status"] == "succeeded", result.get("error")
        assert (
            result["verification_status"] == "succeeded"
            and result["restore_drill"]["passed"]
        )
        receipt = result["remote"]
        archive = self.path.parent / (
            "v4-" + self.state["suffix"] + "-catalog.obbackup"
        )
        digest = self.source.download(
            self.backup["connection_id"], receipt["bucket"], receipt["key"], archive
        )
        assert digest == receipt["sha256"] and archive.stat().st_size == receipt["size"]
        receipt_file = archive.with_suffix(".receipt.json")
        self.source.download(
            self.backup["connection_id"],
            receipt["bucket"],
            receipt["key"] + ".receipt.json",
            receipt_file,
        )
        assert json.loads(receipt_file.read_text()) == receipt
        self.backup.update(
            run_id=run["id"],
            receipt=receipt,
            archive=str(archive),
            receipt_file=str(receipt_file),
        )
        self.recovery["prepared"] = True
        self.passed(
            "catalog-inclusive encrypted backup uploads, verifies, and passes a real restore drill"
        )

    def restore(self):
        assert self.recovery.get(
            "prepared"
        ), "Run --phase prepare before restoring the fresh workspace"
        assert not self.recovery.get(
            "restore_completed"
        ), "Fresh restore is already recorded; restart the fresh service and run --phase recover"
        password = os.getenv("OPENBRICKS_RECOVERY_BOOTSTRAP_PASSWORD")
        if not password:
            raise ValueError(
                "Set OPENBRICKS_RECOVERY_BOOTSTRAP_PASSWORD for the fresh disposable account"
            )
        self.fresh.login(
            password, os.getenv("OPENBRICKS_RECOVERY_BOOTSTRAP_USERNAME", "admin")
        )
        projects = self.fresh.request("GET", "/projects")
        assert all(
            item["id"] != self.project for item in projects
        ), "Fresh destination already contains the source project"
        connection = self.connection(
            self.fresh,
            "default",
            SOURCE_STORAGE,
            "QA bootstrap archive access " + self.state["suffix"],
        )
        self.recovery["bootstrap_connection_id"] = connection
        self.save()
        receipt = self.backup["receipt"]
        result = self.fresh.request(
            "POST",
            "/operations/remote-backups/restore",
            json={
                "project_id": "default",
                "connection_id": connection,
                "bucket": receipt["bucket"],
                "key": receipt["key"],
                "passphrase": self.backup["passphrase"],
            },
        )
        assert result["restart_required"]
        self.recovery["restore_completed"] = True
        self.passed(
            "fresh host restores the encrypted archive directly from remote storage and requires restart"
        )

    def recover(self):
        assert self.recovery.get(
            "restore_completed"
        ), "First restore the fresh host and restart its management service"
        self.fresh.login(self.source_password())
        assert any(
            item["id"] == self.project
            for item in self.fresh.request("GET", "/projects")
        )
        assert self.fresh.request(
            "GET", "/catalog/status", params={"project_id": self.project}
        )["available"]
        catalog = self.recovery["catalog"]
        if not self.recovery.get("attachments_applied"):
            response = self.fresh.client.get(
                "/api/catalog/tables/" + catalog["full_name"],
                params={"project_id": self.project},
            )
            assert (
                response.status_code == 404
            ), "Recovery catalog must start without the source's synthetic table"
        self.passed(
            "restarted fresh host accepts the restored source account and retained project metadata"
        )
        connection = self.recovery.get("destination_connection_id")
        if not connection:
            connection = self.connection(
                self.fresh,
                self.project,
                RECOVERY_STORAGE,
                "QA empty recovery destination " + self.state["suffix"],
            )
            self.recovery["destination_connection_id"] = connection
            self.save()
        buckets = self.fresh.request("GET", f"/connections/{connection}/buckets")[
            "items"
        ]
        if not any(item["name"] == self.backup["source_bucket"] for item in buckets):
            self.fresh.request(
                "POST",
                f"/connections/{connection}/buckets",
                json={"name": self.backup["source_bucket"]},
            )
        if not self.recovery.get("attachments_applied"):
            empty = self.fresh.request(
                "GET",
                f"/connections/{connection}/objects",
                params={"bucket": self.backup["source_bucket"]},
            )
            assert (
                empty["items"] == [] and empty["prefixes"] == []
            ), "Recovery destination bucket must start empty"
        body = {
            "project_id": self.project,
            "mappings": {self.backup["connection_id"]: connection},
            "include_catalog": True,
        }
        plan = self.fresh.request("POST", "/operations/recovery/plan", json=body)
        assert (
            plan["objects"] == 2
            and plan["catalog_tables"] >= 1
            and not plan["overwrites"]
        )
        result = self.fresh.request(
            "POST",
            "/operations/recovery/apply",
            json={**body, "plan_hash": plan["plan_hash"]},
        )
        if not self.recovery.get("attachments_applied"):
            assert (
                result["objects_restored"] == 2
                and result["catalog_objects_created"] >= 3
            )
        assert not result["overwrites"]
        self.recovery.update(attachments_applied=True, plan=plan, first_apply=result)
        self.save()
        actual = self.fresh.object_bytes(
            connection, self.backup["source_bucket"], self.backup["source_key"]
        )
        assert actual.decode() == self.backup["source_expected_text"]
        csv = self.fresh.object_bytes(
            connection, self.backup["source_bucket"], catalog["csv_key"]
        )
        assert (
            csv.decode() == catalog["csv_text"]
            and hashlib.sha256(csv).hexdigest() == catalog["csv_sha256"]
        )
        excluded = self.fresh.request(
            "GET",
            f"/connections/{connection}/objects",
            params={
                "bucket": self.backup["source_bucket"],
                "prefix": self.backup["excluded_key"],
            },
        )
        assert excluded["items"] == [] and excluded["prefixes"] == []
        restored = self.fresh.request(
            "GET",
            "/catalog/tables/" + catalog["full_name"],
            params={"project_id": self.project},
        )
        assert {key: restored.get(key) for key in catalog["expected"]} == catalog[
            "expected"
        ], "Recovered catalog columns, properties or table metadata differ"
        self.passed(
            "selected objects recover with exact content and hashes; unselected data stays absent"
        )
        self.passed(
            "new catalog service recovers exact external table columns, properties and storage metadata"
        )

        repeated = self.fresh.request(
            "POST",
            "/operations/recovery/apply",
            json={**body, "plan_hash": plan["plan_hash"]},
        )
        assert (
            repeated["objects_restored"] == 0
            and repeated["existing_identical_skipped"] == 2
        )
        assert (
            repeated["catalog_objects_created"] == 0
            and repeated["catalog_existing_skipped"] >= 3
        )
        assert not repeated["overwrites"]
        after = self.fresh.request(
            "GET",
            "/catalog/tables/" + catalog["full_name"],
            params={"project_id": self.project},
        )
        assert {key: after.get(key) for key in catalog["expected"]} == catalog[
            "expected"
        ]
        assert (
            self.fresh.object_bytes(
                connection, self.backup["source_bucket"], catalog["csv_key"]
            )
            == csv
        )
        self.recovery.update(repeated_apply=repeated)
        self.passed(
            "repeated external recovery safely skips identical objects and existing catalog metadata"
        )
        self.verify_local_execution()
        self.recovery["passed"] = True
        self.save()

    def close(self):
        self.save()
        self.source.close()
        self.fresh.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--phase",
        required=True,
        choices=("prepare", "restore", "prepare-restore", "recover"),
    )
    parser.add_argument(
        "--receipt", default=str(ROOT / ".test-data" / "v4-backup-smoke.json")
    )
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument("--fresh-url", default=FRESH_URL)
    args = parser.parse_args()
    proof = RecoveryProof(args)
    try:
        if args.phase in ("prepare", "prepare-restore"):
            proof.prepare()
        if args.phase in ("restore", "prepare-restore"):
            proof.restore()
        if args.phase == "recover":
            proof.recover()
    finally:
        proof.close()


if __name__ == "__main__":
    main()
