"""Project-scoped, write-only secret management and live dispatch authorization."""

from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from .credentials import SecretBox
from .permissions import Access, DEFAULT_PROJECT, now

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
MAX_REFS = 32


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(
            "Secret scope and key names use 1–64 letters, digits, dots, dashes or underscores"
        )
    return value


def scope_id(project_id, scope):
    return hashlib.sha256((project_id + "\x00" + name(scope)).encode()).hexdigest()


def key_id(identifier, key):
    return hashlib.sha256((identifier + "\x00" + name(key)).encode()).hexdigest()


def normalize_refs(values):
    if not isinstance(values, list) or len(values) > MAX_REFS:
        raise ValueError("Select at most 32 notebook secrets")
    result, seen = [], set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {"scope", "key"}:
            raise ValueError("Each secret reference contains only scope and key")
        scope, key = name(item["scope"]), name(item["key"])
        if (scope, key) in seen:
            raise ValueError("Notebook secret references must be unique")
        seen.add((scope, key))
        result.append({"scope": scope, "key": key})
    return result


def _scope(store, project_id, scope):
    item = store.get("secret_scopes", scope_id(project_id, scope))
    if not item or item.get("project_id") != project_id:
        raise HTTPException(404, "Secret scope not found in this project")
    return item


def _require_usage(access, user, item, action="run"):
    access.require_project(user, item["project_id"], action)
    role = access.role(user, item["project_id"])
    if role != "owner" and role not in item.get(
        "allowed_roles", ["operator", "editor", "owner"]
    ):
        raise HTTPException(403, "Your project role cannot use this secret scope")


def validate_secret_refs(store, user, project_id, values, *, action="edit"):
    """Validate names and grants without returning or decrypting values."""
    refs = normalize_refs(values)
    access = Access(store)
    access.require_project(user, project_id or DEFAULT_PROJECT, action)
    for ref in refs:
        item = _scope(store, project_id or DEFAULT_PROJECT, ref["scope"])
        _require_usage(access, user, item, action)
        entry = store.get("notebook_secret_values", key_id(item["id"], ref["key"]))
        if not entry or entry.get("scope_id") != item["id"]:
            raise HTTPException(404, "Selected notebook secret no longer exists")
    return refs


def secrets_for_runtime(store, notebook, user=None, *, owner_id=None):
    """Resolve current grants and values immediately before actual dispatch.

    Callers must keep this return value in memory, never in durable run bodies,
    notebook snapshots, package manifests, source files, or environment values.
    """
    refs = normalize_refs(notebook.get("secret_refs", []))
    if not refs:
        return []
    user = user or (store.get("users", owner_id) if owner_id else None)
    current = store.get("users", user.get("id")) if isinstance(user, dict) else None
    if not current or current.get("disabled"):
        raise HTTPException(403, "Notebook secrets require a current run-as account")
    user = {
        **current,
        **({"_api_token": user["_api_token"]} if user.get("_api_token") else {}),
    }
    project_id = notebook.get("project_id") or DEFAULT_PROJECT
    with store.lock:
        validate_secret_refs(store, user, project_id, refs, action="run")
        box, result, total = SecretBox(store.directory), [], 0
        for ref in refs:
            item = _scope(store, project_id, ref["scope"])
            entry = store.get("notebook_secret_values", key_id(item["id"], ref["key"]))
            value = box.decrypt(entry["_value"])
            total += len(value.encode("utf-8"))
            if total > 128 * 1024:
                raise ValueError(
                    "Selected notebook secrets exceed the 128 KB dispatch budget"
                )
            result.append({**ref, "value": value})
        Access(store).audit(user, "secret.dispatch", project_id, notebook.get("id"))
        return result


def _public_scope(store, item):
    with store.connect() as db:
        rows = db.execute(
            "SELECT body FROM documents WHERE kind='notebook_secret_values' AND project_id=? AND json_extract(body,'$.scope_id')=? ORDER BY name COLLATE NOCASE,id LIMIT 101",
            (item["project_id"], item["id"]),
        ).fetchall()
    keys = [json.loads(row[0]) for row in rows]
    return {
        **{key: value for key, value in item.items() if not key.startswith("_")},
        "keys": [
            {
                key: value
                for key, value in entry.items()
                if key in ("name", "version", "created_at", "updated_at")
            }
            for entry in keys
        ],
    }


class ScopeInput(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    project_id: str = Field(default=DEFAULT_PROJECT, min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    allowed_roles: list[str] = Field(
        default_factory=lambda: ["operator", "editor", "owner"],
        min_length=1,
        max_length=3,
    )


class SecretValueInput(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


class ScopeUpdateInput(BaseModel):
    description: str = Field(default="", max_length=2000)
    allowed_roles: list[str] = Field(
        default_factory=lambda: ["operator", "editor", "owner"],
        min_length=1,
        max_length=3,
    )


class SecretRefsInput(BaseModel):
    secret_refs: list[dict] = Field(default_factory=list, max_length=MAX_REFS)
    version: int | None = Field(default=None, ge=1)


class SecretRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_validation(request):
            try:
                return await original(request)
            except RequestValidationError:
                # Default validation errors can echo a submitted credential.
                raise HTTPException(
                    422, "Secret request does not match the required fields or limits"
                ) from None

        return safe_validation


def register_secret_routes(app, authenticated):
    router = APIRouter(route_class=SecretRoute)

    @router.get("/api/secret-scopes")
    def scopes(
        request: Request,
        project_id: str = DEFAULT_PROJECT,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        store, access = request.app.state.store, request.app.state.access
        access.require_project(user, project_id)
        with store.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind='secret_scopes' AND project_id=?",
                (project_id,),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='secret_scopes' AND project_id=? ORDER BY name COLLATE NOCASE,id LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            ).fetchall()
        return {
            "items": [_public_scope(store, json.loads(row[0])) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @router.post("/api/secret-scopes", status_code=201)
    def create_scope(body: ScopeInput, request: Request, user=Depends(authenticated)):
        store, access = request.app.state.store, request.app.state.access
        access.require_project(user, body.project_id, "manage")
        identifier = scope_id(body.project_id, body.name)
        if set(body.allowed_roles) - {"operator", "editor", "owner"}:
            raise ValueError("Secret scope roles are operator, editor or owner")
        with store.lock:
            if store.get("secret_scopes", identifier):
                raise HTTPException(409, "This secret scope already exists")
            with store.connect() as db:
                total = db.execute(
                    "SELECT COUNT(*) FROM documents WHERE kind='secret_scopes' AND project_id=?",
                    (body.project_id,),
                ).fetchone()[0]
            if total >= 100:
                raise ValueError("A project supports at most 100 secret scopes")
            item = store.put(
                "secret_scopes",
                {
                    **body.model_dump(),
                    "id": identifier,
                    "name": name(body.name),
                    "allowed_roles": sorted(set(body.allowed_roles) | {"owner"}),
                    "created_at": now(),
                    "updated_at": now(),
                },
            )
            access.audit(user, "secret.scope.create", body.project_id, identifier)
        return _public_scope(store, item)

    @router.patch("/api/secret-scopes/{id}")
    def update_scope(
        id: str, body: ScopeUpdateInput, request: Request, user=Depends(authenticated)
    ):
        store, access = request.app.state.store, request.app.state.access
        if set(body.allowed_roles) - {"operator", "editor", "owner"}:
            raise ValueError("Secret scope roles are operator, editor or owner")
        with store.lock:
            item = store.get("secret_scopes", id)
            if not item:
                raise HTTPException(404, "Secret scope not found")
            access.require_project(user, item["project_id"], "manage")
            item.update(
                description=body.description,
                allowed_roles=sorted(set(body.allowed_roles) | {"owner"}),
                updated_at=now(),
            )
            store.put("secret_scopes", item)
            access.audit(user, "secret.scope.update", item["project_id"], id)
        return _public_scope(store, item)

    @router.put("/api/secret-scopes/{id}/keys/{key}")
    def put_key(
        id: str,
        key: str,
        body: SecretValueInput,
        request: Request,
        user=Depends(authenticated),
    ):
        if len(body.value.encode("utf-8")) > 4096:
            raise ValueError("A notebook secret is limited to 4096 UTF-8 bytes")
        store, access = request.app.state.store, request.app.state.access
        item = store.get("secret_scopes", id)
        if not item:
            raise HTTPException(404, "Secret scope not found")
        access.require_project(user, item["project_id"], "manage")
        identifier, key = key_id(id, key), name(key)
        with store.lock:
            previous = store.get("notebook_secret_values", identifier)
            if previous is None:
                with store.connect() as db:
                    count = db.execute(
                        "SELECT COUNT(*) FROM documents WHERE kind='notebook_secret_values' AND project_id=? AND json_extract(body,'$.scope_id')=?",
                        (item["project_id"], id),
                    ).fetchone()[0]
                if count >= 100:
                    raise ValueError("A secret scope supports at most 100 keys")
            stamp = now()
            entry = store.put(
                "notebook_secret_values",
                {
                    "id": identifier,
                    "scope_id": id,
                    "project_id": item["project_id"],
                    "name": key,
                    "_value": SecretBox(store.directory).encrypt(body.value),
                    "version": (previous or {}).get("version", 0) + 1,
                    "created_at": (previous or {}).get("created_at", stamp),
                    "updated_at": stamp,
                },
            )
            access.audit(
                user,
                "secret.key.rotate" if previous else "secret.key.create",
                item["project_id"],
                identifier,
            )
        return {
            key: value
            for key, value in entry.items()
            if key in ("name", "version", "created_at", "updated_at")
        }

    @router.delete("/api/secret-scopes/{id}/keys/{key}")
    def delete_key(id: str, key: str, request: Request, user=Depends(authenticated)):
        store, access = request.app.state.store, request.app.state.access
        item = store.get("secret_scopes", id)
        if not item:
            raise HTTPException(404, "Secret scope not found")
        access.require_project(user, item["project_id"], "manage")
        with store.lock:
            store.delete("notebook_secret_values", key_id(id, key))
            access.audit(user, "secret.key.delete", item["project_id"], key_id(id, key))
        return {"ok": True}

    @router.delete("/api/secret-scopes/{id}")
    def delete_scope(id: str, request: Request, user=Depends(authenticated)):
        store, access = request.app.state.store, request.app.state.access
        item = store.get("secret_scopes", id)
        if not item:
            raise HTTPException(404, "Secret scope not found")
        access.require_project(user, item["project_id"], "manage")
        with store.lock, store.connect() as db:
            db.execute(
                "DELETE FROM documents WHERE kind='notebook_secret_values' AND project_id=? AND json_extract(body,'$.scope_id')=?",
                (item["project_id"], id),
            )
            db.execute(
                "DELETE FROM documents WHERE kind='secret_scopes' AND id=?", (id,)
            )
        access.audit(user, "secret.scope.delete", item["project_id"], id)
        return {"ok": True}

    @router.put("/api/notebooks/{id}/secret-refs")
    def select_keys(
        id: str, body: SecretRefsInput, request: Request, user=Depends(authenticated)
    ):
        store, access = request.app.state.store, request.app.state.access
        with store.lock:
            notebook = store.get("notebooks", id)
            if not notebook:
                raise HTTPException(404, "Notebook not found")
            access.require_notebook(user, notebook, "edit")
            if body.version is not None and body.version != notebook.get("version", 1):
                raise HTTPException(
                    409, "Notebook changed; reload before selecting secrets"
                )
            refs = validate_secret_refs(
                store,
                user,
                notebook.get("project_id") or DEFAULT_PROJECT,
                body.secret_refs,
            )
            notebook.update(
                secret_refs=refs,
                updated_at=now(),
                version=notebook.get("version", 1) + 1,
            )
            store.put("notebooks", notebook)
            access.audit(
                user, "notebook.secrets.select", notebook.get("project_id"), id
            )
        return {
            "id": id,
            "secret_refs": refs,
            "version": notebook["version"],
            "updated_at": notebook["updated_at"],
        }

    @router.get("/api/notebooks/{id}/secret-refs")
    def selected_keys(id: str, request: Request, user=Depends(authenticated)):
        store = request.app.state.store
        notebook = store.get("notebooks", id)
        if not notebook:
            raise HTTPException(404, "Notebook not found")
        request.app.state.access.require_notebook(user, notebook)
        return {
            "id": id,
            "secret_refs": normalize_refs(notebook.get("secret_refs", [])),
            "version": notebook.get("version", 1),
            "updated_at": notebook.get("updated_at"),
        }

    app.include_router(router)
