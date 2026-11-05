# Parameterized bronze-to-silver workflow

This example uses synthetic orders, two Python notebooks, typed inputs, quality checks, explicit lineage, and a dependency workflow. It requires the 0.3 runtime and a disposable learning project with local compute. No saved connection, extra wheel, cloud account, or Unity Catalog is needed.

Steps exchange local Parquet data through `blob://workflow-demo/<run_date>/<region>/`. Keep both on the same compute service; another host does not share those local buckets automatically. Writes use `overwrite`, and the workflow explicitly marks these supplied steps safe to retry. That declaration applies to this example, rather than arbitrary notebook writes.

## Install in a running workspace

From an OpenBricks source checkout with Python installed:

```console
python examples/workflow-starter/install.py --url http://localhost:3000
```

The installer prompts privately for the password, or reads `OPENBRICKS_ADMIN_PASSWORD` from the process environment. It does not load `.env`. Use `--username` and `--project` for another existing account/project. The account needs project edit/run access and access to local compute.

It creates two new notebooks and one workflow and prints their IDs. Each invocation creates new resources; it does not overwrite or clean up previous installations. `workflow.json` is the actual API definition with notebook-ID tokens; the installer fills them from notebook creation responses. It also attaches typed definitions to the notebooks. Plain source-file import alone does not declare UI parameters.

To install and immediately queue the workflow in your disposable project:

```console
python examples/workflow-starter/install.py --url http://localhost:3000 --run
```

## Explore in the UI

1. Select the project and open **Workflows → Parameterized bronze to silver demo**.
2. Inspect the `bronze → silver` graph and safe-retry settings.
3. Run with defaults, or override `run_date` and `region`. `minimum_rows` is a quality threshold; set it above four for a deliberate failure, then start a fresh run with corrected inputs.
4. Inspect workflow history, step logs, and quality reports. **Data workspace → Quality & lineage** shows the recorded reads/writes.
5. Run again with the same inputs: output is replaced without duplicated orders.
6. Try **Add schedule**. Remove that learning job before deleting its workflow.

| Region | Expected orders | Expected total revenue |
| --- | --- | --- |
| All | 4 | 96.50 |
| North | 2 | 42.50 |
| South | 2 | 54.00 |

The silver notebook asserts these results. `run_date` is a partition label for the fixture, rather than a business-date filter. Repairing a deliberately failed workflow preserves its captured parameters, including the failing threshold; start a new run to supply corrected inputs.

You can import the `.py` files and configure their parameters/dependency manually using `workflow.json` as a reference. Source markers preserve Markdown and Python cells. Delete created jobs/workflows/notebooks through the UI when finished; the example's local bucket remains until you explicitly delete its files.

See [Workflows](../../docs/WORKFLOWS.md), [Data workspace](../../docs/DATA_WORKSPACE.md), and [Projects](../../docs/PROJECTS.md). These original examples and synthetic fixtures use the project's [Apache-2.0 license](../../LICENSE).
