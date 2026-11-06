"""Saved-query dashboards with bounded caches and durable refresh coordination.

Reading a dashboard never starts compute. Refreshes use the existing data
workspace submission path and recheck current accounts, project assignments,
query definitions and connection access before each dispatch.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .data_workspace import ExecuteInput
from .parameters import normalize_definitions, resolve_parameters
from .permissions import now

ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted", "unknown")
MAX_CACHE_BYTES = 2_000_000
MAX_VARIANTS = 10
MAX_REFRESH_HISTORY = 100
MAX_WORKSPACE_CACHE_BYTES = 64 * 1024**2


def public(value):
    return {key: item for key, item in value.items() if not key.startswith("_")}


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def query_identity(query):
    return fingerprint(
        {
            key: query.get(key)
            for key in (
                "project_id",
                "engine",
                "sql",
                "params",
                "connection_id",
                "cluster_id",
                "payload",
                "updated_at",
            )
        }
    )


def read_query(sql):
    """Conservative explicit-write guard, not a SQL or arbitrary-code sandbox."""
    if not isinstance(sql, str) or not 1 <= len(sql) <= 100_000:
        raise ValueError("A dashboard tile requires saved SQL")
    # Ignore literals, quoted identifiers and comments before checking keywords.
    # A closed allowlist of leading statements and explicit write verbs avoids
    # accidentally scheduling DDL/DML saved in the general-purpose SQL editor.
    pattern = r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[(?:\]\]|[^\]])*\]|--[^\n]*|/\*[\s\S]*?\*/"
    text = re.sub(pattern, " ", sql).strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.upper())
    forbidden = {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "EXEC",
        "EXECUTE",
        "CALL",
        "COPY",
        "ATTACH",
        "DETACH",
        "PRAGMA",
        "GRANT",
        "REVOKE",
        "SET",
        "INTO",
        "VACUUM",
        "INSTALL",
        "LOAD",
    }
    if (
        not words
        or words[0] not in ("SELECT", "WITH")
        or ";" in text
        or forbidden.intersection(words)
    ):
        raise ValueError(
            "Dashboard tiles accept one SELECT/WITH query without explicit write statements"
        )


class DashboardTile(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(default="", max_length=200)
    query_id: str = Field(min_length=1, max_length=100)
    chart: str = Field(default="table", pattern="^(table|bar|line|number)$")
    x: str | None = Field(default=None, max_length=200)
    y: str | None = Field(default=None, max_length=200)
    width: int = Field(default=6, ge=1, le=12)
    height: int = Field(default=4, ge=1, le=12)
    parameter_bindings: dict[str, str] = Field(default_factory=dict, max_length=100)


class DashboardInput(BaseModel):
    project_id: str = "default"
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    parameters: list[dict] = Field(default_factory=list, max_length=100)
    tiles: list[DashboardTile] = Field(min_length=1, max_length=12)
    cron: str | None = Field(default=None, max_length=200)
    timezone: str = Field(default="UTC", max_length=100)
    enabled: bool = True
    cache_ttl_seconds: int = Field(default=3600, ge=60, le=2_592_000)


class RefreshInput(BaseModel):
    parameters: dict = Field(default_factory=dict, max_length=100)


def bounded_result(result, byte_budget):
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("columns"), list)
        or not isinstance(result.get("rows"), list)
    ):
        raise ValueError("The query did not return a table")
    columns = [str(value)[:200] for value in result["columns"][:200]]
    rows = []
    truncated = (
        bool(result.get("truncated"))
        or len(columns) != len(result["columns"])
        or len(result["rows"]) > 1000
    )
    for source in result["rows"][:1000]:
        if not isinstance(source, (list, tuple)):
            raise ValueError("The query returned an invalid row")
        row = []
        for value in source[: len(columns)]:
            if isinstance(value, str) and len(value) > 10000:
                value, truncated = value[:10000], True
            row.append(value)
        rows.append(row)
    # Binary search avoids repeatedly encoding a large preview one row at a time.
    low, high = 0, len(rows)
    while low < high:
        middle = (low + high + 1) // 2
        if (
            len(
                json.dumps(
                    {"columns": columns, "rows": rows[:middle]}, allow_nan=False
                ).encode()
            )
            <= byte_budget
        ):
            low = middle
        else:
            high = middle - 1
    truncated |= low < len(rows)
    value = {
        "columns": columns,
        "rows": rows[:low],
        "row_count": low,
        "truncated": truncated,
    }
    if len(json.dumps(value, allow_nan=False).encode()) > byte_budget:
        raise ValueError("Query column metadata exceeds the dashboard preview budget")
    return value


class DashboardManager:
    def __init__(self, app, cron_factory=None):
        self.app, self.store = app, app.state.store
        self.access = app.state.access
        self.cron_factory = cron_factory
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.paused = 0
        self.thread = threading.Thread(
            target=self._loop, daemon=True, name="openbricks-dashboards"
        )
        with self.store.connect() as db:
            db.execute(
                "CREATE INDEX IF NOT EXISTS dashboard_parent_status ON documents(kind,parent_id,status,created_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS dashboard_cache_variant ON documents(kind,parent_id,name,updated_at DESC)"
            )
        self._recover()

    def _cron(self, expression, timezone_name):
        if self.cron_factory is None:
            from .main import cron_trigger

            return cron_trigger(expression, timezone_name)
        return self.cron_factory(expression, timezone_name)

    def start(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='dashboards' ORDER BY id LIMIT 1000"
            ).fetchall()
        for (raw,) in rows:
            self._schedule(json.loads(raw))
        if not self.thread.is_alive():
            self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=6)

    @contextmanager
    def maintenance(self):
        with self.lock:
            self.paused += 1
        try:
            yield
        finally:
            with self.lock:
                self.paused = max(0, self.paused - 1)

    @contextmanager
    def _mutation(self):
        operations = getattr(self.app.state, "operations", None)
        admitted = not operations or operations.enter_mutation()
        if not admitted:
            raise HTTPException(
                503, "Dashboard refreshes are paused for workspace maintenance"
            )
        try:
            yield
        finally:
            if operations:
                operations.leave_mutation()

    def _ready(self):
        if (
            self.stopped.is_set()
            or self.paused
            or getattr(self.app.state, "maintenance", None)
        ):
            raise HTTPException(
                503, "Dashboard refreshes are paused for workspace maintenance"
            )

    def _actor(self, user_id, project_id, token_id=None):
        user = self.store.get("users", user_id)
        if not user or user.get("disabled") or user.get("enabled") is False:
            raise ValueError("The refresh run-as account is unavailable")
        if token_id:
            token = self.store.get("api_tokens", token_id)
            if (
                not token
                or token.get("revoked_at")
                or token.get("expires_at", "") <= now()
                or token.get("service_account_id") != user_id
            ):
                raise ValueError("The refresh automation token is expired or revoked")
            user = {
                **user,
                "_api_token": {
                    "id": token_id,
                    "project_ids": token["project_ids"],
                    "actions": token["actions"],
                },
            }
        self.access.require_project(user, project_id, "run")
        return user

    def _record(self, identifier, user, action="read"):
        value = self.store.get("dashboards", identifier)
        if not value:
            raise HTTPException(404, "Dashboard not found")
        self.access.require_project(user, value["project_id"], action)
        return value

    def _active(self, identifier):
        with self.store.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM documents WHERE kind='dashboard_refreshes' AND parent_id=? AND status IN ('queued','running') LIMIT 1",
                    (identifier,),
                ).fetchone()
                is not None
            )

    def _query(self, identifier, project_id, user):
        value = self.store.get("data_queries", identifier)
        if not value or value.get("project_id", "default") != project_id:
            raise ValueError(
                "Choose an existing saved query in this dashboard's project"
            )
        self.access.require_project(user, project_id, "run")
        self.access.require_cluster(
            user, value.get("cluster_id") or "local", project_id
        )
        if value.get("connection_id"):
            record = self.access.require_connection(
                user, value["connection_id"], project_id
            )
            project = self.access.require_project(user, project_id, "run")
            if project_id not in record.get("project_ids", ["default"]) and value[
                "connection_id"
            ] not in project.get("connection_ids", []):
                raise HTTPException(
                    403,
                    "Assign the saved query's connection to this project before refreshing",
                )
        if value.get("engine", "spark") not in ("spark", "sqlserver", "duckdb"):
            raise ValueError("The saved query has an unsupported engine")
        read_query(value.get("sql"))
        return value

    def _validate(self, body, user):
        self.access.require_project(user, body.project_id, "edit")
        values = body.model_dump()
        values["name"] = body.name.strip()
        if not values["name"]:
            raise ValueError("A dashboard name is required")
        values["parameters"] = normalize_definitions(body.parameters)
        names = {value["name"] for value in values["parameters"]}
        ids = set()
        for tile in values["tiles"]:
            if tile["id"] in ids:
                raise ValueError("Dashboard tile IDs must be unique")
            ids.add(tile["id"])
            query = self._query(tile["query_id"], body.project_id, user)
            bindings = tile["parameter_bindings"]
            for position, parameter in bindings.items():
                if (
                    not re.fullmatch(r"0|[1-9][0-9]{0,2}", position)
                    or parameter not in names
                ):
                    raise ValueError(
                        "Tile bindings need a positional index and a declared dashboard parameter"
                    )
            positions = {int(value) for value in bindings}
            required = set(range(max(positions, default=-1) + 1)) - set(
                range(len(query.get("params") or []))
            )
            if not required <= positions:
                raise ValueError("Tile bindings cannot leave missing positional values")
        if body.cron:
            self._cron(body.cron, body.timezone)
            # A scheduled cache must be refreshable without browser filter input.
            defaults = resolve_parameters(values["parameters"], {})
            if any(
                name not in defaults
                for tile in values["tiles"]
                for name in tile["parameter_bindings"].values()
            ):
                raise ValueError("Scheduled tile parameters require default values")
        return values

    def save(self, body, user, identifier=None):
        with self.lock, self.store.lock:
            self._ready()
            old = self._record(identifier, user, "edit") if identifier else None
            if old and old["project_id"] != body.project_id:
                raise ValueError("Dashboards cannot move between projects")
            if old and self._active(identifier):
                raise HTTPException(
                    409, "Finish the current refresh before editing this dashboard"
                )
            if not old and self.store.count("dashboards") >= 1000:
                raise HTTPException(
                    409, "This workspace supports at most 1,000 dashboards"
                )
            values = self._validate(body, user)
            value = {
                **values,
                "id": identifier or uuid4().hex,
                "generation": uuid4().hex,
                "run_as_user_id": user["id"],
                "_run_as_token_id": user.get("_api_token", {}).get("id"),
                "created_at": (old or {}).get("created_at", now()),
                "updated_at": now(),
            }
            self.store.put("dashboards", value)
            self._schedule(value)
            self.access.audit(
                user,
                "dashboard.update" if old else "dashboard.create",
                body.project_id,
                value["id"],
            )
            return public(value)

    def delete(self, identifier, user):
        with self.lock, self.store.lock:
            value = self._record(identifier, user, "edit")
            if self._active(identifier):
                raise HTTPException(
                    409, "Finish the current refresh before deleting this dashboard"
                )
            self.store.delete("dashboards", identifier)
            with self.store.connect() as db:
                db.execute(
                    "DELETE FROM documents WHERE kind IN ('dashboard_snapshots','dashboard_refreshes') AND parent_id=?",
                    (identifier,),
                )
            self._schedule({**value, "enabled": False})
            self.access.audit(user, "dashboard.delete", value["project_id"], identifier)
            return {"ok": True}

    def page(self, project_id, search="", limit=25, offset=0):
        return self.store.page(
            "dashboards",
            [project_id],
            search,
            limit,
            offset,
            omit=("tiles", "parameters", "_run_as_token_id"),
        )

    def detail(self, identifier, user):
        value = self._record(identifier, user)
        latest = self.store.get("dashboard_refreshes", value.get("last_refresh_id", ""))
        return {**public(value), "last_refresh": public(latest) if latest else None}

    def _schedule(self, value):
        scheduler = getattr(self.app.state, "scheduler", None)
        if scheduler is None:
            return
        identifier = "dashboard-refresh:" + value["id"]
        if scheduler.get_job(identifier):
            scheduler.remove_job(identifier)
        if value.get("enabled") and value.get("cron"):
            scheduler.add_job(
                self._scheduled,
                self._cron(value["cron"], value.get("timezone", "UTC")),
                args=[value["id"]],
                id=identifier,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
            )

    def _scheduled(self, identifier):
        value = self.store.get("dashboards", identifier)
        if not value or not value.get("enabled") or not value.get("cron"):
            return
        try:
            user = self._actor(
                value["run_as_user_id"],
                value["project_id"],
                value.get("_run_as_token_id"),
            )
            self.refresh(identifier, {}, user, source="schedule")
        except HTTPException as exc:
            if exc.status_code not in (409, 503):
                self._schedule_failure(value)
        except Exception:
            self._schedule_failure(value)

    def _schedule_failure(self, value):
        # No credentials, SQL or driver exception text is stored in the dashboard.
        with self._mutation(), self.lock:
            self._ready()
            latest = self.store.get("dashboards", value["id"])
            if latest and latest["generation"] == value["generation"]:
                latest.update(
                    last_schedule_error="Scheduled refresh could not be admitted. Review the run-as account, query and project assignments.",
                    last_schedule_error_at=now(),
                )
                self.store.put("dashboards", latest)

    def refresh(self, identifier, parameters, user, source="manual"):
        with self._mutation(), self.lock, self.store.lock:
            self._ready()
            dashboard = self._record(identifier, user, "run")
            if self._active(identifier):
                raise HTTPException(
                    409, "This dashboard already has a queued or running refresh"
                )
            with self.store.connect() as db:
                pending = db.execute(
                    "SELECT COUNT(*) FROM documents WHERE kind='dashboard_refreshes' AND status IN ('queued','running')"
                ).fetchone()[0]
            if pending >= 100:
                raise HTTPException(
                    409,
                    "The dashboard refresh queue is full; try again after current work finishes",
                )
            resolved = resolve_parameters(dashboard.get("parameters", []), parameters)
            queries = {}
            tiles = []
            for tile in dashboard["tiles"]:
                query = self._query(tile["query_id"], dashboard["project_id"], user)
                if any(
                    name not in resolved for name in tile["parameter_bindings"].values()
                ):
                    raise ValueError(
                        "Supply a value for every parameter bound to a tile"
                    )
                queries[tile["id"]] = query
                tiles.append(
                    {
                        "id": tile["id"],
                        "query_id": tile["query_id"],
                        "query_hash": query_identity(query),
                        "query_updated_at": query.get("updated_at"),
                        "status": "pending",
                    }
                )
            value = {
                "id": uuid4().hex,
                "parent_id": identifier,
                "dashboard_id": identifier,
                "project_id": dashboard["project_id"],
                "generation": dashboard["generation"],
                "name": dashboard["name"],
                "parameters": resolved,
                "parameter_key": fingerprint(resolved),
                "run_as_user_id": user["id"],
                "_run_as_token_id": user.get("_api_token", {}).get("id"),
                "source": source,
                "status": "queued",
                "tiles": tiles,
                "_queries": queries,
                "created_at": now(),
            }
            self.store.put("dashboard_refreshes", value)
            dashboard.update(last_refresh_id=value["id"], last_schedule_error=None)
            self.store.put("dashboards", dashboard)
            self.access.audit(
                user, "dashboard.refresh", dashboard["project_id"], identifier
            )
            return public(value)

    def refresh_page(self, identifier, limit=25, offset=0):
        with self.store.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind='dashboard_refreshes' AND parent_id=?",
                (identifier,),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT json_remove(body,'$._queries','$._run_as_token_id') FROM documents WHERE kind='dashboard_refreshes' AND parent_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (identifier, limit, offset),
            ).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def refresh_detail(self, identifier, refresh_id, user):
        self._record(identifier, user)
        value = self.store.get("dashboard_refreshes", refresh_id)
        if not value or value.get("dashboard_id") != identifier:
            raise HTTPException(404, "Dashboard refresh not found")
        return public(value)

    def snapshot(self, identifier, parameters, user):
        dashboard = self._record(identifier, user)
        resolved = resolve_parameters(dashboard.get("parameters", []), parameters)
        key = fingerprint([identifier, dashboard["generation"], resolved])
        value = self.store.get("dashboard_snapshots", key)
        if not value:
            raise HTTPException(
                404,
                "No cached result for these filters. An operator can refresh the dashboard.",
            )
        with self.store.connect() as db:
            latest = db.execute(
                "SELECT json_remove(body,'$._queries','$._run_as_token_id') FROM documents WHERE kind='dashboard_refreshes' AND parent_id=? AND json_extract(body,'$.generation')=? AND json_extract(body,'$.parameter_key')=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (identifier, dashboard["generation"], fingerprint(resolved)),
            ).fetchone()
        observed = datetime.fromisoformat(value["refreshed_at"])
        age = max(0, (datetime.now(timezone.utc) - observed).total_seconds())
        latest = json.loads(latest[0]) if latest else None
        stale = age > dashboard["cache_ttl_seconds"] or bool(
            latest and latest["status"] in ("failed", "interrupted", "unknown")
        )
        return {
            **public(value),
            "stale": stale,
            "age_seconds": age,
            "latest_refresh": latest,
        }

    def _recover(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='dashboard_refreshes' AND status IN ('queued','running') LIMIT 100"
            ).fetchall()
        for (raw,) in rows:
            value = json.loads(raw)
            if any(
                tile.get("status") == "dispatching" and not tile.get("execution_id")
                for tile in value["tiles"]
            ):
                self._finish_error(
                    value,
                    "interrupted",
                    "Management restarted during an uncertain query dispatch. Inspect existing runs before requesting another refresh.",
                )

    def _finish_error(self, value, status, message):
        value.update(status=status, error=message, finished_at=now())
        value.pop("_queries", None)
        self.store.put("dashboard_refreshes", value)
        self._trim(value["dashboard_id"])

    def _trim(self, identifier):
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM documents WHERE kind='dashboard_snapshots' AND parent_id=? AND id NOT IN (SELECT id FROM documents WHERE kind='dashboard_snapshots' AND parent_id=? ORDER BY updated_at DESC,id DESC LIMIT ?)",
                (identifier, identifier, MAX_VARIANTS),
            )
            db.execute(
                "DELETE FROM documents WHERE kind='dashboard_refreshes' AND parent_id=? AND status NOT IN ('queued','running') AND id NOT IN (SELECT id FROM documents WHERE kind='dashboard_refreshes' AND parent_id=? ORDER BY created_at DESC,id DESC LIMIT ?)",
                (identifier, identifier, MAX_REFRESH_HISTORY),
            )
            retained = 0
            expired = []
            for cache_id, size in db.execute(
                "SELECT id,LENGTH(CAST(body AS BLOB)) FROM documents WHERE kind='dashboard_snapshots' ORDER BY updated_at DESC,id DESC"
            ):
                retained += size
                if retained > MAX_WORKSPACE_CACHE_BYTES:
                    expired.append((cache_id,))
            db.executemany(
                "DELETE FROM documents WHERE kind='dashboard_snapshots' AND id=?",
                expired,
            )

    def _complete(self, refresh, dashboard):
        tiles = []
        per_tile = max(16_000, (MAX_CACHE_BYTES - 80_000) // len(refresh["tiles"]))
        definitions = {tile["id"]: tile for tile in dashboard["tiles"]}
        for tile in refresh["tiles"]:
            run = self.store.get("runs", tile["run_id"])
            result = bounded_result(run.get("data_result"), per_tile)
            tiles.append(
                {
                    **definitions[tile["id"]],
                    "result": result,
                    "run_id": tile["run_id"],
                    "execution_id": tile["execution_id"],
                    "query_hash": tile["query_hash"],
                    "query_updated_at": tile.get("query_updated_at"),
                }
            )
        value = {
            "id": fingerprint(
                [dashboard["id"], dashboard["generation"], refresh["parameters"]]
            ),
            "parent_id": dashboard["id"],
            "dashboard_id": dashboard["id"],
            "project_id": dashboard["project_id"],
            "generation": dashboard["generation"],
            "name": refresh["parameter_key"],
            "parameters": refresh["parameters"],
            "tiles": tiles,
            "refresh_id": refresh["id"],
            "created_at": now(),
            "updated_at": now(),
            "refreshed_at": now(),
        }
        if len(json.dumps(value, allow_nan=False).encode()) > MAX_CACHE_BYTES:
            raise ValueError("The combined dashboard snapshot exceeds 2 MB")
        self.store.put("dashboard_snapshots", value)
        refresh.update(status="succeeded", finished_at=now(), snapshot_key=value["id"])
        refresh.pop("_queries", None)
        self.store.put("dashboard_refreshes", refresh)
        dashboard.update(last_success_at=refresh["finished_at"])
        self.store.put("dashboards", dashboard)
        self._trim(dashboard["id"])

    def tick(self):
        with self._mutation(), self.lock:
            self._ready()
            with self.store.connect() as db:
                rows = db.execute(
                    "SELECT id FROM documents WHERE kind='dashboard_refreshes' AND status IN ('queued','running') ORDER BY created_at,id LIMIT 100"
                ).fetchall()
            for (identifier,) in rows:
                refresh = self.store.get("dashboard_refreshes", identifier)
                if not refresh or refresh.get("status") not in ACTIVE:
                    continue
                try:
                    if self._step(refresh):
                        # At most one outbound dispatch per polling pass; slow
                        # remote hosts never hold the global SQLite store lock.
                        break
                except HTTPException as exc:
                    if exc.status_code == 503:
                        return
                    self._finish_error(
                        refresh,
                        "failed",
                        "Refresh permission or data access changed. Review the run-as account and saved queries.",
                    )
                except Exception:
                    self._finish_error(
                        refresh,
                        "failed",
                        "Dashboard refresh failed. Review the referenced query runs and current query definitions.",
                    )

    def _step(self, refresh):
        dashboard = self.store.get("dashboards", refresh["dashboard_id"])
        if not dashboard or dashboard["generation"] != refresh["generation"]:
            self._finish_error(
                refresh,
                "failed",
                "The dashboard definition changed before refresh completed",
            )
            return
        user = self._actor(
            refresh["run_as_user_id"],
            refresh["project_id"],
            refresh.get("_run_as_token_id"),
        )
        for tile in refresh["tiles"]:
            query = self._query(tile["query_id"], refresh["project_id"], user)
            if query_identity(query) != tile["query_hash"]:
                self._finish_error(
                    refresh,
                    "failed",
                    "A saved query changed while this refresh was pending. Request a new refresh.",
                )
                return
        # Reconcile submitted runs before admitting another tile. One tile at a
        # time per dashboard keeps default compute memory use predictable.
        for tile in refresh["tiles"]:
            if tile["status"] == "submitted":
                run = self.store.get("runs", tile["run_id"])
                if not run:
                    self._finish_error(
                        refresh,
                        "interrupted",
                        "A query run is missing. No query was automatically retried.",
                    )
                    return
                if run.get("status") in ACTIVE:
                    return
                if run.get("status") != "succeeded":
                    tile.update(
                        status=run.get("status", "unknown"),
                        error="Query did not succeed; inspect its run for details",
                    )
                    self._finish_error(
                        refresh,
                        "failed",
                        "A dashboard query did not succeed. The previous successful snapshot is preserved.",
                    )
                    return
                tile["status"] = "succeeded"
                self.store.put("dashboard_refreshes", refresh)
        pending = next(
            (tile for tile in refresh["tiles"] if tile["status"] == "pending"), None
        )
        if pending is None:
            self._complete(refresh, dashboard)
            return
        self._ready()
        definition = next(
            tile for tile in dashboard["tiles"] if tile["id"] == pending["id"]
        )
        query = refresh["_queries"][pending["id"]]
        parameters = list(query.get("params") or [])
        for position, name in definition["parameter_bindings"].items():
            index = int(position)
            while len(parameters) <= index:
                parameters.append(None)
            parameters[index] = refresh["parameters"].get(name)
        values = {
            key: query[key]
            for key in ("engine", "connection_id", "cluster_id", "sql", "payload")
            if key in query
        }
        body = ExecuteInput(
            project_id=refresh["project_id"],
            query_id=query["id"],
            params=parameters,
            limit=1000,
            **values,
        )
        pending["status"] = "dispatching"
        refresh.update(status="running", started_at=refresh.get("started_at") or now())
        self.store.put("dashboard_refreshes", refresh)
        request = Request({"type": "http", "app": self.app})
        result = self.app.state.data_workspace_api["execute"](body, request, user)
        pending.update(
            status="submitted", execution_id=result["id"], run_id=result["run_id"]
        )
        self.store.put("dashboard_refreshes", refresh)
        return True

    def _loop(self):
        while not self.stopped.wait(0.5):
            try:
                self.tick()
            except Exception:
                # Maintenance admission and shutdown are transient; pending work
                # remains durable. Individual failures are recorded by tick.
                pass


def register_dashboard_routes(app, authenticated, access):
    def manager(request):
        return request.app.state.dashboards

    @app.get("/api/dashboards")
    def dashboards(
        request: Request,
        project_id: str = "default",
        search: str = Query("", max_length=200),
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return manager(request).page(project_id, search, limit, offset)

    @app.post("/api/dashboards", status_code=201)
    def create(body: DashboardInput, request: Request, user=Depends(authenticated)):
        return manager(request).save(body, user)

    @app.get("/api/dashboards/query-options")
    def query_options(
        request: Request,
        project_id: str = "default",
        search: str = Query("", max_length=200),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        access.require_project(user, project_id)
        return request.app.state.store.page(
            "data_queries",
            [project_id],
            search,
            limit,
            offset,
            omit=("sql", "params", "payload"),
        )

    @app.get("/api/dashboards/{identifier}")
    def detail(identifier: str, request: Request, user=Depends(authenticated)):
        return manager(request).detail(identifier, user)

    @app.put("/api/dashboards/{identifier}")
    def update(
        identifier: str,
        body: DashboardInput,
        request: Request,
        user=Depends(authenticated),
    ):
        return manager(request).save(body, user, identifier)

    @app.delete("/api/dashboards/{identifier}")
    def delete(identifier: str, request: Request, user=Depends(authenticated)):
        return manager(request).delete(identifier, user)

    @app.post("/api/dashboards/{identifier}/refresh", status_code=202)
    def refresh(
        identifier: str,
        request: Request,
        body: RefreshInput | None = None,
        user=Depends(authenticated),
    ):
        return manager(request).refresh(
            identifier, (body or RefreshInput()).parameters, user
        )

    @app.get("/api/dashboards/{identifier}/refreshes")
    def history(
        identifier: str,
        request: Request,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(authenticated),
    ):
        manager(request)._record(identifier, user)
        return manager(request).refresh_page(identifier, limit, offset)

    @app.get("/api/dashboards/{identifier}/refreshes/{refresh_id}")
    def refresh_detail(
        identifier: str, refresh_id: str, request: Request, user=Depends(authenticated)
    ):
        return manager(request).refresh_detail(identifier, refresh_id, user)

    @app.get("/api/dashboards/{identifier}/snapshot")
    def snapshot(
        identifier: str,
        request: Request,
        parameters: str = Query("{}", max_length=65536),
        user=Depends(authenticated),
    ):
        try:
            values = json.loads(parameters)
        except ValueError:
            raise ValueError(
                "Dashboard filter parameters must be a JSON object"
            ) from None
        return manager(request).snapshot(identifier, values, user)
