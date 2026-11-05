from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from openbricks import backup_automation as module
from openbricks.backup_automation import (
    BackupAutomation,
    BackupPolicyInput,
    public,
    register_backup_automation_routes,
    save_policy,
)
from openbricks.credentials import SecretBox
from openbricks.operations import build_archive, crypt_file, extract_archive
from openbricks.permissions import Access, now
from openbricks.store import Store

PHRASE = "offsite-backup-test-phrase-12345"


class Missing(Exception):
    response = {"Error": {"Code": "404"}}


class Conflict(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class Storage:
    def __init__(self):
        self.objects, self.deleted, self.uploaded = {}, [], []
        self.before_upload = lambda: None

    def head_object(self, Bucket, Key):
        item = self.objects.get((Bucket, Key))
        if not item:
            raise Missing()
        return {
            "ContentLength": len(item["data"]),
            "Metadata": item.get("metadata", {}),
            "ETag": "fixture-version",
        }

    def put_object(self, Bucket, Key, Body, **kwargs):
        assert kwargs.get("IfNoneMatch") == "*"
        self.before_upload()
        if (Bucket, Key) in self.objects:
            raise Conflict()
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[Bucket, Key] = {
            "data": data,
            "metadata": kwargs.get("Metadata", {}),
        }
        self.uploaded.append((Bucket, Key))

    def get_object(self, Bucket, Key, **kwargs):
        return {"Body": io.BytesIO(self.objects[Bucket, Key]["data"])}

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)

    def close(self):
        pass


class Operations:
    def __init__(self, app):
        self.app, self.store = app, app.state.store
        self.lock = threading.Lock()
        self.busy = False

    def require_idle(self):
        if self.busy:
            raise HTTPException(409, "Work is active")

    def begin_maintenance(self, message):
        self.app.state.maintenance = message

    def backup(self, body, exporter=None):
        root = self.store.directory / "backups"
        identifier = str(len(self.store.list("backups")))
        raw, final = root / f"{identifier}.tar.gz", root / f"{identifier}.obbackup"
        attachments = root / f"attachments-{identifier}"
        attachments.mkdir()
        if exporter:
            exporter(attachments)
        build_archive(
            self.store, raw, body.include_data, attachments if exporter else None
        )
        crypt_file(raw, final, body.passphrase)
        item = {
            "id": identifier,
            "status": "succeeded",
            "created_at": now(),
            "_path": str(final),
            "encrypted": True,
        }
        self.store.put("backups", item)
        return public(item)

    def restore(self, path, phrase):
        extract_archive(path, self.store.directory / "restored-workspace", phrase)
        self.app.state.restore_required = True
        return {"ok": True, "restart_required": True}

    def _restore_locked(self, path, phrase):
        assert self.lock.locked()
        return self.restore(path, phrase)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    (store.directory / "backups").mkdir()
    admin = {"id": "admin", "username": "admin", "role": "admin"}
    member = {"id": "member", "username": "member", "role": "member"}
    store.put("users", admin)
    store.put("users", member)
    access = Access(store)
    access.seed()
    box = SecretBox(store.directory)
    store.put(
        "connections",
        {
            "id": "remote",
            "type": "s3",
            "name": "Remote",
            "project_ids": ["default"],
            "config": {},
            "_secrets": box.encrypt(
                json.dumps({"access_key": "fixture", "secret_key": "fixture-secret"})
            ),
        },
    )
    storage = Storage()
    monkeypatch.setattr(module, "s3_client", lambda *args: storage)
    app = FastAPI()
    app.state.store, app.state.maintenance, app.state.restore_required = (
        store,
        None,
        False,
    )
    app.state.operations = Operations(app)
    app.state.scheduler = SimpleNamespace(pause=lambda: None, resume=lambda: None)
    events = []
    app.state.notifications = SimpleNamespace(
        lock=threading.RLock(), _enqueue=lambda *args: events.append(args)
    )
    manager = BackupAutomation(app)
    monkeypatch.setattr(
        manager,
        "drill",
        lambda path, phrase: {
            "passed": True,
            "management_boot": True,
            "authentication": True,
        },
    )
    app.state.backup_automation = manager

    def authenticated(request: Request):
        role = request.headers.get("x-role")
        if not role:
            raise HTTPException(401, "Login required")
        return admin if role == "admin" else member

    def administrator(user=Depends(authenticated)):
        if user["role"] != "admin":
            raise HTTPException(403, "Administrator required")
        return user

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    register_backup_automation_routes(app, authenticated, administrator, access)
    try:
        yield SimpleNamespace(
            store=store,
            app=app,
            manager=manager,
            access=access,
            admin=admin,
            storage=storage,
            events=events,
        )
    finally:
        manager.close()


def policy(workspace, **overrides):
    return save_policy(
        workspace.store,
        workspace.access,
        workspace.admin,
        BackupPolicyInput.model_validate(
            {
                "name": "Nightly",
                "connection_id": "remote",
                "bucket": "backups",
                "passphrase": PHRASE,
                **overrides,
            }
        ),
    )


def test_policy_secrets_blank_update_and_admin_api_gates(workspace):
    with TestClient(workspace.app) as client:
        body = {
            "name": "Nightly",
            "connection_id": "remote",
            "bucket": "backups",
            "passphrase": PHRASE,
        }
        assert (
            client.post("/api/operations/backup-policies", json=body).status_code == 401
        )
        assert (
            client.post(
                "/api/operations/backup-policies",
                headers={"x-role": "member"},
                json=body,
            ).status_code
            == 403
        )
        response = client.post(
            "/api/operations/backup-policies", headers={"x-role": "admin"}, json=body
        )
        assert response.status_code == 201, response.text
        item = response.json()
        assert (
            item["has_passphrase"]
            and PHRASE not in response.text
            and "_passphrase" not in item
        )
        stored = workspace.store.get("backup_policies", item["id"])
        assert PHRASE not in stored["_passphrase"]
        body["passphrase"] = ""
        assert (
            client.put(
                f"/api/operations/backup-policies/{item['id']}",
                headers={"x-role": "admin"},
                json=body,
            ).status_code
            == 200
        )
        assert (
            workspace.store.get("backup_policies", item["id"])["_passphrase"]
            == stored["_passphrase"]
        )


def test_encrypted_upload_hash_receipt_restore_drill_and_repeat_are_safe(workspace):
    saved = policy(workspace)
    run = workspace.manager.enqueue(saved, workspace.admin)
    workspace.manager.poll_once()
    result = workspace.store.get("backup_runs", run["id"])
    assert result["status"] == "succeeded", result
    assert result["restore_drill"]["passed"] and result["remote"]["verified"]
    receipt = result["remote"]
    remote = workspace.storage.objects[receipt["bucket"], receipt["key"]]
    assert PHRASE.encode() not in remote["data"] and remote["data"].startswith(
        b"OBBK1\n"
    )
    assert hashlib.sha256(remote["data"]).hexdigest() == receipt["sha256"]
    assert (
        workspace.storage.head_object(Bucket=receipt["bucket"], Key=receipt["key"])[
            "Metadata"
        ]["openbricks-sha256"]
        == receipt["sha256"]
    )
    path = workspace.manager._archive(result["_backup_id"])
    workspace.manager._upload(result, path, saved)
    assert (
        len(workspace.storage.uploaded) == 2
    )  # One archive and its immutable receipt.
    assert not workspace.store.get("backups", result["_backup_id"]).get(
        "_offsite_uploading"
    )


def test_schedule_intent_precedes_cursor_and_restarts_do_not_duplicate(
    workspace, monkeypatch
):
    saved = policy(workspace)
    saved["next_fire_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat()
    workspace.store.put("backup_policies", saved)
    original = workspace.store.put
    calls = [0]

    def crash(kind, value):
        if kind == "backup_policies" and calls[0] == 0:
            calls[0] += 1
            raise RuntimeError("Simulated interruption before cursor commit")
        return original(kind, value)

    monkeypatch.setattr(workspace.store, "put", crash)
    with pytest.raises(RuntimeError):
        workspace.manager._schedule()
    assert len(workspace.store.list("backup_runs")) == 1
    workspace.manager._schedule()
    assert len(workspace.store.list("backup_runs")) == 1
    assert workspace.store.get("backup_policies", saved["id"])["next_fire_at"] > now()


def test_busy_workspace_waits_without_creating_or_uploading_archive(workspace):
    workspace.app.state.operations.busy = True
    run = workspace.manager.enqueue(policy(workspace), workspace.admin)
    workspace.manager.poll_once()
    assert workspace.store.get("backup_runs", run["id"])["status"] == "waiting"
    assert workspace.store.list("backups") == [] and workspace.storage.uploaded == []


def test_pause_interrupts_transfer_and_drains_before_db_replacement(workspace):
    began = threading.Event()

    def on_upload():
        began.set()
        assert workspace.manager.paused.wait(3)

    workspace.storage.before_upload = on_upload
    run = workspace.manager.enqueue(policy(workspace), workspace.admin)
    worker = threading.Thread(target=workspace.manager.poll_once)
    worker.start()
    assert began.wait(5)
    workspace.manager.pause(wait=True)
    worker.join(2)
    assert not worker.is_alive() and not workspace.manager.active
    assert workspace.store.get("backup_runs", run["id"])["status"] == "waiting"
    assert workspace.storage.uploaded == []
    workspace.manager.resume()


def test_retention_deletes_only_recorded_owned_keys_after_verified_success(workspace):
    saved = policy(workspace, keep_remote=1, verify_restore=False)
    workspace.storage.objects["backups", "someone-elses/object"] = {
        "data": b"unrelated"
    }
    for _ in range(3):
        workspace.manager.enqueue(saved, workspace.admin)
        workspace.manager.poll_once()
    runs = workspace.store.list("backup_runs")
    assert all(item["status"] == "succeeded" for item in runs)
    assert sum(bool(item.get("remote_expired")) for item in runs) == 2
    assert ("backups", "someone-elses/object") in workspace.storage.objects
    assert (
        len(
            [
                key
                for bucket, key in workspace.storage.objects
                if key.endswith(".obbackup")
            ]
        )
        == 1
    )


def test_remote_key_collision_never_overwrites_existing_data(workspace):
    saved = policy(workspace)
    run = workspace.manager.enqueue(saved, workspace.admin)
    key = f"{saved['prefix']}/openbricks/{workspace.manager.instance_id}/{saved['id']}/{run['id']}.obbackup"
    workspace.storage.objects["backups", key] = {
        "data": b"do-not-overwrite",
        "metadata": {"openbricks-instance": "other"},
    }
    workspace.manager.poll_once()
    assert workspace.store.get("backup_runs", run["id"])["status"] == "failed"
    assert workspace.storage.objects["backups", key]["data"] == b"do-not-overwrite"
    assert workspace.storage.uploaded == []


def test_fresh_host_restore_streams_from_storage_and_rejects_bad_passphrase(workspace):
    saved = policy(workspace)
    run = workspace.manager.enqueue(saved, workspace.admin)
    workspace.manager.poll_once()
    receipt = workspace.store.get("backup_runs", run["id"])["remote"]
    workspace.store.delete("backup_runs", run["id"])
    with TestClient(workspace.app) as client:
        body = {
            "project_id": "default",
            "connection_id": "remote",
            "bucket": receipt["bucket"],
            "key": receipt["key"],
            "passphrase": "incorrect-passphrase-value",
        }
        assert (
            client.post(
                "/api/operations/remote-backups/restore",
                headers={"x-role": "admin"},
                json=body,
            ).status_code
            == 400
        )
        assert (
            workspace.app.state.maintenance is None
            and not workspace.manager.paused.is_set()
        )
        body["passphrase"] = PHRASE
        response = client.post(
            "/api/operations/remote-backups/restore",
            headers={"x-role": "admin"},
            json=body,
        )
        assert response.status_code == 200, response.text
        assert (
            response.json()["restart_required"] and workspace.app.state.restore_required
        )
        assert workspace.manager.paused.is_set()


def test_config_revocation_and_failures_do_not_expose_credentials(workspace):
    saved = policy(workspace)
    run = workspace.manager.enqueue(saved, workspace.admin)
    connection = workspace.store.get("connections", "remote")
    connection["project_ids"] = []
    workspace.store.put("connections", connection)
    workspace.manager.poll_once()
    result = workspace.store.get("backup_runs", run["id"])
    assert result["status"] == "failed" and PHRASE not in json.dumps(public(result))
    assert workspace.storage.uploaded == []


def test_source_aliases_cannot_capture_the_backup_destination(workspace):
    original = workspace.store.get("connections", "remote")
    original["config"] = {"endpoint_url": "https://OBJECTS.example.test:443/"}
    workspace.store.put("connections", original)
    workspace.store.put(
        "connections",
        {
            **original,
            "id": "alias",
            "config": {"endpoint_url": "https://objects.example.test"},
        },
    )
    with pytest.raises(ValueError, match="outside"):
        policy(
            workspace,
            storage_sources=[
                {"connection_id": "alias", "bucket": "backups", "prefix": ""}
            ],
        )


def test_overlapping_storage_sources_are_rejected_across_aliases(workspace):
    original = workspace.store.get("connections", "remote")
    workspace.store.put("connections", {**original, "id": "alias"})
    with pytest.raises(ValueError, match="must not overlap"):
        policy(
            workspace,
            storage_sources=[
                {
                    "connection_id": "remote",
                    "bucket": "source-data",
                    "prefix": "tables/",
                },
                {
                    "connection_id": "alias",
                    "bucket": "source-data",
                    "prefix": "tables/events",
                },
            ],
        )


def test_conditional_upload_rejects_a_destination_created_after_head(workspace):
    saved = policy(workspace)
    run = workspace.manager.enqueue(saved, workspace.admin)
    key = f"{saved['prefix']}/openbricks/{workspace.manager.instance_id}/{saved['id']}/{run['id']}.obbackup"

    def concurrent_writer():
        workspace.storage.objects["backups", key] = {"data": b"concurrent-owner-data"}

    workspace.storage.before_upload = concurrent_writer
    workspace.manager.poll_once()
    assert workspace.store.get("backup_runs", run["id"])["status"] == "failed"
    assert workspace.storage.objects["backups", key]["data"] == b"concurrent-owner-data"


def test_backup_mutations_are_blocked_during_maintenance_and_after_restore(workspace):
    with TestClient(workspace.app) as client:
        body = {
            "name": "Nightly",
            "connection_id": "remote",
            "bucket": "backups",
            "passphrase": PHRASE,
        }
        workspace.app.state.maintenance = "Restoring"
        assert (
            client.post(
                "/api/operations/backup-policies",
                headers={"x-role": "admin"},
                json=body,
            ).status_code
            == 409
        )
        workspace.app.state.maintenance = None
        workspace.app.state.restore_required = True
        assert (
            client.post(
                "/api/operations/backup-policies",
                headers={"x-role": "admin"},
                json=body,
            ).status_code
            == 503
        )
        assert workspace.store.list("backup_policies") == []


def test_full_admission_queue_still_drains_before_scheduled_cursor_advances(workspace):
    saved = policy(workspace, verify_restore=False)
    saved["next_fire_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat()
    workspace.store.put("backup_policies", saved)
    for _ in range(100):
        workspace.manager.enqueue(saved, workspace.admin)
    workspace.manager.poll_once()
    assert (
        sum(
            item["status"] == "succeeded"
            for item in workspace.store.list("backup_runs")
        )
        == 1
    )
    assert (
        workspace.store.get("backup_policies", saved["id"])["next_fire_at"]
        == saved["next_fire_at"]
    )


def test_restart_requeues_an_interrupted_remote_restore_drill(workspace):
    saved = policy(workspace)
    run = workspace.manager.enqueue(saved, workspace.admin)
    run.update(status="succeeded", verification_status="running")
    workspace.store.put("backup_runs", run)
    recovered = BackupAutomation(workspace.app)
    try:
        item = workspace.store.get("backup_runs", run["id"])
        assert item["status"] == "succeeded" and item["verification_status"] == "queued"
    finally:
        recovered.close()


def test_known_backup_restore_requires_the_recorded_checksum(workspace):
    saved = policy(workspace)
    run = workspace.manager.enqueue(saved, workspace.admin)
    workspace.manager.poll_once()
    receipt = workspace.store.get("backup_runs", run["id"])["remote"]
    stored = workspace.storage.objects[receipt["bucket"], receipt["key"]]
    stored["data"] = b"a replacement object with different content"
    stored["metadata"]["openbricks-sha256"] = hashlib.sha256(stored["data"]).hexdigest()
    with TestClient(workspace.app) as client:
        response = client.post(
            f"/api/operations/backup-runs/{run['id']}/restore",
            headers={"x-role": "admin"},
            json={"passphrase": PHRASE},
        )
        assert response.status_code == 400 and "checksum" in response.text
        assert (
            not workspace.app.state.restore_required
            and workspace.app.state.maintenance is None
        )
