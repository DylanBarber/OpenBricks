"""Guard persisted synchronization checkpoints against lost or contested sinks."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openbricks import synchronization as sync


@pytest.fixture
def absent_sink(tmp_path, monkeypatch):
    from delta.tables import DeltaTable
    from pyspark.sql import functions as functions

    blob_root = tmp_path / "buckets"
    blob_root.mkdir()
    blob = SimpleNamespace(root=blob_root, path=lambda uri: str(blob_root / "orders"))
    frame = MagicMock()
    frame.filter.return_value.drop.return_value = frame
    frame.alias.return_value = frame
    spark = SimpleNamespace(read=SimpleNamespace(parquet=lambda path: frame))
    connection = MagicMock()
    registry = SimpleNamespace(
        sqlserver=lambda identifier: SimpleNamespace(connect=lambda: connection)
    )
    info = {
        "rows": 1,
        "deletes": 0,
        "schema_sha256": "schema-one",
        "columns": [{"name": "id"}],
        "checkpoint": {
            "version": 2,
            "source_identity": "fixture",
            "schema_sha256": "schema-one",
        },
    }
    monkeypatch.setattr(sync, "snapshot_to_parquet", lambda *args, **kwargs: info)
    monkeypatch.setattr(functions, "col", lambda value: MagicMock())
    monkeypatch.setattr(DeltaTable, "isDeltaTable", lambda *args: False)
    target = MagicMock()
    target.detail.return_value.select.return_value.first.return_value = {
        "properties": {sync.OWNER: "another-ingestion", sync.SCHEMA: "schema-one"}
    }
    target.alias.return_value.merge.side_effect = AssertionError(
        "A protected sink must not receive a merge"
    )
    builder = MagicMock()
    builder.location.return_value = builder
    builder.addColumns.return_value = builder
    builder.property.return_value = builder
    builder.execute.return_value = target
    monkeypatch.setattr(DeltaTable, "createIfNotExists", lambda *args: builder)
    spec = {
        "ingestion_id": "this-ingestion",
        "connection_id": "fixture",
        "source": {"schema": "dbo", "table": "Orders"},
        "sink": {"path": "blob://sync-test/orders"},
        "keys": ["id"],
        "mode": "change_tracking",
    }
    return sync.ChangeTrackingIngestor(lambda: spark, registry, blob), spec, builder


def test_existing_checkpoint_cannot_initialize_an_empty_delta_sink(absent_sink):
    ingestor, spec, builder = absent_sink
    spec["previous_watermark"] = {
        "version": 1,
        "source_identity": "fixture",
        "schema_sha256": "schema-one",
    }
    with pytest.raises(ValueError, match="(?i)sink"):
        ingestor.run(spec)
    builder.execute.assert_not_called()


def test_create_if_missing_rechecks_sink_ownership_after_concurrent_creation(
    absent_sink,
):
    ingestor, spec, _ = absent_sink
    spec["previous_watermark"] = None
    with pytest.raises(ValueError, match="(?i)sink"):
        ingestor.run(spec)
