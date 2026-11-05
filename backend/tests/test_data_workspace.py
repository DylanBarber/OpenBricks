"""No SQL Server endpoint required: native JDBC contract and project/API safety."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from openbricks.blob import LocalBlobStore
from openbricks.connectors import SQLServerConnection, connections_for_runtime
from openbricks.credentials import SecretBox
from openbricks.data_workspace import (
    ComputeDataWorkspace,
    finalize_data_run,
    persist_data_event,
    register_data_routes,
)
from openbricks.delta_tools import DeltaTools, data_path, table_identifier
from openbricks.ingestion import sql_literal
from openbricks.permissions import Access
from openbricks.store import Store


class FakeRuntime:
    def __init__(self, store):
        self.store, self.calls = store, []

    def submit(self, notebook, **options):
        self.calls.append((notebook, options))
        return self.store.put(
            "runs",
            {
                "id": options["run_id"],
                "project_id": notebook["project_id"],
                "data_execution_id": notebook["data_execution_id"],
                "status": "queued",
                "created_at": "2026-01-01",
                "outputs": [],
            },
        )


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path)
    access = Access(store)
    access.seed()
    for project in ("one", "two"):
        store.put(
            "projects",
            {
                "id": project,
                "name": project,
                "cluster_ids": ["local"],
                "connection_ids": [],
            },
        )
    for user, role in (("writer", "editor"), ("viewer", "viewer")):
        store.put(
            "project_members",
            {"id": "one:" + user, "project_id": "one", "user_id": user, "role": role},
        )
    app = FastAPI()
    app.state.store, app.state.runtime = store, FakeRuntime(store)

    def authenticated(x_user: str = Header(default="writer")):
        return {"id": x_user, "role": "admin" if x_user == "admin" else "member"}

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(exc)}, status_code=400)

    register_data_routes(app, authenticated, access)
    with TestClient(app) as client:
        yield client, store, app.state.runtime


def test_query_permissions_dispatch_and_bounded_history(workspace):
    client, store, runtime = workspace
    for index in range(81):
        store.put(
            "data_queries",
            {
                "id": str(index),
                "project_id": "one",
                "name": f"query {index}",
                "sql": "SELECT 1",
                "created_at": f"2026-{index:03}",
            },
        )
    page = client.get("/api/data/queries?project_id=one&offset=25&limit=25").json()
    assert page["total"] == 81 and len(page["items"]) == 25
    assert client.get("/api/data/queries?project_id=two").status_code == 403
    body = {
        "project_id": "one",
        "sql": "SELECT 'quoted \\' payload'",
        "engine": "spark",
    }
    assert (
        client.post(
            "/api/data/execute", json=body, headers={"x-user": "viewer"}
        ).status_code
        == 403
    )
    response = client.post("/api/data/execute", json=body)
    assert response.status_code == 202, response.text
    execution = response.json()
    notebook, options = runtime.calls[-1]
    assert options["connections"] == [] and notebook["project_id"] == "one"
    compile(notebook["cells"][0]["source"], "<generated>", "exec")
    run = store.get("runs", execution["run_id"])
    run.update(status="succeeded", data_result={"columns": ["value"], "rows": [[42]]})
    store.put("runs", run)
    detail = client.get("/api/data/executions/" + execution["id"]).json()
    assert detail["status"] == "succeeded" and detail["result"]["rows"] == [[42]]
    assert (
        client.get(
            "/api/data/executions/" + execution["id"], headers={"x-user": "outsider"}
        ).status_code
        == 403
    )


def test_mutating_delta_requires_manage_and_dry_run_is_default(workspace):
    client, _, runtime = workspace
    body = {
        "project_id": "one",
        "operation": "delta_vacuum",
        "payload": {"path": "blob://demo/events"},
    }
    assert client.post("/api/data/execute", json=body).status_code == 202
    body["payload"]["dry_run"] = False
    assert client.post("/api/data/execute", json=body).status_code == 403
    assert (
        client.post(
            "/api/data/execute", json=body, headers={"x-user": "admin"}
        ).status_code
        == 202
    )


def test_saved_query_can_run_unsaved_editor_changes(workspace):
    client, store, runtime = workspace
    saved = client.post(
        "/api/data/queries",
        json={"project_id": "one", "name": "saved", "sql": "SELECT 1"},
    ).json()
    response = client.post(
        "/api/data/execute",
        json={"project_id": "one", "query_id": saved["id"], "sql": "SELECT 2"},
    )
    assert response.status_code == 202
    assert "SELECT 2" in runtime.calls[-1][0]["cells"][0]["source"]
    assert store.get("data_queries", saved["id"])["sql"] == "SELECT 1"


def test_ingestion_overlap_and_admin_dispatch_assignment(workspace):
    client, store, runtime = workspace
    secret = SecretBox(store.directory).encrypt(
        json.dumps({"password": "synthetic-db-password"})
    )
    for name, projects in (("allowed", ["one"]), ("unassigned", ["two"])):
        store.put(
            "connections",
            {
                "id": name,
                "name": name,
                "type": "sqlserver",
                "config": {},
                "project_ids": projects,
                "_secrets": secret,
            },
        )
    forbidden = {
        "project_id": "one",
        "engine": "sqlserver",
        "connection_id": "unassigned",
        "sql": "SELECT 1",
    }
    assert (
        client.post(
            "/api/data/execute", json=forbidden, headers={"x-user": "admin"}
        ).status_code
        == 403
    )
    body = {
        "project_id": "one",
        "name": "orders",
        "connection_id": "allowed",
        "source": {"schema": "dbo", "table": "Orders"},
        "sink": {"path": "blob://silver/orders"},
        "keys": ["id"],
        "watermark": {
            "column": "updated_at",
            "type": "timestamp",
            "initial": "2026-01-01T00:00:00",
        },
    }
    response = client.post("/api/data/ingestions", json=body)
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    started = client.post(f"/api/data/ingestions/{id}/run", json={})
    assert started.status_code == 202, started.text
    assert runtime.calls[-1][1]["connections"][0]["id"] == "allowed"
    assert client.post(f"/api/data/ingestions/{id}/run", json={}).status_code == 409
    assert client.put(f"/api/data/ingestions/{id}", json=body).status_code == 409
    assert client.delete(f"/api/data/ingestions/{id}").status_code == 409


def test_explicit_connection_selection_and_project_assignment(tmp_path):
    store = Store(tmp_path)
    secret = SecretBox(tmp_path).encrypt(
        json.dumps({"password": "synthetic-test-password"})
    )
    for id, projects in (("a", ["one"]), ("b", ["two"])):
        store.put(
            "connections",
            {
                "id": id,
                "name": id,
                "type": "sqlserver",
                "config": {},
                "project_ids": projects,
                "_secrets": secret,
            },
        )
    store.put("projects", {"id": "one", "connection_ids": ["b"]})
    assert connections_for_runtime(store, "one", []) == []
    assert {
        item["id"] for item in connections_for_runtime(store, "one", ["a", "b"])
    } == {"a", "b"}
    assert connections_for_runtime(store, "three", ["a", "b"]) == []


def test_native_jdbc_pushes_partition_fetch_and_predicates_without_python_rows():
    class Reader:
        def jdbc(self, **options):
            self.options = options
            return "distributed-frame"

    class Spark:
        read = Reader()

        def createDataFrame(self, *args):
            raise AssertionError("Native ingestion must not collect Python rows")

    spark = Spark()
    connection = SQLServerConnection(
        {"server": "db.example", "database": "Warehouse", "username": "reader"},
        {"password": "synthetic-jdbc-secret"},
    )
    assert (
        connection.read_spark(
            spark,
            "dbo.orders",
            partition_column="id",
            lower_bound=0,
            upper_bound=100000000,
            num_partitions=8,
            fetch_size=5000,
        )
        == "distributed-frame"
    )
    options = spark.read.options
    assert (
        options["numPartitions"] == 8 and options["properties"]["fetchsize"] == "5000"
    )
    assert (
        "synthetic-jdbc-secret" not in options["url"]
        and options["properties"]["password"] == "synthetic-jdbc-secret"
    )
    connection.read_spark(
        spark, query="SELECT id FROM dbo.orders", predicates=["id < 100", "id >= 100"]
    )
    assert spark.read.options["predicates"] == ["id < 100", "id >= 100"]
    with pytest.raises(ValueError, match="Partition reads"):
        connection.read_spark(spark, "dbo.orders", partition_column="id")
    with pytest.raises(ValueError, match="Choose predicates"):
        connection.read_spark(
            spark,
            "dbo.orders",
            partition_column="id",
            lower_bound=0,
            upper_bound=10,
            num_partitions=2,
            predicates=["1=1"],
        )


def test_delta_commands_are_quoted_and_retain_safe_vacuum(tmp_path):
    class Spark:
        def sql(self, sql):
            self.last = sql
            return sql

    spark = Spark()
    helper = DeltaTools(lambda: spark, LocalBlobStore(tmp_path))
    assert "DRY RUN" in helper.vacuum(path="blob://demo/orders")
    assert helper.restore(table="spark_catalog.demo.orders", version=2).endswith(
        "TO VERSION AS OF 2"
    )
    with pytest.raises(ValueError, match="168"):
        helper.vacuum(table="demo.orders", retention_hours=1, dry_run=False)
    with pytest.raises(ValueError):
        helper.restore(table="demo.orders; DROP TABLE x", version=2)
    with pytest.raises(ValueError):
        helper.restore(table="demo.orders", version="1; DROP TABLE x")
    with pytest.raises(ValueError):
        data_path("file:///etc/passwd", LocalBlobStore(tmp_path))


def test_event_replay_and_watermark_only_after_entire_success(tmp_path):
    store = Store(tmp_path)
    store.put(
        "data_ingestions",
        {
            "id": "ingest",
            "project_id": "one",
            "watermark_value": "10",
            "watermark": {"column": "id"},
        },
    )
    store.put(
        "data_executions",
        {
            "id": "execution",
            "project_id": "one",
            "run_id": "run",
            "ingestion_id": "ingest",
        },
    )
    run = {
        "id": "run",
        "project_id": "one",
        "data_execution_id": "execution",
        "status": "running",
    }
    lineage = {
        "type": "lineage",
        "event_id": "event",
        "run_id": "run",
        "dataset": "demo.orders",
        "action": "write",
        "details": {},
    }
    # Mirror remote data_events before replay, as the control plane does.
    run["data_events"] = [lineage]
    persist_data_event(store, run, lineage)
    persist_data_event(store, run, lineage)
    assert store.count("data_lineage") == 1
    persist_data_event(
        store,
        run,
        {
            "type": "data_result",
            "event_id": "result",
            "result": {
                "ingestion_id": "ingest",
                "previous_watermark": "10",
                "watermark": "20",
            },
        },
    )
    finalize_data_run(store, run)
    assert store.get("data_ingestions", "ingest")["watermark_value"] == "10"
    run["status"] = "failed"  # A later cell/quality check fails after sink write.
    finalize_data_run(store, run)
    assert store.get("data_ingestions", "ingest")["watermark_value"] == "10"
    run["status"] = "succeeded"
    finalize_data_run(store, run)
    finalize_data_run(store, run)
    assert store.get("data_ingestions", "ingest")["watermark_value"] == "20"
    forged = {
        **run,
        "id": "other-run",
        "data_execution_id": None,
        "data_result": {
            "ingestion_id": "ingest",
            "previous_watermark": "20",
            "watermark": "999",
        },
    }
    finalize_data_run(store, forged)
    assert store.get("data_ingestions", "ingest")["watermark_value"] == "20"


def test_watermark_literal_escapes_and_rejects_nonfinite():
    assert sql_literal("2026-01-01T10:00:00", "timestamp") == "N'2026-01-01T10:00:00'"
    assert sql_literal("O'Brien", "string") == "N'O''Brien'"
    with pytest.raises(ValueError):
        sql_literal("NaN", "number")


@pytest.fixture(scope="module")
def packaged_spark():
    if os.getenv("OPENBRICKS_TEST_SPARK") != "1":
        pytest.skip("Enable in packaged Java17/Spark compute environment")
    from pyspark.sql import SparkSession
    from openbricks.spark_catalog import configure_catalog

    builder = (
        SparkSession.builder.master("local[1]")
        .appName("data-workspace-tests")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
    )
    spark = configure_catalog(builder).getOrCreate()
    try:
        yield spark
    finally:
        spark.stop()


def test_real_spark_quality_rejects_bad_pipeline_and_reports_all_checks(packaged_spark):
    from openbricks.data_quality import check_data, DataQualityError

    spark = packaged_spark
    frame = spark.createDataFrame(
        [
            (1, 5, datetime.now(timezone.utc)),
            (1, -1, datetime.now(timezone.utc)),
            (None, 20, datetime.now(timezone.utc)),
        ],
        ["id", "value", "updated_at"],
    )
    reports = []
    checks = [
        {"type": "not_null", "column": "id"},
        {"type": "unique", "column": "id"},
        {"type": "range", "column": "value", "min": 0, "max": 10},
        {"type": "row_count", "min": 4},
        {"type": "freshness", "column": "updated_at", "max_age_seconds": 60},
    ]
    with pytest.raises(DataQualityError) as error:
        check_data(frame, checks, emit=reports.append)
    report = error.value.report
    assert reports == [report] and report["row_count"] == 3
    assert [item["failed_rows"] for item in report["checks"]] == [1, 1, 2, 1, 0]
    assert check_data(frame, [{"type": "row_count", "min": 3, "max": 3}])["passed"]


def test_real_delta_time_travel_restore_vacuum_and_replayed_incremental_merge(
    packaged_spark, tmp_path
):
    from openbricks.ingestion import IncrementalIngestor
    from openbricks.data_quality import DataQualityError

    spark = packaged_spark
    loader = spark._jvm.java.lang.Thread.currentThread().getContextClassLoader()
    assert loader.loadClass("com.microsoft.sqlserver.jdbc.SQLServerDriver") is not None
    blob = LocalBlobStore(tmp_path / "buckets")

    class Source:
        rows = [(1, 1, "first"), (2, 2, "second")]

        def read_spark(self, session, table, **options):
            self.last_query = table
            return session.createDataFrame(self.rows, ["id", "updated_at", "value"])

    class Registry:
        source = Source()

        def sqlserver(self, name):
            return self.source

    registry = Registry()
    ingestor = IncrementalIngestor(lambda: spark, registry, blob)
    spec = {
        "connection_id": "fixture",
        "source": {"table": "Orders"},
        "sink": {"path": "blob://demo/merge"},
        "keys": ["id"],
        "watermark": {"column": "updated_at", "type": "number"},
        "previous_watermark": None,
    }
    assert ingestor.run(spec) == {"rows": 2, "watermark": 2}
    registry.source.rows = [(2, 3, "updated"), (3, 4, "third")]
    spec["previous_watermark"] = 2
    assert ingestor.run(spec) == {"rows": 2, "watermark": 4}
    assert "[updated_at] > 2" in registry.source.last_query
    # A replay after a later notebook failure must not append duplicates.
    ingestor.run(spec)
    helper = DeltaTools(lambda: spark, blob)
    latest = helper.read(path="blob://demo/merge")
    assert latest.count() == 3
    assert latest.filter("id = 2").first()["value"] == "updated"
    assert helper.read(path="blob://demo/merge", version=0).count() == 2
    assert helper.history(path="blob://demo/merge").count() >= 2
    version_before = helper.history(path="blob://demo/merge", limit=1).first()[
        "version"
    ]
    spec["checks"] = [{"type": "row_count", "min": 999}]
    with pytest.raises(DataQualityError):
        ingestor.run(spec)
    assert (
        helper.history(path="blob://demo/merge", limit=1).first()["version"]
        == version_before
    )
    helper.restore(path="blob://demo/merge", version=0).collect()
    assert helper.read(path="blob://demo/merge").count() == 2
    helper.vacuum(path="blob://demo/merge", dry_run=True).collect()
