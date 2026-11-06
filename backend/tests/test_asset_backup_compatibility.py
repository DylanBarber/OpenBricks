"""Asset lookup indexes must survive strict SQLite 3.40 backup validation."""

import json
import sqlite3

from openbricks.assets import AssetService
from openbricks.permissions import Access
from openbricks.store import Store


def observation(
    identifier="read", *, dataset="blob://data/source", action="read", run_id="run"
):
    return {
        "id": identifier,
        "project_id": "default",
        "dataset": dataset,
        "action": action,
        "run_id": run_id,
        "created_at": "2026-11-06T00:00:00+00:00",
    }


def assert_safe_indexes(store):
    with store.connect() as db:
        indexes = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE name IN ('asset_lineage_dataset','asset_lineage_run')"
        ).fetchall()
        assert len(indexes) == 2
        assert all("json_extract" not in sql.lower() for _, sql in indexes)
        db.execute("PRAGMA trusted_schema=OFF")
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_legacy_asset_indexes_migrate_without_changing_documents(tmp_path, monkeypatch):
    store = Store(tmp_path)
    original = [
        observation(),
        observation("write", dataset="blob://data/sink", action="write"),
    ]
    for value in original:
        store.put("data_lineage", value)
    store.put("runs", {"id": "run", "project_id": "default", "status": "succeeded"})
    with store.connect() as db:
        db.execute(
            "UPDATE documents SET name=NULL,parent_id=NULL,notebook_name=NULL WHERE kind='data_lineage'"
        )
        db.execute(
            "CREATE INDEX asset_lineage_dataset ON documents(kind,COALESCE(project_id,'default'),json_extract(body,'$.dataset'),created_at DESC)"
        )
        db.execute(
            "CREATE INDEX asset_lineage_run ON documents(kind,COALESCE(project_id,'default'),json_extract(body,'$.run_id'),json_extract(body,'$.action'))"
        )
    assets = AssetService(store, Access(store))
    assert_safe_indexes(store)
    assert [store.get("data_lineage", value["id"]) for value in original] == original
    graph = assets.graph("default", "blob://data/sink", "upstream")
    assert len(graph["nodes"]) == 2
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["run_id"] == "run"

    # Reopening the service leaves the replacement indexes and evidence intact.
    def must_not_repeat_backfill(db):
        raise AssertionError(
            "Migrated lineage metadata must not be scanned on every boot"
        )

    monkeypatch.setattr(
        "openbricks.assets.backfill_lineage_metadata", must_not_repeat_backfill
    )
    AssetService(Store(tmp_path), Access(store))
    assert_safe_indexes(store)


def test_lineage_metadata_tracks_writes_and_other_document_kinds_are_unchanged(
    tmp_path,
):
    store = Store(tmp_path)
    assets = AssetService(store, Access(store))
    first = observation()
    assert store.put("data_lineage", first) == first
    updated = observation(
        dataset="blob://data/replaced", action="write", run_id="replacement-run"
    )
    store.put("data_lineage", updated)
    other = {
        "id": "other",
        "name": "ordinary-name",
        "parent_id": "ordinary-parent",
        "notebook_name": "ordinary-notebook",
    }
    store.put("other", other)
    with store.connect() as db:
        assert db.execute(
            "SELECT name,parent_id,notebook_name FROM documents WHERE kind='data_lineage' AND id='read'"
        ).fetchone() == (updated["dataset"], updated["run_id"], updated["action"])
        assert db.execute(
            "SELECT name,parent_id,notebook_name FROM documents WHERE kind='other' AND id='other'"
        ).fetchone() == (other["name"], other["parent_id"], other["notebook_name"])
        plans = db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM documents WHERE kind='data_lineage' AND COALESCE(project_id,'default')=? AND name=? ORDER BY created_at DESC,id DESC LIMIT 100",
            ("default", updated["dataset"]),
        ).fetchall()
        assert any("asset_lineage_dataset" in str(row) for row in plans)
        plans = db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM documents WHERE kind='data_lineage' AND COALESCE(project_id,'default')=? AND parent_id=? AND notebook_name=?",
            ("default", updated["run_id"], updated["action"]),
        ).fetchall()
        assert any("asset_lineage_run" in str(row) for row in plans)
    assert store.get("data_lineage", "read") == updated
    assert assets.page("default")["items"][0]["dataset"] == updated["dataset"]


def test_store_metadata_column_migration_uses_lineage_fields(tmp_path):
    original = {**observation(), "name": "body-name-is-not-the-dataset"}
    with sqlite3.connect(tmp_path / "openbricks.sqlite3") as db:
        db.execute(
            "CREATE TABLE documents(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id))"
        )
        db.execute(
            "INSERT INTO documents VALUES('data_lineage',?,?)",
            (original["id"], json.dumps(original)),
        )
    store = Store(tmp_path)
    with store.connect() as db:
        assert db.execute(
            "SELECT name,parent_id,notebook_name FROM documents"
        ).fetchone() == (
            original["dataset"],
            original["run_id"],
            original["action"],
        )
    assert store.get("data_lineage", original["id"]) == original
