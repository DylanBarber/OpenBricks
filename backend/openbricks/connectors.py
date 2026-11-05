"""Named SQL Server and S3 connections, usable from the UI and notebook kernels."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit
from uuid import uuid4

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .credentials import SecretBox


def now():
    return datetime.now(timezone.utc).isoformat()


def json_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value:
        raise ValueError("SQL identifiers must contain 1â€“128 characters")
    return "[" + value.replace("]", "]]") + "]"


def odbc_value(value: Any) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


class SQLServerConnection:
    """Each operation owns a connection and commits or rolls back explicitly.

    Parameters use ODBC ``?`` placeholders. No SQL/value interpolation is used.
    ``query`` rolls its transaction back after fetching; ``execute`` and
    ``insert`` commit only when their operation has completed successfully.
    """

    def __init__(self, config: dict, secrets: dict):
        self.config, self.secrets = config, secrets

    def connect(self):
        import pyodbc

        cfg = self.config
        host = cfg["server"]
        if cfg.get("port"):
            host += "," + str(cfg["port"])
        values = {
            "DRIVER": "ODBC Driver 18 for SQL Server",
            "SERVER": host,
            "DATABASE": cfg["database"],
            "UID": cfg["username"],
            "PWD": self.secrets["password"],
            "Encrypt": "yes" if cfg.get("encrypt", True) else "no",
            "TrustServerCertificate": (
                "yes" if cfg.get("trust_server_certificate", False) else "no"
            ),
            "APP": "OpenBricks",
        }
        connection = pyodbc.connect(
            ";".join(key + "=" + odbc_value(value) for key, value in values.items()),
            timeout=min(int(cfg.get("timeout", 30)), 60),
            autocommit=False,
        )
        connection.timeout = int(cfg.get("timeout", 30))
        return connection

    @contextmanager
    def transaction(self, commit=False):
        connection = self.connect()
        try:
            yield connection
            if commit:
                connection.commit()
            else:
                connection.rollback()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def query(
        self, sql: str, params: list | tuple | None = None, limit: int = 1000
    ) -> dict:
        limit = max(1, min(int(limit), 10_000))
        with self.transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, tuple(params or []))
            while cursor.description is None:
                if not cursor.nextset():
                    return {
                        "columns": [],
                        "rows": [],
                        "truncated": False,
                        "row_count": 0,
                    }
            columns = [column[0] for column in cursor.description]
            values = cursor.fetchmany(limit + 1)
            return {
                "columns": columns,
                "rows": [
                    [json_value(value) for value in row] for row in values[:limit]
                ],
                "row_count": min(len(values), limit),
                "truncated": len(values) > limit,
            }

    def read(self, sql: str, params=None, limit=1000) -> list[dict]:
        result = self.query(sql, params, limit)
        return [dict(zip(result["columns"], row)) for row in result["rows"]]

    def read_table(self, table: str, schema="dbo", limit=1000) -> list[dict]:
        return self.read(
            f"SELECT TOP ({max(1, min(int(limit), 10000))}) * FROM {identifier(schema)}.{identifier(table)}",
            limit=limit,
        )

    def execute(self, sql: str, params=None) -> dict:
        with self.transaction(commit=True) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, tuple(params or []))
            count = max(0, cursor.rowcount)
            while cursor.nextset():
                count += max(0, cursor.rowcount)
            return {"row_count": count}

    def insert(self, table: str, rows: list[dict], schema="dbo") -> dict:
        if not rows:
            return {"row_count": 0}
        if len(rows) > 10_000:
            raise ValueError("Insert at most 10,000 rows per batch")
        columns = list(rows[0])
        if not columns or any(set(row) != set(columns) for row in rows):
            raise ValueError(
                "Each inserted row must have the same non-empty column names"
            )
        sql = f"INSERT INTO {identifier(schema)}.{identifier(table)} ({', '.join(identifier(c) for c in columns)}) VALUES ({', '.join('?' for _ in columns)})"
        with self.transaction(commit=True) as connection:
            cursor = connection.cursor()
            cursor.fast_executemany = True
            cursor.executemany(
                sql, [tuple(row[column] for column in columns) for row in rows]
            )
            return {"row_count": len(rows)}

    def to_spark(self, spark, sql: str, params=None, limit=10_000):
        """Create a bounded Spark DataFrame; use batching for larger transfers."""
        result = self.query(sql, params, limit)
        if not result["rows"]:
            raise ValueError(
                "Query returned no rows; specify a Spark schema explicitly for an empty DataFrame"
            )
        return spark.createDataFrame(result["rows"], result["columns"])

    def read_spark(
        self,
        spark,
        table: str | None = None,
        *,
        query: str | None = None,
        partition_column=None,
        lower_bound=None,
        upper_bound=None,
        num_partitions=None,
        fetch_size=10000,
        predicates=None,
    ):
        """Read through Spark's native JDBC source; never materialize rows in Python.

        Bounds define partition strides, not an input filter. Use a SQL query
        or explicit predicates to filter records. Credentials stay properties.
        """
        if bool(table) == bool(query):
            raise ValueError("Specify exactly one JDBC table or query")
        if not 1 <= int(fetch_size) <= 1_000_000:
            raise ValueError("fetch_size must be 1â€“1,000,000")
        partition_values = [partition_column, lower_bound, upper_bound, num_partitions]
        if any(value is not None for value in partition_values) and not all(
            value is not None for value in partition_values
        ):
            raise ValueError(
                "Partition reads need column, lower_bound, upper_bound, and num_partitions"
            )
        if predicates and partition_column:
            raise ValueError("Choose predicates or numeric partition options")
        if predicates and (
            not isinstance(predicates, list)
            or not 1 <= len(predicates) <= 256
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 100000
                for value in predicates
            )
        ):
            raise ValueError("Provide 1â€“256 non-empty SQL predicates")
        options = {}
        if partition_column:
            identifier(partition_column)
            if not 1 <= int(num_partitions) <= 256 or int(lower_bound) >= int(
                upper_bound
            ):
                raise ValueError(
                    "Partition bounds must increase and partitions must be 1â€“256"
                )
            options = {
                "column": partition_column,
                "lowerBound": int(lower_bound),
                "upperBound": int(upper_bound),
                "numPartitions": int(num_partitions),
            }
        elif predicates:
            options = {"predicates": predicates}
        server = self.config["server"]
        if not re.fullmatch(r"[A-Za-z0-9_.\\:\[\]-]+", server):
            raise ValueError("Invalid SQL Server JDBC hostname")
        database = "{" + self.config["database"].replace("}", "}}") + "}"
        url = f"jdbc:sqlserver://{server}:{int(self.config.get('port', 1433))};databaseName={database}"
        properties = {
            "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
            "user": self.config["username"],
            "password": self.secrets["password"],
            "encrypt": str(self.config.get("encrypt", True)).lower(),
            "trustServerCertificate": str(
                self.config.get("trust_server_certificate", False)
            ).lower(),
            "fetchsize": str(int(fetch_size)),
            "queryTimeout": str(int(self.config.get("timeout", 30))),
        }
        return spark.read.jdbc(
            url=url,
            table=table or "(" + query.rstrip().rstrip(";") + ") AS openbricks_source",
            properties=properties,
            **options,
        )


class S3Connection:
    def __init__(self, config: dict, secrets: dict):
        self.config, self.secrets = config, secrets

    @property
    def client(self):
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=self.config.get("endpoint_url") or None,
            region_name=self.config.get("region", "us-east-1"),
            aws_access_key_id=self.secrets["access_key"],
            aws_secret_access_key=self.secrets["secret_key"],
            aws_session_token=self.secrets.get("session_token") or None,
            verify=self.config.get("verify_tls", True),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": self.config.get("addressing_style", "path")},
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 2},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def list_buckets(self):
        return [
            {"name": value["Name"], "created_at": json_value(value.get("CreationDate"))}
            for value in self.client.list_buckets().get("Buckets", [])
        ]

    def create_bucket(self, name: str):
        arguments = {"Bucket": name}
        if self.config.get("region", "us-east-1") != "us-east-1":
            arguments["CreateBucketConfiguration"] = {
                "LocationConstraint": self.config["region"]
            }
        self.client.create_bucket(**arguments)
        return {"name": name}

    def list(self, bucket: str, prefix="", continuation_token=None, limit=50):
        arguments = {
            "Bucket": bucket,
            "Prefix": prefix,
            "MaxKeys": max(1, min(int(limit), 1000)),
            "Delimiter": "/",
        }
        if continuation_token:
            arguments["ContinuationToken"] = continuation_token
        result = self.client.list_objects_v2(**arguments)
        return {
            "items": [
                {
                    "key": item["Key"],
                    "size": item["Size"],
                    "modified_at": json_value(item.get("LastModified")),
                }
                for item in result.get("Contents", [])
            ],
            "prefixes": [item["Prefix"] for item in result.get("CommonPrefixes", [])],
            "next_token": result.get("NextContinuationToken"),
        }

    def read_bytes(self, bucket: str, key: str, max_bytes=100 * 1024 * 1024):
        result = self.client.get_object(Bucket=bucket, Key=key)
        body = result["Body"]
        try:
            if result.get("ContentLength", 0) > max_bytes:
                raise ValueError(
                    "Object exceeds the in-memory read limit; use download_file instead"
                )
            value = body.read(max_bytes + 1)
            if len(value) > max_bytes:
                raise ValueError(
                    "Object exceeds the in-memory read limit; use download_file instead"
                )
            return value
        finally:
            body.close()

    def read_text(self, bucket: str, key: str, encoding="utf-8"):
        return self.read_bytes(bucket, key).decode(encoding)

    def write_bytes(self, bucket: str, key: str, data: bytes):
        self.client.put_object(Bucket=bucket, Key=key, Body=data)
        return f"s3://{bucket}/{key}"

    def write_text(self, bucket: str, key: str, text: str, encoding="utf-8"):
        return self.write_bytes(bucket, key, text.encode(encoding))

    def upload_file(self, filename, bucket, key):
        self.client.upload_file(str(filename), bucket, key)
        return f"s3://{bucket}/{key}"

    def download_file(self, bucket, key, filename):
        self.client.download_file(bucket, key, str(filename))
        return str(filename)

    def path(self, uri: str) -> str:
        """Stage one S3 object on this compute host for a Spark file reader."""
        parsed = urlsplit(uri)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
            raise ValueError("Use s3://bucket/key")
        root = Path(os.getenv("DATA_DIR", "/data")) / "staging"
        root.mkdir(parents=True, exist_ok=True)
        destination = root / (uuid4().hex + "-" + Path(parsed.path).name)
        return self.download_file(parsed.netloc, parsed.path.lstrip("/"), destination)

    def delete(self, bucket, key):
        self.client.delete_object(Bucket=bucket, Key=key)


class ConnectionRegistry:
    def __init__(self, definitions: list[dict]):
        self.definitions = definitions

    def _find(self, name, kind):
        value = next(
            (
                item
                for item in self.definitions
                if item["type"] == kind
                and (
                    item["id"] == name
                    or item["name"].casefold() == str(name).casefold()
                )
            ),
            None,
        )
        if not value:
            raise ValueError(f"No saved {kind} connection named {name!r}")
        return value

    def sqlserver(self, name):
        item = self._find(name, "sqlserver")
        return SQLServerConnection(item["config"], item["secrets"])

    def s3(self, name):
        item = self._find(name, "s3")
        return S3Connection(item["config"], item["secrets"])

    def list(self):
        return [
            {"id": item["id"], "name": item["name"], "type": item["type"]}
            for item in self.definitions
        ]


def connections_for_runtime(store, project_id="default", connection_ids=None):
    bootstrap_connections(store)
    selected = set(connection_ids or [])
    assigned = set((store.get("projects", project_id) or {}).get("connection_ids", []))
    values = [
        item
        for item in store.list("connections")
        if item["id"] in selected
        and (
            project_id in item.get("project_ids", ["default"]) or item["id"] in assigned
        )
    ]
    if not values:
        return []
    box = SecretBox(store.directory)
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "type": item["type"],
            "config": item["config"],
            "secrets": json.loads(box.decrypt(item["_secrets"])),
        }
        for item in values
    ]


def public_connection(item):
    return {key: value for key, value in item.items() if not key.startswith("_")}


def bootstrap_connections(store):
    """Optional local SeaweedFS service, with credentials supplied by Compose."""
    endpoint = os.getenv("OPENBRICKS_S3_ENDPOINT")
    if (
        endpoint
        and os.getenv("OPENBRICKS_S3_ACCESS_KEY")
        and os.getenv("OPENBRICKS_S3_SECRET_KEY")
    ):
        id = "local-seaweedfs"
        secret = {
            "access_key": os.environ["OPENBRICKS_S3_ACCESS_KEY"],
            "secret_key": os.environ["OPENBRICKS_S3_SECRET_KEY"],
        }
        config = {
            "endpoint_url": endpoint,
            "region": "us-east-1",
            "verify_tls": True,
            "addressing_style": "path",
        }
        previous = store.get("connections", id)
        box = SecretBox(store.directory)
        if (
            not previous
            or previous["config"] != config
            or json.loads(box.decrypt(previous["_secrets"])) != secret
            or not previous.get("built_in")
        ):
            store.put(
                "connections",
                {
                    "id": id,
                    "name": "Local SeaweedFS",
                    "type": "s3",
                    "built_in": True,
                    "config": config,
                    "secret_fields": sorted(secret),
                    "_secrets": box.encrypt(json.dumps(secret)),
                    "created_at": previous["created_at"] if previous else now(),
                    "updated_at": now(),
                },
            )


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str
    config: dict = Field(default_factory=dict)
    secrets: dict[str, str | None] = Field(default_factory=dict)
    project_ids: list[str] = Field(
        default_factory=lambda: ["default"], min_length=1, max_length=100
    )


class SQLInput(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    params: list[Any] = Field(default_factory=list, max_length=10_000)
    limit: int = Field(default=1000, ge=1, le=10_000)


class InsertInput(BaseModel):
    table: str = Field(min_length=1, max_length=128)
    schema: str = Field(default="dbo", min_length=1, max_length=128)
    rows: list[dict[str, Any]] = Field(max_length=10_000)


class S3BucketInput(BaseModel):
    name: str = Field(min_length=3, max_length=63)


def normalize_config(kind, values):
    if kind == "sqlserver":
        required = ("server", "database", "username")
        if any(not str(values.get(key, "")).strip() for key in required):
            raise ValueError("SQL Server needs a server, database, and username")
        port = int(values.get("port", 1433))
        timeout = int(values.get("timeout", 30))
        if not 1 <= port <= 65535 or not 1 <= timeout <= 300:
            raise ValueError("Port must be 1â€“65535 and timeout 1â€“300 seconds")
        return {key: str(values[key]).strip() for key in required} | {
            "port": port,
            "timeout": timeout,
            "encrypt": values.get("encrypt", True) is not False,
            "trust_server_certificate": values.get("trust_server_certificate", False)
            is True,
        }
    if kind == "s3":
        endpoint = str(values.get("endpoint_url", "")).strip().rstrip("/")
        if endpoint:
            parsed = urlparse(endpoint)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "S3 endpoint must be an HTTP(S) URL without embedded credentials or query parameters"
                )
        style = values.get("addressing_style", "path")
        if style not in ("path", "virtual", "auto"):
            raise ValueError("S3 addressing style must be path, virtual, or auto")
        return {
            "endpoint_url": endpoint,
            "region": str(values.get("region", "us-east-1")),
            "verify_tls": values.get("verify_tls", True) is not False,
            "addressing_style": style,
        }
    raise ValueError("Connection type must be sqlserver or s3")


def register_connector_routes(app, authenticated, admin, access=None):
    def record(request, id, kind=None):
        item = request.app.state.store.get("connections", id)
        if not item or (kind and item["type"] != kind):
            raise HTTPException(404, "Connection not found")
        return item

    def client(request, id, kind, user):
        item = record(request, id, kind)
        if access:
            access.require_connection(user, id)
        secrets = json.loads(
            SecretBox(request.app.state.store.directory).decrypt(item["_secrets"])
        )
        return (
            SQLServerConnection(item["config"], secrets)
            if kind == "sqlserver"
            else S3Connection(item["config"], secrets)
        )

    def safely(operation):
        try:
            return operation()
        except HTTPException:
            raise
        except Exception as exc:
            # Driver exception strings can contain connection strings or auth
            # headers. Send a useful category without returning those strings.
            if isinstance(exc, (ValueError, TypeError)):
                raise HTTPException(400, str(exc)) from None
            raise HTTPException(
                400,
                f"Connection operation failed ({type(exc).__name__}). Check the endpoint, credentials, TLS settings, and server permissions.",
            ) from None

    def save(body, request, previous=None):
        store = request.app.state.store
        name = body.name.strip()
        if not name:
            raise ValueError("Connection name is required")
        if any(
            item["name"].casefold() == name.casefold()
            and item["id"] != (previous or {}).get("id")
            for item in store.list("connections")
        ):
            raise HTTPException(409, "Connection name is already in use")
        if previous and previous["type"] != body.type:
            raise ValueError("Create a new connection to change its type")
        config = normalize_config(body.type, body.config)
        box = SecretBox(store.directory)
        secrets = json.loads(box.decrypt(previous["_secrets"])) if previous else {}
        allowed = (
            ("password",)
            if body.type == "sqlserver"
            else ("access_key", "secret_key", "session_token")
        )
        for key, value in body.secrets.items():
            if key not in allowed:
                raise ValueError("Unexpected credential field")
            if value is None:
                secrets.pop(key, None)
            elif value:
                secrets[key] = value
        required = (
            ("password",) if body.type == "sqlserver" else ("access_key", "secret_key")
        )
        if any(not secrets.get(key) for key in required):
            raise ValueError("Enter the required connection credentials")
        item = {
            "id": previous["id"] if previous else uuid4().hex,
            "name": name,
            "type": body.type,
            "config": config,
            "project_ids": list(
                dict.fromkeys(
                    body.project_ids
                    if not previous or "project_ids" in body.model_fields_set
                    else previous.get("project_ids", ["default"])
                )
            ),
            "_secrets": box.encrypt(json.dumps(secrets)),
            "secret_fields": sorted(secrets),
            "created_at": previous["created_at"] if previous else now(),
            "updated_at": now(),
        }
        return public_connection(store.put("connections", item))

    @app.get("/api/connections")
    def list_connections(request: Request, user=Depends(authenticated)):
        bootstrap_connections(request.app.state.store)

        def visible(item):
            if not access:
                return True
            try:
                access.require_connection(user, item["id"], action="read")
                return True
            except HTTPException:
                return False

        return [
            public_connection(item)
            for item in request.app.state.store.list("connections")
            if visible(item)
        ]

    @app.post("/api/connections", status_code=201)
    def create_connection(body: ConnectionInput, request: Request, user=Depends(admin)):
        if access:
            for project in body.project_ids:
                access.require_project(user, project, "manage")
        return save(body, request)

    @app.put("/api/connections/{id}")
    def update_connection(
        id: str, body: ConnectionInput, request: Request, user=Depends(admin)
    ):
        if access:
            access.require_connection(user, id, action="manage")
            for project in body.project_ids:
                access.require_project(user, project, "manage")
        previous = record(request, id)
        if previous.get("built_in"):
            raise HTTPException(
                409,
                "This connection is managed through the server's environment settings",
            )
        return save(body, request, previous)

    @app.delete("/api/connections/{id}")
    def delete_connection(id: str, request: Request, user=Depends(admin)):
        if access:
            access.require_connection(user, id, action="manage")
        if record(request, id).get("built_in"):
            raise HTTPException(
                409,
                "Disable the local storage endpoint in the server configuration to remove this built-in connection",
            )
        request.app.state.store.delete("connections", id)
        return {"ok": True}

    @app.post("/api/connections/{id}/test")
    def test_connection(id: str, request: Request, user=Depends(authenticated)):
        item = record(request, id)
        if access:
            access.require_connection(user, id)

        def operation():
            if item["type"] == "sqlserver":
                result = client(request, id, "sqlserver", user).query(
                    "SELECT DB_NAME() AS database_name, @@VERSION AS version", limit=1
                )
                return {
                    "ok": True,
                    "message": "SQL Server connection succeeded",
                    "details": result,
                }
            result = client(request, id, "s3", user).list_buckets()
            return {
                "ok": True,
                "message": "S3 connection succeeded",
                "details": {"bucket_count": len(result)},
            }

        return safely(operation)

    @app.post("/api/connections/{id}/query")
    def query(id: str, body: SQLInput, request: Request, user=Depends(authenticated)):
        return safely(
            lambda: client(request, id, "sqlserver", user).query(
                body.sql, body.params, body.limit
            )
        )

    @app.post("/api/connections/{id}/execute")
    def execute(id: str, body: SQLInput, request: Request, user=Depends(authenticated)):
        return safely(
            lambda: client(request, id, "sqlserver", user).execute(
                body.sql, body.params
            )
        )

    @app.post("/api/connections/{id}/insert")
    def insert(
        id: str, body: InsertInput, request: Request, user=Depends(authenticated)
    ):
        return safely(
            lambda: client(request, id, "sqlserver", user).insert(
                body.table, body.rows, body.schema
            )
        )

    @app.get("/api/connections/{id}/buckets")
    def list_remote_buckets(id: str, request: Request, user=Depends(authenticated)):
        return safely(
            lambda: {
                "items": client(request, id, "s3", user).list_buckets(),
                "next_token": None,
            }
        )

    @app.post("/api/connections/{id}/buckets", status_code=201)
    def create_remote_bucket(
        id: str, body: S3BucketInput, request: Request, user=Depends(authenticated)
    ):
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", body.name)
            or ".." in body.name
        ):
            raise ValueError("Use a valid lowercase S3 bucket name")
        return safely(lambda: client(request, id, "s3", user).create_bucket(body.name))

    @app.get("/api/connections/{id}/objects")
    def list_remote_objects(
        id: str,
        request: Request,
        bucket: str,
        prefix: str = "",
        continuation_token: str | None = None,
        limit: int = 50,
        user=Depends(authenticated),
    ):
        return safely(
            lambda: client(request, id, "s3", user).list(
                bucket, prefix, continuation_token, limit
            )
        )

    @app.post("/api/connections/{id}/objects", status_code=201)
    async def upload_remote_object(
        id: str,
        request: Request,
        bucket: str = Form(...),
        key: str = Form(...),
        file: UploadFile = File(...),
        user=Depends(authenticated),
    ):
        connection = client(request, id, "s3", user)
        if not key or len(key) > 1024:
            raise ValueError("Object key must contain 1â€“1024 characters")
        limit = int(os.getenv("OPENBRICKS_MAX_UPLOAD_MB", "100")) * 1024 * 1024
        # Spool rather than retaining a second full copy of a large upload.
        total = 0
        with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024) as temporary:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, "Upload exceeds the configured size limit")
                temporary.write(chunk)
            temporary.seek(0)
            await run_in_threadpool(
                lambda: safely(
                    lambda: connection.client.upload_fileobj(temporary, bucket, key)
                )
            )
        return {"key": key, "size": total}

    @app.get("/api/connections/{id}/download")
    def download_remote_object(
        id: str, request: Request, bucket: str, key: str, user=Depends(authenticated)
    ):
        result = safely(
            lambda: client(request, id, "s3", user).client.get_object(
                Bucket=bucket, Key=key
            )
        )
        body = result["Body"]

        def chunks():
            try:
                while chunk := body.read(1024 * 1024):
                    yield chunk
            finally:
                body.close()

        filename = re.sub(r"[^a-zA-Z0-9._-]", "_", key.rsplit("/", 1)[-1]) or "download"
        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            background=BackgroundTask(body.close),
        )

    @app.delete("/api/connections/{id}/objects")
    def delete_remote_object(
        id: str, request: Request, bucket: str, key: str, user=Depends(authenticated)
    ):
        safely(lambda: client(request, id, "s3", user).delete(bucket, key))
        return {"ok": True}
