import json
import threading
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from openbricks import compute_client
from openbricks.clusters import cluster_public, register_cluster_routes, validate_cluster_url
from openbricks.compute import create_compute_app
from openbricks.credentials import SecretBox
from openbricks.runtime import Runtime
from openbricks.store import Store


TOKEN = "compute-test-token-that-is-at-least-32-characters"


def notebook(id="notebook", source="print(42)"):
    return {"id": id, "name": "Cluster test", "cells": [{"id": "one", "language": "python", "source": source}]}


def wait_run(store, id, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = store.get("runs", id)
        if result["status"] not in ("queued", "running"):
            return result
        time.sleep(0.03)
    pytest.fail("Cluster run did not finish")


def save_cluster(store, id="remote", url="https://compute.example"):
    return store.put("clusters", {"id": id, "name": id.title(), "url": url, "verify_tls": True,
                                   "allow_insecure": False, "built_in": False, "_shared_data": False,
                                   "_token": SecretBox(store.directory).encrypt(TOKEN)})


def test_compute_auth_idempotency_and_secret_snapshots(tmp_path):
    app = create_compute_app(tmp_path, TOKEN)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/status").status_code == 401
        assert client.get("/runs/missing").status_code == 401
        client.headers["Authorization"] = "Bearer " + TOKEN
        status = client.get("/status")
        assert status.status_code == 200 and status.json()["service"] == "openbricks-compute"
        payload = {"run_id": "idempotent", "notebook": notebook(), "connections": [{"name": "sensitive", "secrets": {"password": "must-not-persist"}}]}
        response = client.post("/runs", json=payload)
        assert response.status_code == 202, response.text
        assert "_cells" not in response.json()
        repeated = client.post("/runs", json=payload)
        assert repeated.status_code == 202 and repeated.json()["id"] == "idempotent"
        result = wait_run(app.state.store, "idempotent")
        assert result["status"] == "succeeded", result
        assert result["outputs"][0]["stdout"].strip() == "42"
        assert len(app.state.store.list("runs")) == 1
        with app.state.store.connect() as db:
            body = db.execute("SELECT body FROM documents WHERE kind='runs'").fetchone()[0]
        assert "must-not-persist" not in body
        assert app.state.store.path == tmp_path / "compute-state" / "openbricks.sqlite3"
        assert not (tmp_path / "openbricks.sqlite3").exists()


def test_compute_rejects_invalid_run_and_wheel(tmp_path):
    with TestClient(create_compute_app(tmp_path, TOKEN)) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        invalid = notebook()
        invalid["cells"][0]["language"] = "bash"
        assert client.post("/runs", json={"run_id": "bad", "notebook": invalid}).status_code == 400
        assert client.post("/environment/wheels", files={"file": ("bad.whl", b"not a zip")}).status_code == 400
        assert client.get("/environment/wheels").json() == {"items": []}


def test_cluster_crud_masks_tokens_and_rejects_insecure_urls(tmp_path):
    store = Store(tmp_path)
    app = FastAPI()
    app.state.store = store

    def user(request: Request):
        return {"role": request.headers.get("Role", "admin")}

    def admin(request: Request):
        value = user(request)
        if value["role"] != "admin":
            raise HTTPException(403, "Administrator access required")
        return value

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    register_cluster_routes(app, user, admin)
    with TestClient(app) as client:
        assert client.get("/api/clusters").json()[0]["id"] == "local"
        values = {"name": "Remote", "url": "http://remote:8001", "token": TOKEN}
        assert client.post("/api/clusters", json=values).status_code == 400
        assert client.post("/api/clusters", json={**values, "allow_insecure": True}, headers={"Role": "member"}).status_code == 403
        response = client.post("/api/clusters", json={**values, "allow_insecure": True})
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["has_token"] and "token" not in created and "_token" not in created
        assert TOKEN not in json.dumps(store.list("clusters"))
        id = created["id"]
        before = store.get("clusters", id)["_token"]
        changed = client.put(f"/api/clusters/{id}", json={**values, "name": "Renamed", "allow_insecure": True, "token": ""})
        assert changed.status_code == 200
        assert store.get("clusters", id)["_token"] == before
        store.put("notebooks", {"id": "assigned", "cluster_id": id})
        assert client.delete(f"/api/clusters/{id}").status_code == 409
        store.delete("notebooks", "assigned")
        assert client.delete(f"/api/clusters/{id}").status_code == 200
        assert client.delete("/api/clusters/local").status_code == 409
    for url in ("file:///tmp/x", "https://user:pass@example.com", "https://example.com?token=secret"):
        with pytest.raises(ValueError):
            validate_cluster_url(url)


def test_remote_routing_persists_results_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    store = Store(tmp_path)
    save_cluster(store)
    captured = []

    def handler(request):
        if request.method == "POST" and request.url.path == "/runs":
            captured.append(json.loads(request.content))
            return httpx.Response(202, json={"id": captured[-1]["run_id"], "status": "queued"})
        if request.method == "GET" and request.url.path.startswith("/runs/"):
            id = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": id, "status": "succeeded", "outputs": [{"cell_id": "one", "stdout": "42\n", "status": "succeeded"}], "duration_ms": 12})
        raise AssertionError(str(request.url))

    monkeypatch.setattr(compute_client, "client_for", lambda store, entry: httpx.Client(base_url=entry["url"], transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(compute_client, "connection_snapshot", lambda store: [{"id": "sql", "name": "warehouse", "secrets": {"password": "sensitive-sql-secret"}}])
    runtime = Runtime(store)
    runtime.start()
    try:
        run = runtime.submit(notebook(), cluster_id="remote")
        result = wait_run(store, run["id"])
        assert result["status"] == "succeeded" and result["cluster_id"] == "remote"
        assert captured[0]["connections"][0]["secrets"]["password"] == "sensitive-sql-secret"
        assert "sensitive-sql-secret" not in json.dumps(store.get("runs", run["id"]))
        assert TOKEN not in json.dumps(store.get("runs", run["id"]))
    finally:
        runtime.close()


def test_different_clusters_execute_concurrently(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    store = Store(tmp_path)
    save_cluster(store, "first", "https://first.example")
    save_cluster(store, "second", "https://second.example")
    barrier = threading.Barrier(2)

    def handler(request):
        if request.method == "POST":
            value = json.loads(request.content)
            barrier.wait(timeout=3)
            return httpx.Response(202, json={"id": value["run_id"], "status": "queued"})
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1], "status": "succeeded", "outputs": []})

    monkeypatch.setattr(compute_client, "client_for", lambda store, entry: httpx.Client(base_url=entry["url"], transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(compute_client, "connection_snapshot", lambda store: [])
    runtime = Runtime(store)
    runtime.start()
    try:
        first = runtime.submit(notebook("first"), cluster_id="first")
        second = runtime.submit(notebook("second"), cluster_id="second")
        assert wait_run(store, first["id"])["status"] == "succeeded"
        assert wait_run(store, second["id"])["status"] == "succeeded"
    finally:
        runtime.close()


def test_remote_connection_failure_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    store = Store(tmp_path)
    save_cluster(store)

    def handler(request):
        raise httpx.ConnectError("sensitive network details", request=request)

    monkeypatch.setattr(compute_client, "client_for", lambda store, entry: httpx.Client(base_url=entry["url"], transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(compute_client, "connection_snapshot", lambda store: [])
    runtime = Runtime(store)
    runtime.start()
    try:
        run = runtime.submit(notebook(), cluster_id="remote")
        result = wait_run(store, run["id"])
        assert result["status"] == "failed"
        assert "sensitive network details" not in result["error"]
        assert "connectivity" in result["error"]
    finally:
        runtime.close()


def test_management_restart_reattaches_idempotent_remote_run(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    store = Store(tmp_path)
    save_cluster(store)
    id = "existing-remote-run"
    store.put("runs", {"id": id, "notebook_id": "saved", "notebook_name": "Saved", "cluster_id": "remote", "cluster_name": "Remote",
                       "trigger": "manual", "status": "running", "outputs": [], "_notebook": notebook("saved"), "_remote_submitted": True})
    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(json.loads(request.content)["run_id"])
            return httpx.Response(202, json={"id": id, "status": "succeeded"})
        return httpx.Response(200, json={"id": id, "status": "succeeded", "outputs": []})

    monkeypatch.setattr(compute_client, "client_for", lambda store, entry: httpx.Client(base_url=entry["url"], transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(compute_client, "connection_snapshot", lambda store: [])
    runtime = Runtime(store)
    runtime.start()
    try:
        assert wait_run(store, id)["status"] == "succeeded"
        assert posted == []
    finally:
        runtime.close()


def test_lost_submission_acknowledgement_reuses_id_and_poll_reconnects(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    store = Store(tmp_path)
    save_cluster(store)
    executions = set()
    post_attempts = []
    polls = []

    def handler(request):
        if request.method == "POST":
            id = json.loads(request.content)["run_id"]
            executions.add(id)
            post_attempts.append(id)
            if len(post_attempts) == 1:
                raise httpx.ReadTimeout("response lost after acceptance", request=request)
            return httpx.Response(202, json={"id": id, "status": "queued"})
        id = request.url.path.rsplit("/", 1)[1]
        polls.append(id)
        if len(polls) == 1:
            raise httpx.ConnectError("temporary disconnect", request=request)
        return httpx.Response(200, json={"id": id, "status": "succeeded", "outputs": []})

    monkeypatch.setattr(compute_client, "client_for", lambda store, entry: httpx.Client(base_url=entry["url"], transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(compute_client, "connection_snapshot", lambda store: [])
    runtime = Runtime(store)
    runtime.start()
    try:
        run = runtime.submit(notebook(), cluster_id="remote")
        result = wait_run(store, run["id"])
        assert result["status"] == "succeeded" and result["error"] is None
        assert len(executions) == 1 and post_attempts == [run["id"], run["id"]]
        assert len(polls) == 2
    finally:
        runtime.close()
