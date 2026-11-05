"""Notebook source revisions and safe source formatting."""
from __future__ import annotations

import json
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .permissions import DEFAULT_PROJECT, now

SOURCE_FIELDS = ("name", "description", "language", "cells", "parameter_definitions")


def record_revision(store, notebook, reason="saved", actor_id=None):
    snapshot = {key: notebook[key] for key in SOURCE_FIELDS if key in notebook}
    revision = {"id": uuid4().hex, "notebook_id": notebook["id"], "project_id": notebook.get("project_id") or DEFAULT_PROJECT,
                "version": notebook.get("version", 1), "reason": reason, "actor_id": actor_id, "created_at": now(), "notebook": snapshot}
    store.put("notebook_revisions", revision)
    # Keep a bounded source history independently of run-output retention.
    with store.connect() as db:
        db.execute("DELETE FROM documents WHERE kind='notebook_revisions' AND notebook_id=? AND id NOT IN (SELECT id FROM documents WHERE kind='notebook_revisions' AND notebook_id=? ORDER BY created_at DESC,id DESC LIMIT 50)", (notebook["id"], notebook["id"]))
    return revision


class FormatInput(BaseModel):
    language: str = "python"
    source: str = Field(max_length=200_000)
    project_id: str = DEFAULT_PROJECT


def register_revision_routes(app, authenticated, access, save_notebook_file):
    def notebook(request, user, id, action="read"):
        value = request.app.state.store.get("notebooks", id)
        if not value:
            raise HTTPException(404, "Notebook not found")
        access.require_notebook(user, value, action)
        return value

    @app.get("/api/notebooks/{id}/revisions")
    def revisions(id: str, request: Request, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0), user=Depends(authenticated)):
        notebook(request, user, id)
        store = request.app.state.store
        with store.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM documents WHERE kind='notebook_revisions' AND notebook_id=?", (id,)).fetchone()[0]
            rows = db.execute("SELECT json_remove(body,'$.notebook') FROM documents WHERE kind='notebook_revisions' AND notebook_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (id, limit, offset)).fetchall()
        return {"items": [json.loads(row[0]) for row in rows], "total": total, "limit": limit, "offset": offset}

    @app.get("/api/notebooks/{id}/revisions/{revision_id}")
    def revision(id: str, revision_id: str, request: Request, user=Depends(authenticated)):
        notebook(request, user, id)
        value = request.app.state.store.get("notebook_revisions", revision_id)
        if not value or value["notebook_id"] != id:
            raise HTTPException(404, "Revision not found")
        return value

    @app.post("/api/notebooks/{id}/revisions/{revision_id}/restore")
    def restore(id: str, revision_id: str, request: Request, user=Depends(authenticated)):
        store = request.app.state.store
        with store.lock:
            current = notebook(request, user, id, "edit")
            selected = store.get("notebook_revisions", revision_id)
            if not selected or selected["notebook_id"] != id:
                raise HTTPException(404, "Revision not found")
            restored = {"description": "", "language": "python", "parameter_definitions": [],
                        **{key: value for key, value in selected["notebook"].items() if key in SOURCE_FIELDS}}
            current.update(restored,
                           version=current.get("version", 1) + 1, updated_at=now())
            store.put("notebooks", current)
            save_notebook_file(store, current)
            record_revision(store, current, f"restored version {selected.get('version', 1)}", user["id"])
        access.audit(user, "notebook.restore", current.get("project_id"), id)
        return {key: value for key, value in current.items() if not key.startswith("_")}

    @app.post("/api/editor/format")
    def format_source(body: FormatInput, user=Depends(authenticated)):
        access.require_project(user, body.project_id, "edit")
        if body.language != "python":
            raise ValueError("This formatter supports Python source")
        import black
        try:
            formatted = black.format_file_contents(body.source, fast=False, mode=black.Mode(target_versions={black.TargetVersion.PY312}))
        except black.NothingChanged:
            formatted = body.source
        except black.InvalidInput as exc:
            raise HTTPException(422, f"Python source cannot be formatted: {exc}") from exc
        return {"source": formatted}
