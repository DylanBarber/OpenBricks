from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.parser import Parser
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import psutil
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .notebooks import export_notebook, new_cell, parse_notebook
from .runtime import Runtime, RuntimeBusy, now
from .security import bucket_name, confined_path, password_hash, password_matches
from .store import Store

COOKIE = "openbricks_session"


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class UserCreate(Login):
    role: str = "member"


class Cell(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=80)
    language: str = "python"
    source: str = Field(default="", max_length=1_000_000)


class NotebookInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    language: str = "python"
    cells: list[Cell] | None = Field(default=None, max_length=500)
    folder_id: str | None = Field(default=None, max_length=80)
    cluster_id: str | None = Field(default=None, max_length=80)


class FolderInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: str | None = Field(default=None, max_length=80)


class NotebookLocation(BaseModel):
    folder_id: str | None = Field(default=None, max_length=80)
    cluster_id: str | None = Field(default=None, max_length=80)


class RunInput(BaseModel):
    cell_id: str | None = None
    cluster_id: str | None = Field(default=None, max_length=80)


class BasicSchedule(BaseModel):
    frequency: str = "daily"
    interval: int = Field(default=1, ge=1, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    time: str = Field(default="09:00", max_length=5)
    days_of_week: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5], max_length=7)


class JobInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    notebook_id: str
    cron: str = Field(default="0 9 * * *", max_length=100)
    timezone: str = "UTC"
    enabled: bool = True
    schedule_mode: str = "advanced"
    basic_schedule: BasicSchedule | None = None
    cluster_id: str | None = Field(default=None, max_length=80)


class RepositoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2000)
    branch: str = Field(default="main", min_length=1, max_length=200)


class BuildInput(BaseModel):
    repository_id: str
    path: str = "."


class BucketInput(BaseModel):
    name: str


def public(value):
    if isinstance(value, list):
        return [public(item) for item in value]
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if not key.startswith("_") and key not in ("password_hash", "expires_at")}
    return value


def cron_trigger(expression, timezone_name):
    try:
        ZoneInfo(timezone_name)
    except (KeyError, ValueError):
        raise ValueError("Use a valid IANA timezone, such as UTC or America/New_York")
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Use a five-field cron expression: minute hour day month weekday")
    # Crontab numbers Sunday=0/7; APScheduler 3 numbers Monday=0. Translate explicitly.
    names = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    if re.fullmatch(r"[0-9*/,\-]+", fields[4]) and fields[4] != "*":
        days = set()
        for term in fields[4].split(","):
            base, *step_value = term.split("/")
            step = int(step_value[0]) if step_value else 1
            if step < 1:
                raise ValueError("Cron weekday step must be positive")
            if base == "*":
                start, end = 0, 6
            elif "-" in base:
                start, end = map(int, base.split("-"))
            else:
                start = int(base)
                end = 7 if step_value else start
            if not 0 <= start <= end <= 7:
                raise ValueError("Cron weekday numbers must be 0–7")
            days.update(value % 7 for value in range(start, end + 1, step))
        fields[4] = ",".join(names[day] for day in sorted(days))
    return CronTrigger.from_crontab(" ".join(fields), timezone=timezone_name)


def create_app(data_dir=None):
    @asynccontextmanager
    async def lifespan(app):
        directory = Path(data_dir or os.getenv("DATA_DIR", "./data")).resolve()
        app.state.store = Store(directory)
        for subdir in ("notebooks", "repositories", "wheels", "site-packages", "buckets", "warehouse", "logs"):
            (directory / subdir).mkdir(exist_ok=True)
        store = app.state.store
        if not store.list("users"):
            password = os.getenv("OPENBRICKS_ADMIN_PASSWORD", "")
            username = os.getenv("OPENBRICKS_ADMIN_USERNAME", "admin")
            if len(password) < 12:
                raise RuntimeError("Set OPENBRICKS_ADMIN_PASSWORD to at least 12 characters for first startup")
            store.put("users", {"id": uuid4().hex, "username": username, "role": "admin", "password_hash": password_hash(password), "created_at": now()})
        if not store.get("settings", "initialized"):
            notebook = {"id": uuid4().hex, "name": "Welcome to OpenBricks", "description": "Your first local Spark notebook. Run each cell to explore Python, Spark SQL and local blob storage.", "language": "python",
                        "created_at": now(), "updated_at": now(), "repository_id": None, "path": None, "folder_id": None, "cluster_id": None,
                        "cells": [new_cell("markdown", "# Your lakehouse, on your machine\n\nPython and Spark SQL share one local Spark runtime. Data stays in your Docker volume. Run all to try this notebook."),
                                  new_cell("python", "orders = spark.createDataFrame([('Coffee', 24), ('Tea', 18), ('Pastry', 32)], ['product', 'revenue'])\norders.createOrReplaceTempView('orders')\ndisplay(orders)"),
                                  new_cell("sql", "SELECT product, revenue, ROUND(revenue / SUM(revenue) OVER () * 100, 1) AS share_pct\nFROM orders\nORDER BY revenue DESC"),
                                  new_cell("python", "blob.write_bytes('blob://workspace/hello.txt', b'Hello from OpenBricks!')\nprint(blob.read_bytes('blob://workspace/hello.txt').decode())\nprint(blob.path('blob://workspace/hello.txt'))")]}
            store.put("notebooks", notebook)
            save_notebook_file(store, notebook)
            (directory / "buckets" / "workspace").mkdir(exist_ok=True)
            store.put("buckets", {"id": "workspace", "name": "workspace", "created_at": now()})
            store.put("settings", {"id": "initialized", "created_at": now()})
        app.state.runtime = Runtime(store)
        app.state.scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60})
        app.state.started = time.monotonic()
        app.state.login_attempts = defaultdict(deque)
        app.state.runtime.start()
        app.state.scheduler.start()
        for job in store.list("jobs"):
            schedule(app, job)
        try:
            yield
        finally:
            app.state.scheduler.shutdown(wait=False)
            app.state.runtime.close()

    app = FastAPI(title="OpenBricks", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def browser_security(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                expected_host = request.headers.get("x-forwarded-host", request.headers.get("host", "")).split(",")[0].strip()
                configured = os.getenv("OPENBRICKS_PUBLIC_URL", "")
                allowed = {expected_host}
                if configured:
                    allowed.add(urlparse(configured).netloc)
                if urlparse(origin).netloc not in allowed:
                    return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
            length = request.headers.get("content-length")
            max_bytes = int(os.getenv("OPENBRICKS_MAX_UPLOAD_MB", "100")) * 1024 * 1024
            if length:
                try:
                    if int(length) > max_bytes + 1024 * 1024:
                        return JSONResponse({"detail": "Upload exceeds the configured size limit"}, status_code=413)
                except ValueError:
                    return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeBusy)
    async def runtime_busy(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def authenticated(request: Request):
        token = request.cookies.get(COOKIE)
        if not token:
            raise HTTPException(401, "Sign in to continue")
        store = request.app.state.store
        session = store.get("sessions", hashlib.sha256(token.encode()).hexdigest())
        if not session or session["expires_at"] < now():
            raise HTTPException(401, "Session expired. Sign in again")
        user = store.get("users", session["user_id"])
        if not user:
            raise HTTPException(401, "User account no longer exists")
        return user

    def admin(user=Depends(authenticated)):
        if user["role"] != "admin":
            raise HTTPException(403, "Administrator access required")
        return user

    def lookup(request, kind, id):
        result = request.app.state.store.get(kind, id)
        if not result:
            raise HTTPException(404, f"{kind.rstrip('s').title()} not found")
        return result

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/auth/login")
    def login(body: Login, request: Request, response: Response):
        key = (request.client.host if request.client else "local", body.username.lower())
        attempts = request.app.state.login_attempts[key]
        cutoff = time.monotonic() - 300
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, "Too many sign-in attempts. Try again in five minutes")
        attempts.append(time.monotonic())
        user = next((user for user in request.app.state.store.list("users") if user["username"].lower() == body.username.lower()), None)
        valid = password_matches(body.password, user["password_hash"] if user else "pbkdf2_sha256$600000$00000000000000000000000000000000$" + "00" * 32)
        if not user or not valid:
            raise HTTPException(401, "Incorrect username or password")
        attempts.clear()
        token = secrets.token_urlsafe(48)
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        store = request.app.state.store
        for session in store.list("sessions"):
            if session["expires_at"] < now():
                store.delete("sessions", session["id"])
        store.put("sessions", {"id": hashlib.sha256(token.encode()).hexdigest(), "user_id": user["id"], "expires_at": expires})
        response.set_cookie(COOKIE, token, httponly=True, secure=os.getenv("OPENBRICKS_SECURE_COOKIE", "false").lower() == "true", samesite="strict", max_age=7 * 86400, path="/")
        return public(user)

    @app.get("/api/auth/me")
    def me(user=Depends(authenticated)):
        return public(user)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user=Depends(authenticated)):
        token = request.cookies.get(COOKIE, "")
        request.app.state.store.delete("sessions", hashlib.sha256(token.encode()).hexdigest())
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/users")
    def users(request: Request, user=Depends(admin)):
        return public(request.app.state.store.list("users"))

    @app.post("/api/users", status_code=201)
    def create_user(body: UserCreate, request: Request, user=Depends(admin)):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", body.username):
            raise ValueError("Username may contain letters, digits, dots, dashes and underscores")
        if len(body.password) < 12:
            raise ValueError("Passwords must contain at least 12 characters")
        if body.role not in ("admin", "member"):
            raise ValueError("Role must be admin or member")
        store = request.app.state.store
        if any(item["username"].lower() == body.username.lower() for item in store.list("users")):
            raise HTTPException(409, "Username is already in use")
        return public(store.put("users", {"id": uuid4().hex, "username": body.username, "password_hash": password_hash(body.password), "role": body.role, "created_at": now()}))

    @app.get("/api/overview")
    def overview(request: Request, user=Depends(authenticated)):
        store = request.app.state.store
        recent = store.notebook_page(None, limit=12, all_folders=True)["items"]
        return {"notebooks": [notebook_view(store, item) for item in recent], "runs": public(store.run_page(limit=8)["items"]),
                "jobs": [job_view(request.app, job) for job in store.list("jobs", limit=500)], "metrics": store.metrics(),
                "repositories": public(store.list("repositories", limit=500)), "packages": public(store.list("packages", limit=500)), "buckets": bucket_views(store), "compute": compute(request.app)}

    @app.get("/api/folders")
    def folders(request: Request, user=Depends(authenticated)):
        store = request.app.state.store
        return [folder_view(store, item) for item in store.list("folders", limit=5000)]

    @app.post("/api/folders", status_code=201)
    def create_folder(body: FolderInput, request: Request, user=Depends(authenticated)):
        store = request.app.state.store
        name = folder_name(body.name)
        parent_id = body.parent_id or None
        require_folder(store, parent_id)
        with store.lock:
            if any(item["name"].casefold() == name.casefold() for item in store.folder_children(parent_id)):
                raise HTTPException(409, "A folder with this name already exists here")
            item = store.put("folders", {"id": uuid4().hex, "name": name, "parent_id": parent_id, "created_at": now(), "updated_at": now()})
        return folder_view(store, item)

    @app.put("/api/folders/{id}")
    def update_folder(id: str, body: FolderInput, request: Request, user=Depends(authenticated)):
        store = request.app.state.store
        with store.lock:
            item = lookup(request, "folders", id)
            parent_id = (body.parent_id or None) if "parent_id" in body.model_fields_set else item.get("parent_id")
            require_folder(store, parent_id)
            if parent_id == id or any(parent["id"] == id for parent in folder_breadcrumbs(store, parent_id)):
                raise ValueError("A folder cannot be moved inside itself or one of its descendants")
            name = folder_name(body.name)
            if any(sibling["id"] != id and sibling["name"].casefold() == name.casefold() for sibling in store.folder_children(parent_id)):
                raise HTTPException(409, "A folder with this name already exists here")
            item.update(name=name, parent_id=parent_id, updated_at=now())
            store.put("folders", item)
        return folder_view(store, item)

    @app.delete("/api/folders/{id}")
    def delete_folder(id: str, request: Request, user=Depends(authenticated)):
        store = request.app.state.store
        with store.lock:
            lookup(request, "folders", id)
            if store.folder_has_content(id):
                raise HTTPException(409, "Move or delete this folder's notebooks and child folders first")
            store.delete("folders", id)
        return {"ok": True}

    @app.get("/api/workspace")
    def workspace(request: Request, folder_id: str | None = None, search: str = Query(default="", max_length=200),
                  limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), all_folders: bool = False, user=Depends(authenticated)):
        store = request.app.state.store
        folder_id = folder_id or None
        require_folder(store, folder_id)
        page = store.notebook_page(folder_id, search.strip(), limit, offset, all_folders=all_folders or bool(search.strip()))
        page["items"] = [notebook_view(store, item) for item in page["items"]]
        page["folders"] = [folder_view(store, item) for item in store.folder_children(folder_id)] if not search.strip() else []
        page["breadcrumbs"] = folder_breadcrumbs(store, folder_id)
        return page

    @app.get("/api/history/notebooks")
    def notebook_history(request: Request, search: str = Query(default="", max_length=200), limit: int = Query(default=25, ge=1, le=100),
                         offset: int = Query(default=0, ge=0), user=Depends(authenticated)):
        store = request.app.state.store
        page = store.run_notebook_page(search.strip(), limit, offset)
        for item in page["items"]:
            item["folder_path"] = "/" + "/".join(folder["name"] for folder in folder_breadcrumbs(store, item.get("folder_id")))
            item["deleted"] = bool(item["deleted"])
        return page

    @app.get("/api/history/runs")
    def paginated_runs(request: Request, notebook_id: str | None = None, status: str | None = None,
                       limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0), user=Depends(authenticated)):
        if status and status not in ("queued", "running", "succeeded", "failed", "cancelled"):
            raise ValueError("Unknown run status")
        return public(request.app.state.store.run_page(notebook_id, status, limit, offset))

    @app.get("/api/compute")
    def get_compute(request: Request, user=Depends(authenticated)):
        return compute(request.app)

    @app.get("/api/notebooks")
    def notebooks(request: Request, limit: int = Query(default=500, ge=1, le=1000), offset: int = Query(default=0, ge=0),
                  search: str = Query(default="", max_length=200), user=Depends(authenticated)):
        store = request.app.state.store
        return [notebook_view(store, item) for item in store.notebook_page(None, search.strip(), limit, offset, all_folders=True)["items"]]

    @app.post("/api/notebooks", status_code=201)
    def create_notebook(body: NotebookInput, request: Request, user=Depends(authenticated)):
        values = validated_notebook(body)
        require_folder(request.app.state.store, values.get("folder_id"))
        validate_cluster_reference(request.app.state.store, values.get("cluster_id"))
        values.update(id=uuid4().hex, created_at=now(), updated_at=now(), repository_id=None, path=None)
        request.app.state.store.put("notebooks", values)
        save_notebook_file(request.app.state.store, values)
        return notebook_view(request.app.state.store, values)

    @app.post("/api/notebooks/import", status_code=201)
    async def import_notebook(request: Request, file: UploadFile = File(...), folder_id: str = Form(""), user=Depends(authenticated)):
        require_folder(request.app.state.store, folder_id or None)
        content = await limited_upload(file, 10 * 1024 * 1024)
        values = parse_notebook(file.filename or "notebook.ipynb", content)
        values.update(id=uuid4().hex, created_at=now(), updated_at=now(), repository_id=None, path=None, folder_id=folder_id or None, cluster_id=None)
        request.app.state.store.put("notebooks", values)
        save_notebook_file(request.app.state.store, values)
        return notebook_view(request.app.state.store, values)

    @app.get("/api/notebooks/{id}")
    def notebook(id: str, request: Request, user=Depends(authenticated)):
        return notebook_view(request.app.state.store, lookup(request, "notebooks", id))

    @app.put("/api/notebooks/{id}")
    def update_notebook(id: str, body: NotebookInput, request: Request, user=Depends(authenticated)):
        notebook = lookup(request, "notebooks", id)
        values = validated_notebook(body)
        for key in ("folder_id", "cluster_id"):
            if key not in body.model_fields_set:
                values.pop(key, None)
        require_folder(request.app.state.store, values.get("folder_id", notebook.get("folder_id")))
        validate_cluster_reference(request.app.state.store, values.get("cluster_id", notebook.get("cluster_id")))
        notebook.update(values, updated_at=now())
        request.app.state.store.put("notebooks", notebook)
        save_notebook_file(request.app.state.store, notebook)
        return notebook_view(request.app.state.store, notebook)

    @app.patch("/api/notebooks/{id}/location")
    def move_notebook(id: str, body: NotebookLocation, request: Request, user=Depends(authenticated)):
        store = request.app.state.store
        notebook = lookup(request, "notebooks", id)
        if "folder_id" in body.model_fields_set:
            require_folder(store, body.folder_id or None)
            notebook["folder_id"] = body.folder_id or None
        if "cluster_id" in body.model_fields_set:
            validate_cluster_reference(store, body.cluster_id or None)
            notebook["cluster_id"] = body.cluster_id or None
        notebook["updated_at"] = now()
        store.put("notebooks", notebook)
        save_notebook_file(store, notebook)
        return notebook_view(store, notebook)

    @app.delete("/api/notebooks/{id}")
    def delete_notebook(id: str, request: Request, user=Depends(authenticated)):
        lookup(request, "notebooks", id)
        store = request.app.state.store
        if any(job["notebook_id"] == id for job in store.list("jobs")):
            raise HTTPException(409, "Remove this notebook's schedules before deleting it")
        store.delete("notebooks", id)
        (store.directory / "notebooks" / f"{id}.ipynb").unlink(missing_ok=True)
        return {"ok": True}

    @app.get("/api/notebooks/{id}/export")
    def export(id: str, request: Request, user=Depends(authenticated)):
        notebook = lookup(request, "notebooks", id)
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", notebook["name"])
        return JSONResponse(export_notebook(notebook), headers={"Content-Disposition": f'attachment; filename="{safe_name}.ipynb"'})

    @app.post("/api/notebooks/{id}/run", status_code=202)
    def run_notebook(id: str, request: Request, body: RunInput | None = None, user=Depends(authenticated)):
        notebook = lookup(request, "notebooks", id)
        target = body.cluster_id if body and "cluster_id" in body.model_fields_set else notebook.get("cluster_id")
        validate_cluster_reference(request.app.state.store, target)
        extra = {"cluster_id": target} if target else {}
        return public(request.app.state.runtime.submit(notebook, body.cell_id if body else None, **extra))

    @app.get("/api/runs")
    def runs(request: Request, user=Depends(authenticated)):
        return public(request.app.state.store.run_page(limit=200)["items"])

    @app.get("/api/runs/{id}")
    def run(id: str, request: Request, user=Depends(authenticated)):
        return public(lookup(request, "runs", id))

    @app.post("/api/runs/{id}/cancel")
    def cancel_run(id: str, request: Request, user=Depends(authenticated)):
        lookup(request, "runs", id)
        return public(request.app.state.runtime.cancel(id))

    @app.get("/api/jobs/page")
    def paginated_jobs(request: Request, search: str = Query(default="", max_length=200),
                       limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0), user=Depends(authenticated)):
        page = request.app.state.store.job_page(search.strip(), limit, offset)
        page["items"] = [job_view(request.app, job) for job in page["items"]]
        return page

    @app.get("/api/jobs")
    def jobs(request: Request, user=Depends(authenticated)):
        return [job_view(request.app, job) for job in request.app.state.store.list("jobs")]

    @app.post("/api/jobs", status_code=201)
    def create_job(body: JobInput, request: Request, user=Depends(authenticated)):
        notebook = lookup(request, "notebooks", body.notebook_id)
        values = validated_job(body)
        validate_cluster_reference(request.app.state.store, values.get("cluster_id"))
        job = {**values, "id": uuid4().hex, "notebook_name": notebook["name"], "created_at": now(), "last_run_at": None}
        request.app.state.store.put("jobs", job)
        schedule(request.app, job)
        return job_view(request.app, job)

    @app.put("/api/jobs/{id}")
    def update_job(id: str, body: JobInput, request: Request, user=Depends(authenticated)):
        job = lookup(request, "jobs", id)
        notebook = lookup(request, "notebooks", body.notebook_id)
        values = validated_job(body)
        if "cluster_id" not in body.model_fields_set:
            values.pop("cluster_id", None)
        validate_cluster_reference(request.app.state.store, values.get("cluster_id", job.get("cluster_id")))
        job.update(values, notebook_name=notebook["name"])
        request.app.state.store.put("jobs", job)
        schedule(request.app, job)
        return job_view(request.app, job)

    @app.delete("/api/jobs/{id}")
    def delete_job(id: str, request: Request, user=Depends(authenticated)):
        lookup(request, "jobs", id)
        if request.app.state.scheduler.get_job(id):
            request.app.state.scheduler.remove_job(id)
        request.app.state.store.delete("jobs", id)
        return {"ok": True}

    @app.post("/api/jobs/{id}/run", status_code=202)
    def run_job(id: str, request: Request, user=Depends(authenticated)):
        job = lookup(request, "jobs", id)
        notebook = lookup(request, "notebooks", job["notebook_id"])
        target = job.get("cluster_id") or notebook.get("cluster_id")
        extra = {"cluster_id": target} if target else {}
        return public(request.app.state.runtime.submit(notebook, trigger="manual", job_id=id, **extra))

    @app.get("/api/repositories")
    def repositories(request: Request, user=Depends(authenticated)):
        return public(request.app.state.store.list("repositories"))

    @app.post("/api/repositories", status_code=201)
    def clone_repository(body: RepositoryInput, request: Request, user=Depends(authenticated)):
        validate_git_url(body.url)
        if body.branch.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_./-]+", body.branch) or ".." in body.branch:
            raise ValueError("Invalid branch name")
        id = uuid4().hex
        store = request.app.state.store
        path = store.directory / "repositories" / id
        try:
            git(["clone", "--single-branch", "--branch", body.branch, "--", body.url, str(path)])
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
            raise
        repo = {"id": id, **body.model_dump(), "path": str(path), "status": "ready", "updated_at": now()}
        update_repository_info(repo)
        return store.put("repositories", repo)

    @app.post("/api/repositories/{id}/pull")
    def pull_repository(id: str, request: Request, user=Depends(authenticated)):
        repo = lookup(request, "repositories", id)
        path = request.app.state.store.directory / "repositories" / id
        git(["-C", str(path), "pull", "--ff-only"])
        update_repository_info(repo)
        repo["updated_at"] = now()
        return request.app.state.store.put("repositories", repo)

    @app.post("/api/repositories/{id}/import")
    def import_repository(id: str, request: Request, user=Depends(authenticated)):
        repo = lookup(request, "repositories", id)
        store = request.app.state.store
        base = store.directory / "repositories" / id
        existing = {(nb.get("repository_id"), nb.get("path")): nb for nb in store.list("notebooks")}
        imported = []
        root_folder = repository_folder(store, repo)
        for path in notebook_paths(base):
            relative = path.relative_to(base).as_posix()
            notebook = parse_notebook(path.name, path.read_bytes())
            previous = existing.get((id, relative))
            folder = root_folder
            for part in path.relative_to(base).parent.parts:
                folder = ensure_folder(store, part, folder["id"])
            notebook.update(id=previous["id"] if previous else uuid4().hex, created_at=previous["created_at"] if previous else now(), updated_at=now(), repository_id=id, path=relative,
                            folder_id=previous.get("folder_id", folder["id"]) if previous else folder["id"], cluster_id=previous.get("cluster_id") if previous else None)
            store.put("notebooks", notebook)
            save_notebook_file(store, notebook)
            imported.append(notebook_view(store, notebook))
        return imported

    @app.get("/api/packages")
    def packages(request: Request, user=Depends(authenticated)):
        return public(request.app.state.store.list("packages"))

    @app.post("/api/packages/upload", status_code=201)
    async def upload_package(request: Request, file: UploadFile = File(...), user=Depends(authenticated)):
        filename = Path(file.filename or "").name
        if not filename.endswith(".whl") or not re.fullmatch(r"[a-zA-Z0-9_.+-]+\.whl", filename):
            raise ValueError("Upload a Python .whl package")
        content = await limited_upload(file)
        def install():
            with request.app.state.runtime.environment_change():
                path = request.app.state.store.directory / "wheels" / filename
                temporary = path.with_suffix(f".{uuid4().hex}.upload")
                temporary.write_bytes(content)
                temporary.replace(path)
                return install_wheel(request.app, path, "upload")
        return await run_in_threadpool(install)

    @app.post("/api/packages/build", status_code=201)
    def build_package(body: BuildInput, request: Request, user=Depends(authenticated)):
        repo = lookup(request, "repositories", body.repository_id)
        base = request.app.state.store.directory / "repositories" / repo["id"]
        project = confined_path(base, body.path, allow_root=True)
        if not project.is_dir() or not ((project / "pyproject.toml").is_file() or (project / "setup.py").is_file()):
            raise ValueError("Project must contain pyproject.toml or setup.py")
        with request.app.state.runtime.environment_change():
            output = request.app.state.store.directory / "wheels" / uuid4().hex
            output.mkdir()
            python_path = str(request.app.state.store.directory / "site-packages") + os.pathsep + os.getenv("PYTHONPATH", "")
            command([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(output), str(project)], timeout=300, extra_env={"PYTHONPATH": python_path})
            wheels = list(output.glob("*.whl"))
            if not wheels:
                raise ValueError("Build did not produce a wheel")
            return [install_wheel(request.app, path, f"{repo['name']}:{body.path}") for path in wheels]

    @app.get("/api/buckets")
    def buckets(request: Request, user=Depends(authenticated)):
        return bucket_views(request.app.state.store)

    @app.post("/api/buckets", status_code=201)
    def create_bucket(body: BucketInput, request: Request, user=Depends(authenticated)):
        name = bucket_name(body.name)
        store = request.app.state.store
        path = confined_path(store.directory / "buckets", name)
        if path.exists():
            raise HTTPException(409, "Bucket already exists")
        path.mkdir()
        store.put("buckets", {"id": name, "name": name, "created_at": now()})
        return next(bucket for bucket in bucket_views(store) if bucket["name"] == name)

    def bucket_path(request, name):
        path = confined_path(request.app.state.store.directory / "buckets", bucket_name(name))
        if not path.is_dir():
            raise HTTPException(404, "Bucket not found")
        return path

    @app.get("/api/buckets/{name}/objects")
    def objects(name: str, request: Request, user=Depends(authenticated)):
        return object_views(bucket_path(request, name))

    @app.post("/api/buckets/{name}/objects", status_code=201)
    async def upload_object(name: str, request: Request, file: UploadFile = File(...), key: str = Form(""), user=Depends(authenticated)):
        base = bucket_path(request, name)
        key = key or file.filename or "upload"
        path = confined_path(base, key)
        content = await limited_upload(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"key": path.relative_to(base).as_posix(), "size": len(content), "modified_at": now()}

    @app.get("/api/buckets/{name}/download")
    def download_object(name: str, key: str, request: Request, user=Depends(authenticated)):
        path = confined_path(bucket_path(request, name), key)
        if not path.is_file():
            raise HTTPException(404, "Object not found")
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.delete("/api/buckets/{name}/objects")
    def delete_object(name: str, key: str, request: Request, user=Depends(authenticated)):
        path = confined_path(bucket_path(request, name), key)
        if not path.is_file():
            raise HTTPException(404, "Object not found")
        path.unlink()
        return {"ok": True}

    from .connectors import register_connector_routes
    from .clusters import register_cluster_routes
    from .catalog import register_catalog_routes
    register_connector_routes(app, authenticated, admin)
    register_cluster_routes(app, authenticated, admin)
    register_catalog_routes(app, authenticated, admin)
    return app


def validated_notebook(body):
    result = body.model_dump()
    result["folder_id"] = result.get("folder_id") or None
    result["cluster_id"] = result.get("cluster_id") or None
    result["name"] = result["name"].strip()
    if not result["name"]:
        raise ValueError("Notebook name is required")
    if result["language"] not in ("python", "sql"):
        raise ValueError("Notebook language must be python or sql")
    if result["cells"] is None:
        result["cells"] = [new_cell(result["language"])]
    if any(cell["language"] not in ("python", "sql", "markdown") for cell in result["cells"]):
        raise ValueError("Supported cell languages: python, sql and markdown")
    if len({cell["id"] for cell in result["cells"]}) != len(result["cells"]):
        raise ValueError("Cell identifiers must be unique")
    return result


def folder_name(value: str):
    value = value.strip()
    if value in ("", ".", "..") or re.search(r"[\\/\x00-\x1f]", value):
        raise ValueError("Folder names cannot be empty, contain slashes, or use traversal segments")
    return value


def require_folder(store, folder_id):
    if folder_id and not store.get("folders", folder_id):
        raise HTTPException(404, "Folder not found")


def folder_breadcrumbs(store, folder_id):
    result, seen = [], set()
    while folder_id:
        if folder_id in seen:
            raise ValueError("Folder hierarchy contains a cycle")
        seen.add(folder_id)
        folder = store.get("folders", folder_id)
        if not folder:
            break
        result.append({"id": folder["id"], "name": folder["name"], "parent_id": folder.get("parent_id")})
        folder_id = folder.get("parent_id")
    result.reverse()
    names = []
    for folder in result:
        names.append(folder["name"])
        folder["path"] = "/" + "/".join(names)
    return result


def folder_view(store, folder):
    return {**public(folder), "path": "/" + "/".join(item["name"] for item in folder_breadcrumbs(store, folder["id"]))}


def notebook_view(store, notebook):
    notebook = public(notebook)
    notebook.setdefault("folder_id", None)
    notebook.setdefault("cluster_id", None)
    notebook["folder_path"] = "/" + "/".join(item["name"] for item in folder_breadcrumbs(store, notebook.get("folder_id")))
    return notebook


def ensure_folder(store, name, parent_id=None, **extra):
    with store.lock:
        existing = next((item for item in store.folder_children(parent_id) if item["name"].casefold() == name.casefold()), None)
        if existing:
            return existing
        return store.put("folders", {"id": uuid4().hex, "name": folder_name(name), "parent_id": parent_id, "created_at": now(), "updated_at": now(), **extra})


def repository_folder(store, repo):
    existing = next((item for item in store.folder_children(None) if item.get("repository_id") == repo["id"]), None)
    if existing:
        return existing
    name = re.sub(r"[\\/\x00-\x1f]", "_", repo["name"]).strip()
    if name in ("", ".", ".."):
        name = "Repository"
    sibling_names = {item["name"].casefold() for item in store.folder_children(None)}
    candidate, suffix = name, 2
    while candidate.casefold() in sibling_names:
        candidate = f"{name} ({suffix})"
        suffix += 1
    return ensure_folder(store, candidate, repository_id=repo["id"])


def validate_cluster_reference(store, cluster_id):
    if cluster_id:
        from .clusters import cluster_record
        cluster_record(store, cluster_id)


def validated_job(body):
    values = body.model_dump()
    values["name"] = values["name"].strip()
    if not values["name"]:
        raise ValueError("Job name is required")
    if body.schedule_mode not in ("basic", "advanced"):
        raise ValueError("Schedule mode must be basic or advanced")
    if body.schedule_mode == "basic":
        if body.basic_schedule is None:
            raise ValueError("Choose basic scheduling options")
        basic = body.basic_schedule
        if basic.frequency == "hourly":
            hour = "*" if basic.interval == 1 else f"*/{basic.interval}"
            values["cron"] = f"{basic.minute} {hour} * * *"
        elif basic.frequency in ("daily", "weekly"):
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", basic.time):
                raise ValueError("Choose a valid 24-hour time in HH:mm format")
            hour, minute = map(int, basic.time.split(":"))
            days = "*"
            if basic.frequency == "weekly":
                if not basic.days_of_week or any(day not in range(7) for day in basic.days_of_week):
                    raise ValueError("Choose at least one weekday (Sunday=0 through Saturday=6)")
                days = ",".join(str(day) for day in sorted(set(basic.days_of_week)))
            values["cron"] = f"{minute} {hour} * * {days}"
        else:
            raise ValueError("Basic frequency must be hourly, daily or weekly")
    else:
        values["basic_schedule"] = None
    values["cluster_id"] = values.get("cluster_id") or None
    cron_trigger(values["cron"], values["timezone"])
    return values


def save_notebook_file(store, notebook):
    path = store.directory / "notebooks" / f"{notebook['id']}.ipynb"
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(export_notebook(notebook), indent=2), encoding="utf-8")
    temporary.replace(path)


def job_view(app, job):
    entry = app.state.scheduler.get_job(job["id"])
    return {**public(job), "next_run_at": entry.next_run_time.isoformat() if entry and entry.next_run_time else None}


def schedule(app, job):
    scheduler = app.state.scheduler
    if scheduler.get_job(job["id"]):
        scheduler.remove_job(job["id"])
    if not job["enabled"]:
        return
    def execute():
        store = app.state.store
        current = store.get("jobs", job["id"])
        notebook = store.get("notebooks", job["notebook_id"])
        if current and current["enabled"] and notebook:
            # A schedule never overlaps itself; interactive work shares the same FIFO queue.
            if store.has_active_job(job["id"]):
                return
            target = current.get("cluster_id") or notebook.get("cluster_id")
            extra = {"cluster_id": target} if target else {}
            app.state.runtime.submit(notebook, trigger="schedule", job_id=job["id"], **extra)
            current["last_run_at"] = now()
            store.put("jobs", current)
    scheduler.add_job(execute, cron_trigger(job["cron"], job["timezone"]), id=job["id"], replace_existing=True)


def compute(app):
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage(app.state.store.directory)
    try:
        spark_version = importlib.metadata.version("pyspark")
    except importlib.metadata.PackageNotFoundError:
        spark_version = "not installed"
    runtime = app.state.runtime
    active_count = getattr(runtime, "active_count", 1 if runtime.active_id else 0)
    queued_count = getattr(runtime, "queued_count", runtime.queue.qsize())
    return {"status": "busy" if active_count else "idle", "hostname": platform.node(), "cpu_count": os.cpu_count() or 1,
            "memory_total": memory.total, "memory_used": memory.used, "disk_total": disk.total, "disk_used": disk.used,
            "spark_version": spark_version, "python_version": platform.python_version(), "active_runs": active_count, "queued_runs": queued_count,
            "uptime_seconds": round(time.monotonic() - app.state.started), "master": os.getenv("SPARK_MASTER", "local[*]")}


def command(args, timeout=120, cwd=None, extra_env=None):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", **(extra_env or {})})
    except subprocess.TimeoutExpired:
        raise ValueError(f"Operation exceeded {timeout} seconds")
    if result.returncode:
        message = (result.stderr or result.stdout or "Operation failed")[-6000:]
        raise ValueError(message)
    return result.stdout.strip()


def git(args):
    return command(["git", "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never", "-c", "core.hooksPath=" + os.devnull, *args])


def validate_git_url(url):
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", url):
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "ssh") or not parsed.hostname or not parsed.path or parsed.password or (parsed.scheme == "https" and parsed.username):
        raise ValueError("Use an HTTPS or SSH repository URL without embedded passwords or tokens")


def notebook_paths(base):
    paths = []
    for path in base.rglob("*"):
        if len(paths) >= 500:
            break
        if not path.is_file() or path.is_symlink() or any(part.startswith(".") or part in ("node_modules", "__pycache__", "venv") for part in path.relative_to(base).parts):
            continue
        if path.suffix.lower() in (".ipynb", ".sql") or (path.suffix.lower() == ".py" and path.stat().st_size < 10 * 1024 * 1024 and "# Databricks notebook source" in path.read_text(encoding="utf-8", errors="ignore")[:100]):
            confined_path(base, path.relative_to(base).as_posix())
            paths.append(path)
    return paths


def update_repository_info(repo):
    repo["commit"] = git(["-C", repo["path"], "rev-parse", "--short", "HEAD"])
    repo["notebook_count"] = len(notebook_paths(Path(repo["path"])))


async def limited_upload(file, limit=None):
    limit = limit or int(os.getenv("OPENBRICKS_MAX_UPLOAD_MB", "100")) * 1024 * 1024
    content = bytearray()
    while chunk := await file.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > limit:
            raise HTTPException(413, f"File exceeds {limit // 1024 // 1024} MB limit")
    return bytes(content)


def idle_runtime(app):
    if app.state.runtime.active_id or not app.state.runtime.queue.empty():
        raise HTTPException(409, "Wait for queued and running notebooks before changing the shared Python environment")


def install_wheel(app, path, source):
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_files = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1:
                raise ValueError("Wheel must have exactly one package metadata file")
            for member in archive.infolist():
                confined_path(app.state.store.directory / "site-packages", member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Symbolic links are not allowed in wheel packages")
            if sum(item.file_size for item in archive.infolist()) > 500 * 1024 * 1024:
                raise ValueError("Expanded wheel exceeds 500 MB")
            metadata = Parser().parsestr(archive.read(metadata_files[0]).decode("utf-8"))
    except zipfile.BadZipFile:
        raise ValueError("Invalid wheel archive")
    app.state.runtime.reset()
    command([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--upgrade", "--target", str(app.state.store.directory / "site-packages"), str(path)], timeout=180)
    entry = {"id": uuid4().hex, "name": metadata.get("Name", path.stem), "version": metadata.get("Version", "unknown"), "filename": path.name,
             "size": path.stat().st_size, "installed_at": now(), "source": source, "status": "installed"}
    for previous in app.state.store.list("packages"):
        if previous["name"].lower().replace("-", "_") == entry["name"].lower().replace("-", "_"):
            app.state.store.delete("packages", previous["id"])
    app.state.store.put("packages", entry)
    if hasattr(app.state.runtime, "sync_wheel"):
        app.state.runtime.sync_wheel(path)
    return entry


def object_views(base):
    entries = []
    for path in base.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                confined_path(base, path.relative_to(base).as_posix())
            except ValueError:
                continue
            stat = path.stat()
            entries.append({"key": path.relative_to(base).as_posix(), "size": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    return sorted(entries, key=lambda item: item["key"])


def bucket_views(store):
    result = []
    root = store.directory / "buckets"
    records = {entry["name"]: entry for entry in store.list("buckets")}
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        objects = object_views(path)
        result.append({"name": path.name, "object_count": len(objects), "size": sum(item["size"] for item in objects), "created_at": records.get(path.name, {}).get("created_at", datetime.fromtimestamp(path.stat().st_ctime, timezone.utc).isoformat())})
    return result


app = create_app()
