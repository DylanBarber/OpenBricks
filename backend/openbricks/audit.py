"""Project-owner audit search and a bounded, spreadsheet-safe CSV export."""

from __future__ import annotations

import csv
import io
import json

from fastapi import Depends, HTTPException, Query, Request, Response


def register_audit_routes(app, authenticated, access):
    def records(request, user, project_id, action, actor_id, limit, offset):
        if user.get("role") == "admin":
            project_ids = [project_id] if project_id else None
        elif project_id:
            access.require_project(user, project_id, "manage")
            project_ids = [project_id]
        else:
            project_ids = access.visible_project_ids(user, "manage")
            if not project_ids:
                raise HTTPException(
                    403, "Project owner access is required to inspect audit history"
                )
        clauses, params = ["kind='audit_events'"], []
        if project_ids == []:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        if project_ids is not None:
            clauses.append(
                "COALESCE(project_id,'default') IN ("
                + ",".join("?" for _ in project_ids)
                + ")"
            )
            params.extend(project_ids)
        if action:
            clauses.append("json_extract(body,'$.action')=?")
            params.append(action)
        if actor_id:
            clauses.append("json_extract(body,'$.actor_id')=?")
            params.append(actor_id)
        condition = " AND ".join(clauses)
        store = request.app.state.store
        with store.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE " + condition, params
            ).fetchone()[0]
            rows = db.execute(
                "SELECT body FROM documents WHERE "
                + condition
                + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        items = [json.loads(row[0]) for row in rows]
        for item in items:
            actor = store.get("users", item.get("actor_id", ""))
            item["actor_name"] = actor.get("username") if actor else "Deleted account"
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get("/api/audit")
    def audit(
        request: Request,
        project_id: str | None = Query(None, max_length=80),
        action: str = Query("", max_length=120),
        actor_id: str = Query("", max_length=80),
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        return records(request, user, project_id, action, actor_id, limit, offset)

    @app.get("/api/audit/export")
    def export(
        request: Request,
        project_id: str | None = Query(None, max_length=80),
        action: str = Query("", max_length=120),
        actor_id: str = Query("", max_length=80),
        limit: int = Query(1000, ge=1, le=10000),
        user=Depends(authenticated),
    ):
        result = records(request, user, project_id, action, actor_id, limit, 0)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        columns = (
            "created_at",
            "project_id",
            "actor_id",
            "actor_name",
            "action",
            "resource_id",
        )
        writer.writerow(columns)
        for item in result["items"]:
            values = [str(item.get(key) or "")[:4000] for key in columns]
            writer.writerow(
                [
                    (
                        "'" + value
                        if value.lstrip(" \t\r\n\ufeff").startswith(
                            ("=", "+", "-", "@")
                        )
                        or value.startswith(("\t", "\r", "\n"))
                        else value
                    )
                    for value in values
                ]
            )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="openbricks-audit.csv"',
                "X-OpenBricks-Export-Truncated": str(result["total"] > limit).lower(),
            },
        )
