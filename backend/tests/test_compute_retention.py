from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient

from openbricks.compute import ComputeStore, create_compute_app
from openbricks.compute_retention import BATCH_SIZE, ComputeRetention

TOKEN = "retention-test-token-with-at-least-32-characters"
INSTANT = datetime(2026, 11, 6, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def retention_defaults(monkeypatch):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_OUTPUT_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("OPENBRICKS_COMPUTE_OUTPUT_BUDGET_MB", raising=False)


def run(run_id, *, status="succeeded", age_days=40, output="retained output"):
    stamp = (INSTANT - timedelta(days=age_days)).isoformat()
    return {
        "id": run_id,
        "notebook_id": "notebook",
        "status": status,
        "project_id": "default",
        "created_at": stamp,
        "finished_at": stamp,
        "outputs": [{"stdout": output}],
        "logs": ["worker log"],
        "profile": {"resources": {"samples": [1, 2]}},
        "data_events": [{"event": "sensitive data"}],
        "data_result": {"rows": [[1, 2]]},
        "quality_reports": ["report"],
        "lineage": ["source"],
        "_cells": [{"source": "print(1)"}],
        "_notebook": {"cells": [{"source": "print(1)"}]},
        "_environment": {"wheels": ["package.whl"]},
        "_execution_spec": {"cells": [{"source": "print(1)"}]},
        "parameters": {"large": "parameter"},
        "error": "x" * 3000,
        "error_class": "test_error",
        "_request_fingerprint": "exact-hash",
        "provenance": {"source_hash": "source-hash"},
    }


def assert_accounting(store):
    with store.connect() as db:
        actual = db.execute(
            "SELECT COALESCE(SUM(compute_output_bytes),0) FROM documents WHERE kind='runs'"
        ).fetchone()[0]
        total = db.execute(
            "SELECT bytes FROM compute_retention_totals WHERE id=1"
        ).fetchone()[0]
    assert total == actual


def test_ttl_compacts_payload_preserving_identity_and_active_records(tmp_path):
    store = ComputeStore(tmp_path)
    retention = ComputeRetention(store)
    original = run("old", status="failed")
    store.put("runs", original)
    for status in ("running", "queued"):
        store.put("runs", run(status, status=status, age_days=400))
    store.put("runs", run("recent", age_days=1))
    result = retention.cleanup(INSTANT)
    assert result["compacted_runs"] == 1
    expired = store.get("runs", "old")
    for key in (
        "id",
        "notebook_id",
        "project_id",
        "status",
        "created_at",
        "finished_at",
        "error_class",
        "_request_fingerprint",
        "provenance",
    ):
        assert expired[key] == original[key]
    assert expired["outputs_expired"] is True
    assert expired["outputs"] == expired["logs"] == []
    assert len(expired["error"]) == 2000
    for key in (
        "profile",
        "data_events",
        "data_result",
        "quality_reports",
        "lineage",
        "_cells",
        "_notebook",
        "_environment",
        "_execution_spec",
        "parameters",
    ):
        assert key not in expired
    for run_id in ("queued", "running", "recent"):
        assert "outputs_expired" not in store.get("runs", run_id)
        assert store.get("runs", run_id)["profile"]
    assert retention.cleanup(INSTANT)["compacted_runs"] == 0
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT compute_output_bytes FROM documents WHERE id='old'"
            ).fetchone()[0]
            == 0
        )
    assert_accounting(store)


def test_budget_removes_oldest_terminal_output_and_protects_new_completion(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENBRICKS_COMPUTE_OUTPUT_BUDGET_MB", "1")
    store = ComputeStore(tmp_path)
    retention = ComputeRetention(store)
    store.put("runs", run("oldest", age_days=3, output="x" * (600 * 1024)))
    store.put("runs", run("newer", age_days=2, output="x" * (600 * 1024)))
    assert retention.cleanup(INSTANT)["compacted_runs"] == 1
    assert store.get("runs", "oldest")["outputs_expired"]
    assert "outputs_expired" not in store.get("runs", "newer")
    store.put("runs", run("just-finished", age_days=0, output="x" * (2 * 1024 * 1024)))
    retention.cleanup(INSTANT)
    assert "outputs_expired" not in store.get("runs", "just-finished")
    assert_accounting(store)


def test_migration_and_pruning_are_bounded_and_restart_accounting_survives(tmp_path):
    store = ComputeStore(tmp_path)
    for index in range(BATCH_SIZE + 5):
        store.put("runs", run(f"old-{index:04}"))
    retention = ComputeRetention(store)
    first = retention.cleanup(INSTANT)
    assert first["backfilled_runs"] == BATCH_SIZE
    assert first["compacted_runs"] == BATCH_SIZE
    second = retention.cleanup(INSTANT)
    assert second["backfilled_runs"] == second["compacted_runs"] == 5
    assert second["retained_bytes"] == 0
    assert store.count("runs") == BATCH_SIZE + 5
    store.put("runs", run("fresh", age_days=1))
    restarted = ComputeRetention(store)
    assert restarted.cleanup(INSTANT)["compacted_runs"] == 0
    assert_accounting(store)
    store.put(
        "runs", {**store.get("runs", "fresh"), "outputs": [{"stdout": "changed"}]}
    )
    assert_accounting(store)
    store.delete("runs", "fresh")
    assert_accounting(store)
    assert restarted.cleanup(INSTANT)["retained_bytes"] == 0


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_ingestion_checkpoint_outcome_survives_without_preview_and_reconciles(
    tmp_path, status
):
    from openbricks.data_workspace import finalize_data_run
    from openbricks.store import Store

    store = ComputeStore(tmp_path / "compute")
    retention = ComputeRetention(store)
    original = run("ingestion-run", status=status)
    original["data_execution_id"] = "execution"
    original["data_result"] = {
        "ingestion_id": "ingestion",
        "previous_watermark": 10,
        "watermark": 55,
        "rows": [[55, "preview output"]],
        "columns": ["value", "data"],
        "source_rows": 12,
    }
    store.put("runs", original)
    retention.cleanup(INSTANT)
    expired = store.get("runs", "ingestion-run")
    assert expired["data_result"] == {
        "ingestion_id": "ingestion",
        "previous_watermark": 10,
        "watermark": 55,
    }
    assert retention.cleanup(INSTANT)["retained_bytes"] == 0
    management = Store(tmp_path / "management")
    management.put(
        "data_ingestions",
        {
            "id": "ingestion",
            "project_id": "default",
            "watermark_value": 10,
        },
    )
    management.put(
        "data_executions",
        {
            "id": "execution",
            "project_id": "default",
            "run_id": original["id"],
            "ingestion_id": "ingestion",
            "status": "running",
        },
    )
    finalize_data_run(management, expired)
    finalize_data_run(management, expired)
    assert management.get("data_executions", "execution")["status"] == status
    assert management.get("data_ingestions", "ingestion")["watermark_value"] == (
        55 if status == "succeeded" else 10
    )


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_expired_duplicate_post_returns_original_status_without_new_execution(
    tmp_path, status
):
    app = create_compute_app(tmp_path, TOKEN)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        app.state.store.put("runs", run("existing", status=status))
        app.state.retention.cleanup(INSTANT)
        assert app.state.store.get("runs", "existing")["outputs_expired"]

        def must_not_create_worker(*args, **kwargs):
            pytest.fail("A duplicate expired RunID must never create a worker")

        app.state.runtime.kernel_factory = must_not_create_worker
        payload = {
            "run_id": "existing",
            "notebook": {
                "id": "notebook",
                "name": "Changed request",
                "cells": [
                    {
                        "id": "one",
                        "language": "python",
                        "source": "raise RuntimeError('must never execute')",
                    }
                ],
            },
        }
        response = client.post("/runs", json=payload)
        assert response.status_code == 202, response.text
        assert response.json()["id"] == "existing"
        assert response.json()["status"] == status
        assert response.json()["outputs_expired"] is True
        assert app.state.runtime.active_count == 0
        assert app.state.store.count("runs") == 1
        payload["notebook"]["id"] = "another-notebook"
        assert client.post("/runs", json=payload).status_code == 400
        policy = client.get("/status").json()["output_retention"]
        assert policy["retention_days"] == 30
        assert policy["budget_bytes"] == 500 * 1024 * 1024
    assert not app.state.retention.thread.is_alive()


@pytest.mark.parametrize(
    "name,value",
    [
        ("OPENBRICKS_COMPUTE_OUTPUT_RETENTION_DAYS", "0"),
        ("OPENBRICKS_COMPUTE_OUTPUT_RETENTION_DAYS", "3651"),
        ("OPENBRICKS_COMPUTE_OUTPUT_BUDGET_MB", "not-a-secret-value"),
        ("OPENBRICKS_COMPUTE_OUTPUT_BUDGET_MB", "100001"),
    ],
)
def test_invalid_policy_fails_without_echoing_input(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as caught:
        ComputeRetention(ComputeStore(tmp_path))
    assert name in str(caught.value)
    assert value not in str(caught.value).split(" must ")[0]


def test_blank_policy_uses_defaults_and_other_documents_are_not_accounted(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENBRICKS_COMPUTE_OUTPUT_RETENTION_DAYS", " ")
    monkeypatch.setenv("OPENBRICKS_COMPUTE_OUTPUT_BUDGET_MB", "")
    store = ComputeStore(tmp_path)
    retention = ComputeRetention(store)
    store.put("other", {"id": "payload", "data": "x" * 10000})
    assert retention.cleanup(INSTANT)["retained_bytes"] == 0
    assert retention.status()["retention_days"] == 30
    assert "payload" in json.dumps(store.list("other"))
