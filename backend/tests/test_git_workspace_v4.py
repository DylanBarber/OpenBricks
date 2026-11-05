import hashlib
import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import httpx
import pytest

from openbricks.credentials import SecretBox
from openbricks import git_workspace as git
from openbricks.main import create_app
from openbricks.notebooks import parse_notebook


def command(base, *args):
    return subprocess.check_output(["git", "-C", str(base), *args], text=True).strip()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "git-workspace-password-123")
    for key in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "git-workspace-password-123"},
        ).raise_for_status()
        base = app.state.store.directory / "repositories" / "fixture"
        base.mkdir(parents=True)
        remote = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            capture_output=True,
        )
        command(base, "init", "--initial-branch=main")
        command(base, "config", "user.name", "Git Fixture")
        command(base, "config", "user.email", "fixture@example.invalid")
        command(base, "config", "core.autocrlf", "false")
        (base / "demo.py").write_text(
            "# Databricks notebook source\nprint('original')\n"
        )
        command(base, "add", "demo.py")
        command(base, "commit", "-m", "Fixture")
        command(base, "remote", "add", "origin", str(remote))
        command(base, "push", "--set-upstream", "origin", "main")
        repository = {
            "id": "fixture",
            "name": "Fixture",
            "url": str(remote),
            "branch": "main",
            "project_id": "default",
            "path": str(base),
            "commit_full": command(base, "rev-parse", "HEAD"),
        }
        app.state.store.put("repositories", repository)
        nb = client.post(
            "/api/notebooks",
            json=parse_notebook("demo.py", (base / "demo.py").read_bytes()),
        ).json()
        nb.update(
            repository_id="fixture",
            path="demo.py",
            imported_commit=repository["commit_full"],
            _git_source_sha256=git.source_fingerprint(nb),
        )
        app.state.store.put("notebooks", nb)
        yield app, client, base, repository, nb


def test_full_branch_workspace_stage_commit_and_real_push(workspace):
    app, client, base, repository, nb = workspace
    prefix = "/api/repositories/fixture/git"
    branch = client.post(prefix + "/branch", json={"name": "feature/notebook"})
    assert branch.status_code == 200, branch.text
    nb["cells"][0]["source"] = "print('updated')\n"
    saved = client.put(f"/api/notebooks/{nb['id']}", json=nb).json()
    status = client.get(prefix + "/status").json()
    assert status["workspace_changes"][0]["modified"]
    stale = client.post(
        prefix + "/stage-notebooks",
        json={"notebook_ids": [nb["id"]], "versions": {nb["id"]: 0}},
    )
    assert stale.status_code == 409
    staged = client.post(
        prefix + "/stage-notebooks",
        json={"notebook_ids": [nb["id"]], "versions": {nb["id"]: saved["version"]}},
    )
    assert staged.status_code == 200, staged.text
    assert (
        "updated"
        in client.get(prefix + "/diff", params={"path": "demo.py"}).json()["text"]
    )
    committed = client.post(
        prefix + "/commit", json={"message": "Update notebook", "paths": ["demo.py"]}
    )
    assert committed.status_code == 200, committed.text
    assert not committed.json()["workspace_changes"][0]["modified"]
    pushed = client.post(prefix + "/push", json={})
    assert pushed.status_code == 200, pushed.text
    assert (
        command(Path(repository["url"]), "rev-parse", "refs/heads/feature/notebook")
        == committed.json()["commit"]
    )


def test_file_compare_and_swap_symlink_and_git_internal_guards(workspace):
    _, client, base, _, _ = workspace
    prefix = "/api/repositories/fixture/git"
    opened = client.get(prefix + "/file", params={"path": "demo.py"}).json()
    (base / "demo.py").write_text("# Databricks notebook source\nprint('concurrent')\n")
    assert (
        client.put(
            prefix + "/file",
            json={
                "path": "demo.py",
                "source": "overwritten",
                "expected_sha256": opened["sha256"],
            },
        ).status_code
        == 409
    )
    assert "concurrent" in (base / "demo.py").read_text()
    for path in ("../secret", ".git/config", ".git./config", "a//b", "a:stream"):
        assert client.get(prefix + "/file", params={"path": path}).status_code == 400
    fresh = client.get(prefix + "/file", params={"path": "demo.py"}).json()
    assert (
        client.put(
            prefix + "/file",
            json={
                "path": "demo.py",
                "source": "print(123)\n",
                "expected_sha256": fresh["sha256"],
            },
        ).status_code
        == 200
    )


def test_import_rejects_workspace_edits_and_requires_matching_explicit_versions(
    workspace,
):
    app, _, base, repository, nb = workspace
    nb["cells"][0]["source"] = "print('workspace only')"
    app.state.store.put("notebooks", nb)
    with pytest.raises(Exception) as error:
        git.check_import_conflicts(app.state.store, repository)
    assert (
        error.value.status_code == 409
        and error.value.detail["conflicts"][0]["id"] == nb["id"]
    )
    with pytest.raises(Exception):
        git.check_import_conflicts(
            app.state.store, repository, overwrite=True, expected_versions={nb["id"]: 0}
        )
    assert git.check_import_conflicts(
        app.state.store,
        repository,
        overwrite=True,
        expected_versions={nb["id"]: nb["version"]},
    ) == {"ok": True}
    assert (
        app.state.store.get("notebooks", nb["id"])["cells"][0]["source"]
        == "print('workspace only')"
    )


def test_github_token_stays_encrypted_and_pull_request_uses_mock_only(
    workspace, monkeypatch
):
    app, client, base, repository, _ = workspace
    command(base, "switch", "-c", "feature/pr")
    repository["url"] = "https://github.com/example/notebooks.git"
    app.state.store.put("repositories", repository)
    token = "github-fixture-token-never-used-externally"
    saved = client.put(
        "/api/repositories/fixture/git/github-credential", json={"token": token}
    )
    assert saved.status_code == 200 and saved.json()["has_github_token"]
    record = app.state.store.get("repositories", "fixture")
    assert (
        token not in json.dumps(record)
        and SecretBox(app.state.store.directory).decrypt(record["_github_token"])
        == token
    )
    assert token not in client.get("/api/repositories").text
    calls = []

    def fake(url, **options):
        calls.append((url, options))
        return httpx.Response(201, json={"number": 7})

    monkeypatch.setattr(git.httpx, "post", fake)
    response = client.post(
        "/api/repositories/fixture/git/pull-request", json={"title": "Fixture PR"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["url"] == "https://github.com/example/notebooks/pull/7"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer " + token
    assert calls[0][1]["json"]["head"] == "feature/pr"


def test_git_project_viewers_cannot_mutate_and_output_is_bounded(workspace):
    app, client, base, _, _ = workspace
    user = client.post(
        "/api/users",
        json={
            "username": "viewer",
            "password": "viewer-git-test-123",
            "role": "member",
        },
    ).json()
    client.put(
        f"/api/projects/default/members/{user['id']}", json={"role": "viewer"}
    ).raise_for_status()
    client.post(
        "/api/auth/login",
        json={"username": "viewer", "password": "viewer-git-test-123"},
    ).raise_for_status()
    assert client.get("/api/repositories/fixture/git/status").status_code == 200
    assert (
        client.post(
            "/api/repositories/fixture/git/branch", json={"name": "forbidden"}
        ).status_code
        == 403
    )
    (base / "demo.py").write_text("x" * 10000)
    value, truncated = git.git_call(base, "diff", "HEAD", "--", "demo.py", limit=100)
    assert len(value) == 100 and truncated


def test_new_workspace_notebook_can_be_linked_exported_and_committed(workspace):
    app, client, base, repository, _ = workspace
    created = client.post(
        "/api/notebooks",
        json={
            "name": "New analysis",
            "cells": [
                {"id": "one", "language": "python", "source": "print('new notebook')"}
            ],
        },
    ).json()
    linked = client.post(
        "/api/repositories/fixture/git/link-notebook",
        json={
            "notebook_id": created["id"],
            "path": "analysis/new.py",
            "version": created["version"],
        },
    )
    assert linked.status_code == 200, linked.text
    exported = client.post(
        "/api/repositories/fixture/git/stage-notebooks",
        json={
            "notebook_ids": [created["id"]],
            "versions": {created["id"]: linked.json()["version"]},
        },
    )
    assert exported.status_code == 200, exported.text
    committed = client.post(
        "/api/repositories/fixture/git/commit",
        json={"message": "Add analysis", "paths": ["analysis/new.py"]},
    )
    assert committed.status_code == 200, committed.text
    assert "new notebook" in command(base, "show", "HEAD:analysis/new.py")


def test_uncommitted_matching_checkout_does_not_reset_notebook_import_baseline(
    workspace,
):
    app, client, base, repository, nb = workspace
    nb["cells"][0]["source"] = "print('workspace edit')"
    saved = client.put(f"/api/notebooks/{nb['id']}", json=nb).json()
    (base / "demo.py").write_text(
        "# Databricks notebook source\nprint('workspace edit')\n"
    )
    (base / "README.md").write_text("An unrelated commit\n")
    response = client.post(
        "/api/repositories/fixture/git/commit",
        json={"message": "Document project", "paths": ["README.md"]},
    )
    assert response.status_code == 200, response.text
    current = app.state.store.get("notebooks", saved["id"])
    assert git.source_fingerprint(current) != git._baseline(base, current)
    (base / "demo.py").write_text(
        "# Databricks notebook source\nprint('incoming branch edit')\n"
    )
    with pytest.raises(Exception) as error:
        git.check_import_conflicts(app.state.store, repository)
    assert error.value.status_code == 409
