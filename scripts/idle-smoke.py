"""Run inside a disposable compute container with a short idle timeout."""
import os
import time
from uuid import uuid4

import httpx
import psutil


client = httpx.Client(base_url="http://127.0.0.1:8001", timeout=15,
                     headers={"Authorization": "Bearer " + os.environ["OPENBRICKS_COMPUTE_TOKEN"]})
deadline = time.monotonic() + 30
while True:
    try:
        client.get("/health").raise_for_status()
        break
    except httpx.HTTPError:
        assert time.monotonic() < deadline, "Compute did not become healthy"
        time.sleep(0.3)

run_id = uuid4().hex
response = client.post("/runs", json={"run_id": run_id, "notebook": {
    "id": "idle-spark-check", "name": "Idle Spark check", "cells": [
        {"id": "one", "language": "sql", "source": "SELECT 42 AS answer"}]}})
response.raise_for_status()
deadline = time.monotonic() + 120
while True:
    response = client.get("/runs/" + run_id)
    response.raise_for_status()
    run = response.json()
    if run["status"] not in ("queued", "running"):
        break
    assert time.monotonic() < deadline, "Spark execution timed out"
    time.sleep(0.3)
assert run["status"] == "succeeded", run.get("error")
assert run["outputs"][0]["result"]["rows"] == [[42]]
print("PASS: real Spark SQL returned 42", flush=True)


def java_processes():
    return [process for process in psutil.process_iter(["name", "status"])
            if process.info["name"] == "java" and process.info["status"] != psutil.STATUS_ZOMBIE]


assert java_processes(), "Expected a running Spark JVM before idle expiration"
deadline = time.monotonic() + 20
while java_processes() and time.monotonic() < deadline:
    time.sleep(0.3)
assert not java_processes(), "Spark JVM survived the idle shutdown"
status = client.get("/status").json()
assert status["idle_timeout_seconds"] == 3
print("PASS: idle shutdown terminated the Spark JVM automatically", flush=True)
