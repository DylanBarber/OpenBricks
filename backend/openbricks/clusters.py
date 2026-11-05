"""Saved compute-service endpoints, with encrypted authentication tokens."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .credentials import SecretBox


class RuntimeBusy(ValueError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class ClusterInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2000)
    token: str = Field(default="", max_length=4096)
    verify_tls: bool = True
    allow_insecure: bool = False


def validate_cluster_url(url, *, allow_insecure=False):
    parsed = urlsplit(url.strip())
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Use an HTTP(S) compute-service URL without credentials, query parameters, or fragments")
    try:
        parsed.port
    except ValueError:
        raise ValueError("Invalid compute-service port")
    if parsed.scheme == "http" and not allow_insecure:
        raise ValueError("Use HTTPS, or explicitly allow unencrypted HTTP for a trusted local network")
    return url.strip().rstrip("/")


def ensure_local_cluster(store):
    url = os.getenv("OPENBRICKS_COMPUTE_URL", "").strip()
    if not url:
        return {"id": "local", "name": "Local compute", "url": None, "verify_tls": True,
                "allow_insecure": False, "built_in": True, "_shared_data": True}
    token = os.getenv("OPENBRICKS_COMPUTE_TOKEN", "")
    if len(token) < 32:
        raise ValueError("OPENBRICKS_COMPUTE_TOKEN must contain at least 32 characters")
    previous = store.get("clusters", "local") or {}
    # The environment remains the authority for the bundled endpoint and token.
    entry = {**previous, "id": "local", "name": "Local compute", "url": validate_cluster_url(url, allow_insecure=True),
             "verify_tls": os.getenv("OPENBRICKS_COMPUTE_VERIFY_TLS", "true").lower() == "true",
             "allow_insecure": url.startswith("http://"), "built_in": True,
             "_shared_data": os.getenv("OPENBRICKS_COMPUTE_SHARED_DATA", "true").lower() == "true",
             "_token": SecretBox(store.directory).encrypt(token),
             "created_at": previous.get("created_at", timestamp()), "updated_at": timestamp()}
    store.put("clusters", entry)
    return entry


def cluster_record(store, cluster_id):
    if cluster_id in (None, "", "local"):
        return store.get("clusters", "local") or ensure_local_cluster(store)
    entry = store.get("clusters", cluster_id)
    if not entry:
        raise ValueError("Compute cluster not found")
    return entry


def cluster_public(entry):
    return {**{key: value for key, value in entry.items() if not key.startswith("_")},
            "has_token": bool(entry.get("_token"))}


def all_clusters(store):
    entries = store.list("clusters")
    if not any(entry["id"] == "local" for entry in entries):
        entries.insert(0, ensure_local_cluster(store))
    return sorted(entries, key=lambda entry: (not entry.get("built_in"), entry["name"].lower()))


def client_for(store, entry):
    if not entry.get("url"):
        raise ValueError("This workspace uses embedded local compute")
    token = SecretBox(store.directory).decrypt(entry["_token"])
    return httpx.Client(base_url=entry["url"], headers={"Authorization": "Bearer " + token},
                        verify=entry.get("verify_tls", True), timeout=httpx.Timeout(20, connect=5), follow_redirects=False)


def active_cluster_runs(store, cluster_id):
    with store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM documents WHERE kind='runs' AND status IN ('queued','running') AND COALESCE(json_extract(body,'$.cluster_id'),'local')=?", (cluster_id,)).fetchone()[0]


def status_for(store, entry):
    if not entry.get("url"):
        return {"status": "idle", "master": os.getenv("SPARK_MASTER", "local[2]"), "storage_scope": "shared-local",
                "capabilities": ["python", "spark-sql", "wheels", "connectors"], "embedded": True}
    try:
        with client_for(store, entry) as client:
            response = client.get("/status")
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict) or value.get("service") != "openbricks-compute":
                raise ValueError("Endpoint did not identify as OpenBricks compute")
    except (httpx.HTTPError, ValueError, KeyError):
        raise ValueError("Cannot reach or authenticate the compute service. Check its URL, token, TLS settings, and network access")
    return value


def register_cluster_routes(app, authenticated, admin):
    @app.get("/api/clusters")
    def list_clusters(request: Request, user=Depends(authenticated)):
        return [cluster_public(entry) for entry in all_clusters(request.app.state.store)]

    @app.post("/api/clusters", status_code=201)
    def create_cluster(body: ClusterInput, request: Request, user=Depends(admin)):
        name = body.name.strip()
        if not name:
            raise ValueError("Cluster name is required")
        if len(body.token) < 32:
            raise ValueError("Compute token must contain at least 32 characters")
        store = request.app.state.store
        entry = {"id": uuid4().hex, "name": name, "url": validate_cluster_url(body.url, allow_insecure=body.allow_insecure),
                 "verify_tls": body.verify_tls, "allow_insecure": body.allow_insecure, "built_in": False,
                 "_shared_data": False, "_token": SecretBox(store.directory).encrypt(body.token),
                 "created_at": timestamp(), "updated_at": timestamp()}
        store.put("clusters", entry)
        return cluster_public(entry)

    @app.put("/api/clusters/{id}")
    def update_cluster(id: str, body: ClusterInput, request: Request, user=Depends(admin)):
        store = request.app.state.store
        entry = cluster_record(store, id)
        if entry.get("built_in"):
            raise HTTPException(409, "The bundled cluster is configured through the deployment environment")
        if active_cluster_runs(store, id):
            raise HTTPException(409, "Wait for this cluster's queued and running notebooks before changing its connection")
        if not body.name.strip():
            raise ValueError("Cluster name is required")
        if body.token and len(body.token) < 32:
            raise ValueError("Compute token must contain at least 32 characters")
        entry.update(name=body.name.strip(), url=validate_cluster_url(body.url, allow_insecure=body.allow_insecure),
                     verify_tls=body.verify_tls, allow_insecure=body.allow_insecure, updated_at=timestamp())
        if body.token:
            entry["_token"] = SecretBox(store.directory).encrypt(body.token)
        entry.pop("last_status", None)
        entry.pop("last_test_at", None)
        store.put("clusters", entry)
        return cluster_public(entry)

    @app.delete("/api/clusters/{id}")
    def delete_cluster(id: str, request: Request, user=Depends(admin)):
        store = request.app.state.store
        entry = cluster_record(store, id)
        if entry.get("built_in"):
            raise HTTPException(409, "The bundled compute cluster cannot be deleted")
        if active_cluster_runs(store, id) or any(item.get("cluster_id") == id for kind in ("jobs", "notebooks") for item in store.list(kind)):
            raise HTTPException(409, "Move notebooks and schedules off this cluster and wait for active runs before deleting it")
        store.delete("clusters", id)
        return {"ok": True}

    @app.post("/api/clusters/{id}/test")
    def test_cluster(id: str, request: Request, user=Depends(admin)):
        store = request.app.state.store
        entry = cluster_record(store, id)
        status = status_for(store, entry)
        entry.update(last_test_at=timestamp(), last_status=status)
        if entry.get("url") or not entry.get("built_in"):
            store.put("clusters", entry)
        return {"ok": True, "cluster_id": id, "status": status}
