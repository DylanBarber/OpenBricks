"""Opt-in real SQL Server → API → real worker → Delta recovery checks.

This complements the external HTTP proof. The sole injected fault appends a
second failing cell at the test runtime's submission boundary, after the real
ingestion cell. No production test hooks or fake database/Spark transports.
"""

from copy import deepcopy
import os
import time

import pytest
from fastapi.testclient import TestClient

from openbricks.main import create_app
from openbricks.synchronization import (
    OP,
    SourceSchemaChanged,
    metadata,
    snapshot_to_parquet,
)
from test_sqlserver_live import database


def wait_execution(client, submitted, expected="succeeded"):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = client.get("/api/data/executions/" + submitted["id"])
        response.raise_for_status()
        result = response.json()
        if result["status"] not in ("queued", "running"):
            assert result["status"] == expected, result.get("error")
            return result
        time.sleep(0.05)
    pytest.fail("Real SQL data execution did not finish")


def saved_ingestion(client, identifier):
    response = client.get("/api/data/ingestions", params={"project_id": "default"})
    response.raise_for_status()
    return next(item for item in response.json()["items"] if item["id"] == identifier)


def test_real_change_tracking_time_preserves_seventh_digit_and_rejects_lossy_keys(
    database, tmp_path
):
    import pyarrow as pa
    import pyarrow.parquet as pq

    database.execute(
        "CREATE TABLE dbo.ExactClockProbe(id int PRIMARY KEY,clock time(7) NULL); "
        "ALTER TABLE dbo.ExactClockProbe ENABLE CHANGE_TRACKING; "
        "INSERT INTO dbo.ExactClockProbe VALUES(1,'12:34:56.1234567'),(2,NULL); "
        "CREATE TABLE dbo.LossyTimestampKeyProbe(id datetime2(7) PRIMARY KEY,value int); "
        "ALTER TABLE dbo.LossyTimestampKeyProbe ENABLE CHANGE_TRACKING; "
        "INSERT INTO dbo.LossyTimestampKeyProbe VALUES('2026-01-01T00:00:00.0000001',1),('2026-01-01T00:00:00.0000002',2)"
    )
    directory = tmp_path / "clock"
    directory.mkdir()
    connection = database.connect()
    try:
        result = snapshot_to_parquet(
            connection,
            {"schema": "dbo", "table": "ExactClockProbe"},
            ["id"],
            None,
            directory,
            1,
        )
    finally:
        connection.close()
    assert result["rows"] == 2
    table = pq.read_table(directory)
    assert table.schema.field("clock").type == pa.string()
    assert sorted(table.to_pylist(), key=lambda row: row["id"]) == [
        {"id": 1, "clock": "12:34:56.1234567", OP: "I"},
        {"id": 2, "clock": None, OP: "I"},
    ]
    unsafe_directory = tmp_path / "unsafe-key"
    unsafe_directory.mkdir()
    connection = database.connect()
    try:
        with pytest.raises(
            ValueError, match="primary keys must have precision at most 6"
        ):
            snapshot_to_parquet(
                connection,
                {"schema": "dbo", "table": "LossyTimestampKeyProbe"},
                ["id"],
                None,
                unsafe_directory,
            )
    finally:
        connection.close()
    assert list(unsafe_directory.iterdir()) == []


def test_real_change_tracking_rejects_text_keys_and_detects_collation_drift_before_staging(
    database, tmp_path
):
    database.execute(
        "CREATE TABLE dbo.CaseKeyProbe(id nvarchar(40) COLLATE Latin1_General_100_CI_AS PRIMARY KEY,value int); "
        "CREATE TABLE dbo.SpaceKeyProbe(id varchar(40) COLLATE Latin1_General_100_BIN2 PRIMARY KEY,value int); "
        "CREATE TABLE dbo.CollationDriftProbe(id int PRIMARY KEY,label nvarchar(40) COLLATE Latin1_General_100_CI_AS NULL); "
        "ALTER TABLE dbo.CaseKeyProbe ENABLE CHANGE_TRACKING; "
        "ALTER TABLE dbo.SpaceKeyProbe ENABLE CHANGE_TRACKING; "
        "ALTER TABLE dbo.CollationDriftProbe ENABLE CHANGE_TRACKING; "
        "INSERT dbo.CaseKeyProbe VALUES(N'Alpha',1); INSERT dbo.SpaceKeyProbe VALUES('Alpha',1); "
        "INSERT dbo.CollationDriftProbe VALUES(1,N'Label 雪')"
    )
    checkpoint = database.read("SELECT CHANGE_TRACKING_CURRENT_VERSION() AS version")[
        0
    ]["version"]
    database.execute(
        "UPDATE dbo.CaseKeyProbe SET id=N'alpha',value=2 WHERE id=N'Alpha'; "
        "UPDATE dbo.SpaceKeyProbe SET id='Alpha ',value=2 WHERE id='Alpha'"
    )
    for table, old_bytes, new_bytes in (
        ("CaseKeyProbe", "41006c00700068006100", "61006c00700068006100"),
        ("SpaceKeyProbe", "416c706861", "416c70686120"),
    ):
        observed = database.read(
            f"SELECT ct.SYS_CHANGE_OPERATION AS operation,CONVERT(varbinary(80),ct.id) AS ct_key,CONVERT(varbinary(80),s.id) AS stored_key FROM CHANGETABLE(CHANGES dbo.[{table}],?) ct LEFT JOIN dbo.[{table}] s ON s.id=ct.id",
            [checkpoint],
        )
        assert observed == [
            {"operation": "U", "ct_key": old_bytes, "stored_key": new_bytes}
        ]
        directory = tmp_path / table
        directory.mkdir()
        connection = database.connect()
        try:
            with pytest.raises(ValueError, match="text primary keys are unsupported"):
                snapshot_to_parquet(
                    connection,
                    {"schema": "dbo", "table": table},
                    ["id"],
                    None,
                    directory,
                )
        finally:
            connection.close()
        assert list(directory.iterdir()) == []

    initial_directory = tmp_path / "collation-initial"
    initial_directory.mkdir()
    connection = database.connect()
    try:
        initial = snapshot_to_parquet(
            connection,
            {"schema": "dbo", "table": "CollationDriftProbe"},
            ["id"],
            None,
            initial_directory,
        )
    finally:
        connection.close()
    assert initial["rows"] == 1
    initial_checkpoint = deepcopy(initial["checkpoint"])
    assert initial["columns"][1]["collation"] == "Latin1_General_100_CI_AS"
    initial_bytes = {
        path.name: path.read_bytes() for path in initial_directory.iterdir()
    }
    database.execute(
        "ALTER TABLE dbo.CollationDriftProbe ALTER COLUMN label nvarchar(40) COLLATE Latin1_General_100_CS_AS NULL"
    )
    connection = database.connect()
    cursor = connection.cursor()
    try:
        changed = metadata(cursor, {"schema": "dbo", "table": "CollationDriftProbe"})
    finally:
        cursor.close()
        connection.close()
    assert changed["identity"] == initial["identity"]
    assert changed["schema_sha256"] != initial["schema_sha256"]
    assert changed["columns"][1]["collation"] == "Latin1_General_100_CS_AS"
    assert [
        {key: value for key, value in column.items() if key != "collation"}
        for column in changed["columns"]
    ] == [
        {key: value for key, value in column.items() if key != "collation"}
        for column in initial["columns"]
    ]
    rejected_directory = tmp_path / "collation-rejected"
    rejected_directory.mkdir()
    connection = database.connect()
    try:
        with pytest.raises(SourceSchemaChanged, match="Source schema changed"):
            snapshot_to_parquet(
                connection,
                {"schema": "dbo", "table": "CollationDriftProbe"},
                ["id"],
                initial_checkpoint,
                rejected_directory,
            )
    finally:
        connection.close()
    assert list(rejected_directory.iterdir()) == []
    assert initial_checkpoint == initial["checkpoint"]
    assert {
        path.name: path.read_bytes() for path in initial_directory.iterdir()
    } == initial_bytes
    assert database.read("SELECT id,label FROM dbo.CollationDriftProbe") == [
        {"id": 1, "label": "Label 雪"}
    ]


def test_real_sql_api_json_insert_preserves_decimal_date_unicode_nulls_and_transactions(
    database, tmp_path, monkeypatch
):
    for key in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
        "OPENBRICKS_MANAGED_CONTAINER",
        "OPENBRICKS_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENBRICKS_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "stable-sql-binding-test-password")
    database.execute(
        "CREATE TABLE dbo.JsonBindingProbe(id int NOT NULL PRIMARY KEY,label nvarchar(200) NOT NULL,amount decimal(20,6) NULL,business_date date NULL)"
    )
    with TestClient(create_app(tmp_path / "binding-workspace")) as client:
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "stable-sql-binding-test-password"},
        ).raise_for_status()
        connection = client.post(
            "/api/connections",
            json={
                "name": "Typed SQL JSON fixture",
                "type": "sqlserver",
                "config": database.config,
                "secrets": database.secrets,
            },
        )
        connection.raise_for_status()
        prefix = "/api/connections/" + connection.json()["id"]
        label = "O'Brien ? 雪 🙂; DROP TABLE dbo.JsonBindingProbe; --"
        inserted = client.post(
            prefix + "/insert",
            json={
                "table": "JsonBindingProbe",
                "rows": [
                    {
                        "id": 1,
                        "label": label,
                        "amount": "12345678901234.123456",
                        "business_date": "2026-01-02",
                    },
                    {"id": 2, "label": "NULLs", "amount": None, "business_date": None},
                ],
            },
        )
        assert inserted.status_code == 200, inserted.text
        assert inserted.json()["row_count"] == 2
        result = client.post(
            prefix + "/query",
            json={
                "sql": "SELECT id,label,amount,business_date FROM dbo.JsonBindingProbe ORDER BY id"
            },
        )
        result.raise_for_status()
        assert result.json()["rows"] == [
            [1, label, "12345678901234.123456", "2026-01-02"],
            [2, "NULLs", None, None],
        ]
        rolled_back_query = client.post(
            prefix + "/query",
            json={
                "sql": "INSERT INTO dbo.JsonBindingProbe VALUES(3,N'query rollback',NULL,NULL); SELECT COUNT(*) FROM dbo.JsonBindingProbe"
            },
        )
        rolled_back_query.raise_for_status()
        assert rolled_back_query.json()["rows"] == [[3]]
        failed_batch = client.post(
            prefix + "/insert",
            json={
                "table": "JsonBindingProbe",
                "rows": [
                    {"id": 4, "label": "must roll back"},
                    {"id": 1, "label": "duplicate"},
                ],
            },
        )
        assert failed_batch.status_code == 400
        count = client.post(
            prefix + "/query", json={"sql": "SELECT COUNT(*) FROM dbo.JsonBindingProbe"}
        )
        count.raise_for_status()
        assert count.json()["rows"] == [[2]]
        temporal = client.post(
            prefix + "/query",
            json={
                "sql": "SELECT CAST('12:34:56.1234567' AS time(7)), "
                "CAST('2024-02-29T12:34:56.1234567-00:30' AS datetimeoffset(7)), "
                "CAST(NULL AS time(7)), CAST(NULL AS datetimeoffset(7))"
            },
        )
        temporal.raise_for_status()
        assert temporal.json()["rows"] == [
            ["12:34:56.1234567", "2024-02-29T12:34:56.1234567-00:30", None, None]
        ]


def test_real_sql_api_checkpoint_waits_for_entire_notebook_and_replays_after_sink_failure(
    database, tmp_path, monkeypatch
):
    for key in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
        "OPENBRICKS_MANAGED_CONTAINER",
        "OPENBRICKS_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "stable-sql-worker-test-password")
    monkeypatch.setenv("OPENBRICKS_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SPARK_MASTER", "local[1]")
    monkeypatch.setenv("SPARK_DRIVER_MEMORY", "512m")
    monkeypatch.setenv("SPARK_SHUFFLE_PARTITIONS", "1")
    monkeypatch.setenv("OPENBRICKS_RUN_TIMEOUT", "150")
    database.execute(
        "CREATE TABLE dbo.AfterSinkProbe(id int NOT NULL PRIMARY KEY,amount decimal(18,2) NOT NULL); "
        "ALTER TABLE dbo.AfterSinkProbe ENABLE CHANGE_TRACKING; "
        "INSERT INTO dbo.AfterSinkProbe VALUES(1,10),(2,20)"
    )
    app = create_app(tmp_path / "workspace")
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "stable-sql-worker-test-password"},
        ).raise_for_status()
        connection = client.post(
            "/api/connections",
            json={
                "name": "Real SQL fixture",
                "type": "sqlserver",
                "config": database.config,
                "secrets": database.secrets,
                "project_ids": ["default"],
            },
        )
        connection.raise_for_status()
        sink = "blob://stable-sql-after-sink/orders"
        definition = client.post(
            "/api/data/ingestions",
            json={
                "name": "After-sink failure proof",
                "project_id": "default",
                "connection_id": connection.json()["id"],
                "source": {"schema": "dbo", "table": "AfterSinkProbe"},
                "sink": {"path": sink},
                "mode": "change_tracking",
                "keys": ["id"],
                "jdbc": {"fetch_size": 1},
            },
        )
        definition.raise_for_status()
        identifier = definition.json()["id"]

        def submit_ingestion():
            response = client.post(f"/api/data/ingestions/{identifier}/run", json={})
            response.raise_for_status()
            return response.json()

        initial = wait_execution(client, submit_ingestion())
        assert initial["result"]["source_rows"] == 2
        original_checkpoint = deepcopy(
            saved_ingestion(client, identifier)["watermark_value"]
        )
        database.execute(
            "UPDATE dbo.AfterSinkProbe SET amount=99 WHERE id=1; "
            "DELETE dbo.AfterSinkProbe WHERE id=2; INSERT INTO dbo.AfterSinkProbe VALUES(3,30)"
        )
        original_submit = app.state.runtime.submit

        def append_failure_after_real_ingestion(notebook, *args, **options):
            captured = deepcopy(notebook)
            assert captured.get("data_execution_id")
            captured["cells"].append(
                {
                    "id": "intentional-after-sink-failure",
                    "language": "python",
                    "source": "raise RuntimeError('expected failure after real sink MERGE')",
                }
            )
            return original_submit(captured, *args, **options)

        with monkeypatch.context() as fault:
            fault.setattr(
                app.state.runtime, "submit", append_failure_after_real_ingestion
            )
            failed = wait_execution(client, submit_ingestion(), expected="failed")
        assert "expected failure after real sink MERGE" in failed["error"]
        assert failed["result"]["watermark"]["version"] > original_checkpoint["version"]
        assert (
            saved_ingestion(client, identifier)["watermark_value"]
            == original_checkpoint
        )

        def inspect_sink():
            response = client.post(
                "/api/data/execute",
                json={
                    "project_id": "default",
                    "engine": "spark",
                    "operation": "delta_preview",
                    "payload": {"path": sink},
                },
            )
            response.raise_for_status()
            result = wait_execution(client, response.json())["result"]
            rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
            return sorted((int(row["id"]), float(row["amount"])) for row in rows)

        assert inspect_sink() == [(1, 99.0), (3, 30.0)]
        recovered = wait_execution(client, submit_ingestion())
        recovered_checkpoint = saved_ingestion(client, identifier)["watermark_value"]
        assert recovered_checkpoint == recovered["result"]["watermark"]
        assert recovered_checkpoint["version"] > original_checkpoint["version"]
        assert inspect_sink() == [(1, 99.0), (3, 30.0)]
        replay = wait_execution(client, submit_ingestion())
        assert replay["result"]["source_rows"] == 0
        assert (
            saved_ingestion(client, identifier)["watermark_value"]
            == recovered_checkpoint
        )
        assert all(
            database.secrets["password"]
            not in client.get("/api/runs/" + execution["run_id"]).text
            for execution in (initial, failed, recovered, replay)
        )
