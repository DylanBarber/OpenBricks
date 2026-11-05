"""Revocable automation identities and scoped, expiring bearer credentials."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .permissions import ROLE_ACTIONS, now
from .security import password_hash


def public(value):
    return {
        key: item
        for key, item in value.items()
        if not key.startswith("_") and key not in ("password_hash", "token_hash")
    }


def normalized_actions(actions):
    if (
        not isinstance(actions, list)
        or not actions
        or any(action not in ("read", "run", "edit") for action in actions)
    ):
        raise ValueError("Choose read, run, or edit token permissions")
    highest = (
        "editor" if "edit" in actions else "operator" if "run" in actions else "viewer"
    )
    return sorted(ROLE_ACTIONS[highest])


def token_allows(user, project_id, action):
    scope = user.get("_api_token") if user else None
    return scope is None or (
        project_id in scope.get("project_ids", [])
        and action in scope.get("actions", [])
    )


def authenticate_bearer(request):
    """Return None only without an Authorization header; malformed tokens fail closed."""
    authorization = request.headers.get("authorization")
    if authorization is None:
        return None
    scheme, separator, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or len(authorization) > 1024:
        raise HTTPException(401, "Invalid automation token")
    if not re.fullmatch(r"ob_[A-Za-z0-9_-]{40,100}", value):
        raise HTTPException(401, "Invalid automation token")
    store = request.app.state.store
    digest = hashlib.sha256(value.encode()).hexdigest()
    token = store.get("api_tokens", digest)
    instant = now()
    if not token or token.get("revoked_at") or token.get("expires_at", "") <= instant:
        raise HTTPException(401, "Automation token expired or revoked")
    account = store.get("users", token.get("service_account_id", ""))
    if not account or not account.get("is_service_account") or account.get("disabled"):
        raise HTTPException(401, "Automation account is unavailable")
    if account.get("role") == "admin":
        raise HTTPException(
            401, "Automation accounts cannot have workspace administrator access"
        )
    # Touch usage at most once per minute, without storing the credential itself.
    if (token.get("last_used_at") or "") < (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat():
        with store.connect() as db:
            db.execute(
                "UPDATE documents SET body=json_set(body,'$.last_used_at',?) WHERE kind='api_tokens' AND id=? AND json_extract(body,'$.revoked_at') IS NULL",
                (instant, token["id"]),
            )
    principal = {key: item for key, item in account.items() if key != "password_hash"}
    principal["_api_token"] = {
        "id": token["id"],
        "project_ids": token["project_ids"],
        "actions": token["actions"],
    }
    return principal


class ServiceAccountInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=2000)
    grants: list[dict] = Field(min_length=1, max_length=100)


class TokenInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    project_ids: list[str] = Field(min_length=1, max_length=100)
    actions: list[str] = Field(default_factory=lambda: ["read", "run"])
    expires_in_days: int = Field(default=30, ge=1, le=365)


def validate_grants(store, values):
    grants, seen = [], set()
    for item in values:
        project_id, role = item.get("project_id"), item.get("role", "operator")
        if not isinstance(project_id, str) or not store.get("projects", project_id):
            raise ValueError("Choose an existing project for every grant")
        if role not in ("viewer", "operator", "editor") or project_id in seen:
            raise ValueError(
                "Service accounts accept unique viewer, operator, or editor project grants"
            )
        grants.append({"project_id": project_id, "role": role})
        seen.add(project_id)
    return grants


def register_automation_auth_routes(app, authenticated, admin, access):
    @app.get("/api/service-accounts")
    def list_accounts(request: Request, user=Depends(admin)):
        store = request.app.state.store
        with store.connect() as db:
            rows = db.execute(
                "SELECT json_remove(body,'$.password_hash') FROM documents WHERE kind='users' AND json_extract(body,'$.is_service_account')=1 ORDER BY name,id LIMIT 200"
            ).fetchall()
        result = []
        for row in rows:
            account = public(json.loads(row[0]))
            account["grants"] = [
                {"project_id": member["project_id"], "role": member["role"]}
                for member in store.list("project_members")
                if member.get("user_id") == account["id"]
            ]
            result.append(account)
        return result

    @app.post("/api/service-accounts", status_code=201)
    def create_account(
        body: ServiceAccountInput, request: Request, user=Depends(admin)
    ):
        store = request.app.state.store
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,59}", body.name):
            raise ValueError(
                "Use letters, numbers, dots, dashes, or underscores in the account name"
            )
        grants = validate_grants(store, body.grants)
        with store.lock:
            if any(
                item.get("username", "").lower() == body.name.lower()
                for item in store.list("users")
            ):
                raise HTTPException(409, "That account name is already in use")
            account = {
                "id": uuid4().hex,
                "username": body.name,
                "name": body.name,
                "description": body.description,
                "role": "member",
                "is_service_account": True,
                "created_at": now(),
                "password_hash": password_hash(secrets.token_urlsafe(64)),
            }
            store.put("users", account)
            for grant in grants:
                store.put(
                    "project_members",
                    {
                        "id": f"{grant['project_id']}:{account['id']}",
                        "user_id": account["id"],
                        **grant,
                        "created_at": now(),
                    },
                )
        access.audit(
            user, "service_account.create", grants[0]["project_id"], account["id"]
        )
        return {**public(account), "grants": grants}

    @app.put("/api/service-accounts/{id}")
    def update_account(
        id: str, body: ServiceAccountInput, request: Request, user=Depends(admin)
    ):
        store = request.app.state.store
        account = store.get("users", id)
        if not account or not account.get("is_service_account"):
            raise HTTPException(404, "Service account not found")
        grants = validate_grants(store, body.grants)
        with store.lock:
            account["description"], account["updated_at"] = body.description, now()
            store.put("users", account)
            with store.connect() as db:
                db.execute(
                    "DELETE FROM documents WHERE kind='project_members' AND json_extract(body,'$.user_id')=?",
                    (id,),
                )
            for grant in grants:
                store.put(
                    "project_members",
                    {
                        "id": f"{grant['project_id']}:{id}",
                        "user_id": id,
                        **grant,
                        "created_at": now(),
                    },
                )
        return {**public(account), "grants": grants}

    @app.delete("/api/service-accounts/{id}")
    def delete_account(id: str, request: Request, user=Depends(admin)):
        store = request.app.state.store
        account = store.get("users", id)
        if not account or not account.get("is_service_account"):
            raise HTTPException(404, "Service account not found")
        with store.connect() as db:
            db.execute(
                "DELETE FROM documents WHERE kind='api_tokens' AND json_extract(body,'$.service_account_id')=?",
                (id,),
            )
            db.execute(
                "DELETE FROM documents WHERE kind='project_members' AND json_extract(body,'$.user_id')=?",
                (id,),
            )
            db.execute("DELETE FROM documents WHERE kind='users' AND id=?", (id,))
        return {"ok": True}

    @app.get("/api/service-accounts/{id}/tokens")
    def list_tokens(id: str, request: Request, user=Depends(admin)):
        with request.app.state.store.connect() as db:
            rows = db.execute(
                "SELECT json_remove(body,'$.token_hash') FROM documents WHERE kind='api_tokens' AND json_extract(body,'$.service_account_id')=? ORDER BY created_at DESC LIMIT 200",
                (id,),
            ).fetchall()
        return [public(json.loads(row[0])) for row in rows]

    @app.post("/api/service-accounts/{id}/tokens", status_code=201)
    def create_token(id: str, body: TokenInput, request: Request, user=Depends(admin)):
        store = request.app.state.store
        account = store.get("users", id)
        if not account or not account.get("is_service_account"):
            raise HTTPException(404, "Service account not found")
        actions = normalized_actions(body.actions)
        projects = list(dict.fromkeys(body.project_ids))
        for project_id in projects:
            for action in actions:
                access.require_project(account, project_id, action)
        value = "ob_" + secrets.token_urlsafe(48)
        digest = hashlib.sha256(value.encode()).hexdigest()
        token = {
            "id": digest,
            "service_account_id": id,
            "name": body.name.strip(),
            "prefix": value[:11],
            "project_ids": projects,
            "actions": actions,
            "created_at": now(),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
            ).isoformat(),
            "last_used_at": None,
        }
        store.put("api_tokens", token)
        return {**public(token), "token": value}

    @app.delete("/api/service-accounts/{id}/tokens/{token_id}")
    def revoke_token(id: str, token_id: str, request: Request, user=Depends(admin)):
        store = request.app.state.store
        token = store.get("api_tokens", token_id)
        if not token or token.get("service_account_id") != id:
            raise HTTPException(404, "Automation token not found")
        token["revoked_at"] = now()
        store.put("api_tokens", token)
        return {"ok": True}
