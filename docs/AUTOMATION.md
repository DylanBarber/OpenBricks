# Backfills, schedule recovery, and event triggers

Open **Automation** in the selected project to create date-range runs, inspect missed schedules, and watch for new input. Viewers can browse records, operators can preview/start/cancel work and check existing triggers, and editors/owners can maintain trigger definitions. Current project, compute, and connection permissions are checked again before execution. Automation stores a run-as account ID, not an API token or a copy of decrypted credentials.

## Reprocess a date range

1. Declare a notebook or workflow input such as `run_date` with type `date`. A declared string input also works if it accepts ISO dates.
2. Choose **New backfill**, select the target and date input, and choose an inclusive start/end date.
3. Set other typed overrides, the maximum number of concurrent dates, and whether a failure should stop later dates.
4. Choose **Preview runs**. Inspect the date count before choosing **Create backfill**.
5. Open the backfill to inspect each date and its workflow/notebook logs. Cancellation stops pending dates and waits for running work to acknowledge cancellation.

Each backfill contains one to 366 dates and permits one to eight concurrent dates. Actual execution remains subject to the compute host's resource and concurrency budgets. One is the default. At most 25 backfills can be active in a workspace.

Creation captures the entire target definition, notebook source, resolved task structure, and immutable wheel manifests once. Later edits, Git imports, and package installations do not alter the batch. Each date gets a durable admission ID and its own typed date value; a management restart or lost acknowledgment reuses that ID. A backfill does not snapshot external SQL databases or datasets. Its captured notebook code can still perform writes, so choose the intended dates deliberately.

The default failure policy stops admitting new dates after a failed date; already admitted dates can finish. Turn it off for independent dates that should continue. Failed/uncertain work is not automatically rerun by the backfill coordinator. Existing workflow retry policies still apply and exclude unknown execution outcomes.

Example preview/create payload (POST `/api/backfills/preview`, then `/api/backfills`):

```json
{
  "name": "January orders",
  "project_id": "default",
  "workflow_id": "YOUR_WORKFLOW_ID",
  "date_parameter": "run_date",
  "start_date": "2026-01-01",
  "end_date": "2026-01-31",
  "parameters": {"region": "North"},
  "max_concurrency": 1,
  "stop_on_failure": true
}
```

Use exactly one `workflow_id` or `notebook_id`. The target must declare the selected date input. Preview validates every date and parameter combination without creating a package environment or starting compute.

## Recover missed schedules

Configure recovery in **Jobs & schedules**:

| Policy | Behavior |
| --- | --- |
| `skip` (default) | Skip missed historical occurrences. A current occurrence observed within 60 seconds can still run normally. |
| `latest` | Queue the latest occurrence within the lookback window and skip older missed occurrences. |
| `all` | Queue each occurrence in chronological order within the lookback window, respecting the per-job pending cap. Remaining work continues to reconcile as that queue drains. |

The lookback is one to 168 hours; the per-job queue cap is one to 366 occurrences. These bounds prevent a long outage from flooding a small server. To reprocess older periods, create an explicit backfill. **Automation → Missed schedules** shows the checked-through cursor, remaining occurrences, and skipped count.

Set a **scheduled date parameter** to process the occurrence's date in the schedule's timezone. Without it, existing static job overrides retain their behavior. For example, a missed January 4 daily schedule receives `2026-01-04`, even if recovery takes place on January 6.

Schedule fire IDs include the job, schedule identity, and UTC occurrence. Reconciliation and normal scheduler callbacks deduplicate against the same IDs, and executions of one scheduled job do not overlap. Fire intent is durable before its cursor advances. Pausing/removing a job prevents pending admissions. Changing its cron, timezone, enabled state, project, or scheduled-date input resets the schedule identity. Cron evaluation follows the existing timezone/DST rules; UTC occurrence records distinguish repeated wall-clock times during a fall transition.

API fields are `catchup_policy`, `catchup_window_hours`, `catchup_max_runs`, and optional `schedule_date_parameter` on a job. Recovery state is available at GET `/api/schedules/catchup?project_id=...`; individual admissions appear at GET `/api/automation/runs`.

## Start pipelines from events

Choose **New trigger**, then select a target notebook/workflow and one of these sources:

| Trigger | Source |
| --- | --- |
| File arrival/update | A local `blob://bucket/prefix/` or an S3-compatible `s3://bucket/prefix/`, optionally filtered by `*.csv`, `*.parquet`, or another path pattern. |
| Delta table change | A Delta table root at a local blob/S3 path. The watcher reads numbered JSON commits in `_delta_log`; it does not start Spark. |
| Workflow completion | A saved upstream workflow in the same project, with selected terminal states: succeeded, failed, or cancelled. Dependency cycles between workflow-completion triggers are rejected. |

Local source buckets must be registered in the project and targets must use the bundled shared local compute. Remote compute can use an explicitly selected project-available S3 connection. The management server must be able to reach that S3 endpoint. SQL Server change capture is configured through the ingestion tools; table-event watchers here watch Delta commit metadata.

Intervals are minimum intervals of 15 seconds or more; bounded polling and queue load can delay a check. The stable-input delay allows file writes to settle before an event is accepted. Workflow completion events use the same delay after completion. **Check now** performs a real check and can start the configured pipeline. Choose an input prefix separate from outputs to avoid generating repeated input events.

By default, existing objects/completions from before source activation are ignored. Enable **Include existing data** to process the existing source. Pausing/resuming or renaming a trigger preserves its fingerprints and queued intents. A changed source starts a new observation generation and cancels unadmitted old-generation events. Already running work continues. Normal edits preserve the original run-as account; recreate the trigger under another account to change that identity.

Choose an optional declared **JSON event input** to receive metadata in `params`, for example:

```python
event = params["event"]
for item in event.get("objects", []):
    print(item["key"], item.get("version"))
# For a Delta event batch, choose max(item["version"] for item in event["objects"])
# if the pipeline should read the newest reported version explicitly.
```

File/Delta payloads contain `type`, `source`, `objects`, and `event_count`. Each object includes its bucket-relative key, size, modification time, fingerprint, and (for Delta commits) version. Workflow-completion payloads use `upstream_runs`, including the upstream workflow/run IDs, status, and finish time. Metadata is batched to fit the existing bounded JSON parameter allowance.

Source observations and event intent commit together in SQLite. A lost compute acknowledgment cannot discard an accepted event: dispatches reuse their durable child IDs. Source permissions, run-as membership, and target project are rechecked. Pending event/admission queues are bounded at 1,000; a full queue leaves unaccepted source changes available for a later scan. At most 1,000 triggers can be configured. The system polls metadata rather than providing a storage transaction log: it can miss a file that appears and disappears between checks or intermediate overwrites. It does not promise exactly-once external writes or snapshot datasets at event time.

Trigger API payload example:

```json
{
  "name": "Incoming orders",
  "project_id": "default",
  "workflow_id": "YOUR_WORKFLOW_ID",
  "type": "file_arrival",
  "source": {"path": "s3://incoming/orders/", "connection_id": "YOUR_S3_CONNECTION_ID", "pattern": "*.parquet"},
  "parameters": {},
  "event_parameter": "event",
  "interval_seconds": 30,
  "debounce_seconds": 10,
  "include_existing": false,
  "enabled": true
}
```

POST/GET `/api/triggers`, PUT/DELETE `/api/triggers/{id}`, POST `/api/triggers/{id}/check`, and GET `/api/triggers/{id}/events` manage/check the source. Source credentials remain in saved connection records. Lists and history are paginated; target parameter metadata is fetched separately from notebook cell source. Backfill, automated-run, and observer metadata are covered by management-volume backups and the operations retention integration; external source data still needs its own backup.
