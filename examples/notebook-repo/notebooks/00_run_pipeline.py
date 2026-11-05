# Databricks notebook source
# MAGIC %md
# MAGIC # Orders: raw data to a daily revenue report
# MAGIC First build and install this repository's `openbricks-notebook-demo` wheel
# MAGIC from **Packages → Build wheel**. Then choose a compute service and **Run all**.
# MAGIC This notebook runs every layer in order and is the notebook to schedule.
# MAGIC It creates only the `openbricks-demo` local bucket and its demo table.

# COMMAND ----------
from openbricks_demo.pipeline import GOLD_TABLE, run_pipeline

result = run_pipeline(spark, blob)
print(result)

# COMMAND ----------
# MAGIC %md
# MAGIC ## Check the supplied fixture
# MAGIC Nine raw rows contain one newer version of an order and two invalid orders.
# MAGIC Six valid orders remain; one is cancelled. Completed revenue is **85.50**.
# MAGIC These assertions intentionally detect changes if you replace the sample CSV.

# COMMAND ----------
assert result["bronze_rows"] == 9, result
assert result["silver_rows"] == 6, result
assert result["rejected_rows"] == 2, result
assert result["completed_orders"] == 5, result
assert result["gold_rows"] == 5, result
assert result["total_revenue"] == "85.50", result
display(spark.table(GOLD_TABLE).orderBy("order_date", "region"))

# COMMAND ----------
# MAGIC %sql
# MAGIC SELECT order_date, region, orders, units, revenue
# MAGIC FROM spark_catalog.openbricks_demo.daily_revenue
# MAGIC ORDER BY order_date, region

# COMMAND ----------
# MAGIC %md
# MAGIC ## Next steps
# MAGIC Run all again to verify that outputs are replaced rather than appended.
# MAGIC Browse `openbricks-demo` in **Object storage** to inspect raw, bronze, silver, and gold.
# MAGIC The layer notebooks are useful for exploration; schedule this notebook alone.
# MAGIC The default idle timeout discards Python variables, so every scheduled run
# MAGIC imports its helpers and reads durable data again.
