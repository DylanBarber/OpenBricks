# Run diagnostics and Spark concurrency

Open a notebook or workflow run, then expand **Run profiler**. The panel loads
only when opened. Running profiles refresh every two seconds; comparison choices
use pages of 25 run summaries rather than loading an entire notebook history.
Both runs must belong to the same notebook and project, and the caller must have
read access to both projects.

## Understand where time and memory went

- **Queued** measures the interval before compute admission. Resource policies
  still enforce concurrency and project memory budgets.
- **Catalog wait** measures waiting for a different worker to release the local
  Hive metastore. This can occur after a worker has been admitted.
- **Python**, **Spark JVM**, and **other processes** show sampled RSS for the
  current notebook process tree. RSS can count shared pages more than once.
- **Whole container** is the cgroup's current memory, including other workers and
  filesystem cache. It is not the host's or Docker Desktop VM's RAM usage.
- **CPU** measures process-tree CPU time per wall-clock interval. 100% represents
  one fully used core; a multicore worker may exceed 100%.

Unavailable cgroup counters are absent values, never silently substituted with
host memory. Process sampling runs four times per second for the existing memory
guard. The plotted series starts at one-second intervals, retains at most 240
points, and downsamples older points for long runs. Exact sampled peaks are kept
separately. Very short spikes between measurements can still be missed; the
container memory ceiling remains the final outer limit.

Spark diagnostics include job and stage identifiers, task counts, stage timing,
executor CPU, input/output bytes, shuffle read/write, and memory/disk spill.
Query plans include displayed DataFrames and retained SQL executions associated
with the run's job group, including SQL writes. Code that launches Spark work on
its own Python threads must propagate Spark local properties to retain that
association. A Spark application that was never started displays **not started**.
Metrics that Spark no longer retains or cannot provide display **unavailable**.

The adapter targets the packaged Spark 4.0.1 status-store APIs. It reads the
in-memory Spark status store through the existing Python/JVM connection; it does
not expose a Spark UI port or create raw Spark event-log archives. Collection
errors never fail an otherwise successful notebook. The collected profile is
bounded to 64 jobs, 128 stages, and 16 query plans of at most 16 KiB each. Spark's
own retained job/stage/SQL counts are bounded separately. See Apache Spark's
[monitoring documentation](https://spark.apache.org/docs/4.0.1/monitoring.html).

Profiles travel through the same known-secret redaction as run output. Native
Python subprocess/JVM stderr is also redacted before the bounded host worker log
is written. Profiles count toward output retention and expire with other run
outputs. Legacy runs and runs whose outputs have expired may have no profile.

### Retention on each compute host

**Operations** controls the management server's run-output retention. Each compute
host owns a separate run database and applies its own policy automatically:

```dotenv
OPENBRICKS_COMPUTE_OUTPUT_RETENTION_DAYS=30
OPENBRICKS_COMPUTE_OUTPUT_BUDGET_MB=500
```

Set these variables on every local or remote compute container. They govern
retained compute run payloads, including profiles and execution-source snapshots;
they do not change the management server's notebook history or its policy.
The defaults are 30 days and 500 MiB. Valid ranges are 1–3650 days and
1–100000 MiB. Invalid values prevent compute startup.

Cleanup runs at startup and every five minutes. It expires old terminal outputs
first, then the oldest terminal outputs until the byte budget is met. Queued and
running work is always preserved. Newly completed runs have a one-minute settling
period. Each pass migrates at most 100 legacy records and compacts at most 100
terminal records, so a large existing backlog converges over successive passes.
Active runs, the settling period, or a backlog can temporarily exceed the budget.
Byte accounting includes retained run JSON; compacted identity records are
excluded. SQLite reuses cleared pages; cleanup does not promise an immediate
decrease in the database file's size on disk.

Cleanup removes outputs, logs, profiles, data-event results and heavy private
execution snapshots. It keeps the run ID, notebook identity, final status,
timestamps, provenance and duplicate-request metadata, with a bounded error
summary. Saved ingestion results also retain the compact previous/new watermark
outcome, so management can reconcile a completed write after a long outage;
preview rows are removed. These compact records remain permanently: retrying an expired run ID
returns its original result status and never starts a second execution. The
authenticated compute `/status` response reports `output_retention`, including
the policy and latest cleanup counts.

The authenticated API is:

```text
GET /api/runs/{run_id}/profile
GET /api/runs/{run_id}/profile?compare_run_id={other_run_id}
```

The response includes the current profile and optional duration, peak-memory,
queue-wait, and catalog-wait comparison. It does not rerun either notebook.

## CPU quotas and JVM sizing

The local and standalone Compose compute services default to:

```dotenv
OPENBRICKS_COMPUTE_CPUS=2
```

This is the entire compute container's CPU quota, shared by its API and workers;
it is not a per-notebook reservation. Container-aware JVMs use assigned CPU
capacity when sizing garbage-collection and supporting thread pools. Bounding
that capacity reduces avoidable native-thread overhead on many-core hosts.
Spark's `local[n]` master controls task threads separately and does not limit
the CPU available to those JVM pools.

For a larger host, adjust the quota deliberately with Spark task threads,
concurrency, and worker/container memory budgets, then recreate the compute
service with its existing data volume. The CPU timeline continues to report
process CPU time: 100% means one used core, not 100% of the configured quota.
The disposable smoke and SQL-test compute containers use a fixed two-CPU quota
so resource checks are reproducible across developer hosts and CI.

See the [Docker Compose CPU limit](https://docs.docker.com/reference/compose-file/services/#cpus)
and [Java 17 container/processor sizing](https://docs.oracle.com/en/java/javase/17/docs/specs/man/java.html)
references. CPU quotas complement the existing RSS guard and memory ceilings;
they do not increase a worker's allowed memory.

## Concurrent Spark workers with Unity Catalog

The default **hive** mode preserves existing local Hive tables and uses one
initialized Spark worker at a time. Separate Python/DuckDB work can still run
under the configured resource policies. The lease is necessary because embedded
Derby cannot safely be opened by independent JVM processes.

An administrator can explicitly select **unity** mode for a compute service:

```dotenv
OPENBRICKS_SPARK_CATALOG_MODE=unity
OPENBRICKS_CATALOG_URL=http://catalog:8080
OPENBRICKS_CATALOG_NAMES=unity
OPENBRICKS_SPARK_DEFAULT_CATALOG=unity
```

Use the catalog endpoint reachable from that compute host, with the appropriate
catalog token and storage credentials. The default catalog must be one of the
configured Unity Catalog names; an empty setting selects the first configured
name. The catalogs and required schemas must already exist. Invalid settings
fail closed before work is admitted.

In this mode workers do not enable Hive or open the shared Derby metastore.
Persistent tables must use Unity Catalog, and existing local Hive tables are not
migrated. `spark_catalog` is a per-process in-memory compatibility catalog;
do not use it for persistent shared table discovery. Existing notebooks that
depend on Hive should stay on a Hive compute target until deliberately migrated.

Unity mode permits more than one Spark JVM under the existing compute and
project budgets. Set **max concurrent** and worker memory reservations in
**Operations → Resources** for the actual available memory. Each JVM needs its
own driver heap, native memory, and Python process; increasing concurrency does
not increase the container ceiling. The status API reports `spark_catalog_mode`,
`spark_default_catalog`, and the configured catalog concurrency.

This removes the embedded-metastore ownership bottleneck. It does not serialize
or make arbitrary concurrent external writes safe. Use transactional formats,
separate destinations where appropriate, and explicit safe retry policies for
jobs that write data. Underlying catalog and storage authorization remain the
responsibility of those services.
