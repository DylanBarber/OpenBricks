import copy
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace

from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from openbricks.automation import (
    AutomationManager,
    BackfillInput,
    TriggerInput,
    instant,
    register_automation_routes,
)
from openbricks.permissions import Access
from openbricks.store import Store
from openbricks.workflows import WorkflowManager


class Runtime:
    def __init__(self, store):
        self.store, self.calls, self.hold = store, [], False
        self.fail_dates = set()

    def submit(self, notebook, **options):
        identifier = options["run_id"]
        existing = self.store.get("runs", identifier)
        if existing:
            return existing
        self.calls.append((copy.deepcopy(notebook), copy.deepcopy(options)))
        failed = options.get("parameters", {}).get("day") in self.fail_dates
        return self.store.put(
            "runs",
            {
                "id": identifier,
                "project_id": notebook.get("project_id", "default"),
                "notebook_id": notebook["id"],
                "job_id": options.get("job_id"),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": (
                    "running" if self.hold else "failed" if failed else "succeeded"
                ),
                "error_class": "execution" if failed else None,
                "finished_at": (
                    None if self.hold else datetime.now(timezone.utc).isoformat()
                ),
                "error": "deliberate" if failed else None,
            },
        )

    def cancel(self, identifier):
        value = self.store.get("runs", identifier)
        value.update(
            status="cancelled", finished_at=datetime.now(timezone.utc).isoformat()
        )
        return self.store.put("runs", value)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    monkeypatch.delenv("OPENBRICKS_CATALOG_URL", raising=False)
    monkeypatch.delenv("OPENBRICKS_S3_ENDPOINT", raising=False)
    store = Store(tmp_path / "workspace")
    actor = store.put("users", {"id": "admin", "username": "admin", "role": "admin"})
    access = Access(store)
    access.seed()
    notebook = store.put(
        "notebooks",
        {
            "id": "n",
            "project_id": "default",
            "name": "Notebook",
            "connection_ids": [],
            "parameter_definitions": [
                {"name": "day", "type": "date", "default": "2026-01-01"},
                {"name": "event", "type": "json", "default": {}},
            ],
            "cells": [
                {"id": "one", "language": "python", "source": "print('original')"}
            ],
        },
    )
    runtime = Runtime(store)
    workflows = WorkflowManager(store, runtime, access)
    workflow = store.put(
        "workflows",
        {
            "id": "w",
            "name": "Workflow",
            "project_id": "default",
            "parameter_definitions": notebook["parameter_definitions"],
            "tasks": [{"key": "one", "notebook_id": "n", "depends_on": []}],
        },
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            store=store,
            runtime=runtime,
            workflows=workflows,
            access=access,
            maintenance=None,
        )
    )
    manager = AutomationManager(
        app,
        lambda expression, zone: CronTrigger.from_crontab(expression, timezone=zone),
    )
    app.state.automation = manager
    yield SimpleNamespace(
        store=store,
        actor=actor,
        runtime=runtime,
        workflows=workflows,
        manager=manager,
        app=app,
        notebook=notebook,
        workflow=workflow,
    )
    manager.close()


def advance(value, steps=1):
    for _ in range(steps):
        value.manager.tick()
        for workflow in value.store.list("workflow_runs"):
            if workflow["status"] not in ("succeeded", "failed", "cancelled"):
                value.workflows._tick(workflow)


def backfill(**extra):
    return BackfillInput(
        name="January",
        project_id="default",
        notebook_id="n",
        date_parameter="day",
        start_date="2026-01-02",
        end_date="2026-01-04",
        **extra,
    )


def test_backfill_preview_pins_source_and_bounds_concurrency(setup):
    value = setup
    preview = value.manager.preview_backfill(backfill(), value.actor)
    assert preview["dates"] == ["2026-01-02", "2026-01-03", "2026-01-04"]
    assert value.store.list("environments") == []  # Preview is read-only.
    created = value.manager.create_backfill(backfill(), value.actor)
    assert "_template_id" in created
    source = value.store.get("notebooks", "n")
    source["cells"][0]["source"] = "print('edited')"
    value.store.put("notebooks", source)
    advance(value)
    assert len(value.store.list("workflow_runs")) == 1
    advance(value, 10)
    finished = value.store.get("backfills", created["id"])
    assert finished["status"] == "succeeded" and finished["progress"] == {
        "completed": 3,
        "total": 3,
    }
    assert [
        options["parameters"]["day"] for _, options in value.runtime.calls
    ] == preview["dates"]
    assert all(
        notebook["cells"][0]["source"] == "print('original')"
        for notebook, _ in value.runtime.calls
    )
    assert (
        len({options["environment"]["id"] for _, options in value.runtime.calls}) == 1
    )


def test_backfill_crash_between_durable_intent_and_admission_recovers_same_id(
    setup, monkeypatch
):
    value = setup
    created = value.manager.create_backfill(backfill(), value.actor)
    original = value.workflows.submit_captured

    def crash(*args, **kwargs):
        raise SystemExit("simulate process loss before creating workflow")

    monkeypatch.setattr(value.workflows, "submit_captured", crash)
    with pytest.raises(SystemExit):
        value.manager.tick()
    durable = value.store.get("backfills", created["id"])
    expected = durable["items"][0]["run_id"]
    assert durable["items"][0]["status"] == "queued"
    monkeypatch.setattr(value.workflows, "submit_captured", original)
    recovered = AutomationManager(value.app)
    value.manager = recovered
    advance(value, 12)
    assert value.store.get("backfills", created["id"])["status"] == "succeeded"
    assert value.store.get("workflow_runs", expected)
    assert len(value.runtime.calls) == 3


def test_backfill_lost_ack_failure_stop_cancel_and_revocation(setup, monkeypatch):
    value = setup
    original = value.workflows.submit_captured

    def lost_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        raise RuntimeError("ack lost")

    monkeypatch.setattr(value.workflows, "submit_captured", lost_ack)
    value.runtime.fail_dates.add("2026-01-02")
    created = value.manager.create_backfill(backfill(), value.actor)
    advance(value, 6)
    assert value.store.get("backfills", created["id"])["status"] == "failed"
    assert len(value.runtime.calls) == 1
    assert [
        item["status"] for item in value.store.get("backfills", created["id"])["items"]
    ] == ["failed", "skipped", "skipped"]
    monkeypatch.setattr(value.workflows, "submit_captured", original)
    second = value.manager.create_backfill(backfill(), value.actor)
    cancelled = value.manager.cancel_backfill(second["id"], value.actor)
    assert cancelled["status"] == "cancelled"
    third = value.manager.create_backfill(backfill(), value.actor)
    value.store.delete("users", "admin")
    advance(value, 3)
    assert value.store.get("backfills", third["id"])["status"] == "failed"
    assert len(value.runtime.calls) == 1


def test_backfill_rejects_invalid_ranges_types_and_maintenance(setup):
    value = setup
    with pytest.raises(ValueError):
        value.manager.preview_backfill(
            BackfillInput(**{**backfill().model_dump(), "end_date": "2028-01-01"}),
            value.actor,
        )
    with pytest.raises(ValueError):
        value.manager.preview_backfill(
            BackfillInput(**{**backfill().model_dump(), "date_parameter": "missing"}),
            value.actor,
        )
    value.manager.pause()
    with pytest.raises(ValueError, match="paused"):
        value.manager.create_backfill(backfill(), value.actor)
    value.manager.resume()
    value.app.state.maintenance = "backup"
    with pytest.raises(ValueError, match="paused"):
        value.manager.create_backfill(backfill(), value.actor)


def job(policy="all", **changes):
    return {
        "id": "daily",
        "name": "Daily",
        "project_id": "default",
        "notebook_id": "n",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "created_at": "2026-01-01T00:00:00+00:00",
        "run_as_user_id": "admin",
        "catchup_policy": policy,
        "catchup_window_hours": 168,
        "catchup_max_runs": 2,
        "schedule_date_parameter": "day",
        **changes,
    }


def test_catchup_all_drains_bounded_queue_without_overlap_or_duplicate_fires(setup):
    value = setup
    saved = value.store.put("jobs", job())
    clock = instant("2026-01-06T12:00:00+00:00")
    value.manager.reconcile_job(saved, clock)
    assert len(value.store.list("automation_dispatches")) == 2
    value.manager.reconcile_job(saved, clock)
    assert len(value.store.list("automation_dispatches")) == 2
    for _ in range(30):
        value.manager._dispatch_pending()
        for workflow in value.store.list("workflow_runs"):
            if workflow["status"] not in ("succeeded", "failed", "cancelled"):
                value.workflows._tick(workflow)
        value.manager.reconcile_job(saved, clock)
    assert len(value.store.list("automation_dispatches")) == 5
    assert len(value.runtime.calls) == 5
    assert [options["parameters"]["day"] for _, options in value.runtime.calls] == [
        f"2026-01-{day:02}" for day in range(2, 7)
    ]
    assert all(
        options["resource_options"]["mode"] == "job"
        for _, options in value.runtime.calls
    )


@pytest.mark.parametrize("policy,count", [("skip", 0), ("latest", 1)])
def test_skip_and_latest_missed_schedule_policies(setup, policy, count):
    value = setup
    saved = value.store.put("jobs", job(policy))
    value.manager.reconcile_job(saved, instant("2026-01-06T12:00:00+00:00"))
    assert len(value.store.list("automation_dispatches")) == count
    state = value.manager.track_job(saved)
    assert state["skipped"] == 5 - count


def test_normal_schedule_fire_and_changed_definition_cursor(setup):
    value = setup
    saved = value.store.put("jobs", job("skip"))
    value.manager.reconcile_job(saved, instant("2026-01-06T00:00:30+00:00"))
    assert len(value.store.list("automation_dispatches")) == 1
    saved["cron"] = "5 0 * * *"
    reset = value.manager.track_job(saved)
    assert instant(reset["cursor"]) > instant("2026-01-06T00:00:30+00:00")


def test_scheduler_forced_occurrence_is_durable_and_deduplicated(setup):
    value = setup
    value.store.put("jobs", job("skip", cron="0 0 1 1 *"))
    scheduled = datetime.now(timezone.utc)
    first = value.manager.record_scheduled_fire("daily", scheduled)
    second = value.manager.record_scheduled_fire("daily", scheduled)
    assert first["id"] == second["id"]
    assert value.store.get("jobs", "daily")["last_run_at"]
    assert len(value.store.list("automation_dispatches")) == 1
    assert len(value.store.list("workflow_runs")) == 1
    value.workflows._tick(value.store.list("workflow_runs")[0])
    assert value.runtime.calls[0][1]["trigger"] == "schedule"
    restarted = AutomationManager(value.app, value.manager.cron_factory)
    try:
        restarted.record_scheduled_fire("daily", scheduled)
        assert len(value.store.list("workflow_runs")) == 1
    finally:
        restarted.close()


def test_scheduler_event_and_cron_reconciliation_share_one_occurrence(setup):
    value = setup
    scheduled = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    saved = value.store.put(
        "jobs",
        job(
            "skip",
            cron="* * * * *",
            created_at=(scheduled - timedelta(seconds=1)).isoformat(),
        ),
    )
    value.manager.reconcile_job(saved, scheduled + timedelta(milliseconds=1))
    value.manager.record_scheduled_fire("daily", scheduled)
    assert len(value.store.list("automation_dispatches")) == 1


def test_scheduler_submission_beyond_grace_does_not_force_stale_occurrence(setup):
    value = setup
    value.store.put("jobs", job("skip", cron="0 0 1 1 *"))
    value.manager.record_scheduled_fire(
        "daily", datetime.now(timezone.utc) - timedelta(minutes=2)
    )
    assert not value.store.list("automation_dispatches")


def test_schedule_dst_repeated_clock_has_distinct_utc_fire_ids(setup):
    value = setup
    saved = value.store.put(
        "jobs",
        job(
            cron="30 1 * * *",
            timezone="America/New_York",
            created_at="2026-11-01T00:00:00-04:00",
            catchup_max_runs=10,
        ),
    )
    state = value.manager.reconcile_job(saved, instant("2026-11-02T12:00:00+00:00"))
    fires = value.store.list("automation_dispatches")
    assert len({entry["id"] for entry in fires}) == len(fires)
    assert len(fires) >= 2
    assert all(entry["observed_at"].endswith("+00:00") for entry in fires)
    assert instant(state["cursor"]) == instant("2026-11-02T12:00:00+00:00")


def local_trigger(value, type="file_arrival", **extra):
    value.store.put(
        "buckets", {"id": "incoming", "name": "incoming", "project_id": "default"}
    )
    root = value.store.directory / "buckets" / "incoming"
    root.mkdir(parents=True, exist_ok=True)
    values = {
        "name": "Arrival",
        "project_id": "default",
        "notebook_id": "n",
        "type": type,
        "source": {"path": "blob://incoming/"},
        "event_parameter": "event",
        "debounce_seconds": 0,
        **extra,
    }
    return value.manager.save_trigger(TriggerInput(**values), value.actor), root


def test_file_arrival_baseline_dedup_restart_and_immutable_admission_identity(setup):
    value = setup
    trigger, root = local_trigger(value)
    old = root / "old.csv"
    old.write_text("old")
    before = instant(trigger["activated_at"]).timestamp() - 10
    os.utime(old, (before, before))
    value.manager.check_trigger(trigger["id"])
    assert value.store.list("automation_dispatches") == []
    new = root / "new.csv"
    new.write_text("new")
    os.utime(new, (before, before))  # A newly copied file may preserve its old mtime.
    value.manager.check_trigger(
        trigger["id"], clock=datetime.now(timezone.utc) + timedelta(seconds=1)
    )
    value.manager.check_trigger(
        trigger["id"], clock=datetime.now(timezone.utc) + timedelta(seconds=2)
    )
    assert len(value.store.list("automation_dispatches")) == 1
    first = value.store.list("automation_dispatches")[0]
    restarted = AutomationManager(value.app)
    restarted.check_trigger(trigger["id"])
    assert len(value.store.list("automation_dispatches")) == 1
    restarted._dispatch_pending()
    value.workflows._tick(value.store.get("workflow_runs", first["run_id"]))
    options = value.runtime.calls[0][1]
    assert options["parameters"]["event"]["objects"][0]["key"] == "new.csv"
    assert options["parameters"]["event"]["event_count"] == 1
    restarted.close()


def test_event_queue_failure_never_checkpoints_unaccepted_source(setup):
    value = setup
    trigger, root = local_trigger(value, include_existing=True)
    (root / "one.csv").write_text("content")
    with value.store.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_events BEFORE INSERT ON automation_events BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    failed = value.manager.check_trigger(trigger["id"])
    assert "IntegrityError" in failed["last_error"]
    with value.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM automation_objects").fetchone()[0] == 0
        db.execute("DROP TRIGGER reject_events")
    value.manager.check_trigger(trigger["id"])
    assert len(value.store.list("automation_dispatches")) == 1


def test_delta_change_scans_commit_metadata_without_loading_spark(setup):
    value = setup
    trigger, root = local_trigger(value, type="table_change", include_existing=True)
    log = root / "_delta_log"
    log.mkdir()
    (log / "00000000000000000003.json").write_text('{"commitInfo":{}}')
    (log / "00000000000000000003.checkpoint.parquet").write_text("not a commit")
    value.manager.check_trigger(trigger["id"])
    dispatch = value.store.list("automation_dispatches")[0]
    assert dispatch["_job"]["parameters"]["event"]["objects"][0]["version"] == 3
    assert len(dispatch["_job"]["parameters"]["event"]["objects"]) == 1


def test_workflow_completion_catches_old_running_row_finished_after_newer_row(setup):
    value = setup
    value.store.put("workflows", {**value.workflow, "id": "target"})
    trigger = value.manager.save_trigger(
        TriggerInput(
            name="After workflow",
            project_id="default",
            workflow_id="target",
            type="workflow_completion",
            source={"workflow_id": "w"},
            event_parameter="event",
            debounce_seconds=0,
        ),
        value.actor,
    )
    clock = datetime.now(timezone.utc) + timedelta(seconds=2)
    for identifier, state in (("older", "running"), ("newer", "succeeded")):
        value.store.put(
            "workflow_runs",
            {
                "id": identifier,
                "project_id": "default",
                "workflow_id": "w",
                "status": state,
                "created_at": "2026-01-01T00:00:00+00:00",
                "finished_at": clock.isoformat() if state == "succeeded" else None,
            },
        )
    value.manager.check_trigger(trigger["id"], clock=clock)
    old = value.store.get("workflow_runs", "older")
    old.update(
        status="succeeded", finished_at=(clock + timedelta(seconds=1)).isoformat()
    )
    value.store.put("workflow_runs", old)
    value.manager.check_trigger(trigger["id"], clock=clock + timedelta(seconds=2))
    assert len(value.store.list("automation_dispatches")) == 2
    with value.store.connect() as db:
        events = [
            json.loads(row[0])
            for row in db.execute("SELECT payload FROM automation_events")
        ]
    assert {item["workflow_run_id"] for item in events} == {"older", "newer"}


def test_trigger_cycles_and_moved_target_project_are_rejected(setup):
    value = setup
    value.store.put("workflows", {**value.workflow, "id": "target"})
    value.manager.save_trigger(
        TriggerInput(
            name="w to target",
            project_id="default",
            workflow_id="target",
            type="workflow_completion",
            source={"workflow_id": "w"},
        ),
        value.actor,
    )
    with pytest.raises(ValueError, match="cycle"):
        value.manager.save_trigger(
            TriggerInput(
                name="target to w",
                project_id="default",
                workflow_id="w",
                type="workflow_completion",
                source={"workflow_id": "target"},
            ),
            value.actor,
        )
    saved = value.store.put("jobs", job())
    moved = value.store.get("notebooks", "n")
    moved["project_id"] = "private"
    value.store.put("notebooks", moved)
    with pytest.raises(ValueError, match="configured project"):
        value.workflows.capture_job(saved, value.actor)


def test_local_scan_is_bounded_and_deleted_trigger_cancels_pending_work(setup):
    value = setup
    trigger, root = local_trigger(value, include_existing=True)
    value.manager.scan_batch = 2
    for index in range(5):
        (root / f"{index}.csv").write_text(str(index))
    value.manager.check_trigger(trigger["id"])
    with value.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM automation_objects").fetchone()[0] == 2
    value.store.delete("triggers", trigger["id"])
    value.manager._dispatch_pending()
    assert all(
        item["status"] == "cancelled"
        for item in value.store.list("automation_dispatches")
    )
    assert value.runtime.calls == []


def test_s3_pages_persist_continuations_and_recheck_revoked_source_access(
    setup, monkeypatch
):
    value = setup
    value.store.put(
        "connections",
        {
            "id": "objects",
            "type": "s3",
            "name": "Objects",
            "project_ids": ["default"],
            "config": {},
            "_secrets": "encrypted-private",
        },
    )
    trigger = value.manager.save_trigger(
        TriggerInput(
            name="S3 arrival",
            project_id="default",
            notebook_id="n",
            type="file_arrival",
            source={
                "path": "s3://incoming/data/",
                "connection_id": "objects",
                "pattern": "*.parquet",
            },
            include_existing=True,
            event_parameter="event",
            debounce_seconds=0,
        ),
        value.actor,
    )
    calls = []

    class Client:
        def list_objects_v2(self, **options):
            calls.append(options)
            second = options.get("ContinuationToken") == "next-page"
            return {
                "Contents": [
                    {
                        "Key": (
                            "data/second.parquet" if second else "data/first.parquet"
                        ),
                        "Size": 42,
                        "ETag": "second" if second else "first",
                        "LastModified": datetime.now(timezone.utc),
                    }
                ],
                **({} if second else {"NextContinuationToken": "next-page"}),
            }

        def close(self):
            pass

    original = value.manager._s3
    monkeypatch.setattr(value.manager, "_s3", lambda *_: Client())
    value.manager.check_trigger(
        trigger["id"], clock=datetime.now(timezone.utc) + timedelta(seconds=1)
    )
    value.manager.check_trigger(
        trigger["id"], clock=datetime.now(timezone.utc) + timedelta(seconds=2)
    )
    assert calls[0]["MaxKeys"] == 500 and calls[1]["ContinuationToken"] == "next-page"
    assert len(value.store.list("automation_dispatches")) == 2
    monkeypatch.setattr(value.manager, "_s3", original)
    record = value.store.get("connections", "objects")
    record["project_ids"] = []
    value.store.put("connections", record)
    failed = value.manager.check_trigger(trigger["id"])
    assert (
        "HTTPException" in failed["last_error"]
        and "encrypted-private" not in failed["last_error"]
    )
    assert len(value.store.list("automation_dispatches")) == 2


def test_automation_api_roles_scoping_and_private_capture_redaction(setup):
    value = setup
    app = FastAPI()
    app.state = value.app.state
    value.store.put("users", {"id": "viewer", "username": "viewer", "role": "member"})
    value.store.put(
        "project_members",
        {
            "id": "default:viewer",
            "project_id": "default",
            "user_id": "viewer",
            "role": "viewer",
        },
    )
    value.store.put(
        "projects",
        {
            "id": "private",
            "name": "Private",
            "connection_ids": [],
            "cluster_ids": ["local"],
        },
    )

    def authenticated(request: Request):
        return value.store.get("users", request.headers.get("x-user", "admin"))

    register_automation_routes(app, authenticated, value.app.state.access)
    with TestClient(app) as client:
        created = client.post("/api/backfills", json=backfill().model_dump(mode="json"))
        assert created.status_code == 201, created.text
        assert "_template_id" not in created.json() and created.json()["snapshot_id"]
        listing = client.get("/api/backfills", headers={"x-user": "viewer"}).json()
        assert listing["total"] == 1 and "items" not in listing["items"][0]
        assert (
            client.post(
                "/api/backfills",
                headers={"x-user": "viewer"},
                json=backfill().model_dump(mode="json"),
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/backfills/{created.json()['id']}/cancel",
                headers={"x-user": "viewer"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/backfills?project_id=private", headers={"x-user": "viewer"}
            ).status_code
            == 403
        )
        assert (
            client.get("/api/triggers", headers={"x-user": "viewer"}).status_code == 200
        )
        assert (
            client.post(
                "/api/triggers",
                headers={"x-user": "viewer"},
                json={
                    "name": "Blocked",
                    "project_id": "default",
                    "notebook_id": "n",
                    "type": "workflow_completion",
                    "source": {"workflow_id": "w"},
                },
            ).status_code
            == 403
        )


def test_schedule_option_validation_and_cancelled_dispatch_stays_cancelled(setup):
    value = setup
    with pytest.raises(ValueError, match="declared"):
        value.manager.validate_job(job(schedule_date_parameter="missing"), value.actor)
    saved = value.store.put("jobs", job("latest"))
    value.manager.reconcile_job(saved, instant("2026-01-06T12:00:00+00:00"))
    dispatch = value.store.list("automation_dispatches")[0]
    assert (
        value.manager.cancel_dispatch(dispatch["id"], value.actor)["status"]
        == "cancelled"
    )
    value.manager.reconcile_job(saved, instant("2026-01-06T12:00:00+00:00"))
    value.manager._dispatch_pending()
    assert value.runtime.calls == []


def test_pause_resume_preserves_event_checkpoint_and_queued_intent(setup):
    value = setup
    trigger, root = local_trigger(value, include_existing=True)
    (root / "ready.csv").write_text("content")
    value.manager.check_trigger(trigger["id"])
    current = value.store.get("triggers", trigger["id"])
    paused = value.manager.save_trigger(
        TriggerInput.model_validate({**current, "enabled": False}), value.actor, current
    )
    assert paused["generation"] == trigger["generation"]
    value.manager._dispatch_pending()
    assert value.runtime.calls == []
    assert value.store.list("automation_dispatches")[0]["status"] == "pending"
    resumed = value.manager.save_trigger(
        TriggerInput.model_validate({**paused, "enabled": True}), value.actor, paused
    )
    value.manager.check_trigger(resumed["id"])
    assert len(value.store.list("automation_dispatches")) == 1
    value.manager._dispatch_pending()
    assert len(value.store.list("workflow_runs")) == 1


def test_current_operator_can_cancel_after_backfill_creator_revoked(setup):
    value = setup
    value.runtime.hold = True
    created = value.manager.create_backfill(backfill(), value.actor)
    advance(value, 2)
    assert len(value.runtime.calls) == 1
    replacement = value.store.put(
        "users", {"id": "owner2", "username": "owner2", "role": "admin"}
    )
    value.store.delete("users", "admin")
    value.manager.cancel_backfill(created["id"], replacement)
    advance(value, 3)
    assert value.store.get("backfills", created["id"])["status"] == "cancelled"
    assert (
        value.store.get("runs", value.runtime.calls[0][1]["run_id"])["status"]
        == "cancelled"
    )


def test_failed_event_admission_acknowledges_event_and_retention_cleans_templates(
    setup,
):
    value = setup
    trigger, root = local_trigger(value, include_existing=True)
    (root / "ready.csv").write_text("content")
    value.manager.check_trigger(trigger["id"])
    moved = value.store.get("notebooks", "n")
    moved["project_id"] = "private"
    value.store.put("notebooks", moved)
    value.manager._dispatch_pending()
    dispatch = value.store.list("automation_dispatches")[0]
    assert dispatch["status"] == "failed"
    with value.store.connect() as db:
        assert (
            db.execute("SELECT status FROM automation_events").fetchone()[0] == "failed"
        )
    value.store.put(
        "automation_templates",
        {
            "id": "orphan",
            "project_id": "default",
            "created_at": "2000-01-01T00:00:00+00:00",
            "_capture": {},
        },
    )
    value.manager.cleanup("2099-01-01T00:00:00+00:00")
    assert value.store.get("automation_templates", "orphan") is None
    assert value.store.list("automation_dispatches") == []
