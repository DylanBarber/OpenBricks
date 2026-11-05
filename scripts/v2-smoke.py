"""Real v2 integration check: run only against a disposable QA deployment.

Run this script inside the management container. It needs the configured
administrator credentials and a prebuilt examples/brickkit wheel under
/tmp/brickkit/dist. The sibling remote compute service defaults to
http://remote-compute:8001 and uses OPENBRICKS_COMPUTE_TOKEN for this test.

Fixtures are intentionally retained for QA browsing. No SQL Server is contacted
and no SQL Server integration success is claimed by this script.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx


parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:3000")
parser.add_argument("--remote-url", default=os.getenv("OPENBRICKS_SMOKE_REMOTE_URL", "http://remote-compute:8001"))
parser.add_argument("--wheel", default="/tmp/brickkit/dist", help="Wheel filename or directory containing a prebuilt fixture wheel")
parser.add_argument("--run-timeout", type=int, default=180)
args = parser.parse_args()
client = httpx.Client(base_url=args.base_url, timeout=httpx.Timeout(90, connect=10))
suffix = uuid4().hex[:8]
prefix = "QA v2 " + suffix
secret_values = [value for key, value in os.environ.items() if value and len(value) >= 4
                 and any(part in key for part in ("PASSWORD", "TOKEN", "SECRET", "ACCESS_KEY"))]


def safe_text(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for secret in secret_values:
        text = text.replace(secret, "[redacted]")
    return text[-3000:]


def ensure(condition, message, details=None):
    if not condition:
        raise AssertionError(safe_text(message + (": " + safe_text(details) if details is not None else "")))


def request(method, path, **kwargs):
    response = client.request(method, "/api" + path, **kwargs)
    ensure(response.is_success, f"{method} {path} returned {response.status_code}", response.text if not response.is_success else None)
    return response.json()


def expect_error(method, path, statuses, **kwargs):
    response = client.request(method, "/api" + path, **kwargs)
    ensure(response.status_code in statuses, f"{method} {path} should reject this operation", {"status": response.status_code, "body": response.text})


def notebook(label, cells, folder_id=None, cluster_id="local"):
    return request("POST", "/notebooks", json={"name": prefix + " " + label, "folder_id": folder_id, "cluster_id": cluster_id,
        "cells": [{"id": f"cell-{index}", "language": language, "source": source}
                  for index, (language, source) in enumerate(cells)]})


def start(notebook_id, **options):
    return request("POST", f"/notebooks/{notebook_id}/run", json=options)


def await_run(id, timeout=None):
    deadline = time.monotonic() + (timeout or args.run_timeout)
    while time.monotonic() < deadline:
        result = request("GET", f"/runs/{id}")
        if result["status"] not in ("queued", "running"):
            ensure(result["status"] == "succeeded", "Notebook execution failed", result)
            return result
        time.sleep(0.7)
    raise AssertionError("Notebook did not complete within the bounded integration deadline")


def run(notebook_id, **options):
    return await_run(start(notebook_id, **options)["id"])


def pass_stage(text):
    print("PASS: " + text, flush=True)


deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        if client.get("/api/health").status_code == 200:
            break
    except httpx.HTTPError:
        pass
    time.sleep(1)
else:
    raise AssertionError("Management UI did not become ready")
ensure(client.get("/").status_code == 200, "Management UI must respond through its production server")
ensure(client.get("/api/notebooks").status_code == 401, "Anonymous notebook requests must be rejected")
request("POST", "/auth/login", json={"username": os.environ.get("OPENBRICKS_ADMIN_USERNAME", "admin"), "password": os.environ["OPENBRICKS_ADMIN_PASSWORD"]})
ensure(request("GET", "/auth/me")["role"] == "admin", "Test account must be an administrator")
pass_stage("production UI proxy, cookie login, and protected workspace API")

root = request("POST", "/folders", json={"name": prefix})
pipeline = request("POST", "/folders", json={"name": "Pipelines", "parent_id": root["id"]})
archive = request("POST", "/folders", json={"name": "Archive", "parent_id": root["id"]})
expect_error("PUT", f"/folders/{root['id']}", {400}, json={"name": prefix, "parent_id": pipeline["id"]})
expect_error("POST", "/folders", {400}, json={"name": "../invalid"})
request("PUT", f"/folders/{pipeline['id']}", json={"name": "Pipelines", "parent_id": archive["id"]})
expect_error("DELETE", f"/folders/{root['id']}", {409})

local = notebook("Python and SQL", [
    ("python", "values = spark.createDataFrame([(20,), (22,)], ['value'])\nvalues.createOrReplaceTempView('v2_values')\ndisplay(values)"),
    ("sql", "SELECT SUM(value) AS answer FROM v2_values"),
    ("python", "answer = 42\nprint(answer)"),
], folder_id=root["id"])
request("PATCH", f"/notebooks/{local['id']}/location", json={"folder_id": pipeline["id"]})
expect_error("PATCH", f"/notebooks/{local['id']}/location", {400, 404}, json={"folder_id": "missing-folder"})
workspace = request("GET", "/workspace", params={"folder_id": pipeline["id"], "limit": 1})
ensure(workspace["items"][0]["id"] == local["id"], "Moved notebook must be listed in its destination")
ensure("cells" not in workspace["items"][0], "Workspace listing must return metadata rather than every cell")
ensure([item["id"] for item in workspace["breadcrumbs"]] == [root["id"], archive["id"], pipeline["id"]], "Nested breadcrumbs must follow folder moves", workspace["breadcrumbs"])
pass_stage("nested folders, cycle/name validation, notebook moves, and paginated workspace metadata")

local_status = request("POST", "/clusters/local/test")
ensure(local_status["ok"] and local_status["status"]["service"] == "openbricks-compute", "Bundled compute service must authenticate")
result = run(local["id"])
ensure(result["cluster_id"] == "local", "Notebook must be routed to its selected local compute")
ensure(result["outputs"][1]["result"]["rows"] == [[42]], "Real local Spark SQL should return 42", result)
cell = run(local["id"], cell_id="cell-2")
ensure(cell["outputs"][0]["stdout"].strip() == "42", "Selected-cell execution must retain the notebook scope")
history = request("GET", "/history/runs", params={"notebook_id": local["id"], "limit": 1, "offset": 0})
next_history = request("GET", "/history/runs", params={"notebook_id": local["id"], "limit": 1, "offset": 1})
ensure(history["total"] >= 2 and len(history["items"]) == 1, "Notebook run history must paginate")
ensure(history["items"][0]["id"] != next_history["items"][0]["id"], "Distinct run pages must not repeat the same entry")
ensure(all("outputs" not in item and not any(key.startswith("_") for key in item) for item in history["items"] + next_history["items"]), "Run summaries must not include cell output or internal notebook snapshots")
pass_stage("real Python/Spark SQL on the separate compute container, cell state, and bounded run-history pages")

basic_values = {"name": prefix + " Weekly", "notebook_id": local["id"], "cluster_id": "local", "schedule_mode": "basic",
                "basic_schedule": {"frequency": "weekly", "time": "08:15", "days_of_week": [1, 3, 5]},
                "timezone": "America/New_York", "enabled": True}
weekly = request("POST", "/jobs", json=basic_values)
ensure(weekly["cron"] == "15 8 * * 1,3,5" and weekly["schedule_mode"] == "basic", "Basic weekly scheduling must derive the expected cron", weekly)
next_time = datetime.fromisoformat(weekly["next_run_at"]).astimezone(ZoneInfo("America/New_York"))
ensure(next_time.hour == 8 and next_time.minute == 15 and next_time.weekday() in (0, 2, 4), "Weekly next-run time must honor selected days and timezone", weekly)
disabled = request("PUT", f"/jobs/{weekly['id']}", json={**basic_values, "enabled": False})
ensure(disabled["next_run_at"] is None and not disabled["enabled"], "Disabling a basic schedule must remove its next trigger")
job_run = request("POST", f"/jobs/{weekly['id']}/run", json={})
job_result = await_run(job_run["id"])
ensure(job_result["job_id"] == weekly["id"] and job_result["cluster_id"] == "local", "Run now must retain the job and cluster selection")
advanced = request("POST", "/jobs", json={"name": prefix + " Advanced", "notebook_id": local["id"], "schedule_mode": "advanced",
                "cron": "*/15 * * * *", "timezone": "UTC", "enabled": False})
ensure(advanced["cron"] == "*/15 * * * *" and advanced["basic_schedule"] is None, "Advanced schedules must preserve explicit cron syntax")
pass_stage("Basic weekly options, timezone-aware next trigger, disable/save, Run now, and Advanced cron mode")

saved = request("GET", "/connections")
s3 = next((item for item in saved if item["id"] == "local-seaweedfs"), None)
ensure(s3 is not None and s3["type"] == "s3", "Configure the bundled SeaweedFS connection before running this check")
ensure("_secrets" not in s3 and "secrets" not in s3, "Connection API must not return saved credentials")
ensure(request("POST", f"/connections/{s3['id']}/test")["ok"], "The actual SeaweedFS connection must succeed")
bucket = "qa-v2-" + suffix
request("POST", f"/connections/{s3['id']}/buckets", json={"name": bucket})
payloads = {"events/values.csv": b"value\n20\n22\n", "events/a.txt": b"first object", "events/b.txt": b"second object"}
for key, value in payloads.items():
    uploaded = request("POST", f"/connections/{s3['id']}/objects", data={"bucket": bucket, "key": key}, files={"file": (key.rsplit("/", 1)[-1], value)})
    ensure(uploaded["size"] == len(value), "Uploaded S3 size must match its content")
    downloaded = client.get(f"/api/connections/{s3['id']}/download", params={"bucket": bucket, "key": key})
    ensure(downloaded.status_code == 200 and downloaded.content == value, "S3 download must reproduce the uploaded bytes")
top = request("GET", f"/connections/{s3['id']}/objects", params={"bucket": bucket, "limit": 1})
ensure("events/" in top["prefixes"], "S3 browser must expose directory-like prefixes")
objects, seen_tokens, token = [], set(), None
for page_index in range(10):
    parameters = {"bucket": bucket, "prefix": "events/", "limit": 1}
    if token:
        parameters["continuation_token"] = token
    page = request("GET", f"/connections/{s3['id']}/objects", params=parameters)
    ensure(len(page["items"]) <= 1, "S3 listing must honor page size")
    objects.extend(item["key"] for item in page["items"])
    token = page.get("next_token")
    if page_index == 0:
        ensure(bool(token), "Multiple S3 objects must produce a continuation token with page size one")
    if not token:
        break
    ensure(token not in seen_tokens, "S3 pagination must advance its continuation token")
    seen_tokens.add(token)
else:
    raise AssertionError("S3 listing exceeded its bounded pagination deadline")
ensure(set(objects) == set(payloads) and len(objects) == len(payloads), "S3 pages must include each uploaded object exactly once", objects)
pass_stage("real SeaweedFS test, bucket creation, uploads/downloads, prefixes, and continuation-token pagination")

token = os.environ["OPENBRICKS_COMPUTE_TOKEN"]
remote = request("POST", "/clusters", json={"name": prefix + " Remote", "url": args.remote_url,
                "token": token, "verify_tls": True, "allow_insecure": args.remote_url.startswith("http://")})
ensure("_token" not in remote and "token" not in remote and remote["has_token"], "Cluster API must mask authentication tokens")
remote_status = request("POST", f"/clusters/{remote['id']}/test")
ensure(remote_status["ok"] and remote_status["status"]["service"] == "openbricks-compute", "Remote compute must authenticate and identify itself")
ensure(remote_status["status"]["hostname"] != local_status["status"]["hostname"], "Local and remote targets must be different actual compute containers")

s3_cells = [
    ("python", f"storage = connections.s3('local-seaweedfs')\nfilename = storage.path('s3://{bucket}/events/values.csv')\nframe = spark.read.option('header', True).option('inferSchema', True).csv(filename)\nframe.createOrReplaceTempView('v2_s3_values')\nprint('staged S3 object on this compute host')"),
    ("sql", "SELECT SUM(value) AS answer FROM v2_s3_values"),
]
local_s3 = notebook("S3 local", s3_cells, pipeline["id"], "local")
remote_s3 = notebook("S3 remote", s3_cells, pipeline["id"], remote["id"])
ensure(run(local_s3["id"])["outputs"][1]["result"]["rows"] == [[42]], "Local compute must read the saved S3 connection")
remote_result = run(remote_s3["id"])
ensure(remote_result["cluster_id"] == remote["id"] and remote_result["cluster_name"] == remote["name"], "Remote notebook history must retain its actual compute target")
ensure(remote_result["outputs"][1]["result"]["rows"] == [[42]], "Remote compute must stage the shared S3 object and execute Spark SQL", remote_result)
pass_stage("remote cluster registration/test, real remote Spark SQL, and named S3 access on both compute services")

wheel_path = Path(args.wheel)
if wheel_path.is_dir():
    matches = sorted(wheel_path.glob("*.whl"))
    ensure(bool(matches), "Build examples/brickkit into the supplied wheel directory before running this check")
    wheel_path = matches[-1]
ensure(wheel_path.is_file() and wheel_path.suffix == ".whl", "A prebuilt brickkit wheel is required")
with wheel_path.open("rb") as stream:
    request("POST", "/packages/upload", files={"file": (wheel_path.name, stream, "application/octet-stream")})
wheel_notebook = notebook("Wheel remote", [("python",
    "from brickkit import normalize_name\nprint(normalize_name('  ada  '))\nfrom pyspark.sql.functions import udf\nfrom pyspark.sql.types import StringType\nclean = udf(lambda value: __import__('brickkit').normalize_name(value), StringType())\ndisplay(spark.createDataFrame([('  ada  ',)], ['name']).select(clean('name').alias('normalized')))"
)], pipeline["id"], remote["id"])
wheel_result = run(wheel_notebook["id"])
ensure(wheel_result["outputs"][0]["stdout"].strip() == "Ada", "Remote driver must import the replicated wheel")
ensure(wheel_result["outputs"][0]["result"]["rows"] == [["Ada"]], "Remote Spark Python worker must import the replicated wheel", wheel_result)
pass_stage("management wheel upload, automatic remote artifact transfer, driver import, and Spark Python UDF import")

sleep_source = "import time\nprint('started')\ntime.sleep(5)\nprint('finished')"
concurrent_local = notebook("Concurrent local", [("python", sleep_source)], pipeline["id"], "local")
concurrent_remote = notebook("Concurrent remote", [("python", sleep_source)], pipeline["id"], remote["id"])
first = start(concurrent_local["id"])
second = start(concurrent_remote["id"])
first_done, second_done = await_run(first["id"], timeout=45), await_run(second["id"], timeout=45)
first_start, second_start = datetime.fromisoformat(first_done["started_at"]), datetime.fromisoformat(second_done["started_at"])
first_end, second_end = datetime.fromisoformat(first_done["finished_at"]), datetime.fromisoformat(second_done["finished_at"])
ensure(max(first_start, second_start) < min(first_end, second_end), "Distinct compute services must execute overlapping notebooks rather than one global serial queue")
pass_stage("local and remote compute run concurrently with overlapping execution timestamps")

grouped = request("GET", "/history/notebooks", params={"search": prefix, "limit": 1, "offset": 0})
next_group = request("GET", "/history/notebooks", params={"search": prefix, "limit": 1, "offset": 1})
ensure(grouped["total"] >= 6 and len(grouped["items"]) == 1, "Run history must group notebooks and paginate groups")
ensure(grouped["items"][0]["notebook_id"] != next_group["items"][0]["notebook_id"], "Notebook history pages must not repeat a group")
ensure(all("outputs" not in item for item in grouped["items"] + next_group["items"]), "Grouped run history must not embed output payloads")
overview = request("GET", "/overview")
ensure(len(overview["runs"]) <= 8 and all("outputs" not in item for item in overview["runs"]), "Dashboard history must remain bounded and contain metadata only")
pass_stage("notebook-grouped history, group pagination, and bounded dashboard history at the end of the complete flow")

marker = Path(os.getenv("DATA_DIR", "/data")) / "v2-smoke.json"
marker.write_text(json.dumps({"prefix": prefix, "folder_id": pipeline["id"], "local_notebook_id": local["id"],
    "remote_notebook_id": remote_s3["id"], "wheel_notebook_id": wheel_notebook["id"], "cluster_id": remote["id"],
    "job_id": weekly["id"], "advanced_job_id": advanced["id"], "bucket": bucket}, indent=2))
pass_stage("all v2 checks complete; QA fixtures retained for browser inspection")
