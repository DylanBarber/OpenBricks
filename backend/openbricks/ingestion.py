"""Incremental JDBC ingestion with a durable post-success watermark.

Delta MERGE is idempotent on the configured keys. The watermark is emitted only
after its sink commits, and the management runtime commits it only after the
entire notebook run succeeds. This is at-least-once source reading, not CDC.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from .delta_tools import data_path, table_identifier
from .connectors import identifier, json_value


def sql_literal(value, kind):
    if kind == "number":
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("Watermarks must be finite")
        return str(result)
    if kind not in ("timestamp", "date", "string"):
        raise ValueError("Watermark type must be number, timestamp, date, or string")
    if kind == "timestamp":
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    elif kind == "date":
        date.fromisoformat(str(value))
    return "N'" + str(value).replace("'", "''") + "'"


class IncrementalIngestor:
    def __init__(self, spark_factory, registry, blob, lineage=None, quality=None):
        self.spark_factory, self.registry, self.blob, self.lineage = (
            spark_factory,
            registry,
            blob,
            lineage,
        )
        self.quality = quality

    def run(self, spec):
        from pyspark.sql import functions as F
        from delta.tables import DeltaTable

        source, sink = spec["source"], spec["sink"]
        keys = spec.get("keys", [])
        watermark = spec["watermark"]
        if not keys or len(keys) > 20:
            raise ValueError("Incremental ingestion needs 1–20 merge keys")
        for name in [*keys, watermark["column"]]:
            identifier(name)
        table = (
            identifier(source.get("schema", "dbo")) + "." + identifier(source["table"])
        )
        where = ""
        previous = spec.get("previous_watermark")
        if previous is not None:
            where = (
                " WHERE "
                + identifier(watermark["column"])
                + " > "
                + sql_literal(previous, watermark.get("type", "number"))
            )
        query = f"(SELECT * FROM {table}{where}) AS openbricks_incremental"
        spark = self.spark_factory()
        frame = self.registry.sqlserver(spec["connection_id"]).read_spark(
            spark, query, **spec.get("jdbc", {})
        )
        frame = frame.persist()
        try:
            rows = frame.count()
            if not rows:
                return {"rows": 0, "watermark": previous}
            missing = set([*keys, watermark["column"]]) - set(frame.columns)
            if missing:
                raise ValueError("Source is missing merge/watermark columns")

            def col(name):
                return F.col("`" + name.replace("`", "``") + "`")

            invalid = col(watermark["column"]).isNull()
            for key in keys:
                invalid = invalid | col(key).isNull()
            if (
                frame.filter(invalid).limit(1).count()
                or frame.select(*[col(key) for key in keys]).distinct().count() != rows
            ):
                raise ValueError(
                    "Incremental source must contain non-null watermark/keys and unique merge keys"
                )
            if spec.get("checks"):
                if self.quality:
                    self.quality(
                        frame, spec["checks"], fail_on_error=True, dataset=table
                    )
                else:
                    from .data_quality import check_data

                    check_data(frame, spec["checks"], fail_on_error=True)
            new_watermark = frame.agg(
                F.max(col(watermark["column"])).alias("value")
            ).first()["value"]
            if sink.get("table"):
                name = table_identifier(sink["table"])
                exists = spark.catalog.tableExists(sink["table"])
                target = DeltaTable.forName(spark, sink["table"]) if exists else None
            else:
                path = data_path(sink["path"], self.blob)
                exists = DeltaTable.isDeltaTable(spark, path)
                target = DeltaTable.forPath(spark, path) if exists else None
            if exists:
                condition = " AND ".join(
                    "target.`"
                    + key.replace("`", "``")
                    + "` = source.`"
                    + key.replace("`", "``")
                    + "`"
                    for key in keys
                )
                target.alias("target").merge(
                    frame.alias("source"), condition
                ).whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()
            elif sink.get("table"):
                frame.write.format("delta").mode("errorifexists").saveAsTable(
                    sink["table"]
                )
            else:
                frame.write.format("delta").mode("errorifexists").save(path)
            if self.lineage:
                self.lineage(
                    "sqlserver://"
                    + spec["connection_id"]
                    + "/"
                    + source.get("schema", "dbo")
                    + "/"
                    + source["table"],
                    "read",
                    {"rows": rows},
                )
                self.lineage(
                    sink.get("table") or sink.get("path"),
                    "write",
                    {"format": "delta", "rows": rows},
                )
            return {"rows": rows, "watermark": json_value(new_watermark)}
        finally:
            frame.unpersist()
