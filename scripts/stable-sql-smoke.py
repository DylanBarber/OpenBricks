"""Real SQL Server integration through a disposable OpenBricks UI proxy.

The HTTP phase accepts only http://127.0.0.1:3001. Bootstrap/cleanup must run
inside the isolated QA network with the packaged ODBC driver. They accept only
the SQL hostname 'sqlserver', and own a uniquely marked openbricks_stable_* DB.
Credentials are read from stdin/environment and the ignored fixture file;
neither notebook source nor the public receipt contains passwords.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "outcome_unknown"}


def quote(value):
    return "[" + value.replace("]", "]]") + "]"


def literal(value):
    return "N'" + value.replace("'", "''") + "'"


def settings(args):
    supplied = json.load(sys.stdin) if args.bootstrap_stdin else {}
    return {
        "server": supplied.get(
            "server", os.getenv("OPENBRICKS_SQLSERVER_HOST", "sqlserver")
        ),
        "password": supplied.get("admin_password")
        or os.getenv("OPENBRICKS_SQLSERVER_PASSWORD"),
    }


def bootstrap(args):
    from openbricks.connectors import SQLServerConnection

    supplied = settings(args)
    if supplied["server"] != "sqlserver" or not supplied["password"]:
        raise ValueError(
            "Bootstrap requires the isolated sqlserver hostname and its test administrator password"
        )
    target = Path(args.fixture)
    if target.exists():
        raise ValueError(
            "Refusing to replace an existing SQL fixture; clean up its marked database first"
        )
    suffix = uuid4().hex
    database = "openbricks_stable_" + suffix
    writer, reader = "ob_writer_" + suffix, "ob_reader_" + suffix
    writer_password, reader_password = (
        secrets.token_urlsafe(28) + "Aa1!",
        secrets.token_urlsafe(28) + "Aa1!",
    )
    config = {
        "server": "sqlserver",
        "port": 1433,
        "database": "master",
        "username": "sa",
        "encrypt": True,
        "trust_server_certificate": True,
        "timeout": 30,
    }
    admin = SQLServerConnection(config, {"password": supplied["password"]})
    connection = admin.connect()
    connection.autocommit = True
    cursor = connection.cursor()
    fixture = {
        "database": database,
        "marker": suffix,
        "writer": writer,
        "writer_password": writer_password,
        "reader": reader,
        "reader_password": reader_password,
        "server": "sqlserver",
        "port": 1433,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    try:
        cursor.execute("CREATE DATABASE " + quote(database))
        cursor.execute(
            "ALTER DATABASE " + quote(database) + " SET ALLOW_SNAPSHOT_ISOLATION ON"
        )
        cursor.execute(
            "ALTER DATABASE "
            + quote(database)
            + " SET CHANGE_TRACKING=ON (CHANGE_RETENTION=7 DAYS,AUTO_CLEANUP=ON)"
        )
        cursor.execute(
            "CREATE LOGIN "
            + quote(writer)
            + " WITH PASSWORD="
            + literal(writer_password)
            + ", CHECK_POLICY=ON"
        )
        cursor.execute(
            "CREATE LOGIN "
            + quote(reader)
            + " WITH PASSWORD="
            + literal(reader_password)
            + ", CHECK_POLICY=ON"
        )
        cursor.execute("USE " + quote(database))
        cursor.execute(
            "EXEC sys.sp_addextendedproperty @name=N'openbricks.stable.fixture', @value=?",
            suffix,
        )
        cursor.execute("CREATE USER " + quote(writer) + " FOR LOGIN " + quote(writer))
        cursor.execute("CREATE USER " + quote(reader) + " FOR LOGIN " + quote(reader))
        cursor.execute("""CREATE TABLE dbo.DriverProbe (
            id int NOT NULL PRIMARY KEY, label nvarchar(200) NOT NULL,
            amount decimal(20,6) NULL, business_date date NULL, payload varbinary(32) NULL);
            CREATE TABLE dbo.BulkProbe (row_id bigint NOT NULL PRIMARY KEY,label nvarchar(100) NOT NULL);
            INSERT INTO dbo.BulkProbe SELECT TOP(12073) ROW_NUMBER() OVER(ORDER BY (SELECT NULL)),N'bulk'
              FROM sys.all_objects a CROSS JOIN sys.all_objects b;
            CREATE TABLE dbo.OrdersComposite (
              tenant_id int NOT NULL,order_id bigint NOT NULL,amount decimal(28,8) NOT NULL,
              label nvarchar(200) NOT NULL,business_date date NOT NULL,modified_at datetime2(6) NOT NULL,
              optional_note nvarchar(200) NULL,payload varbinary(32) NULL,
              zoned_at datetimeoffset(6) NULL,is_active bit NOT NULL,
              CONSTRAINT PK_OrdersComposite PRIMARY KEY(tenant_id,order_id));
            ALTER TABLE dbo.OrdersComposite ENABLE CHANGE_TRACKING;
            INSERT INTO dbo.OrdersComposite VALUES
              (1,1,12345678901234567890.12345678,N'O''Brien ? 雪','2026-01-02','2026-01-02T03:04:05.123456',NULL,0x0001FF,'2026-01-02T08:34:05.123456+05:30',1),
              (1,2,20.00000001,N'東京','2026-01-03','2026-01-03T00:00:00',N'optional',NULL,NULL,0),
              (2,9223372036854775807,0.00000001,N'🙂','2026-01-04','2026-01-04T00:00:00',N'',0xFF,'2026-01-04T00:00:00-04:00',1);
            CREATE TABLE dbo.TimeProbe (id int NOT NULL PRIMARY KEY,clock time(6) NOT NULL);
            ALTER TABLE dbo.TimeProbe ENABLE CHANGE_TRACKING;
            INSERT INTO dbo.TimeProbe VALUES(1,'12:34:56.123456');""")
        for table in ("DriverProbe", "BulkProbe", "OrdersComposite", "TimeProbe"):
            cursor.execute(
                "GRANT SELECT,INSERT,UPDATE,DELETE ON OBJECT::dbo."
                + quote(table)
                + " TO "
                + quote(writer)
            )
            cursor.execute(
                "GRANT SELECT ON OBJECT::dbo." + quote(table) + " TO " + quote(reader)
            )
        for table in ("OrdersComposite", "TimeProbe"):
            for login in (writer, reader):
                cursor.execute(
                    "GRANT VIEW CHANGE TRACKING ON OBJECT::dbo."
                    + quote(table)
                    + " TO "
                    + quote(login)
                )
        fixture["ready"] = True
        target.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
        print(
            "PASS: uniquely marked SQL database, typed/composite-key fixtures, least-privilege writer and reader created",
            flush=True,
        )
    finally:
        cursor.close()
        connection.close()


def cleanup(args):
    from openbricks.connectors import SQLServerConnection

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    supplied = settings(args)
    if supplied["server"] != "sqlserver" or not supplied["password"]:
        raise ValueError(
            "Cleanup requires the isolated SQL server and test administrator password"
        )
    suffix = fixture["marker"]
    if (
        not re.fullmatch("[a-f0-9]{32}", suffix)
        or fixture["database"] != "openbricks_stable_" + suffix
    ):
        raise ValueError("Invalid fixture ownership marker")
    connection = SQLServerConnection(
        {
            "server": "sqlserver",
            "port": 1433,
            "database": "master",
            "username": "sa",
            "trust_server_certificate": True,
        },
        {"password": supplied["password"]},
    ).connect()
    connection.autocommit = True
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT DB_ID(?)", fixture["database"])
        if cursor.fetchone()[0] is not None:
            cursor.execute(
                "SELECT CAST(value AS nvarchar(128)) FROM "
                + quote(fixture["database"])
                + ".sys.extended_properties WHERE class=0 AND name=N'openbricks.stable.fixture'"
            )
            marker = cursor.fetchone()
            if not marker or marker[0] != suffix:
                raise ValueError(
                    "Database ownership marker does not match; cleanup refused"
                )
            cursor.execute(
                "ALTER DATABASE "
                + quote(fixture["database"])
                + " SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
            )
            cursor.execute("DROP DATABASE " + quote(fixture["database"]))
        for key, prefix in (("writer", "ob_writer_"), ("reader", "ob_reader_")):
            if fixture[key] != prefix + suffix:
                raise ValueError("Login ownership marker does not match")
            cursor.execute(
                "IF EXISTS(SELECT 1 FROM sys.sql_logins WHERE name=?) DROP LOGIN "
                + quote(fixture[key]),
                fixture[key],
            )
        print(
            "PASS: only this proof's marked SQL database and two logins removed",
            flush=True,
        )
    finally:
        cursor.close()
        connection.close()


class Proof:
    def __init__(self, args):
        parsed = urlsplit(args.url)
        if (
            args.url.rstrip("/") != "http://127.0.0.1:3001"
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "The HTTP proof accepts only disposable http://127.0.0.1:3001"
            )
        self.fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        if not self.fixture.get("ready") or not re.fullmatch(
            "openbricks_stable_[a-f0-9]{32}", self.fixture["database"]
        ):
            raise ValueError("Bootstrap a marked SQL fixture before the HTTP proof")
        self.client = httpx.Client(base_url=args.url.rstrip("/") + "/api", timeout=180)
        self.suffix = self.fixture["marker"][:10]
        self.output = Path(args.receipt)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.state = {
            "format": "openbricks-stable-sql-proof-v1",
            "database": self.fixture["database"],
            "checks": [],
            "runs": [],
            "executions": [],
            "ingestions": [],
            "notebooks": [],
        }
        self.secret_values = [
            self.fixture["writer_password"],
            self.fixture["reader_password"],
            os.getenv("OPENBRICKS_SQLSERVER_PASSWORD", ""),
            os.getenv("OPENBRICKS_SMOKE_PASSWORD", ""),
            os.getenv("OPENBRICKS_ADMIN_PASSWORD", ""),
        ]
        self.project = self.connection = self.reader = self.admin_connection = None
        if args.resume:
            previous = json.loads(self.output.read_text(encoding="utf-8"))
            if previous.get("database") != self.fixture["database"]:
                raise ValueError("Resume receipt belongs to another SQL fixture")
            self.state = previous
            self.state.setdefault("failure_history", []).extend(
                self.state.pop("failures", [])
            )
            self.project = previous["project_id"]
            values = previous["connection_ids"]
            self.connection, self.reader, self.admin_connection = (
                values["writer"],
                values["reader"],
                values["fixture_admin"],
            )
            self.ingestion_id = (
                previous["ingestions"][0] if previous["ingestions"] else None
            )
            self.sink = previous.get("sink")
            self.login()

    def save(self):
        self.output.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def passed(self, message):
        self.state["checks"].append(message)
        self.save()
        print("PASS: " + message, flush=True)

    def request(self, method, path, *, expected=None, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if expected is None:
            if not response.is_success:
                raise AssertionError(f"{method} {path}: HTTP {response.status_code}")
        elif response.status_code != expected:
            raise AssertionError(
                f"{method} {path}: expected {expected}, received {response.status_code}"
            )
        content = response.text
        if any(value and value in content for value in self.secret_values):
            raise AssertionError("A public API response exposed a SQL credential")
        return response.json() if response.content else None

    def query(self, sql, params=None, *, connection=None, limit=1000):
        return self.request(
            "POST",
            "/connections/" + (connection or self.connection) + "/query",
            json={"sql": sql, "params": params or [], "limit": limit},
        )

    def execute(self, sql, params=None, *, admin=False, expected=None):
        return self.request(
            "POST",
            "/connections/"
            + (self.admin_connection if admin else self.connection)
            + "/execute",
            expected=expected,
            json={"sql": sql, "params": params or []},
        )

    def wait_run(self, run_id, *, expected="succeeded", timeout=240):
        self.state["runs"].append(run_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.request("GET", "/runs/" + run_id)
            if run["status"] in TERMINAL:
                self.save()
                if run["status"] != expected:
                    raise AssertionError(
                        "Notebook finished "
                        + run["status"]
                        + ": "
                        + str(run.get("error", ""))[:600]
                    )
                return run
            time.sleep(0.3)
        raise AssertionError("Timed out waiting for SQL notebook")

    def notebook(self, name, source, *, expect="succeeded", connections=None):
        item = self.request(
            "POST",
            "/notebooks",
            json={
                "project_id": self.project,
                "name": "Stable SQL " + name + " " + self.suffix,
                "cluster_id": "local",
                "connection_ids": (
                    [self.connection] if connections is None else connections
                ),
                "cells": [{"id": "proof", "language": "python", "source": source}],
            },
        )
        self.state["notebooks"].append(item["id"])
        result = self.request(
            "POST",
            "/notebooks/" + item["id"] + "/run",
            json={"resource_options": {"mode": "job"}},
        )
        return self.wait_run(result["id"], expected=expect)

    def operation(self, operation, *, engine="spark", expect="succeeded", **values):
        response = self.request(
            "POST",
            "/data/execute",
            json={
                "project_id": self.project,
                "cluster_id": "local",
                "engine": engine,
                "operation": operation,
                "connection_id": self.connection if engine == "sqlserver" else None,
                **values,
            },
        )
        return self.execution(response, expect=expect)

    def execution(self, response, *, expect="succeeded"):
        self.state["executions"].append(response["id"])
        run = self.wait_run(response["run_id"], expected=expect)
        result = self.request("GET", "/data/executions/" + response["id"])
        if result["status"] != expect:
            raise AssertionError("Execution status did not match terminal run")
        return result, run

    def ingestion(self):
        rows = self.request(
            "GET", "/data/ingestions", params={"project_id": self.project, "limit": 100}
        )["items"]
        return next(item for item in rows if item["id"] == self.ingestion_id)

    def sync(self, *, expect="succeeded"):
        result = self.request(
            "POST", "/data/ingestions/" + self.ingestion_id + "/run", json={}
        )
        return self.execution(result, expect=expect)

    def login(self):
        password = os.getenv("OPENBRICKS_SMOKE_PASSWORD") or os.getenv(
            "OPENBRICKS_ADMIN_PASSWORD"
        )
        if not password:
            raise ValueError("Set the disposable OpenBricks administrator password")
        self.request(
            "POST",
            "/auth/login",
            json={
                "username": os.getenv("OPENBRICKS_SMOKE_USERNAME", "admin"),
                "password": password,
            },
        )

    def setup(self):
        self.login()
        project = self.request(
            "POST",
            "/projects",
            json={"name": "Stable SQL proof " + self.suffix, "cluster_ids": ["local"]},
        )
        self.project = project["id"]
        self.state["project_id"] = self.project
        admin_project = self.request(
            "POST",
            "/projects",
            json={
                "name": "Stable SQL fixture administration " + self.suffix,
                "cluster_ids": ["local"],
            },
        )
        admin_password = os.getenv("OPENBRICKS_SQLSERVER_PASSWORD")
        if not admin_password:
            raise ValueError(
                "The fixture administrator password is required only for isolated fault injection"
            )
        for key, username, secret, project_id in (
            (
                "connection",
                self.fixture["writer"],
                self.fixture["writer_password"],
                self.project,
            ),
            (
                "reader",
                self.fixture["reader"],
                self.fixture["reader_password"],
                self.project,
            ),
            ("admin_connection", "sa", admin_password, admin_project["id"]),
        ):
            response = self.request(
                "POST",
                "/connections",
                json={
                    "name": "Stable SQL " + key + " " + self.suffix,
                    "type": "sqlserver",
                    "project_ids": [project_id],
                    "config": {
                        "server": "sqlserver",
                        "port": 1433,
                        "database": self.fixture["database"],
                        "username": username,
                        "encrypt": True,
                        "trust_server_certificate": True,
                        "timeout": 30,
                    },
                    "secrets": {"password": secret},
                },
            )
            setattr(self, key, response["id"])
            assert (
                response["secret_fields"] == ["password"] and "_secrets" not in response
            )
            assert self.request(
                "POST", "/connections/" + response["id"] + "/test", json={}
            )["ok"]
        self.state["connection_ids"] = {
            "writer": self.connection,
            "reader": self.reader,
            "fixture_admin": self.admin_connection,
        }
        self.passed(
            "saved SQL connections authenticate to the marked database without exposing credentials"
        )

    def driver(self):
        label = "O'Brien ? 雪 🙂; DROP TABLE dbo.DriverProbe; --"
        self.request(
            "POST",
            "/connections/" + self.connection + "/insert",
            json={
                "table": "DriverProbe",
                "rows": [
                    {
                        "id": 1,
                        "label": label,
                        "amount": "12345678901234.123456",
                        "business_date": "2026-01-02",
                    },
                    {"id": 2, "label": "NULLs", "amount": None, "business_date": None},
                ],
            },
        )
        row = self.query(
            "SELECT label,CONVERT(nvarchar(50),amount),CONVERT(nvarchar(10),business_date,23),payload FROM dbo.DriverProbe WHERE id=?",
            [1],
        )["rows"][0]
        assert row == [label, "12345678901234.123456", "2026-01-02", None]
        assert self.query(
            "SELECT label,amount,business_date,payload FROM dbo.DriverProbe WHERE id=?",
            [2],
        )["rows"] == [["NULLs", None, None, None]]
        self.execute(
            "UPDATE dbo.DriverProbe SET payload=CONVERT(varbinary(32),?,2) WHERE id=?",
            ["0001ff", 1],
        )
        assert self.query("SELECT payload FROM dbo.DriverProbe WHERE id=?", [1])[
            "rows"
        ] == [["0001ff"]]
        self.execute(
            "INSERT INTO dbo.DriverProbe(id,label) VALUES(3,?); THROW 50001,'expected rollback',1",
            ["rolled back"],
            expected=400,
        )
        assert self.query("SELECT COUNT(*) FROM dbo.DriverProbe WHERE id=3")[
            "rows"
        ] == [[0]]
        self.request(
            "POST",
            "/connections/" + self.connection + "/insert",
            expected=400,
            json={
                "table": "DriverProbe",
                "rows": [
                    {"id": 4, "label": "batch must rollback"},
                    {"id": 1, "label": "duplicate primary key"},
                ],
            },
        )
        assert self.query("SELECT COUNT(*) FROM dbo.DriverProbe WHERE id=4")[
            "rows"
        ] == [[0]]
        assert self.query(
            "SELECT IS_SRVROLEMEMBER('sysadmin'),HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','ALTER')"
        )["rows"] == [[0, 0]]
        self.execute("CREATE TABLE dbo.MustNotExist(id int)", expected=400)
        self.request(
            "POST",
            "/connections/" + self.reader + "/insert",
            expected=400,
            json={
                "table": "DriverProbe",
                "rows": [{"id": 9, "label": "reader cannot write"}],
            },
        )
        assert self.query("SELECT COUNT(*) FROM dbo.DriverProbe")["rows"] == [[2]]
        observed = self.query(
            "INSERT INTO dbo.DriverProbe(id,label) VALUES(10,N'query rollback'); SELECT COUNT(*) AS observed FROM dbo.DriverProbe"
        )
        assert observed["rows"] == [[3]]
        assert self.query("SELECT COUNT(*) FROM dbo.DriverProbe WHERE id=10")[
            "rows"
        ] == [[0]]
        limited = self.query(
            "SELECT row_id FROM dbo.BulkProbe ORDER BY row_id", limit=5
        )
        assert (
            limited["row_count"] == 5
            and limited["truncated"]
            and limited["rows"] == [[n] for n in range(1, 6)]
        )
        self.passed(
            "real ODBC parameter binding, Unicode/decimal/date/binary/nulls, commit and rollback, bounded previews, least-privilege denials"
        )

    def data_workspace(self):
        result, _ = self.operation(
            "query",
            engine="sqlserver",
            sql="SELECT label FROM dbo.DriverProbe WHERE id=?",
            params=[1],
        )
        assert result["result"]["rows"][0][0].startswith("O'Brien ? 雪")
        for operation, options in (
            ("schemas", {}),
            ("tables", {"schema": "dbo"}),
            ("columns", {"schema": "dbo", "table": "OrdersComposite"}),
            ("preview", {"schema": "dbo", "table": "DriverProbe"}),
        ):
            result, _ = self.operation(
                operation, engine="sqlserver", limit=1, **options
            )
            assert result["result"]["row_count"] == 1
        saved = self.request(
            "POST",
            "/data/queries",
            json={
                "project_id": self.project,
                "name": "Bound SQL Server saved query",
                "engine": "sqlserver",
                "connection_id": self.connection,
                "sql": "SELECT label FROM dbo.DriverProbe WHERE id=?",
                "params": [1],
            },
        )
        response = self.request(
            "POST",
            "/data/execute",
            json={
                "project_id": self.project,
                "query_id": saved["id"],
                "operation": "query",
            },
        )
        result, _ = self.execution(response)
        assert result["result"]["rows"][0][0].startswith("O'Brien ? 雪")
        self.state["saved_query_id"] = saved["id"]
        self.passed(
            "queued SQL Server query, source browsing/paging and saved-query reload execute through the remote compute API"
        )

    def notebook_jdbc(self):
        code = f"""connection = connections.sqlserver({self.connection!r})
assert connection.read("SELECT COUNT(*) AS n FROM dbo.DriverProbe")[0]["n"] == 2
assert connection.insert("DriverProbe", [{{"id": 20, "label": "notebook bound ? ' 雪"}}])["row_count"] == 1
assert connection.read("SELECT label FROM dbo.DriverProbe WHERE id=?", [20])[0]["label"] == "notebook bound ? ' 雪"
frame = connection.read_spark(spark, "dbo.BulkProbe", fetch_size=512, partition_column="row_id", lower_bound=1, upper_bound=12073, num_partitions=2)
assert frame.count() == 12073
assert frame.where("row_id > 12000").count() == 73
print("Native JDBC and notebook SQL helper assertions passed")"""
        self.notebook("JDBC", code)
        omitted = self.notebook(
            "credential selection",
            f"connections.sqlserver({self.connection!r}).read('SELECT 1')",
            expect="failed",
            connections=[],
        )
        assert omitted.get("error")
        self.passed(
            "notebook SQL writes and native Spark JDBC read all 12,073 rows; an unselected connection is not dispatched"
        )

    def initial_sync(self):
        self.sink = "blob://stable-sql-" + self.suffix + "/composite_orders"
        created = self.request(
            "POST",
            "/data/ingestions",
            json={
                "project_id": self.project,
                "name": "Composite typed Change Tracking",
                "mode": "change_tracking",
                "connection_id": self.connection,
                "cluster_id": "local",
                "source": {"schema": "dbo", "table": "OrdersComposite"},
                "sink": {"path": self.sink},
                "keys": ["tenant_id", "order_id"],
                "jdbc": {"fetch_size": 2},
                "checks": [
                    {"type": "not_null", "column": "tenant_id"},
                    {"type": "not_null", "column": "order_id"},
                ],
            },
        )
        self.ingestion_id = created["id"]
        self.state["ingestions"].append(self.ingestion_id)
        self.state["sink"] = self.sink
        self.sync()
        self.checkpoint = self.ingestion()["watermark_value"]
        self.notebook(
            "typed Delta snapshot",
            f"""from decimal import Decimal
from datetime import date
rows = spark.read.format("delta").load(blob.path({self.sink!r})).orderBy("tenant_id","order_id").collect()
assert len(rows) == 3
assert rows[0].amount == Decimal("12345678901234567890.12345678")
assert rows[0].label == "O'Brien ? 雪" and rows[0].business_date == date(2026,1,2)
assert rows[0].optional_note is None and bytes(rows[0].payload) == bytes.fromhex("0001ff")
assert rows[0].zoned_at.isoformat() == "2026-01-02T03:04:05.123456"
assert rows[2].order_id == 9223372036854775807 and rows[2].label == "🙂"
assert bool(rows[0].is_active) and not bool(rows[1].is_active)
print("Composite-key and typed snapshot assertions passed")""",
        )
        self.passed(
            "saved Change Tracking ingestion snapshots composite keys, exact decimal/date/offset/Unicode/binary/null/boolean values into Delta"
        )

    def changes(self):
        self.execute(
            "UPDATE dbo.OrdersComposite SET amount=?,label=? WHERE tenant_id=? AND order_id=?; DELETE dbo.OrdersComposite WHERE tenant_id=? AND order_id=?; INSERT INTO dbo.OrdersComposite VALUES(3,4,40,N'late timestamp','1999-01-01','1999-01-01',NULL,0x02,NULL,1)",
            ["99.12345678", "updated 雪", 1, 1, 1, 2],
        )
        self.sync()
        changed = self.ingestion()["watermark_value"]
        assert changed["version"] > self.checkpoint["version"]
        self.checkpoint = changed
        self.sync()
        assert self.ingestion()["watermark_value"] == changed
        self.assert_current()
        self.passed(
            "Change Tracking captures updates, deletions and a late-timestamp insert; no-op replay preserves the checkpoint and creates no duplicates"
        )

    def assert_current(self):
        run = self.notebook(
            "sink verification",
            f"""from decimal import Decimal
import hashlib,json
frame = spark.read.format("delta").load(blob.path({self.sink!r}))
rows = frame.orderBy("tenant_id","order_id").collect()
assert [(r.tenant_id,r.order_id) for r in rows] == [(1,1),(2,9223372036854775807),(3,4)]
assert rows[0].amount == Decimal("99.12345678") and rows[0].label == "updated 雪"
assert str(rows[2].business_date) == "1999-01-01"
def exact(value):
    if isinstance(value,(bytes,bytearray)):
        return bytes(value).hex()
    if hasattr(value,"isoformat"):
        return value.isoformat()
    return str(value)
payload = json.dumps({{"schema":frame.schema.jsonValue(),"rows":[r.asDict(recursive=True) for r in rows]}},sort_keys=True,ensure_ascii=False,default=exact,separators=(",",":"))
print("STABLE_SQL_DIGEST="+hashlib.sha256(payload.encode("utf-8")).hexdigest())
print("No missing, stale or duplicate sink rows")""",
        )
        output = "\n".join(item.get("stdout", "") for item in run.get("outputs", []))
        found = re.findall(r"STABLE_SQL_DIGEST=([a-f0-9]{64})", output)
        assert len(found) == 1, "Sink verification did not emit one dataset fingerprint"
        previous = self.state.get("current_dataset_sha256")
        if previous is None:
            self.state["current_dataset_sha256"] = found[0]
            self.save()
        else:
            assert (
                found[0] == previous
            ), "Recovered sink changed a row value or its schema"

    def failures(self):
        before = deepcopy(self.ingestion()["watermark_value"])
        # Missing sink is simulated by a reversible move of this proof's own path.
        code = f"""from pathlib import Path
path = Path(blob.path({self.sink!r}))
assert path.name == "composite_orders" and path.parent.name == {("stable-sql-"+self.suffix)!r}
path.rename(path.with_name("composite_orders_missing_probe"))
print("Moved only the marked fixture sink")"""
        self.notebook("missing sink setup", code)
        try:
            _, failed = self.sync(expect="failed")
            assert "sink is missing" in failed.get("error", "")
            assert self.ingestion()["watermark_value"] == before
        finally:
            self.notebook(
                "missing sink recovery",
                f"""from pathlib import Path
path = Path(blob.path({self.sink!r}))
path.with_name("composite_orders_missing_probe").rename(path)
print("Restored marked fixture sink")""",
            )
        self.assert_current()
        self.passed(
            "missing Delta sink fails closed without advancing its saved checkpoint; reversible recovery preserves every row"
        )
        history, _ = self.operation(
            "delta_history", payload={"path": self.sink}, limit=100
        )
        index = history["result"]["columns"].index("version")
        current = int(history["result"]["rows"][0][index])
        self.operation("delta_restore", payload={"path": self.sink, "version": 0})
        try:
            _, failed = self.sync(expect="failed")
            assert "older than" in failed.get("error", "")
            assert self.ingestion()["watermark_value"] == before
        finally:
            self.operation(
                "delta_restore", payload={"path": self.sink, "version": current}
            )
        self.assert_current()
        self.passed(
            "Delta sink restored behind its saved source checkpoint is rejected; restoring the matching version recovers the exact dataset"
        )
        self.execute(
            "ALTER TABLE dbo.OrdersComposite ADD drift_probe nvarchar(30) NULL",
            admin=True,
        )
        try:
            _, failed = self.sync(expect="failed")
            assert "schema" in failed.get("error", "").lower()
            assert self.ingestion()["watermark_value"] == before
        finally:
            self.execute(
                "ALTER TABLE dbo.OrdersComposite DROP COLUMN drift_probe", admin=True
            )
        self.assert_current()
        self.passed(
            "source schema drift stops ingestion before target mutation and checkpoint advancement"
        )
        self.execute("TRUNCATE TABLE dbo.OrdersComposite", admin=True)
        _, failed = self.sync(expect="failed")
        assert (
            "recreated" in failed.get("error", "")
            or "checkpoint" in failed.get("error", "").lower()
        )
        assert self.ingestion()["watermark_value"] == before
        self.assert_current()
        self.passed(
            "a reset source checkpoint is rejected while the last valid Delta snapshot remains readable and unchanged"
        )

    def close(self):
        self.save()
        self.client.close()

    def check(self, name, operation):
        try:
            operation()
            return True
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, (AssertionError, ValueError))
                else type(exc).__name__
            )
            for secret in self.secret_values:
                if secret:
                    message = message.replace(secret, "[redacted]")
            self.state.setdefault("failures", []).append(
                {"stage": name, "message": message[:1000]}
            )
            self.save()
            print("FAIL: " + name + ": " + message[:1000], flush=True)
            return False

    def edge_types(self):
        self.check("native SQL datetimeoffset", self.offset_type)
        if self.check("prepare SQL time(7) precision", self.prepare_time):
            self.check("native SQL time(7)", self.native_time)
            self.check("SQL time(7) ingestion", self.time_type)

    def prepare_time(self):
        self.execute(
            "ALTER TABLE dbo.TimeProbe ALTER COLUMN clock time(7) NOT NULL; UPDATE dbo.TimeProbe SET clock='12:34:56.1234567' WHERE id=1",
            admin=True,
        )

    def native_time(self):
        assert self.query("SELECT clock FROM dbo.TimeProbe WHERE id=1")["rows"] == [
            ["12:34:56.1234567"]
        ], "SQL time(7) query lost its seventh fractional digit"
        self.passed("native SQL time(7) query preserves all seven fractional digits")

    def offset_type(self):
        result = self.query(
            "SELECT CAST('2026-01-04T00:00:00-04:00' AS datetimeoffset(6)) AS zoned_at"
        )
        # This method normally runs before source-reset failure injection.
        assert result["rows"] and result["rows"][0][0].endswith("-04:00")
        self.passed("ODBC datetimeoffset reads preserve the source UTC offset")

    def time_type(self):
        time_sink = "blob://stable-sql-" + self.suffix + "/sql_time_" + uuid4().hex[:6]
        created = self.request(
            "POST",
            "/data/ingestions",
            json={
                "project_id": self.project,
                "name": "SQL time precision preservation",
                "mode": "change_tracking",
                "connection_id": self.connection,
                "cluster_id": "local",
                "source": {"schema": "dbo", "table": "TimeProbe"},
                "sink": {"path": time_sink},
                "keys": ["id"],
            },
        )
        self.state["ingestions"].append(created["id"])
        response = self.request(
            "POST", "/data/ingestions/" + created["id"] + "/run", json={}
        )
        self.execution(response)
        self.notebook(
            "SQL time preservation",
            f"""rows = spark.read.format("delta").load(blob.path({time_sink!r})).collect()
assert len(rows) == 1 and str(rows[0].clock) == "12:34:56.1234567"
print("SQL time precision preserved")""",
        )
        self.passed(
            "ODBC time reads and Change Tracking time-of-day precision round-trip"
        )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--phase", required=True, choices=("bootstrap", "http", "cleanup")
    )
    parser.add_argument(
        "--fixture", default=str(ROOT / ".test-data/stable-sql-fixture.json")
    )
    parser.add_argument(
        "--receipt", default=str(ROOT / ".test-data/stable-sql-smoke.json")
    )
    parser.add_argument("--url", default="http://127.0.0.1:3001")
    parser.add_argument("--bootstrap-stdin", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse synthetic IDs for a focused section",
    )
    parser.add_argument(
        "--section", choices=("all", "types", "failures"), default="all"
    )
    args = parser.parse_args()
    if args.phase == "bootstrap":
        bootstrap(args)
        return
    if args.phase == "cleanup":
        cleanup(args)
        return
    proof = Proof(args)
    try:
        if args.section == "all":
            if args.resume:
                raise ValueError(
                    "Use a focused section for resume; the complete proof requires fresh fixture rows"
                )
            proof.setup()
            proof.check("driver", proof.driver)
            proof.check("data workspace", proof.data_workspace)
            proof.check("notebook JDBC", proof.notebook_jdbc)
            if proof.check("initial Change Tracking snapshot", proof.initial_sync):
                if proof.check("Change Tracking mutations and replay", proof.changes):
                    proof.check("SQL temporal types", proof.edge_types)
                    proof.check("Change Tracking failure matrix", proof.failures)
        else:
            if not args.resume:
                raise ValueError(
                    "Focused sections require --resume and an existing public receipt"
                )
            proof.check(
                args.section,
                proof.edge_types if args.section == "types" else proof.failures,
            )
        succeeded = not proof.state.get("failures")
        if args.section == "all":
            proof.state["passed"] = succeeded
        else:
            proof.state.setdefault("focused_sections", {})[args.section] = succeeded
        if succeeded:
            proof.passed(
                "complete real SQL Server UI-proxy proof"
                if args.section == "all"
                else "focused SQL " + args.section + " proof"
            )
        else:
            raise AssertionError(
                "One or more SQL proof stages failed; see the synthetic receipt"
            )
    finally:
        proof.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never echo a raw ODBC exception, SQL statement or private fixture.
        message = (
            str(exc)
            if isinstance(exc, (AssertionError, ValueError))
            else type(exc).__name__
        )
        print("FAIL: SQL stable proof: " + message[:1000], file=sys.stderr)
        sys.exit(1)
