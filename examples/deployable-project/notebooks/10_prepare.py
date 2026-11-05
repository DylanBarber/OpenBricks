# Databricks notebook source
# MAGIC %md
# MAGIC # Prepare a deployable project fixture
# MAGIC This example uses local files and the packaged Python runtime. It does not start Spark or require named connections.

# COMMAND ----------

import json

run_date = params["run_date"]
blob.create_bucket("deployment-demo")
dataset = f"blob://deployment-demo/{run_date}/orders.json"
payload = {"run_date": run_date, "orders": 4, "revenue": 96.5}
blob.write_text(dataset, json.dumps(payload))
print(f"Prepared {payload['orders']} orders for {run_date}")
