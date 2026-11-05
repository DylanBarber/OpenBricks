# Parameters, workflows, and reproducible runs

OpenBricks can schedule a single notebook or connect several notebooks in a dependency graph. Workspace jobs capture saved source when a run starts. A Git release lets you select an immutable source revision instead.

## Define notebook inputs

Open a notebook, choose **Notebook settings**, and add parameters with a name, type, optional default, and required flag. Save the notebook before running it. Supported types are `string`, `integer`, `number`, `boolean`, ISO `date` (`YYYY-MM-DD`), and `json`.

Use **Run options** to override defaults and optionally choose compute or a pinned package environment. Leave an override blank to use the saved default. Scheduled notebook jobs have parameter overrides too. Unknown names, missing required values, invalid types, and invalid configured choices are rejected before execution. The API also supports typed choice lists on parameter definitions.

Read resolved inputs through the Python `params` dictionary:

```python
# Define run_date as a date parameter, and minimum_total as a number parameter.
run_date = params["run_date"]
minimum_total = params.get("minimum_total", 0)
print(run_date, minimum_total)
```

Use SQL parameters for values; do not concatenate run inputs into SQL text. Both query engines in [Data workspace](DATA_WORKSPACE.md) accept positional `?` placeholders and a JSON parameter array.

### Limited widgets compatibility

Notebook code can use `dbutils.widgets.text`, `dropdown`, `combobox`, `multiselect`, `get`, `getAll`, `remove`, and `removeAll`. This is a small compatibility helper, rather than Databricks' interactive widget toolbar.

```python
dbutils.widgets.text("region", "West")
region = dbutils.widgets.get("region")
print(region)
```

Widget declarations supply missing defaults without replacing supplied run values. To override `region` from the UI, define it as a saved notebook parameter too. Widget reads return strings; non-string run values are represented as JSON text. Changing the notebook's widget dictionary does not rewrite its recorded run inputs. Unsupported utilities such as `dbutils.notebook.run`, `%run`, `%pip`, and secret scopes are not supplied by this compatibility layer.

Try the [runnable workflow starter](../examples/workflow-starter/) for two parameterized bronze/silver notebooks, explicit quality/lineage events, and an installer that resolves the workflow's notebook IDs.

## Build a notebook workflow

1. Select the intended project and open **Workflows → New workflow**.
2. Add a step for each notebook. Give it a unique key, such as `ingest`, `clean`, or `publish`.
3. Choose a workspace notebook, or an immutable Git release and notebook path. Each step can choose its compute target, timeout, and parameter overrides.
4. Under **Runs after**, select upstream steps. The graph reflects those dependencies. Cycles, missing dependencies, and duplicate keys are rejected.
5. Add workflow parameters if needed, then save and run the workflow.

A step waits for all its dependencies to succeed. A failed, cancelled, or skipped upstream step causes dependent steps to be skipped. Independent steps can progress on different compute services; work targeting the same compute service shares its execution queue. Steps do not share Python variables. Exchange data through persisted tables or storage reachable by their compute targets.

Workflow values with names matching a notebook's declared inputs are inherited. A step override takes precedence. To reference a workflow value explicitly, use an exact JSON string such as:

```json
{"run_date": "${params.run_date}"}
```

This substitutes a typed value, including inside nested JSON structures. It does not evaluate Python or expand arbitrary strings such as `prefix-${params.run_date}`.

Choose **Add schedule** on a saved workflow to create a job with Basic or Advanced scheduling. Edit, pause, or remove that job under **Jobs & schedules**. Workflow steps retain their own compute and environment selections. A scheduled workspace workflow resolves its saved definition and captures its sources when each trigger starts; it does not pull Git or rebuild packages.

## Watch progress and recover deliberately

Open workflow **History**, then a run. The graph shows each step's state and attempt count; **Open notebook logs** opens the underlying run. Notebook run details show cell progress, bounded live logs, peak interpreter memory, errors, source provenance, and the captured package environment. These are application telemetry, not a full Spark monitoring system.

Retries are off by default. To enable them, set a retry count and delay, and explicitly select **This step is safe to retry (writes are idempotent)**. Up to five retries are supported. That declaration is your assessment of the task; OpenBricks cannot prove that its external writes are safe.

An execution whose outcome is unknown, or which fails with a configuration, permission, or cancellation classification, is not automatically retried. A lost connection is not evidence that a SQL write failed. Reconcile external effects before manually starting replacement work.

For a finished failed workflow, **Restart failed steps** creates a new workflow run using the original captured specifications. It preserves successful steps outside the affected downstream branch and retries unsuccessful steps and their dependents. It does not silently switch to today's edited notebooks or wheel versions. **Rerun captured sources** starts the entire captured workflow again. A repair is still a new execution and can repeat external side effects.

Cancellation can remain pending while a remote compute service confirms that work stopped. Wait for the final outcome before replacement execution. Schedules run as their recorded account, and current project, compute, and connection access is rechecked.

## Pin Git sources and wheels

Under **Releases & environments**:

1. **Capture environment** records the currently installed wheel artifacts by SHA-256, together with the runtime identity.
2. **Create release** selects a cloned repository, its exact full commit hash, optional notebook paths, connection selections, and a package environment. Leaving paths empty captures supported notebooks at that commit. The commit must exist in the management checkout.
3. Open a release to see captured paths or compare added, removed, and changed notebooks and environment identity against another release.
4. Use **Run release**, or choose that release/path in a workflow step.

The UI uses full 40-character Git hashes. The release API also accepts full hashes from SHA-256 Git repositories. A release reads committed Git objects, not unsaved workspace edits. Pulling or importing later changes does not alter an existing release.

Each new notebook run captures its source, resolved parameters, and exact wheel manifest. **Rerun captured source** in run details uses those snapshots. Legacy runs without snapshots cannot use this action. Notebook **Revisions** provides a separate, bounded history of the newest 50 source saves/imports; comparing and restoring a revision creates another saved version. A source revision does not restore datasets, credentials, project assignments, or a running interpreter.

Pinned environments verify wheel hashes and the management/compute runtime identity, including Python, Spark, architecture, and build ID. Deploy the compatible original runtime image if that identity differs. Dependency wheels must already be supplied; OpenBricks does not resolve them from a package registry at execution time.

Snapshots do not freeze database contents, external files, secrets, network services, or catalog state. Current connection credentials and permissions apply when work is dispatched. Persist inputs and manage external data versions separately when you need repeatable results.

See [Git imports](GIT_IMPORTS.md), [projects and access](PROJECTS.md), and [operations](OPERATIONS.md) for the surrounding workflows.
