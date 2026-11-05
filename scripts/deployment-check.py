"""Deployment connection/preservation check using management's existing credentials."""
import os
import time

import httpx
import pyodbc


client = httpx.Client(base_url="http://127.0.0.1:3000", timeout=30)
deadline = time.monotonic() + 60
while True:
    try:
        client.get("/api/health").raise_for_status()
        break
    except httpx.HTTPError:
        assert time.monotonic() < deadline, "Management did not become healthy"
        time.sleep(0.5)

assert client.get("/api/workspace").status_code == 401
client.post("/api/auth/login", json={"username": os.environ.get("OPENBRICKS_ADMIN_USERNAME", "admin"),
                                   "password": os.environ["OPENBRICKS_ADMIN_PASSWORD"]}).raise_for_status()


def api(method, path):
    response = client.request(method, "/api" + path)
    response.raise_for_status()
    return response.json()


notebooks = api("GET", "/workspace")
runs = api("GET", "/history/runs")
print(f"Workspace contains {notebooks['total']} notebooks and {runs['total']} run records", flush=True)
expected_notebooks = os.environ.get("CHECK_EXPECTED_NOTEBOOKS")
expected_runs = os.environ.get("CHECK_EXPECTED_RUNS")
if expected_notebooks:
    assert notebooks["total"] == int(expected_notebooks), "Notebook count changed during upgrade"
if expected_runs:
    assert runs["total"] == int(expected_runs), "Run history count changed during upgrade"

cluster = api("POST", "/clusters/local/test")
assert cluster["ok"] and cluster["status"]["idle_timeout_seconds"] == 900
print("PASS: authenticated compute, 15-minute idle shutdown configured", flush=True)
if os.environ.get("OPENBRICKS_S3_ENDPOINT"):
    connections = api("GET", "/connections")
    assert any(item["id"] == "local-seaweedfs" for item in connections)
    assert api("POST", "/connections/local-seaweedfs/test")["ok"]
    print("PASS: local SeaweedFS connection", flush=True)
if os.environ.get("OPENBRICKS_CATALOG_URL"):
    deadline = time.monotonic() + 60
    while not api("GET", "/catalog/status")["available"]:
        assert time.monotonic() < deadline, "Catalog did not become available"
        time.sleep(0.5)
    api("GET", "/catalog/catalogs")
    print("PASS: optional Unity Catalog API", flush=True)
assert "ODBC Driver 18 for SQL Server" in pyodbc.drivers()
print("PASS: Microsoft SQL Server ODBC Driver 18 installed", flush=True)
