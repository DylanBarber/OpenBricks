# Databricks notebook source
# MAGIC %md
# MAGIC # Gold: publish daily completed revenue
# MAGIC Run silver first on this same compute service. This creates a persistent
# MAGIC table in the compute service's local Spark metastore; Unity Catalog is
# MAGIC optional and is not needed for this demo.

# COMMAND ----------
from openbricks_demo.pipeline import GOLD_TABLE, publish_revenue

gold = publish_revenue(spark, blob)
print(f"Published {GOLD_TABLE}")
display(gold.orderBy("order_date", "region"))

# COMMAND ----------
# MAGIC %sql
# MAGIC SELECT SUM(orders) AS completed_orders, SUM(revenue) AS total_revenue
# MAGIC FROM spark_catalog.openbricks_demo.daily_revenue
