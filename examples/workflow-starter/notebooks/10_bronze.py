# Databricks notebook source
# MAGIC %md
# MAGIC # Parameterized bronze ingestion
# MAGIC Install with `install.py` to create the typed parameters and workflow. Synthetic data and local Parquet require no saved connections or extra packages.

# COMMAND ----------

from pyspark.sql import functions as F

run_date = params["run_date"]
region = params["region"]
blob.create_bucket("workflow-demo")
bronze_uri = f"blob://workflow-demo/{run_date}/{region.lower()}/bronze"
orders = spark.createDataFrame(
    [(1, "North", 2, 10.0), (2, "South", 1, 30.0),
     (3, "North", 3, 7.5), (4, "South", 2, 12.0)],
    ["order_id", "region", "quantity", "unit_price"],
)
if region != "All":
    orders = orders.where(F.col("region") == region)
data.quality(
    orders,
    [{"type": "not_null", "column": "order_id"},
     {"type": "unique", "column": "order_id"},
     {"type": "row_count", "min": params["minimum_rows"]}],
    fail_on_error=True,
    dataset=bronze_uri,
)
data.write(orders, {"path": bronze_uri, "format": "parquet"}, mode="overwrite")
display(orders.orderBy("order_id"))
print(f"Bronze ready: {bronze_uri}")
