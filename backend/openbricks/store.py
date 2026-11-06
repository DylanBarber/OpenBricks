"""Small, transactional SQLite document store (no remote services)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, local
from typing import Any

# Only data_lineage reuses these scalar lookup columns: notebook_name holds its
# observed action. The JSON/API document remains unchanged for every kind.
LINEAGE_METADATA = {"name": "dataset", "parent_id": "run_id", "notebook_name": "action"}


def metadata_expression(column):
    ordinary = f"json_extract(body,'$.{column}')"
    if column not in LINEAGE_METADATA:
        return ordinary
    lineage = f"json_extract(body,'$.{LINEAGE_METADATA[column]}')"
    return f"CASE WHEN kind='data_lineage' THEN {lineage} ELSE {ordinary} END"


def backfill_lineage_metadata(db):
    assignments = ",".join(
        f"{column}=json_extract(body,'$.{field}')"
        for column, field in LINEAGE_METADATA.items()
    )
    changed = " OR ".join(
        f"{column} IS NOT json_extract(body,'$.{field}')"
        for column, field in LINEAGE_METADATA.items()
    )
    db.execute(
        f"UPDATE documents SET {assignments} WHERE kind='data_lineage' AND ({changed})"
    )


def scope_clause(project_ids, prefix=""):
    if project_ids is None:
        return "", []
    if not project_ids:
        return " AND 0", []
    return (
        f" AND COALESCE({prefix}project_id,'default') IN ({','.join('?' for _ in project_ids)})",
        list(project_ids),
    )


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "openbricks.sqlite3"
        self.lock = RLock()
        self._live_edit = local()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS documents (kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(kind,id))"
            )
            # Keep queryable metadata outside large notebook/run JSON bodies.
            # This migrates old data in place without rewriting notebooks or runs.
            existing = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
            metadata = (
                "name",
                "parent_id",
                "folder_id",
                "notebook_id",
                "status",
                "job_id",
                "created_at",
                "updated_at",
                "finished_at",
                "notebook_name",
                "project_id",
            )
            added = [column for column in metadata if column not in existing]
            for column in added:
                db.execute(f"ALTER TABLE documents ADD COLUMN {column} TEXT")
            if added:
                assignments = ", ".join(
                    f"{column}={metadata_expression(column)}" for column in added
                )
                db.execute(f"UPDATE documents SET {assignments}")
            db.execute(
                "CREATE INDEX IF NOT EXISTS documents_recent ON documents(kind,created_at DESC,id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS notebook_folder ON documents(kind,folder_id,updated_at DESC,id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS folder_parent ON documents(kind,parent_id,name COLLATE NOCASE)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS run_notebook_history ON documents(kind,notebook_id,created_at DESC,id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS run_status_history ON documents(kind,status,created_at DESC,id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS run_job_status ON documents(kind,job_id,status)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS project_documents ON documents(kind,project_id,created_at DESC,id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS project_scope_documents ON documents(kind,COALESCE(project_id,'default'),created_at DESC,id)"
            )

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def put(self, kind: str, value: dict[str, Any]):
        with self.lock:
            return self._put(kind, value)

    def _put(self, kind: str, value: dict[str, Any]):
        if kind == "notebooks":
            with self.lock:
                existing = self.get(kind, value["id"])
                if existing is None or existing.get("cells") != value.get("cells"):
                    from .notebooks import validate_source_size

                    validate_source_size(value.get("cells", []))
                if (
                    existing
                    and existing.get("collaboration_enabled")
                    and getattr(self._live_edit, "notebook_id", None) != value["id"]
                ):
                    if "collaboration_enabled" not in value:
                        value = {**value, "collaboration_enabled": True}
                    elif not value["collaboration_enabled"]:
                        raise ValueError(
                            "Disable live collaboration through its mode endpoint before replacing notebook metadata"
                        )
                if (
                    existing
                    and existing.get("collaboration_enabled")
                    and existing.get("cells") != value.get("cells")
                    and getattr(self._live_edit, "notebook_id", None) != value["id"]
                ):
                    raise ValueError(
                        "Live collaboration is enabled. Finish cell edits and disable it before importing or replacing notebook source"
                    )
        metadata = (
            "name",
            "parent_id",
            "folder_id",
            "notebook_id",
            "status",
            "job_id",
            "created_at",
            "updated_at",
            "finished_at",
            "notebook_name",
            "project_id",
        )
        columns = ",".join(metadata)
        assignments = ",".join(f"{column}=excluded.{column}" for column in metadata)
        placeholders = ",".join("?" for _ in range(3 + len(metadata)))
        with self.connect() as db:
            db.execute(
                f"INSERT INTO documents(kind,id,body,{columns}) VALUES({placeholders}) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,{assignments}",
                (
                    kind,
                    value["id"],
                    json.dumps(value, allow_nan=False),
                    *(
                        value.get(
                            LINEAGE_METADATA.get(column, column)
                            if kind == "data_lineage"
                            else column
                        )
                        for column in metadata
                    ),
                ),
            )
        return value

    @contextmanager
    def live_notebook_edit(self, notebook_id):
        previous = getattr(self._live_edit, "notebook_id", None)
        self._live_edit.notebook_id = notebook_id
        try:
            yield
        finally:
            self._live_edit.notebook_id = previous

    def get(self, kind: str, id: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT body FROM documents WHERE kind=? AND id=?", (kind, id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind: str, limit: int | None = None):
        suffix = " LIMIT ?" if limit is not None else ""
        params = (kind, max(0, limit)) if limit is not None else (kind,)
        with self.connect() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind=? ORDER BY rowid DESC" + suffix,
                params,
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def count(self, kind: str):
        with self.connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind=?", (kind,)
            ).fetchone()[0]

    def folder_children(self, parent_id: str | None, project_ids=None):
        scope, params = scope_clause(project_ids)
        with self.connect() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='folders' AND parent_id IS ?"
                + scope
                + " ORDER BY name COLLATE NOCASE,id",
                (parent_id, *params),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def folder_has_content(self, folder_id: str):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM documents WHERE (kind='folders' AND parent_id=?) OR (kind='notebooks' AND folder_id=?) LIMIT 1",
                    (folder_id, folder_id),
                ).fetchone()
                is not None
            )

    def has_active_job(self, job_id: str):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM documents WHERE kind IN ('runs','workflow_runs') AND job_id=? AND status IN ('queued','running') LIMIT 1",
                    (job_id,),
                ).fetchone()
                is not None
            )

    def has_active_runs(self):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM documents WHERE kind='runs' AND status IN ('queued','running') LIMIT 1"
                ).fetchone()
                is not None
            )

    def active_runs(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='runs' AND status IN ('queued','running') ORDER BY created_at,id"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def notebook_page(
        self,
        folder_id: str | None,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
        *,
        all_folders: bool = False,
        project_ids=None,
    ):
        clauses = ["kind='notebooks'"]
        params: list[Any] = []
        if not all_folders:
            clauses.append("folder_id IS ?")
            params.append(folder_id)
        if search:
            clauses.append("instr(lower(name),lower(?))>0")
            params.append(search)
        scope, scope_params = scope_clause(project_ids)
        where = " AND ".join(clauses) + scope
        params.extend(scope_params)
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE " + where, params
            ).fetchone()[0]
            rows = db.execute(
                "SELECT json_remove(body,'$.cells') FROM documents WHERE "
                + where
                + " ORDER BY COALESCE(updated_at,created_at) DESC,id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def run_page(
        self,
        notebook_id: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
        project_ids=None,
    ):
        clauses, params = ["kind='runs'"], []
        if notebook_id:
            clauses.append("notebook_id=?")
            params.append(notebook_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        scope, scope_params = scope_clause(project_ids)
        where = " AND ".join(clauses) + scope
        params.extend(scope_params)
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE " + where, params
            ).fetchone()[0]
            rows = db.execute(
                "SELECT json_remove(body,'$.outputs','$.logs','$.data_events','$.data_result','$.quality_reports','$.lineage','$.profile','$._cells','$._reset_scope','$._notebook','$._cell_id','$._remote_submitted','$._execution_spec') FROM documents WHERE "
                + where
                + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def job_page(
        self, search: str = "", limit: int = 25, offset: int = 0, project_ids=None
    ):
        where = "kind='jobs'"
        params: list[Any] = []
        if search:
            where += " AND (instr(lower(name),lower(?))>0 OR instr(lower(COALESCE(notebook_name,'')),lower(?))>0)"
            params.extend((search, search))
        scope, scope_params = scope_clause(project_ids)
        where += scope
        params.extend(scope_params)
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE " + where, params
            ).fetchone()[0]
            rows = db.execute(
                "SELECT body FROM documents WHERE "
                + where
                + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def run_notebook_page(
        self, search: str = "", limit: int = 25, offset: int = 0, project_ids=None
    ):
        # Group only indexed scalar metadata. Outputs/cell source never enter
        # Python memory while rendering the notebook-level history overview.
        scope, scope_params = scope_clause(project_ids)
        history_scope, history_params = scope_clause(project_ids, "r.")
        notebook_scope, notebook_params = scope_clause(project_ids, "n.")
        query = """WITH grouped AS (
            SELECT notebook_id,COUNT(*) AS total_runs,
                SUM(status='succeeded') AS succeeded,SUM(status='failed') AS failed,
                SUM(status='running') AS running,SUM(status='queued') AS queued,
                MAX(created_at) AS last_run_at
            FROM documents WHERE kind='runs' AND notebook_id IS NOT NULL {scope} GROUP BY notebook_id
        ), named AS (
            SELECT g.*,n.folder_id,
                COALESCE(n.name,(SELECT r.notebook_name FROM documents r WHERE r.kind='runs' AND r.notebook_id=g.notebook_id {history_scope} ORDER BY r.created_at DESC,r.id DESC LIMIT 1),'Deleted notebook') AS notebook_name,
                (SELECT r.status FROM documents r WHERE r.kind='runs' AND r.notebook_id=g.notebook_id {history_scope} ORDER BY r.created_at DESC,r.id DESC LIMIT 1) AS last_status,
                CASE WHEN n.id IS NULL THEN 1 ELSE 0 END AS deleted
            FROM grouped g LEFT JOIN documents n ON n.kind='notebooks' AND n.id=g.notebook_id {notebook_scope}
        ) """.format(
            scope=scope, history_scope=history_scope, notebook_scope=notebook_scope
        )
        params = [*scope_params, *history_params, *history_params, *notebook_params]
        where = "WHERE instr(lower(notebook_name),lower(?))>0"
        with self.connect() as db:
            total = db.execute(
                query + "SELECT COUNT(*) FROM named " + where, (*params, search)
            ).fetchone()[0]
            db.row_factory = sqlite3.Row
            rows = db.execute(
                query
                + "SELECT * FROM named "
                + where
                + " ORDER BY last_run_at DESC,notebook_id LIMIT ? OFFSET ?",
                (*params, search, limit, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def metrics(self, project_ids=None):
        scope, params = scope_clause(project_ids)
        with self.connect() as db:
            counts = dict(
                db.execute(
                    "SELECT kind,COUNT(*) FROM documents WHERE 1"
                    + scope
                    + " GROUP BY kind",
                    params,
                ).fetchall()
            )
            statuses = dict(
                db.execute(
                    "SELECT status,COUNT(*) FROM documents WHERE kind='runs'"
                    + scope
                    + " GROUP BY status",
                    params,
                ).fetchall()
            )
            active_jobs = db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind='jobs' AND json_extract(body,'$.enabled')=1"
                + scope,
                params,
            ).fetchone()[0]
        return {
            "notebooks": counts.get("notebooks", 0),
            "runs": counts.get("runs", 0),
            "jobs": counts.get("jobs", 0),
            "active_jobs": active_jobs,
            "succeeded": statuses.get("succeeded", 0),
            "failed": statuses.get("failed", 0),
            "running": statuses.get("running", 0),
            "queued": statuses.get("queued", 0),
        }

    def delete(self, kind: str, id: str):
        with self.connect() as db:
            db.execute("DELETE FROM documents WHERE kind=? AND id=?", (kind, id))

    def page(self, kind, project_ids=None, search="", limit=25, offset=0, omit=()):
        scope, params = scope_clause(project_ids)
        where = "kind=?" + scope
        params = [kind, *params]
        if search:
            where += " AND instr(lower(COALESCE(name,'')),lower(?))>0"
            params.append(search)
        expression = "body"
        if omit:
            if any(not key.replace("_", "").isalnum() for key in omit):
                raise ValueError("Invalid metadata field")
            expression = (
                "json_remove(body," + ",".join("'$." + key + "'" for key in omit) + ")"
            )
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE " + where, params
            ).fetchone()[0]
            rows = db.execute(
                "SELECT "
                + expression
                + " FROM documents WHERE "
                + where
                + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
