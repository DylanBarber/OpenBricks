"""Live 0.5 feature and fresh-host recovery proof on fixed disposable origins.

prepare: 127.0.0.1:3001 only; leaves synthetic fixtures for the encrypted backup.
recover: 127.0.0.1:3002 only; verifies restored metadata, caches and secret access.
No Docker commands, production configuration or plaintext receipt credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "openbricks-v5-features-v1"
ORIGINS = {"prepare": "http://127.0.0.1:3001", "recover": "http://127.0.0.1:3002"}
TERMINAL = {
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
    "unknown",
    "skipped",
}


class SmokeError(RuntimeError):
    """Messages are synthetic; never include response bodies or credentials."""


def require(condition, message):
    if not condition:
        raise SmokeError(message)


def digest(value):
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )
    return hashlib.sha256(text.encode()).hexdigest()


def private_receipt(value):
    path = Path(value).resolve()
    require(
        path.is_relative_to((ROOT / ".test-data").resolve()),
        "Receipts must stay in the ignored project .test-data directory",
    )
    return path


def validate_origin(value, phase):
    parsed = urlsplit(value)
    require(
        value.rstrip("/") == ORIGINS[phase]
        and parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port == (3001 if phase == "prepare" else 3002)
        and parsed.path in ("", "/")
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment,
        "Use only this phase's fixed disposable origin",
    )
    return value.rstrip("/")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            json.dump(value, target, indent=2, allow_nan=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class API:
    def __init__(self, origin):
        self.client = httpx.Client(
            base_url=origin,
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
        )

    def call(self, method, path, *, expected=None, **kwargs):
        require(path.startswith("/") and ".." not in path, "Unsafe smoke API path")
        response = self.client.request(method, "/api" + path, **kwargs)
        allowed = (
            expected
            if isinstance(expected, tuple)
            else (expected,) if expected is not None else tuple(range(200, 300))
        )
        require(
            response.status_code in allowed,
            f"Smoke API {method} failed with HTTP {response.status_code}",
        )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise SmokeError(
                "Smoke API returned an unexpected non-JSON response"
            ) from None

    def login(self, username, password):
        require(bool(password), "Configure the disposable smoke login password")
        self.call(
            "POST", "/auth/login", json={"username": username, "password": password}
        )
        return self.call("GET", "/auth/me")

    def close(self):
        self.client.close()


def poll(api, path, expected="succeeded", label="run", timeout=180):
    deadline, heartbeat = time.monotonic() + timeout, time.monotonic()
    while time.monotonic() < deadline:
        value = api.call("GET", path)
        if value.get("status") in TERMINAL:
            require(
                value["status"] == expected,
                f"The {label} did not reach its expected terminal state",
            )
            return value
        if time.monotonic() - heartbeat >= 20:
            print("RUN: waiting for " + label, flush=True)
            heartbeat = time.monotonic()
        time.sleep(0.5)
    raise SmokeError("Timed out waiting for " + label)


def run_notebook(api, notebook_id, memory=512):
    value = api.call(
        "POST",
        f"/notebooks/{notebook_id}/run",
        expected=202,
        json={
            "timeout_seconds": 180,
            "resource_options": {"mode": "job", "memory_mb": memory},
        },
    )
    return poll(api, "/runs/" + value["id"], label="notebook worker")


def make_notebook(api, project, name, source, cell_id="main"):
    return api.call(
        "POST",
        "/notebooks",
        expected=201,
        json={
            "project_id": project,
            "name": name,
            "cells": [{"id": cell_id, "language": "python", "source": source}],
        },
    )


def stdout(run):
    return "\n".join(str(output.get("stdout", "")) for output in run.get("outputs", []))


def cache_identity(snapshot):
    return digest(
        {
            key: snapshot.get(key)
            for key in (
                "parameters",
                "tiles",
                "refreshed_at",
                "refresh_id",
                "generation",
            )
        }
    )


def profile_proof(api, run_id, *, spark=False):
    value = api.call("GET", "/runs/" + run_id + "/profile")
    profile = value.get("profile")
    require(
        isinstance(profile, dict) and profile.get("version") == 1,
        "The run has no persisted versioned profile",
    )
    require(
        bool(profile.get("resources", {}).get("samples")),
        "The run profile has no measured resource samples",
    )
    if spark:
        require(
            profile.get("spark", {}).get("status") == "measured"
            and bool(profile.get("spark", {}).get("stages")),
            "Real Spark stage metrics were not recorded",
        )
    return digest(profile)


def prepare(api, receipt_path):
    require(not receipt_path.exists(), "Prepare requires a fresh receipt path")
    state = {
        "format": FORMAT,
        "suffix": uuid4().hex[:12],
        "source_origin": ORIGINS["prepare"],
        "checks": [],
        "prepared": False,
    }
    suffix = state["suffix"]
    account = api.call("GET", "/auth/me")
    require(account.get("role") == "admin", "The smoke login must be an administrator")
    state["admin_id"] = account["id"]
    state["project_id"] = api.call(
        "POST",
        "/projects",
        expected=201,
        json={
            "name": "V5 full smoke " + suffix,
            "description": "Synthetic 0.5 recovery fixtures",
            "connection_ids": [],
            "cluster_ids": ["local"],
        },
    )["id"]
    project = state["project_id"]

    # A separate viewer and editor exercise actual role checks. Their temporary
    # passwords are never persisted; recovery verification uses the restored admin.
    actors = {}
    clients = []
    try:
        for role in ("viewer", "editor"):
            password = secrets.token_urlsafe(32)
            username = "v5_" + role + "_" + suffix
            value = api.call(
                "POST",
                "/users",
                expected=201,
                json={"username": username, "password": password, "role": "member"},
            )
            api.call(
                "PUT", f"/projects/{project}/members/{value['id']}", json={"role": role}
            )
            if role == "viewer":
                # New local members inherit the legacy shared project. Remove
                # only this disposable viewer's inherited grant so isolation
                # from a real, existing foreign project is exercised too.
                api.call("DELETE", f"/projects/default/members/{value['id']}")
            client = API(ORIGINS["prepare"])
            client.login(username, password)
            clients.append(client)
            actors[role] = (value, client)
            state[role + "_id"] = value["id"]

        # Actual saved-query execution and a cached result with a bound filter.
        query = api.call(
            "POST",
            "/data/queries",
            expected=201,
            json={
                "project_id": project,
                "name": "V5 total " + suffix,
                "engine": "duckdb",
                "sql": "SELECT ? AS total",
                "params": [7],
            },
        )
        state["query_id"] = query["id"]
        dashboard = api.call(
            "POST",
            "/dashboards",
            expected=201,
            json={
                "project_id": project,
                "name": "V5 dashboard " + suffix,
                "parameters": [{"name": "value", "type": "integer", "default": 7}],
                "tiles": [
                    {
                        "id": "total",
                        "title": "Total",
                        "query_id": query["id"],
                        "chart": "number",
                        "y": "total",
                        "width": 6,
                        "height": 4,
                        "parameter_bindings": {"0": "value"},
                    }
                ],
            },
        )
        state["dashboard_id"] = dashboard["id"]
        dashboard_path = "/dashboards/" + dashboard["id"]
        refresh = api.call(
            "POST",
            dashboard_path + "/refresh",
            expected=202,
            json={"parameters": {"value": 42}},
        )
        complete = poll(
            api,
            dashboard_path + "/refreshes/" + refresh["id"],
            label="dashboard refresh",
        )
        require(
            complete["tiles"][0].get("run_id"),
            "The dashboard did not link its compute run",
        )
        state["dashboard_run_id"] = complete["tiles"][0]["run_id"]
        parameters = {"parameters": json.dumps({"value": 42})}
        snapshot = api.call("GET", dashboard_path + "/snapshot", params=parameters)
        require(
            snapshot["tiles"][0]["result"]["rows"] == [[42]],
            "The bound dashboard parameter did not reach compute",
        )
        require(
            not snapshot["stale"],
            "A newly created dashboard cache was unexpectedly stale",
        )
        viewer_api = actors["viewer"][1]
        before = api.call(
            "GET",
            "/data/executions",
            params={"project_id": project, "query_id": query["id"]},
        )["total"]
        for _ in range(2):
            viewed = viewer_api.call(
                "GET", dashboard_path + "/snapshot", params=parameters
            )
            require(
                viewed["tiles"][0]["result"]["rows"] == [[42]],
                "The viewer could not read its project's cached result",
            )
        after = api.call(
            "GET",
            "/data/executions",
            params={"project_id": project, "query_id": query["id"]},
        )["total"]
        require(
            before == after, "Reading a dashboard unexpectedly submitted compute work"
        )
        viewer_api.call("POST", dashboard_path + "/refresh", expected=403, json={})
        api.call(
            "GET",
            dashboard_path + "/snapshot",
            expected=400,
            params={"parameters": json.dumps({"unknown": 1})},
        )
        viewer_api.call(
            "GET", "/assets", expected=403, params={"project_id": "default"}
        )
        state["checks"].append("dashboard_cache_parameters_and_viewer_permissions")
        print(
            "PASS: real parameterized dashboard refresh and viewer cache-only access",
            flush=True,
        )

        # An explicit query failure must not replace a known-good cache.
        api.call(
            "PUT",
            "/data/queries/" + query["id"],
            json={**query, "sql": "SELECT * FROM missing_v5_" + suffix + " WHERE 1=?"},
        )
        failed = api.call(
            "POST",
            dashboard_path + "/refresh",
            expected=202,
            json={"parameters": {"value": 42}},
        )
        poll(
            api,
            dashboard_path + "/refreshes/" + failed["id"],
            expected="failed",
            label="intentional failed dashboard refresh",
        )
        preserved = api.call("GET", dashboard_path + "/snapshot", params=parameters)
        require(
            preserved["refresh_id"] == snapshot["refresh_id"] and preserved["stale"],
            "A failed refresh discarded or relabeled the successful cache",
        )
        state["snapshot_sha256"] = cache_identity(preserved)
        state["successful_refresh_id"], state["failed_refresh_id"] = (
            refresh["id"],
            failed["id"],
        )
        state["checks"].append("failed_refresh_preserves_last_success")

        source, target = (
            "blob://workspace/v5_" + suffix + "/source",
            "blob://workspace/v5_" + suffix + "/gold",
        )
        state["source_dataset"], state["target_dataset"] = source, target
        code = "\n".join(
            [
                "frame = spark.createDataFrame([(1, 10), (2, 20)], ['OrderId', 'Amount'])",
                "data.lineage("
                + repr(source)
                + ", 'read', {'proof': 'v5-explicit-observation'})",
                "data.lineage("
                + repr(target)
                + ", 'write', {'proof': 'v5-explicit-observation'})",
                "data.quality(frame, [{'type': 'not_null', 'column': 'OrderId'}, {'type': 'row_count', 'min': 1}], dataset="
                + repr(target)
                + ")",
                "display(frame)",
            ]
        )
        data_book = make_notebook(api, project, "V5 dataset evidence " + suffix, code)
        state["data_notebook_id"] = data_book["id"]
        state["data_source_sha256"] = digest(code)
        data_run = run_notebook(api, data_book["id"], memory=1536)
        state["data_run_id"] = data_run["id"]
        state["data_profile_sha256"] = profile_proof(api, data_run["id"], spark=True)
        graph = api.call(
            "GET",
            "/assets/graph",
            params={
                "project_id": project,
                "dataset": target,
                "direction": "upstream",
                "depth": 2,
            },
        )
        require(
            {node["dataset"] for node in graph["nodes"]} == {source, target},
            "The observed asset graph lost a dataset",
        )
        require(
            len(graph["edges"]) == 1 and graph["edges"][0]["run_id"] == data_run["id"],
            "The asset dependency was not linked to its real run",
        )
        asset = next(node for node in graph["nodes"] if node["dataset"] == target)
        state["asset_id"] = asset["id"]
        description = "V5 recovery proof dataset " + suffix
        annotated = api.call(
            "PUT",
            "/assets/" + asset["id"],
            json={
                "project_id": project,
                "dataset": target,
                "description": description,
                "owner_id": account["id"],
                "freshness_seconds": 3600,
            },
        )
        require(
            annotated["owner_id"] == account["id"] and annotated["quality"]["passed"],
            "Asset ownership or observed quality was not retained",
        )
        state["asset_description_sha256"] = digest(description)
        history = api.call(
            "GET",
            "/assets/" + asset["id"] + "/quality",
            params={"project_id": project, "limit": 1},
        )
        require(
            history["total"] == 1 and history["items"][0]["run_id"] == data_run["id"],
            "The asset quality history did not reference its compute run",
        )
        api.call(
            "GET",
            "/assets/graph",
            expected=422,
            params={"project_id": project, "depth": 3},
        )
        api.call(
            "GET", "/assets", expected=422, params={"project_id": project, "limit": 101}
        )
        state["checks"].append("spark_quality_asset_health_graph_and_profile")
        print(
            "PASS: real Spark quality, dataset graph, ownership and measured profiler",
            flush=True,
        )

        # The only stored expectation about the credential is its SHA-256.
        secret_value = "v5-fixture-secret-" + secrets.token_hex(32)
        secret_hash = digest(secret_value)
        scope_name = "v5_scope_" + suffix
        state["scope_name"], state["secret_sha256"] = scope_name, secret_hash
        scope = api.call(
            "POST",
            "/secret-scopes",
            expected=201,
            json={
                "project_id": project,
                "name": scope_name,
                "allowed_roles": ["owner"],
            },
        )
        state["scope_id"] = scope["id"]
        key = api.call(
            "PUT",
            "/secret-scopes/" + scope["id"] + "/keys/token",
            json={"value": secret_value},
        )
        require(
            "value" not in key and key.get("version") == 1,
            "The secret write response exposed a value or lost its version",
        )
        api.call(
            "GET", "/secret-scopes/" + scope["id"] + "/keys/token", expected=(404, 405)
        )
        viewer_api.call(
            "PUT",
            "/secret-scopes/" + scope["id"] + "/keys/token",
            expected=403,
            json={"value": "disposable-unprivileged-write"},
        )
        secret_source = "\n".join(
            [
                "import hashlib, time",
                "value = dbutils.secrets.get(" + repr(scope_name) + ", 'token')",
                "assert notebook_secrets.get("
                + repr(scope_name)
                + ", 'token') == value",
                "print('split:', value[:8], sep='', end='', flush=True)",
                "print(value[8:])",
                "display([{value: value}])",
                "print('secret_sha256=' + hashlib.sha256(value.encode()).hexdigest())",
                "time.sleep(0.35)",
            ]
        )
        secret_book = make_notebook(
            api, project, "V5 secret proof " + suffix, secret_source, "secret"
        )
        state["secret_notebook_id"] = secret_book["id"]
        selection = api.call(
            "PUT",
            "/notebooks/" + secret_book["id"] + "/secret-refs",
            json={
                "secret_refs": [{"scope": scope_name, "key": "token"}],
                "version": secret_book.get("version", 1),
            },
        )
        require(
            selection["secret_refs"] == [{"scope": scope_name, "key": "token"}],
            "The notebook secret selection was not recorded",
        )
        secret_run = run_notebook(api, secret_book["id"])
        encoded = json.dumps(secret_run)
        require(
            secret_value not in encoded and secret_value[:8] not in stdout(secret_run),
            "Captured notebook output exposed the synthetic secret",
        )
        require(
            "[REDACTED]" in encoded
            and "secret_sha256=" + secret_hash in stdout(secret_run),
            "The selected secret was not usable and redacted",
        )
        state["secret_run_id"] = secret_run["id"]
        state["secret_profile_sha256"] = profile_proof(api, secret_run["id"])
        metadata = api.call("GET", "/secret-scopes", params={"project_id": project})
        require(
            secret_value not in json.dumps(metadata),
            "Secret metadata exposed the synthetic credential",
        )
        state["checks"].append(
            "notebook_secret_selection_use_redaction_and_write_only_api"
        )
        print(
            "PASS: selected notebook secret is usable, write-only and redacted",
            flush=True,
        )

        # Live editing persists source while leases and presence remain ephemeral.
        path = "/notebooks/" + secret_book["id"]
        book = api.call("GET", path)
        room = api.call(
            "PUT",
            path + "/collaboration",
            json={"enabled": True, "version": book.get("version", 1)},
        )
        client_id = "v5_smoke_" + suffix
        api.call(
            "POST",
            path + "/presence",
            json={"client_id": client_id, "cell_id": "secret"},
        )
        lease = api.call(
            "POST",
            path + "/collaboration/lease",
            json={"client_id": client_id, "cell_id": "secret"},
        )
        edited = secret_source + "\nprint('collaboration-checkpoint')"
        payload = {
            "client_id": client_id,
            "cell_id": "secret",
            "token": lease["token"],
            "base_hash": lease["source_hash"],
            "source": edited,
        }
        changed = api.call("PATCH", path + "/collaboration/cell", json=payload)
        require(
            changed["source_hash"] == digest(edited),
            "The leased cell edit did not persist its exact source",
        )
        api.call(
            "POST",
            path + "/collaboration/release",
            json={**payload, "base_hash": changed["source_hash"]},
        )
        room = api.call("GET", path + "/collaboration")
        require(not room["leases"], "The editing lease was not released")
        api.call(
            "PUT",
            path + "/collaboration",
            json={"enabled": False, "version": room["version"]},
        )
        state["source_sha256"] = digest(edited)
        comment_text = "V5 durable comment " + suffix
        comment = api.call(
            "POST",
            path + "/comments",
            expected=201,
            json={"text": comment_text, "cell_id": "secret"},
        )
        reply_text = "V5 durable reply " + suffix
        reply = actors["editor"][1].call(
            "POST",
            path + "/comments",
            expected=201,
            json={"text": reply_text, "parent_id": comment["id"]},
        )
        review = api.call(
            "POST",
            path + "/reviews",
            expected=201,
            json={
                "reviewer_id": actors["editor"][0]["id"],
                "message": "Review this exact smoke source",
            },
        )
        decision = actors["editor"][1].call(
            "POST",
            path + "/reviews/" + review["id"] + "/decision",
            json={"decision": "approved", "message": "Synthetic smoke approval"},
        )
        require(
            decision["status"] == "approved" and not decision["stale"],
            "The exact-source review was not approved",
        )
        viewer_api.call(
            "POST",
            path + "/comments",
            expected=403,
            json={"text": "Viewer cannot edit discussions"},
        )
        state.update(
            comment_id=comment["id"],
            reply_id=reply["id"],
            comment_sha256=digest(comment_text),
            reply_sha256=digest(reply_text),
            review_id=review["id"],
            review_source_hash=review["source_hash"],
            comment_count=2,
            review_count=1,
        )
        state["checks"].append("live_cell_edit_presence_comments_and_source_review")
        state["prepared"] = True
        state["notebook_count"] = 2
        state["run_count"] = api.call(
            "GET", "/history/runs", params={"project_id": project, "limit": 1}
        )["total"]
        state["asset_count"] = api.call(
            "GET", "/assets", params={"project_id": project}
        )["total"]
        save(receipt_path, state)
        print(
            "PASS: durable live-edit source, threaded comments and reviewed revision prepared for encrypted recovery",
            flush=True,
        )
        print(
            json.dumps(
                {
                    "phase": "prepare",
                    "passed": True,
                    "checks": len(state["checks"]),
                    "notebooks": state["notebook_count"],
                    "receipt": str(receipt_path),
                }
            ),
            flush=True,
        )
    finally:
        for client in clients:
            client.close()


def validate_state(value):
    require(
        value.get("format") == FORMAT
        and value.get("prepared") is True
        and value.get("source_origin") == ORIGINS["prepare"],
        "The recovery receipt is not a completed v5 prepare proof",
    )
    for key in (
        "admin_id",
        "project_id",
        "query_id",
        "dashboard_id",
        "dashboard_run_id",
        "successful_refresh_id",
        "failed_refresh_id",
        "data_notebook_id",
        "data_run_id",
        "secret_notebook_id",
        "secret_run_id",
        "viewer_id",
        "editor_id",
        "comment_id",
        "reply_id",
        "review_id",
    ):
        require(
            bool(re.fullmatch(r"[a-f0-9]{32}", value.get(key, ""))),
            "A recovery fixture identifier is invalid",
        )
    for key in (
        "scope_id",
        "asset_id",
        "secret_sha256",
        "snapshot_sha256",
        "source_sha256",
        "data_profile_sha256",
        "data_source_sha256",
        "secret_profile_sha256",
        "comment_sha256",
        "reply_sha256",
        "review_source_hash",
        "asset_description_sha256",
    ):
        require(
            bool(re.fullmatch(r"[a-f0-9]{64}", value.get(key, ""))),
            "A recovery proof digest is invalid",
        )
    require(
        bool(re.fullmatch(r"[a-f0-9]{12}", value.get("suffix", "")))
        and value.get("scope_name") == "v5_scope_" + value["suffix"],
        "A recovery scope name is invalid",
    )


def recover(api, receipt_path):
    require(receipt_path.is_file(), "The v5 prepare receipt is missing")
    require(
        receipt_path.stat().st_size <= 262144,
        "The v5 prepare receipt exceeds its bounded metadata size",
    )
    state = json.loads(receipt_path.read_text(encoding="utf-8"))
    validate_state(state)
    project = state["project_id"]
    require(
        api.call("GET", "/auth/me")["id"] == state["admin_id"],
        "Recovery is not using the restored source administrator",
    )
    projects = api.call("GET", "/projects")
    require(
        any(item["id"] == project for item in projects),
        "The v5 project was not restored",
    )
    dashboard_path = "/dashboards/" + state["dashboard_id"]
    notebooks = api.call(
        "GET", "/notebooks", params={"project_id": project, "limit": 100}
    )
    require(
        len(notebooks) == state["notebook_count"]
        and {state["data_notebook_id"], state["secret_notebook_id"]}
        == {item["id"] for item in notebooks},
        "The restored notebook count or identifiers differ from the captured project",
    )
    data_book = api.call("GET", "/notebooks/" + state["data_notebook_id"])
    require(
        digest(data_book["cells"][0]["source"]) == state["data_source_sha256"],
        "The restored data notebook source differs from its captured hash",
    )
    require(
        api.call("GET", "/history/runs", params={"project_id": project, "limit": 1})[
            "total"
        ]
        == state["run_count"],
        "The restored run history count differs from the captured project",
    )
    dashboard = api.call("GET", dashboard_path)
    require(
        dashboard["project_id"] == project
        and dashboard["tiles"][0]["parameter_bindings"] == {"0": "value"},
        "The dashboard definition or parameter binding was not restored",
    )
    before = api.call(
        "GET",
        "/data/executions",
        params={"project_id": project, "query_id": state["query_id"]},
    )["total"]
    cached = api.call(
        "GET",
        dashboard_path + "/snapshot",
        params={"parameters": json.dumps({"value": 42})},
    )
    after = api.call(
        "GET",
        "/data/executions",
        params={"project_id": project, "query_id": state["query_id"]},
    )["total"]
    require(
        cache_identity(cached) == state["snapshot_sha256"]
        and cached["latest_refresh"]["status"] == "failed",
        "The successful cache or failed-refresh evidence was not restored",
    )
    require(before == after, "Reading a restored cache unexpectedly started compute")
    refreshes = api.call("GET", dashboard_path + "/refreshes")
    require(
        {state["successful_refresh_id"], state["failed_refresh_id"]}
        <= {item["id"] for item in refreshes["items"]},
        "Dashboard refresh history was not restored",
    )
    asset = api.call(
        "GET", "/assets/" + state["asset_id"], params={"project_id": project}
    )
    require(
        asset["owner_id"] == state["admin_id"]
        and asset["freshness_seconds"] == 3600
        and digest(asset["description"]) == state["asset_description_sha256"],
        "Asset ownership, description or freshness policy was not restored",
    )
    require(
        asset["quality"]["passed"]
        and asset["quality"]["run_id"] == state["data_run_id"],
        "Observed asset quality evidence was not restored",
    )
    require(
        api.call("GET", "/assets", params={"project_id": project})["total"]
        == state["asset_count"],
        "The restored asset count differs from the captured project",
    )
    graph = api.call(
        "GET",
        "/assets/graph",
        params={
            "project_id": project,
            "dataset": asset["dataset"],
            "direction": "upstream",
            "depth": 2,
        },
    )
    require(
        len(graph["nodes"]) == state["asset_count"] and len(graph["edges"]) == 1,
        "The observed asset dependency graph was not restored",
    )
    require(
        profile_proof(api, state["data_run_id"], spark=True)
        == state["data_profile_sha256"]
        and profile_proof(api, state["secret_run_id"])
        == state["secret_profile_sha256"],
        "Persisted run profiles changed during recovery",
    )
    print(
        "PASS: restored dashboard caches, data asset evidence and exact run profiles",
        flush=True,
    )

    path = "/notebooks/" + state["secret_notebook_id"]
    book = api.call("GET", path)
    require(
        book["project_id"] == project
        and digest(book["cells"][0]["source"]) == state["source_sha256"],
        "The live-edited notebook source was not restored exactly",
    )
    require(
        book.get("secret_refs") == [{"scope": state["scope_name"], "key": "token"}],
        "The notebook's explicit secret references were not restored",
    )
    room = api.call("GET", path + "/collaboration")
    require(
        not room["enabled"] and not room["leases"] and not room["presence"],
        "Ephemeral editing leases or presence were incorrectly restored",
    )
    comments = api.call("GET", path + "/comments")
    require(
        comments["total"] == state["comment_count"],
        "The restored comment count is incorrect",
    )
    texts = {item["id"]: digest(item["text"]) for item in comments["items"]}
    require(
        texts.get(state["comment_id"]) == state["comment_sha256"]
        and texts.get(state["reply_id"]) == state["reply_sha256"],
        "A restored discussion thread differs from its captured text",
    )
    reviews = api.call("GET", path + "/reviews")
    require(
        reviews["total"] == state["review_count"],
        "The restored review count is incorrect",
    )
    review = next(
        (item for item in reviews["items"] if item["id"] == state["review_id"]), None
    )
    require(
        review
        and review["status"] == "approved"
        and not review["stale"]
        and review["source_hash"] == state["review_source_hash"],
        "The exact-source review was not restored",
    )
    scopes = api.call("GET", "/secret-scopes", params={"project_id": project})
    scope = next(
        (item for item in scopes["items"] if item["id"] == state["scope_id"]), None
    )
    require(
        scope
        and any(
            key["name"] == "token" and key["version"] == 1 for key in scope["keys"]
        ),
        "Secret scope/key metadata was not restored",
    )
    api.call(
        "GET",
        "/secret-scopes/" + state["scope_id"] + "/keys/token",
        expected=(404, 405),
    )
    restored = run_notebook(api, book["id"])
    require(
        "secret_sha256=" + state["secret_sha256"] in stdout(restored)
        and "[REDACTED]" in json.dumps(restored),
        "The restored encryption key and selected notebook secret did not reproduce the captured hash",
    )
    require(
        "collaboration-checkpoint" in stdout(restored),
        "The restored notebook did not execute its live-edited source",
    )
    state["recovered"] = True
    state["recovery_run_id"] = restored["id"]
    save(receipt_path, state)
    print(
        "PASS: restored discussions, reviewed source and usable redacted notebook secret",
        flush=True,
    )
    print(
        json.dumps(
            {
                "phase": "recover",
                "passed": True,
                "notebooks": state["notebook_count"],
                "secret_hash_verified": True,
            }
        ),
        flush=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("prepare", "recover"))
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--base-url")
    args = parser.parse_args(argv)
    origin = validate_origin(args.base_url or ORIGINS[args.phase], args.phase)
    receipt = private_receipt(args.receipt)
    username = (
        os.getenv("OPENBRICKS_SMOKE_USERNAME") or os.getenv("SMOKE_USERNAME") or "admin"
    )
    password = os.getenv("OPENBRICKS_SMOKE_PASSWORD") or os.getenv("SMOKE_PASSWORD")
    if args.phase == "recover":
        username = os.getenv("OPENBRICKS_RECOVERY_USERNAME") or username
        password = os.getenv("OPENBRICKS_RECOVERY_SOURCE_PASSWORD") or password
    api = API(origin)
    try:
        require(
            api.login(username, password).get("role") == "admin",
            "The smoke login must be an administrator",
        )
        (prepare if args.phase == "prepare" else recover)(api, receipt)
    finally:
        api.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "FAIL: "
            + (str(exc) if isinstance(exc, SmokeError) else type(exc).__name__),
            flush=True,
        )
        raise SystemExit(1)
