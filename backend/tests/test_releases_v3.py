import io
import subprocess
import time
import zipfile

import httpx
from fastapi.testclient import TestClient

from openbricks.environments import artifact_path, capture_environment
from openbricks.releases import capture_release
from openbricks.runtime import LocalRuntime
from openbricks.store import Store


class Access:
    def require_project(self, *args):
        return {}

    def require_connection(self, *args):
        return {}

    def require_notebook(self, *args):
        return {}


def wheel(value):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        prefix = "pinned_demo-1.0.dist-info/"
        entries = {"pinned_demo.py": f"VALUE = {value!r}\n", prefix + "METADATA": "Metadata-Version: 2.1\nName: pinned-demo\nVersion: 1.0\n",
            prefix + "WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
        for path, content in entries.items():
            archive.writestr(path, content)
        archive.writestr(prefix + "RECORD", "".join(path + ",,\n" for path in [*entries, prefix + "RECORD"]))
    return output.getvalue()


def test_immutable_wheels_switch_one_interpreter_and_replay(tmp_path):
    store = Store(tmp_path)
    directory = tmp_path / "wheels"
    directory.mkdir()
    filename = "pinned_demo-1.0-py3-none-any.whl"
    (directory / filename).write_bytes(wheel("first"))
    store.put("packages", {"id": "pkg", "name": "pinned-demo", "filename": filename, "installed_at": "1"})
    first = capture_environment(store)
    immutable = artifact_path(tmp_path, first["wheels"][0]).read_bytes()
    (directory / filename).write_bytes(wheel("second"))
    second = capture_environment(store)
    assert first["id"] != second["id"]
    assert artifact_path(tmp_path, first["wheels"][0]).read_bytes() == immutable
    runtime = LocalRuntime(store)
    runtime.start()
    try:
        processes = []
        for environment, expected in ((first, "first"), (second, "second"), (first, "first")):
            notebook = {"id": "pinned", "name": "Pinned", "cells": [{"id": "one", "language": "python", "source": "import pinned_demo; print(pinned_demo.VALUE)"}]}
            run = runtime.submit(notebook, environment=environment)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                done = store.get("runs", run["id"])
                if done["status"] not in ("queued", "running"):
                    break
                time.sleep(0.03)
            assert done["status"] == "succeeded", done
            assert done["outputs"][0]["stdout"].strip() == expected
            processes.append(runtime.process.pid)
        assert len(set(processes)) == 3
    finally:
        runtime.close()


def test_release_reads_exact_git_objects_not_mutable_checkout(tmp_path):
    store = Store(tmp_path)
    base = tmp_path / "repositories" / "source"
    base.mkdir(parents=True)
    def git(*args):
        result = subprocess.run(["git", "-C", str(base), *args], capture_output=True, text=True, check=True)
        return result.stdout.strip()
    git("init", "--initial-branch=main")
    source = base / "pipeline.py"
    source.write_text("# Databricks notebook source\nprint('committed')\n", encoding="utf-8")
    git("add", "pipeline.py")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=", "commit", "-m", "Fixture snapshot")
    commit = git("rev-parse", "HEAD")
    source.write_text("# Databricks notebook source\nprint('uncommitted')\n", encoding="utf-8")
    store.put("repositories", {"id": "source", "name": "Source", "project_id": "default"})
    body = {"name": "Pinned", "project_id": "default", "repository_id": "source", "commit": commit, "paths": ["pipeline.py"]}
    first = capture_release(store, body, {"id": "owner"}, Access())
    second = capture_release(store, body, {"id": "owner"}, Access())
    assert first["commit"] == commit
    assert first["notebooks"][0]["notebook"]["cells"][0]["source"] == "print('committed')"
    assert first["source_sha256"] == second["source_sha256"]


def test_real_remote_http_manifest_upload_parameters_and_live_results(tmp_path, monkeypatch):
    from openbricks import compute_client
    from openbricks.compute import create_compute_app
    from openbricks.credentials import SecretBox
    token = "real-compute-protocol-test-token-123456789"
    store = Store(tmp_path / "control")
    wheels = store.directory / "wheels"
    wheels.mkdir()
    filename = "pinned_demo-1.0-py3-none-any.whl"
    (wheels / filename).write_bytes(wheel("remote"))
    store.put("packages", {"id": "package", "name": "pinned-demo", "filename": filename})
    store.put("clusters", {"id": "remote", "name": "Remote", "url": "https://agent.example", "verify_tls": True,
        "_token": SecretBox(store.directory).encrypt(token), "_shared_data": False})
    with TestClient(create_compute_app(tmp_path / "agent", token)) as agent:
        requests = []
        def forward(request):
            requests.append((request.method, request.url.path))
            response = agent.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content, headers={"content-type": "application/json"})
        monkeypatch.setattr(compute_client, "client_for", lambda store, entry: httpx.Client(base_url=entry["url"], headers={"Authorization": "Bearer " + token}, transport=httpx.MockTransport(forward)))
        runtime = compute_client.ClusterRuntime(store)
        runtime.start()
        try:
            notebook = {"id": "remote-notebook", "name": "Remote parameters", "project_id": "default", "connection_ids": [],
                "parameter_definitions": [{"name": "count", "type": "integer", "required": True}],
                "cells": [{"id": "one", "language": "python", "source": "import pinned_demo,time\nprint('live remote')\ntime.sleep(0.5)\nprint(pinned_demo.VALUE, params['count'], dbutils.widgets.get('count'))"}]}
            run = runtime.submit(notebook, cluster_id="remote", parameters={"count": 5})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                done = store.get("runs", run["id"])
                if done["status"] not in ("queued", "running"):
                    break
                time.sleep(0.03)
            assert done["status"] == "succeeded", done
            assert "remote 5 5" in done["outputs"][0]["stdout"]
            assert done["logs"] and done["peak_memory_bytes"] > 0
            assert ("POST", "/artifacts/wheels") in requests
            assert ("POST", "/environment/wheels") not in requests
            remote = agent.app.state.store.get("runs", run["id"])
            assert remote["environment_id"] == done["environment_id"]
        finally:
            runtime.close()


def test_release_and_environment_lists_project_scoped_bounded_metadata_only(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from fastapi import FastAPI
    from openbricks import releases
    from openbricks.releases import register_release_routes
    store = Store(tmp_path)
    store.put("releases", {"id": "large", "name": "Large source", "project_id": "default", "created_at": "2026-11-05T12:00:00+00:00",
        "notebooks": [{"path": "large.py", "notebook": {"cells": [{"id": "one", "language": "python", "source": "SOURCE_MUST_STAY_IN_SQLITE" + "x" * 2_000_000}]}}],
        "_environment": {"private": "manifest"}})
    for index in range(205):
        store.put("releases", {"id": f"release-{index:03}", "name": "Release", "project_id": "default", "created_at": "2026-11-05T00:00:00+00:00", "notebooks": []})
        identity = f"{index:064x}"
        store.put("environments", {"id": "default:" + identity, "manifest_id": identity, "name": "Environment", "project_id": "default", "created_at": "2026-11-05T00:00:00+00:00", "wheels": []})
    store.put("releases", {"id": "other-project", "project_id": "other", "created_at": "2026-11-06T00:00:00+00:00", "notebooks": []})
    store.put("environments", {"id": "other:manifest", "manifest_id": "other-manifest", "project_id": "other", "created_at": "2026-11-06T00:00:00+00:00", "wheels": []})
    original_list = store.list
    def forbid_full_listing(kind, *args, **kwargs):
        if kind in ("releases", "environments"):
            raise AssertionError("Listing must project/filter/page in SQL")
        return original_list(kind, *args, **kwargs)
    monkeypatch.setattr(store, "list", forbid_full_listing)
    loaded_sizes = []
    def metadata_json(value):
        assert "SOURCE_MUST_STAY_IN_SQLITE" not in value
        loaded_sizes.append(len(value))
        return json.loads(value)
    monkeypatch.setattr(releases, "json", SimpleNamespace(loads=metadata_json))
    app = FastAPI()
    app.state.store = store
    register_release_routes(app, lambda: {"role": "admin"}, Access())
    with TestClient(app) as client:
        response = client.get("/api/releases")
        assert response.status_code == 200, response.text
        items = response.json()
        assert len(items) == 200 and items[0]["id"] == "large" and items[0]["notebook_count"] == 1
        assert "notebooks" not in items[0] and "_environment" not in items[0]
        assert "other-project" not in {item["id"] for item in items}
        assert len(response.content) < 100_000 and max(loaded_sizes) < 10_000
        assert len(client.get("/api/releases", params={"limit": 2, "offset": 200}).json()) == 2
        environments = client.get("/api/environments").json()
        assert len(environments) == 200 and all(item["project_id"] == "default" for item in environments)
        assert len(client.get("/api/environments", params={"limit": 2, "offset": 200}).json()) == 2
        assert client.get("/api/releases", params={"limit": 201}).status_code == 422
