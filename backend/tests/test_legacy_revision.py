from fastapi.testclient import TestClient

from openbricks.main import create_app


def test_legacy_source_restore_clears_later_required_parameters(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "legacy-revision-test-password")
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    app = create_app(tmp_path / "workspace")
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "legacy-revision-test-password"}).raise_for_status()
        created = client.post("/api/notebooks", json={"name": "New source", "description": "Later description",
            "parameter_definitions": [{"name": "required_new_input", "type": "string", "required": True}],
            "cells": [{"id": "one", "language": "python", "source": "print(params['required_new_input'])"}]}).json()
        app.state.store.put("notebook_revisions", {"id": "legacy", "notebook_id": created["id"],
            "project_id": "default", "version": 1, "created_at": created["created_at"],
            "notebook": {"name": "Legacy source", "cells": [{"id": "one", "language": "python", "source": "print('legacy')"}]}})
        response = client.post(f"/api/notebooks/{created['id']}/revisions/legacy/restore")
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["name"] == "Legacy source" and value["cells"][0]["source"] == "print('legacy')"
        assert value["parameter_definitions"] == [] and value["description"] == ""
        assert value["version"] == created["version"] + 1
        submitted = client.post(f"/api/notebooks/{created['id']}/run", json={})
        assert submitted.status_code == 202, submitted.text
