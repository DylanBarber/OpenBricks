# Brickkit: your first reusable wheel

Brickkit is a tiny package with two functions: `normalize_name` and `revenue`. It demonstrates the difference between notebook cells and reusable package code. OpenBricks does not discover this ordinary Python module as a notebook.

## Build and install

From **Packages → Build wheel**, select a clone of the OpenBricks repository and set **Project directory** to `examples/brickkit`. Choose **Build & install**. The distribution is named `openbricks-brickkit`; Python imports use `brickkit`.

Alternatively, build outside OpenBricks using Python with the `build` package installed, from the OpenBricks root:

```bash
python -m build --wheel examples/brickkit
```

Upload the resulting `.whl` from `examples/brickkit/dist/` through **Packages → Upload wheel**. The package has no additional runtime dependencies.

## Use it in a notebook

```python
from brickkit import normalize_name, revenue

print(normalize_name("  ada   lovelace "))  # Ada Lovelace
print(revenue(3, 12.50))                    # 37.5
```

Use the installed package from a Spark Python UDF too:

```python
from pyspark.sql import functions as F
from pyspark.sql.types import StringType
from brickkit import normalize_name

normalize = F.udf(normalize_name, StringType())
names = spark.createDataFrame([("  ada   lovelace ",), ("grace hopper",)], ["raw_name"])
display(names.withColumn("name", normalize("raw_name")))
```

The sample function expects a string, so the example intentionally uses non-null names. It is a learning package rather than a general-purpose data-cleaning library.

## Update and reuse

Change the package source, bump `version` in `pyproject.toml`, commit/push, then use **Pull latest** and build again. A Git pull alone does not update installed code. Wait for queued and running work to finish before installing; package changes reset the interpreter.

Installed wheel artifacts synchronize before remote execution. Package source files and arbitrary local datasets do not. The larger [notebook starter](../notebook-repo/README.md) demonstrates distributing small CSV fixtures inside a wheel and scheduling a complete pipeline.

This example is covered by the project's [Apache-2.0 license](../../LICENSE).
