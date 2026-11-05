"""A small, repeatable local-lake pipeline; Spark is provided by OpenBricks.

Each write replaces one demo output directory. Run the whole pipeline on one
compute service at a time. These helpers deliberately do not depend on the
location of the Git checkout or on variables left by a previous notebook run.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

BUCKET = "openbricks-demo"
RAW_URI = f"blob://{BUCKET}/raw/orders.csv"
BRONZE_URI = f"blob://{BUCKET}/bronze/orders"
SILVER_URI = f"blob://{BUCKET}/silver/orders"
REJECTED_URI = f"blob://{BUCKET}/silver/rejected_orders"
GOLD_URI = f"blob://{BUCKET}/gold/daily_revenue"
GOLD_TABLE = "spark_catalog.openbricks_demo.daily_revenue"


def seed_sample_data(blob) -> str:
    """Copy the CSV packaged in the wheel on first run; keep later user edits."""
    blob.create_bucket(BUCKET)
    if not Path(blob.path(RAW_URI)).exists():
        csv = files("openbricks_demo").joinpath("fixtures/orders.csv").read_text(encoding="utf-8")
        blob.write_text(RAW_URI, csv, overwrite=False)
    return RAW_URI


def ingest_orders(spark, blob):
    """Bronze: land CSV strings as Parquet without silently dropping bad rows."""
    from pyspark.sql.types import StringType, StructField, StructType

    seed_sample_data(blob)
    names = ("order_id", "order_date", "region", "quantity", "unit_price", "status", "updated_at")
    schema = StructType([StructField(name, StringType(), True) for name in names])
    frame = spark.read.option("header", True).schema(schema).csv(blob.path(RAW_URI))
    frame.write.mode("overwrite").parquet(blob.path(BRONZE_URI))
    return spark.read.parquet(blob.path(BRONZE_URI))


def clean_orders(spark, blob):
    """Silver: normalize, keep the newest version per ID, quarantine bad rows."""
    from pyspark.sql import Window, functions as F

    raw = spark.read.parquet(blob.path(BRONZE_URI))
    # The hash makes equal-timestamp duplicates deterministic for this example.
    normalized = raw.withColumn("_tie", F.sha2(F.to_json(F.struct(*raw.columns)), 256)).select(
        F.trim("order_id").alias("order_id"),
        F.expr("try_cast(order_date as date)").alias("order_date"),
        F.initcap(F.trim("region")).alias("region"),
        F.expr("try_cast(quantity as int)").alias("quantity"),
        F.expr("try_cast(unit_price as decimal(12, 2))").alias("unit_price"),
        F.when(F.lower(F.trim("status")) == "complete", "completed")
            .otherwise(F.lower(F.trim("status"))).alias("status"),
        F.expr("try_cast(updated_at as timestamp)").alias("updated_at"),
        "_tie",
    )
    window = Window.partitionBy("order_id").orderBy(F.col("updated_at").desc_nulls_last(), F.col("_tie").desc())
    latest = normalized.withColumn("_rank", F.row_number().over(window)).filter("_rank = 1").drop("_rank", "_tie")
    checked = latest.withColumn("rejection_reason",
        F.when(F.col("order_id").isNull() | (F.col("order_id") == ""), "missing order ID")
         .when(F.col("order_date").isNull(), "invalid order date")
         .when(F.col("region").isNull() | (F.col("region") == ""), "missing region")
         .when(F.col("quantity").isNull() | (F.col("quantity") <= 0), "quantity must be positive")
         .when(F.col("unit_price").isNull() | (F.col("unit_price") < 0), "invalid unit price")
         .when(F.col("updated_at").isNull(), "invalid update timestamp")
         .when(F.col("status").isNull() | ~F.col("status").isin("completed", "pending", "cancelled"), "unknown status"))
    checked.filter("rejection_reason is null").drop("rejection_reason").write.mode("overwrite").parquet(blob.path(SILVER_URI))
    checked.filter("rejection_reason is not null").write.mode("overwrite").parquet(blob.path(REJECTED_URI))
    return spark.read.parquet(blob.path(SILVER_URI))


def publish_revenue(spark, blob):
    """Gold: publish completed sales and a persistent local Spark SQL table."""
    from pyspark.sql import functions as F

    orders = spark.read.parquet(blob.path(SILVER_URI)).filter("status = 'completed'")
    revenue = orders.groupBy("order_date", "region").agg(
        F.count("order_id").alias("orders"),
        F.sum("quantity").alias("units"),
        F.sum(F.col("quantity") * F.col("unit_price")).cast("decimal(18, 2)").alias("revenue"),
    )
    target = Path(blob.path(GOLD_URI)).resolve().as_uri()
    revenue.write.mode("overwrite").parquet(blob.path(GOLD_URI))
    spark.sql("CREATE DATABASE IF NOT EXISTS spark_catalog.openbricks_demo")
    # This is a persistent external table in this compute service's local
    # metastore, not a temporary view and not an optional Unity Catalog table.
    escaped = target.replace("'", "''")
    spark.sql(f"CREATE TABLE IF NOT EXISTS {GOLD_TABLE} USING PARQUET LOCATION '{escaped}'")
    spark.sql(f"REFRESH TABLE {GOLD_TABLE}")
    return spark.table(GOLD_TABLE)


def run_pipeline(spark, blob) -> dict:
    """Run every layer in order; safe to repeat with the same demo input."""
    from pyspark.sql import functions as F

    bronze = ingest_orders(spark, blob)
    bronze_rows = bronze.count()
    silver = clean_orders(spark, blob)
    silver_rows = silver.count()
    rejected_rows = spark.read.parquet(blob.path(REJECTED_URI)).count()
    gold = publish_revenue(spark, blob)
    summary = gold.agg(F.sum("orders").alias("completed_orders"), F.sum("revenue").alias("total_revenue")).first()
    return {
        "bronze_rows": bronze_rows,
        "silver_rows": silver_rows,
        "rejected_rows": rejected_rows,
        "gold_rows": gold.count(),
        "completed_orders": summary["completed_orders"],
        "total_revenue": str(summary["total_revenue"]),
        "gold_table": GOLD_TABLE,
    }
