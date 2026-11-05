import io
import json
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openbricks.blob import LocalBlobStore
from openbricks.main import create_app, cron_trigger, validate_git_url
from openbricks.security import confined_path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "test-password-long-123")
    monkeypatch.setenv("OPENBRICKS_RUN_TIMEOUT", "5")
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-long-123"})
        assert response.status_code == 200
        yield client


def create_python(client, source):
    response = client.post("/api/notebooks", json={"name": "Test notebook", "cells": [{"id": "one", "language": "python", "source": source}]})
    assert response.status_code == 201, response.text
    return response.json()


def await_run(client, id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{id}")
        value = response.json()
        if value["status"] not in ("queued", "running"):
            return value
        time.sleep(0.05)
    pytest.fail("Run did not finish")


def test_authentication_roles_and_csrf(client):
    user = client.get("/api/auth/me").json()
    assert user["role"] == "admin" and "password_hash" not in user
    assert client.post("/api/notebooks", json={"name": "bad"}, headers={"Origin": "https://evil.example"}).status_code == 403
    created = client.post("/api/users", json={"username": "analyst", "password": "another-password-123", "role": "member"})
    assert created.status_code == 201 and "password_hash" not in created.json()
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/notebooks").status_code == 401
    assert client.post("/api/auth/login", json={"username": "analyst", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "analyst", "password": "another-password-123"}).status_code == 200
    assert client.post("/api/users", json={"username": "intruder", "password": "another-password-123"}).status_code == 403
    assert client.get("/api/notebooks").status_code == 200


def test_notebook_import_export_and_persistence(client):
    document = {"nbformat": 4, "cells": [{"cell_type": "code", "source": ["%%sql\n", "SELECT 42 AS answer"]}, {"cell_type": "markdown", "source": "# Hi"}]}
    response = client.post("/api/notebooks/import", files={"file": ("queries.ipynb", json.dumps(document).encode(), "application/json")})
    assert response.status_code == 201, response.text
    notebook = response.json()
    assert notebook["cells"][0]["language"] == "sql"
    assert notebook["cells"][0]["source"] == "SELECT 42 AS answer"
    exported = client.get(f"/api/notebooks/{notebook['id']}/export")
    assert exported.status_code == 200
    assert exported.json()["cells"][0]["source"][0] == "%%sql\n"
    assert (client.app.state.store.directory / "notebooks" / f"{notebook['id']}.ipynb").exists()
    source = b"# Databricks notebook source\n# MAGIC %sql\n# MAGIC SELECT 1\n# COMMAND ----------\nprint('done')"
    imported = client.post("/api/notebooks/import", files={"file": ("source.py", source)}).json()
    assert [cell["language"] for cell in imported["cells"]] == ["sql", "python"]
    assert "_cells" not in client.get("/api/overview").text


def test_real_python_kernel_state_failure_and_cancel(client):
    notebook = create_python(client, "answer = 41\nprint('ready')")
    first = client.post(f"/api/notebooks/{notebook['id']}/run", json={"cell_id": "one"}).json()
    done = await_run(client, first["id"])
    assert done["status"] == "succeeded", done
    assert done["outputs"][0]["stdout"].strip() == "ready"
    notebook["cells"] = [{"id": "two", "language": "python", "source": "answer + 1"}]
    client.put(f"/api/notebooks/{notebook['id']}", json=notebook)
    second = client.post(f"/api/notebooks/{notebook['id']}/run", json={"cell_id": "two"}).json()
    done = await_run(client, second["id"])
    assert done["status"] == "succeeded" and done["outputs"][0]["stdout"].strip() == "42", done
    # Whole-notebook runs start with a clean Python namespace.
    full = client.post(f"/api/notebooks/{notebook['id']}/run", json={}).json()
    done = await_run(client, full["id"])
    assert done["status"] == "failed" and "NameError" in done["error"]
    slow = create_python(client, "import time\ntime.sleep(20)")
    run = client.post(f"/api/notebooks/{slow['id']}/run", json={}).json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/runs/{run['id']}").json()["status"] == "queued":
        time.sleep(0.05)
    assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 200
    assert await_run(client, run["id"])["status"] == "cancelled"
    resumed = create_python(client, "print('recovered')")
    run = client.post(f"/api/notebooks/{resumed['id']}/run", json={}).json()
    assert await_run(client, run["id"])["status"] == "succeeded"


def test_jobs_persist_validate_and_protect_notebook(client):
    notebook = create_python(client, "print('scheduled')")
    body = {"name": "Daily", "notebook_id": notebook["id"], "cron": "0 9 * * 1-5", "timezone": "America/New_York", "enabled": True}
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["next_run_at"] and job["timezone"] == "America/New_York"
    assert client.delete(f"/api/notebooks/{notebook['id']}").status_code == 409
    body["enabled"] = False
    assert client.put(f"/api/jobs/{job['id']}", json=body).json()["next_run_at"] is None
    assert client.app.state.store.get("jobs", job["id"])["enabled"] is False
    assert client.post("/api/jobs", json={**body, "cron": "not a cron"}).status_code == 400
    assert client.post("/api/jobs", json={**body, "timezone": "invalid/zone"}).status_code == 400
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert client.delete(f"/api/notebooks/{notebook['id']}").status_code == 200


def test_local_objects_and_notebook_helper_share_data(client):
    assert client.post("/api/buckets", json={"name": "raw-data"}).status_code == 201
    uploaded = client.post("/api/buckets/raw-data/objects", files={"file": ("data.csv", b"x,y\n1,2")}, data={"key": "nested/data.csv"})
    assert uploaded.status_code == 201, uploaded.text
    assert client.get("/api/buckets/raw-data/download", params={"key": "nested/data.csv"}).content == b"x,y\n1,2"
    notebook = create_python(client, "print(blob.read_text('blob://raw-data/nested/data.csv'))\nblob.write_text('blob://raw-data/from-notebook.txt', 'hello')")
    run = client.post(f"/api/notebooks/{notebook['id']}/run", json={}).json()
    assert await_run(client, run["id"])["status"] == "succeeded"
    assert client.get("/api/buckets/raw-data/download", params={"key": "from-notebook.txt"}).content == b"hello"
    assert len(client.get("/api/buckets/raw-data/objects").json()) == 2
    assert client.post("/api/buckets/raw-data/objects", files={"file": ("bad", b"bad")}, data={"key": "../escape"}).status_code == 400
    assert client.get("/api/buckets/raw-data/download", params={"key": "../openbricks.sqlite3"}).status_code == 400
    assert client.delete("/api/buckets/raw-data/objects", params={"key": "nested/data.csv"}).status_code == 200


def test_paths_and_git_protocols(tmp_path):
    for value in ("../escape", "/absolute", "C:\\escape", "folder/../../escape"):
        with pytest.raises(ValueError):
            confined_path(tmp_path, value)
    for url in ("https://github.com/example/repo.git", "ssh://git@github.com/example/repo.git", "git@github.com:example/repo.git"):
        validate_git_url(url)
    for url in ("file:///etc/password", "ext::echo bad", "https://token@github.com/repo.git", "--config=bad"):
        with pytest.raises(ValueError):
            validate_git_url(url)


def test_standard_cron_weekdays_are_translated():
    trigger = cron_trigger("0 9 * * 1-5", "UTC")
    assert str(trigger.fields[4]) == "mon,tue,wed,thu,fri"
    assert str(cron_trigger("0 9 * * 0", "UTC").fields[4]) == "sun"


def test_invalid_wheel_rejected(client):
    response = client.post("/api/packages/upload", files={"file": ("bad-0.0.1-py3-none-any.whl", b"not zip")})
    assert response.status_code == 400
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr("bad-0.0.1.dist-info/METADATA", "Name: bad\nVersion: 0.0.1\n")
        wheel.writestr("../escape.py", "bad")
    response = client.post("/api/packages/upload", files={"file": ("bad-0.0.1-py3-none-any.whl", archive.getvalue())})
    assert response.status_code == 400 and "traversal" in response.text


def test_actual_wheel_build_install_and_import(client):
    store = client.app.state.store
    project = store.directory / "repositories" / "test-repo"
    project.mkdir()
    (project / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools>=61", "wheel"]\nbuild-backend = "setuptools.build_meta"\n[project]\nname = "openbricks-test-helper"\nversion = "0.1.0"\n[tool.setuptools]\npy-modules = ["openbricks_test_helper"]\n', encoding="utf-8")
    (project / "openbricks_test_helper.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    store.put("repositories", {"id": "test-repo", "name": "local build fixture", "path": str(project)})
    built = client.post("/api/packages/build", json={"repository_id": "test-repo", "path": "."})
    assert built.status_code == 201, built.text
    assert built.json()[0]["name"] == "openbricks-test-helper"
    notebook = create_python(client, "from openbricks_test_helper import add\nprint(add(20, 22))")
    run = client.post(f"/api/notebooks/{notebook['id']}/run", json={}).json()
    result = await_run(client, run["id"])
    assert result["status"] == "succeeded" and result["outputs"][0]["stdout"].strip() == "42", result
    with client.app.state.runtime.environment_change():
        assert client.post(f"/api/notebooks/{notebook['id']}/run", json={}).status_code == 409


def test_scheduler_executes_and_reloads_after_restart(client):
    notebook = create_python(client, "print('scheduled for real')")
    created = client.post("/api/jobs", json={"name": "Durable job", "notebook_id": notebook["id"], "cron": "0 0 1 1 *", "timezone": "UTC", "enabled": True})
    assert created.status_code == 201, created.text
    job = created.json()
    client.app.state.scheduler.modify_job(job["id"], next_run_time=datetime.now(timezone.utc) + timedelta(milliseconds=100))
    deadline = time.monotonic() + 10
    runs = []
    while time.monotonic() < deadline:
        runs = [run for run in client.get("/api/runs").json() if run.get("job_id") == job["id"]]
        if runs:
            break
        time.sleep(0.05)
    assert runs, "Scheduler did not enqueue the notebook"
    result = await_run(client, runs[0]["id"])
    assert result["status"] == "succeeded" and result["trigger"] == "schedule", result
    assert client.get("/api/jobs").json()[0]["last_run_at"]
    directory = client.app.state.store.directory
    # A second application instance reloads persisted definitions; tests do not
    # run overlapping API services in deployment.
    with TestClient(create_app(directory)) as reopened:
        assert reopened.post("/api/auth/login", json={"username": "admin", "password": "test-password-long-123"}).status_code == 200
        restored = next(item for item in reopened.get("/api/jobs").json() if item["id"] == job["id"])
        assert restored["enabled"] and restored["next_run_at"] and restored["last_run_at"]


def test_cancelled_queued_work_does_not_execute_and_timeout_recovers(client):
    slow = create_python(client, "import time\ntime.sleep(20)")
    first = client.post(f"/api/notebooks/{slow['id']}/run", json={}).json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/runs/{first['id']}").json()["status"] == "queued":
        time.sleep(0.01)
    cancelled = create_python(client, "blob.write_text('blob://workspace/should-not-exist.txt', 'executed')")
    queued = client.post(f"/api/notebooks/{cancelled['id']}/run", json={}).json()
    assert client.post(f"/api/runs/{queued['id']}/cancel").json()["status"] == "cancelled"
    recovery = create_python(client, "print('queue survived')")
    following = client.post(f"/api/notebooks/{recovery['id']}/run", json={}).json()
    assert client.post(f"/api/runs/{first['id']}/cancel").status_code == 200
    assert await_run(client, following["id"])["status"] == "succeeded"
    assert client.get("/api/buckets/workspace/download", params={"key": "should-not-exist.txt"}).status_code == 404
    assert await_run(client, queued["id"])["status"] == "cancelled"
    client.app.state.runtime.timeout = 1
    timed = client.post(f"/api/notebooks/{slow['id']}/run", json={}).json()
    following = client.post(f"/api/notebooks/{recovery['id']}/run", json={}).json()
    result = await_run(client, timed["id"])
    assert result["status"] == "failed" and "exceeded 1 seconds" in result["error"], result
    assert await_run(client, following["id"])["status"] == "succeeded"


def test_scheduled_run_waits_for_package_changes(client):
    notebook = create_python(client, "print('scheduled after install')")
    runtime = client.app.state.runtime
    with runtime.environment_change():
        cancelled = runtime.submit(notebook, trigger="schedule", job_id="cancelled-package-overlap")
        assert client.post(f"/api/runs/{cancelled['id']}/cancel").json()["status"] == "cancelled"
        scheduled = runtime.submit(notebook, trigger="schedule", job_id="package-overlap")
        assert client.post(f"/api/notebooks/{notebook['id']}/run", json={}).status_code == 409
        time.sleep(0.1)
        assert client.get(f"/api/runs/{scheduled['id']}").json()["status"] == "queued"
    result = await_run(client, scheduled["id"])
    assert result["status"] == "succeeded" and result["trigger"] == "schedule"
    assert client.get(f"/api/runs/{cancelled['id']}").json()["status"] == "cancelled"
