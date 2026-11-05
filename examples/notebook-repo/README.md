# Orders, one brick at a time

A complete starter Git project for OpenBricks: five notebooks, a reusable Python wheel, a nine-row CSV, and a repeatable bronze → silver → gold pipeline. It uses real Spark DataFrames, Parquet files, a local blob bucket, and Spark SQL. No external dataset, cloud account, SQL Server, or Unity Catalog is required.

This directory can become its own Git repository. It also works when imported from the full OpenBricks repository. The filenames and notebook cell markers are part of the runnable example.

To publish a standalone copy, create an empty repository on your Git host, copy this directory's contents (including `.gitignore` and `LICENSE`) into a new directory outside the OpenBricks checkout, and run the following there. Replace `YOUR_REPOSITORY_CLONE_URL` with that empty repository's actual clone URL:

```sh
git init -b main
git add .
git commit -m "Add OpenBricks notebook starter"
git remote add origin YOUR_REPOSITORY_CLONE_URL
git push -u origin main
```

## What you will build

```text
notebook-repo/
├── pyproject.toml                       # Buildable openbricks-notebook-demo wheel
├── src/openbricks_demo/
│   ├── __init__.py
│   ├── pipeline.py                      # Shared transformation functions
│   └── fixtures/orders.csv              # Packaged fixture; travels with the wheel
├── notebooks/
│   ├── 00_run_pipeline.py               # Run and schedule this complete pipeline
│   ├── bronze/10_ingest_orders.py        # Explore ingestion on its own
│   ├── silver/20_clean_orders.py         # Explore quality checks and quarantine
│   └── gold/
│       ├── 30_publish_revenue.py         # Build the report and persistent table
│       └── 40_query_revenue.sql          # Query the published report
└── tools/verify_imports.py               # Actual importer + optional worker check
```

| Layer | Purpose | Location |
| --- | --- | --- |
| Raw | Seed the fixture on the first run; preserve later edits | `blob://openbricks-demo/raw/orders.csv` |
| Bronze | Keep input columns as strings in Parquet | `blob://openbricks-demo/bronze/orders/` |
| Silver | Keep the latest order version, normalize values, validate data | `blob://openbricks-demo/silver/orders/` |
| Quarantine | Retain invalid latest orders with a reason | `blob://openbricks-demo/silver/rejected_orders/` |
| Gold | Aggregate completed orders by date and region | `blob://openbricks-demo/gold/daily_revenue/` |

The gold table is `spark_catalog.openbricks_demo.daily_revenue`. It is an external Parquet table in the compute service's local Spark metastore, so it survives interpreter restarts on the same persistent data volume. It is separate from optional Unity Catalog.

## Import, build, and run

1. Put this directory into a Git repository you control, or use the full OpenBricks repository URL that you actually cloned. OpenBricks does not provide a separate hosted demo repository URL.
2. In **Git repositories → Clone repository**, enter a display name, your repository's HTTPS/SSH clone URL, and an existing branch such as `main`. Clone it, then choose **Import notebooks** on its card.
3. In **Packages → Build wheel**, select the repository. Set **Project directory** to `.` for this standalone project, or `examples/notebook-repo` for a clone of the full OpenBricks source repository. Select **Build & install**. The installed distribution is `openbricks-notebook-demo`, version `0.1.0`; the Python import is `openbricks_demo`.
4. In **Notebooks**, open your repository folder, then `notebooks`. Open **00 run pipeline**, choose the built-in compute service, and select **Run all**. Filename underscores become spaces in the workspace notebook name.
5. Open **Object storage** and select the local `openbricks-demo` bucket. Explore the raw CSV and the Parquet output files. Open **Run history** and select **00 run pipeline** to inspect its runs.
6. Select **Run all** again. Both runs should pass the assertions below. Outputs are replaced on each run rather than appended.

The package includes its CSV using `importlib.resources`. There is no reliance on the notebook's working directory or on a raw Git checkout being present on compute.

## Expected results

The fixture has one duplicate order with a later status, one negative quantity, one missing region, and one cancelled order.

| Check | Expected |
| --- | ---: |
| Bronze rows | 9 |
| Latest order IDs before validation | 8 |
| Valid silver orders | 6 |
| Quarantined orders | 2 |
| Completed orders | 5 |
| Gold rows | 5 |
| Completed revenue | 85.50 |

```sql
SELECT order_date, region, orders, units, revenue
FROM spark_catalog.openbricks_demo.daily_revenue
ORDER BY order_date, region
```

| order_date | region | orders | units | revenue |
| --- | --- | ---: | ---: | ---: |
| 2026-01-01 | North | 1 | 2 | 25.00 |
| 2026-01-01 | South | 1 | 1 | 20.00 |
| 2026-01-02 | East | 1 | 2 | 9.50 |
| 2026-01-02 | West | 1 | 3 | 21.00 |
| 2026-01-03 | West | 1 | 4 | 10.00 |

## Schedule the full pipeline

In **Jobs & schedules → Create job**, search for **00 run pipeline**. Choose the same compute service you used for the first run. Select **Basic**, **Every day**, `08:00`, and an explicit time zone such as `UTC`. Save, then use **Run now** to verify the scheduled job's configuration.

For a weekday schedule, choose **Advanced**, use `0 8 * * 1-5`, and select your intended time zone. Cron has five fields; this means Monday–Friday at 08:00. The UI's **Basic → On selected weekdays** option can express the same schedule.

Schedule only `00_run_pipeline.py`. It calls each layer's wheel function in order. The three layer notebooks are for exploration, and are not a dependency graph. OpenBricks does not implement Databricks `%run`, `dbutils.notebook.run`, or multi-notebook DAG jobs. Do not run the layer notebooks concurrently with the full pipeline because they replace the same demo paths.

## Make it your project

- Change code under `src/openbricks_demo`, bump the version in `pyproject.toml` and `__init__.py`, commit/push, select **Pull latest**, then **Build & install** again.
- Change notebook cells in Git, commit/push, select **Pull latest**, then **Import notebooks** again. Re-import updates the same workspace notebook by repository and relative path, replacing workspace edits. It does not commit or push UI changes.
- The raw fixture is seeded only if `raw/orders.csv` is absent. Updating the CSV in the wheel does not replace an already seeded raw object. To reset this demo, delete that raw CSV in **Object storage** and rerun the full notebook. For your own input, upload a replacement CSV with the same columns and revise the fixture-specific assertions in `00_run_pipeline.py`.
- Transformations rebuild all demo output directories. This is a small batch example, with no incremental ingestion, streaming, concurrent writers, or transaction guarantees across all layers.
- Default idle shutdown drops Python variables after 15 minutes without work. Every full pipeline run imports its code and reads saved data again, so it does not depend on a warm interpreter.

## Remote compute

Installed wheels are synchronized before remote execution. The packaged CSV goes with the wheel, and the pipeline seeds its bucket on the selected compute service. `blob://` storage and this demo's Spark metastore belong to that compute service's data volume. A separate remote server does not share the built-in service's files or tables, and its local buckets are not automatically browsable in the management server's local **Object storage** view.

Run every layer on the same service. For a dataset shared across services, configure an S3-compatible connection such as SeaweedFS, move the dataset there, and adapt the I/O functions to that connection. This starter deliberately uses local blobs; changing the compute selector does not convert local paths into shared storage.

## Verify from a source checkout

These checks do not clone repositories or contact Git hosting. Use the OpenBricks Python environment, which has the actual importer and dependencies installed. From the OpenBricks project root:

```sh
python examples/notebook-repo/tools/verify_imports.py
```

The check confirms that exactly five notebooks are discovered, ordinary package/helper files are excluded, source cell boundaries and languages survive import, Python cells compile, and the fixture totals are correct.

With Java and PySpark available, add `--execute` to run all five imported notebooks through the real OpenBricks worker and repeat the complete pipeline. This uses a temporary data directory and does not touch your running server's data:

```sh
python examples/notebook-repo/tools/verify_imports.py --execute
```

For a standalone starter checkout, use its script with `--openbricks-root /path/to/openbricks`. To verify an installed wheel rather than the source package, add `--package-dir /path/to/wheel-install-target`. The starter wheel has no third-party dependencies: OpenBricks supplies Spark.

This starter's source, notebooks, and synthetic CSV fixture are provided under the included Apache-2.0 [LICENSE](LICENSE). The CSV is made-up demo data, with no external data source or attribution requirement. If you are reading this in the full OpenBricks source checkout, `docs/GIT_IMPORTS.md` has additional repository creation, private SSH, supported-format, and troubleshooting guidance.
