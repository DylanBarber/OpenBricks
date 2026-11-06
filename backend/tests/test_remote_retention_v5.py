from openbricks.compute_client import merge_remote_expiry


def test_expired_remote_output_drops_partial_profile_and_keeps_checkpoint_outcome():
    run = {
        "id": "original",
        "notebook_id": "book",
        "status": "succeeded",
        "profile": {"partial": True},
        "outputs": [{"stdout": "partial output"}],
        "data_result": {"rows": [["partial"]]},
        "_execution_spec": {"source": "captured source"},
        "quality_reports": [{"partial": True}],
    }
    checkpoint = {"ingestion_id": "sync", "previous_watermark": 1, "watermark": 2}
    merge_remote_expiry(
        run,
        {
            "outputs_expired": True,
            "compute_outputs_expired_at": "2026-11-06T00:00:00+00:00",
            "data_result": checkpoint,
        },
    )
    assert run["outputs_expired"] and run["outputs"] == run["logs"] == []
    assert "profile" not in run and "quality_reports" not in run
    assert run["data_result"] == checkpoint
    assert run["_execution_spec"] == {"source": "captured source"}
    assert run["id"] == "original" and run["status"] == "succeeded"


def test_management_copy_is_unchanged_without_remote_expiration_marker():
    run = {
        "id": "captured",
        "profile": {"complete": True},
        "outputs": [{"stdout": "captured"}],
    }
    original = run.copy()
    merge_remote_expiry(run, {"status": "succeeded"})
    assert run == original
