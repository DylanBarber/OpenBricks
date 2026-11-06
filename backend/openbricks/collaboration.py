"""Durable discussions/reviews and bounded, leased cell coediting."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import secrets
import time
from threading import RLock
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .permissions import DEFAULT_PROJECT
from .revisions import record_revision
from .runtime import now


def source_hash(value):
    if isinstance(value, str):
        return hashlib.sha256(value.encode()).hexdigest()
    return hashlib.sha256(
        json.dumps(
            {
                key: value.get(key)
                for key in (
                    "name",
                    "description",
                    "language",
                    "cells",
                    "parameter_definitions",
                )
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class PresenceInput(BaseModel):
    client_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    cell_id: str | None = Field(default=None, max_length=80)


class LeaseInput(PresenceInput):
    cell_id: str = Field(min_length=1, max_length=80)


class CellEdit(LeaseInput):
    token: str = Field(min_length=20, max_length=100)
    source: str = Field(max_length=1_000_000)
    base_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class EnableInput(BaseModel):
    enabled: bool
    version: int = Field(ge=1)


class CommentInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    parent_id: str | None = Field(default=None, max_length=80)
    cell_id: str | None = Field(default=None, max_length=80)


class CommentChange(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    resolved: bool | None = None


class ReviewInput(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=80)
    message: str = Field(default="", max_length=4000)


class ReviewDecision(BaseModel):
    decision: str = Field(pattern=r"^(approved|changes_requested)$")
    message: str = Field(default="", max_length=4000)


class Collaboration:
    def __init__(self, store, clock=time.monotonic):
        self.store, self.clock = store, clock
        self.lock = RLock()
        self.presence, self.leases = {}, {}

    def prune(self):
        current = self.clock()
        self.presence = {
            key: value
            for key, value in self.presence.items()
            if value["until"] > current
        }
        self.leases = {
            key: value for key, value in self.leases.items() if value["until"] > current
        }

    def heartbeat(self, notebook, user, body, can_edit=True):
        with self.lock:
            self.prune()
            key = (notebook["id"], user["id"], body.client_id)
            if key not in self.presence:
                if (
                    len(self.presence) >= 5000
                    or sum(k[1] == user["id"] for k in self.presence) >= 20
                ):
                    raise HTTPException(429, "Too many open collaboration sessions")
            self.presence[key] = {
                "user_id": user["id"],
                "username": user["username"],
                "client_id": body.client_id,
                "cell_id": body.cell_id,
                "until": self.clock() + 45,
            }
            for (book, cell), lease in list(self.leases.items()):
                if (
                    book == notebook["id"]
                    and lease["user_id"] == user["id"]
                    and lease["client_id"] == body.client_id
                ):
                    if can_edit:
                        lease["until"] = self.clock() + 30
                    else:
                        self.leases.pop((book, cell), None)

    def acquire(self, notebook, user, body):
        with self.lock:
            self.prune()
            key = notebook["id"], body.cell_id
            lease = self.leases.get(key)
            if lease and (lease["user_id"], lease["client_id"]) != (
                user["id"],
                body.client_id,
            ):
                raise HTTPException(
                    409,
                    "A teammate is editing this cell. Choose another cell or wait for the lease to expire",
                )
            if lease is None:
                if len(self.leases) >= 5000:
                    raise HTTPException(429, "Too many active editing leases")
                lease = {
                    "token": secrets.token_urlsafe(32),
                    "user_id": user["id"],
                    "username": user["username"],
                    "client_id": body.client_id,
                    "cell_id": body.cell_id,
                }
                self.leases[key] = lease
            lease["until"] = self.clock() + 30
            return {"token": lease["token"], "expires_in_seconds": 30}

    def authorize_lease(self, id, user, body):
        self.prune()
        lease = self.leases.get((id, body.cell_id))
        if (
            not lease
            or (lease["user_id"], lease["client_id"]) != (user["id"], body.client_id)
            or not secrets.compare_digest(lease["token"], body.token)
        ):
            raise HTTPException(
                409,
                "Editing lease expired. Reconnect and acquire this cell again; your unsaved text is retained locally",
            )
        return lease


def register_collaboration_routes(app, authenticated, access, save_notebook_file):
    def book(request, user, id, action="read"):
        value = request.app.state.store.get("notebooks", id)
        if not value:
            raise HTTPException(404, "Notebook not found")
        access.require_notebook(user, value, action)
        return value

    def manager(request):
        return request.app.state.collaboration

    def room(request, user, notebook, known_version=None):
        service = manager(request)
        with service.lock:
            service.prune()
            presence = [
                {key: value for key, value in item.items() if key != "until"}
                for (id, _, _), item in service.presence.items()
                if id == notebook["id"]
            ]
            leases = [
                {
                    key: value
                    for key, value in item.items()
                    if key not in ("token", "until")
                }
                for (id, _), item in service.leases.items()
                if id == notebook["id"]
            ]

        # Never acquire the store lock while holding the room lock: writers
        # consistently acquire store, then room, in that order.
        def visible(value):
            actor = service.store.get("users", value["user_id"])
            return actor and access.allowed(
                actor, notebook.get("project_id") or DEFAULT_PROJECT, "read"
            )

        presence = [item for item in presence if visible(item)]
        leases = [item for item in leases if visible(item)]
        version = notebook.get("version", 1)
        return {
            "enabled": bool(notebook.get("collaboration_enabled")),
            "version": version,
            "presence": presence,
            "leases": leases,
            "notebook": (
                {
                    key: value
                    for key, value in notebook.items()
                    if not key.startswith("_")
                }
                if known_version != version
                else None
            ),
        }

    @app.get("/api/notebooks/{id}/collaboration")
    def state(
        id: str,
        request: Request,
        known_version: int | None = Query(None, ge=1),
        user=Depends(authenticated),
    ):
        return room(request, user, book(request, user, id), known_version)

    @app.put("/api/notebooks/{id}/collaboration")
    def enable(
        id: str, body: EnableInput, request: Request, user=Depends(authenticated)
    ):
        store, service = request.app.state.store, manager(request)
        with store.lock, service.lock:
            value = book(request, user, id, "edit")
            if value.get("version", 1) != body.version:
                raise HTTPException(
                    409, "Notebook changed. Reload before changing collaboration mode"
                )
            service.prune()
            if not body.enabled and any(key[0] == id for key in service.leases):
                raise HTTPException(
                    409,
                    "Finish and release all cell edits before leaving live collaboration",
                )
            value.update(
                collaboration_enabled=body.enabled,
                updated_at=now(),
                version=value.get("version", 1) + 1,
            )
            with store.live_notebook_edit(id):
                store.put("notebooks", value)
            save_notebook_file(store, value)
        access.audit(
            user,
            "collaboration.enable" if body.enabled else "collaboration.disable",
            value.get("project_id"),
            id,
        )
        return room(request, user, value)

    @app.post("/api/notebooks/{id}/presence")
    def heartbeat(
        id: str, body: PresenceInput, request: Request, user=Depends(authenticated)
    ):
        value = book(request, user, id)
        if body.cell_id and not any(
            c["id"] == body.cell_id for c in value.get("cells", [])
        ):
            raise HTTPException(404, "Cell not found")
        manager(request).heartbeat(
            value,
            user,
            body,
            access.allowed(user, value.get("project_id") or DEFAULT_PROJECT, "edit"),
        )
        return {"ok": True, "expires_in_seconds": 45}

    @app.post("/api/notebooks/{id}/collaboration/lease")
    def lease(id: str, body: LeaseInput, request: Request, user=Depends(authenticated)):
        store, service = request.app.state.store, manager(request)
        with store.lock, service.lock:
            value = book(request, user, id, "edit")
            if not value.get("collaboration_enabled"):
                raise HTTPException(409, "Enable live collaboration first")
            if not any(cell["id"] == body.cell_id for cell in value["cells"]):
                raise HTTPException(404, "Cell not found")
            previous = service.leases.get((id, body.cell_id))
            if previous:
                actor = store.get("users", previous["user_id"])
                if (
                    not actor
                    or actor.get("disabled")
                    or not access.allowed(
                        actor, value.get("project_id") or DEFAULT_PROJECT, "edit"
                    )
                ):
                    service.leases.pop((id, body.cell_id), None)
            result = service.acquire(value, user, body)
            result["source_hash"] = source_hash(
                next(c["source"] for c in value["cells"] if c["id"] == body.cell_id)
            )
            return result

    @app.patch("/api/notebooks/{id}/collaboration/cell")
    def edit_cell(
        id: str, body: CellEdit, request: Request, user=Depends(authenticated)
    ):
        store, service = request.app.state.store, manager(request)
        with store.lock, service.lock:
            value = book(request, user, id, "edit")
            if not value.get("collaboration_enabled"):
                raise HTTPException(409, "Live collaboration is disabled")
            lease = service.authorize_lease(id, user, body)
            cell = next(
                (item for item in value["cells"] if item["id"] == body.cell_id), None
            )
            if cell is None:
                raise HTTPException(404, "Cell not found")
            if source_hash(cell["source"]) != body.base_hash:
                raise HTTPException(
                    409,
                    "Cell changed since your last save. Keep your draft and reload the saved cell",
                )
            if cell["source"] != body.source:
                from .notebooks import validate_source_size

                validate_source_size(
                    [
                        (
                            {**item, "source": body.source}
                            if item["id"] == body.cell_id
                            else item
                        )
                        for item in value["cells"]
                    ]
                )
                cell["source"] = body.source
                value.update(version=value.get("version", 1) + 1, updated_at=now())
                with store.live_notebook_edit(id):
                    store.put("notebooks", value)
                save_notebook_file(store, value)
                # Save meaningful checkpoints, rather than exhausting history on every keystroke.
                if (
                    not lease.get("last_revision")
                    or service.clock() - lease["last_revision"] >= 60
                ):
                    record_revision(
                        store, value, "live collaboration checkpoint", user["id"]
                    )
                    lease["last_revision"] = service.clock()
                    access.audit(
                        user, "notebook.live_edit", value.get("project_id"), id
                    )
            lease["until"] = service.clock() + 30
            return {
                "version": value["version"],
                "source_hash": source_hash(body.source),
                "updated_at": value["updated_at"],
            }

    @app.post("/api/notebooks/{id}/collaboration/release")
    def release(id: str, body: CellEdit, request: Request, user=Depends(authenticated)):
        book(request, user, id, "edit")
        service = manager(request)
        with service.lock:
            service.authorize_lease(id, user, body)
            service.leases.pop((id, body.cell_id), None)
        return {"ok": True}

    def page(request, value, kind, limit, offset):
        project = value.get("project_id") or DEFAULT_PROJECT
        with request.app.state.store.connect() as db:
            condition, params = (
                "kind=? AND notebook_id=? AND COALESCE(project_id,'default')=?",
                (kind, value["id"], project),
            )
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE " + condition, params
            ).fetchone()[0]
            rows = db.execute(
                "SELECT body FROM documents WHERE "
                + condition
                + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/notebooks/{id}/comments")
    def comments(
        id: str,
        request: Request,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        return page(
            request, book(request, user, id), "notebook_comments", limit, offset
        )

    @app.post("/api/notebooks/{id}/comments", status_code=201)
    def comment(
        id: str, body: CommentInput, request: Request, user=Depends(authenticated)
    ):
        value = book(request, user, id, "edit")
        store = request.app.state.store
        if not body.text.strip():
            raise ValueError("Comment text is required")
        if body.cell_id and not any(c["id"] == body.cell_id for c in value["cells"]):
            raise HTTPException(404, "Cell not found")
        if body.parent_id:
            parent = store.get("notebook_comments", body.parent_id)
            if (
                not parent
                or parent.get("notebook_id") != id
                or parent.get("project_id") != value.get("project_id", DEFAULT_PROJECT)
            ):
                raise HTTPException(404, "Thread not found")
            if parent.get("parent_id"):
                raise ValueError(
                    "Reply to the root comment; threads have one reply level"
                )
        item = {
            "id": uuid4().hex,
            "project_id": value.get("project_id") or DEFAULT_PROJECT,
            "notebook_id": id,
            "author_id": user["id"],
            "author_name": user["username"],
            "text": body.text.strip(),
            "parent_id": body.parent_id,
            "cell_id": body.cell_id,
            "resolved": False,
            "created_at": now(),
            "updated_at": now(),
        }
        store.put("notebook_comments", item)
        access.audit(user, "comment.create", item["project_id"], item["id"])
        return item

    @app.patch("/api/notebooks/{id}/comments/{comment_id}")
    def change_comment(
        id: str,
        comment_id: str,
        body: CommentChange,
        request: Request,
        user=Depends(authenticated),
    ):
        store = request.app.state.store
        with store.lock:
            value = book(request, user, id, "edit")
            item = store.get("notebook_comments", comment_id)
            if (
                not item
                or item.get("notebook_id") != id
                or item.get("project_id")
                != (value.get("project_id") or DEFAULT_PROJECT)
            ):
                raise HTTPException(404, "Comment not found")
            if body.text is not None:
                if item["author_id"] != user["id"]:
                    raise HTTPException(403, "Only the author may edit comment text")
                if not body.text.strip():
                    raise ValueError("Comment text is required")
                item["text"] = body.text.strip()
            if body.resolved is not None:
                if item.get("parent_id"):
                    raise ValueError("Resolve the root thread")
                item["resolved"] = body.resolved
            item["updated_at"] = now()
            store.put("notebook_comments", item)
        access.audit(user, "comment.update", item["project_id"], item["id"])
        return item

    @app.get("/api/notebooks/{id}/reviews")
    def reviews(
        id: str,
        request: Request,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        value = book(request, user, id)
        result = page(request, value, "notebook_reviews", limit, offset)
        for item in result["items"]:
            item["stale"] = item["source_hash"] != source_hash(value)
        return result

    @app.post("/api/notebooks/{id}/reviews", status_code=201)
    def request_review(
        id: str, body: ReviewInput, request: Request, user=Depends(authenticated)
    ):
        store = request.app.state.store
        with store.lock:
            value = book(request, user, id, "edit")
            reviewer = store.get("users", body.reviewer_id)
            if (
                not reviewer
                or reviewer.get("is_service_account")
                or not access.allowed(
                    reviewer, value.get("project_id") or DEFAULT_PROJECT, "edit"
                )
            ):
                raise ValueError("Choose a project editor or owner as reviewer")
            if reviewer["id"] == user["id"]:
                raise ValueError("Choose another teammate to review your notebook")
            item = {
                "id": uuid4().hex,
                "project_id": value.get("project_id") or DEFAULT_PROJECT,
                "notebook_id": id,
                "requester_id": user["id"],
                "requester_name": user["username"],
                "reviewer_id": reviewer["id"],
                "reviewer_name": reviewer["username"],
                "message": body.message,
                "status": "open",
                "created_at": now(),
                "source_hash": source_hash(value),
                "notebook_version": value.get("version", 1),
            }
            store.put("notebook_reviews", item)
        access.audit(user, "review.request", item["project_id"], item["id"])
        return item

    @app.post("/api/notebooks/{id}/reviews/{review_id}/decision")
    def decide(
        id: str,
        review_id: str,
        body: ReviewDecision,
        request: Request,
        user=Depends(authenticated),
    ):
        store = request.app.state.store
        with store.lock:
            value = book(request, user, id, "edit")
            item = store.get("notebook_reviews", review_id)
            if (
                not item
                or item.get("notebook_id") != id
                or item.get("project_id")
                != (value.get("project_id") or DEFAULT_PROJECT)
            ):
                raise HTTPException(404, "Review not found")
            if item["reviewer_id"] != user["id"]:
                raise HTTPException(
                    403, "Only the assigned reviewer may decide this review"
                )
            if item["status"] != "open":
                raise HTTPException(409, "This review already has a decision")
            if item["source_hash"] != source_hash(value):
                raise HTTPException(
                    409,
                    "Notebook source changed. Request a new review of the current source",
                )
            item.update(
                status=body.decision, decision_message=body.message, decided_at=now()
            )
            store.put("notebook_reviews", item)
        access.audit(user, "review." + body.decision, item["project_id"], item["id"])
        return {**item, "stale": False}
