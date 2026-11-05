"""Project membership and API access for trusted-team workspaces.

These checks scope application actions and credential dispatch. Python code
still executes as a trusted compute account, not in an untrusted-user sandbox.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

DEFAULT_PROJECT = "default"
ROLE_ACTIONS = {
    "viewer": {"read"},
    "operator": {"read", "run"},
    "editor": {"read", "run", "edit"},
    "owner": {"read", "run", "edit", "manage"},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def public(value):
    return {
        key: item
        for key, item in value.items()
        if not key.startswith("_") and key != "password_hash"
    }


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    connection_ids: list[str] = Field(default_factory=list, max_length=100)
    cluster_ids: list[str] = Field(default_factory=lambda: ["local"], max_length=100)


class MembershipInput(BaseModel):
    role: str


class Access:
    def __init__(self, store):
        self.store = store

    def seed(self):
        if not self.store.get("projects", DEFAULT_PROJECT):
            self.store.put(
                "projects",
                {
                    "id": DEFAULT_PROJECT,
                    "name": "Shared workspace",
                    "description": "The original workspace",
                    "connection_ids": [],
                    "cluster_ids": ["local"],
                    "created_at": now(),
                    "updated_at": now(),
                },
            )
        if not self.store.get("settings", "project_acl_initialized"):
            for user in self.store.list("users"):
                if user.get("role") != "admin":
                    self.add_default_member(user["id"])
            self.store.put(
                "settings", {"id": "project_acl_initialized", "created_at": now()}
            )

    def add_default_member(self, user_id):
        return self.store.put(
            "project_members",
            {
                "id": f"{DEFAULT_PROJECT}:{user_id}",
                "project_id": DEFAULT_PROJECT,
                "user_id": user_id,
                "role": "editor",
                "created_at": now(),
            },
        )

    def role(self, user, project_id=DEFAULT_PROJECT):
        if user and user.get("role") == "admin":
            return "owner"
        member = (
            self.store.get("project_members", f"{project_id}:{user['id']}")
            if user
            else None
        )
        return member.get("role") if member else None

    def allowed(self, user, project_id=DEFAULT_PROJECT, action="read"):
        from .automation_auth import token_allows

        if not token_allows(user, project_id or DEFAULT_PROJECT, action):
            return False
        return action in ROLE_ACTIONS.get(
            self.role(user, project_id or DEFAULT_PROJECT), set()
        )

    def require_project(self, user, project_id=DEFAULT_PROJECT, action="read"):
        project_id = project_id or DEFAULT_PROJECT
        project = self.store.get("projects", project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        if not self.allowed(user, project_id, action):
            raise HTTPException(403, f"This project requires {action} access")
        return project

    def visible_project_ids(self, user, action="read"):
        return [
            project["id"]
            for project in self.store.list("projects")
            if self.allowed(user, project["id"], action)
        ]

    def require_notebook(self, user, notebook, action="read"):
        self.require_project(
            user, notebook.get("project_id") or DEFAULT_PROJECT, action
        )
        return notebook

    def require_connection(self, user, connection_id, project_id=None, action="run"):
        record = self.store.get("connections", connection_id)
        if not record:
            raise HTTPException(404, "Connection not found")
        if user.get("role") == "admin" and project_id is None:
            return record
        projects = (
            [project_id] if project_id else self.visible_project_ids(user, action)
        )
        for candidate in projects:
            project = self.require_project(user, candidate, action)
            if candidate in record.get(
                "project_ids", [DEFAULT_PROJECT]
            ) or connection_id in project.get("connection_ids", []):
                return record
        raise HTTPException(
            403, "This connection is not available to the selected project"
        )

    def require_cluster(
        self, user, cluster_id, project_id=DEFAULT_PROJECT, action="run"
    ):
        project = self.require_project(user, project_id, action)
        selected = cluster_id or "local"
        if user.get("role") != "admin" and selected not in project.get(
            "cluster_ids", ["local"]
        ):
            raise HTTPException(
                403, "This compute service is not assigned to the project"
            )
        return selected

    def filter_records(self, user, records, action="read"):
        projects = set(self.visible_project_ids(user, action))
        return [
            record
            for record in records
            if (record.get("project_id") or DEFAULT_PROJECT) in projects
        ]

    def audit(self, user, action, project_id=DEFAULT_PROJECT, resource_id=None):
        self.store.put(
            "audit_events",
            {
                "id": uuid4().hex,
                "project_id": project_id or DEFAULT_PROJECT,
                "actor_id": user.get("id") if user else None,
                "action": action,
                "resource_id": resource_id,
                "created_at": now(),
            },
        )


class AccessProxy:
    """Route registration precedes application lifespan initialization."""

    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app.state.access, name)


def register_project_routes(app, authenticated):
    def access(request):
        return request.app.state.access

    @app.get("/api/projects")
    def projects(request: Request, user=Depends(authenticated)):
        acl = access(request)
        return [
            {**public(project), "role": acl.role(user, project["id"])}
            for project in acl.store.list("projects")
            if acl.allowed(user, project["id"])
        ]

    def validate(body, store):
        values = body.model_dump()
        values["name"] = values["name"].strip()
        if not values["name"]:
            raise ValueError("Project name is required")
        for connection in values["connection_ids"]:
            if not store.get("connections", connection):
                raise ValueError("Selected connection does not exist")
        for cluster in values["cluster_ids"]:
            if cluster != "local" and not store.get("clusters", cluster):
                raise ValueError("Selected compute service does not exist")
        return values

    @app.post("/api/projects", status_code=201)
    def create_project(
        body: ProjectInput, request: Request, user=Depends(authenticated)
    ):
        if user.get("role") != "admin":
            raise HTTPException(
                403, "Administrator access required to create a project"
            )
        acl = access(request)
        values = validate(body, acl.store)
        item = acl.store.put(
            "projects",
            {**values, "id": uuid4().hex, "created_at": now(), "updated_at": now()},
        )
        acl.audit(user, "project.create", item["id"], item["id"])
        return {**item, "role": "owner"}

    @app.put("/api/projects/{id}")
    def update_project(
        id: str, body: ProjectInput, request: Request, user=Depends(authenticated)
    ):
        acl = access(request)
        item = acl.require_project(user, id, "manage")
        # Assigning credentials/compute broadens access and remains an admin action.
        if user.get("role") != "admin" and (
            set(body.connection_ids) != set(item.get("connection_ids", []))
            or set(body.cluster_ids) != set(item.get("cluster_ids", ["local"]))
        ):
            raise HTTPException(
                403, "An administrator must assign project connections and compute"
            )
        item.update(validate(body, acl.store), updated_at=now())
        acl.store.put("projects", item)
        acl.audit(user, "project.update", id, id)
        return {**public(item), "role": acl.role(user, id)}

    @app.delete("/api/projects/{id}")
    def delete_project(id: str, request: Request, user=Depends(authenticated)):
        acl = access(request)
        acl.require_project(user, id, "manage")
        if id == DEFAULT_PROJECT:
            raise HTTPException(409, "The shared workspace cannot be deleted")
        with acl.store.connect() as db:
            occupied = db.execute(
                "SELECT 1 FROM documents WHERE project_id=? AND kind NOT IN ('project_members','audit_events') LIMIT 1",
                (id,),
            ).fetchone()
            if occupied:
                raise HTTPException(
                    409, "Remove the project's notebooks, jobs, and data records first"
                )
            db.execute(
                "DELETE FROM documents WHERE project_id=? AND kind='project_members'",
                (id,),
            )
        acl.store.delete("projects", id)
        return {"ok": True}

    @app.get("/api/projects/{id}/members")
    def members(id: str, request: Request, user=Depends(authenticated)):
        acl = access(request)
        acl.require_project(user, id)
        result = []
        for account in acl.store.list("users"):
            role = acl.role(account, id)
            if role:
                result.append(
                    {
                        "user_id": account["id"],
                        "username": account["username"],
                        "role": role,
                        "global_admin": account.get("role") == "admin",
                        "is_service_account": bool(account.get("is_service_account")),
                    }
                )
        return result

    @app.get("/api/projects/{id}/available-users")
    def available_users(id: str, request: Request, user=Depends(authenticated)):
        acl = access(request)
        acl.require_project(user, id, "manage")
        return [
            {
                "id": account["id"],
                "username": account["username"],
                "is_service_account": bool(account.get("is_service_account")),
            }
            for account in acl.store.list("users", limit=1000)
        ]

    @app.put("/api/projects/{id}/members/{user_id}")
    def put_member(
        id: str,
        user_id: str,
        body: MembershipInput,
        request: Request,
        user=Depends(authenticated),
    ):
        acl = access(request)
        acl.require_project(user, id, "manage")
        account = acl.store.get("users", user_id)
        if not account:
            raise HTTPException(404, "User not found")
        if body.role not in ROLE_ACTIONS or account.get("role") == "admin":
            raise ValueError("Choose a project role for a non-administrator account")
        if account.get("is_service_account") and body.role == "owner":
            raise ValueError("Service accounts can be viewers, operators, or editors")
        acl.store.put(
            "project_members",
            {
                "id": f"{id}:{user_id}",
                "project_id": id,
                "user_id": user_id,
                "role": body.role,
                "updated_at": now(),
            },
        )
        acl.audit(user, "membership.update", id, user_id)
        return {"user_id": user_id, "username": account["username"], "role": body.role}

    @app.delete("/api/projects/{id}/members/{user_id}")
    def delete_member(
        id: str, user_id: str, request: Request, user=Depends(authenticated)
    ):
        acl = access(request)
        acl.require_project(user, id, "manage")
        if user_id == user["id"] and user.get("role") != "admin":
            raise HTTPException(409, "Ask another owner to remove your membership")
        acl.store.delete("project_members", f"{id}:{user_id}")
        acl.audit(user, "membership.delete", id, user_id)
        return {"ok": True}
