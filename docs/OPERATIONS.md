# Operations, retention, backups, and notifications

Workspace administrators use **Operations** to inspect resources, set retention, manage local backups, and configure alert destinations. These controls operate on the running workspace; use a disposable deployment to rehearse restore or external delivery tests.

## Resources and memory

Operations shows management memory, local disk availability, active/queued work, and compute health. Compute metrics refresh asynchronously and can temporarily show refreshing, stale, or unavailable states. Notebook run details separately show bounded logs, cell progress, and peak interpreter memory.

Container limits and Spark sizing remain host configuration, rather than live-editable memory controls. Standard Compose defaults are a 1 GB management ceiling, a 4 GB compute ceiling, and a 1 GB Spark driver heap. Limits do not reserve that memory at startup. Idle compute releases its interpreter after 900 seconds by default; `OPENBRICKS_IDLE_TIMEOUT_SECONDS=0` disables idle shutdown. Saved notebooks/data persist; Python variables and temporary views reset.

Use [DEPLOYMENT.md](../DEPLOYMENT.md) for memory limits, remote compute, Docker Desktop/WSL reclamation, and host configuration. Independent remote targets need their own compatible images, volumes, and storage access.

## Retain useful history

| Policy | Default | Effect |
| --- | --- | --- |
| Keep cell outputs | 30 days | Clears older terminal-run outputs, logs, and recorded data previews/events |
| Keep run records | 90 days | Removes older finished notebook/workflow history and associated aged audit/data/notification history |
| Output storage budget | 500 MB | Clears older terminal-run output when the retained budget is exceeded |
| Keep newest backups | 5 | Removes older completed local backup artifacts during cleanup |

Run-history retention must be at least as long as output retention. Active and queued runs are preserved. Notebook source revisions have a separate newest-50-per-notebook bound.

Save the policy, choose **Preview cleanup**, inspect the output/run counts and estimated bytes, then choose **Apply cleanup** if appropriate. The preview uses the saved policy. A background cleanup also runs approximately hourly while management is running. Clearing outputs preserves the remaining run metadata until run-record retention expires; it does not delete your persisted datasets. The estimate describes record payloads, rather than a guarantee of immediate filesystem reclamation by SQLite.

## Create and download a backup

1. Finish queued/running work and save notebook edits.
2. Choose **Create backup**, supply a name, and optionally set an encryption passphrase of at least 12 characters.
3. Wait for the backup to succeed, then **Download** it to a separate protected location.

Backup runs in a maintenance window: the scheduler pauses, in-flight workspace mutations drain, and the local interpreter resets before local warehouse files are copied. Do not depend on live Python state surviving a backup. Remote compute services are not reset or copied by this operation.

The UI includes workspace metadata, saved notebook files, Git checkouts, wheel artifacts/environments, local objects, the local warehouse/metastore, and the local home directory. Existing `secrets.key` is included. Archives can therefore contain connection credentials, Git credentials, notebook output, and sensitive local data. An archive without a passphrase is **not encrypted**, even though individual saved connector secrets are encrypted in its metadata.

The backup excludes external S3/SeaweedFS service data, external Unity Catalog databases, remote compute volumes, server environment variables, and Docker images. The optional services' separate volumes need their own backup process. If `OPENBRICKS_SECRET_KEY` supplies the encryption key externally, preserve that exact value outside the archive. Preserve `.env`, deployment configuration, compatible runtime images, and any external services separately.

The archive format currently supports at most 100,000 entries and 20 GiB of uncompressed file content. Creation size and restore upload size are separate limits: a successfully created archive may exceed the UI's configured upload allowance.

## Restore carefully

Restoring replaces local workspace metadata and included local data; it is not a merge. Export anything newer that you need and make a fresh backup before restoring another one.

1. Finish work and keep other users out of the workspace during maintenance.
2. Choose **Restore**, select the downloaded archive, and enter its passphrase if encrypted.
3. Confirm the replacement. The server validates the archive, paths, checksums, database, and saved credential key before swapping local content.
4. Restart management and local compute before resuming work. In the standard deployment:

```console
docker compose restart openbricks compute
```

The managed management process may restart automatically, but still verify both services and sign in again if required. A failure after execution services have shut down also requires a reload, even if previous files were recovered. Keep the `pre-restore-*` recovery directory until the restored workspace has been verified; follow the returned recovery message if rollback was incomplete. Do not use the workspace while it reports that a restart is required.

Restore uploads are bounded by `OPENBRICKS_MAX_UPLOAD_MB`, default `100`. The UI proxy's upload allowance is configured when the web image is built, so raising only the running API variable is insufficient. Rebuild/redeploy management with matching upload configuration before attempting a larger archive; preserve its current data volume. No larger-file restore path is provided by the UI. Allow disk space for the uploaded archive, decrypted/staged content, and the previous workspace copy.

External data/catalog state and remote compute data are unchanged by restore. Recheck account access, named connections, schedules, package manifests, and external data consistency before running restored pipelines. A local workspace backup is not a transactionally coordinated backup of all those systems.

## Configure notifications

Under **Operations → Notifications**, create an email (SMTP) or webhook target for the current project. Set the events—failed, slow, and/or successful runs—and enable delivery. Slow-run alerts default to 300 seconds; the configurable threshold is 30–86,400 seconds while a run is still running.

Webhook URLs, including secret paths/query values, are encrypted at rest. The UI displays only their origin; leave the URL blank while editing to preserve the stored destination. Optional bearer tokens and SMTP passwords are also encrypted, and blank secret fields preserve stored values. HTTPS is the default expectation; HTTP requires an explicit trusted-local-endpoint option. SMTP supports STARTTLS.

Choose **Send test** only when ready to deliver a real message to that destination. Then select the target in a notebook job or workflow configuration. Targets must belong to the same project. Creating a target alone does not subscribe every interactive notebook run to it.

Delivery is bounded and best effort, with persisted deduplication and retry state. Destination outages do not stop execution or scheduling. The notifier scans a bounded recent window, so it is not a replacement for a large-scale external incident/monitoring system. Delivery tests and mocked connector coverage do not establish that a particular live SMTP, webhook, or SQL Server deployment has been validated.

See [projects and access](PROJECTS.md), [workflow recovery](WORKFLOWS.md), and the [verification record](../VERIFICATION.md).
