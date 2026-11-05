"""SQL Server Change Tracking snapshots, streamed to a dedicated Delta sink.

Version validation, source rows, and net changes are read in one SQL Server
snapshot transaction. Only bounded Arrow batches are held in Python memory.
The controller advances the checkpoint after the entire notebook succeeds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile

from .connectors import identifier
from .delta_tools import data_path, table_identifier

OP = "__openbricks_ct_operation"
OWNER = "openbricks.ingestion.id"
SCHEMA = "openbricks.source.schema"
APPLIED = "openbricks.ingestion.version"


class SourceSchemaChanged(ValueError):
    pass


class SourceCheckpointExpired(ValueError):
    pass


def schema_hash(columns):
    return hashlib.sha256(
        json.dumps(columns, sort_keys=True, default=str).encode()
    ).hexdigest()


def arrow_type(column):
    import pyarrow as pa

    kind = column["type"].lower()
    if kind in ("bigint",):
        return pa.int64()
    if kind in ("int", "smallint", "tinyint"):
        return {"int": pa.int32(), "smallint": pa.int16(), "tinyint": pa.uint8()}[kind]
    if kind in ("decimal", "numeric", "money", "smallmoney"):
        return pa.decimal128(int(column["precision"]), int(column["scale"]))
    if kind in ("float", "real"):
        return pa.float64() if kind == "float" else pa.float32()
    if kind == "bit":
        return pa.bool_()
    if kind == "date":
        return pa.date32()
    if kind in ("datetime", "datetime2", "smalldatetime"):
        return pa.timestamp("us")
    if kind == "datetimeoffset":
        return pa.timestamp("us", tz="UTC")
    if kind == "time":
        return pa.time64("us")
    if kind in ("binary", "varbinary", "image", "timestamp", "rowversion"):
        return pa.binary()
    if kind in (
        "char",
        "varchar",
        "text",
        "nchar",
        "nvarchar",
        "ntext",
        "xml",
        "uniqueidentifier",
        "sysname",
    ):
        return pa.string()
    raise ValueError(
        f"Change Tracking does not support SQL type {kind}; select a supported source table"
    )


def metadata(cursor, source):
    schema, table = source.get("schema", "dbo"), source["table"]
    qualified = identifier(schema) + "." + identifier(table)
    cursor.execute(
        "SELECT DB_NAME(),t.object_id,t.create_date,ct.begin_version FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id LEFT JOIN sys.change_tracking_tables ct ON ct.object_id=t.object_id WHERE s.name=? AND t.name=?",
        (schema, table),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError(
            "Source table was not found or is not visible to the connection"
        )
    if row[3] is None:
        raise ValueError(
            "Enable Change Tracking on the source database and table before synchronizing"
        )
    identity = hashlib.sha256(json.dumps(list(row), default=str).encode()).hexdigest()
    cursor.execute(
        "SELECT c.name,COALESCE(ty.name,TYPE_NAME(c.user_type_id)),c.max_length,c.precision,c.scale,c.is_nullable FROM sys.columns c LEFT JOIN sys.types ty ON ty.user_type_id=c.system_type_id AND ty.user_type_id=ty.system_type_id WHERE c.object_id=? ORDER BY c.column_id",
        (row[1],),
    )
    columns = [
        dict(zip(("name", "type", "length", "precision", "scale", "nullable"), item))
        for item in cursor.fetchall()
    ]
    if not columns or any(item["name"].lower() == OP.lower() for item in columns):
        raise ValueError(
            "Source has no supported columns or uses a reserved synchronization column"
        )
    cursor.execute(
        "SELECT c.name FROM sys.indexes i JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id WHERE i.object_id=? AND i.is_primary_key=1 ORDER BY ic.key_ordinal",
        (row[1],),
    )
    keys = [item[0] for item in cursor.fetchall()]
    cursor.execute(
        "SELECT CHANGE_TRACKING_MIN_VALID_VERSION(?),CHANGE_TRACKING_CURRENT_VERSION()",
        (row[1],),
    )
    minimum, version = cursor.fetchone()
    if minimum is None or version is None:
        raise ValueError("Change Tracking is unavailable for this source")
    return {
        "table": qualified,
        "columns": columns,
        "keys": keys,
        "identity": identity,
        "schema_sha256": schema_hash(columns),
        "minimum": int(minimum),
        "version": int(version),
    }


def validate_checkpoint(info, checkpoint, keys):
    if not keys or set(keys) != set(info["keys"]):
        raise ValueError(
            "Change Tracking merge keys must match the source table's primary key"
        )
    if checkpoint is None:
        return
    if (
        not isinstance(checkpoint, dict)
        or not isinstance(checkpoint.get("version"), int)
        or isinstance(checkpoint.get("version"), bool)
    ):
        raise ValueError("Invalid Change Tracking checkpoint")
    if checkpoint.get("source_identity") != info["identity"]:
        raise SourceCheckpointExpired(
            "Source was recreated, truncated, or Change Tracking was reset; create a new ingestion and sink for a fresh snapshot"
        )
    if not info["minimum"] <= checkpoint["version"] <= info["version"]:
        raise SourceCheckpointExpired(
            "Change Tracking checkpoint is outside retained source history; create a new ingestion and sink for a fresh snapshot"
        )
    if checkpoint.get("schema_sha256") != info["schema_sha256"]:
        raise SourceSchemaChanged(
            "Source schema changed; review the new schema and create a new ingestion and sink before synchronizing"
        )


def snapshot_to_parquet(
    connection, source, keys, checkpoint, directory, batch_size=4096
):
    import pyarrow as pa
    import pyarrow.parquet as pq

    batch_size = max(1, min(int(batch_size), 10000))
    # ODBC manual-commit mode can begin a read-committed transaction before SET.
    # This is a fresh, helper-owned connection: use explicit SQL transactions so
    # SNAPSHOT is selected before the transaction starts.
    connection.autocommit = True
    cursor = connection.cursor()
    # Do not enable snapshot isolation/Change Tracking silently on a user's DB.
    cursor.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
    cursor.execute("BEGIN TRANSACTION")
    try:
        info = metadata(cursor, source)
        validate_checkpoint(info, checkpoint, keys)
        columns = info["columns"]
        schema = pa.schema(
            [
                pa.field(item["name"], arrow_type(item), nullable=True)
                for item in columns
            ]
            + [pa.field(OP, pa.string(), nullable=False)]
        )
        names = [item["name"] for item in columns]
        expressions = []
        for item in columns:
            name = item["name"]
            alias = "ct" if checkpoint is not None and name in keys else "s"
            value = alias + "." + identifier(name)
            if item["type"].lower() in ("datetimeoffset", "xml", "uniqueidentifier"):
                value = "CONVERT(nvarchar(max)," + value + ",127)"
            expressions.append(value + " AS " + identifier(name))
        if checkpoint is None:
            query = (
                "SELECT "
                + ",".join(expressions)
                + ",CAST('I' AS nvarchar(1)) AS "
                + identifier(OP)
                + " FROM "
                + info["table"]
                + " s"
            )
            cursor.execute(query)
        else:
            join = " AND ".join(
                "s." + identifier(key) + "=ct." + identifier(key) for key in keys
            )
            query = (
                "SELECT "
                + ",".join(expressions)
                + ",ct.SYS_CHANGE_OPERATION AS "
                + identifier(OP)
                + " FROM CHANGETABLE(CHANGES "
                + info["table"]
                + ",?) ct LEFT JOIN "
                + info["table"]
                + " s ON "
                + join
                + " WHERE ct.SYS_CHANGE_VERSION<=?"
            )
            cursor.execute(query, (checkpoint["version"], info["version"]))
        count = deletes = chunks = 0
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            arrays = [[] for _ in schema]
            for row in batch:
                for index, value in enumerate(row):
                    if index < len(columns) and value is not None:
                        kind = columns[index]["type"].lower()
                        if kind == "datetimeoffset":
                            value = datetime.fromisoformat(
                                str(value).replace("Z", "+00:00")
                            ).astimezone(timezone.utc)
                        elif pa.types.is_binary(schema[index].type):
                            value = bytes(value)
                        elif pa.types.is_string(schema[index].type):
                            value = str(value)
                    arrays[index].append(value)
                deletes += str(row[-1]) == "D"
            table = pa.Table.from_arrays(
                [
                    pa.array(values, type=field.type)
                    for field, values in zip(schema, arrays)
                ],
                schema=schema,
            )
            pq.write_table(table, Path(directory) / f"batch-{chunks:08d}.parquet")
            chunks += 1
            count += len(batch)
        if not chunks:
            pq.write_table(
                pa.Table.from_pylist([], schema=schema),
                Path(directory) / "batch-00000000.parquet",
            )
        cursor.execute("COMMIT TRANSACTION")
        return {
            **info,
            "rows": count,
            "deletes": deletes,
            "checkpoint": {
                "version": info["version"],
                "source_identity": info["identity"],
                "schema_sha256": info["schema_sha256"],
            },
        }
    except BaseException:
        try:
            cursor.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
        except Exception:
            pass
        raise
    finally:
        cursor.close()


class ChangeTrackingIngestor:
    def __init__(self, spark_factory, registry, blob, lineage=None, quality=None):
        self.spark_factory, self.registry, self.blob = spark_factory, registry, blob
        self.lineage, self.quality = lineage, quality

    def run(self, spec):
        from delta.tables import DeltaTable
        from pyspark.sql import functions as F

        source, sink, keys = spec["source"], spec["sink"], spec["keys"]
        identifier(source.get("schema", "dbo"))
        identifier(source["table"])
        for key in keys:
            identifier(key)
        owner = str(spec.get("ingestion_id") or "")
        if not owner:
            raise ValueError("Change Tracking requires a saved ingestion identity")
        checkpoint = spec.get("previous_watermark")
        stage = self.blob.root.parent / "staging" / "sql-sync"
        stage.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=stage) as directory:
            connection = self.registry.sqlserver(spec["connection_id"]).connect()
            try:
                info = snapshot_to_parquet(
                    connection,
                    source,
                    keys,
                    checkpoint,
                    directory,
                    spec.get("jdbc", {}).get("fetch_size", 4096),
                )
            finally:
                connection.close()
            spark = self.spark_factory()
            frame = spark.read.parquet(directory)
            upserts = frame.filter(F.col(OP) != "D").drop(OP)
            if spec.get("checks") and info["rows"] > info["deletes"]:
                if self.quality:
                    self.quality(
                        upserts,
                        spec["checks"],
                        fail_on_error=True,
                        dataset=info["table"],
                    )
                else:
                    from .data_quality import check_data

                    check_data(upserts, spec["checks"], fail_on_error=True)
            if sink.get("table"):
                sink_identifier = table_identifier(sink["table"])
                exists = spark.catalog.tableExists(sink["table"])
                builder = DeltaTable.createIfNotExists(spark).tableName(sink["table"])
                target = DeltaTable.forName(spark, sink["table"]) if exists else None
            else:
                path = data_path(sink["path"], self.blob)
                sink_identifier = "delta.`" + path.replace("`", "``") + "`"
                exists = DeltaTable.isDeltaTable(spark, path)
                builder = DeltaTable.createIfNotExists(spark).location(path)
                target = DeltaTable.forPath(spark, path) if exists else None
            if not exists:
                if checkpoint is not None:
                    raise SourceCheckpointExpired(
                        "The checkpoint's Delta sink is missing; create a new ingestion and sink for a complete snapshot"
                    )
                target = (
                    builder.addColumns(upserts.schema)
                    .property(OWNER, owner)
                    .property(SCHEMA, info["schema_sha256"])
                    .property(APPLIED, "0")
                    .execute()
                )
            # createIfNotExists can encounter a concurrently created table.
            properties = target.detail().select("properties").first()["properties"]
            if properties.get(OWNER) != owner:
                raise ValueError(
                    "Change Tracking requires a dedicated sink owned by this ingestion; choose a new sink"
                )
            if properties.get(SCHEMA) != info["schema_sha256"]:
                raise SourceSchemaChanged(
                    "Source schema differs from this sink; create a new ingestion and sink after reviewing the schema"
                )
            applied = int(properties.get(APPLIED, "-1"))
            if checkpoint is not None and applied < int(checkpoint["version"]):
                raise SourceCheckpointExpired(
                    "The Delta sink is older than its saved checkpoint; recover matching data/checkpoint or create a new ingestion and sink"
                )
            names = [item["name"] for item in info["columns"]]
            values = {
                name: F.col("source.`" + name.replace("`", "``") + "`")
                for name in names
            }
            condition = " AND ".join(
                "target.`"
                + key.replace("`", "``")
                + "`=source.`"
                + key.replace("`", "``")
                + "`"
                for key in keys
            )
            merge = (
                target.alias("target")
                .merge(frame.alias("source"), condition)
                .whenMatchedDelete(condition=f"source.`{OP}`='D'")
                .whenMatchedUpdate(condition=f"source.`{OP}`<>'D'", set=values)
                .whenNotMatchedInsert(condition=f"source.`{OP}`<>'D'", values=values)
            )
            if checkpoint is None:
                merge = merge.whenNotMatchedBySourceDelete()
            merge.execute()
            if applied != int(info["version"]):
                spark.sql(
                    f"ALTER TABLE {sink_identifier} SET TBLPROPERTIES ('{APPLIED}' = '{int(info['version'])}')"
                )
            if self.lineage:
                self.lineage(
                    "sqlserver://"
                    + spec["connection_id"]
                    + "/"
                    + source.get("schema", "dbo")
                    + "/"
                    + source["table"],
                    "read",
                    {
                        "mode": "change_tracking",
                        "rows": info["rows"],
                        "version": info["version"],
                    },
                )
                self.lineage(
                    sink.get("table") or sink["path"],
                    "write",
                    {
                        "format": "delta",
                        "rows": info["rows"],
                        "deletes": info["deletes"],
                    },
                )
            return {
                "rows": info["rows"],
                "deletes": info["deletes"],
                "watermark": info["checkpoint"],
                "schema_sha256": info["schema_sha256"],
                "mode": "change_tracking",
            }
