"""Stable-release regressions for workspace maintenance and worker recovery."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from openbricks.backup_automation import BackupAutomation
from openbricks.operations import BackupInput, Operations, build_archive
from openbricks.store import Store


@contextmanager
def no_environment_change():
    yield


@pytest.fixture
def maintenance_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_MANAGED_CONTAINER", raising=False)
    store = Store(tmp_path / "workspace")
    scheduler = SimpleNamespace(paused=False)
    scheduler.pause = lambda: setattr(scheduler, "paused", True)
    scheduler.resume = lambda: setattr(scheduler, "paused", False)
    runtime = SimpleNamespace(
        environment_change=no_environment_change, reset=lambda: None, close=lambda: None
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            store=store,
            runtime=runtime,
            workflows=SimpleNamespace(lock=threading.RLock(), close=lambda: None),
            scheduler=scheduler,
            maintenance=None,
            restore_required=False,
        )
    )
    operations = app.state.operations = Operations(app)
    manager = app.state.backup_automation = BackupAutomation(app)
    yield app
    manager.close()
    operations.close()


def test_conflicting_remote_restore_keeps_existing_backup_maintenance(
    maintenance_workspace, tmp_path, monkeypatch
):
    app = maintenance_workspace
    store, operations, manager = (
        app.state.store,
        app.state.operations,
        app.state.backup_automation,
    )
    scheduler = app.state.scheduler
    exporting, finish_export = threading.Event(), threading.Event()

    def exporter(destination):
        exporting.set()
        assert finish_export.wait(timeout=10)
        return {"objects": 0}

    @contextmanager
    def fake_download(*args, **kwargs):
        # restore() rejects the occupied operations lock before opening this.
        yield Path(tmp_path / "unneeded-remote-archive.obbackup")

    monkeypatch.setattr(manager, "downloaded", fake_download)
    backup = operations.backup(BackupInput(name="Existing local backup"), exporter)
    try:
        assert exporting.wait(timeout=5)
        original_message = app.state.maintenance
        assert original_message and scheduler.paused
        with pytest.raises(HTTPException) as rejected:
            manager.restore_remote({"project_id": "default"}, "unused-passphrase")
        assert rejected.value.status_code == 409
        assert app.state.maintenance == original_message
        assert scheduler.paused
    finally:
        finish_export.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if store.get("backups", backup["id"])["status"] != "running":
                break
            time.sleep(0.01)


@pytest.mark.parametrize("already_paused", [False, True])
def test_download_failure_releases_only_its_maintenance_reservation(
    maintenance_workspace, monkeypatch, already_paused
):
    app = maintenance_workspace
    operations, manager = app.state.operations, app.state.backup_automation
    if already_paused:
        manager.pause(wait=False)

    @contextmanager
    def failed_download(*args, **kwargs):
        assert operations.lock.locked()
        assert app.state.maintenance and app.state.scheduler.paused
        raise ValueError("Synthetic transfer failure")
        yield  # pragma: no cover

    monkeypatch.setattr(manager, "downloaded", failed_download)
    with pytest.raises(ValueError, match="Synthetic transfer failure"):
        manager.restore_remote({"project_id": "default"}, "unused-passphrase")
    assert app.state.maintenance is None and not app.state.restore_required
    assert not app.state.scheduler.paused
    assert manager.paused.is_set() is already_paused
    assert operations.lock.acquire(blocking=False)
    operations.lock.release()
    assert manager.external_restore_lock.acquire(blocking=False)
    manager.external_restore_lock.release()


def test_remote_restore_holds_reservation_through_download_and_swap(
    maintenance_workspace, tmp_path, monkeypatch
):
    app = maintenance_workspace
    operations, manager = app.state.operations, app.state.backup_automation
    source = Store(tmp_path / "source")
    source.put("settings", {"id": "restored-marker", "value": "Exact recovery fixture"})
    archive = tmp_path / "workspace.tar.gz"
    build_archive(source, archive)

    @contextmanager
    def downloaded(*args, **kwargs):
        assert operations.lock.locked() and app.state.scheduler.paused
        with pytest.raises(HTTPException) as conflict:
            operations.backup(BackupInput(name="Overlapping backup"))
        assert conflict.value.status_code == 409
        yield archive

    monkeypatch.setattr(manager, "downloaded", downloaded)
    result = manager.restore_remote({"project_id": "default"}, "")
    assert result["restart_required"] and app.state.restore_required
    assert app.state.maintenance and app.state.scheduler.paused
    assert (
        app.state.store.get("settings", "restored-marker")["value"]
        == "Exact recovery fixture"
    )
    assert manager.stop_event.is_set()
    assert operations.lock.acquire(blocking=False)
    operations.lock.release()
    assert manager.external_restore_lock.acquire(blocking=False)
    manager.external_restore_lock.release()


def test_remote_restore_releases_locks_even_when_scheduler_resume_fails(
    maintenance_workspace, monkeypatch
):
    app = maintenance_workspace
    operations, manager = app.state.operations, app.state.backup_automation

    @contextmanager
    def failed_download(*args, **kwargs):
        raise ValueError("Synthetic transfer failure")
        yield  # pragma: no cover

    def failed_resume():
        raise RuntimeError("Synthetic stopped scheduler")

    monkeypatch.setattr(manager, "downloaded", failed_download)
    monkeypatch.setattr(app.state.scheduler, "resume", failed_resume)
    with pytest.raises(RuntimeError, match="Synthetic stopped scheduler"):
        manager.restore_remote({"project_id": "default"}, "unused-passphrase")
    assert not manager.paused.is_set()
    assert operations.lock.acquire(blocking=False)
    operations.lock.release()
    assert manager.external_restore_lock.acquire(blocking=False)
    manager.external_restore_lock.release()


def test_remote_restore_api_rejects_occupied_lock_without_touching_state(
    tmp_path, monkeypatch
):
    from openbricks.main import create_app

    for key in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
        "OPENBRICKS_MANAGED_CONTAINER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "OPENBRICKS_ADMIN_PASSWORD", "stable-maintenance-fixture-password"
    )
    with TestClient(create_app(tmp_path / "api")) as client:
        client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": "stable-maintenance-fixture-password",
            },
        ).raise_for_status()
        state = client.app.state
        state.store.put(
            "connections", {"id": "fixture", "type": "s3", "project_ids": ["default"]}
        )
        assert state.operations.lock.acquire(blocking=False)
        try:
            response = client.post(
                "/api/operations/remote-backups/restore",
                json={
                    "connection_id": "fixture",
                    "bucket": "backups",
                    "key": "archive.obbackup",
                    "passphrase": "unused-passphrase",
                },
            )
            assert response.status_code == 409
            assert state.maintenance is None and not state.restore_required
            assert state.operations.lock.locked()
            assert not state.backup_automation.paused.is_set()
        finally:
            state.operations.lock.release()


def test_startup_terminalizes_interrupted_local_backups_and_preserves_artifacts(
    tmp_path,
):
    store = Store(tmp_path / "workspace")
    artifacts = store.directory / "backups"
    artifacts.mkdir()
    original = {}
    for status in ("running", "queued", "succeeded", "failed", "cancelled"):
        artifact = artifacts / (status + ".obbackup")
        artifact.write_bytes(("Original " + status + " bytes").encode())
        record = {
            "id": status,
            "name": "Preserved fixture " + status,
            "status": status,
            "created_at": "2026-01-01T00:00:00+00:00",
            "_path": str(artifact),
            "size": artifact.stat().st_size,
            "encrypted": True,
            "includes": ["workspace"],
        }
        if status not in ("running", "queued"):
            record.update(finished_at="2026-01-01T01:00:00+00:00", error=None)
        original[status] = store.put("backups", record)
    app = SimpleNamespace(state=SimpleNamespace(store=store))
    operations = Operations(app)
    recovered = {item["id"]: item for item in store.list("backups")}
    operations.close()
    for status, prior in original.items():
        item = recovered[status]
        if status in ("running", "queued"):
            assert item["status"] == "failed"
            assert item["finished_at"] and "restarted" in item["error"]
            assert {
                key: value
                for key, value in item.items()
                if key not in ("status", "finished_at", "error")
            } == {
                key: value
                for key, value in prior.items()
                if key not in ("status", "finished_at", "error")
            }
        else:
            assert item == prior
        assert (
            Path(item["_path"]).read_bytes()
            == ("Original " + status + " bytes").encode()
        )
    restarted = Operations(app)
    try:
        assert {item["id"]: item for item in store.list("backups")} == recovered
    finally:
        restarted.close()


def test_offsite_restart_drops_failed_local_archive_reservation(
    maintenance_workspace, monkeypatch
):
    from openbricks.credentials import SecretBox
    from openbricks.permissions import now

    app = maintenance_workspace
    app.state.store.put("backups", {"id": "interrupted", "status": "failed"})
    app.state.store.put(
        "backup_runs",
        {
            "id": "retry",
            "status": "running",
            "_backup_id": "interrupted",
            "created_at": now(),
        },
    )
    recovered = BackupAutomation(app)
    try:
        item = app.state.store.get("backup_runs", "retry")
        assert item["status"] == "queued" and item["recovered_at"]
        assert "_backup_id" not in item
        policy = {
            "_passphrase": SecretBox(app.state.store.directory).encrypt("fixture")
        }
        monkeypatch.setattr(recovered, "_policy_and_actor", lambda item: (policy, None))

        def workspace_busy():
            raise HTTPException(409, "Synthetic active notebook")

        monkeypatch.setattr(app.state.operations, "require_idle", workspace_busy)
        recovered._process(item)
        waiting = app.state.store.get("backup_runs", "retry")
        assert waiting["status"] == "waiting" and waiting["_next_attempt_at"]
        assert waiting["queued_reason"] == "Waiting for an idle workspace"
    finally:
        recovered.close()


def test_parallel_environment_publication_reuses_the_completed_matching_cache(
    tmp_path, monkeypatch
):
    from openbricks.environments import prepare_environment, validate_manifest

    manifest = validate_manifest({"wheels": []})
    root = tmp_path / "environments" / manifest["id"]
    exists = Path.exists
    raced = False

    def publish_between_check_and_rename(path):
        nonlocal raced
        if path == root and not raced:
            raced = True
            (root / "site-packages").mkdir(parents=True)
            (root / "ready.json").write_text(json.dumps(manifest), encoding="utf-8")
            return False
        return exists(path)

    monkeypatch.setattr(Path, "exists", publish_between_check_and_rename)
    assert prepare_environment(tmp_path, manifest) == root / "site-packages"
    assert raced
    assert not list(root.parent.glob(root.name + ".*.prepare"))


def test_parallel_environment_publication_rejects_mismatched_completed_cache(
    tmp_path, monkeypatch
):
    from openbricks.environments import prepare_environment, validate_manifest

    manifest = validate_manifest({"wheels": []})
    root = tmp_path / "environments" / manifest["id"]
    exists = Path.exists
    raced = False

    def incompatible_publish(path):
        nonlocal raced
        if path == root and not raced:
            raced = True
            (root / "site-packages").mkdir(parents=True)
            (root / "ready.json").write_text(
                json.dumps({**manifest, "unexpected": True}), encoding="utf-8"
            )
            return False
        return exists(path)

    monkeypatch.setattr(Path, "exists", incompatible_publish)
    with pytest.raises(ValueError, match="cache is incomplete or inconsistent"):
        prepare_environment(tmp_path, manifest)
    assert raced and json.loads((root / "ready.json").read_text())["unexpected"]
    assert not list(root.parent.glob(root.name + ".*.prepare"))


def test_environment_publication_does_not_hide_permission_errors(tmp_path, monkeypatch):
    from openbricks.environments import prepare_environment, validate_manifest
    import errno

    manifest = validate_manifest({"wheels": []})
    root = tmp_path / "environments" / manifest["id"]
    replace = Path.replace

    def denied(path, target):
        if target == root:
            raise PermissionError(errno.EACCES, "Synthetic publication denial")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(PermissionError, match="Synthetic publication denial"):
        prepare_environment(tmp_path, manifest)
    assert not root.exists()
    assert not list(root.parent.glob(root.name + ".*.prepare"))


def test_completed_environment_rejects_a_symlinked_ready_marker(tmp_path):
    from openbricks.environments import prepare_environment, validate_manifest

    manifest = validate_manifest({"wheels": []})
    root = tmp_path / "environments" / manifest["id"]
    (root / "site-packages").mkdir(parents=True)
    other = tmp_path / "borrowed-ready.json"
    other.write_text(json.dumps(manifest), encoding="utf-8")
    try:
        (root / "ready.json").symlink_to(other)
    except OSError:
        pytest.skip("Symlinks unavailable for this user")
    with pytest.raises(ValueError, match="cache is incomplete or inconsistent"):
        prepare_environment(tmp_path, manifest)
    assert other.is_file() and json.loads(other.read_text()) == manifest
