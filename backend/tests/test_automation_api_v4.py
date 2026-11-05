import time

from fastapi.testclient import TestClient

from openbricks.main import create_app


def test_real_authenticated_backfill_keeps_captured_source_and_resolved_dates(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "automation-v4-test-password")
    for key in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
        "OPENBRICKS_SECRET_KEY",
        "OPENBRICKS_MANAGED_CONTAINER",
    ):
        monkeypatch.delenv(key, raising=False)
    app = create_app(tmp_path / "workspace")
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "automation-v4-test-password"},
        ).raise_for_status()
        response = client.post(
            "/api/notebooks",
            json={
                "name": "Backfill date proof",
                "parameter_definitions": [
                    {"name": "day", "type": "date", "required": True}
                ],
                "cells": [
                    {
                        "id": "one",
                        "language": "python",
                        "source": "print(params['day'])",
                    }
                ],
            },
        )
        response.raise_for_status()
        notebook = response.json()
        body = {
            "name": "Three captured dates",
            "project_id": "default",
            "notebook_id": notebook["id"],
            "date_parameter": "day",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "max_concurrency": 1,
        }
        preview = client.post("/api/backfills/preview", json=body)
        assert preview.status_code == 200 and preview.json()["count"] == 3
        created = client.post("/api/backfills", json=body)
        assert created.status_code == 201, created.text
        notebook["cells"][0]["source"] = "raise RuntimeError('edited after capture')"
        client.put(f"/api/notebooks/{notebook['id']}", json=notebook).raise_for_status()
        identifier = created.json()["id"]
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            completed = client.get(f"/api/backfills/{identifier}").json()
            if completed["status"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.1)
        assert completed["status"] == "succeeded", completed
        assert completed["progress"] == {"completed": 3, "total": 3}
        for entry in completed["items"]:
            workflow = client.get(f"/api/workflow-runs/{entry['run_id']}").json()
            run = client.get(f"/api/runs/{workflow['tasks'][0]['run_id']}").json()
            assert run["outputs"][0]["stdout"].strip() == entry["date"]
            assert run["resource_options"]["mode"] == "job"
