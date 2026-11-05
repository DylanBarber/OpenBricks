# Databricks notebook source
# MAGIC %md
# MAGIC # Quality-checked silver orders
# MAGIC Run after bronze with the same parameters on the same compute host. Repeat runs replace the supplied example outputs.

# COMMAND ----------

from pyspark.sql import functions as F

region = params["region"]
base_uri = f"blob://workflow-demo/{params['run_date']}/{region.lower()}"
orders = data.read({"path": base_uri + "/bronze", "format": "parquet"})
silver = orders.withColumn("revenue", F.round(F.col("quantity") * F.col("unit_price"), 2))
data.quality(
    silver,
    [{"type": "not_null", "column": "order_id"},
     {"type": "unique", "column": "order_id"},
     {"type": "range", "column": "revenue", "min": 0},
     {"type": "row_count", "min": params["minimum_rows"]}],
    fail_on_error=True,
    dataset=base_uri + "/silver",
)
data.write(silver, {"path": base_uri + "/silver", "format": "parquet"}, mode="overwrite")
summary = silver.agg(F.count("*").alias("orders"), F.round(F.sum("revenue"), 2).alias("revenue"))
result = summary.first()
expected = {"All": (4, 96.5), "North": (2, 42.5), "South": (2, 54.0)}[region]
assert result["orders"] == expected[0], result
assert abs(float(result["revenue"]) - expected[1]) < 0.001, result
display(summary)
print(f"Silver checks passed: {base_uri}/silver")
