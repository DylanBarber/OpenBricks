"""Git objects and wheel manifests captured as immutable execution releases."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .clusters import timestamp
from .environments import capture_environment, get_environment
from .notebooks import parse_notebook
from .parameters import normalize_definitions


def public(value):
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [public(item) for item in value]
    return value


def git_object(base, *args):
    result = subprocess.run(["git", "--no-replace-objects", "-c", "core.hooksPath=", "-C", str(base), *args], capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError("Git object could not be read; pull the repository and choose an existing exact commit")
    return result.stdout


def capture_release(store, body, user, access):
    project_id = body.get("project_id", "default")
    access.require_project(user, project_id, "edit")
    repository = store.get("repositories", body["repository_id"])
    if not repository or repository.get("project_id", "default") != project_id:
        raise ValueError("Repository was not found in this project")
    commit = body.get("commit", "").lower()
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
        raise ValueError("Use an exact full Git commit hash")
    base = store.directory / "repositories" / repository["id"]
    resolved = git_object(base, "rev-parse", "--verify", commit + "^{commit}").decode().strip()
    if resolved != commit:
        raise ValueError("Commit did not resolve to the requested immutable object")
    selected = body.get("paths")
    if selected is not None and (not isinstance(selected, list) or not 1 <= len(selected) <= 500):
        raise ValueError("Select 1 to 500 notebook paths")
    paths = set(selected or [])
    for path in paths:
        if not isinstance(path, str) or "\\" in path or PurePosixPath(path).is_absolute() or any(part in ("..", ".") for part in path.split("/")):
            raise ValueError("Notebook paths must be relative Git paths")
    connections = body.get("connection_ids", [])
    for identifier in connections:
        access.require_connection(user, identifier, project_id, "run")
    identifier = uuid4().hex
    definitions = body.get("parameter_definitions", {})
    notebooks, total, discovered = [], 0, set()
    tree = git_object(base, "ls-tree", "-r", "-z", "--full-tree", commit)
    if len(tree) > 10_000_000:
        raise ValueError("Repository tree is too large; use a smaller notebook repository")
    for row in tree.split(b"\0"):
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        mode, kind, _ = metadata.split()
        path = raw_path.decode("utf-8")
        if selected is not None and path not in paths:
            continue
        if kind != b"blob" or mode == b"120000" or any(part.startswith(".") or part in ("node_modules", "venv", "__pycache__") for part in PurePosixPath(path).parts):
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in (".py", ".ipynb", ".sql"):
            continue
        size = int(git_object(base, "cat-file", "-s", commit + ":" + path))
        if size > 10_000_000:
            raise ValueError("Release notebooks must each be at most 10 MB")
        content = git_object(base, "cat-file", "blob", commit + ":" + path)
        if suffix == ".py" and b"# Databricks notebook source" not in content[:100]:
            continue
        total += len(content)
        if total > 20_000_000 or len(notebooks) >= 500:
            raise ValueError("A release supports at most 500 notebooks and 20 MB of source")
        notebook = parse_notebook(PurePosixPath(path).name, content)
        notebook.update(id=hashlib.sha256((identifier + ":" + path).encode()).hexdigest(), project_id=project_id,
            connection_ids=connections, parameter_definitions=normalize_definitions(definitions.get(path, [])),
            repository_id=repository["id"], path=path)
        notebooks.append({"path": path, "notebook": notebook})
        discovered.add(path)
    if not notebooks or paths - discovered:
        raise ValueError("One or more selected paths are missing or are not supported notebooks")
    environment = get_environment(store, body["environment_id"], project_id) if body.get("environment_id") else capture_environment(store, project_id)
    notebooks.sort(key=lambda item: item["path"])
    identity = [{"path": item["path"], "cells": [{"language": cell["language"], "source": cell["source"]} for cell in item["notebook"]["cells"]],
        "parameter_definitions": item["notebook"]["parameter_definitions"]} for item in notebooks]
    source_hash = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    value = {"id": identifier, "name": body["name"], "project_id": project_id, "repository_id": repository["id"],
        "commit": commit, "notebooks": notebooks, "source_sha256": source_hash, "environment_id": environment["id"],
        "_environment": environment, "created_at": timestamp(), "created_by": user.get("id")}
    return store.put("releases", value)


class ReleaseInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    project_id: str = "default"
    repository_id: str
    commit: str
    paths: list[str] | None = Field(default=None, max_length=500)
    environment_id: str | None = None
    connection_ids: list[str] = Field(default_factory=list, max_length=256)
    parameter_definitions: dict = Field(default_factory=dict)


class EnvironmentInput(BaseModel):
    project_id: str = "default"
    name: str = Field(default="Captured environment", min_length=1, max_length=200)


class ReleaseRunInput(BaseModel):
    path: str
    parameters: dict = Field(default_factory=dict)
    cluster_id: str | None = None


class RerunInput(BaseModel):
    cluster_id: str | None = None


def register_release_routes(app, authenticated, access):
    @app.get("/api/environments")
    def environments(request: Request, project_id: str = "default", limit: int = Query(default=200, ge=1, le=200),
                     offset: int = Query(default=0, ge=0), user=Depends(authenticated)):
        access.require_project(user, project_id, "read")
        with request.app.state.store.connect() as db:
            rows = db.execute("SELECT body FROM documents WHERE kind='environments' AND COALESCE(project_id,'default')=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                              (project_id, limit, offset)).fetchall()
        entries = [json.loads(row[0]) for row in rows]
        return [{**entry, "id": entry["manifest_id"]} for entry in entries]

    @app.post("/api/environments", status_code=201)
    def environment(body: EnvironmentInput, request: Request, user=Depends(authenticated)):
        access.require_project(user, body.project_id, "edit")
        return capture_environment(request.app.state.store, body.project_id, body.name)

    @app.get("/api/environments/{id}")
    def environment_detail(id: str, request: Request, project_id: str = "default", user=Depends(authenticated)):
        access.require_project(user, project_id, "read")
        return get_environment(request.app.state.store, id, project_id)

    def lookup(request, identifier, user):
        value = request.app.state.store.get("releases", identifier)
        if not value:
            raise HTTPException(404, "Release not found")
        access.require_project(user, value.get("project_id", "default"), "read")
        return value

    @app.get("/api/releases")
    def releases(request: Request, project_id: str = "default", limit: int = Query(default=200, ge=1, le=200),
                 offset: int = Query(default=0, ge=0), user=Depends(authenticated)):
        access.require_project(user, project_id, "read")
        with request.app.state.store.connect() as db:
            rows = db.execute("SELECT json_remove(body,'$.notebooks','$._environment'), COALESCE(json_array_length(body,'$.notebooks'),0) FROM documents WHERE kind='releases' AND COALESCE(project_id,'default')=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                              (project_id, limit, offset)).fetchall()
        return [public(json.loads(row[0])) | {"notebook_count": row[1]} for row in rows]

    @app.post("/api/releases", status_code=201)
    def create(body: ReleaseInput, request: Request, user=Depends(authenticated)):
        return public(capture_release(request.app.state.store, body.model_dump(), user, access))

    @app.get("/api/releases/{id}")
    def detail(id: str, request: Request, user=Depends(authenticated)):
        return public(lookup(request, id, user))

    @app.get("/api/releases/{id}/compare")
    def compare(id: str, other_id: str, request: Request, user=Depends(authenticated)):
        first, second = lookup(request, id, user), lookup(request, other_id, user)
        def sources(release):
            return {item["path"]: {"cells": [(cell["language"], cell["source"]) for cell in item["notebook"]["cells"]],
                "parameter_definitions": item["notebook"].get("parameter_definitions", [])} for item in release["notebooks"]}
        left, right = sources(first), sources(second)
        return {"added": sorted(right.keys() - left.keys()), "removed": sorted(left.keys() - right.keys()),
            "changed": sorted(path for path in left.keys() & right.keys() if left[path] != right[path]),
            "environment_changed": first["environment_id"] != second["environment_id"]}

    def authorize_run(user, notebook, cluster_id=None):
        access.require_notebook(user, notebook, "run")
        if hasattr(access, "require_cluster"):
            access.require_cluster(user, cluster_id or notebook.get("cluster_id"), notebook.get("project_id", "default"), "run")
        for identifier in notebook.get("connection_ids", []):
            access.require_connection(user, identifier, notebook.get("project_id", "default"), "run")

    @app.post("/api/releases/{id}/run", status_code=202)
    def run_release(id: str, body: ReleaseRunInput, request: Request, user=Depends(authenticated)):
        release = lookup(request, id, user)
        notebook = next((item["notebook"] for item in release["notebooks"] if item["path"] == body.path), None)
        if not notebook:
            raise HTTPException(404, "Notebook path was not found in the release")
        authorize_run(user, notebook, body.cluster_id)
        return public(request.app.state.runtime.submit(notebook, parameters=body.parameters, cluster_id=body.cluster_id,
            environment=release["_environment"], provenance={"release_id": id, "commit": release["commit"], "path": body.path,
                "repository_id": release["repository_id"], "source_sha256": release["source_sha256"]}))

    @app.post("/api/runs/{id}/rerun", status_code=202)
    def rerun(id: str, body: RerunInput, request: Request, user=Depends(authenticated)):
        run = request.app.state.store.get("runs", id)
        if not run:
            raise HTTPException(404, "Run not found")
        access.require_project(user, run.get("project_id", "default"), "run")
        if run["status"] in ("queued", "running"):
            raise HTTPException(409, "Wait for the original run's outcome before starting a replacement")
        notebook, environment = run.get("_notebook"), run.get("_environment")
        if not notebook or not environment:
            raise HTTPException(409, "This legacy run has no immutable source/environment snapshot")
        notebook = {key: value for key, value in notebook.items() if key != "data_execution_id"}
        authorize_run(user, notebook, body.cluster_id or run.get("cluster_id"))
        return public(request.app.state.runtime.submit(notebook, cell_id=run.get("_cell_id"), parameters=run.get("parameters", {}),
            timeout_seconds=run.get("timeout_seconds"), cluster_id=body.cluster_id or run.get("cluster_id"),
            environment=environment, provenance={**run.get("provenance", {}), "rerun_of": id}))
