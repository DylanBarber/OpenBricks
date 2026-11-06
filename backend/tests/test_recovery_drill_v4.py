"""Disposable encrypted archives and isolated management boot checks."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from openbricks.credentials import SecretBox
from openbricks.operations import build_archive, crypt_file, extract_archive
from openbricks.recovery_drill import run_probe
from openbricks.store import Store


@pytest.fixture
def source(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("OPENBRICKS_"):
            monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path / "source")
    store.put("settings", {"id": "initialized"})
    store.put(
        "users",
        {
            "id": "original",
            "username": "original",
            "role": "admin",
            "password_hash": "unused",
        },
    )
    store.put("projects", {"id": "default", "name": "Recovered project"})
    store.put(
        "notebooks",
        {
            "id": "queued-source",
            "name": "Do not execute",
            "project_id": "default",
            "cells": [
                {
                    "id": "cell",
                    "language": "python",
                    "source": f"from pathlib import Path\nPath({str(tmp_path / 'UNSAFE-EXECUTION')!r}).write_text('executed')",
                }
            ],
        },
    )
    store.put(
        "runs",
        {
            "id": "queued-run",
            "notebook_id": "queued-source",
            "project_id": "default",
            "status": "queued",
            "cluster_id": "local",
        },
    )
    store.put(
        "workflow_runs",
        {
            "id": "queued-workflow",
            "project_id": "default",
            "status": "queued",
            "run_as_user_id": "original",
            "tasks": [],
        },
    )
    store.put(
        "jobs",
        {
            "id": "enabled-job",
            "project_id": "default",
            "name": "Every minute",
            "notebook_id": "queued-source",
            "enabled": True,
            "cron": "* * * * *",
            "timezone": "UTC",
            "run_as_user_id": "original",
        },
    )
    store.put(
        "triggers", {"id": "enabled-trigger", "project_id": "default", "enabled": True}
    )
    store.put(
        "backup_policies",
        {"id": "enabled-policy", "project_id": "default", "enabled": True},
    )
    store.put(
        "backup_runs",
        {"id": "active-backup", "project_id": "default", "status": "uploading"},
    )
    store.put(
        "notification_targets",
        {
            "id": "target",
            "project_id": "default",
            "enabled": True,
            "url": "https://never-contact.invalid",
        },
    )
    store.put("sessions", {"id": "old-session", "user_id": "original"})
    store.put("api_tokens", {"id": "old-token", "user_id": "original"})
    store.put(
        "connections",
        {
            "id": "secret",
            "project_id": "default",
            "type": "s3",
            "_secrets": SecretBox(store.directory).encrypt(
                json.dumps(
                    {
                        "access_key": "synthetic",
                        "secret_key": "synthetic-secret-must-decrypt",
                    }
                )
            ),
        },
    )
    (store.directory / "buckets/data").mkdir(parents=True)
    (store.directory / "buckets/data/Orders.CSV").write_text("id,value\n1,recovered\n")
    return store


def encrypted_archive(source, tmp_path):
    plain, encrypted = tmp_path / "workspace.tar.gz", tmp_path / "workspace.obbackup"
    build_archive(source, plain)
    crypt_file(plain, encrypted, "synthetic-long-passphrase")
    return encrypted


def test_archive_cancels_active_backup_in_body_and_index_without_mutating_source(
    source, tmp_path
):
    encrypted = encrypted_archive(source, tmp_path)
    recovered_root = tmp_path / "restored"
    extract_archive(encrypted, recovered_root, "synthetic-long-passphrase")
    recovered = Store(recovered_root)
    backup = recovered.get("backup_runs", "active-backup")
    assert backup["status"] == "cancelled" and backup["finished_at"]
    assert source.get("backup_runs", "active-backup")["status"] == "uploading"
    with recovered.connect() as db:
        assert db.execute(
            "SELECT status,finished_at FROM documents WHERE kind='backup_runs' AND id='active-backup'"
        ).fetchone() == ("cancelled", backup["finished_at"])
    assert (
        json.loads(
            SecretBox(recovered_root).decrypt(
                recovered.get("connections", "secret")["_secrets"]
            )
        )["secret_key"]
        == "synthetic-secret-must-decrypt"
    )


def test_probe_boot_decrypts_and_disables_all_external_work(
    source, tmp_path, monkeypatch
):
    import openbricks.main as main
    from openbricks.automation import AutomationManager
    from openbricks.backup_automation import BackupAutomation
    from openbricks.notifications import NotificationManager
    from openbricks.runtime import SingleSessionRuntime

    encrypted = encrypted_archive(source, tmp_path)
    starts, workers, boots = [], [], []
    for cls in (AutomationManager, BackupAutomation, NotificationManager):
        monkeypatch.setattr(
            cls, "start", lambda self, name=cls.__name__: starts.append(name)
        )
    monkeypatch.setattr(
        SingleSessionRuntime, "_get_process", lambda *args, **kw: workers.append(True)
    )
    real_create_app = main.create_app

    def capture(root):
        root = Path(root)
        restored = Store(root)
        assert os.environ["DATA_DIR"] == str(root)
        assert Path(os.environ["HOME"]).parent == root.parent
        assert Path(os.environ["USERPROFILE"]) == Path(os.environ["HOME"])
        assert "OPENBRICKS_COMPUTE_URL" not in os.environ
        assert "OPENBRICKS_CATALOG_URL" not in os.environ
        assert restored.count("sessions") == restored.count("api_tokens") == 0
        assert restored.get("jobs", "enabled-job")["enabled"] is False
        assert restored.get("triggers", "enabled-trigger")["enabled"] is False
        assert restored.get("backup_policies", "enabled-policy")["enabled"] is False
        assert restored.get("runs", "queued-run")["status"] == "cancelled"
        assert restored.get("workflow_runs", "queued-workflow")["status"] == "cancelled"
        assert "synthetic-secret-must-decrypt" in SecretBox(root).decrypt(
            restored.get("connections", "secret")["_secrets"]
        )
        boots.append(root)
        return real_create_app(root)

    monkeypatch.setattr(main, "create_app", capture)
    environment = dict(
        os.environ,
        OPENBRICKS_COMPUTE_URL="http://never-contact.invalid",
        OPENBRICKS_CATALOG_URL="http://never-contact.invalid",
        DATA_DIR=str(source.directory),
        HOME=str(source.directory),
        USERPROFILE=str(source.directory),
    )
    with patch.dict(os.environ, environment, clear=True):
        result = run_probe(encrypted, "synthetic-long-passphrase")
    assert result["passed"] and result["management_boot"] and result["authentication"]
    assert result["notebooks"] == 1 and result["projects"] >= 1
    assert result["external_services_started"] is False
    assert boots and not boots[0].exists()
    assert not starts and not workers and not (tmp_path / "UNSAFE-EXECUTION").exists()
    assert source.get("jobs", "enabled-job")["enabled"] is True
    assert source.get("runs", "queued-run")["status"] == "queued"


def probe_subprocess(archive, passphrase):
    return subprocess.run(
        [sys.executable, "-m", "openbricks.recovery_drill"],
        input=json.dumps({"archive": str(archive), "passphrase": passphrase}),
        text=True,
        capture_output=True,
        timeout=45,
        cwd=Path(__file__).parents[1],
        env=dict(os.environ),
    )


def test_real_subprocess_encrypted_probe_boots_without_executing_restored_work(
    source, tmp_path
):
    encrypted = encrypted_archive(source, tmp_path)
    completed = probe_subprocess(encrypted, "synthetic-long-passphrase")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["passed"] and result["external_services_started"] is False
    assert result["files_validated"] >= 3
    assert not (tmp_path / "UNSAFE-EXECUTION").exists()
    assert not list(tmp_path.glob(".restore-drill-*"))


def test_populated_assets_archive_passes_strict_extract_and_real_restore_probe(
    source, tmp_path
):
    from openbricks.assets import AssetService
    from openbricks.permissions import Access

    source.put(
        "runs", {"id": "completed", "project_id": "default", "status": "succeeded"}
    )
    for action, dataset in (
        ("read", "blob://data/source"),
        ("write", "blob://data/sink"),
    ):
        source.put(
            "data_lineage",
            {
                "id": action,
                "project_id": "default",
                "run_id": "completed",
                "action": action,
                "dataset": dataset,
                "created_at": "2026-11-06T00:00:00+00:00",
            },
        )
    AssetService(source, Access(source))
    encrypted = encrypted_archive(source, tmp_path)
    restored_root = tmp_path / "strict-extraction"
    extract_archive(encrypted, restored_root, "synthetic-long-passphrase")
    restored = Store(restored_root)
    graph = AssetService(restored, Access(restored)).graph(
        "default", "blob://data/sink", "upstream"
    )
    assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 1
    assert graph["edges"][0]["run_id"] == "completed"
    completed = probe_subprocess(encrypted, "synthetic-long-passphrase")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["passed"] is True
    assert not (tmp_path / "UNSAFE-EXECUTION").exists()


def test_real_subprocess_wrong_passphrase_returns_only_generic_failure(
    source, tmp_path
):
    encrypted = encrypted_archive(source, tmp_path)
    completed = probe_subprocess(encrypted, "wrong-sensitive-passphrase")
    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "passed": False,
        "error": "Restored workspace validation or isolated boot failed",
    }
    assert "wrong-sensitive-passphrase" not in completed.stdout + completed.stderr
    assert "synthetic-secret" not in completed.stdout + completed.stderr
    assert not list(tmp_path.glob(".restore-drill-*"))
