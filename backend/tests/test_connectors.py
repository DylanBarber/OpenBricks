"""Connector protocol tests use fake ODBC/S3 transports, never a user's database."""

import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyodbc
import pytest
from fastapi.testclient import TestClient

from openbricks.connectors import (
    ConnectionRegistry,
    S3Connection,
    SQLServerConnection,
    bootstrap_connections,
    connections_for_runtime,
    identifier,
    json_value,
    normalize_config,
    odbc_value,
)
from openbricks.credentials import SecretBox
from openbricks.main import create_app
from openbricks.store import Store

SQL_CONFIG = {
    "server": "sql.example",
    "port": 1433,
    "database": "Warehouse",
    "username": "analyst",
    "timeout": 30,
    "encrypt": True,
    "trust_server_certificate": False,
}
SQL_SECRET = {"password": "private-sql-password"}
S3_CONFIG = {
    "endpoint_url": "http://s3.example:8333",
    "region": "us-east-1",
    "verify_tls": True,
    "addressing_style": "path",
}
S3_SECRET = {"access_key": "private-access-key", "secret_key": "private-secret-key"}


class Cursor:
    def __init__(self, sets=None, error=None):
        self.sets = sets or [{"columns": ["value"], "rows": [(42,)], "rowcount": 1}]
        self.position = 0
        self.error = error
        self.calls = []
        self.fast_executemany = False

    @property
    def description(self):
        columns = self.sets[self.position].get("columns")
        return [(column,) for column in columns] if columns is not None else None

    @property
    def rowcount(self):
        return self.sets[self.position].get("rowcount", -1)

    def execute(self, sql, params=()):
        self.calls.append(("execute", sql, params))
        if self.error:
            raise self.error
        return self

    def executemany(self, sql, values):
        self.calls.append(("executemany", sql, values))
        if self.error:
            raise self.error
        return self

    def fetchmany(self, size):
        self.calls.append(("fetchmany", size))
        return self.sets[self.position].get("rows", [])[:size]

    def nextset(self):
        if self.position + 1 < len(self.sets):
            self.position += 1
            return True
        return False


class ODBCConnection:
    def __init__(self, cursor):
        self.value = cursor
        self.calls = []
        self.timeout = None
        self.converters = {}

    def add_output_converter(self, kind, converter):
        self.converters[kind] = converter

    def cursor(self):
        return self.value

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")


def fake_odbc(monkeypatch, cursor):
    value = ODBCConnection(cursor)
    calls = []

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return value

    monkeypatch.setattr(pyodbc, "connect", connect)
    return value, calls


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "test-password-long-123")
    for variable in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_S3_ENDPOINT",
        "OPENBRICKS_S3_ACCESS_KEY",
        "OPENBRICKS_S3_SECRET_KEY",
        "OPENBRICKS_SECRET_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    with TestClient(create_app(tmp_path / "data")) as value:
        value.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test-password-long-123"},
        ).raise_for_status()
        yield value


def saved(client, kind="sqlserver"):
    body = {
        "name": "Warehouse" if kind == "sqlserver" else "Object store",
        "type": kind,
        "config": SQL_CONFIG if kind == "sqlserver" else S3_CONFIG,
        "secrets": SQL_SECRET if kind == "sqlserver" else S3_SECRET,
    }
    result = client.post("/api/connections", json=body)
    assert result.status_code == 201, result.text
    return result.json()


def test_sql_query_parameter_binding_and_explicit_rollback(monkeypatch):
    injection = "'; DROP TABLE Important;--"
    cursor = Cursor([{"columns": ["value"], "rows": [(injection,)], "rowcount": 1}])
    connection, calls = fake_odbc(monkeypatch, cursor)
    result = SQLServerConnection(SQL_CONFIG, SQL_SECRET).query(
        "SELECT ? AS value", [injection]
    )
    assert cursor.calls[0] == ("execute", "SELECT ? AS value", (injection,))
    assert injection not in cursor.calls[0][1]
    assert result["rows"] == [[injection]]
    assert connection.calls == ["rollback", "close"]
    assert calls[0][1] == {"timeout": 30, "autocommit": False}
    assert connection.timeout == 30


def test_odbc_connection_values_and_identifiers_are_escaped(monkeypatch):
    connection, calls = fake_odbc(monkeypatch, Cursor())
    config = {
        **SQL_CONFIG,
        "server": "db};UID=intruder",
        "database": "data};Encrypt=no",
        "username": "name};PWD=bad",
        "timeout": 300,
    }
    secret = {"password": "password};Trusted_Connection=yes"}
    SQLServerConnection(config, secret).connect()
    value = calls[0][0][0]
    assert "SERVER={db}};UID=intruder,1433}" in value
    assert "DATABASE={data}};Encrypt=no}" in value
    assert "UID={name}};PWD=bad}" in value
    assert "PWD={password}};Trusted_Connection=yes}" in value
    assert "Encrypt={yes}" in value and "TrustServerCertificate={no}" in value
    assert calls[0][1]["timeout"] == 60 and connection.timeout == 300
    assert identifier("order] ; DROP TABLE x;--") == "[order]] ; DROP TABLE x;--]"
    assert odbc_value("a}b;c") == "{a}}b;c}"
    for invalid in ("", "x\x00y", "x" * 129):
        with pytest.raises(ValueError):
            identifier(invalid)


def test_sql_results_are_bounded_and_serializable(monkeypatch):
    moment = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)
    cursor = Cursor(
        [
            {"columns": None, "rowcount": 0},
            {
                "columns": ["money", "when", "bytes", "day"],
                "rows": [
                    (Decimal("12.3400"), moment, b"\x00\xff", date(2026, 1, 1)),
                    (Decimal("99.99"), moment, b"x", date(2026, 1, 1)),
                ],
            },
        ]
    )
    connection, _ = fake_odbc(monkeypatch, cursor)
    result = SQLServerConnection(SQL_CONFIG, SQL_SECRET).query(
        "EXEC reporting.procedure", limit=1
    )
    assert ("fetchmany", 2) in cursor.calls
    assert result["truncated"] and result["row_count"] == 1
    assert result["rows"] == [["12.3400", moment.isoformat(), "00ff", "2026-01-01"]]
    json.dumps(result, allow_nan=False)
    assert connection.calls == ["rollback", "close"]
    assert json_value(float("inf")) == "inf"


def test_execute_commits_all_result_counts_and_failure_rolls_back(monkeypatch):
    cursor = Cursor(
        [{"columns": None, "rowcount": 2}, {"columns": None, "rowcount": 3}]
    )
    connection, _ = fake_odbc(monkeypatch, cursor)
    assert SQLServerConnection(SQL_CONFIG, SQL_SECRET).execute(
        "UPDATE table_a SET value=?", [42]
    ) == {"row_count": 5}
    assert connection.calls == ["commit", "close"]
    failing, _ = fake_odbc(
        monkeypatch, Cursor(error=pyodbc.Error("driver rejected the batch"))
    )
    with pytest.raises(pyodbc.Error):
        SQLServerConnection(SQL_CONFIG, SQL_SECRET).execute(
            "UPDATE table_a SET value=?", [42]
        )
    assert failing.calls == ["rollback", "close"]


def test_insert_uses_identifiers_and_bound_bulk_rows(monkeypatch):
    cursor = Cursor()
    connection, _ = fake_odbc(monkeypatch, cursor)
    text = "'); DROP TABLE accounts;--"
    result = SQLServerConnection(SQL_CONFIG, SQL_SECRET).insert(
        "table]name",
        [{"id": 1, "full]name": text}, {"full]name": "Grace", "id": 2}],
        "my]schema",
    )
    assert result == {"row_count": 2}
    assert not cursor.fast_executemany
    operation, sql, values = cursor.calls[0]
    assert (
        operation == "executemany"
        and sql
        == "INSERT INTO [my]]schema].[table]]name] ([id], [full]]name]) VALUES (?, ?)"
    )
    assert values == [(1, text), (2, "Grace")] and text not in sql
    assert connection.calls == ["commit", "close"]
    for rows in ([{}], [{"a": 1}, {"b": 2}], [{"a": 1}] * 10001):
        with pytest.raises(ValueError):
            SQLServerConnection(SQL_CONFIG, SQL_SECRET).insert("x", rows)
    assert SQLServerConnection(SQL_CONFIG, SQL_SECRET).insert("x", []) == {
        "row_count": 0
    }
    failing, _ = fake_odbc(monkeypatch, Cursor(error=pyodbc.Error("insert failed")))
    with pytest.raises(pyodbc.Error):
        SQLServerConnection(SQL_CONFIG, SQL_SECRET).insert("x", [{"id": 1}])
    assert failing.calls == ["rollback", "close"]


def test_saved_connection_secrets_are_encrypted_redacted_and_preserved(client):
    item = saved(client)
    store = client.app.state.store
    record = store.get("connections", item["id"])
    assert (
        "_secrets" not in item
        and "secrets" not in item
        and item["secret_fields"] == ["password"]
    )

    assert SQL_SECRET["password"] not in json.dumps(record)
    assert (
        json.loads(SecretBox(store.directory).decrypt(record["_secrets"])) == SQL_SECRET
    )
    updated = client.put(
        f"/api/connections/{item['id']}",
        json={
            "name": "Warehouse renamed",
            "type": "sqlserver",
            "config": {**SQL_CONFIG, "password": "do-not-store-in-config"},
            "secrets": {},
        },
    )
    assert updated.status_code == 200 and "password" not in updated.json()["config"]
    assert "do-not-store-in-config" not in json.dumps(
        store.get("connections", item["id"])
    )
    assert connections_for_runtime(store) == []
    assert (
        connections_for_runtime(
            store, connection_ids=[item["id"] for item in store.list("connections")]
        )[0]["secrets"]
        == SQL_SECRET
    )
    registry = ConnectionRegistry(
        connections_for_runtime(
            store, connection_ids=[item["id"] for item in store.list("connections")]
        )
    )
    assert registry.sqlserver("warehouse RENAMED").secrets == SQL_SECRET
    assert SQL_SECRET["password"] not in json.dumps(registry.list())
    assert SQL_SECRET["password"] not in client.get("/api/connections").text
    assert (
        client.post(
            "/api/connections",
            json={
                "name": "Warehouse renamed",
                "type": "sqlserver",
                "config": SQL_CONFIG,
                "secrets": SQL_SECRET,
            },
        ).status_code
        == 409
    )
    assert (
        client.put(
            f"/api/connections/{item['id']}",
            json={
                "name": "Warehouse renamed",
                "type": "sqlserver",
                "config": SQL_CONFIG,
                "secrets": {"password": None},
            },
        ).status_code
        == 400
    )


def test_connection_update_preserves_omitted_project_allowlist(client):
    item = saved(client)
    store = client.app.state.store
    store.put(
        "projects", {"id": "restricted", "name": "Restricted", "cluster_ids": ["local"]}
    )
    record = store.get("connections", item["id"])
    record["project_ids"] = ["restricted"]
    store.put("connections", record)
    response = client.put(
        f"/api/connections/{item['id']}",
        json={
            "name": item["name"],
            "type": "sqlserver",
            "config": SQL_CONFIG,
            "secrets": {},
        },
    )
    assert response.status_code == 200
    assert response.json()["project_ids"] == ["restricted"]
    assert connections_for_runtime(store, "default", [item["id"]]) == []
    assert (
        connections_for_runtime(store, "restricted", [item["id"]])[0]["id"]
        == item["id"]
    )


def test_connection_roles_query_routes_and_driver_errors_do_not_reveal_secrets(
    client, monkeypatch
):
    item = saved(client)
    client.post(
        "/api/users",
        json={
            "username": "analyst",
            "password": "reader-password-long",
            "role": "member",
        },
    ).raise_for_status()
    client.post("/api/auth/logout").raise_for_status()
    assert client.get("/api/connections").status_code == 401
    client.post(
        "/api/auth/login",
        json={"username": "analyst", "password": "reader-password-long"},
    ).raise_for_status()
    assert client.get("/api/connections").status_code == 200
    assert (
        client.post(
            "/api/connections",
            json={
                "name": "New",
                "type": "sqlserver",
                "config": SQL_CONFIG,
                "secrets": SQL_SECRET,
            },
        ).status_code
        == 403
    )
    assert client.delete(f"/api/connections/{item['id']}").status_code == 403
    cursor = Cursor()
    connection, _ = fake_odbc(monkeypatch, cursor)
    assert client.post(
        f"/api/connections/{item['id']}/query", json={"sql": "SELECT ?", "params": [42]}
    ).json()["rows"] == [[42]]
    assert connection.calls == ["rollback", "close"]
    fake_odbc(
        monkeypatch,
        Cursor(
            error=pyodbc.Error(
                "PWD={private-sql-password}; Authorization: private-token-value"
            )
        ),
    )
    response = client.post(
        f"/api/connections/{item['id']}/execute",
        json={"sql": "UPDATE important SET value=?", "params": [42]},
    )
    assert (
        response.status_code == 400
        and "private-sql-password" not in response.text
        and "private-token-value" not in response.text
    )
    assert "Check the endpoint" in response.text


class S3Client:
    def __init__(self):
        self.objects = {("raw", "first.csv"): b"id\n1\n"}
        self.calls = []
        self.last_body = None

    def list_buckets(self):
        return {
            "Buckets": [
                {
                    "Name": "raw",
                    "CreationDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
                }
            ]
        }

    def create_bucket(self, **kwargs):
        self.calls.append(("create_bucket", kwargs))

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        return {
            "Contents": [{"Key": "first.csv", "Size": 5}],
            "CommonPrefixes": [{"Prefix": "nested/"}],
            "NextContinuationToken": "next-page-token",
        }

    def get_object(self, Bucket, Key):
        self.last_body = io.BytesIO(self.objects[(Bucket, Key)])
        return {
            "Body": self.last_body,
            "ContentLength": len(self.objects[(Bucket, Key)]),
        }

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def upload_fileobj(self, stream, bucket, key):
        self.objects[(bucket, key)] = stream.read()

    def upload_file(self, filename, bucket, key):
        self.objects[(bucket, key)] = Path(filename).read_bytes()

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)])

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def test_s3_pagination_reads_writes_and_downloads_close_bodies(monkeypatch, tmp_path):
    transport = S3Client()
    monkeypatch.setattr(S3Connection, "client", property(lambda self: transport))
    value = S3Connection(S3_CONFIG, S3_SECRET)
    page = value.list(
        "raw", prefix="input/", continuation_token="previous-page", limit=5000
    )
    assert page["prefixes"] == ["nested/"] and page["next_token"] == "next-page-token"
    assert transport.calls[-1][1] == {
        "Bucket": "raw",
        "Prefix": "input/",
        "MaxKeys": 1000,
        "Delimiter": "/",
        "ContinuationToken": "previous-page",
    }
    assert (
        value.read_text("raw", "first.csv") == "id\n1\n" and transport.last_body.closed
    )
    with pytest.raises(ValueError):
        value.read_bytes("raw", "first.csv", max_bytes=2)
    assert transport.last_body.closed
    assert (
        value.write_text("raw", "nested/file.txt", "hello")
        == "s3://raw/nested/file.txt"
    )
    downloaded = tmp_path / "result.txt"
    assert value.download_file("raw", "nested/file.txt", downloaded) == str(downloaded)
    assert downloaded.read_text() == "hello"
    value.upload_file(downloaded, "raw", "uploaded.txt")
    assert value.read_bytes("raw", "uploaded.txt") == b"hello"
    value.delete("raw", "uploaded.txt")
    assert ("raw", "uploaded.txt") not in transport.objects
    alternate = S3Connection({**S3_CONFIG, "region": "eu-west-1"}, S3_SECRET)
    alternate.create_bucket("another")
    assert transport.calls[-1][1]["CreateBucketConfiguration"] == {
        "LocationConstraint": "eu-west-1"
    }


def test_s3_api_upload_stream_download_and_delete(client, monkeypatch):
    item = saved(client, "s3")
    transport = S3Client()
    monkeypatch.setattr(S3Connection, "client", property(lambda self: transport))
    assert (
        client.get(f"/api/connections/{item['id']}/buckets").json()["items"][0]["name"]
        == "raw"
    )
    page = client.get(
        f"/api/connections/{item['id']}/objects",
        params={"bucket": "raw", "continuation_token": "opaque", "limit": 10},
    ).json()
    assert page["next_token"] == "next-page-token"
    uploaded = client.post(
        f"/api/connections/{item['id']}/objects",
        data={"bucket": "raw", "key": "new.csv"},
        files={"file": ("new.csv", b"id\n2\n")},
    )
    assert uploaded.status_code == 201 and uploaded.json()["size"] == 5
    downloaded = client.get(
        f"/api/connections/{item['id']}/download",
        params={"bucket": "raw", "key": "new.csv"},
    )
    assert downloaded.content == b"id\n2\n" and transport.last_body.closed
    assert (
        client.delete(
            f"/api/connections/{item['id']}/objects",
            params={"bucket": "raw", "key": "new.csv"},
        ).status_code
        == 200
    )
    assert ("raw", "new.csv") not in transport.objects
    assert (
        client.post(
            f"/api/connections/{item['id']}/query", json={"sql": "SELECT 1"}
        ).status_code
        == 404
    )


def test_s3_sdk_uses_saved_credentials_endpoint_and_checksum_compatibility(monkeypatch):
    import boto3

    calls = []
    monkeypatch.setattr(
        boto3,
        "client",
        lambda *args, **kwargs: calls.append((args, kwargs)) or object(),
    )
    S3Connection(
        S3_CONFIG, {**S3_SECRET, "session_token": "private-session-token"}
    ).client
    args, values = calls[0]
    assert args == ("s3",) and values["endpoint_url"] == S3_CONFIG["endpoint_url"]
    assert (
        values["aws_access_key_id"] == S3_SECRET["access_key"]
        and values["aws_secret_access_key"] == S3_SECRET["secret_key"]
    )
    assert (
        values["aws_session_token"] == "private-session-token"
        and values["verify"] is True
    )
    assert values["config"].s3["addressing_style"] == "path"
    assert values["config"].request_checksum_calculation == "when_required"


def test_local_seaweed_bootstrap_and_persistent_key_encryption(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_SECRET_KEY", raising=False)
    monkeypatch.setenv("OPENBRICKS_S3_ENDPOINT", "http://seaweedfs:8333")
    monkeypatch.setenv("OPENBRICKS_S3_ACCESS_KEY", "local-test-access")
    monkeypatch.setenv("OPENBRICKS_S3_SECRET_KEY", "local-test-secret")
    store = Store(tmp_path / "data")
    bootstrap_connections(store)
    bootstrap_connections(store)
    items = store.list("connections")
    assert len(items) == 1 and items[0]["id"] == "local-seaweedfs"
    assert "local-test-secret" not in json.dumps(items)
    box = SecretBox(store.directory)
    cipher = box.encrypt("a different secret")
    assert SecretBox(store.directory).decrypt(cipher) == "a different secret"
    assert (
        connections_for_runtime(store, connection_ids=["local-seaweedfs"])[0][
            "secrets"
        ]["secret_key"]
        == "local-test-secret"
    )
    with pytest.raises(ValueError, match="cannot be decrypted"):
        SecretBox(tmp_path / "other").decrypt(cipher)


def test_connector_configuration_rejects_unsafe_endpoints_and_bad_ranges():
    for endpoint in (
        "file:///tmp/socket",
        "http://access:secret@host:8333",
        "https://host?key=secret",
        "https://host/#secret",
    ):
        with pytest.raises(ValueError):
            normalize_config("s3", {**S3_CONFIG, "endpoint_url": endpoint})
    for override in (
        {"port": 0},
        {"port": 70000},
        {"timeout": 0},
        {"timeout": 500},
        {"server": ""},
    ):
        with pytest.raises(ValueError):
            normalize_config("sqlserver", {**SQL_CONFIG, **override})
