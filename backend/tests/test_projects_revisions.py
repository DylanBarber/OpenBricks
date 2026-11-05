"""Project transfers must preserve source and scope historical metadata."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from openbricks.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "project-review-fixture-password")
    for key in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
        "OPENBRICKS_MANAGED_CONTAINER",
    ):
        monkeypatch.delenv(key, raising=False)
    with TestClient(create_app(tmp_path)) as value:
        value.post(
            "/api/auth/login",
            json={"username": "admin", "password": "project-review-fixture-password"},
        ).raise_for_status()
        yield value


def project(client, name):
    response = client.post("/api/projects", json={"name": name})
    response.raise_for_status()
    return response.json()["id"]


def test_service_account_cannot_become_project_owner_through_membership_api(client):
    response = client.post(
        "/api/service-accounts",
        json={
            "name": "deployment-bot",
            "grants": [{"project_id": "default", "role": "editor"}],
        },
    )
    response.raise_for_status()
    identifier = response.json()["id"]
    rejected = client.put(
        f"/api/projects/default/members/{identifier}", json={"role": "owner"}
    )
    assert rejected.status_code == 400
    members = client.get("/api/projects/default/members").json()
    assert (
        next(item for item in members if item["user_id"] == identifier)["role"]
        == "editor"
    )


def notebook(client, project_id="default", name="Notebook"):
    response = client.post(
        "/api/notebooks",
        json={
            "name": name,
            "project_id": project_id,
            "cells": [{"language": "python", "source": "print('original')"}],
        },
    )
    response.raise_for_status()
    return response.json()


def member_a(client, project_id):
    account = client.post(
        "/api/users",
        json={
            "username": "project-a-member",
            "password": "project-member-fixture-password",
            "role": "member",
        },
    ).json()
    client.put(
        f"/api/projects/{project_id}/members/{account['id']}", json={"role": "editor"}
    ).raise_for_status()
    client.post(
        "/api/auth/login",
        json={
            "username": "project-a-member",
            "password": "project-member-fixture-password",
        },
    ).raise_for_status()


def test_moved_notebook_history_does_not_reveal_private_project_metadata(client):
    a, b = project(client, "Visible A"), project(client, "Private B")
    original = notebook(client, a, "Visible old A name")
    store = client.app.state.store
    store.put(
        "runs",
        {
            "id": "old-a-run",
            "notebook_id": original["id"],
            "notebook_name": "Visible old A name",
            "project_id": a,
            "status": "succeeded",
            "created_at": "2026-01-01",
            "outputs": [],
        },
    )
    folder = client.post(
        "/api/folders", json={"name": "B-only-folder", "project_id": b}
    ).json()
    moved = client.patch(
        f"/api/notebooks/{original['id']}/location",
        json={"project_id": b, "folder_id": folder["id"]},
    )
    moved.raise_for_status()
    client.put(
        f"/api/notebooks/{original['id']}",
        json={"name": "Private B name", "cells": moved.json()["cells"]},
    ).raise_for_status()
    store.put(
        "runs",
        {
            "id": "new-b-run",
            "notebook_id": original["id"],
            "notebook_name": "Private B name",
            "project_id": b,
            "status": "failed",
            "created_at": "2026-02-01",
            "outputs": [],
        },
    )
    member_a(client, a)
    assert client.get(f"/api/notebooks/{original['id']}").status_code == 403
    grouped = client.get(f"/api/history/notebooks?project_id={a}")
    grouped.raise_for_status()
    row = grouped.json()["items"][0]
    assert row["notebook_name"] == "Visible old A name"
    assert row["last_status"] == "succeeded" and row["total_runs"] == 1
    assert "B-only" not in grouped.text and "Private B" not in grouped.text
    runs = client.get(f"/api/history/runs?project_id={a}&notebook_id={original['id']}")
    runs.raise_for_status()
    assert [run["id"] for run in runs.json()["items"]] == ["old-a-run"]


def test_git_reimport_creates_a_copy_instead_of_overwriting_moved_private_notebook(
    client,
):
    a, b = project(client, "A"), project(client, "B")
    store = client.app.state.store
    base = store.directory / "repositories" / "fixture-repository"
    (base / "notebooks").mkdir(parents=True)
    (base / "notebooks/demo.py").write_text(
        "# Databricks notebook source\nprint('git source')\n"
    )
    store.put(
        "repositories",
        {
            "id": "fixture-repository",
            "name": "A repository",
            "project_id": a,
            "path": str(base),
        },
    )
    imported = client.post("/api/repositories/fixture-repository/import")
    imported.raise_for_status()
    original = imported.json()[0]
    folder = client.post(
        "/api/folders", json={"name": "B-only", "project_id": b}
    ).json()
    client.patch(
        f"/api/notebooks/{original['id']}/location",
        json={"project_id": b, "folder_id": folder["id"]},
    ).raise_for_status()
    private = store.get("notebooks", original["id"])
    private.update(
        name="Private B source",
        cells=[
            {"id": "private-cell", "language": "python", "source": "print('private B')"}
        ],
        connection_ids=["B-connection"],
        environment_id="B-environment",
        parameter_definitions=[
            {"name": "B-param", "type": "string", "default": "B-value"}
        ],
    )
    store.put("notebooks", private)
    member_a(client, a)
    copied = client.post("/api/repositories/fixture-repository/import")
    copied.raise_for_status()
    value = copied.json()[0]
    assert (
        value["id"] != original["id"]
        and value["project_id"] == a
        and value["folder_id"] != folder["id"]
    )
    assert (
        value["connection_ids"] == []
        and value.get("environment_id") is None
        and value["parameter_definitions"] == []
    )
    untouched = store.get("notebooks", original["id"])
    assert untouched == private
    # Once the source project has its own copy, re-import keeps that copy's ID.
    assert (
        client.post("/api/repositories/fixture-repository/import").json()[0]["id"]
        == value["id"]
    )


def test_concurrent_move_and_save_preserve_latest_source_and_increment_versions(
    client, monkeypatch
):
    import openbricks.main as module

    value = notebook(client)
    folder = client.post("/api/folders", json={"name": "Destination"}).json()
    entered, release = threading.Event(), threading.Event()
    original = module.validate_cluster_reference

    def pause_move(store, cluster_id):
        if cluster_id == "local" and not release.is_set():
            entered.set()
            assert release.wait(5)
        return original(store, cluster_id)

    monkeypatch.setattr(module, "validate_cluster_reference", pause_move)
    source = "print('new saved source')"
    with ThreadPoolExecutor(max_workers=2) as pool:
        move = pool.submit(
            client.patch,
            f"/api/notebooks/{value['id']}/location",
            json={"folder_id": folder["id"], "cluster_id": "local"},
        )
        assert entered.wait(5)
        save = pool.submit(
            client.put,
            f"/api/notebooks/{value['id']}",
            json={
                "name": value["name"],
                "cells": [{"id": "new", "language": "python", "source": source}],
            },
        )
        # With the former unlocked move, the save completed here and was later
        # overwritten by the move's stale full-document write.
        try:
            threading.Event().wait(0.25)
        finally:
            release.set()
        move.result(timeout=8).raise_for_status()
        save.result(timeout=8).raise_for_status()
    final = client.get(f"/api/notebooks/{value['id']}").json()
    assert final["cells"][0]["source"] == source
    assert (
        final["folder_id"] == folder["id"] and final["version"] == value["version"] + 2
    )


def test_notebook_delete_cascades_revisions_so_empty_project_can_delete(client):
    empty_project = project(client, "Temporary project")
    value = notebook(client, empty_project)
    assert client.get(f"/api/notebooks/{value['id']}/revisions").json()["total"] == 1
    client.delete(f"/api/notebooks/{value['id']}").raise_for_status()
    with client.app.state.store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM documents WHERE kind='notebook_revisions' AND notebook_id=?",
                (value["id"],),
            ).fetchone()[0]
            == 0
        )
    client.delete(f"/api/projects/{empty_project}").raise_for_status()
    historical = notebook(client)
    client.app.state.store.put(
        "runs",
        {
            "id": "retained-history",
            "notebook_id": historical["id"],
            "notebook_name": historical["name"],
            "project_id": "default",
            "status": "succeeded",
            "created_at": "2026-01-01",
            "outputs": [],
        },
    )
    client.delete(f"/api/notebooks/{historical['id']}").raise_for_status()
    assert client.app.state.store.get("runs", "retained-history")
