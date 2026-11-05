"""Check this starter with the real OpenBricks importer, optionally its worker.

From an OpenBricks checkout:
    python examples/notebook-repo/tools/verify_imports.py
    python examples/notebook-repo/tools/verify_imports.py --execute

For a separate starter checkout, add --openbricks-root /path/to/openbricks.
The execution check uses a temporary data directory, never the server's /data.
"""
from __future__ import annotations

import argparse
import ast
import csv
from decimal import Decimal
import io
import os
from pathlib import Path
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
EXPECTED = {
    "notebooks/00_run_pipeline.py": ["markdown", "python", "markdown", "python", "sql", "markdown"],
    "notebooks/bronze/10_ingest_orders.py": ["markdown", "python"],
    "notebooks/silver/20_clean_orders.py": ["markdown", "python", "python"],
    "notebooks/gold/30_publish_revenue.py": ["markdown", "python", "sql"],
    "notebooks/gold/40_query_revenue.sql": ["sql"],
}


def check_fixture() -> None:
    """Independently calculate expected output from the tiny CSV fixture."""
    rows = list(csv.DictReader(io.StringIO((REPO / "src/openbricks_demo/fixtures/orders.csv").read_text(encoding="utf-8"))))
    assert len(rows) == 9
    latest = {}
    for row in sorted(rows, key=lambda value: value["updated_at"]):
        latest[row["order_id"]] = row
    valid = [row for row in latest.values() if row["region"].strip() and int(row["quantity"]) > 0]
    completed = [row for row in valid if row["status"].strip().lower() in ("completed", "complete")]
    total = sum((int(row["quantity"]) * Decimal(row["unit_price"]) for row in completed), Decimal("0"))
    assert len(latest) == 8 and len(valid) == 6 and len(completed) == 5
    assert total == Decimal("85.50"), total


def execute_notebooks(notebooks: dict, package_dir: Path) -> None:
    import openbricks
    from openbricks.runtime import LocalRuntime
    from openbricks.store import Store

    # The smoke check exercises local Spark and needs no catalog/cloud service.
    os.environ["OPENBRICKS_CATALOG_URL"] = ""
    os.environ.setdefault("SPARK_MASTER", "local[2]")
    os.environ.setdefault("SPARK_SHUFFLE_PARTITIONS", "2")
    backend = str(Path(openbricks.__file__).resolve().parent.parent)
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(package_dir), backend, os.environ.get("PYTHONPATH")]))
    with tempfile.TemporaryDirectory(prefix="openbricks-git-demo-") as temporary:
        store = Store(Path(temporary))
        runtime = LocalRuntime(store)
        runtime.timeout = 300
        runtime.start()
        try:
            for relative in [*EXPECTED, "notebooks/00_run_pipeline.py"]:
                notebook = {**notebooks[relative], "id": relative}
                submitted = runtime.submit(notebook)
                deadline = time.monotonic() + 310
                while True:
                    done = store.get("runs", submitted["id"])
                    if done["status"] not in ("running", "queued"):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{relative} did not finish within 310 seconds")
                    time.sleep(0.1)
                if done["status"] != "succeeded":
                    raise AssertionError(f"{relative} failed:\n{done.get('error')}")
                outputs = done["outputs"]
                assert len(outputs) == len(notebooks[relative]["cells"])
                if relative.endswith("40_query_revenue.sql"):
                    assert len(outputs[0]["result"]["rows"]) == 5
                print(f"EXECUTED {relative}")
            print("Real worker/Spark check passed, including repeat-run assertions.")
        finally:
            # Use the actual runtime's process-tree cleanup before deleting the
            # temporary metastore, including on Windows with open Derby files.
            runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openbricks-root", type=Path, help="OpenBricks source checkout, if not on PYTHONPATH")
    parser.add_argument("--execute", action="store_true", help="Run all five notebooks and repeat the pipeline using real Spark")
    parser.add_argument("--package-dir", type=Path, default=REPO / "src", help="Installed wheel target, or src for a source check")
    args = parser.parse_args()
    source_root = args.openbricks_root
    if source_root is None:
        source_root = next((parent for parent in REPO.parents if (parent / "backend/openbricks/main.py").is_file()), None)
    if source_root is not None:
        sys.path.insert(0, str(source_root.resolve() / "backend"))
    try:
        from openbricks.main import notebook_paths
        from openbricks.notebooks import parse_notebook
    except ImportError as error:
        raise SystemExit("Use the OpenBricks Python environment and --openbricks-root when needed.") from error

    paths = notebook_paths(REPO)
    actual = {path.relative_to(REPO).as_posix() for path in paths}
    assert actual == set(EXPECTED), f"Unexpected discovered notebooks: {actual ^ set(EXPECTED)}"
    notebooks = {}
    for path in paths:
        relative = path.relative_to(REPO).as_posix()
        notebook = parse_notebook(path.name, path.read_bytes())
        assert [cell["language"] for cell in notebook["cells"]] == EXPECTED[relative], relative
        for cell in notebook["cells"]:
            if cell["language"] == "python":
                ast.parse(cell["source"], filename=relative)
        notebooks[relative] = notebook
        print(f"IMPORTED {relative}: {len(notebook['cells'])} cells")
    for path in (REPO / "src").rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    check_fixture()
    print("Discovery, cell boundaries/languages, Python syntax, and fixture checks passed.")
    if args.execute:
        execute_notebooks(notebooks, args.package_dir.resolve())


if __name__ == "__main__":
    main()
