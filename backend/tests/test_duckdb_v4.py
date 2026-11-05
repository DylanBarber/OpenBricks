import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openbricks.blob import LocalBlobStore
from openbricks.duckdb_engine import run_duckdb


@pytest.fixture
def blob(tmp_path):
    value = LocalBlobStore(tmp_path / "buckets")
    value.create_bucket("demo")
    value.write_text(
        "blob://demo/orders.csv",
        "id,region,amount\n1,North,12.5\n2,South,20\n3,North,10\n",
    )
    return value


def query(sql, **values):
    return {"engine": "duckdb", "operation": "query", "sql": sql, **values}


def test_real_csv_parameter_binding_and_bounded_preview_without_spark(blob):
    spec = query(
        "SELECT region, sum(amount) AS total FROM orders WHERE id > ? GROUP BY region ORDER BY region",
        parameters=[1],
        payload={"sources": [{"name": "orders", "path": "blob://demo/orders.csv"}]},
    )
    result = run_duckdb(spec, blob, None, memory_mb=128, threads=1)
    assert result["columns"] == ["region", "total"] and result["rows"] == [
        ["North", 10.0],
        ["South", 20.0],
    ]
    assert result["memory_limit_mb"] == 128 and result["threads"] == 1
    assert run_duckdb(query("SELECT * FROM range(20)", limit=3), blob, None)[
        "truncated"
    ]


def test_data_workspace_parameter_contract_does_not_initialize_spark(blob):
    from openbricks.data_workspace import ComputeDataWorkspace

    def forbidden_spark():
        pytest.fail("A DuckDB query must not initialize Spark")

    workspace = ComputeDataWorkspace(
        forbidden_spark, None, blob, lambda _: None, "duckdb-integration"
    )
    result = workspace.run(query("SELECT ? AS answer", params=[42]))
    assert result["rows"] == [[42]]


def test_real_parquet_blob_literal_quotes_and_metadata_paging(blob):
    import pyarrow as pa
    import pyarrow.parquet as parquet

    path = blob.path("blob://demo/quoted'file.parquet")
    parquet.write_table(pa.table({"answer": [42], "text": ["sample"]}), path)
    result = run_duckdb(
        query("SELECT answer FROM read_parquet('blob://demo/quoted''file.parquet')"),
        blob,
        None,
    )
    assert result["rows"] == [[42]]
    sources = [
        {"name": "first", "path": "blob://demo/orders.csv"},
        {"name": "second", "path": "blob://demo/orders.csv"},
    ]
    result = run_duckdb(
        {"operation": "tables", "payload": {"sources": sources}, "limit": 1}, blob, None
    )
    assert result["row_count"] == 1 and result["next_offset"] == 1
    result = run_duckdb(
        {
            "operation": "columns",
            "table": "first",
            "payload": {"sources": sources},
            "limit": 2,
        },
        blob,
        None,
    )
    assert result["rows"][0][0] == "id" and result["next_offset"] == 2


def test_staged_s3_uses_only_selected_connection_and_cleans_up(
    blob, tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setenv("OPENBRICKS_WORKER_TMPDIR", str(staging))
    body = io.BytesIO(b"id,value\n1,test\n")
    calls = []

    class Registry:
        def s3(self, identifier):
            calls.append(identifier)
            return SimpleNamespace(
                client=SimpleNamespace(
                    get_object=lambda **kw: {"Body": body, "ContentLength": 16}
                )
            )

    result = run_duckdb(
        query(
            "SELECT * FROM remote",
            payload={
                "sources": [
                    {
                        "name": "remote",
                        "path": "s3://bucket/file.csv",
                        "connection_id": "selected",
                    }
                ]
            },
        ),
        blob,
        Registry(),
    )
    assert calls == ["selected"] and result["rows"] == [[1, "test"]]
    assert body.closed and list(staging.iterdir()) == []


def test_staged_s3_size_limit_closes_body(blob, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_DUCKDB_STAGE_MB", "1")
    body = io.BytesIO(b"x" * (1024**2 + 1))
    registry = SimpleNamespace(
        s3=lambda _: SimpleNamespace(
            client=SimpleNamespace(get_object=lambda **kw: {"Body": body})
        )
    )
    with pytest.raises(ValueError, match="staging allowance"):
        run_duckdb(
            query(
                "SELECT * FROM remote",
                payload={
                    "sources": [
                        {
                            "name": "remote",
                            "path": "s3://bucket/file.csv",
                            "connection_id": "selected",
                        }
                    ]
                },
            ),
            blob,
            registry,
        )
    assert body.closed


def test_no_extension_network_or_unscoped_file_reads_and_locked_limits(blob, tmp_path):
    secret = tmp_path / "outside.csv"
    secret.write_text("secret\nexample\n")
    for sql in (
        "INSTALL httpfs",
        "SET memory_limit = '4TB'",
        f"SELECT * FROM read_csv('{secret.as_posix()}')",
    ):
        with pytest.raises(Exception):
            run_duckdb(query(sql), blob, None)
    with pytest.raises(ValueError):
        run_duckdb(
            query(
                "SELECT 1",
                payload={
                    "sources": [
                        {"name": "unsafe;drop", "path": "blob://demo/orders.csv"}
                    ]
                },
            ),
            blob,
            None,
        )


def test_large_strings_and_special_types_are_json_safe(blob):
    result = run_duckdb(
        query(
            "SELECT repeat('x', 12000) AS long, DATE '2026-11-05' AS day, CAST(12.34 AS DECIMAL(5,2)) AS amount, 'abc'::BLOB AS raw"
        ),
        blob,
        None,
    )
    assert result["truncated"] and len(result["rows"][0][0]) == 10000
    assert result["rows"][0][1:] == ["2026-11-05", "12.34", "616263"]
    json.dumps(result, allow_nan=False)
