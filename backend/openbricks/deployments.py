"""Validated project deployment plans, atomic application, and exact promotion."""

from __future__ import annotations

from contextlib import ExitStack
import copy
import json
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .bundle import plan_bundle
from .permissions import now
from .revisions import SOURCE_FIELDS


class PlanInput(BaseModel):
    bundle: dict
    target: str = Field(min_length=1, max_length=80)


class DeployInput(PlanInput):
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class PromotionInput(BaseModel):
    target: str = Field(min_length=1, max_length=80)
    expected_plan_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def public_plan(plan):
    return {
        key: value
        for key, value in plan.items()
        if not key.startswith("_") and key != "bundle_owner"
    }


def put_document(db, kind, value):
    metadata = [
        row[1]
        for row in db.execute("PRAGMA table_info(documents)")
        if row[1] not in ("kind", "id", "body")
    ]
    columns = ",".join(metadata)
    assignments = ",".join(f"{column}=excluded.{column}" for column in metadata)
    placeholders = ",".join("?" for _ in range(3 + len(metadata)))
    db.execute(
        f"INSERT INTO documents(kind,id,body,{columns}) VALUES({placeholders}) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,{assignments}",
        (
            kind,
            value["id"],
            json.dumps(value, allow_nan=False),
            *(value.get(column) for column in metadata),
        ),
    )


def apply_plan(app, body, user, access, schedule_job, save_notebook_file):
    store = app.state.store
    # Match the admission lock order: automation -> workflow -> SQLite. This
    # prevents a scheduler from observing part of a multi-resource deployment.
    with ExitStack() as locks:
        for owner in (
            getattr(app.state, "automation", None),
            getattr(app.state, "workflows", None),
        ):
            if getattr(owner, "lock", None) is not None:
                locks.enter_context(owner.lock)
        locks.enter_context(store.lock)
        plan = plan_bundle(store, body.bundle, body.target, user, access)
        if body.expected_plan_hash != plan["plan_hash"]:
            raise HTTPException(
                409,
                {
                    "message": "The target changed after planning. Review a fresh plan before deploying",
                    "plan": public_plan(plan),
                },
            )
        records = plan["_records"]
        deployment = {
            "id": uuid4().hex,
            "name": plan["name"],
            "target": plan["target"],
            "project_id": plan["project_id"],
            "bundle_owner": plan["bundle_owner"],
            "plan_hash": plan["plan_hash"],
            "environment_id": plan["environment_id"],
            "status": "succeeded",
            "counts": plan["counts"],
            "created_at": now(),
            "created_by": user["id"],
            "resources": [
                {"kind": kind, "id": identifier, "key": value.get("bundle_key")}
                for (kind, identifier), value in records.items()
                if kind != "environments"
            ],
            "_bundle": plan["_bundle"],
        }
        old = {key: store.get(*key) for key in records}
        # Delivery history and source revisions are part of the same compensated
        # operation as the resources. A failed deployment cannot leave a success
        # record behind, or update notebooks without recording their revision.
        metadata = {("deployments", deployment["id"]): deployment}
        audit = {
            "id": uuid4().hex,
            "project_id": plan["project_id"],
            "actor_id": user["id"],
            "action": "bundle.deploy",
            "resource_id": deployment["id"],
            "created_at": now(),
        }
        metadata[("audit_events", audit["id"])] = audit
        revised = []
        for (kind, identifier), value in records.items():
            if (
                kind == "notebooks"
                and (old[(kind, identifier)] or {}).get("version") != value["version"]
            ):
                revision = {
                    "id": uuid4().hex,
                    "notebook_id": identifier,
                    "project_id": plan["project_id"],
                    "version": value["version"],
                    "reason": "bundle deployment",
                    "actor_id": user["id"],
                    "created_at": now(),
                    "notebook": {
                        key: copy.deepcopy(value[key])
                        for key in SOURCE_FIELDS
                        if key in value
                    },
                }
                metadata[("notebook_revisions", revision["id"])] = revision
                revised.append(identifier)
        old.update({key: None for key in metadata})
        scheduler = getattr(app.state, "scheduler", None)
        was_running = getattr(scheduler, "state", None) == 1
        if was_running:
            scheduler.pause()
        jobs = [value for (kind, _), value in records.items() if kind == "jobs"]
        old_cursors = {
            job["id"]: store.get("schedule_cursors", job["id"]) for job in jobs
        }
        files = store.directory / "notebooks"
        files.mkdir(exist_ok=True)
        restored_paths = []
        try:
            with tempfile.TemporaryDirectory(
                prefix="deployment-", dir=store.directory
            ) as temporary:
                temporary = Path(temporary)
                for (kind, identifier), value in records.items():
                    if kind != "notebooks":
                        continue
                    path = files / f"{identifier}.ipynb"
                    backup = temporary / path.name
                    if path.exists():
                        shutil.copyfile(path, backup)
                    restored_paths.append((path, backup if backup.exists() else None))
                try:
                    with store.connect() as db:
                        for (kind, _), value in {**records, **metadata}.items():
                            put_document(db, kind, value)
                    for (kind, _), value in records.items():
                        if kind == "notebooks":
                            save_notebook_file(store, value)
                    for job in jobs:
                        schedule_job(job)
                    with store.connect() as db:
                        for identifier in revised:
                            db.execute(
                                "DELETE FROM documents WHERE kind='notebook_revisions' AND notebook_id=? AND id NOT IN (SELECT id FROM documents WHERE kind='notebook_revisions' AND notebook_id=? ORDER BY created_at DESC,id DESC LIMIT 50)",
                                (identifier, identifier),
                            )
                except BaseException:
                    # Admission is still quiesced and the Store lock is held:
                    # other managed threads cannot have observed this state.
                    with store.connect() as db:
                        for key, previous in old.items():
                            if previous is None:
                                db.execute(
                                    "DELETE FROM documents WHERE kind=? AND id=?", key
                                )
                            else:
                                put_document(db, key[0], previous)
                    for path, backup in restored_paths:
                        if backup is None:
                            path.unlink(missing_ok=True)
                        else:
                            shutil.copyfile(backup, path)
                    try:
                        for job in jobs:
                            previous = old[("jobs", job["id"])]
                            if scheduler and scheduler.get_job(job["id"]):
                                scheduler.remove_job(job["id"])
                            if previous:
                                schedule_job(previous)
                    finally:
                        # Registration updates the durable missed-fire cursor.
                        # Restore it after re-registering the previous job, so a
                        # failed edit cannot skip or replay schedule occurrences.
                        with store.connect() as db:
                            for identifier, previous in old_cursors.items():
                                if previous is None:
                                    db.execute(
                                        "DELETE FROM documents WHERE kind='schedule_cursors' AND id=?",
                                        (identifier,),
                                    )
                                else:
                                    put_document(db, "schedule_cursors", previous)
                    raise
        finally:
            if was_running:
                scheduler.resume()
    return {key: value for key, value in deployment.items() if not key.startswith("_")}


def register_deployment_routes(
    app, authenticated, access, schedule_job, save_notebook_file
):
    def lookup(request, identifier, user):
        value = request.app.state.store.get("deployments", identifier)
        if not value:
            raise HTTPException(404, "Deployment not found")
        access.require_project(user, value["project_id"], "read")
        return value

    @app.post("/api/deployments/plan")
    def plan(body: PlanInput, request: Request, user=Depends(authenticated)):
        with request.app.state.store.lock:
            value = plan_bundle(
                request.app.state.store, body.bundle, body.target, user, access
            )
        return public_plan(value)

    @app.post("/api/deployments", status_code=201)
    def deploy(body: DeployInput, request: Request, user=Depends(authenticated)):
        return apply_plan(
            request.app, body, user, access, schedule_job, save_notebook_file
        )

    @app.get("/api/deployments")
    def listing(
        request: Request,
        project_id: str = "default",
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id, "read")
        store = request.app.state.store
        with store.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind='deployments' AND project_id=?",
                (project_id,),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT json_remove(body,'$._bundle','$.resources') FROM documents WHERE kind='deployments' AND project_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            ).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/deployments/{id}")
    def detail(id: str, request: Request, user=Depends(authenticated)):
        return {
            key: value
            for key, value in lookup(request, id, user).items()
            if not key.startswith("_")
        }

    @app.get("/api/deployments/{id}/export")
    def export(id: str, request: Request, user=Depends(authenticated)):
        return copy.deepcopy(lookup(request, id, user)["_bundle"])

    @app.post("/api/deployments/{id}/promotion-plan")
    def promotion_plan(
        id: str, body: PromotionInput, request: Request, user=Depends(authenticated)
    ):
        value = lookup(request, id, user)
        with request.app.state.store.lock:
            plan = plan_bundle(
                request.app.state.store, value["_bundle"], body.target, user, access
            )
        return public_plan(plan)

    @app.post("/api/deployments/{id}/promote", status_code=201)
    def promote(
        id: str, body: PromotionInput, request: Request, user=Depends(authenticated)
    ):
        value = lookup(request, id, user)
        if body.target == value["target"]:
            raise ValueError("Choose another declared target for promotion")
        if body.expected_plan_hash is None:
            raise ValueError("Review a promotion plan before applying it")
        return apply_plan(
            request.app,
            DeployInput(
                bundle=value["_bundle"],
                target=body.target,
                expected_plan_hash=body.expected_plan_hash,
            ),
            user,
            access,
            schedule_job,
            save_notebook_file,
        )
