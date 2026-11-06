"""Secret values cross only the explicit execution boundary, never metadata APIs."""

from concurrent.futures import ThreadPoolExecutor
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from openbricks.execution import Redactor, StreamRedactor
from openbricks.notebook_secrets import register_secret_routes, secrets_for_runtime
from openbricks.permissions import Access
from openbricks.secret_runtime import (
    RedactionHistory,
    SecretHistoryFull,
    SecretRegistry,
    redaction_connections,
)
from openbricks.store import Store


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.state.store = Store(tmp_path)
    app.state.access = Access(app.state.store)
    app.state.access.seed()
    for identifier, role in (
        ("admin", "admin"),
        ("editor", "member"),
        ("viewer", "member"),
        ("operator", "member"),
    ):
        app.state.store.put(
            "users", {"id": identifier, "username": identifier, "role": role}
        )
        if role != "admin":
            app.state.store.put(
                "project_members",
                {
                    "id": "default:" + identifier,
                    "user_id": identifier,
                    "project_id": "default",
                    "role": identifier,
                },
            )
    app.state.store.put(
        "notebooks",
        {
            "id": "book",
            "name": "Book",
            "project_id": "default",
            "version": 1,
            "cells": [],
        },
    )

    def authenticated(request: Request):
        user = app.state.store.get("users", request.headers.get("x-test-user", "admin"))
        if not user:
            raise HTTPException(401)
        return user

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    register_secret_routes(app, authenticated)
    with TestClient(app) as value:
        yield value


def scope(client):
    return client.post(
        "/api/secret-scopes", json={"name": "warehouse", "project_id": "default"}
    ).json()


def configure(client, secret="fixture-secret-value"):
    item = scope(client)
    response = client.put(
        f"/api/secret-scopes/{item['id']}/keys/password", json={"value": secret}
    )
    response.raise_for_status()
    selected = client.put(
        "/api/notebooks/book/secret-refs",
        json={"secret_refs": [{"scope": "warehouse", "key": "password"}], "version": 1},
    )
    selected.raise_for_status()
    return item


def test_values_are_encrypted_and_all_management_responses_are_write_only(client):
    secret = "fixture-unique-password-never-in-metadata"
    item = configure(client, secret)
    metadata = client.get("/api/secret-scopes").json()
    assert metadata["items"][0]["keys"][0]["name"] == "password"
    assert secret not in json.dumps(metadata)
    assert "_value" not in json.dumps(metadata)
    store = client.app.state.store
    assert secret not in json.dumps(store.list("notebook_secret_values"))
    assert secret not in json.dumps(store.list("audit_events"))
    assert secret not in json.dumps(store.get("notebooks", "book"))
    # There is deliberately no value-read endpoint.
    assert (
        client.get(f"/api/secret-scopes/{item['id']}/keys/password").status_code == 405
    )


def test_rotation_and_revocation_apply_at_dispatch_not_from_a_saved_snapshot(client):
    item = configure(client)
    store = client.app.state.store
    notebook = store.get("notebooks", "book")
    assert (
        secrets_for_runtime(store, notebook, owner_id="operator")[0]["value"]
        == "fixture-secret-value"
    )
    client.put(
        f"/api/secret-scopes/{item['id']}/keys/password",
        json={"value": "rotated-fixture-value"},
    ).raise_for_status()
    assert (
        secrets_for_runtime(store, notebook, owner_id="operator")[0]["value"]
        == "rotated-fixture-value"
    )
    client.patch(
        f"/api/secret-scopes/{item['id']}", json={"allowed_roles": ["editor"]}
    ).raise_for_status()
    with pytest.raises(HTTPException) as error:
        secrets_for_runtime(store, notebook, owner_id="operator")
    assert error.value.status_code == 403
    client.delete(f"/api/secret-scopes/{item['id']}/keys/password").raise_for_status()
    with pytest.raises(HTTPException) as error:
        secrets_for_runtime(store, notebook, owner_id="admin")
    assert error.value.status_code == 404


def test_roles_cross_project_selection_and_notebook_cas(client):
    item = configure(client)
    assert (
        client.put(
            f"/api/secret-scopes/{item['id']}/keys/x",
            headers={"x-test-user": "editor"},
            json={"value": "not-allowed"},
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/notebooks/book/secret-refs",
            headers={"x-test-user": "viewer"},
            json={"secret_refs": []},
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/notebooks/book/secret-refs", json={"secret_refs": [], "version": 1}
        ).status_code
        == 409
    )
    store = client.app.state.store
    store.put("projects", {"id": "other", "name": "Private"})
    store.put(
        "notebooks",
        {"id": "otherbook", "name": "Private", "project_id": "other", "version": 1},
    )
    assert (
        client.put(
            "/api/notebooks/otherbook/secret-refs",
            json={"secret_refs": [{"scope": "warehouse", "key": "password"}]},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/secret-scopes?project_id=other", headers={"x-test-user": "editor"}
        ).status_code
        == 403
    )


def test_empty_selection_and_missing_or_removed_run_as_never_get_values(client):
    configure(client)
    store = client.app.state.store
    assert secrets_for_runtime(store, {"secret_refs": []}) == []
    notebook = store.get("notebooks", "book")
    with pytest.raises(HTTPException):
        secrets_for_runtime(store, notebook)
    store.delete("project_members", "default:editor")
    with pytest.raises(HTTPException):
        secrets_for_runtime(store, notebook, owner_id="editor")
    assert (
        client.put(
            "/api/notebooks/book/secret-refs",
            json={
                "secret_refs": [
                    {"scope": "warehouse", "key": "password"},
                    {"scope": "warehouse", "key": "password"},
                ]
            },
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    "value", ["", "x" * 4097, {"sensitive": "nested-value"}, ["sensitive-value"]]
)
def test_validation_errors_never_echo_submitted_values(client, value):
    item = scope(client)
    response = client.put(
        f"/api/secret-scopes/{item['id']}/keys/password", json={"value": value}
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": "Secret request does not match the required fields or limits"
    }


def test_scope_delete_revokes_refs_and_sibling_scopes_survive(client):
    item = configure(client)
    sibling = client.post("/api/secret-scopes", json={"name": "other"}).json()
    client.delete(f"/api/secret-scopes/{item['id']}").raise_for_status()
    assert client.app.state.store.list("notebook_secret_values") == []
    assert client.app.state.store.get("secret_scopes", sibling["id"])


def test_runtime_registry_is_selected_only_and_representations_hide_values():
    values = [
        {"scope": "warehouse", "key": "password", "value": "fixture-hidden-secret"}
    ]
    registry = SecretRegistry(values)
    assert registry.get(scope="warehouse", key="password") == "fixture-hidden-secret"
    assert registry.list("warehouse") == [{"key": "password"}]
    assert registry.listScopes() == [{"name": "warehouse"}]
    assert "fixture-hidden-secret" not in repr(registry)
    with pytest.raises(ValueError):
        registry.get("other", "password")
    with pytest.raises(ValueError):
        SecretRegistry(values * 2)
    with pytest.raises(ValueError):
        SecretRegistry([{**values[0], "value": "x" * 4097}])


def test_stream_keys_nested_errors_and_rotated_history_are_redacted():
    history = RedactionHistory()
    history.include([{"value": "old-fixture-secret"}])
    adapters = history.include([{"value": "new-fixture-secret"}])
    redactor = Redactor(adapters)
    stream = StreamRedactor(redactor)
    output = (
        stream.feed("old-fixture-")
        + stream.feed("secret new-")
        + stream.feed("fixture-secret", final=True)
    )
    assert output == "[REDACTED] [REDACTED]"
    event = redactor.event(
        {
            "type": "cell",
            "run_id": "stable-id",
            "output": {
                "status": "succeeded",
                "cell_id": "c",
                "table": {"old-fixture-secret": ["new-fixture-secret"]},
            },
        }
    )
    assert "old-fixture-secret" not in json.dumps(event)
    assert "new-fixture-secret" not in json.dumps(event)
    assert event["type"] == "cell" and event["output"]["status"] == "succeeded"
    assert redaction_connections([{"value": "v"}]) == [{"secrets": {"value": "v"}}]


def test_history_is_thread_safe_and_overflow_is_fail_closed():
    history = RedactionHistory(max_values=100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: history.include([{"value": str(i)}]), range(100)))
    assert len(history.include([])) == 100
    with pytest.raises(SecretHistoryFull):
        history.include([{"value": "new"}])
    assert len(history.include([])) == 100


@pytest.mark.parametrize(
    "secret", ["output", "stdout", "text", "result", "columns", "status"]
)
def test_short_secret_matches_do_not_rename_wire_schema_or_lose_output(secret):
    redactor = Redactor([{"secrets": {"key": secret}}])
    source = {
        "type": "cell",
        "run_id": "run",
        "output": {
            "cell_id": "cell",
            "status": "succeeded",
            "stdout": secret,
            "result": {"columns": [secret], "rows": [[{secret: secret}]]},
        },
    }
    event = redactor.event(source)
    assert event["output"]["status"] == "succeeded"
    assert event["output"]["stdout"] == "[REDACTED]"
    assert event["output"]["result"]["columns"] == ["[REDACTED]"]
    assert event["output"]["result"]["rows"] == [[{"[REDACTED]": "[REDACTED]"}]]
