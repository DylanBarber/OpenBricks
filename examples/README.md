# Build your first OpenBricks project

These examples use small synthetic datasets and the Python/Spark runtime supplied by OpenBricks. Start with a disposable learning workspace so you can inspect and rerun their outputs.

| Example | Learn | Requirements |
| --- | --- | --- |
| [Hello Spark](hello_spark.py) | Import a source notebook, mix Python/SQL, and save a Spark table | Default OpenBricks compute |
| [Brickkit](brickkit/) | Build a reusable wheel and import it from a notebook or Spark UDF | Build/upload the wheel first |
| [Notebook repository starter](notebook-repo/) | Git import, packaged data, nested folders, bronze/silver/gold, quality checks, scheduling | Clone the project, import notebooks, build its wheel |

## A quick notebook

Download [hello_spark.py](hello_spark.py) and use **Import notebook**, or clone the source repository and choose **Import notebooks**. Its Databricks source markers preserve Markdown, Python, and SQL cells.

Choose compute and select **Run all**. The notebook creates a small sales DataFrame, aggregates it with SQL, and creates/replaces the persistent demo table `sample_sales` on that compute service.

## A complete Git project

The [notebook-repo directory](notebook-repo/README.md) can become a standalone Git repository. It includes everything needed for a repeatable orders pipeline: five notebooks, a wheel project, packaged CSV data, and result assertions. Start with its `00_run_pipeline.py` notebook after building the wheel.

The [Git guide](../docs/GIT_IMPORTS.md) explains exact UI steps, private repositories, updates, copy semantics, and remote compute. The [curated upstream repositories](../docs/EXAMPLE_REPOSITORIES.md) offer additional learning material with explicit compatibility notes.

Local blob paths and the local Spark metastore belong to the selected compute host. A remote service receives installed wheels, including packaged fixtures; it does not receive arbitrary Git files or another host's local buckets. Use an accessible S3-compatible endpoint for shared datasets.

All original examples and synthetic fixtures are covered by the project's [Apache-2.0 license](../LICENSE). Linked upstream examples retain their own licenses.
