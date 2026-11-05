"""Authenticated OpenBricks compute service, deployable on a separate host."""
from __future__ import annotations

import hashlib
import hmac
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile
from contextlib import asynccontextmanager
from email.parser import Parser
from pathlib import Path
from uuid import uuid4

import psutil
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .runtime import LocalRuntime, RuntimeBusy, now
from . import __version__
from .security import confined_path
from .store import Store


def public(value):
    if isinstance(value, list):
        return [public(item) for item in value]
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if not key.startswith("_")}
    return value


class RunPayload(BaseModel):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    notebook: dict
    cell_id: str | None = None
    trigger: str = Field(default="manual", max_length=30)
    job_id: str | None = Field(default=None, max_length=80)
    connections: list[dict] = Field(default_factory=list, max_length=256)
    parameters: dict = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    environment: dict | None = None
    provenance: dict = Field(default_factory=dict)


class ComputeStore(Store):
    def __init__(self, directory):
        # Only artifact/data files are shared with the control plane. Compute
        # owns a separate run database and never marks control-plane runs.
        super().__init__(directory / "compute-state")
        self.directory = directory.resolve()


def validate_notebook(notebook):
    if not isinstance(notebook.get("id"), str) or not 1 <= len(notebook["id"]) <= 80:
        raise ValueError("Notebook identifier is required")
    if not isinstance(notebook.get("name"), str) or not notebook["name"].strip() or len(notebook["name"]) > 200:
        raise ValueError("Notebook name is required")
    cells = notebook.get("cells")
    if not isinstance(cells, list) or len(cells) > 500:
        raise ValueError("Notebook must contain at most 500 cells")
    identifiers = set()
    total = 0
    for cell in cells:
        if (not isinstance(cell, dict) or not isinstance(cell.get("id"), str) or not 1 <= len(cell["id"]) <= 80
                or cell["id"] in identifiers or cell.get("language") not in ("python", "sql", "markdown")
                or not isinstance(cell.get("source"), str) or len(cell["source"]) > 1_000_000):
            raise ValueError("Notebook contains an invalid cell")
        identifiers.add(cell["id"])
        total += len(cell["source"])
    if total > 10_000_000:
        raise ValueError("Notebook source exceeds 10 MB")
    return notebook


def memory_metrics():
    values = psutil.virtual_memory()
    total, used = values.total, values.used
    try:
        maximum = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if maximum != "max":
            total = int(maximum)
            used = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
    except (OSError, ValueError):
        pass
    return total, used


def compute_status(app):
    runtime = app.state.runtime
    total, used = memory_metrics()
    disk = shutil.disk_usage(app.state.store.directory)
    try:
        spark_version = importlib.metadata.version("pyspark")
    except importlib.metadata.PackageNotFoundError:
        spark_version = "not installed"
    return {"service": "openbricks-compute", "version": __version__, "status": "busy" if runtime.active_id else "idle",
            "hostname": platform.node(), "cpu_count": os.cpu_count() or 1, "memory_total": total, "memory_used": used,
            "disk_total": disk.total, "disk_used": disk.used, "spark_version": spark_version, "python_version": platform.python_version(),
            "active_runs": int(bool(runtime.active_id)), "queued_runs": runtime.queue.qsize(),
            "master": os.getenv("SPARK_MASTER", "local[2]"), "spark_driver_memory": os.getenv("SPARK_DRIVER_MEMORY", "1g"),
            "idle_timeout_seconds": runtime.idle_timeout,
            "uptime_seconds": int(time.monotonic() - app.state.started),
            "capabilities": ["python", "spark-sql", "wheels", "connectors"],
            "storage_scope": "shared-local" if os.getenv("OPENBRICKS_SHARED_DATA", "false").lower() == "true" else "compute-local"}


def install_compute_wheel(app, filename, content):
    if not re.fullmatch(r"[a-zA-Z0-9_.+-]+\.whl", filename):
        raise ValueError("Upload a Python .whl package")
    root = app.state.store.directory
    path = root / "wheels" / filename
    digest = hashlib.sha256(content).hexdigest()
    existing = app.state.store.get("wheel_artifacts", filename)
    if existing and existing["sha256"] == digest and path.is_file():
        return public(existing)
    # Concurrent uploads must never share a temporary archive path.
    temporary = path.with_suffix(f".{uuid4().hex}.upload")
    temporary.write_bytes(content)
    try:
        with zipfile.ZipFile(temporary) as archive:
            members = archive.infolist()
            metadata_files = [item.filename for item in members if item.filename.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1:
                raise ValueError("Wheel must contain exactly one package metadata file")
            if sum(item.file_size for item in members) > 500 * 1024 * 1024:
                raise ValueError("Expanded wheel exceeds 500 MB")
            for member in members:
                confined_path(root / "site-packages", member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Wheel cannot contain symbolic links")
            metadata = Parser().parsestr(archive.read(metadata_files[0]).decode("utf-8"))
        with app.state.runtime.environment_change():
            app.state.runtime.reset()
            temporary.replace(path)
            result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--upgrade", "--target", str(root / "site-packages"), str(path)], capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise ValueError("Compute could not install this wheel. Verify its Python/platform compatibility and dependencies")
        entry = {"id": filename, "filename": filename, "name": metadata.get("Name", filename), "version": metadata.get("Version", "unknown"),
                 "sha256": digest, "size": len(content), "installed_at": now()}
        app.state.store.put("wheel_artifacts", entry)
        return public(entry)
    except zipfile.BadZipFile:
        raise ValueError("Invalid wheel archive")
    finally:
        temporary.unlink(missing_ok=True)


def create_compute_app(data_dir=None, token=None):
    @asynccontextmanager
    async def lifespan(app):
        secret = token if token is not None else os.getenv("OPENBRICKS_COMPUTE_TOKEN", "")
        if len(secret) < 32:
            raise RuntimeError("Set OPENBRICKS_COMPUTE_TOKEN to at least 32 characters")
        app.state.token = secret
        directory = Path(data_dir or os.getenv("DATA_DIR", "./data")).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("buckets", "wheels", "site-packages", "repositories", "warehouse", "logs"):
            (directory / name).mkdir(exist_ok=True)
        app.state.store = ComputeStore(directory)
        app.state.runtime = LocalRuntime(app.state.store)
        app.state.started = time.monotonic()
        app.state.runtime.start()
        try:
            yield
        finally:
            app.state.runtime.close()

    app = FastAPI(title="OpenBricks compute", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authenticated(request: Request):
        authorization = request.headers.get("authorization", "")
        provided = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not provided or not hmac.compare_digest(provided, request.app.state.token):
            raise HTTPException(401, "Compute authentication required")

    @app.middleware("http")
    async def request_limits(request: Request, call_next):
        length = request.headers.get("content-length")
        if length:
            try:
                limit = int(os.getenv("OPENBRICKS_MAX_UPLOAD_MB", "100")) * 1024 * 1024 + 1024 * 1024
                if int(length) > limit:
                    return JSONResponse({"detail": "Request exceeds upload limit"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RuntimeBusy)
    async def runtime_busy(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/status", dependencies=[Depends(authenticated)])
    def status(request: Request):
        return compute_status(request.app)

    @app.post("/runs", status_code=202, dependencies=[Depends(authenticated)])
    def submit(body: RunPayload, request: Request):
        notebook = validate_notebook(body.notebook)
        return public(request.app.state.runtime.submit(notebook, body.cell_id, body.trigger, body.job_id,
            run_id=body.run_id, connections=body.connections, parameters=body.parameters, timeout_seconds=body.timeout_seconds,
            environment=body.environment, provenance=body.provenance))

    @app.get("/runs/{id}", dependencies=[Depends(authenticated)])
    def run(id: str, request: Request):
        value = request.app.state.store.get("runs", id)
        if not value:
            raise HTTPException(404, "Compute run not found")
        return public(value)

    @app.post("/runs/{id}/cancel", dependencies=[Depends(authenticated)])
    def cancel(id: str, request: Request):
        if not request.app.state.store.get("runs", id):
            raise HTTPException(404, "Compute run not found")
        return public(request.app.state.runtime.cancel(id))

    @app.post("/environment/reset", dependencies=[Depends(authenticated)])
    def reset(request: Request):
        with request.app.state.runtime.environment_change():
            request.app.state.runtime.reset()
        return {"ok": True}

    @app.get("/environment/wheels", dependencies=[Depends(authenticated)])
    def wheels(request: Request):
        return {"items": public(request.app.state.store.list("wheel_artifacts"))}

    @app.post("/environment/wheels", status_code=201, dependencies=[Depends(authenticated)])
    async def upload_wheel(request: Request, file: UploadFile = File(...)):
        limit = int(os.getenv("OPENBRICKS_MAX_UPLOAD_MB", "100")) * 1024 * 1024
        content = bytearray()
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > limit:
                raise HTTPException(413, "Wheel exceeds upload limit")
        return await run_in_threadpool(install_compute_wheel, request.app, file.filename or "", bytes(content))

    @app.post("/artifacts/wheels", status_code=201, dependencies=[Depends(authenticated)])
    async def artifact_wheel(request: Request, sha256: str = Form(...), file: UploadFile = File(...)):
        from .environments import save_artifact
        limit = int(os.getenv("OPENBRICKS_MAX_UPLOAD_MB", "100")) * 1024 * 1024
        content = bytearray()
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > limit:
                raise HTTPException(413, "Wheel exceeds upload limit")
        return await run_in_threadpool(save_artifact, request.app.state.store.directory, file.filename or "", bytes(content), sha256)

    return app


app = create_compute_app()
