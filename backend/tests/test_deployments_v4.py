import copy
import json
import time

from fastapi.testclient import TestClient
import pytest

from openbricks.bundle import plan_bundle
from openbricks.deployments import DeployInput, apply_plan
from openbricks.main import create_app, save_notebook_file


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "deployment-test-password-123")
    for key in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        user = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "deployment-test-password-123"},
        ).json()
        prod = client.post("/api/projects", json={"name": "Production"}).json()
        bundle = {
            "format_version": 1,
            "name": "sales-pipeline",
            "targets": {
                "dev": {"project_id": "default"},
                "prod": {"project_id": prod["id"]},
            },
            "notebooks": [
                {
                    "key": "bronze",
                    "path": "notebooks/bronze.py",
                    "source": "# Databricks notebook source\nprint('captured bronze')\n",
                },
                {
                    "key": "silver",
                    "path": "notebooks/silver.py",
                    "source": "# Databricks notebook source\nprint('captured silver')\n",
                },
            ],
            "workflows": [
                {
                    "key": "daily",
                    "tasks": [
                        {"key": "bronze", "notebook": "bronze"},
                        {
                            "key": "silver",
                            "notebook": "silver",
                            "depends_on": ["bronze"],
                        },
                    ],
                }
            ],
            "schedules": [
                {
                    "key": "daily-job",
                    "workflow": "daily",
                    "cron": "0 8 * * *",
                    "timezone": "UTC",
                    "enabled": False,
                }
            ],
        }
        yield app, client, user, prod, bundle


def plan(client, bundle, target="dev"):
    response = client.post(
        "/api/deployments/plan", json={"bundle": bundle, "target": target}
    )
    assert response.status_code == 200, response.text
    return response.json()


def deploy(client, bundle, target="dev"):
    preview = plan(client, bundle, target)
    response = client.post(
        "/api/deployments",
        json={
            "bundle": bundle,
            "target": target,
            "expected_plan_hash": preview["plan_hash"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_bundle_real_api_stable_ids_exact_environment_and_captured_promotion(workspace):
    app, client, user, prod, bundle = workspace
    preview = plan(client, bundle)
    assert preview["counts"] == {"notebooks": 2, "workflows": 1, "jobs": 1}
    assert "captured bronze" not in json.dumps(preview)
    deployed = deploy(client, bundle)
    resources = {
        (item["kind"], item["key"]): item["id"] for item in deployed["resources"]
    }
    nb = app.state.store.get("notebooks", resources[("notebooks", "bronze")])
    assert (
        nb["environment_id"] == deployed["environment_id"]
        and nb["connection_ids"] == []
    )
    assert (app.state.store.directory / "notebooks" / f"{nb['id']}.ipynb").is_file()
    again = plan(client, bundle)
    assert all(item["action"] == "unchanged" for item in again["changes"])
    second = deploy(client, bundle)
    assert second["resources"] == deployed["resources"]
    nb["cells"][0]["source"] = "print('workspace changed later')"
    client.put(f"/api/notebooks/{nb['id']}", json=nb).raise_for_status()
    promotion = client.post(
        f"/api/deployments/{deployed['id']}/promotion-plan", json={"target": "prod"}
    )
    assert promotion.status_code == 200, promotion.text
    response = client.post(
        f"/api/deployments/{deployed['id']}/promote",
        json={"target": "prod", "expected_plan_hash": promotion.json()["plan_hash"]},
    )
    assert response.status_code == 201, response.text
    promoted = response.json()
    assert promoted["environment_id"] == deployed["environment_id"]
    promoted_nb_id = next(
        item["id"]
        for item in promoted["resources"]
        if item["kind"] == "notebooks" and item["key"] == "bronze"
    )
    assert (
        "captured bronze"
        in app.state.store.get("notebooks", promoted_nb_id)["cells"][0]["source"]
    )
    assert promoted_nb_id != nb["id"]
    workflow_id = resources[("workflows", "daily")]
    run = client.post(f"/api/workflows/{workflow_id}/run", json={"parameters": {}})
    assert run.status_code == 202, run.text
    for _ in range(400):
        value = client.get(f"/api/workflow-runs/{run.json()['id']}").json()
        if value["status"] not in ("queued", "running"):
            break
        time.sleep(0.03)
    assert value["status"] == "succeeded", value


def test_stale_plan_cannot_overwrite_concurrent_edits_or_moved_resources(workspace):
    app, client, _, prod, bundle = workspace
    deployed = deploy(client, bundle)
    preview = plan(client, bundle)
    identifier = next(
        item["id"] for item in deployed["resources"] if item["kind"] == "notebooks"
    )
    notebook = app.state.store.get("notebooks", identifier)
    notebook["name"] = "Concurrent edit"
    client.put(f"/api/notebooks/{identifier}", json=notebook).raise_for_status()
    response = client.post(
        "/api/deployments",
        json={
            "bundle": bundle,
            "target": "dev",
            "expected_plan_hash": preview["plan_hash"],
        },
    )
    assert response.status_code == 409
    assert app.state.store.get("notebooks", identifier)["name"] == "Concurrent edit"
    moved = client.patch(
        f"/api/notebooks/{identifier}/location",
        json={"project_id": prod["id"], "folder_id": None},
    )
    assert moved.status_code == 200, moved.text
    assert (
        client.post(
            "/api/deployments/plan", json={"bundle": bundle, "target": "dev"}
        ).status_code
        == 409
    )
    assert app.state.store.get("notebooks", identifier)["project_id"] == prod["id"]


def test_invalid_graph_credential_bindings_pins_and_target_permissions_rejected(
    workspace,
):
    _, client, _, _, bundle = workspace
    bad = copy.deepcopy(bundle)
    bad["workflows"][0]["tasks"][0]["depends_on"] = ["silver"]
    response = client.post(
        "/api/deployments/plan", json={"bundle": bad, "target": "dev"}
    )
    assert response.status_code == 400 and "cycle" in response.text
    bad = copy.deepcopy(bundle)
    bad["targets"]["dev"]["password"] = "never-store-credentials"
    response = client.post(
        "/api/deployments/plan", json={"bundle": bad, "target": "dev"}
    )
    assert (
        response.status_code == 400 and "never-store-credentials" not in response.text
    )
    bad = copy.deepcopy(bundle)
    bad["environment"] = {
        "wheels": [{"filename": "missing-1.0-py3-none-any.whl", "sha256": "a" * 64}]
    }
    assert (
        client.post(
            "/api/deployments/plan", json={"bundle": bad, "target": "dev"}
        ).status_code
        == 400
    )
    bad = copy.deepcopy(bundle)
    bad["notebooks"][0]["path"] = "../escape.py"
    assert (
        client.post(
            "/api/deployments/plan", json={"bundle": bad, "target": "dev"}
        ).status_code
        == 400
    )
    viewer = client.post(
        "/api/users",
        json={
            "username": "deploy-viewer",
            "password": "deployment-viewer-123",
            "role": "member",
        },
    ).json()
    client.put(
        f"/api/projects/default/members/{viewer['id']}", json={"role": "viewer"}
    ).raise_for_status()
    client.post(
        "/api/auth/login",
        json={"username": "deploy-viewer", "password": "deployment-viewer-123"},
    ).raise_for_status()
    assert (
        client.post(
            "/api/deployments/plan", json={"bundle": bundle, "target": "dev"}
        ).status_code
        == 403
    )


def test_failed_file_write_rolls_back_database_and_files(workspace):
    app, client, user, _, bundle = workspace
    deployed = deploy(client, bundle)
    before = {
        item["id"]: app.state.store.get(item["kind"], item["id"])
        for item in deployed["resources"]
    }
    changed = copy.deepcopy(bundle)
    changed["notebooks"][0][
        "source"
    ] = "# Databricks notebook source\nprint('new source')\n"
    preview = plan_bundle(app.state.store, changed, "dev", user, app.state.access)
    count = 0

    def failing(store, notebook):
        nonlocal count
        count += 1
        save_notebook_file(store, notebook)
        if count == 2:
            raise OSError("Fixture disk failure")

    with pytest.raises(OSError):
        apply_plan(
            app,
            DeployInput(
                bundle=changed, target="dev", expected_plan_hash=preview["plan_hash"]
            ),
            user,
            app.state.access,
            lambda value: None,
            failing,
        )
    for item in deployed["resources"]:
        assert app.state.store.get(item["kind"], item["id"]) == before[item["id"]]
        if item["kind"] == "notebooks":
            saved = json.loads(
                (
                    app.state.store.directory / "notebooks" / f"{item['id']}.ipynb"
                ).read_text()
            )
            assert "new source" not in json.dumps(saved)


def test_failed_schedule_registration_restores_durable_cursor_and_history(workspace):
    app, client, user, _, bundle = workspace
    deployed = deploy(client, bundle)
    store = app.state.store
    job_id = next(
        item["id"] for item in deployed["resources"] if item["kind"] == "jobs"
    )
    before = {
        item["id"]: store.get(item["kind"], item["id"])
        for item in deployed["resources"]
    }
    cursor = store.get("schedule_cursors", job_id)
    histories = {
        kind: store.list(kind)
        for kind in ("deployments", "notebook_revisions", "audit_events")
    }
    changed = copy.deepcopy(bundle)
    changed["schedules"][0]["cron"] = "0 9 * * *"
    changed["schedules"].append(
        {"key": "new-job", "notebook": "bronze", "enabled": False}
    )
    changed["notebooks"][0]["source"] = "print('failed delivery')\n"
    preview = plan_bundle(store, changed, "dev", user, app.state.access)
    registrations = []

    def failing(job):
        app.state.automation.track_job(job)
        registrations.append(job["id"])
        if len(registrations) == 2:
            raise OSError("Fixture registration failure")

    with pytest.raises(OSError):
        apply_plan(
            app,
            DeployInput(
                bundle=changed, target="dev", expected_plan_hash=preview["plan_hash"]
            ),
            user,
            app.state.access,
            failing,
            save_notebook_file,
        )
    assert store.get("schedule_cursors", job_id) == cursor
    assert store.get("schedule_cursors", registrations[1]) is None
    assert store.get("jobs", registrations[1]) is None
    for item in deployed["resources"]:
        assert store.get(item["kind"], item["id"]) == before[item["id"]]
    assert {kind: store.list(kind) for kind in histories} == histories


def test_bundle_job_resources_validate_and_cannot_choose_budget_owner(workspace):
    _, client, _, _, bundle = workspace
    for resources in (
        {"owner_id": "another-account"},
        {"memory_mb": True},
        {"priority": 21},
        {"mode": "interactive"},
    ):
        bad = copy.deepcopy(bundle)
        bad["workflows"][0]["tasks"][0]["resource_options"] = resources
        assert (
            client.post(
                "/api/deployments/plan", json={"bundle": bad, "target": "dev"}
            ).status_code
            == 400
        )
        bad = copy.deepcopy(bundle)
        bad["schedules"][0]["resource_options"] = resources
        assert (
            client.post(
                "/api/deployments/plan", json={"bundle": bad, "target": "dev"}
            ).status_code
            == 400
        )
    bundle["workflows"][0]["tasks"][0]["resource_options"] = {
        "memory_mb": 384,
        "priority": 2,
    }
    deployed = deploy(client, bundle)
    assert deployed["counts"]["workflows"] == 1


def test_bundle_schedule_date_parameter_requires_a_declared_compatible_input(workspace):
    _, client, _, _, bundle = workspace
    bundle["schedules"][0]["schedule_date_parameter"] = "run_date"
    assert (
        client.post(
            "/api/deployments/plan", json={"bundle": bundle, "target": "dev"}
        ).status_code
        == 400
    )
    bundle["workflows"][0]["parameter_definitions"] = [
        {"name": "run_date", "type": "number"}
    ]
    assert (
        client.post(
            "/api/deployments/plan", json={"bundle": bundle, "target": "dev"}
        ).status_code
        == 400
    )
    bundle["workflows"][0]["parameter_definitions"] = [
        {"name": "run_date", "type": "date"}
    ]
    assert plan(client, bundle)["counts"]["jobs"] == 1


def test_metadata_listing_never_materializes_bundle_sources(workspace, monkeypatch):
    app, client, _, _, bundle = workspace
    deployed = deploy(client, bundle)
    original = app.state.store.list

    def guarded(kind, *args, **kwargs):
        assert kind != "deployments"
        return original(kind, *args, **kwargs)

    monkeypatch.setattr(app.state.store, "list", guarded)
    response = client.get("/api/deployments?project_id=default")
    assert (
        response.status_code == 200
        and response.json()["items"][0]["id"] == deployed["id"]
    )
    assert "captured bronze" not in response.text and "_bundle" not in response.text
