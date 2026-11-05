"""Compute admission policy and project-scoped session management routes."""

from __future__ import annotations

from pathlib import Path
import os

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field


def memory_ceiling_mb():
    configured = max(256, int(os.getenv("OPENBRICKS_RESOURCE_MEMORY_MB", "3072")))
    try:
        value = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if value != "max":
            configured = min(configured, max(128, int(value) // 1024**2 - 256))
    except (OSError, ValueError):
        pass
    return configured


class ProjectLimit(BaseModel):
    max_concurrent: int | None = Field(default=None, ge=1, le=8)
    memory_budget_mb: int | None = Field(default=None, ge=128, le=262144)
    priority: int = Field(default=0, ge=-20, le=20)


class ResourcePolicy(BaseModel):
    memory_budget_mb: int = Field(default=3072, ge=128, le=262144)
    max_concurrent: int = Field(default=1, ge=1, le=8)
    max_sessions: int = Field(default=8, ge=1, le=32)
    default_memory_mb: int = Field(default=2048, ge=128, le=262144)
    max_memory_mb: int = Field(default=3072, ge=128, le=262144)
    idle_timeout_seconds: int = Field(default=900, ge=0, le=86400)
    project_limits: dict[str, ProjectLimit] = Field(
        default_factory=dict, max_length=1000
    )


def normalize_policy(value=None, *, ceiling=None):
    maximum = ceiling or memory_ceiling_mb()
    if value is None:
        value = {
            "memory_budget_mb": maximum,
            "default_memory_mb": min(2048, maximum),
            "max_memory_mb": maximum,
            "idle_timeout_seconds": max(
                0, int(os.getenv("OPENBRICKS_IDLE_TIMEOUT_SECONDS", "900"))
            ),
        }
    body = ResourcePolicy.model_validate(value).model_dump()
    if body["memory_budget_mb"] > maximum:
        raise ValueError(
            f"Compute memory budget cannot exceed this host's {maximum} MB admission ceiling"
        )
    if (
        body["default_memory_mb"] > body["max_memory_mb"]
        or body["max_memory_mb"] > body["memory_budget_mb"]
    ):
        raise ValueError(
            "Default worker memory must fit the per-worker maximum and total compute budget"
        )
    for project in body["project_limits"].values():
        project["memory_budget_mb"] = (
            project["memory_budget_mb"] or body["memory_budget_mb"]
        )
        project["max_concurrent"] = project["max_concurrent"] or body["max_concurrent"]
        if (
            project["memory_budget_mb"] > body["memory_budget_mb"]
            or project["max_concurrent"] > body["max_concurrent"]
        ):
            raise ValueError(
                "Project budgets must fit the compute memory and concurrency limits"
            )
    return body


def load_policy(store):
    saved = store.get("settings", "compute_resource_policy")
    if saved:
        try:
            return normalize_policy(saved["policy"])
        except ValueError:
            # An image moved to a smaller host must start within that host's
            # ceiling instead of retaining an unsafe previous reservation.
            pass
    return normalize_policy()


def run_options(value, notebook, policy, *, trigger="manual", job_id=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - {
        "mode",
        "memory_mb",
        "priority",
        "owner_id",
    }:
        raise ValueError(
            "Choose valid worker mode, memory, priority, and owner options"
        )
    mode = value.get("mode") or (
        "job"
        if job_id or trigger != "manual" or notebook.get("data_execution_id")
        else "interactive"
    )
    if mode not in ("job", "interactive"):
        raise ValueError("Worker mode must be interactive or job")
    engine = notebook.get("execution_engine", "spark")
    default_memory = (
        min(512, policy["default_memory_mb"])
        if engine in ("duckdb", "sqlserver")
        else policy["default_memory_mb"]
    )
    memory = value.get("memory_mb", default_memory)
    priority = value.get("priority", 10 if mode == "interactive" else 0)
    if (
        isinstance(memory, bool)
        or not isinstance(memory, int)
        or not 128 <= memory <= policy["max_memory_mb"]
    ):
        raise ValueError(
            f"Worker memory must be between 128 and {policy['max_memory_mb']} MB"
        )
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or not -20 <= priority <= 20
    ):
        raise ValueError("Run priority must be an integer between -20 and 20")
    owner = str(value.get("owner_id") or "workspace")
    if (
        not owner
        or len(owner) > 160
        or any(character in owner for character in ("\n", "\r", "\x00"))
    ):
        raise ValueError("Invalid run owner")
    project_id = notebook.get("project_id", "default")
    project = policy["project_limits"].get(project_id, {})
    if memory > project.get("memory_budget_mb", policy["memory_budget_mb"]):
        raise ValueError(
            "The requested worker memory exceeds the project's compute budget"
        )
    return {"mode": mode, "memory_mb": memory, "priority": priority, "owner_id": owner}


class StopSessionInput(BaseModel):
    cluster_id: str = "local"
    project_id: str = "default"


def register_resource_routes(app, authenticated, admin, access):
    @app.get("/api/resources/status")
    def status(
        request: Request,
        cluster_id: str = "local",
        project_id: str = "default",
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id, "read")
        access.require_cluster(user, cluster_id, project_id, "read")
        value = request.app.state.runtime.resource_status(cluster_id)
        value["sessions"] = [
            session
            for session in value.get("sessions", [])
            if session["project_id"] == project_id
        ]
        value["recent_sessions"] = [
            session
            for session in value.get("recent_sessions", [])
            if session["project_id"] == project_id
        ]
        value["policy"] = {
            **value["policy"],
            "project_limits": {
                project_id: value["policy"]
                .get("project_limits", {})
                .get(project_id, {})
            },
        }
        return value

    @app.get("/api/resources/policy")
    def policy(request: Request, cluster_id: str = "local", user=Depends(admin)):
        return request.app.state.runtime.resource_policy(cluster_id)

    @app.put("/api/resources/policy")
    def update(
        body: ResourcePolicy,
        request: Request,
        cluster_id: str = "local",
        user=Depends(admin),
    ):
        for identifier in body.project_limits:
            access.require_project(user, identifier, "manage")
        value = request.app.state.runtime.update_resource_policy(
            cluster_id, body.model_dump()
        )
        access.audit(user, "resources.policy", "default", cluster_id)
        return value

    @app.post("/api/resources/sessions/{id}/stop")
    def stop(
        id: str, body: StopSessionInput, request: Request, user=Depends(authenticated)
    ):
        access.require_project(user, body.project_id, "run")
        access.require_cluster(user, body.cluster_id, body.project_id, "run")
        state = request.app.state.runtime.resource_status(body.cluster_id)
        session = next(
            (
                item
                for item in state.get("sessions", [])
                if item["id"] == id and item["project_id"] == body.project_id
            ),
            None,
        )
        if not session:
            raise HTTPException(404, "Session not found in this project")
        if session["owner_id"] != user["id"] and not access.allowed(
            user, body.project_id, "manage"
        ):
            raise HTTPException(
                403, "Only the session owner or a project owner can stop this session"
            )
        return request.app.state.runtime.stop_session(body.cluster_id, id)
