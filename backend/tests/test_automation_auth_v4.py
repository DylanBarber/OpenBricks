import json
import time

from fastapi.testclient import TestClient
import pytest

from openbricks.main import create_app


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "automation-test-password-123")
    for name in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "automation-test-password-123"},
        ).raise_for_status()
        yield app, client


def create_account(client, projects):
    response = client.post(
        "/api/service-accounts",
        json={
            "name": "pipeline-bot",
            "grants": [
                {"project_id": identifier, "role": role}
                for identifier, role in projects
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def issue(client, account, projects, actions):
    response = client.post(
        f"/api/service-accounts/{account['id']}/tokens",
        json={"name": "Test token", "project_ids": projects, "actions": actions},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_scoped_service_token_has_no_admin_or_other_project_access(workspace):
    app, client = workspace
    private = client.post("/api/projects", json={"name": "Private"}).json()
    account = create_account(client, [("default", "editor"), (private["id"], "editor")])
    created = client.post(
        "/api/notebooks",
        json={
            "name": "Run fixture",
            "cells": [{"id": "one", "language": "python", "source": "print(73)"}],
        },
    ).json()
    token = issue(client, account, ["default"], ["run"])
    header = {"Authorization": "Bearer " + token["token"]}
    assert [
        item["id"] for item in client.get("/api/projects", headers=header).json()
    ] == ["default"]
    assert client.get("/api/users", headers=header).status_code == 403
    assert (
        client.get(
            f"/api/notebooks?project_id={private['id']}", headers=header
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/notebooks", headers=header, json={"name": "Forbidden"}
        ).status_code
        == 403
    )
    submitted = client.post(
        f"/api/notebooks/{created['id']}/run", headers=header, json={}
    )
    assert submitted.status_code == 202, submitted.text
    for _ in range(200):
        done = client.get(f"/api/runs/{submitted.json()['id']}", headers=header).json()
        if done["status"] not in ("queued", "running"):
            break
        time.sleep(0.03)
    assert done["status"] == "succeeded", done.get("error")
    with app.state.store.connect() as db:
        bodies = " ".join(row[0] for row in db.execute("SELECT body FROM documents"))
    assert token["token"] not in bodies
    listed = client.get(f"/api/service-accounts/{account['id']}/tokens").json()
    assert "token" not in listed[0] and "password_hash" not in json.dumps(listed)
    assert "_api_token" not in client.get("/api/auth/me", headers=header).json()


def test_expiry_revocation_deleted_identity_and_bad_header_fail_closed(workspace):
    app, client = workspace
    account = create_account(client, [("default", "editor")])
    for value in ("Bearer bogus", "Basic anything", "Bearer ob_" + "x" * 64):
        assert (
            client.get("/api/projects", headers={"Authorization": value}).status_code
            == 401
        )
    expired = issue(client, account, ["default"], ["read"])
    record = app.state.store.get("api_tokens", expired["id"])
    record["expires_at"] = "2000-01-01T00:00:00+00:00"
    app.state.store.put("api_tokens", record)
    assert (
        client.get(
            "/api/projects", headers={"Authorization": "Bearer " + expired["token"]}
        ).status_code
        == 401
    )
    revoked = issue(client, account, ["default"], ["edit"])
    assert (
        client.delete(
            f"/api/service-accounts/{account['id']}/tokens/{revoked['id']}"
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/projects", headers={"Authorization": "Bearer " + revoked["token"]}
        ).status_code
        == 401
    )
    deleted = issue(client, account, ["default"], ["read"])
    assert client.delete(f"/api/service-accounts/{account['id']}").status_code == 200
    assert (
        client.get(
            "/api/projects", headers={"Authorization": "Bearer " + deleted["token"]}
        ).status_code
        == 401
    )


def test_membership_changes_apply_to_existing_tokens_and_issuance_cannot_expand(
    workspace,
):
    app, client = workspace
    account = create_account(client, [("default", "viewer")])
    escalation = client.post(
        f"/api/service-accounts/{account['id']}/tokens",
        json={"name": "No escalation", "project_ids": ["default"], "actions": ["edit"]},
    )
    assert escalation.status_code == 403
    token = issue(client, account, ["default"], ["read"])
    header = {"Authorization": "Bearer " + token["token"]}
    assert client.get("/api/projects", headers=header).status_code == 200
    app.state.store.delete("project_members", f"default:{account['id']}")
    assert client.get("/api/projects", headers=header).json() == []
    assert (
        client.get("/api/notebooks?project_id=default", headers=header).status_code
        == 403
    )
    account_record = app.state.store.get("users", account["id"])
    assert account_record["password_hash"] and account_record["role"] == "member"
    assert (
        client.post(
            "/api/auth/login",
            json={
                "username": account["username"],
                "password": "automation-test-password-123",
            },
        ).status_code
        == 401
    )


def test_usage_touch_cannot_resurrect_a_concurrently_revoked_token(
    workspace, monkeypatch
):
    app, client = workspace
    account = create_account(client, [("default", "viewer")])
    token = issue(client, account, ["default"], ["read"])
    original = app.state.store.get
    raced = False

    def get(kind, identifier):
        nonlocal raced
        value = original(kind, identifier)
        if kind == "api_tokens" and identifier == token["id"] and not raced:
            raced = True
            with app.state.store.connect() as db:
                db.execute(
                    "UPDATE documents SET body=json_set(body,'$.revoked_at','2026-01-01T00:00:00+00:00') WHERE kind=? AND id=?",
                    (kind, identifier),
                )
        return value

    monkeypatch.setattr(app.state.store, "get", get)
    client.get("/api/projects", headers={"Authorization": "Bearer " + token["token"]})
    assert original("api_tokens", token["id"])["revoked_at"]
    assert (
        client.get(
            "/api/projects", headers={"Authorization": "Bearer " + token["token"]}
        ).status_code
        == 401
    )
