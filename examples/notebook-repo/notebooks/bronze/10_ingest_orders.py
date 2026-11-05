# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze: ingest the packaged CSV
# MAGIC Requires the `openbricks-notebook-demo` wheel. The first run seeds the raw
# MAGIC CSV; later runs preserve it. This step replaces the bronze Parquet output.

# COMMAND ----------
from openbricks_demo.pipeline import BRONZE_URI, ingest_orders

bronze = ingest_orders(spark, blob)
print(f"{bronze.count()} rows written to {BRONZE_URI}")
display(bronze.orderBy("order_id", "updated_at"))
