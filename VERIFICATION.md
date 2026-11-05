# Verification

## Public documentation and branding update

- The expanded README and focused Git guide were checked against the actual clone/import/build/runtime code. Re-import overwrite behavior, branch/tag limits, private Git credentials, and local versus remote storage are documented explicitly.
- The standalone example project built and installed its wheel without dependency downloads. Its packaged synthetic CSV, import name, license metadata, and all five notebook cell boundaries were checked.
- A disposable management container cloned the starter from a local HTTPS Git fixture with a temporary trusted certificate, imported its nested folders, built/installed its wheel, executed the full Spark pipeline twice, queried its persistent table from SQL, and saved the documented Basic schedule. Expected completed revenue was 85.50 on both runs.
- The starter's own verification tool executed all five imported notebooks through the actual worker, then repeated the complete pipeline successfully in temporary data. No production notebooks or datasets were used.
- The new original SVG logo, wordmarks, README banner, social card, favicon, and shared UI geometry were generated together. SVG XML parsed successfully; PNG exports and browser screenshots were inspected. TypeScript and the optimized Docker web build passed.
- Browser checks confirmed the new logo in the real UI and the synthetic report output. The README screenshots are tracked under `docs/assets/`, separate from ignored local QA artifacts.
- Public documentation links and fenced blocks were checked. Curated external files passed the notebook parser; their code is not claimed executed. See `docs/EXAMPLE_REPOSITORIES.md` for pinned sources and compatibility notes.
- The screenshot review exposed a missing dark background on notebook cell headers and a SQL-file picker omission. Both small UI corrections are included.

## OpenBricks v0.2

Validation used disposable QA data, separate from an existing workspace used for the upgrade check. The browser UI and authenticated API ran through the real Next.js proxy, with management, compute, SeaweedFS, and Unity Catalog in separate containers. A second authenticated compute container used an independent data volume to test remote routing.

### Completed checks

- Web TypeScript checks passed. The existing voting application's TypeScript check also passed.
- The final Linux image passed **55 backend tests** in 87.58 seconds under a 1 GB test-container ceiling. An isolated local Python run passed the earlier 50-test suite; all 25 runtime, cluster, and idle-memory checks also passed after the idle policy was added.
- Both final Docker image targets built successfully, including the optimized Next.js build and its TypeScript check.
- The Compose deployment passed its authenticated connection check after an in-place upgrade. Its existing 2 notebooks and 3 run records remained available. A SQLite backup was created in the persistent data volume before replacement.
- Anonymous requests were rejected; cookie login and authenticated API access worked through the UI proxy.
- Nested notebook folders, moves, cycle validation, notebook placement, breadcrumbs, and paginated metadata worked.
- Real Python and Spark SQL ran in the separate compute service. Individual cells reused interpreter state.
- Basic weekly scheduling produced the expected cron and timezone. Pause, run-now, and Advanced cron scheduling worked.
- Real SeaweedFS passed bucket creation, object upload/download, prefix browsing, and continuation pagination.
- The second compute agent accepted registration and authentication testing, ran Spark SQL, and accessed SeaweedFS through named connectors.
- An uploaded wheel replicated to that agent and imported in both the Python driver and a Spark Python UDF.
- Notebooks on different compute targets ran concurrently. Each target retained its own execution queue.
- Notebook-grouped history, per-notebook pagination, bounded overview payloads, and schedule pagination/search passed. A 610-job regression confirms schedules beyond overview's 500-item bound remain reachable.
- Real Unity Catalog 0.7 catalog/schema/external-table registration worked. Three-part Spark SQL over registered local Parquet returned 42.
- Native Spark S3A wrote Parquet to SeaweedFS. A Unity Catalog table at that S3 location returned 42 through three-part Spark SQL.
- Browser checks confirmed Basic/Advanced scheduling, grouped history, compute targets, and catalog browsing in Dark mode. Current public UI evidence is tracked at `docs/assets/workspace-dark.png` and `docs/assets/workspace-folders-dark.png`.
- Regression tests cover lost acknowledgements, transient polling failures, cancellation, and management reattachment without resubmitting acknowledged work.
- Saved connector credentials and compute tokens are encrypted. Tests verify plaintext connector secrets do not enter persisted run records or public API responses.

### Memory policy

Management is limited to 1 GB, compute to 4 GB, SeaweedFS to 512 MB, and the optional catalog to 1 GB. Matching RAM-plus-swap ceilings prevent additional swap allocation. Spark's driver heap defaults to 1 GB with two local execution threads.

The Windows validation host used a 6 GB WSL ceiling and autoMemoryReclaim=dropCache. After build/QA cleanup, with all four normal OpenBricks services running idle, Windows reported **2.28 GB** for vmmemWSL. This is one host's measurement rather than a memory guarantee for other workloads. Container maxima are ceilings, not reserved memory, and share the host-wide WSL budget. Docker inspection confirmed all four configured container RAM and RAM-plus-swap limits. See the deployment guide for portable host configuration.

Compute releases its Python/Spark interpreter after 15 idle minutes when no work is active or queued and no package environment change is in progress. The next run creates a fresh interpreter. Temporary variables and views reset; saved notebooks, files, tables, wheels, and run records remain. OPENBRICKS_IDLE_TIMEOUT_SECONDS=0 disables this policy. Tests exercise actual process exit/restart, data preservation, work-in-progress guards, and disabling the policy.

A real final-image Spark check used a disposable compute container with a three-second idle interval. Spark SQL returned 42, then the running Spark JVM exited automatically. The test container and all v0.2 QA containers/volumes were removed afterward. The normal deployment uses the 15-minute setting.

The passing backend suite emits two non-fatal warnings: the test client's deprecated AnyIO alias and Pydantic's inherited schema name being shadowed by the SQL insert request field.

### Limits of validation

No SQL Server endpoint or credentials were supplied. Tests cover ODBC configuration, parameterized reads, execution, transactional inserts, identifier quoting, API behavior, and credential protection using a fake driver. The Linux image contains Microsoft's ODBC Driver 18. A live SQL Server integration is not claimed.

The remote-compute protocol was tested with a separate container and volume on this Docker host. Connectivity, TLS, and dataset access on an independently hosted machine depend on that machine's configuration.

Bundled Unity Catalog uses a private Docker network and static credentials for SeaweedFS compatibility. This validates shared metadata and Spark access, not enterprise governance, per-user storage permissions, or short-lived scoped IAM credentials.

Original v0.1 checks covered real Git clone/pull, Databricks/Jupyter import/export, wheel build/install from Git, scheduled execution without a browser, a 12 MB multipart upload, and persistence after container recreation. They remain evidence for the initial implementation; the v0.2 checks cover the changed architecture.

### Reproduction

Build both targets with `docker compose build openbricks compute`. Run backend tests in a disposable container without deployment environment variables:

```powershell
docker run --rm --memory=1g --memory-swap=1g --entrypoint python openbricks:local -m pytest -q
```

For full integration, use an isolated Compose project with separate ports, volumes, and test credentials. Enable storage,catalog profiles, add a second compute container on its network with its own volume and token, and prepare a wheel from examples/brickkit. Copy and execute scripts/v2-smoke.py and scripts/catalog-smoke.py in that QA management container. These scripts create fixtures; do not run them against a real workspace. See README.md and DEPLOYMENT.md for configuration.
