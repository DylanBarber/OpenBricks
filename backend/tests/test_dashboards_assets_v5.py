"""Project isolation, durable dashboard refreshes and observed asset evidence."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from openbricks.assets import AssetService, asset_id, register_asset_routes
from openbricks.dashboards import (
    DashboardManager,
    bounded_result,
    register_dashboard_routes,
)
from openbricks.data_workspace import register_data_routes
from openbricks.permissions import Access, now
from openbricks.store import Store


class QueuedRuntime:
    def __init__(self, store):
        self.store, self.calls = store, []

    def submit(self, notebook, **options):
        self.calls.append((notebook, options))
        return self.store.put(
            "runs",
            {
                "id": options["run_id"],
                "project_id": notebook["project_id"],
                "data_execution_id": notebook["data_execution_id"],
                "status": "queued",
                "created_at": now(),
                "outputs": [],
            },
        )


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path)
    access = Access(store)
    access.seed()
    for project in ("one", "two"):
        store.put(
            "projects",
            {
                "id": project,
                "name": project,
                "cluster_ids": ["local"],
                "connection_ids": [],
            },
        )
    for username, role in (
        ("writer", "editor"),
        ("viewer", "viewer"),
        ("operator", "operator"),
    ):
        store.put("users", {"id": username, "username": username, "role": "member"})
        store.put(
            "project_members",
            {
                "id": "one:" + username,
                "project_id": "one",
                "user_id": username,
                "role": role,
            },
        )
    store.put("users", {"id": "admin", "username": "admin", "role": "admin"})
    app = FastAPI()
    app.state.store, app.state.access = store, access
    app.state.runtime = QueuedRuntime(store)
    app.state.maintenance = None
    app.state.scheduler = BackgroundScheduler()

    def authenticated(x_user: str = Header(default="writer")):
        return store.get("users", x_user) or {"id": x_user, "role": "member"}

    @app.exception_handler(ValueError)
    async def error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.state.data_workspace_api = register_data_routes(app, authenticated, access)
    app.state.assets = AssetService(store, access)
    app.state.dashboards = DashboardManager(
        app, lambda value, tz: CronTrigger.from_crontab(value, timezone=tz)
    )
    register_dashboard_routes(app, authenticated, access)
    register_asset_routes(app, authenticated, access)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            store=store,
            access=access,
            manager=app.state.dashboards,
            runtime=app.state.runtime,
        )
    app.state.dashboards.close()


def query(workspace, name="Orders", **overrides):
    response = workspace.client.post(
        "/api/data/queries",
        json={
            "project_id": "one",
            "name": name,
            "engine": "duckdb",
            "sql": "SELECT ? AS value",
            "params": [1],
            **overrides,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def dashboard(workspace, **overrides):
    saved = query(workspace)
    body = {
        "project_id": "one",
        "name": "Daily orders",
        "parameters": [{"name": "value", "type": "integer", "default": 7}],
        "tiles": [
            {
                "id": "orders",
                "title": "Order total",
                "query_id": saved["id"],
                "chart": "number",
                "parameter_bindings": {"0": "value"},
            }
        ],
        **overrides,
    }
    response = workspace.client.post("/api/dashboards", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def finish(workspace, identifier, *, value=42, status="succeeded"):
    refresh = workspace.store.get("dashboard_refreshes", identifier)
    for _ in range(30):
        workspace.manager.tick()
        refresh = workspace.store.get("dashboard_refreshes", identifier)
        for tile in refresh["tiles"]:
            if tile.get("run_id"):
                run = workspace.store.get("runs", tile["run_id"])
                if run["status"] == "queued":
                    run.update(
                        status=status,
                        finished_at=now(),
                        data_result={"columns": ["value"], "rows": [[value]]},
                    )
                    workspace.store.put("runs", run)
        if refresh["status"] not in ("queued", "running"):
            return refresh
    raise AssertionError("Refresh did not finish")


def test_dashboard_cache_reads_never_dispatch_and_filter_variants_are_exact(workspace):
    client = workspace.client
    board = dashboard(workspace)
    route = "/api/dashboards/" + board["id"]
    assert client.get(route, headers={"x-user": "viewer"}).status_code == 200
    assert (
        client.get(route + "/snapshot", headers={"x-user": "viewer"}).status_code == 404
    )
    assert workspace.runtime.calls == []
    assert (
        client.post(
            route + "/refresh", json={}, headers={"x-user": "viewer"}
        ).status_code
        == 403
    )
    response = client.post(
        route + "/refresh",
        json={"parameters": {"value": 42}},
        headers={"x-user": "operator"},
    )
    assert response.status_code == 202
    assert "_queries" not in response.json() and "SELECT ?" not in response.text
    refresh = finish(workspace, response.json()["id"])
    assert refresh["status"] == "succeeded"
    before = len(workspace.runtime.calls)
    snapshot = client.get(
        route + "/snapshot",
        params={"parameters": json.dumps({"value": 42})},
        headers={"x-user": "viewer"},
    ).json()
    assert snapshot["tiles"][0]["result"]["rows"] == [[42]] and snapshot[
        "parameters"
    ] == {"value": 42}
    assert snapshot["stale"] is False
    assert (
        client.get(route + "/snapshot").status_code == 404
    )  # The default value7 has no cached result.
    assert len(workspace.runtime.calls) == before
    generated = workspace.runtime.calls[0][0]["cells"][0]["source"]
    assert "'params': [42]" in generated
    assert client.get(route, headers={"x-user": "outsider"}).status_code == 403


def test_dashboard_failure_preserves_previous_success_and_exposes_staleness(workspace):
    board = dashboard(workspace)
    route = "/api/dashboards/" + board["id"]
    first = workspace.client.post(route + "/refresh", json={}).json()
    assert finish(workspace, first["id"], value=7)["status"] == "succeeded"
    second = workspace.client.post(route + "/refresh", json={}).json()
    assert finish(workspace, second["id"], status="failed")["status"] == "failed"
    snapshot = workspace.client.get(route + "/snapshot").json()
    assert snapshot["tiles"][0]["result"]["rows"] == [[7]]
    assert snapshot["stale"] and snapshot["latest_refresh"]["status"] == "failed"
    assert "_queries" not in snapshot["latest_refresh"]


def test_queries_are_current_and_permissions_are_rechecked_before_dispatch(workspace):
    board = dashboard(workspace)
    route = "/api/dashboards/" + board["id"]
    refresh = workspace.client.post(route + "/refresh", json={}).json()
    saved = workspace.store.get("data_queries", board["tiles"][0]["query_id"])
    saved.update(sql="SELECT 123", updated_at=now())
    workspace.store.put("data_queries", saved)
    workspace.manager.tick()
    assert workspace.runtime.calls == []
    assert (
        workspace.store.get("dashboard_refreshes", refresh["id"])["status"] == "failed"
    )
    refresh = workspace.client.post(route + "/refresh", json={}).json()
    workspace.store.delete("project_members", "one:writer")
    workspace.manager.tick()
    assert workspace.runtime.calls == []
    assert (
        workspace.store.get("dashboard_refreshes", refresh["id"])["status"] == "failed"
    )


def test_connection_removal_and_expired_tokens_cannot_dispatch(workspace):
    workspace.store.put(
        "connections",
        {"id": "sql", "type": "sqlserver", "project_ids": ["one"], "config": {}},
    )
    saved = query(workspace, engine="sqlserver", connection_id="sql")
    board = dashboard(workspace, tiles=[{"id": "sql", "query_id": saved["id"]}])
    route = "/api/dashboards/" + board["id"]
    refresh = workspace.client.post(route + "/refresh", json={}).json()
    connection = workspace.store.get("connections", "sql")
    connection["project_ids"] = ["two"]
    workspace.store.put("connections", connection)
    workspace.manager.tick()
    assert (
        workspace.store.get("dashboard_refreshes", refresh["id"])["status"] == "failed"
    )
    assert workspace.runtime.calls == []
    # A queued automation refresh retains the originating token scope.
    connection["project_ids"] = ["one"]
    workspace.store.put("connections", connection)
    workspace.store.put(
        "api_tokens",
        {
            "id": "token",
            "service_account_id": "writer",
            "project_ids": ["one"],
            "actions": ["read", "run"],
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        },
    )
    actor = {
        **workspace.store.get("users", "writer"),
        "_api_token": {
            "id": "token",
            "project_ids": ["one"],
            "actions": ["read", "run"],
        },
    }
    refresh = workspace.manager.refresh(board["id"], {}, actor)
    workspace.manager.tick()
    assert (
        workspace.store.get("dashboard_refreshes", refresh["id"])["status"] == "failed"
    )
    assert workspace.runtime.calls == []


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT 1; DROP TABLE orders",
        "WITH x AS (SELECT 1) DELETE FROM orders",
        "SELECT * INTO secret FROM orders",
    ],
)
def test_dashboard_rejects_explicit_writes_and_other_project_queries(workspace, sql):
    saved = query(workspace, sql=sql)
    response = workspace.client.post(
        "/api/dashboards",
        json={
            "project_id": "one",
            "name": "bad",
            "tiles": [{"id": "a", "query_id": saved["id"]}],
        },
    )
    assert response.status_code == 400
    saved["project_id"] = "two"
    workspace.store.put("data_queries", saved)
    assert (
        workspace.client.post(
            "/api/dashboards",
            json={
                "project_id": "one",
                "name": "cross",
                "tiles": [{"id": "a", "query_id": saved["id"]}],
            },
        ).status_code
        == 400
    )


def test_schedule_defaults_revision_and_uncertain_restart_are_safe(workspace):
    board = dashboard(workspace, cron="0 9 * * *", timezone="America/New_York")
    assert (
        workspace.app.state.scheduler.get_job("dashboard-refresh:" + board["id"])
        is not None
    )
    route = "/api/dashboards/" + board["id"]
    refresh = workspace.client.post(route + "/refresh", json={}).json()
    assert (
        workspace.client.put(route, json={**board, "name": "changed"}).status_code
        == 409
    )
    value = workspace.store.get("dashboard_refreshes", refresh["id"])
    value["tiles"][0]["status"] = "dispatching"
    workspace.store.put("dashboard_refreshes", value)
    restarted = DashboardManager(
        workspace.app,
        lambda expression, tz: CronTrigger.from_crontab(expression, timezone=tz),
    )
    try:
        assert (
            workspace.store.get("dashboard_refreshes", refresh["id"])["status"]
            == "interrupted"
        )
        assert workspace.runtime.calls == []
    finally:
        restarted.close()
    changed = workspace.client.put(
        route, json={**board, "name": "new definition"}
    ).json()
    assert changed["generation"] != board["generation"]
    assert workspace.client.get(route + "/snapshot").status_code == 404
    body = {**board, "parameters": [{"name": "value", "type": "integer"}]}
    assert workspace.client.put(route, json=body).status_code == 400


def test_maintenance_admission_and_paged_history(workspace):
    board = dashboard(workspace)
    route = "/api/dashboards/" + board["id"]
    workspace.app.state.maintenance = "Backup"
    assert workspace.client.post(route + "/refresh", json={}).status_code == 503
    workspace.app.state.maintenance = None
    refresh = workspace.client.post(route + "/refresh", json={}).json()

    class Operations:
        def enter_mutation(self):
            return False

    workspace.app.state.operations = Operations()
    with pytest.raises(Exception):
        workspace.manager.tick()
    assert (
        workspace.store.get("dashboard_refreshes", refresh["id"])["status"] == "queued"
    )
    del workspace.app.state.operations
    assert finish(workspace, refresh["id"])["status"] == "succeeded"
    history = workspace.client.get(route + "/refreshes?limit=1&offset=0").json()
    assert history["total"] == 1 and len(history["items"]) == 1
    assert "_queries" not in history["items"][0]
    assert workspace.client.get("/api/dashboards?project_id=two").status_code == 403
    assert (
        workspace.client.get("/api/dashboards?project_id=one&limit=101").status_code
        == 422
    )


def test_result_cache_has_hard_byte_row_and_column_limits():
    result = bounded_result(
        {"columns": ["value"], "rows": [["x" * 10_100] for _ in range(1100)]}, 200_000
    )
    assert (
        len(json.dumps(result).encode()) <= 200_000
        and result["row_count"] < 1000
        and result["truncated"]
    )
    assert len(result["rows"][0][0]) == 10_000


def test_query_picker_is_summary_only_and_workspace_cache_budget_is_bounded(
    workspace, monkeypatch
):
    import openbricks.dashboards as module

    saved = query(workspace)
    options = workspace.client.get(
        "/api/dashboards/query-options?project_id=one"
    ).json()
    assert options["items"][0]["id"] == saved["id"]
    assert not {"sql", "params", "payload"} & options["items"][0].keys()
    assert (
        workspace.client.get("/api/dashboards/query-options?project_id=two").status_code
        == 403
    )
    monkeypatch.setattr(module, "MAX_WORKSPACE_CACHE_BYTES", 1000)
    for index in range(3):
        workspace.store.put(
            "dashboard_snapshots",
            {
                "id": f"cache-{index}",
                "parent_id": "example",
                "updated_at": str(index),
                "data": "x" * 600,
            },
        )
    workspace.manager._trim("example")
    assert workspace.store.count("dashboard_snapshots") == 1
    assert workspace.store.get("dashboard_snapshots", "cache-2")


def evidence(
    workspace,
    run_id,
    reads,
    writes,
    *,
    project="one",
    status="succeeded",
    report=None,
    created=None,
):
    created = created or now()
    workspace.store.put(
        "runs",
        {
            "id": run_id,
            "project_id": project,
            "status": status,
            "created_at": created,
            "finished_at": created,
            "quality_reports": [report] if report else [],
            "outputs": [{"secret_unrelated_output": "must never be selected"}],
        },
    )
    for index, (action, dataset) in enumerate(
        [*(("read", value) for value in reads), *(("write", value) for value in writes)]
    ):
        workspace.store.put(
            "data_lineage",
            {
                "id": run_id + str(index),
                "project_id": project,
                "run_id": run_id,
                "action": action,
                "dataset": dataset,
                "created_at": created,
            },
        )


def test_asset_graph_direction_bounds_and_project_isolation(workspace):
    evidence(workspace, "first", ["bronze.orders"], ["silver.orders"])
    evidence(workspace, "second", ["silver.orders"], ["gold.orders"])
    evidence(workspace, "other", ["private.source"], ["gold.orders"], project="two")
    route = "/api/assets/graph"
    graph = workspace.client.get(
        route,
        params={
            "project_id": "one",
            "dataset": "gold.orders",
            "direction": "upstream",
            "depth": 2,
        },
    ).json()
    assert {value["dataset"] for value in graph["nodes"]} == {
        "bronze.orders",
        "silver.orders",
        "gold.orders",
    }
    assert len(graph["edges"]) == 2 and all(edge["inferred"] for edge in graph["edges"])
    assert "private.source" not in json.dumps(graph)
    bounded = workspace.client.get(
        route, params={"project_id": "one", "dataset": "bronze.orders", "max_nodes": 1}
    ).json()
    assert (
        len(bounded["nodes"]) == 1 and bounded["truncated"] and bounded["edges"] == []
    )
    assert workspace.client.get(route + "?project_id=two").status_code == 403


def test_asset_health_uses_observed_quality_and_successful_writes_only(workspace):
    recent = now()
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    report = {
        "dataset": "gold.orders",
        "passed": False,
        "row_count": 10,
        "checks": [
            {
                "name": "freshness",
                "type": "freshness",
                "passed": False,
                "failed_rows": 1,
                "actual": 7200,
            }
        ],
    }
    evidence(workspace, "successful", [], ["gold.orders"], created=old)
    evidence(
        workspace,
        "failed",
        ["silver.orders"],
        ["gold.orders"],
        status="failed",
        report=report,
        created=recent,
    )
    identifier = asset_id("one", "gold.orders")
    response = workspace.client.put(
        "/api/assets/" + identifier,
        json={
            "project_id": "one",
            "dataset": "gold.orders",
            "description": "Daily reporting table",
            "owner_id": "writer",
            "freshness_seconds": 3600,
        },
    )
    assert response.status_code == 200, response.text
    asset = response.json()
    assert asset["health"] == "failed" and asset["quality"]["live"] is False
    assert asset["last_writer_status"] == "failed"
    assert (
        asset["freshness"]["observed_at"] == old
        and asset["freshness"]["status"] == "stale"
    )
    assert asset["owner"]["username"] == "writer"
    # No whole run document is retrieved by a read of asset health.
    original = workspace.store.get

    def limited(kind, identifier):
        assert kind != "runs"
        return original(kind, identifier)

    workspace.store.get = limited
    assert (
        workspace.client.get(
            "/api/assets/" + identifier + "?project_id=one"
        ).status_code
        == 200
    )


def test_asset_annotations_reject_unauthorized_owners_and_credential_urls(workspace):
    identifier = asset_id("one", "orders")
    route = "/api/assets/" + identifier
    body = {"project_id": "one", "dataset": "orders"}
    assert (
        workspace.client.put(route, json=body, headers={"x-user": "viewer"}).status_code
        == 403
    )
    assert (
        workspace.client.put(route, json={**body, "owner_id": "missing"}).status_code
        == 400
    )
    assert (
        workspace.client.put(route, json={**body, "project_id": "two"}).status_code
        == 403
    )
    assert (
        workspace.client.put(
            route, json={**body, "dataset": "https://user:password@example.org/data"}
        ).status_code
        == 400
    )
    assert workspace.client.put(route, json=body).status_code == 200
    page = workspace.client.get(
        "/api/assets?project_id=one&limit=1&search=orders"
    ).json()
    assert page["total"] == 1 and page["items"][0]["dataset"] == "orders"
    assert page["items"][0]["health"] == "unknown"


def test_malformed_quality_events_do_not_break_asset_browsing(workspace):
    evidence(workspace, "bad", [], ["orders"])
    value = workspace.store.get("runs", "bad")
    value["quality_reports"] = [
        "not a report",
        {
            "dataset": "orders",
            "passed": True,
            "checks": [{"name": "x" * 2000, "actual": {"large": "untrusted"}}],
        },
    ]
    workspace.store.put("runs", value)
    page = workspace.client.get("/api/assets?project_id=one").json()
    check = page["items"][0]["quality"]["checks"][0]
    assert len(check["name"]) == 500 and check["actual"] is None


def test_asset_quality_history_is_project_scoped_and_paginated(workspace):
    dataset = "orders"
    for index in range(3):
        evidence(
            workspace,
            "quality-" + str(index),
            [],
            [dataset],
            report={"dataset": dataset, "passed": bool(index % 2), "checks": []},
        )
    evidence(
        workspace,
        "private-quality",
        [],
        [dataset],
        project="two",
        report={"dataset": dataset, "passed": False, "checks": []},
    )
    identifier = asset_id("one", dataset)
    route = "/api/assets/" + identifier + "/quality?project_id=one&limit=1&offset=1"
    response = workspace.client.get(route)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert "private-quality" not in json.dumps(page)
    assert page["evidence"]["live_queries"] is False
    assert (
        workspace.client.get(route, headers={"x-user": "outsider"}).status_code == 403
    )
