from concurrent.futures import ThreadPoolExecutor
import copy

import pytest
from fastapi.testclient import TestClient

from openbricks.collaboration import source_hash
from openbricks.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "collaboration-fixture-password")
    for key in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
        "OPENBRICKS_MANAGED_CONTAINER",
    ):
        monkeypatch.delenv(key, raising=False)
    with TestClient(create_app(tmp_path)) as client:
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "collaboration-fixture-password"},
        ).raise_for_status()
        yield client


def teammate(client, username="editor"):
    account = client.post(
        "/api/users",
        json={"username": username, "password": "collaboration-peer-password"},
    ).json()
    peer = TestClient(client.app)
    peer.post(
        "/api/auth/login",
        json={"username": username, "password": "collaboration-peer-password"},
    ).raise_for_status()
    return peer, account


def notebook(client):
    r = client.post(
        "/api/notebooks",
        json={
            "name": "Collaborative notebook",
            "cells": [
                {"id": "first", "language": "python", "source": "a = 1"},
                {"id": "second", "language": "python", "source": "b = 2"},
            ],
        },
    )
    r.raise_for_status()
    return r.json()


def enable(client, book):
    result = client.put(
        f"/api/notebooks/{book['id']}/collaboration",
        json={"enabled": True, "version": book["version"]},
    )
    result.raise_for_status()
    return result.json()["notebook"]


def lease(client, book, cell, client_id):
    r = client.post(
        f"/api/notebooks/{book['id']}/collaboration/lease",
        json={"client_id": client_id, "cell_id": cell},
    )
    r.raise_for_status()
    return {
        "client_id": client_id,
        "cell_id": cell,
        "token": r.json()["token"],
        "base_hash": r.json()["source_hash"],
    }


def test_two_users_can_edit_separate_cells_without_lost_updates(client):
    peer, _ = teammate(client)
    value = enable(client, notebook(client))
    one, two = lease(client, value, "first", "client-one"), lease(
        peer, value, "second", "client-two"
    )
    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(
                c.patch,
                f"/api/notebooks/{value['id']}/collaboration/cell",
                json={**body, "source": text},
            )
            for c, body, text in ((client, one, "a = 100"), (peer, two, "b = 200"))
        ]
        for future in futures:
            future.result().raise_for_status()
    saved = client.get(f"/api/notebooks/{value['id']}").json()
    assert [c["source"] for c in saved["cells"]] == ["a = 100", "b = 200"]
    assert saved["version"] == value["version"] + 2
    peer.close()


def test_exclusive_leases_stale_hash_and_expiry_fail_closed(client):
    peer, _ = teammate(client)
    value = enable(client, notebook(client))
    body = lease(client, value, "first", "client-one")
    assert (
        peer.post(
            f"/api/notebooks/{value['id']}/collaboration/lease",
            json={"client_id": "client-two", "cell_id": "first"},
        ).status_code
        == 409
    )
    assert (
        peer.patch(
            f"/api/notebooks/{value['id']}/collaboration/cell",
            json={**body, "source": "stolen"},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/notebooks/{value['id']}/collaboration/cell",
            json={**body, "source": "wrong", "base_hash": "0" * 64},
        ).status_code
        == 409
    )
    service = client.app.state.collaboration
    current = service.clock()
    service.clock = lambda: current + 31
    replacement = lease(peer, value, "first", "client-two")
    assert replacement["token"] != body["token"]
    assert (
        client.patch(
            f"/api/notebooks/{value['id']}/collaboration/cell",
            json={**body, "source": "expired"},
        ).status_code
        == 409
    )
    assert (
        client.get(f"/api/notebooks/{value['id']}").json()["cells"][0]["source"]
        == "a = 1"
    )
    peer.close()


def test_legacy_saves_and_other_source_writers_cannot_overwrite_live_edits(client):
    value = enable(client, notebook(client))
    assert (
        client.put(
            f"/api/notebooks/{value['id']}",
            json={"name": value["name"], "cells": value["cells"]},
        ).status_code
        == 409
    )
    changed = copy.deepcopy(value)
    changed["cells"][0]["source"] = "overwrite"
    with pytest.raises(ValueError, match="Live collaboration"):
        client.app.state.store.put("notebooks", changed)
    assert (
        client.get(f"/api/notebooks/{value['id']}").json()["cells"][0]["source"]
        == "a = 1"
    )
    assert client.delete(f"/api/notebooks/{value['id']}").status_code == 409


def test_presence_is_bounded_ephemeral_and_does_not_expose_lease_tokens(client):
    value = enable(client, notebook(client))
    body = lease(client, value, "first", "client-one")
    client.post(
        f"/api/notebooks/{value['id']}/presence",
        json={"client_id": "client-one", "cell_id": "first"},
    ).raise_for_status()
    room = client.get(
        f"/api/notebooks/{value['id']}/collaboration?known_version={value['version']}"
    )
    assert room.json()["notebook"] is None and len(room.json()["presence"]) == 1
    assert body["token"] not in room.text
    for index in range(19):
        client.post(
            f"/api/notebooks/{value['id']}/presence",
            json={"client_id": f"client-extra-{index}"},
        ).raise_for_status()
    assert (
        client.post(
            f"/api/notebooks/{value['id']}/presence",
            json={"client_id": "client-too-many"},
        ).status_code
        == 429
    )
    service = client.app.state.collaboration
    current = service.clock()
    service.clock = lambda: current + 46
    assert (
        client.get(f"/api/notebooks/{value['id']}/collaboration").json()["presence"]
        == []
    )


def test_mode_change_requires_latest_version_and_finished_edits(client):
    value = enable(client, notebook(client))
    body = lease(client, value, "first", "client-one")
    assert (
        client.put(
            f"/api/notebooks/{value['id']}/collaboration",
            json={"enabled": False, "version": value["version"]},
        ).status_code
        == 409
    )
    client.post(
        f"/api/notebooks/{value['id']}/collaboration/release",
        json={**body, "source": "a = 1"},
    ).raise_for_status()
    client.put(
        f"/api/notebooks/{value['id']}/collaboration",
        json={"enabled": False, "version": value["version"]},
    ).raise_for_status()
    assert (
        client.put(
            f"/api/notebooks/{value['id']}/collaboration",
            json={"enabled": True, "version": value["version"]},
        ).status_code
        == 409
    )


def test_threads_authorship_resolve_and_project_transfer(client):
    peer, _ = teammate(client)
    value = notebook(client)
    root = client.post(
        f"/api/notebooks/{value['id']}/comments",
        json={"text": "Review this cell", "cell_id": "first"},
    ).json()
    peer.post(
        f"/api/notebooks/{value['id']}/comments",
        json={"text": "Reply", "parent_id": root["id"]},
    ).raise_for_status()
    assert (
        peer.patch(
            f"/api/notebooks/{value['id']}/comments/{root['id']}",
            json={"text": "impersonation"},
        ).status_code
        == 403
    )
    peer.patch(
        f"/api/notebooks/{value['id']}/comments/{root['id']}", json={"resolved": True}
    ).raise_for_status()
    page = client.get(f"/api/notebooks/{value['id']}/comments?limit=1").json()
    assert page["total"] == 2 and len(page["items"]) == 1
    project = client.post("/api/projects", json={"name": "Private project"}).json()
    client.patch(
        f"/api/notebooks/{value['id']}/location", json={"project_id": project["id"]}
    ).raise_for_status()
    assert client.get(f"/api/notebooks/{value['id']}/comments").json()["total"] == 0
    assert peer.get(f"/api/notebooks/{value['id']}/comments").status_code == 403
    assert (
        client.post(
            f"/api/notebooks/{value['id']}/comments",
            json={"text": "No leaked old thread", "parent_id": root["id"]},
        ).status_code
        == 404
    )
    peer.close()


def test_reviews_pin_source_reject_self_approval_and_become_stale(client):
    peer, account = teammate(client)
    value = notebook(client)
    endpoint = f"/api/notebooks/{value['id']}/reviews"
    review = client.post(
        endpoint, json={"reviewer_id": account["id"], "message": "Please review"}
    ).json()
    assert (
        client.post(
            endpoint + f"/{review['id']}/decision", json={"decision": "approved"}
        ).status_code
        == 403
    )
    peer.post(
        endpoint + f"/{review['id']}/decision", json={"decision": "approved"}
    ).raise_for_status()
    assert (
        peer.post(
            endpoint + f"/{review['id']}/decision", json={"decision": "approved"}
        ).status_code
        == 409
    )
    fresh = client.post(endpoint, json={"reviewer_id": account["id"]}).json()
    client.put(
        f"/api/notebooks/{value['id']}",
        json={"name": value["name"], "cells": [{"id": "first", "source": "a = 3"}]},
    ).raise_for_status()
    assert (
        peer.post(
            endpoint + f"/{fresh['id']}/decision", json={"decision": "approved"}
        ).status_code
        == 409
    )
    assert all(row["stale"] for row in peer.get(endpoint).json()["items"])
    assert peer.post(endpoint, json={"reviewer_id": account["id"]}).status_code == 400
    peer.close()


def test_audit_requires_project_owner_and_csv_escapes_formulas(client):
    peer, _ = teammate(client)
    assert peer.get("/api/audit?project_id=default").status_code == 403
    store = client.app.state.store
    administrator = client.get("/api/auth/me").json()
    raw = store.get("users", administrator["id"])
    raw["username"] = "=SUM(1,1)"
    store.put("users", raw)
    client.app.state.access.audit(raw, "fixture.audit", "default", "resource")
    data = client.get("/api/audit?project_id=default&action=fixture.audit").json()
    assert data["total"] == 1 and data["items"][0]["actor_name"] == "=SUM(1,1)"
    exported = client.get("/api/audit/export?project_id=default&action=fixture.audit")
    assert exported.status_code == 200 and "'=SUM" in exported.text
    assert exported.headers["x-openbricks-export-truncated"] == "false"
    peer.close()


def test_revoked_editor_cannot_keep_cell_leased_by_heartbeating(client):
    peer, account = teammate(client)
    value = enable(client, notebook(client))
    lease(peer, value, "first", "client-two")
    client.put(
        f"/api/projects/default/members/{account['id']}", json={"role": "viewer"}
    ).raise_for_status()
    peer.post(
        f"/api/notebooks/{value['id']}/presence", json={"client_id": "client-two"}
    ).raise_for_status()
    replacement = lease(client, value, "first", "client-one")
    assert replacement["token"]
    peer.close()


def test_metadata_replacement_cannot_implicitly_disable_collaboration(client):
    value = enable(client, notebook(client))
    omitted = copy.deepcopy(value)
    omitted.pop("collaboration_enabled")
    saved = client.app.state.store.put("notebooks", omitted)
    assert saved["collaboration_enabled"] is True
    explicit = {**saved, "collaboration_enabled": False}
    with pytest.raises(ValueError, match="mode endpoint"):
        client.app.state.store.put("notebooks", explicit)


def test_total_source_limit_is_checked_before_source_persistence(client):
    value = notebook(client)
    oversized = [
        {"id": str(i), "language": "python", "source": "x" * 999_999} for i in range(11)
    ]
    with pytest.raises(ValueError, match="10 MB"):
        client.app.state.store.put("notebooks", {**value, "cells": oversized})
    assert (
        client.get(f"/api/notebooks/{value['id']}").json()["cells"][0]["source"]
        == "a = 1"
    )


def test_project_transfer_clears_secret_aliases_to_prevent_unintended_rebinding(client):
    value = notebook(client)
    value["secret_refs"] = [{"scope": "same-name-in-two-projects", "key": "token"}]
    client.app.state.store.put("notebooks", value)
    target = client.post(
        "/api/projects", json={"name": "Secret transfer target"}
    ).json()
    result = client.patch(
        f"/api/notebooks/{value['id']}/location", json={"project_id": target["id"]}
    )
    result.raise_for_status()
    assert result.json()["secret_refs"] == []


def test_admin_audit_retains_visibility_of_deleted_project_actions(client):
    actor = client.get("/api/auth/me").json()
    client.app.state.access.audit(
        actor, "project.deleted.fixture", "deleted-project", "deleted-resource"
    )
    all_rows = client.get("/api/audit?action=project.deleted.fixture").json()
    assert all_rows["total"] == 1
    assert (
        client.get("/api/audit?project_id=deleted-project").json()["items"][0][
            "resource_id"
        ]
        == "deleted-resource"
    )
