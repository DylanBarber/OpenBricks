"""Regression for backups taken before the first embedded Hive operation."""
from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from openbricks.credentials import SecretBox
from openbricks.operations import Operations, build_archive
from openbricks.runtime import LocalRuntime
from openbricks.store import Store
from openbricks.worker import prepare_metastore_path


def test_empty_legacy_metastore_is_removed_but_nonempty_contents_are_preserved(tmp_path):
    path = tmp_path / "metastore"
    path.mkdir()
    assert prepare_metastore_path(tmp_path) == path
    assert not path.exists()
    path.mkdir()
    marker = path / "service.properties"
    marker.write_text("Existing database must be preserved", encoding="utf-8")
    assert prepare_metastore_path(tmp_path) == path
    assert marker.read_text(encoding="utf-8") == "Existing database must be preserved"


def test_metastore_guard_does_not_remove_files_created_during_empty_check(tmp_path, monkeypatch):
    path = tmp_path / "metastore"
    path.mkdir()
    original = Path.rmdir
    def concurrent_writer(target):
        if target == path:
            (path / "created-concurrently").write_text("preserve", encoding="utf-8")
        return original(target)
    monkeypatch.setattr(Path, "rmdir", concurrent_writer)
    prepare_metastore_path(tmp_path)
    assert (path / "created-concurrently").read_text(encoding="utf-8") == "preserve"


def test_metastore_guard_leaves_symlinks_untouched(tmp_path):
    elsewhere = tmp_path / "external"
    elsewhere.mkdir()
    path = tmp_path / "metastore"
    try:
        path.symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are not available for this user")
    prepare_metastore_path(tmp_path)
    assert path.is_symlink() and elsewhere.is_dir()


class Service:
    def __init__(self):
        self.lock = threading.RLock()

    @contextmanager
    def environment_change(self):
        yield

    def close(self):
        pass

    def reset(self):
        pass


def wait(store, identifier):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        value = store.get("runs", identifier)
        if value["status"] not in ("queued", "running"):
            assert value["status"] == "succeeded", value.get("error")
            return value
        time.sleep(0.05)
    pytest.fail("Real Hive operation did not complete")


@pytest.mark.parametrize("legacy_empty_directory", [False, True], ids=["fresh-restore", "legacy-empty-restore"])
def test_real_hive_after_never_used_metastore_backup_restore(tmp_path, monkeypatch, legacy_empty_directory):
    if os.getenv("OPENBRICKS_TEST_SPARK") != "1":
        pytest.skip("Enable in the packaged Java17/Spark compute environment")
    for key in ("OPENBRICKS_MANAGED_CONTAINER", "OPENBRICKS_SECRET_KEY", "OPENBRICKS_CATALOG_URL", "OPENBRICKS_S3_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SPARK_MASTER", "local[1]")
    monkeypatch.setenv("SPARK_SHUFFLE_PARTITIONS", "1")
    source = Store(tmp_path / "source")
    SecretBox(source.directory)
    archive = tmp_path / "before-first-hive.tar.gz"
    build_archive(source, archive)
    destination = Store(tmp_path / "restored")
    app = SimpleNamespace(state=SimpleNamespace(store=destination, runtime=Service(), workflows=Service(), notifications=Service(),
        scheduler=SimpleNamespace(pause=lambda: None), maintenance=None, restore_required=False))
    operations = Operations(app)
    try:
        assert operations.restore(archive, "")["restart_required"]
    finally:
        operations.close()
    metastore = destination.directory / "metastore"
    assert not metastore.exists()
    if legacy_empty_directory:
        metastore.mkdir()  # State created by the earlier restore implementation.
    restored = Store(destination.directory)
    runtime = LocalRuntime(restored)
    runtime.timeout = 110
    runtime.start()
    try:
        notebook = {"id": "restored-hive", "name": "Restored Hive", "project_id": "default", "connection_ids": [], "cells": [
            {"id": "database", "language": "sql", "source": "CREATE DATABASE IF NOT EXISTS spark_catalog.restore_probe"},
            {"id": "table", "language": "sql", "source": "CREATE TABLE IF NOT EXISTS spark_catalog.restore_probe.sample USING PARQUET AS SELECT 42 AS answer"},
            {"id": "query", "language": "sql", "source": "SELECT answer FROM spark_catalog.restore_probe.sample"}]}
        done = wait(restored, runtime.submit(notebook)["id"])
        assert done["outputs"][-1]["result"]["rows"] == [[42]]
        assert (metastore / "service.properties").is_file()
        runtime.reset()
        notebook["cells"] = [notebook["cells"][-1]]
        reopened = wait(restored, runtime.submit(notebook)["id"])
        assert reopened["outputs"][0]["result"]["rows"] == [[42]]
        assert (metastore / "service.properties").is_file()
    finally:
        runtime.close()
