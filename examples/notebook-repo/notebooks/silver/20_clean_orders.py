# Databricks notebook source
# MAGIC %md
# MAGIC # Silver: normalize and validate orders
# MAGIC Run bronze first on this same compute service. We keep the latest version
# MAGIC of each order, normalize status/region, and quarantine invalid records.
# MAGIC This is batch replacement, not an incremental CDC or streaming pipeline.

# COMMAND ----------
from openbricks_demo.pipeline import REJECTED_URI, clean_orders

silver = clean_orders(spark, blob)
print(f"{silver.count()} valid orders")
display(silver.orderBy("order_id"))

# COMMAND ----------
display(spark.read.parquet(blob.path(REJECTED_URI)).orderBy("order_id"))
