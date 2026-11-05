# Changelog

## 0.4.0 — 2026-11-05

First stable release of the 0.4 feature set, validated against a disposable SQL Server and the complete containerized application.

- Fix typed SQL Server inserts for precise decimals, Unicode, and nulls; retain transactional batch rollback.
- Preserve SQL temporal values in native results and support exact SQL `time` text in Delta synchronization; reject timestamp keys beyond Spark precision.
- Fix concurrent wheel environment publication, overlapping backup/restore maintenance, and interrupted local backup recovery.
- Add real-server regression tests and a reproducible whole-system test gate covering remote compute, permissions, automation, resource recovery, storage, catalog, notifications, and fresh-host restore.
- Publish matching stable application, compute, and CLI versions. Default management/compute memory caps remain 1 GB / 4 GB.

See [release notes](docs/releases/0.4.0.md) and [system testing](docs/STABLE_TESTING.md).

## 0.4.0-beta.1 — 2026-11-05

- Independent notebook/user worker processes, fresh job sessions, memory and concurrency budgets, project priorities, and session controls.
- Git authoring with branches, file edits/diffs, notebook export, commits/pushes, pull requests, and version-checked import conflict review.
- Captured-source date backfills, durable missed-schedule recovery, file/Delta/workflow event triggers, and paged automation history.
- Declarative Git project bundles, reviewed deployment plans, captured promotion, CLI wheel, and scoped expiring service-account tokens.
- SQL Server Change Tracking synchronization into owned Delta sinks, schema and checkpoint safety checks, and real SQL Server ODBC/JDBC/Delta integration tests.
- Encrypted scheduled S3 backups, selected object/catalog exports, isolated restore drills, bounded remote recovery, and additive recovery plans.
- DuckDB CSV/JSON/Parquet queries and saved source bindings without Spark startup.

The default Docker ceilings remain 1 GB management and 4 GB compute. Workers use a process-tree memory guard within the compute container; they share the host account/filesystem. Local Hive-backed Spark workers use a single-owner lease. Change Tracking and external recovery require the setup described in the focused guides. See [release notes](docs/releases/0.4.0-beta.1.md).

## 0.3.0-beta.1 — 2026-11-05

Pre-release source changes. Published images and release artifacts are outputs of the release workflow; check the actual release before relying on registry availability.

### Added

- Typed notebook/workflow parameters, manual and scheduled overrides, and a limited `dbutils.widgets` compatibility helper.
- Multi-notebook dependency workflows with per-step compute, timeouts, explicitly safe retries, step history, cancellation, captured-source reruns, and failed-branch repair.
- Immutable Git notebook releases and content-addressed wheel environments, source/environment comparisons, runtime compatibility checks, and run provenance.
- A CodeMirror editor with Python/SQL/Markdown highlighting, completion suggestions, formatting, multiple notebook tabs, notebook-wide find/replace, and bounded source revision history/restore.
- Project membership roles, project-scoped application access, assigned compute, and explicit notebook connection selections.
- A SQL/data workspace with saved queries, paged history and metadata browsing, previews/charts, quality reports, recorded lineage, Delta inspection/maintenance, and key-based incremental SQL Server ingestion.
- Bounded live execution logs, cell progress, peak interpreter memory, and classified run errors.
- An administrator operations screen with resource health, output/run retention, optional encrypted local workspace backups, restore, and encrypted webhook/SMTP notification targets.

### Changed

- Notebook/workflow runs capture source, resolved parameters, and exact wheel manifests instead of relying on whatever package set is installed later.
- Remote credential dispatch uses the notebook's selected, project-available connections; an empty selection sends no saved connector credentials.
- Schedules can target workflows as well as notebooks and preserve parameter/environment choices.
- Workspace, workflow, run, query, and metadata browsing use bounded or paged responses.
- Unknown execution outcomes are reconciled through durable run records and are excluded from automatic task retries.

### Boundaries

- Project permissions organize trusted teams; compute remains trusted code execution, with no untrusted-user isolation or full Databricks/API compatibility.
- Snapshots do not capture external datasets, current credentials, remote services, or catalog state. Local backups exclude external storage/catalog databases, remote compute data, server environment, and runtime images.
- Incremental ingestion is watermark-based rather than CDC; source deletions and late rows below the saved watermark are not replicated.
- Live SQL Server validation remains outside the current verification record. See [VERIFICATION.md](VERIFICATION.md) and the [data guide](docs/DATA_WORKSPACE.md).
