import time

import pytest
from fastapi.testclient import TestClient

from openbricks.main import create_app


def await_value(client, path, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200, response.text
        value = response.json()
        if value["status"] not in ("queued", "running"):
            return value
        time.sleep(0.04)
    pytest.fail("API execution did not finish")


def test_real_workflow_api_typed_inputs_permissions_repair_and_rerun(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "workflow-password-12345")
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "workflow-password-12345"}).status_code == 200
        def create(name, source):
            response = client.post("/api/notebooks", json={"name": name, "project_id": "default", "connection_ids": [],
                "parameter_definitions": [{"name": "count", "type": "integer", "required": True}],
                "cells": [{"id": "one", "language": "python", "source": source}]})
            assert response.status_code == 201, response.text
            return response.json()
        first = create("Write", "blob.write_text('blob://workflow/value.txt', str(params['count']))")
        second = create("Read", "print(int(blob.read_text('blob://workflow/value.txt')) + params['count'])")
        body = {"name": "Typed DAG", "project_id": "default", "parameter_definitions": [{"name": "count", "type": "integer", "required": True}],
            "tasks": [{"key": "write", "notebook_id": first["id"]}, {"key": "read", "notebook_id": second["id"], "depends_on": ["write"]}]}
        created = client.post("/api/workflows", json=body)
        assert created.status_code == 201, created.text
        workflow = created.json()
        assert client.get("/api/workflows/page", params={"search": "Typed", "project_id": "default"}).json()["total"] == 1
        assert client.post(f"/api/workflows/{workflow['id']}/run", json={}).status_code == 400
        start = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {"count": 7}})
        assert start.status_code == 202, start.text
        done = await_value(client, f"/api/workflow-runs/{start.json()['id']}")
        assert done["status"] == "succeeded", done
        assert "_specs" not in done and done["progress"] == {"completed": 2, "total": 2}
        child_id = done["tasks"][1]["run_id"]
        child = client.get(f"/api/runs/{child_id}").json()
        assert child["outputs"][0]["stdout"].strip() == "14"
        second["cells"][0]["source"] = "print('changed workspace')"
        update = client.put(f"/api/notebooks/{second['id']}", json=second)
        assert update.status_code == 200, update.text
        repeated = client.post(f"/api/runs/{child_id}/rerun", json={})
        assert repeated.status_code == 202, repeated.text
        assert await_value(client, f"/api/runs/{repeated.json()['id']}")["outputs"][0]["stdout"].strip() == "14"
        exact = client.post(f"/api/workflow-runs/{done['id']}/rerun", json={})
        assert exact.status_code == 202
        assert await_value(client, f"/api/workflow-runs/{exact.json()['id']}")["status"] == "succeeded"
        # Operators can execute a saved definition but cannot change it.
        member = client.post("/api/users", json={"username": "runner", "password": "runner-password-123", "role": "member"}).json()
        client.app.state.store.put("project_members", {"id": "default:" + member["id"], "project_id": "default", "user_id": member["id"], "role": "operator"})
        client.post("/api/auth/logout")
        assert client.post("/api/auth/login", json={"username": "runner", "password": "runner-password-123"}).status_code == 200
        assert client.put(f"/api/workflows/{workflow['id']}", json=body).status_code == 403
        allowed = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {"count": 2}})
        assert allowed.status_code == 202, allowed.text
        assert await_value(client, f"/api/workflow-runs/{allowed.json()['id']}")["status"] == "succeeded"
