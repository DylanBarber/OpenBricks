"""Bounded DuckDB operations on the selected compute host, without Spark."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


def _value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    return str(value)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]{0,127}", value
    ):
        raise ValueError("DuckDB dataset names use letters, digits, and underscores")
    return value


def _local_sql(sql, blob):
    # Resolve complete quoted blob URIs only. Escaped apostrophes remain SQL
    # string contents; no user-controlled text is inserted as SQL syntax.
    def resolve(match):
        value = match.group(1).replace("''", "'")
        return "'" + blob.path(value).replace("'", "''") + "'"

    return re.sub(r"'(blob://(?:[^']|'')*)'", resolve, sql)


def run_duckdb(spec, blob, connections, *, memory_mb=None, threads=None):
    """Run one in-memory connection, staging only explicitly selected S3 files.

    The process-tree resource supervisor supplies the outer memory ceiling.
    DuckDB's internal memory setting alone does not bound every allocation.
    """
    try:
        import duckdb
    except ImportError as exc:
        raise ValueError(
            "This compute image does not include DuckDB; use a matching OpenBricks image"
        ) from exc
    limit = max(1, min(int(spec.get("limit", 100)), 1000))
    offset = max(0, int(spec.get("offset", 0)))
    budget = int(os.getenv("OPENBRICKS_WORKER_MEMORY_MB", "512"))
    memory = max(32, min(int(memory_mb or max(32, budget - 128)), max(32, budget - 64)))
    thread_count = max(1, min(int(threads or 1), 4))
    stage_limit = (
        min(1024, max(1, int(os.getenv("OPENBRICKS_DUCKDB_STAGE_MB", "256")))) * 1024**2
    )
    payload = spec.get("payload") or {}
    sources = payload.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 32:
        raise ValueError("Choose at most 32 DuckDB source datasets")
    operation = spec.get("operation", "query")
    with tempfile.TemporaryDirectory(
        prefix="openbricks-duckdb-", dir=os.getenv("OPENBRICKS_WORKER_TMPDIR")
    ) as temporary:
        with duckdb.connect(
            ":memory:",
            config={
                "memory_limit": f"{memory}MB",
                "threads": thread_count,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "allow_unsigned_extensions": False,
                "temp_directory": temporary,
                "max_temp_directory_size": f"{max(256, memory * 2)}MB",
            },
        ) as db:
            directories = ",".join(
                "'" + path.replace("'", "''") + "'"
                for path in (str(blob.root), temporary)
            )
            db.execute("SET allowed_directories = [" + directories + "]")
            db.execute("SET enable_external_access = false")
            for index, source in enumerate(sources):
                name = _identifier(source.get("name"))
                path = str(source.get("path", ""))
                parsed = urlsplit(path)
                if parsed.scheme == "blob":
                    local_path = blob.path(path)
                elif parsed.scheme == "s3" and source.get("connection_id"):
                    if (
                        not parsed.netloc
                        or not parsed.path.lstrip("/")
                        or parsed.username
                        or parsed.password
                        or parsed.query
                        or parsed.fragment
                    ):
                        raise ValueError(
                            "Use an s3://bucket/object source without embedded credentials"
                        )
                    client = connections.s3(source["connection_id"]).client
                    response = client.get_object(
                        Bucket=parsed.netloc, Key=parsed.path.lstrip("/")
                    )
                    body = response["Body"]
                    try:
                        if int(response.get("ContentLength", 0)) > stage_limit:
                            raise ValueError(
                                "DuckDB S3 source exceeds the configured staging allowance"
                            )
                        local_path = str(Path(temporary) / f"source-{index}")
                        size = 0
                        with open(local_path, "wb") as destination:
                            while chunk := body.read(1024 * 1024):
                                size += len(chunk)
                                if size > stage_limit:
                                    raise ValueError(
                                        "DuckDB S3 source exceeds the configured staging allowance"
                                    )
                                destination.write(chunk)
                    finally:
                        body.close()
                else:
                    raise ValueError(
                        "DuckDB sources use blob:// paths or s3:// objects with an explicitly selected connection"
                    )
                format_name = str(
                    source.get("format") or Path(parsed.path).suffix.lstrip(".")
                ).lower()
                if format_name == "parquet":
                    db.read_parquet(local_path).create_view(name)
                elif format_name == "csv":
                    db.read_csv(local_path, header=True).create_view(name)
                elif format_name in ("json", "jsonl", "ndjson"):
                    db.read_json(local_path).create_view(name)
                else:
                    raise ValueError(
                        "DuckDB source format must be parquet, csv, or json"
                    )
            db.execute("SET lock_configuration = true")
            if operation == "query":
                sql = spec.get("sql", "")
                if not isinstance(sql, str) or not sql.strip() or len(sql) > 200_000:
                    raise ValueError("Enter a DuckDB SQL query up to 200 KB")
                cursor = db.execute(
                    _local_sql(sql, blob),
                    spec.get("params", spec.get("parameters", [])),
                )
            elif operation in ("schemas", "catalogs"):
                cursor = db.execute(
                    "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name LIMIT ? OFFSET ?",
                    [limit + 1, offset],
                )
            elif operation == "tables":
                cursor = db.execute(
                    "SELECT table_schema AS schema_name, table_name, table_type FROM information_schema.tables WHERE table_schema=? ORDER BY table_name LIMIT ? OFFSET ?",
                    [spec.get("schema") or "main", limit + 1, offset],
                )
            elif operation == "columns":
                cursor = db.execute(
                    "SELECT column_name,data_type,is_nullable,ordinal_position AS position FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position LIMIT ? OFFSET ?",
                    [
                        spec.get("schema") or "main",
                        _identifier(spec.get("table")),
                        limit + 1,
                        offset,
                    ],
                )
            elif operation == "preview":
                cursor = db.execute(
                    f'SELECT * FROM "{_identifier(spec.get("table"))}" LIMIT ? OFFSET ?',
                    [limit + 1, offset],
                )
            else:
                raise ValueError(
                    "DuckDB supports query, schemas, tables, columns, and preview operations"
                )
            columns = (
                [item[0] for item in cursor.description] if cursor.description else []
            )
            values = cursor.fetchmany(limit + 1) if columns else []
            rows = [[_value(value) for value in row] for row in values[:limit]]
            truncated = len(values) > limit
            for row in rows:
                for index, value in enumerate(row):
                    if isinstance(value, str) and len(value) > 10000:
                        row[index] = value[:10000]
                        truncated = True
            while rows and len(json.dumps(rows).encode()) > 2_000_000:
                rows.pop()
                truncated = True
            result = {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "engine": "duckdb",
                "memory_limit_mb": memory,
                "threads": thread_count,
            }
            if operation in ("schemas", "catalogs", "tables", "columns"):
                result.update(
                    offset=offset,
                    next_offset=offset + len(rows) if len(values) > limit else None,
                )
            return result
