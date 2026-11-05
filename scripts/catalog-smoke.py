"""Real Unity Catalog + Spark checks against a disposable QA deployment."""
import json
import os
import time
from uuid import uuid4
import httpx

c = httpx.Client(base_url="http://127.0.0.1:3000", timeout=30)
c.post("/api/auth/login", json={"username": os.getenv("OPENBRICKS_ADMIN_USERNAME", "admin"), "password": os.environ["OPENBRICKS_ADMIN_PASSWORD"]}).raise_for_status()


def api(method, path, **kwargs):
    response = c.request(method, "/api" + path, **kwargs)
    assert response.is_success, f"{path}: {response.status_code} {response.text[:1500]}"
    return response.json()


def execute(source):
    notebook = api("POST", "/notebooks", json={"name": "Catalog integration check", "cells": [{"id": "catalog-check", "language": "python", "source": source}]})
    result = api("POST", f"/notebooks/{notebook['id']}/run", json={})
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = api("GET", f"/runs/{result['id']}")
        if result["status"] not in ("queued", "running"):
            break
        time.sleep(1)
    assert result["status"] == "succeeded", json.dumps(result)[-6000:]
    return result["outputs"][0]


assert api("GET", "/catalog/status")["available"]
created = c.post("/api/catalog/catalogs", json={"name": "unity", "comment": "OpenBricks integration test"})
assert created.status_code in (201, 409), created.text
schema = "qa_" + uuid4().hex[:8]
api("POST", "/catalog/schemas", json={"catalog_name": "unity", "name": schema})
local_path = "/data/buckets/workspace/" + schema
execute(f"spark.createDataFrame([(20,), (22,)], ['value']).write.mode('overwrite').parquet({local_path!r})\nprint('written')")
definition = {"catalog_name": "unity", "schema_name": schema, "name": "local_values", "data_source_format": "PARQUET",
              "storage_location": "file://" + local_path, "columns": [{"name": "value", "type_name": "LONG", "type_text": "bigint"}]}
api("POST", "/catalog/tables", json=definition)
assert any(item["name"] == "local_values" for item in api("GET", f"/catalog/tables?catalog_name=unity&schema_name={schema}")["items"])
output = execute(f"display(spark.sql('SELECT SUM(value) AS total FROM unity.{schema}.local_values'))")
assert output["result"]["rows"] == [[42]], output
print("PASS: real Unity Catalog catalog/schema/table registration and three-part Spark SQL over shared Parquet", flush=True)

s3_path = "s3a://openbricks/catalog-integration/" + schema
execute(f"spark.createDataFrame([(20,), (22,)], ['value']).write.mode('overwrite').parquet({s3_path!r})\nprint('written to S3')")
definition.update(name="s3_values", storage_location=s3_path.replace("s3a://", "s3://"))
api("POST", "/catalog/tables", json=definition)
output = execute(f"display(spark.sql('SELECT SUM(value) AS total FROM unity.{schema}.s3_values'))")
assert output["result"]["rows"] == [[42]], output
print("PASS: native Spark S3A writes to SeaweedFS and Unity Catalog table reads return 42", flush=True)
