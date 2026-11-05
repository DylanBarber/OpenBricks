# Databricks notebook source
# MAGIC %md
# MAGIC # Verify the deployed project's output
# MAGIC Run after prepare, using the same date and compute host.

# COMMAND ----------

import json

run_date = params["run_date"]
payload = json.loads(blob.read_text(f"blob://deployment-demo/{run_date}/orders.json"))
assert payload == {"run_date": run_date, "orders": 4, "revenue": 96.5}, payload
print(f"Validated {payload['orders']} orders and {payload['revenue']:.2f} revenue for {run_date}")
