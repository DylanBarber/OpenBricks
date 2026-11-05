# Reproduce the stable-release system tests

Run the complete application against fresh local services with one command:

```sh
python -m pip install ./cli
python scripts/run-system-smoke.py
```

This builds the runtime and compute images from your checkout, starts a disposable installation, exercises the real HTTP APIs through the web proxy, restarts services at defined checkpoints, and restores an encrypted backup into a second fresh installation. A failed assertion, timeout, child process, or cleanup returns a nonzero exit code. No production installation or cloud account is required.

Use Python 3.10 or newer, Git, and Docker Compose v2 with support for `up --wait`. Docker must run Linux amd64 containers; SQL Server's Developer image is amd64 and requires acceptance of Microsoft's development/test license. The fixture accepts its EULA only for this disposable test. Internet access is required for image pulls, the initial build, and cloning the public OpenBricks example repository. The Git and remote-wheel proofs deliberately verify a real public clone; this is not an offline test.

Allow at least 8 GB of memory for Docker and about 20 GB of free disk space for images, temporary databases, and encrypted recovery archives. The full sequence can take 45–90 minutes on a developer machine, particularly with an uncached image build. The runner prints the active phase and a heartbeat while longer checks run. It runs Spark phases sequentially, stops SQL Server before remote-worker tests, and stops the source worker before starting the fresh recovery host. Each container also has a hard memory and swap ceiling in [compose.smoke.yaml](../compose.smoke.yaml).

To inspect the complete sequence without Docker or generated credentials:

```sh
python scripts/run-system-smoke.py --plan
```

To test already built images, supply both image references:

```sh
python scripts/run-system-smoke.py \
  --runtime-image ghcr.io/dylanbarber/openbricks:0.4.0 \
  --compute-image ghcr.io/dylanbarber/openbricks-compute:0.4.0
```

In PowerShell, put this command on one line or use PowerShell's backtick continuation. Supplied runtime and compute images must have the same build identity and contain the corresponding backend tests. Running images from another revision against a modified checkout is not a valid release proof.

## Isolation and cleanup

The namespace `openbricks-smoke` and loopback ports `3001`, `3002`, and `8025` are reserved for this harness. **Every run removes existing containers and volumes in that namespace before starting.** Do not use that namespace for anything you intend to keep. The regular OpenBricks project, port `3000`, and production volumes are never targeted. Running a normal OpenBricks installation alongside the harness is supported if enough memory remains available; stopping it yourself may free capacity.

The runner accepts only the fixed disposable origin `http://127.0.0.1:3001`. It refuses another project or origin, ignores normal `OPENBRICKS_*` and `COMPOSE_*` configuration, and uses an explicit generated environment file instead of your `.env`. Before mutation, it validates the rendered Compose project, loopback ports, named volumes, networks, and read-only configuration mounts. A conflicting process on a reserved test port causes a failure. It never runs a global Docker prune or deletes host data directories.

Credentials are generated afresh for the test administrator, compute services, object storage, SQL Server, and recovery host. The credential template [docker/smoke.env.example](../docker/smoke.env.example) documents the Compose inputs; the runner does not use its placeholder passwords. SQL bootstrap creates uniquely marked databases and separate least-privilege read/write logins. Its cleanup verifies ownership before dropping those fixtures.

Normal completion, a failed phase, Ctrl+C, and SIGTERM all attempt to capture service logs and remove only this Compose project's containers, networks, and volumes. An abrupt machine shutdown, Docker outage, or forced process kill can prevent cleanup. To remove leftovers manually:

```sh
docker compose --project-name openbricks-smoke --file compose.smoke.yaml \
  --env-file docker/smoke.env.example --profile '*' \
  down --volumes --remove-orphans --timeout 30
```

The committed placeholders are sufficient for this cleanup because Compose only needs to render its configuration; they do not authenticate to the services. CI also runs this cleanup in an `always()` step.

## What the full sequence proves

| Area | Real assertions |
| --- | --- |
| Runtime and storage | Stateful notebook execution, fresh job workers, queue/resource behavior, DuckDB operations, encrypted off-host archives, authenticated restore drills, wrong-passphrase and tampered-archive rejection. |
| Microsoft SQL Server | Packaged ODBC and JDBC drivers; parameter binding; Unicode, decimal, binary, null, date/time and offset fidelity; commit/rollback; read-only denials; bounded previews; composite-key Change Tracking into Delta; replay, source reset, schema and retention failures. |
| SQL checkpoint integrity | Both `test_sqlserver_live.py` and `test_sqlserver_stable.py` run against the real SQL Server. The latter injects a failure after an actual Delta merge and verifies the checkpoint advances only when the complete notebook succeeds. The ordinary worker and management services stop while this separate test worker runs. |
| Accounts and projects | Cookie authentication, scoped project permissions, service-account tokens, rejected out-of-scope actions and immediate token revocation. |
| Workflows and history | Dependencies, retries, repair, cancellation, paginated run history and schedules, and bounded metadata responses. The default history proof creates 24 real runs and 121 disabled schedules. |
| Git and delivery | Real public-repository import, notebook export, conflict detection, local branch/stage/commit operations, CLI pack/plan/deploy, promotion with captured source and environments, and scoped automation credentials. The proof does not push or create pull requests. |
| Automation | Date backfills, file arrival, upstream completion, deduplication, catch-up configuration and real Delta commit triggers. |
| Independent compute | A second worker with its own data volume, authenticated remote routing, local/remote overlap, wheel replication, and imports in Python drivers and Spark UDF workers. |
| Unity Catalog | Actual local-Parquet and native S3A reads through registered tables and three-part Spark SQL, including the remote worker. |
| Resource enforcement | A detached child exceeds its 256 MB allocation and is stopped while a separate 512 MB interactive notebook retains its state. |
| Notifications | Local non-relaying SMTP plus an HTTP webhook that rejects the first attempt. A watcher observes that rejection, restarts management before the accepted retry, and verifies `rejection < container StartedAt < acceptance` with one durable delivery ID and no duplicate acceptance. Merely restarting after delivery cannot pass this check. |
| Scheduling | Basic weekday/timezone validation and an Advanced cron schedule persisted across management restart. Verification waits for a new real minute occurrence, checks UTC date injection, then disables the synthetic schedule. |
| Compute interruption | A notebook commits one side-effect marker, then compute is restarted during execution. The interrupted run becomes terminal, its side effect remains exactly once, no automatic replay appears, and a fresh worker accepts a new run. |
| Fresh-host recovery | A new encrypted backup includes selected S3 objects, catalog metadata, an exact local blob, and a real Delta table. A separate empty host restores it, restarts management and compute, accepts the restored source account, recovers selected objects/catalog metadata into separate services, checks exact bytes and hashes, excludes unselected data, and safely repeats the recovery. A new job then verifies restored notebook source, blob SHA-256, Delta schema/rows, and real Spark SQL. |

The SMTP/webhook receiver only stores synthetic messages locally. It does not relay email or contact third-party webhook services. S3-compatible storage uses local SeaweedFS. Unity Catalog and the SQL Server run entirely in the disposable Docker network; SQL Server is not published on a host port.

## Receipts and diagnosis

The final line identifies a new directory such as `.test-data/system-smoke/20261105T120000Z-abc12345`. It contains per-phase logs, `summary.json`, platform/SQL/Git/automation/delivery receipts, a precise notification-restart receipt, the generated environment, SQL credentials, and encrypted backup artifacts. These are **private test artifacts**: recovery receipts include generated backup passphrases, so the encryption is not a reason to publish them.

The directory is Git-ignored. On Unix the runner sets owner-only directory/file permissions. Windows inherits the workspace's access controls; use a workspace restricted to your account. Do not commit, upload, or paste the full directory into an issue. CI deliberately uploads no logs or receipts from this harness. Console output contains phase names and status, with child output redirected to the local logs.

On failure, inspect the last failed phase in `summary.json`, then its named log and the captured service logs. Retain only a manually reviewed, redacted excerpt when reporting a bug. The runner stops at the first failing phase and still cleans up; start a fresh run after fixing the cause. It intentionally does not resume half-mutated SQL failure fixtures or an already restored host.

This is an integration and recovery gate, not a benchmark or a substitute for browser accessibility/visual testing, external SMTP/TLS configuration, Windows-native execution, distributed Spark, production restore drills, or a security audit. It validates a trusted-team, single-management-owner deployment with Linux containers. Container memory caps and the sampled process-tree guard do not provide an untrusted-code sandbox.

## CI and release gate

[The Checks workflow](../.github/workflows/ci.yml) builds both images with layer caches and runs the same public command. Backend, real Spark/Delta, SQL Server, web build, and CLI package checks remain separate jobs. The release workflow calls the complete Checks workflow first, so a stable tag cannot publish its images and release assets while the system smoke job fails.

The narrower SQL harness is also reproducible independently:

```sh
# Set OPENBRICKS_SQLSERVER_PASSWORD to a strong disposable password first.
docker compose --file compose.sql-tests.yaml up --build \
  --abort-on-container-exit --exit-code-from tests
docker compose --file compose.sql-tests.yaml down --volumes --remove-orphans
```

It runs both real SQL test files in the reserved `openbricks-sql-tests` project. Use a fresh password supplied through your environment, rather than recording credentials in a command history. Cleanup is required even when the test command fails.
