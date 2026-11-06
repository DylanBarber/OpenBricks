"""Project-scoped SQL, data exploration, quality, lineage, and ingestion.

HTTP routes only submit work. Spark and database access happen in compute via
ComputeDataWorkspace. Result previews are bounded; native JDBC ingestion is not.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .connectors import identifier, json_value
from .delta_tools import DeltaTools, data_path, table_identifier


def now():
    return datetime.now(timezone.utc).isoformat()


def ingestion_fingerprint(item):
    values = {
        key: item.get(key)
        for key in ("connection_id", "source", "sink", "keys", "watermark")
    }
    values["mode"] = item.get("mode", "watermark")
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def dataframe_result(frame, limit=100):
    limit = max(1, min(int(limit), 1000))
    values = frame.take(limit + 1)
    rows = [[json_value(value) for value in row] for row in values[:limit]]
    # Bound individual strings and the complete persisted/browser preview.
    truncated = len(values) > limit or any(
        isinstance(value, str) and len(value) > 10000 for row in rows for value in row
    )
    rows = [
        [value[:10000] if isinstance(value, str) else value for value in row]
        for row in rows
    ]
    while rows and len(json.dumps(rows).encode()) > 2_000_000:
        rows.pop()
        truncated = True
    return {
        "columns": list(frame.columns),
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


class ComputeDataWorkspace:
    def __init__(
        self, spark_factory, connections, blob, emit, run_id, project_id="default"
    ):
        self.spark_factory, self.connections, self.blob = (
            spark_factory,
            connections,
            blob,
        )
        self.emit, self.run_id, self.project_id = emit, run_id, project_id
        self.delta = DeltaTools(spark_factory, blob, self.lineage)

    def _event(self, kind, **values):
        self.emit(
            {"type": kind, "event_id": uuid4().hex, "run_id": self.run_id, **values}
        )

    def lineage(self, dataset, action, details=None):
        if action not in ("read", "write") or not dataset or len(str(dataset)) > 1000:
            raise ValueError("Lineage needs a dataset name and read/write action")
        self._event(
            "lineage", dataset=str(dataset), action=action, details=details or {}
        )

    def read(self, dataset):
        spark = self.spark_factory()
        if dataset.get("table"):
            table_identifier(dataset["table"])
            frame = spark.table(dataset["table"])
        else:
            format_name = dataset.get("format", "parquet").lower()
            if format_name not in ("parquet", "delta", "csv", "json"):
                raise ValueError("Unsupported dataset format")
            frame = spark.read.format(format_name).load(
                data_path(dataset["path"], self.blob)
            )
        self.lineage(
            dataset.get("table") or dataset["path"],
            "read",
            {"format": dataset.get("format")},
        )
        return frame

    def write(self, frame, dataset, *, mode="append"):
        if mode not in ("append", "overwrite", "errorifexists"):
            raise ValueError("Unsupported dataset write mode")
        format_name = dataset.get("format", "delta").lower()
        if format_name not in ("delta", "parquet", "csv", "json"):
            raise ValueError("Unsupported dataset format")
        writer = frame.write.format(format_name).mode(mode)
        if dataset.get("table"):
            table_identifier(dataset["table"])
            writer.saveAsTable(dataset["table"])
        else:
            writer.save(data_path(dataset["path"], self.blob))
        self.lineage(
            dataset.get("table") or dataset["path"],
            "write",
            {"format": format_name, "mode": mode},
        )
        return {"dataset": dataset.get("table") or dataset["path"], "written": True}

    def quality(self, frame, checks, *, fail_on_error=True, dataset=None):
        from .data_quality import check_data

        return check_data(
            frame,
            checks,
            fail_on_error=fail_on_error,
            dataset=dataset,
            emit=lambda report: self._event("quality", report=report),
        )

    def _sql_lineage(self, sql, engine, connection_id):
        # Best-effort identifiers only, never SQL text or credentials. Use
        # read/write/lineage helpers for dynamic SQL and ambiguous expressions.
        for match in re.finditer(
            r"\b(from|join|into|update|table)\s+([A-Za-z_][A-Za-z0-9_.]*)", sql, re.I
        ):
            verb, name = match.groups()
            self.lineage(
                ("sqlserver://" + connection_id + "/" if engine == "sqlserver" else "")
                + name,
                "read" if verb.lower() in ("from", "join") else "write",
                {"inferred": True},
            )

    def run(self, spec):
        operation = spec.get("operation", "query")
        engine = spec.get("engine", "spark")
        payload = spec.get("payload") or {}
        limit = max(1, min(int(spec.get("limit", 100)), 1000))
        offset = max(0, int(spec.get("offset", 0)))
        if operation == "quality":
            dataset = payload["dataset"]
            report = self.quality(
                self.read(dataset),
                payload["checks"],
                fail_on_error=payload.get("fail_on_error", True),
                dataset=dataset.get("table") or dataset.get("path"),
            )
            result = {
                "columns": ["check", "passed", "failed_rows", "actual"],
                "rows": [
                    [item["name"], item["passed"], item["failed_rows"], item["actual"]]
                    for item in report["checks"]
                ],
                "report": report,
            }
        elif operation.startswith("delta_"):
            args = {key: payload[key] for key in ("path", "table") if payload.get(key)}
            if operation == "delta_history":
                frame = self.delta.history(**args, limit=limit)
            elif operation == "delta_detail":
                frame = self.delta.detail(**args)
            elif operation == "delta_restore":
                frame = self.delta.restore(**args, version=payload["version"])
            elif operation == "delta_vacuum":
                frame = self.delta.vacuum(
                    **args,
                    retention_hours=payload.get("retention_hours", 168),
                    dry_run=payload.get("dry_run", True),
                )
            elif operation == "delta_preview":
                frame = self.delta.read(
                    **args,
                    version=payload.get("version"),
                    timestamp=payload.get("timestamp"),
                )
            else:
                raise ValueError("Unsupported Delta operation")
            result = dataframe_result(frame, limit)
        elif operation == "ingest":
            from .ingestion import IncrementalIngestor
            from .synchronization import ChangeTrackingIngestor

            ingestor = (
                ChangeTrackingIngestor
                if payload.get("mode") == "change_tracking"
                else IncrementalIngestor
            )
            result = ingestor(
                self.spark_factory,
                self.connections,
                self.blob,
                self.lineage,
                self.quality,
            ).run(payload)
            result.update(
                ingestion_id=payload.get("ingestion_id"),
                previous_watermark=payload.get("previous_watermark"),
            )
            result.update(
                source_rows=result["rows"],
                columns=["rows", "watermark"],
                rows=[[result["rows"], result["watermark"]]],
                row_count=1,
                truncated=False,
            )
        elif engine == "duckdb":
            from .duckdb_engine import run_duckdb

            result = run_duckdb(spec, self.blob, self.connections)
        elif engine == "sqlserver":
            connection = self.connections.sqlserver(spec["connection_id"])
            schema = spec.get("schema") or "dbo"
            table = spec.get("table")
            if operation == "schemas":
                result = connection.query(
                    "SELECT SCHEMA_NAME AS schema_name FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
                    [offset, limit + 1],
                    limit,
                )
            elif operation == "tables":
                result = connection.query(
                    "SELECT TABLE_SCHEMA AS schema_name, TABLE_NAME AS table_name, TABLE_TYPE AS table_type FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
                    [schema, offset, limit + 1],
                    limit,
                )
            elif operation == "columns":
                result = connection.query(
                    "SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type, IS_NULLABLE AS is_nullable, ORDINAL_POSITION AS position FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
                    [schema, table, offset, limit + 1],
                    limit,
                )
            elif operation == "preview":
                result = connection.query(
                    f"SELECT TOP ({limit + 1}) * FROM {identifier(schema)}.{identifier(table)}",
                    limit=limit,
                )
                self.lineage(
                    "sqlserver://" + spec["connection_id"] + "/" + schema + "/" + table,
                    "read",
                )
            elif operation == "query":
                result = connection.query(spec["sql"], spec.get("params"), limit)
                self._sql_lineage(spec["sql"], engine, spec["connection_id"])
            else:
                raise ValueError("Unsupported SQL Server operation")
        else:
            spark = self.spark_factory()
            catalog = spec.get("catalog") or "spark_catalog"
            namespace = catalog + "." + (spec.get("schema") or "default")
            if operation == "catalogs":
                frame = spark.sql("SHOW CATALOGS")
            elif operation == "schemas":
                frame = spark.sql("SHOW NAMESPACES IN " + table_identifier(catalog))
            elif operation == "tables":
                frame = spark.sql("SHOW TABLES IN " + table_identifier(namespace))
            elif operation == "columns":
                frame = spark.sql(
                    "DESCRIBE TABLE "
                    + table_identifier(namespace + "." + spec["table"])
                )
            elif operation == "preview":
                target = namespace + "." + spec["table"]
                frame = spark.table(table_identifier(target))
                self.lineage(target, "read")
            elif operation == "query":
                frame = spark.sql(spec["sql"], args=spec.get("params") or None)
                self._sql_lineage(spec["sql"], engine, "")
            else:
                raise ValueError("Unsupported data operation")
            if operation in ("catalogs", "schemas", "tables", "columns"):
                frame = frame.orderBy(*frame.columns).offset(offset)
            result = dataframe_result(frame, limit)
        if result.get("rows") and isinstance(result["rows"], list):
            if any(
                isinstance(value, str) and len(value) > 10000
                for row in result["rows"]
                for value in row
            ):
                result["truncated"] = True
            result["rows"] = [
                [value[:10000] if isinstance(value, str) else value for value in row]
                for row in result["rows"]
            ]
            while (
                result["rows"] and len(json.dumps(result["rows"]).encode()) > 2_000_000
            ):
                result["rows"].pop()
                result["truncated"] = True
            result["row_count"] = len(result["rows"])
        if operation in ("catalogs", "schemas", "tables", "columns"):
            result.update(
                offset=offset,
                next_offset=(
                    offset + result.get("row_count", 0)
                    if result.get("truncated")
                    else None
                ),
            )
        self._event("data_result", result=result)
        return result


def persist_data_event(store, run, event):
    """Idempotently persist bounded event metadata on either control plane."""
    kind = event.get("type")
    if kind not in ("data_result", "quality", "lineage"):
        return
    event_id = (
        event.get("event_id")
        or hashlib.sha256(
            json.dumps(event, sort_keys=True, default=str).encode()
        ).hexdigest()
    )
    events = run.setdefault("data_events", [])
    seen = any(item.get("event_id") == event_id for item in events)
    project = run.get("project_id") or "default"
    if kind == "lineage":
        record_id = hashlib.sha256((run["id"] + ":" + event_id).encode()).hexdigest()
        if not store.get("data_lineage", record_id):
            store.put(
                "data_lineage",
                {
                    "id": record_id,
                    "project_id": project,
                    "run_id": run["id"],
                    "dataset": event["dataset"],
                    "action": event["action"],
                    "details": event.get("details", {}),
                    "created_at": now(),
                },
            )
    if seen:
        return
    if len(events) >= 1000:
        return
    safe = {**event, "event_id": event_id}
    events.append(safe)
    if kind == "data_result":
        run["data_result"] = event["result"]
    elif kind == "quality":
        run.setdefault("quality_reports", []).append(event["report"])
    else:
        run.setdefault("lineage", []).append(
            {
                key: event[key]
                for key in ("dataset", "action", "details")
                if key in event
            }
        )


def finalize_data_run(store, run):
    execution_id = run.get("data_execution_id")
    execution = None
    if execution_id:
        execution = store.get("data_executions", execution_id)
        if execution:
            if execution.get("run_id") != run["id"] or execution.get(
                "project_id", "default"
            ) != run.get("project_id", "default"):
                return
            execution.update(status=run["status"], finished_at=run.get("finished_at"))
            store.put("data_executions", execution)
    result = run.get("data_result") or {}
    if (
        run.get("status") != "succeeded"
        or not result.get("ingestion_id")
        or not execution
        or execution.get("ingestion_id") != result["ingestion_id"]
        or execution.get("run_id") != run["id"]
    ):
        return
    # Compare-and-set avoids a stale/duplicated remote event moving a saved
    # watermark backwards. Routes prohibit overlapping runs for one ingestion.
    with store.connect() as db:
        row = db.execute(
            "SELECT body FROM documents WHERE kind='data_ingestions' AND id=?",
            (result["ingestion_id"],),
        ).fetchone()
        if not row:
            return
        item = json.loads(row[0])
        if item.get("project_id", "default") != run.get("project_id", "default"):
            return
        if execution.get("ingestion_fingerprint") and execution[
            "ingestion_fingerprint"
        ] != ingestion_fingerprint(item):
            return
        if item.get("last_run_id") == run["id"]:
            return
        previous = item.get("watermark_value", item.get("watermark", {}).get("initial"))
        if previous != result.get("previous_watermark"):
            return
        item.update(
            watermark_value=result.get("watermark"),
            last_run_id=run["id"],
            updated_at=now(),
        )
        db.execute(
            "UPDATE documents SET body=?,updated_at=? WHERE kind='data_ingestions' AND id=?",
            (json.dumps(item), item["updated_at"], item["id"]),
        )


class SavedQueryInput(BaseModel):
    project_id: str = "default"
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    engine: str = "spark"
    sql: str = Field(min_length=1, max_length=100000)
    params: list = Field(default_factory=list, max_length=1000)
    connection_id: str | None = None
    cluster_id: str | None = None
    payload: dict = Field(default_factory=dict)


class ExecuteInput(BaseModel):
    project_id: str = "default"
    cluster_id: str | None = None
    query_id: str | None = None
    operation: str = "query"
    engine: str = "spark"
    connection_id: str | None = None
    sql: str | None = Field(default=None, max_length=100000)
    params: list = Field(default_factory=list, max_length=1000)
    catalog: str | None = None
    schema: str | None = None
    table: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0, le=1000000)
    payload: dict = Field(default_factory=dict)


class IngestionInput(BaseModel):
    project_id: str = "default"
    name: str = Field(min_length=1, max_length=200)
    connection_id: str
    cluster_id: str | None = None
    source: dict
    sink: dict
    keys: list[str] = Field(min_length=1, max_length=20)
    mode: str = "watermark"
    watermark: dict = Field(default_factory=dict)
    jdbc: dict = Field(default_factory=dict)
    checks: list[dict] = Field(default_factory=list, max_length=100)


def register_data_routes(app, authenticated, access):
    def store(request):
        return request.app.state.store

    def require_record(request, kind, id, user, action="read"):
        item = store(request).get(kind, id)
        if not item:
            raise HTTPException(404, "Data workspace record not found")
        access.require_project(user, item.get("project_id", "default"), action)
        return item

    def page(request, kind, project_id, limit, offset, search="", extra=None):
        clauses = ["kind=?", "COALESCE(project_id,'default')=?"]
        args = [kind, project_id]
        if search:
            clauses.append("instr(lower(name),lower(?))>0")
            args.append(search)
        for key, value in (extra or {}).items():
            if value:
                clauses.append("json_extract(body,'$." + key + "')=?")
                args.append(value)
        where = " AND ".join(clauses)
        with store(request).connect() as db:
            total = db.execute(
                "SELECT count(*) FROM documents WHERE " + where, args
            ).fetchone()[0]
            rows = db.execute(
                "SELECT body FROM documents WHERE "
                + where
                + " ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def validate_target(body, user, action="run"):
        if len(json.dumps(getattr(body, "payload", {}) or {})) > 500000:
            raise ValueError("Operation payload exceeds 500 KB")
        access.require_project(user, body.project_id, action)
        access.require_cluster(
            user,
            body.cluster_id or "local",
            body.project_id,
            action="run" if action == "run" else "read",
        )
        if body.engine not in ("spark", "sqlserver", "duckdb"):
            raise ValueError("Engine must be spark, sqlserver, or duckdb")
        if body.engine == "duckdb":
            sources = (getattr(body, "payload", {}) or {}).get("sources", [])
            if not isinstance(sources, list) or len(sources) > 32:
                raise ValueError("Choose at most 32 DuckDB dataset sources")
            for source in sources:
                if not isinstance(source, dict):
                    raise ValueError("DuckDB sources must be objects")
                if source.get("connection_id"):
                    record = access.require_connection(
                        user, source["connection_id"], body.project_id, action
                    )
                    if record.get("type") != "s3":
                        raise ValueError(
                            "DuckDB remote sources require an S3 connection"
                        )
        if body.engine == "sqlserver":
            if not body.connection_id:
                raise ValueError("Choose a SQL Server connection")
            record = access.require_connection(
                user, body.connection_id, body.project_id
            )
            # Dispatch assignment remains strict even for administrators.
            project = access.require_project(user, body.project_id, action)
            if body.project_id not in record.get(
                "project_ids", ["default"]
            ) and body.connection_id not in project.get("connection_ids", []):
                raise HTTPException(
                    403,
                    "Assign this connection to the selected project before dispatch",
                )
            if record["type"] != "sqlserver":
                raise ValueError("Choose a SQL Server connection")

    def submit(body, request, user, ingestion_id=None):
        validate_target(body, user)
        if len(json.dumps(body.payload)) > 500000:
            raise ValueError("Operation payload exceeds 500 KB")
        spec = body.model_dump(exclude_none=True)
        if body.operation == "query" and not body.sql:
            raise ValueError("SQL is required")
        if body.operation not in (
            "query",
            "catalogs",
            "schemas",
            "tables",
            "columns",
            "preview",
            "quality",
            "delta_history",
            "delta_detail",
            "delta_preview",
            "delta_restore",
            "delta_vacuum",
            "ingest",
        ):
            raise ValueError("Unsupported data operation")
        if body.operation in ("delta_restore",) or (
            body.operation == "delta_vacuum" and body.payload.get("dry_run") is False
        ):
            access.require_project(user, body.project_id, "manage")
        execution_id = uuid4().hex
        notebook = {
            "id": "data-" + execution_id,
            "name": body.operation + " workspace",
            "project_id": body.project_id,
            "connection_ids": [body.connection_id] if body.connection_id else [],
            "data_execution_id": execution_id,
            "execution_engine": (
                "spark"
                if body.operation in ("ingest", "quality")
                or body.operation.startswith("delta_")
                else body.engine
            ),
            "cells": [
                {
                    "id": uuid4().hex,
                    "language": "python",
                    "source": "data.run(" + repr(spec) + ")",
                }
            ],
        }
        if body.engine == "duckdb":
            notebook["connection_ids"] = sorted(
                set(
                    source["connection_id"]
                    for source in body.payload.get("sources", [])
                    if source.get("connection_id")
                )
            )
        from .connectors import connections_for_runtime

        snapshots = connections_for_runtime(
            store(request), body.project_id, notebook["connection_ids"]
        )
        run_id = uuid4().hex
        item = {
            "id": execution_id,
            "project_id": body.project_id,
            "run_id": run_id,
            "query_id": body.query_id,
            "ingestion_id": ingestion_id,
            "name": body.operation,
            "operation": body.operation,
            "engine": body.engine,
            "connection_id": body.connection_id,
            "cluster_id": body.cluster_id or "local",
            "status": "queued",
            "created_at": now(),
        }
        if ingestion_id:
            item["ingestion_fingerprint"] = ingestion_fingerprint(body.payload)
        store(request).put("data_executions", item)
        try:
            request.app.state.runtime.submit(
                notebook,
                cluster_id=body.cluster_id,
                connections=snapshots,
                run_id=run_id,
                resource_options={"mode": "job", "owner_id": user["id"]},
            )
        except Exception:
            store(request).delete("data_executions", execution_id)
            raise
        return item

    @app.get("/api/data/queries")
    def queries(
        request: Request,
        project_id: str = "default",
        search: str = "",
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return page(request, "data_queries", project_id, limit, offset, search)

    @app.post("/api/data/queries", status_code=201)
    def create_query(
        body: SavedQueryInput, request: Request, user=Depends(authenticated)
    ):
        validate_target(body, user, "edit")
        return store(request).put(
            "data_queries",
            {
                **body.model_dump(),
                "id": uuid4().hex,
                "created_at": now(),
                "updated_at": now(),
            },
        )

    @app.put("/api/data/queries/{id}")
    def update_query(
        id: str, body: SavedQueryInput, request: Request, user=Depends(authenticated)
    ):
        old = require_record(request, "data_queries", id, user, "edit")
        validate_target(body, user, "edit")
        if old["project_id"] != body.project_id:
            raise ValueError("Saved queries cannot move between projects")
        return store(request).put(
            "data_queries", {**old, **body.model_dump(), "updated_at": now()}
        )

    @app.delete("/api/data/queries/{id}")
    def delete_query(id: str, request: Request, user=Depends(authenticated)):
        require_record(request, "data_queries", id, user, "edit")
        store(request).delete("data_queries", id)
        return {"ok": True}

    @app.post("/api/data/execute", status_code=202)
    def execute(body: ExecuteInput, request: Request, user=Depends(authenticated)):
        if body.query_id:
            saved = require_record(request, "data_queries", body.query_id, user, "run")
            if saved["project_id"] != body.project_id:
                raise ValueError("Query belongs to another project")
            if body.operation == "query" and body.sql is None:
                values = {
                    key: saved[key]
                    for key in (
                        "engine",
                        "sql",
                        "params",
                        "connection_id",
                        "cluster_id",
                        "payload",
                    )
                    if key in saved and key not in body.model_fields_set
                }
                body = body.model_copy(update=values)
        return submit(body, request, user)

    @app.get("/api/data/executions")
    def executions(
        request: Request,
        project_id: str = "default",
        query_id: str | None = None,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        result = page(
            request,
            "data_executions",
            project_id,
            limit,
            offset,
            extra={"query_id": query_id},
        )
        with store(request).connect() as db:
            for item in result["items"]:
                row = db.execute(
                    "SELECT status,finished_at FROM documents WHERE kind='runs' AND id=?",
                    (item["run_id"],),
                ).fetchone()
                if row:
                    item.update(status=row[0], finished_at=row[1])
        return result

    @app.get("/api/data/executions/{id}")
    def execution(id: str, request: Request, user=Depends(authenticated)):
        item = require_record(request, "data_executions", id, user)
        run = store(request).get("runs", item["run_id"]) or {}
        if run:
            finalize_data_run(store(request), run)
        return {
            **item,
            "status": run.get("status", item["status"]),
            "finished_at": run.get("finished_at"),
            "result": run.get("data_result"),
            "quality_reports": run.get("quality_reports", []),
            "lineage": run.get("lineage", []),
            "error": run.get("error"),
        }

    @app.get("/api/data/lineage")
    def lineage(
        request: Request,
        project_id: str = "default",
        dataset: str | None = None,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return page(
            request,
            "data_lineage",
            project_id,
            limit,
            offset,
            extra={"dataset": dataset},
        )

    @app.get("/api/data/ingestions")
    def ingestions(
        request: Request,
        project_id: str = "default",
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return page(request, "data_ingestions", project_id, limit, offset)

    def save_ingestion(body, request, user, old=None):
        access.require_project(user, body.project_id, "edit")
        validate_target(
            ExecuteInput(
                project_id=body.project_id,
                cluster_id=body.cluster_id,
                engine="sqlserver",
                connection_id=body.connection_id,
            ),
            user,
            "edit",
        )
        for key in [
            *body.keys,
            body.source.get("schema", "dbo"),
            body.source.get("table", ""),
        ]:
            identifier(key)
        if body.mode not in ("watermark", "change_tracking"):
            raise ValueError("Choose watermark or change_tracking synchronization")
        if body.mode == "watermark":
            identifier(body.watermark.get("column", ""))
        if bool(body.sink.get("table")) == bool(body.sink.get("path")):
            raise ValueError("Choose exactly one sink table or blob path")
        if body.sink.get("table"):
            table_identifier(body.sink["table"])
        elif not str(body.sink["path"]).startswith(("blob://", "s3a://", "s3://")):
            raise ValueError("Choose a local blob or configured S3 Delta sink")
        if old and old["project_id"] != body.project_id:
            raise ValueError("Ingestions cannot move between projects")
        from .ingestion import sql_literal

        if body.mode == "watermark" and body.watermark.get("type", "number") not in (
            "number",
            "timestamp",
            "date",
            "string",
        ):
            raise ValueError(
                "Watermark type must be number, timestamp, date, or string"
            )
        if set(body.jdbc) - {
            "fetch_size",
            "partition_column",
            "lower_bound",
            "upper_bound",
            "num_partitions",
            "predicates",
        }:
            raise ValueError("Unsupported JDBC option")
        if body.mode == "change_tracking" and set(body.jdbc) - {"fetch_size"}:
            raise ValueError(
                "Change Tracking reads a single snapshot transaction; parallel JDBC options are unavailable"
            )
        if body.watermark.get("initial") is not None:
            sql_literal(body.watermark["initial"], body.watermark.get("type", "number"))
        with store(request).lock:
            if old:
                old = store(request).get("data_ingestions", old["id"])
                if ingestion_active(request, old["id"]):
                    raise HTTPException(
                        409,
                        "Finish queued/running ingestion before editing its definition",
                    )
                if old.get("last_run_id") and ingestion_fingerprint(
                    old
                ) != ingestion_fingerprint(body.model_dump()):
                    raise HTTPException(
                        409,
                        "Create a new ingestion when changing a source, sink, key, or watermark after a successful run",
                    )
            values = {
                **(old or {}),
                **body.model_dump(),
                "id": old["id"] if old else uuid4().hex,
                "created_at": old["created_at"] if old else now(),
                "updated_at": now(),
            }
            return store(request).put("data_ingestions", values)

    def ingestion_active(request, id):
        with store(request).connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM documents e JOIN documents r ON r.kind='runs' AND r.id=json_extract(e.body,'$.run_id') WHERE e.kind='data_executions' AND json_extract(e.body,'$.ingestion_id')=? AND r.status IN ('queued','running') LIMIT 1",
                    (id,),
                ).fetchone()
                is not None
            )

    @app.post("/api/data/ingestions", status_code=201)
    def create_ingestion(
        body: IngestionInput, request: Request, user=Depends(authenticated)
    ):
        return save_ingestion(body, request, user)

    @app.put("/api/data/ingestions/{id}")
    def update_ingestion(
        id: str, body: IngestionInput, request: Request, user=Depends(authenticated)
    ):
        return save_ingestion(
            body,
            request,
            user,
            require_record(request, "data_ingestions", id, user, "edit"),
        )

    @app.delete("/api/data/ingestions/{id}")
    def delete_ingestion(id: str, request: Request, user=Depends(authenticated)):
        require_record(request, "data_ingestions", id, user, "edit")
        with store(request).lock:
            if ingestion_active(request, id):
                raise HTTPException(
                    409,
                    "Finish queued/running ingestion before deleting its definition",
                )
            store(request).delete("data_ingestions", id)
        return {"ok": True}

    @app.post("/api/data/ingestions/{id}/run", status_code=202)
    def run_ingestion(
        id: str,
        request: Request,
        payload: dict | None = None,
        user=Depends(authenticated),
    ):
        item = require_record(request, "data_ingestions", id, user, "run")
        with store(request).lock:
            item = store(request).get("data_ingestions", id)
            if ingestion_active(request, id):
                raise HTTPException(409, "This ingestion is already queued or running")
            spec = {
                **item,
                "ingestion_id": id,
                "previous_watermark": item.get(
                    "watermark_value", item["watermark"].get("initial")
                ),
            }
            body = ExecuteInput(
                project_id=item["project_id"],
                cluster_id=(payload or {}).get("cluster_id") or item.get("cluster_id"),
                connection_id=item["connection_id"],
                engine="sqlserver",
                operation="ingest",
                payload=spec,
            )
            return submit(body, request, user, id)

    # Internal coordinators reuse exactly the same authorization, connector
    # selection and bounded compute submission path as the public SQL API.
    return {"submit": submit, "execute": execute}
