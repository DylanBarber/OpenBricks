"""Real integration check. Run ONLY against a disposable OpenBricks test volume.

Uses the running image's OPENBRICKS_ADMIN_PASSWORD. No credentials are printed.
Creates test notebooks, a bucket, schedule, Git checkout, wheel, and Spark table.
The Git check downloads the public PyPA sampleproject repository.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx


parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:3000")
parser.add_argument("--verify-persistence", action="store_true")
args = parser.parse_args()
marker = Path(os.environ.get("DATA_DIR", "/data")) / "smoke-check.json"
client = httpx.Client(base_url=args.base_url, timeout=180)


def request(method, path, **kwargs):
    response = client.request(method, "/api" + path, **kwargs)
    assert response.is_success, f"{method} {path}: {response.status_code} {response.text[:3000]}"
    return response.json()


def run(notebook_id, cell_id=None, timeout=150):
    started = request("POST", f"/notebooks/{notebook_id}/run", json={"cell_id": cell_id} if cell_id else {})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = request("GET", f"/runs/{started['id']}")
        if result["status"] not in ("queued", "running"):
            assert result["status"] == "succeeded", json.dumps(result, indent=2)[-8000:]
            return result
        time.sleep(1)
    raise AssertionError("Notebook run did not complete within integration deadline")


def notebook(name, cells):
    return request("POST", "/notebooks", json={"name": name, "cells": [
        {"id": str(index), "language": language, "source": source}
        for index, (language, source) in enumerate(cells)
    ]})


def large_upload_check(bucket):
    large = b"x" * (12 * 1024 * 1024)
    uploaded = request("POST", f"/buckets/{bucket}/objects", data={"key": "large-file.bin"}, files={"file": ("large.bin", large, "application/octet-stream")})
    assert uploaded["size"] == len(large)
    assert client.get(f"/api/buckets/{bucket}/download", params={"key": "large-file.bin"}).content == large
    print("PASS: a 12 MB multipart upload passes through the UI proxy intact", flush=True)


assert client.get("/api/health").status_code == 200
assert client.get("/api/notebooks").status_code == 401
assert client.get("/").status_code == 200
request("POST", "/auth/login", json={"username": os.environ.get("OPENBRICKS_ADMIN_USERNAME", "admin"), "password": os.environ["OPENBRICKS_ADMIN_PASSWORD"]})
assert request("GET", "/auth/me")["role"] == "admin"
print("PASS: UI, same-origin proxy, protected API, and cookie login", flush=True)

if args.verify_persistence:
    state = json.loads(marker.read_text())
    assert request("GET", f"/notebooks/{state['notebook_id']}")["id"] == state["notebook_id"]
    assert any(j["id"] == state["job_id"] and j["enabled"] for j in request("GET", "/jobs"))
    assert request("GET", f"/buckets/{state['bucket']}/objects")
    large_upload_check(state["bucket"])
    assert any(p["name"] == "sampleproject" for p in request("GET", "/packages"))
    check = notebook("Persistence check", [("python", "import sample\nprint(sample.__name__)"),
        ("sql", f"SELECT SUM(value) AS total FROM {state['table']}")])
    output = run(check["id"])
    assert "sample" in output["outputs"][0]["stdout"]
    assert output["outputs"][1]["result"]["rows"] == [[42]]
    print("PASS: notebooks, enabled schedule, objects, wheels, Hive catalog, and Spark table survive container recreation", flush=True)
    raise SystemExit(0)

suffix = uuid4().hex[:8]
bucket = "qa-" + suffix
table = "qa_" + suffix
request("POST", "/buckets", json={"name": bucket})
request("POST", f"/buckets/{bucket}/objects", data={"key": "input/values.csv"}, files={"file": ("values.csv", b"value\n20\n22\n", "text/csv")})
assert client.get(f"/api/buckets/{bucket}/download", params={"key": "input/values.csv"}).content == b"value\n20\n22\n"
bad = client.post(f"/api/buckets/{bucket}/objects", data={"key": "../escape"}, files={"file": ("bad", b"bad")})
assert bad.status_code == 400
large_upload_check(bucket)
main = notebook("Spark integration " + suffix, [
    ("python", f"df = spark.read.option('header', True).option('inferSchema', True).csv(blob.path('blob://{bucket}/input/values.csv'))\ndf.createOrReplaceTempView('qa_values')\ndf.write.mode('overwrite').saveAsTable('{table}')\ndisplay(df)"),
    ("sql", "SELECT SUM(value) AS total FROM qa_values"),
    ("python", "answer = 40\nprint('state initialized')"),
    ("python", "print(answer + 2)"),
])
output = run(main["id"])
assert output["outputs"][1]["result"]["rows"] == [[42]]
assert "42" in output["outputs"][3]["stdout"]
assert "42" in run(main["id"], "3")["outputs"][0]["stdout"]
print("PASS: uploaded CSV -> local Spark -> SQL aggregate -> persistent Hive table; interactive cell state", flush=True)

source = b"# Databricks notebook source\nprint('imported')\n# COMMAND ----------\n# MAGIC %sql\n# MAGIC SELECT 42 AS answer\n"
imported = request("POST", "/notebooks/import", files={"file": ("git_example.py", source, "text/x-python")})
assert len(imported["cells"]) == 2
assert run(imported["id"])["outputs"][1]["result"]["rows"] == [[42]]
exported = request("GET", f"/notebooks/{imported['id']}/export")
assert exported["nbformat"] == 4
print("PASS: Databricks source import, mixed Python/SQL execution, and Jupyter export", flush=True)

scheduled = notebook("Scheduled check " + suffix, [("python", "print('scheduled execution succeeded')")])
job = request("POST", "/jobs", json={"name": "Minute check " + suffix, "notebook_id": scheduled["id"], "cron": "* * * * *", "timezone": "America/New_York", "enabled": True})
request("POST", f"/jobs/{job['id']}/run", json={})
deadline = time.monotonic() + 80
while time.monotonic() < deadline:
    scheduled_runs = [r for r in request("GET", "/runs") if r.get("job_id") == job["id"] and r.get("trigger") == "schedule" and r["status"] == "succeeded"]
    if scheduled_runs:
        break
    time.sleep(1)
else:
    raise AssertionError("A real cron-triggered run did not execute within 80 seconds")
print("PASS: real clock-triggered notebook execution with an explicit IANA timezone", flush=True)

repo = request("POST", "/repositories", json={"name": "PyPA integration " + suffix, "url": "https://github.com/pypa/sampleproject.git", "branch": "main"})
assert repo["commit"]
request("POST", f"/repositories/{repo['id']}/pull", json={})
# Put a fixture beside the actual Git checkout to exercise discovery/import.
fixture = notebook("Git fixture staging", [("python", f"from pathlib import Path\nPath({repo['path']!r}, 'openbricks-check.py').write_text({source.decode()!r})")])
run(fixture["id"])
assert request("POST", f"/repositories/{repo['id']}/import", json={})
built = request("POST", "/packages/build", json={"repository_id": repo["id"], "path": "."})
assert any(p["name"] == "sampleproject" for p in built)
wheel = notebook("Wheel and Spark Python worker check", [("python",
    "import sample\nprint(sample.__name__)\nfrom pyspark.sql.functions import udf\nfrom pyspark.sql.types import StringType\ncheck = udf(lambda x: __import__('sample').__name__, StringType())\ndisplay(spark.range(1).select(check('id').alias('module')))"
)])
output = run(wheel["id"])
assert output["outputs"][0]["result"]["rows"] == [["sample"]]
print("PASS: real Git clone/pull, notebook discovery/import, offline wheel build/install, driver import, Spark Python UDF import", flush=True)

marker.write_text(json.dumps({"notebook_id": main["id"], "job_id": job["id"], "bucket": bucket, "table": table}, indent=2))
print("PASS: all integration checks. Recreate the container with this volume, then run --verify-persistence.", flush=True)
