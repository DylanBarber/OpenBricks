"""Opt-in checks against a disposable, real SQL Server instance.

Set OPENBRICKS_TEST_SQLSERVER=1 and OPENBRICKS_SQLSERVER_PASSWORD. These tests
create and drop only a new uniquely named openbricks_test_* database.
"""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from openbricks.blob import LocalBlobStore
from openbricks.connectors import SQLServerConnection
from openbricks.synchronization import (
    ChangeTrackingIngestor,
    SourceSchemaChanged,
    SourceCheckpointExpired,
)


@pytest.fixture(scope="module")
def database():
    if os.getenv("OPENBRICKS_TEST_SQLSERVER") != "1":
        pytest.skip("Enable only against a disposable SQL Server test instance")
    password = os.getenv("OPENBRICKS_SQLSERVER_PASSWORD")
    if not password:
        pytest.fail("Set the disposable SQL Server password")
    config = {
        "server": os.getenv("OPENBRICKS_SQLSERVER_HOST", "sqlserver"),
        "port": int(os.getenv("OPENBRICKS_SQLSERVER_PORT", "1433")),
        "database": "master",
        "username": os.getenv("OPENBRICKS_SQLSERVER_USERNAME", "sa"),
        "encrypt": True,
        "trust_server_certificate": True,
        "timeout": 30,
    }
    admin = SQLServerConnection(config, {"password": password})
    name = "openbricks_test_" + uuid4().hex
    connection = admin.connect()
    connection.autocommit = True
    cursor = connection.cursor()
    try:
        cursor.execute(f"CREATE DATABASE [{name}]")
        cursor.execute(f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION ON")
        cursor.execute(
            f"ALTER DATABASE [{name}] SET CHANGE_TRACKING=ON (CHANGE_RETENTION=2 DAYS,AUTO_CLEANUP=ON)"
        )
        source = SQLServerConnection(
            {**config, "database": name}, {"password": password}
        )
        yield source
    finally:
        assert (
            name.startswith("openbricks_test_")
            and len(name) == len("openbricks_test_") + 32
        )
        cursor.execute(
            f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
        )
        cursor.execute(f"DROP DATABASE [{name}]")
        cursor.close()
        connection.close()


@pytest.fixture(scope="module")
def spark(database):
    from pyspark.sql import SparkSession
    from openbricks.spark_catalog import configure_catalog

    session = configure_catalog(
        SparkSession.builder.master("local[1]")
        .appName("openbricks-sqlserver-integration")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.default.parallelism", "1")
        .config("spark.sql.session.timeZone", "UTC")
    ).getOrCreate()
    try:
        yield session
    finally:
        session.stop()


def test_real_odbc_parameter_binding_inserts_rollback_and_native_jdbc(database, spark):
    database.execute(
        "CREATE TABLE dbo.DriverProbe (id int NOT NULL PRIMARY KEY,label nvarchar(100) NOT NULL)"
    )
    assert (
        database.insert("DriverProbe", [{"id": 1, "label": "O'Brien ?"}])["row_count"]
        == 1
    )
    assert database.read("SELECT label FROM dbo.DriverProbe WHERE id=?", [1]) == [
        {"label": "O'Brien ?"}
    ]
    with pytest.raises(Exception):
        database.execute(
            "INSERT INTO dbo.DriverProbe VALUES(2,N'rollback'); THROW 50001,'expected failure',1"
        )
    assert database.read("SELECT COUNT(*) AS rows FROM dbo.DriverProbe")[0]["rows"] == 1
    database.execute(
        "CREATE TABLE dbo.BulkProbe (id bigint NOT NULL PRIMARY KEY); INSERT INTO dbo.BulkProbe SELECT TOP(12050) ROW_NUMBER() OVER(ORDER BY (SELECT NULL)) FROM sys.all_objects a CROSS JOIN sys.all_objects b"
    )
    frame = database.read_spark(spark, "dbo.BulkProbe", fetch_size=512)
    assert frame.count() == 12050
    assert frame.where("id > 12000").count() == 50


def test_real_change_tracking_snapshot_updates_deletes_replay_and_schema_drift(
    database, spark, tmp_path
):
    database.execute(
        "CREATE TABLE dbo.Orders (id int NOT NULL PRIMARY KEY,amount decimal(18,2) NOT NULL,updated_at datetime2 NOT NULL); ALTER TABLE dbo.Orders ENABLE CHANGE_TRACKING; INSERT INTO dbo.Orders VALUES(1,10,'2020-01-01'),(2,20,'2020-01-01'),(3,30,'2020-01-01')"
    )

    class Registry:
        def sqlserver(self, name):
            return database

    blob = LocalBlobStore(tmp_path / "buckets")
    ingestor = ChangeTrackingIngestor(lambda: spark, Registry(), blob)
    spec = {
        "ingestion_id": uuid4().hex,
        "connection_id": "fixture",
        "source": {"schema": "dbo", "table": "Orders"},
        "sink": {"path": "blob://sync-test/orders"},
        "keys": ["id"],
        "mode": "change_tracking",
        "previous_watermark": None,
        "jdbc": {"fetch_size": 2},
    }
    initial = ingestor.run(spec)
    assert initial["rows"] == 3 and initial["deletes"] == 0
    path = blob.path(spec["sink"]["path"])
    initial_sink_version = spark.sql(
        f"DESCRIBE HISTORY delta.`{path}` LIMIT 1"
    ).first()["version"]
    database.execute(
        "UPDATE dbo.Orders SET amount=99 WHERE id=1; DELETE dbo.Orders WHERE id=2; INSERT INTO dbo.Orders VALUES(4,40,'1999-01-01')"
    )
    spec["previous_watermark"] = initial["watermark"]
    changes = ingestor.run(spec)
    assert changes["rows"] == 3 and changes["deletes"] == 1
    assert changes["watermark"]["version"] > initial["watermark"]["version"]
    ingestor.run(spec)  # Replay before controller checkpoint commit is idempotent.
    result = spark.read.format("delta").load(path).orderBy("id").collect()
    assert [row.id for row in result] == [1, 3, 4]
    assert float(result[0].amount) == 99
    spec["previous_watermark"] = changes["watermark"]
    assert ingestor.run(spec)["rows"] == 0
    missing = {**spec, "sink": {"path": "blob://sync-test/missing-remote-copy"}}
    with pytest.raises(SourceCheckpointExpired, match="sink is missing"):
        ingestor.run(missing)
    assert not Path(blob.path(missing["sink"]["path"])).exists()
    current_sink_version = spark.sql(
        f"DESCRIBE HISTORY delta.`{path}` LIMIT 1"
    ).first()["version"]
    spark.sql(f"RESTORE TABLE delta.`{path}` TO VERSION AS OF {initial_sink_version}")
    with pytest.raises(SourceCheckpointExpired, match="older than"):
        ingestor.run(spec)
    spark.sql(f"RESTORE TABLE delta.`{path}` TO VERSION AS OF {current_sink_version}")
    other = {**spec, "ingestion_id": uuid4().hex}
    with pytest.raises(ValueError, match="dedicated sink"):
        ingestor.run(other)
    database.execute("ALTER TABLE dbo.Orders ADD note nvarchar(30) NULL")
    with pytest.raises(SourceSchemaChanged):
        ingestor.run(spec)
    assert spark.read.format("delta").load(path).count() == 3
    database.execute("TRUNCATE TABLE dbo.Orders")
    with pytest.raises(SourceCheckpointExpired):
        ingestor.run(spec)
