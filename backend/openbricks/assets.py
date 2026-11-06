"""Bounded, project-scoped data asset health and observed dependency graphs.

These views read existing evidence. They neither query a dataset nor certify
that arbitrary external writes were committed atomically with a notebook run.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .permissions import now
from .store import backfill_lineage_metadata

QUALITY_RUN_WINDOW = 500
QUALITY_REPORT_LIMIT = 1000
GRAPH_EDGE_LIMIT = 500


def instant(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


def dataset_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError("Choose a dataset name of at most 1,000 characters")
    value = value.strip()
    if any(ord(character) < 32 for character in value):
        raise ValueError("Dataset names cannot contain control characters")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Dataset locations cannot contain credentials, queries or fragments"
            )
    return value


def asset_id(project_id, dataset):
    return hashlib.sha256(
        json.dumps([project_id, dataset], separators=(",", ":")).encode()
    ).hexdigest()


def scalar(value, limit=500):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return value[:limit] if isinstance(value, str) else None


def quality_record(run_id, status, observed_at, report):
    checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    return {
        "passed": (
            report.get("passed") if isinstance(report.get("passed"), bool) else None
        ),
        "row_count": scalar(report.get("row_count")),
        "checks": [
            {
                key: scalar(item.get(key))
                for key in ("name", "type", "column", "passed", "failed_rows", "actual")
            }
            for item in checks[:100]
            if isinstance(item, dict)
        ],
        "run_id": run_id,
        "run_status": status,
        "observed_at": observed_at,
        "live": False,
    }


class AssetInput(BaseModel):
    project_id: str = "default"
    dataset: str = Field(min_length=1, max_length=1000)
    description: str = Field(default="", max_length=4000)
    owner_id: str | None = None
    freshness_seconds: int | None = Field(default=None, ge=60, le=31_536_000)


class AssetService:
    def __init__(self, store, access):
        self.store, self.access = store, access
        with store.connect() as db:
            # SQLite 3.40 (the packaged Debian runtime) rejects JSON expression
            # indexes when an untrusted backup is checked with trusted_schema=OFF.
            # Ordinary scalar metadata keeps the same lookup keys and permits
            # that strict validation. Never enable trusted_schema to restore.
            needs_backfill = False
            for index in ("asset_lineage_dataset", "asset_lineage_run"):
                previous = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                    (index,),
                ).fetchone()
                if not previous:
                    needs_backfill = True
                elif "json_extract" in (previous[0] or "").lower():
                    db.execute(f"DROP INDEX {index}")
                    needs_backfill = True
            if needs_backfill:
                backfill_lineage_metadata(db)
            db.execute(
                "CREATE INDEX IF NOT EXISTS asset_lineage_dataset ON documents(kind,COALESCE(project_id,'default'),name,created_at DESC,id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS asset_lineage_run ON documents(kind,COALESCE(project_id,'default'),parent_id,notebook_name)"
            )

    def _datasets_sql(self):
        # Metadata only: no run outputs or notebook sources enter Python.
        return """WITH datasets AS (
            SELECT name AS dataset,MAX(created_at) AS last_observed_at
            FROM documents WHERE kind='data_lineage' AND COALESCE(project_id,'default')=?
            GROUP BY name
            UNION ALL
            SELECT name AS dataset,updated_at AS last_observed_at FROM documents
            WHERE kind='asset_annotations' AND COALESCE(project_id,'default')=?
        ), grouped AS (SELECT dataset,MAX(last_observed_at) AS last_observed_at FROM datasets
            WHERE dataset IS NOT NULL GROUP BY dataset) """

    def _known_dataset(self, project_id, identifier):
        annotation = self.store.get("asset_annotations", identifier)
        if annotation and annotation.get("project_id") == project_id:
            return annotation["dataset"]
        with self.store.connect() as db:
            # The opaque public ID is a digest, so an unannotated legacy asset
            # is resolved using a bounded scalar stream rather than whole runs.
            cursor = db.execute(
                self._datasets_sql() + "SELECT dataset FROM grouped",
                (project_id, project_id),
            )
            for (dataset,) in cursor:
                if asset_id(project_id, dataset) == identifier:
                    return dataset
        raise HTTPException(404, "Data asset not found in this project")

    def _qualities(self, project_id, datasets):
        if not datasets:
            return {}
        placeholders = ",".join("?" for _ in datasets)
        query = (
            """WITH recent AS (
            SELECT id,status,created_at,finished_at,CASE WHEN json_type(body,'$.quality_reports')='array' THEN json_extract(body,'$.quality_reports') ELSE '[]' END AS reports
            FROM documents WHERE kind='runs' AND COALESCE(project_id,'default')=?
            ORDER BY created_at DESC,id DESC LIMIT ?)
            SELECT recent.id,recent.status,COALESCE(recent.finished_at,recent.created_at),report.value
            FROM recent,json_each(recent.reports) report
            WHERE CASE WHEN report.type='object' THEN json_extract(report.value,'$.dataset') END IN ("""
            + placeholders
            + ") ORDER BY recent.created_at DESC,recent.id DESC LIMIT ?"
        )
        with self.store.connect() as db:
            rows = db.execute(
                query, (project_id, QUALITY_RUN_WINDOW, *datasets, QUALITY_REPORT_LIMIT)
            ).fetchall()
        result = {}
        for run_id, status, observed_at, raw in rows:
            report = json.loads(raw)
            dataset = report.get("dataset")
            if dataset not in result:
                result[dataset] = quality_record(run_id, status, observed_at, report)
        return result

    def quality_page(self, project_id, identifier, limit=25, offset=0):
        dataset = self._known_dataset(project_id, identifier)
        prefix = """WITH recent AS (
            SELECT id,status,created_at,finished_at,CASE WHEN json_type(body,'$.quality_reports')='array'
                THEN json_extract(body,'$.quality_reports') ELSE '[]' END AS reports
            FROM documents WHERE kind='runs' AND COALESCE(project_id,'default')=?
            ORDER BY created_at DESC,id DESC LIMIT ?), observed AS (
            SELECT recent.id,recent.status,COALESCE(recent.finished_at,recent.created_at) AS observed_at,
                report.value AS report,report.key AS report_index
            FROM recent,json_each(recent.reports) report
            WHERE CASE WHEN report.type='object' THEN json_extract(report.value,'$.dataset') END=?
            ORDER BY recent.created_at DESC,recent.id DESC,report.key DESC LIMIT ?) """
        arguments = (project_id, QUALITY_RUN_WINDOW, dataset, QUALITY_REPORT_LIMIT)
        with self.store.connect() as db:
            total = db.execute(
                prefix + "SELECT COUNT(*) FROM observed", arguments
            ).fetchone()[0]
            rows = db.execute(
                prefix
                + "SELECT id,status,observed_at,report,report_index FROM observed LIMIT ? OFFSET ?",
                (*arguments, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    **quality_record(run_id, status, observed_at, json.loads(report)),
                    "id": run_id + ":" + str(index),
                }
                for run_id, status, observed_at, report, index in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "evidence": {
                "quality_run_window": QUALITY_RUN_WINDOW,
                "quality_report_limit": QUALITY_REPORT_LIMIT,
                "live_queries": False,
            },
        }

    def nodes(self, project_id, datasets):
        datasets = list(dict.fromkeys(datasets))[:100]
        qualities = self._qualities(project_id, datasets)
        result = []
        clock = datetime.now(timezone.utc)
        with self.store.connect() as db:
            for raw_dataset in datasets:
                try:
                    dataset = dataset_name(raw_dataset)
                except ValueError:
                    continue
                identifier = asset_id(project_id, dataset)
                annotation = self.store.get("asset_annotations", identifier) or {}
                observations = db.execute(
                    """SELECT l.created_at,l.notebook_name,l.parent_id,r.status
                    FROM documents l LEFT JOIN documents r ON r.kind='runs' AND r.id=l.parent_id
                        AND COALESCE(r.project_id,'default')=COALESCE(l.project_id,'default')
                    WHERE l.kind='data_lineage' AND COALESCE(l.project_id,'default')=? AND l.name=?
                    ORDER BY l.created_at DESC,l.id DESC LIMIT 100""",
                    (project_id, dataset),
                ).fetchall()
                reads = [item for item in observations if item[1] == "read"]
                writes = [item for item in observations if item[1] == "write"]
                successes = [item for item in writes if item[3] == "succeeded"]
                last_write = successes[0][0] if successes else None
                observed = instant(last_write)
                age = max(0, (clock - observed).total_seconds()) if observed else None
                threshold = annotation.get("freshness_seconds")
                freshness = (
                    "unknown"
                    if age is None or threshold is None
                    else "fresh" if age <= threshold else "stale"
                )
                quality = qualities.get(dataset)
                status = (
                    "failed"
                    if quality and quality.get("passed") is False
                    else (
                        "warning"
                        if freshness == "stale"
                        else (
                            "healthy"
                            if quality and quality.get("passed") is True
                            else "unknown"
                        )
                    )
                )
                owner = (
                    self.store.get("users", annotation.get("owner_id"))
                    if annotation.get("owner_id")
                    else None
                )
                # A removed project member must not remain a visible owner.
                if owner and (
                    owner.get("disabled")
                    or owner.get("enabled") is False
                    or not self.access.allowed(owner, project_id)
                ):
                    owner = None
                result.append(
                    {
                        "id": identifier,
                        "project_id": project_id,
                        "dataset": dataset,
                        "description": annotation.get("description", ""),
                        "owner_id": owner["id"] if owner else None,
                        "owner": (
                            {
                                "id": owner["id"],
                                "username": owner.get("username", owner["id"]),
                            }
                            if owner
                            else None
                        ),
                        "freshness_seconds": threshold,
                        "health": status,
                        "quality": quality,
                        "freshness": {
                            "status": freshness,
                            "age_seconds": age,
                            "threshold_seconds": threshold,
                            "observed_at": last_write,
                            "basis": "last_recorded_successful_write",
                            "live": False,
                        },
                        "last_read_at": reads[0][0] if reads else None,
                        "last_write_at": writes[0][0] if writes else None,
                        "last_writer_run_id": writes[0][2] if writes else None,
                        "last_writer_status": writes[0][3] if writes else None,
                        "observation_limit": 100,
                    }
                )
        return result

    def page(self, project_id, search="", limit=25, offset=0):
        where = " WHERE instr(lower(dataset),lower(?))>0"
        with self.store.connect() as db:
            total = db.execute(
                self._datasets_sql() + "SELECT COUNT(*) FROM grouped" + where,
                (project_id, project_id, search),
            ).fetchone()[0]
            values = db.execute(
                self._datasets_sql()
                + "SELECT dataset FROM grouped"
                + where
                + " ORDER BY last_observed_at DESC,dataset LIMIT ? OFFSET ?",
                (project_id, project_id, search, limit, offset),
            ).fetchall()
        return {
            "items": self.nodes(project_id, [row[0] for row in values]),
            "total": total,
            "limit": limit,
            "offset": offset,
            "evidence": {
                "quality_run_window": QUALITY_RUN_WINDOW,
                "quality_report_limit": QUALITY_REPORT_LIMIT,
                "live_queries": False,
            },
        }

    def detail(self, project_id, identifier):
        values = self.nodes(project_id, [self._known_dataset(project_id, identifier)])
        if not values:
            raise HTTPException(404, "Data asset identifier is unavailable")
        return values[0]

    def save(self, identifier, body, user):
        dataset = dataset_name(body.dataset)
        self.access.require_project(user, body.project_id, "edit")
        if identifier != asset_id(body.project_id, dataset):
            raise ValueError("The asset ID must match this project and dataset")
        if body.owner_id:
            owner = self.store.get("users", body.owner_id)
            if (
                not owner
                or owner.get("disabled")
                or owner.get("enabled") is False
                or not self.access.allowed(owner, body.project_id)
            ):
                raise ValueError("Choose an active member of this project as the owner")
        old = self.store.get("asset_annotations", identifier)
        value = {
            **body.model_dump(),
            "dataset": dataset,
            "name": dataset,
            "id": identifier,
            "created_at": (old or {}).get("created_at", now()),
            "updated_at": now(),
        }
        self.store.put("asset_annotations", value)
        self.access.audit(user, "asset.annotate", body.project_id, identifier)
        return self.nodes(body.project_id, [dataset])[0]

    def graph(self, project_id, dataset=None, direction="both", depth=1, max_nodes=100):
        if dataset:
            datasets = {dataset_name(dataset)}
            truncated = False
        else:
            page = self.page(project_id, limit=max_nodes)
            datasets = {item["dataset"] for item in page["items"]}
            truncated = page["total"] > max_nodes
        frontier, edges = set(datasets), {}
        if len(datasets) > max_nodes:
            datasets = set(sorted(datasets)[:max_nodes])
        for _ in range(depth):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            base = """SELECT r.name,w.name,
                    r.parent_id,MAX(r.created_at,w.created_at),run.status
                FROM documents r JOIN documents w ON w.kind='data_lineage'
                    AND COALESCE(w.project_id,'default')=COALESCE(r.project_id,'default')
                    AND w.parent_id=r.parent_id
                LEFT JOIN documents run ON run.kind='runs' AND run.id=r.parent_id
                    AND COALESCE(run.project_id,'default')=COALESCE(r.project_id,'default')
                WHERE r.kind='data_lineage' AND COALESCE(r.project_id,'default')=?
                    AND r.notebook_name='read' AND w.notebook_name='write'
                    AND """
            if direction == "upstream":
                condition, values = (
                    "w.name IN (" + placeholders + ")",
                    [*frontier],
                )
            elif direction == "downstream":
                condition, values = (
                    "r.name IN (" + placeholders + ")",
                    [*frontier],
                )
            else:
                condition = (
                    "(r.name IN ("
                    + placeholders
                    + ") OR w.name IN ("
                    + placeholders
                    + "))"
                )
                values = [*frontier, *frontier]
            with self.store.connect() as db:
                rows = db.execute(
                    base + condition + " ORDER BY 4 DESC LIMIT ?",
                    (project_id, *values, GRAPH_EDGE_LIMIT + 1),
                ).fetchall()
            truncated |= len(rows) > GRAPH_EDGE_LIMIT
            next_frontier = set()
            for source, target, run_id, observed_at, status in rows[:GRAPH_EDGE_LIMIT]:
                try:
                    source, target = dataset_name(source), dataset_name(target)
                except ValueError:
                    continue
                missing = {source, target} - datasets
                if (
                    len(datasets) + len(missing) > max_nodes
                    or len(edges) >= GRAPH_EDGE_LIMIT
                ):
                    truncated = True
                    continue
                datasets.update(missing)
                next_frontier.update(missing)
                key = (source, target)
                if key not in edges:
                    edges[key] = {
                        "source": asset_id(project_id, source),
                        "target": asset_id(project_id, target),
                        "run_id": run_id,
                        "run_status": status,
                        "last_observed_at": observed_at,
                        "inferred": True,
                        "evidence": "same_run_read_write",
                    }
            frontier = next_frontier
        return {
            "nodes": self.nodes(project_id, sorted(datasets)),
            "edges": list(edges.values()),
            "truncated": truncated,
            "depth": depth,
            "direction": direction,
            "evidence": {
                "quality_run_window": QUALITY_RUN_WINDOW,
                "quality_report_limit": QUALITY_REPORT_LIMIT,
                "edge_limit": GRAPH_EDGE_LIMIT,
                "live_queries": False,
                "meaning": "Read/write observations in the same run; failed runs remain observations, not certified commits.",
            },
        }


def register_asset_routes(app, authenticated, access):
    def service(request):
        return request.app.state.assets

    @app.get("/api/assets")
    def list_assets(
        request: Request,
        project_id: str = "default",
        search: str = Query("", max_length=200),
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return service(request).page(project_id, search, limit, offset)

    @app.get("/api/assets/graph")
    def graph(
        request: Request,
        project_id: str = "default",
        dataset: str | None = Query(None, max_length=1000),
        direction: str = Query("both", pattern="^(both|upstream|downstream)$"),
        depth: int = Query(1, ge=1, le=2),
        max_nodes: int = Query(100, ge=1, le=100),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return service(request).graph(project_id, dataset, direction, depth, max_nodes)

    @app.get("/api/assets/{identifier}")
    def detail(
        identifier: str,
        request: Request,
        project_id: str = "default",
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return service(request).detail(project_id, identifier)

    @app.put("/api/assets/{identifier}")
    def update(
        identifier: str, body: AssetInput, request: Request, user=Depends(authenticated)
    ):
        return service(request).save(identifier, body, user)

    @app.get("/api/assets/{identifier}/quality")
    def quality_history(
        identifier: str,
        request: Request,
        project_id: str = "default",
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return service(request).quality_page(project_id, identifier, limit, offset)
