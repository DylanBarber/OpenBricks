"""Single-process, persistent notebook kernel with an explicit JSON protocol.

This is trusted code execution, not an untrusted-user sandbox. Its parent owns
the execution queue, timeout, and process-group cancellation.
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import io
import json
import math
import os
import pprint
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .blob import LocalBlobStore, LocalFS


MAX_ROWS = 200
MAX_OUTPUT = 100_000
_spark = None
_scopes: dict[str, dict[str, Any]] = {}


class CappedOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.truncated = False

    def write(self, value: str) -> int:
        remaining = MAX_OUTPUT - self.tell()
        if len(value) > remaining:
            self.truncated = True
        if remaining > 0:
            super().write(value[:remaining])
        return len(value)

    def text(self) -> str:
        return self.getvalue() + ("\n[Output truncated at 100 KB]" if self.truncated else "")


def spark_session(data_dir: str):
    global _spark
    if _spark is None:
        from pyspark.sql import SparkSession
        root = Path(data_dir).resolve()
        warehouse = root / "warehouse"
        warehouse.mkdir(parents=True, exist_ok=True)
        metastore = root / "metastore"
        # A single worker owns this local Derby metastore and SparkContext.
        builder = (SparkSession.builder.appName("OpenBricks")
            .master(os.environ.get("SPARK_MASTER", "local[*]"))
            .config("spark.sql.warehouse.dir", warehouse.as_uri())
            .config("spark.hadoop.javax.jdo.option.ConnectionURL", f"jdbc:derby:;databaseName={metastore};create=true")
            .config("spark.driver.bindAddress", os.environ.get("SPARK_DRIVER_BIND_ADDRESS", "127.0.0.1"))
            .config("spark.driver.host", os.environ.get("SPARK_DRIVER_HOST", "127.0.0.1"))
            .config("spark.driver.memory", os.environ.get("SPARK_DRIVER_MEMORY", "1g"))
            .config("spark.ui.enabled", "false")
            .config("spark.sql.shuffle.partitions", os.environ.get("SPARK_SHUFFLE_PARTITIONS", "8"))
            .enableHiveSupport())
        from .spark_catalog import configure_catalog
        _spark = configure_catalog(builder).getOrCreate()
        _spark.sparkContext.setLogLevel("WARN")
    return _spark


class SparkProxy:
    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    def __getattr__(self, name: str):
        return getattr(spark_session(self._data_dir), name)

    def __repr__(self):
        return "<OpenBricks local Spark session (initialized on first Spark operation)>"


class ContextProxy:
    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    def __getattr__(self, name: str):
        return getattr(spark_session(self._data_dir).sparkContext, name)


def serializable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def table(value: Any) -> dict | None:
    # Import Spark only after a Spark object is actually present.
    if type(value).__module__.startswith("pyspark") and hasattr(value, "columns") and hasattr(value, "take"):
        return {"columns": list(value.columns), "rows": [[serializable(v) for v in row] for row in value.take(MAX_ROWS)]}
    if type(value).__module__.startswith("pandas") and hasattr(value, "columns") and hasattr(value, "head"):
        return {"columns": [str(c) for c in value.columns], "rows": serializable(value.head(MAX_ROWS).values.tolist())}
    if isinstance(value, list) and value and isinstance(value[0], dict):
        columns = list(dict.fromkeys(str(k) for row in value[:MAX_ROWS] for k in row))
        return {"columns": columns, "rows": [[serializable(row.get(c)) for c in columns] for row in value[:MAX_ROWS]]}
    return None


def execute_cell(cell: dict, scope: dict, data_dir: str) -> dict:
    started = time.monotonic()
    stdout, stderr = CappedOutput(), CappedOutput()
    output = {"cell_id": cell["id"], "status": "succeeded", "stdout": "", "stderr": "", "result": None, "duration_ms": 0}

    def display(value: Any) -> None:
        result = table(value)
        if result is not None:
            output["result"] = result
        elif value is not None:
            pprint.pprint(value, stream=sys.stdout)

    scope["display"] = display
    source = cell.get("source", "")
    language = cell.get("language", "python")
    if source.lstrip().startswith("%sql"):
        language, source = "sql", source.lstrip()[4:].lstrip("\r\n ")
    elif source.lstrip().startswith("%python"):
        language, source = "python", source.lstrip()[7:].lstrip("\r\n ")
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            if language == "sql":
                display(spark_session(data_dir).sql(source))
            elif language == "python":
                tree = ast.parse(source, filename=f"<notebook-cell-{cell['id']}>", mode="exec")
                if tree.body and isinstance(tree.body[-1], ast.Expr):
                    final = tree.body.pop()
                    exec(compile(tree, f"<notebook-cell-{cell['id']}>", "exec"), scope)
                    value = eval(compile(ast.Expression(final.value), f"<notebook-cell-{cell['id']}>", "eval"), scope)
                    if value is not None:
                        display(value)
                else:
                    exec(compile(tree, f"<notebook-cell-{cell['id']}>", "exec"), scope)
            elif language != "markdown":
                raise ValueError(f"Unsupported cell language: {language}")
        except BaseException:
            output["status"] = "failed"
            traceback.print_exc(file=stderr)
    output.update(stdout=stdout.text(), stderr=stderr.text(), duration_ms=round((time.monotonic() - started) * 1000))
    return output


def run_request(request: dict, emit) -> None:
    run_id = request["run_id"]
    data_dir = request["data_dir"]
    package_dir = str(Path(request["package_dir"]).resolve())
    if package_dir not in sys.path:
        sys.path.insert(0, package_dir)
    for path in request.get("repository_paths", request.get("python_paths", [])):
        value = str(Path(path).resolve())
        if value not in sys.path:
            sys.path.append(value)
    importlib.invalidate_caches()
    notebook_id = request["notebook_id"]
    if request.get("reset_scope"):
        _scopes.pop(notebook_id, None)
    if notebook_id not in _scopes:
        blob = LocalBlobStore(Path(data_dir) / "buckets")
        _scopes[notebook_id] = {"__name__": "__main__", "spark": SparkProxy(data_dir),
            "sc": ContextProxy(data_dir), "blob": blob, "dbutils": SimpleNamespace(fs=LocalFS(blob))}
    scope = _scopes[notebook_id]
    # Connections refresh per request, so edits and revocations apply to the
    # next cell without persisting plaintext credentials in notebook files.
    from .connectors import ConnectionRegistry
    scope["connections"] = ConnectionRegistry(request.get("connections", []))
    from .spark_catalog import SparkCatalogHelper
    scope["catalog"] = SparkCatalogHelper(lambda: spark_session(data_dir))
    error = None
    for cell in request["cells"]:
        output = execute_cell(cell, scope, data_dir)
        emit({"type": "cell", "run_id": run_id, "output": output})
        if output["status"] == "failed":
            error = output["stderr"]
            break
    emit({"type": "done", "run_id": run_id, "status": "failed" if error else "succeeded", "error": error})


def main() -> None:
    # Keep native/JVM stdout away from the protocol. Python cell output still
    # goes through redirect_stdout above and is captured per cell.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def emit(message: dict) -> None:
        protocol.write(json.dumps(serializable(message), default=serializable, allow_nan=False) + "\n")
        protocol.flush()

    try:
        for line in sys.stdin:
            request = {}
            try:
                request = json.loads(line)
                run_request(request, emit)
            except BaseException:
                emit({"type": "done", "run_id": request.get("run_id"), "status": "failed", "error": traceback.format_exc()})
    finally:
        if _spark is not None:
            _spark.stop()


if __name__ == "__main__":
    main()
