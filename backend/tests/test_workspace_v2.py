import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from openbricks.main import create_app
from openbricks.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "test-password-long-123")
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    with TestClient(create_app(tmp_path / "data")) as value:
        assert value.post("/api/auth/login", json={"username": "admin", "password": "test-password-long-123"}).status_code == 200
        yield value


def folder(client, name, parent_id=None):
    response = client.post("/api/folders", json={"name": name, "parent_id": parent_id})
    assert response.status_code == 201, response.text
    return response.json()


def notebook(client, name, folder_id=None):
    response = client.post("/api/notebooks", json={"name": name, "folder_id": folder_id, "cells": [{"id": "one", "language": "python", "source": "print('saved')"}]})
    assert response.status_code == 201, response.text
    return response.json()


def test_folders_move_rename_cycles_and_nonempty_protection(client):
    engineering = folder(client, "Engineering")
    daily = folder(client, "Daily", engineering["id"])
    assert daily["path"] == "/Engineering/Daily"
    assert client.post("/api/folders", json={"name": "engineering"}).status_code == 409
    for name in ("../escape", "nested/folder", "nested\\folder", ".", ".."):
        assert client.post("/api/folders", json={"name": name}).status_code == 400
    assert client.put(f"/api/folders/{engineering['id']}", json={"name": "Engineering", "parent_id": daily["id"]}).status_code == 400
    item = notebook(client, "Revenue", daily["id"])
    assert item["folder_path"] == "/Engineering/Daily"
    assert client.delete(f"/api/folders/{daily['id']}").status_code == 409
    assert client.delete(f"/api/folders/{engineering['id']}").status_code == 409
    # A normal old-client save must not accidentally move a notebook to root.
    response = client.put(f"/api/notebooks/{item['id']}", json={"name": "Renamed", "cells": item["cells"]})
    assert response.status_code == 200 and response.json()["folder_id"] == daily["id"]
    moved = client.patch(f"/api/notebooks/{item['id']}/location", json={"folder_id": None})
    assert moved.status_code == 200 and moved.json()["folder_id"] is None
    assert client.delete(f"/api/folders/{daily['id']}").status_code == 200
    assert client.delete(f"/api/folders/{engineering['id']}").status_code == 200
    assert client.patch(f"/api/notebooks/{item['id']}/location", json={"folder_id": "missing"}).status_code == 404


def test_workspace_is_paged_metadata_with_breadcrumbs_and_global_search(client):
    reports = folder(client, "Reports")
    nested = folder(client, "Daily", reports["id"])
    store = client.app.state.store
    for index in range(71):
        store.put("notebooks", {"id": f"report-{index:03}", "name": f"Report {index:03}", "folder_id": nested["id"], "language": "python",
            "created_at": f"2026-01-01T00:{index % 60:02}:00+00:00", "updated_at": f"2026-01-01T00:{index % 60:02}:00+00:00",
            "cells": [{"id": "large", "language": "python", "source": "large notebook source\n" * 1000}]})
    first = client.get("/api/workspace", params={"folder_id": nested["id"], "limit": 20}).json()
    second = client.get("/api/workspace", params={"folder_id": nested["id"], "limit": 20, "offset": 20}).json()
    assert first["total"] == 71 and len(first["items"]) == 20
    assert not ({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
    assert "cells" not in first["items"][0]
    assert [item["name"] for item in first["breadcrumbs"]] == ["Reports", "Daily"]
    found = client.get("/api/workspace", params={"search": "Report 070"}).json()
    assert found["total"] == 1 and found["items"][0]["folder_path"] == "/Reports/Daily"
    children = client.get("/api/workspace", params={"folder_id": reports["id"]}).json()
    assert children["folders"][0]["id"] == nested["id"]
    assert client.get("/api/workspace", params={"limit": 10000}).status_code == 422
    overview = client.get("/api/overview").json()
    assert len(overview["notebooks"]) == 12 and overview["metrics"]["notebooks"] == 72
    assert all("cells" not in item for item in overview["notebooks"])


def test_run_history_groups_and_pages_without_loading_outputs(client):
    reports = folder(client, "Reports")
    first = notebook(client, "Alpha report", reports["id"])
    second = notebook(client, "Beta report")
    removed = notebook(client, "Archived report")
    store = client.app.state.store
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(360):
        item = first if index < 250 else second if index < 350 else removed
        status = "succeeded" if index < 250 else "failed" if index < 350 else "cancelled"
        store.put("runs", {"id": f"run-{index:04}", "notebook_id": item["id"], "notebook_name": item["name"], "status": status,
            "created_at": (moment + timedelta(seconds=index)).isoformat(), "outputs": [{"stdout": "large output" * 10000}], "_cells": [{"source": "secret source"}]})
    remote = store.get("runs", "run-0000")
    remote.update(_notebook={"cells": [{"source": "large remote notebook source" * 10000}]}, _cell_id="one", _remote_submitted=True)
    store.put("runs", remote)
    assert client.delete(f"/api/notebooks/{removed['id']}").status_code == 200
    grouped = client.get("/api/history/notebooks", params={"limit": 2}).json()
    assert grouped["total"] == 3 and len(grouped["items"]) == 2
    archived = next(item for item in grouped["items"] if item["notebook_id"] == removed["id"])
    assert archived["deleted"] and archived["notebook_name"] == "Archived report"
    alpha = client.get("/api/history/notebooks", params={"search": "Alpha"}).json()["items"][0]
    assert alpha["total_runs"] == 250 and alpha["succeeded"] == 250 and alpha["folder_path"] == "/Reports"
    page = client.get("/api/history/runs", params={"notebook_id": first["id"], "limit": 25, "offset": 25}).json()
    assert page["total"] == 250 and len(page["items"]) == 25
    assert all("outputs" not in item and "_cells" not in item for item in page["items"])
    assert client.get("/api/history/runs", params={"notebook_id": first["id"], "status": "failed"}).json()["total"] == 0
    assert client.get("/api/history/runs", params={"status": "bogus"}).status_code == 400
    assert "outputs" in client.get("/api/runs/run-0000").json()
    direct_metadata = store.run_page(first["id"], limit=300)["items"]
    assert all("_notebook" not in item and "_remote_submitted" not in item and "_cell_id" not in item for item in direct_metadata)
    overview = client.get("/api/overview").json()
    assert len(overview["runs"]) == 8 and overview["metrics"]["runs"] == 360
    assert all("outputs" not in item for item in overview["runs"])


def test_job_pagination_and_search_reach_records_beyond_overview_limit(client, monkeypatch):
    store = client.app.state.store
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(610):
        store.put("jobs", {"id": f"job-{index:04}", "name": "Legacy nightly refresh" if index == 0 else f"Job {index:04}",
            "notebook_id": "example-notebook", "notebook_name": "Archived finance pipeline" if index == 0 else "New pipeline",
            "created_at": (moment + timedelta(seconds=index)).isoformat(), "cron": "0 8 * * *", "timezone": "UTC", "enabled": index % 2 == 0})
    overview = client.get("/api/overview").json()
    assert len(overview["jobs"]) == 500 and overview["metrics"]["jobs"] == 610
    original_list = store.list
    def guarded_list(kind, *args, **kwargs):
        assert kind != "jobs", "The job pager must use SQL bounds rather than loading every job"
        return original_list(kind, *args, **kwargs)
    monkeypatch.setattr(store, "list", guarded_list)
    page = client.get("/api/jobs/page", params={"limit": 25}).json()
    following = client.get("/api/jobs/page", params={"limit": 25, "offset": 25}).json()
    assert page["total"] == 610 and len(page["items"]) == 25 and page["items"][0]["id"] == "job-0609"
    assert not ({item["id"] for item in page["items"]} & {item["id"] for item in following["items"]})
    assert all("next_run_at" in item for item in page["items"])
    for search in ("legacy nightly", "FINANCE"):
        found = client.get("/api/jobs/page", params={"search": search}).json()
        assert found["total"] == 1 and found["items"][0]["id"] == "job-0000"
    assert client.get("/api/jobs/page", params={"limit": 10001}).status_code == 422


@pytest.mark.parametrize("basic,cron", [
    ({"frequency": "hourly", "interval": 3, "minute": 15}, "15 */3 * * *"),
    ({"frequency": "daily", "time": "08:30"}, "30 8 * * *"),
    ({"frequency": "weekly", "time": "16:05", "days_of_week": [5, 1, 3]}, "5 16 * * 1,3,5"),
])
def test_basic_schedule_derives_cron_and_persists_options(client, basic, cron):
    item = notebook(client, "Scheduled")
    response = client.post("/api/jobs", json={"name": "Friendly schedule", "notebook_id": item["id"], "schedule_mode": "basic", "basic_schedule": basic, "timezone": "America/New_York"})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["cron"] == cron and created["schedule_mode"] == "basic" and created["next_run_at"]
    assert created["basic_schedule"]["frequency"] == basic["frequency"]
    assert client.app.state.store.get("jobs", created["id"])["cron"] == cron


def test_invalid_basic_schedule_and_advanced_mode(client):
    item = notebook(client, "Scheduled")
    base = {"name": "Schedule", "notebook_id": item["id"], "schedule_mode": "basic", "timezone": "UTC"}
    for basic in ({"frequency": "weekly", "days_of_week": []}, {"frequency": "weekly", "days_of_week": [8]}, {"frequency": "daily", "time": "25:99"}, {"frequency": "nonsense"}):
        assert client.post("/api/jobs", json={**base, "basic_schedule": basic}).status_code == 400
    assert client.post("/api/jobs", json=base).status_code == 400
    response = client.post("/api/jobs", json={**base, "schedule_mode": "advanced", "cron": "0 8 * * 1-5", "basic_schedule": {"frequency": "daily"}})
    assert response.status_code == 201 and response.json()["basic_schedule"] is None


def test_existing_json_store_migrates_indexes_without_data_loss(tmp_path):
    directory = tmp_path / "old-data"
    directory.mkdir()
    original = {"id": "old-run", "notebook_id": "old-notebook", "notebook_name": "Old notebook", "status": "succeeded", "created_at": "2025-01-01T00:00:00+00:00", "outputs": [{"stdout": "kept"}]}
    with sqlite3.connect(directory / "openbricks.sqlite3") as db:
        db.execute("CREATE TABLE documents(kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(kind,id))")
        db.execute("INSERT INTO documents VALUES(?,?,?)", ("runs", original["id"], json.dumps(original)))
    store = Store(directory)
    assert store.get("runs", "old-run") == original
    assert store.run_notebook_page()["items"][0]["notebook_name"] == "Old notebook"
    assert store.run_page()["items"][0].get("outputs") is None
    reopened = Store(directory)
    assert reopened.metrics()["succeeded"] == 1
