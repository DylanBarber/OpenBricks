"""Disposable SQLite/filesystem checks; never operate a live workspace."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from openbricks.credentials import SecretBox
from openbricks.operations import (
    BackupInput,
    Operations,
    build_archive,
    crypt_file,
    extract_archive,
    register_operation_routes,
    retention_preview,
)
from openbricks.store import Store


class Scheduler:
    paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


class Service:
    def __init__(self):
        self.closed, self.resets = False, 0
        self.lock = threading.RLock()
        self.active_count = self.queued_count = 0

    def close(self):
        self.closed = True

    def reset(self):
        self.resets += 1

    @contextmanager
    def environment_change(self):
        yield


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in (
        "OPENBRICKS_SECRET_KEY",
        "OPENBRICKS_MANAGED_CONTAINER",
        "OPENBRICKS_COMPUTE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    app = FastAPI()
    app.state.store = Store(tmp_path / "workspace")
    app.state.runtime, app.state.workflows, app.state.notifications = (
        Service(),
        Service(),
        Service(),
    )
    app.state.scheduler = Scheduler()
    app.state.maintenance, app.state.restore_required = None, False
    app.state.operations = Operations(app)

    def authenticated(x_admin: str = Header(default="no")):
        return {"id": "fixture", "role": "admin" if x_admin == "yes" else "member"}

    def admin(x_admin: str = Header(default="no")):
        if x_admin != "yes":
            raise HTTPException(403, "Admin required")
        return {"id": "fixture", "role": "admin"}

    register_operation_routes(app, authenticated, admin)
    yield app
    app.state.operations.close()


def seed_source(root):
    store = Store(root)
    store.put(
        "notebooks",
        {
            "id": "source",
            "name": "backed notebook",
            "project_id": "default",
            "cells": [],
        },
    )
    key = SecretBox(root)
    store.put(
        "connections",
        {
            "id": "fixture",
            "name": "fixture",
            "_secrets": key.encrypt(
                json.dumps({"password": "synthetic-backup-fixture"})
            ),
        },
    )
    (root / "notebooks").mkdir()
    (root / "notebooks/MiXeD.py").write_text("print('synthetic notebook')")
    (root / "buckets/demo").mkdir(parents=True)
    (root / "buckets/demo/Orders.CSV").write_text("id,value\n1,example\n")
    store.put(
        "backups", {"id": "stale", "status": "running", "_path": "/not-a-portable-path"}
    )
    return store


def rewrite_archive(source, destination, change):
    with tarfile.open(source, "r:gz") as original, tarfile.open(
        destination, "w:gz"
    ) as target:
        for member in original.getmembers():
            data = original.extractfile(member).read()
            member, data = change(member, data)
            member.size = len(data)
            target.addfile(member, io.BytesIO(data))


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Operation did not finish")
        time.sleep(0.02)


def test_encrypted_backup_roundtrip_preserves_case_and_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENBRICKS_SECRET_KEY", raising=False)
    source = seed_source(tmp_path / "source")
    plain, encrypted = tmp_path / "backup.tar.gz", tmp_path / "backup.obbackup"
    manifest = build_archive(source, plain)
    crypt_file(plain, encrypted, "synthetic-passphrase-long")
    stage = tmp_path / "restored"
    assert (
        extract_archive(encrypted, stage, "synthetic-passphrase-long")["files"]
        == manifest["files"]
    )
    assert (stage / "buckets/demo/Orders.CSV").read_text() == "id,value\n1,example\n"
    assert (stage / "notebooks/MiXeD.py").is_file()
    recovered = Store(stage)
    assert recovered.count("backups") == 0
    secret = recovered.get("connections", "fixture")["_secrets"]
    assert (
        json.loads(SecretBox(stage).decrypt(secret))["password"]
        == "synthetic-backup-fixture"
    )
    assert not (stage / ".decrypted.tar.gz").exists()


def test_wrong_passphrase_and_tamper_remove_decrypted_temporary(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_SECRET_KEY", raising=False)
    plain, encrypted = tmp_path / "plain.tar.gz", tmp_path / "encrypted.obbackup"
    build_archive(seed_source(tmp_path / "source"), plain)
    crypt_file(plain, encrypted, "synthetic-passphrase-long")
    stage = tmp_path / "bad-password"
    with pytest.raises(ValueError, match="passphrase"):
        extract_archive(encrypted, stage, "incorrect-passphrase")
    assert not (stage / ".decrypted.tar.gz").exists()
    data = bytearray(encrypted.read_bytes())
    data[-1] ^= 1
    encrypted.write_bytes(data)
    with pytest.raises(ValueError, match="damaged"):
        extract_archive(encrypted, tmp_path / "tampered", "synthetic-passphrase-long")


@pytest.mark.parametrize(
    "unsafe",
    [
        "buckets/../escape",
        "buckets//alias",
        "buckets/./alias",
        "/buckets/escape",
        "buckets\\alias",
        "buckets/NUL",
        "buckets/name.",
    ],
)
def test_unsafe_archive_paths_are_rejected_before_restore(
    tmp_path, unsafe, monkeypatch
):
    monkeypatch.delenv("OPENBRICKS_SECRET_KEY", raising=False)
    plain, bad = tmp_path / "plain.tar.gz", tmp_path / "unsafe.tar.gz"
    build_archive(seed_source(tmp_path / "source"), plain)

    def change(member, data):
        if member.name == "buckets/demo/Orders.CSV":
            member.name = unsafe
        return member, data

    rewrite_archive(plain, bad, change)
    with pytest.raises(ValueError):
        extract_archive(bad, tmp_path / "stage")
    assert not (tmp_path / "escape").exists()


def test_checksum_duplicate_case_and_link_rejection(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_SECRET_KEY", raising=False)
    plain = tmp_path / "plain.tar.gz"
    build_archive(seed_source(tmp_path / "source"), plain)
    corrupted = tmp_path / "corrupt.tar.gz"
    rewrite_archive(
        plain,
        corrupted,
        lambda m, d: (
            m,
            d.replace(b"example", b"changed") if m.name.endswith("Orders.CSV") else d,
        ),
    )
    with pytest.raises(ValueError, match="checksum"):
        extract_archive(corrupted, tmp_path / "corrupt")
    for name, link in (("NOTebooks/mixed.PY", False), ("buckets/link", True)):
        bad = tmp_path / ("link.tar.gz" if link else "duplicate.tar.gz")
        with tarfile.open(plain, "r:gz") as original, tarfile.open(
            bad, "w:gz"
        ) as target:
            for member in original.getmembers():
                target.addfile(member, original.extractfile(member))
            entry = tarfile.TarInfo(name)
            if link:
                entry.type, entry.linkname = tarfile.SYMTYPE, "/outside"
            target.addfile(entry, io.BytesIO())
        with pytest.raises(ValueError):
            extract_archive(bad, tmp_path / ("link" if link else "duplicate"))


def test_restore_replaces_workspace_and_removes_unbacked_local_files(app, tmp_path):
    source = seed_source(tmp_path / "source")
    archive = tmp_path / "source.tar.gz"
    build_archive(source, archive)
    destination = app.state.store.directory
    app.state.store.put("notebooks", {"id": "old", "name": "old workspace"})
    (destination / "home").mkdir()
    (destination / "home/old-secret").write_text("fixture-only")
    result = app.state.operations.restore(archive, "")
    assert result["restart_required"] and app.state.restore_required
    assert (
        app.state.runtime.closed
        and app.state.workflows.closed
        and app.state.notifications.closed
    )
    assert app.state.runtime.resets == 1 and app.state.scheduler.paused
    assert app.state.store.get("notebooks", "source")["name"] == "backed notebook"
    assert not app.state.store.get("notebooks", "old")
    assert not (destination / "home/old-secret").exists()
    assert (destination / "buckets/demo/Orders.CSV").exists()


def test_restore_swap_failure_rolls_back_and_requires_restart(
    app, tmp_path, monkeypatch
):
    archive = tmp_path / "source.tar.gz"
    build_archive(seed_source(tmp_path / "source"), archive)
    app.state.store.put("notebooks", {"id": "old", "name": "original"})
    root = app.state.store.directory
    (root / "notebooks").mkdir()
    (root / "notebooks/original.py").write_text("original")
    original = Path.replace

    def fail(source, target):
        if source.name == "notebooks" and source.parent.name.startswith(".restore-"):
            raise OSError("synthetic swap failure")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(HTTPException) as error:
        app.state.operations.restore(archive, "")
    assert error.value.status_code == 503 and app.state.restore_required
    assert app.state.store.get("notebooks", "old")["name"] == "original"
    assert (root / "notebooks/original.py").read_text() == "original"
    assert app.state.scheduler.paused and app.state.maintenance


def test_restore_shutdown_failure_never_resumes_closed_services(app, tmp_path):
    archive = tmp_path / "source.tar.gz"
    build_archive(seed_source(tmp_path / "source"), archive)

    def fail():
        raise RuntimeError("synthetic shutdown failure")

    app.state.workflows.close = fail
    with pytest.raises(HTTPException):
        app.state.operations.restore(archive, "")
    assert (
        app.state.restore_required
        and app.state.maintenance
        and app.state.scheduler.paused
    )


def test_backup_drains_inflight_mutations_and_resets_only_local_kernel(app):
    operations = app.state.operations
    assert operations.enter_mutation()
    item = operations.backup(BackupInput())
    wait_until(lambda: bool(app.state.maintenance))
    assert not operations.enter_mutation() and app.state.runtime.resets == 0
    operations.leave_mutation()
    wait_until(
        lambda: (app.state.store.get("backups", item["id"]) or {}).get("status")
        == "succeeded"
        and not app.state.maintenance
    )
    assert app.state.runtime.resets == 1 and not app.state.runtime.closed
    assert not app.state.scheduler.paused
    record = app.state.store.get("backups", item["id"])
    extract_archive(record["_path"], app.state.store.directory / "verify-backup")


def test_failed_backup_releases_maintenance_and_admission_lock(app, monkeypatch):
    import openbricks.operations as module

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(module, "build_archive", fail)
    item = app.state.operations.backup(BackupInput())
    wait_until(
        lambda: (app.state.store.get("backups", item["id"]) or {}).get("status")
        == "failed"
        and not app.state.maintenance
    )
    assert not app.state.operations.lock.locked() and not app.state.scheduler.paused
    assert not app.state.runtime.closed


def test_retention_accounts_for_data_previews_and_preserves_active_work(app, tmp_path):
    store, operations = app.state.store, app.state.operations
    current = datetime.now(timezone.utc)
    old = (current - timedelta(days=100)).isoformat()
    aged = (current - timedelta(days=5)).isoformat()
    for id, status, created in (
        ("expired", "succeeded", aged),
        ("delete", "failed", old),
        ("active", "running", old),
    ):
        store.put(
            "runs",
            {
                "id": id,
                "status": status,
                "created_at": created,
                "outputs": ["fixture"],
                "logs": ["fixture"],
                "data_result": {"rows": [["x" * 10000]]},
                "data_events": ["large fixture"],
                "quality_reports": [{"passed": True}],
                "lineage": ["fixture"],
            },
        )
    store.put(
        "workflow_runs",
        {"id": "active-workflow", "status": "queued", "created_at": old},
    )
    store.put("data_lineage", {"id": "old-lineage", "created_at": old})
    store.put(
        "settings",
        {
            "id": "retention",
            "output_days": 1,
            "run_days": 90,
            "max_output_mb": 500,
            "backup_keep": 1,
        },
    )
    preview = retention_preview(store, operations.policy())
    assert preview["bytes_to_reclaim"] > 10000 and preview["runs_to_delete"] == 1
    operations.cleanup()
    expired = store.get("runs", "expired")
    assert (
        expired["outputs_expired"]
        and expired["outputs"] == []
        and "data_result" not in expired
        and "data_events" not in expired
    )
    assert store.get("runs", "active")["data_result"] and store.get(
        "workflow_runs", "active-workflow"
    )
    assert not store.get("runs", "delete") and not store.get(
        "data_lineage", "old-lineage"
    )


def test_operations_api_is_admin_only_and_retention_cannot_race_backup(app):
    with TestClient(app) as client:
        for path in (
            "/api/backups",
            "/api/operations/overview",
            "/api/operations/retention",
        ):
            assert client.get(path).status_code == 403
        assert client.post("/api/backups", json={}).status_code == 403
        assert client.get("/api/backups", headers={"x-admin": "yes"}).status_code == 200
        app.state.operations.lock.acquire()
        try:
            response = client.put(
                "/api/operations/retention",
                json={"output_days": 1, "run_days": 2},
                headers={"x-admin": "yes"},
            )
            assert response.status_code == 409
        finally:
            app.state.operations.lock.release()


def test_live_metrics_refreshes_with_short_timeouts_and_cache(app, monkeypatch):
    from openbricks import clusters

    app.state.store.put(
        "clusters", {"id": "remote", "name": "remote", "url": "https://compute.example"}
    )
    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, path, timeout):
            calls.append((path, timeout))
            return httpx.Response(
                200,
                json={
                    "service": "openbricks-compute",
                    "status": "idle",
                    "memory_used": 123,
                    "memory_total": 456,
                },
                request=httpx.Request("GET", "https://compute.example/status"),
            )

    monkeypatch.setattr(clusters, "client_for", lambda *args: Client())
    operations = app.state.operations
    operations.live_metrics()
    wait_until(lambda: not operations.metrics_refreshing)
    result = operations.live_metrics()
    remote = next(item for item in result if item["id"] == "remote")
    assert remote["memory_used"] == 123 and remote["measured_at"]
    assert len(calls) == 1 and calls[0][1].connect == 0.5 and calls[0][1].read == 2
    operations.live_metrics()
    assert len(calls) == 1
