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
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .blob import LocalBlobStore, LocalFS

MAX_ROWS = 200
MAX_OUTPUT = 100_000
_spark = None
_spark_lease = None
_active_profiler = None
_secret_history = None
_scopes: dict[str, dict[str, Any]] = {}


class CappedOutput(io.StringIO):
    def __init__(self, callback=None):
        super().__init__()
        self.truncated = False
        self.callback = callback

    def write(self, value: str) -> int:
        remaining = MAX_OUTPUT - self.tell()
        if len(value) > remaining:
            self.truncated = True
        if remaining > 0:
            super().write(value[:remaining])
            if self.callback:
                self.callback(value[:remaining])
        return len(value)

    def text(self) -> str:
        return self.getvalue() + (
            "\n[Output truncated at 100 KB]" if self.truncated else ""
        )


def prepare_metastore_path(data_dir):
    """Let Derby create its database; old restores may leave an empty folder.

    rmdir is deliberately atomic and nonrecursive. An initialized database,
    a symlink, or files appearing after the emptiness check are never removed.
    """
    metastore = Path(data_dir).resolve() / "metastore"
    if (
        metastore.is_dir()
        and not metastore.is_symlink()
        and not any(metastore.iterdir())
    ):
        try:
            metastore.rmdir()
        except OSError:
            # A concurrent creator or permission change must leave its files
            # intact; Derby will report any remaining initialization problem.
            pass
    return metastore


def spark_session(data_dir: str):
    global _spark
    if _spark is None:
        if _spark_lease is not None:
            _spark_lease()
        from pyspark.sql import SparkSession

        root = Path(data_dir).resolve()
        warehouse = root / "warehouse"
        warehouse.mkdir(parents=True, exist_ok=True)
        from .spark_catalog import execution_catalog

        mode = execution_catalog()
        builder = (
            SparkSession.builder.appName("OpenBricks")
            .master(os.environ.get("SPARK_MASTER", "local[*]"))
            .config("spark.sql.warehouse.dir", warehouse.as_uri())
            .config(
                "spark.driver.bindAddress",
                os.environ.get("SPARK_DRIVER_BIND_ADDRESS", "127.0.0.1"),
            )
            .config(
                "spark.driver.host", os.environ.get("SPARK_DRIVER_HOST", "127.0.0.1")
            )
            .config("spark.driver.memory", os.environ.get("SPARK_DRIVER_MEMORY", "1g"))
            .config("spark.ui.enabled", "false")
            .config("spark.ui.retainedJobs", "128")
            .config("spark.ui.retainedStages", "256")
            .config("spark.sql.ui.retainedExecutions", "64")
            .config(
                "spark.sql.shuffle.partitions",
                os.environ.get("SPARK_SHUFFLE_PARTITIONS", "8"),
            )
        )
        if mode["shared_derby"]:
            metastore = prepare_metastore_path(root)
            builder = builder.config(
                "spark.hadoop.javax.jdo.option.ConnectionURL",
                f"jdbc:derby:;databaseName={metastore};create=true",
            ).enableHiveSupport()
        else:
            builder = builder.config("spark.sql.catalogImplementation", "in-memory")
        from .spark_catalog import configure_catalog

        _spark = configure_catalog(builder).getOrCreate()
        _spark.sparkContext.setLogLevel("WARN")
    if _active_profiler is not None:
        _active_profiler.bind(_spark)
    return _spark


class SparkProxy:
    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    def __getattr__(self, name: str):
        value = getattr(spark_session(self._data_dir), name)
        if name == "sql":

            def sql(*args, **kwargs):
                frame = value(*args, **kwargs)
                if _active_profiler is not None:
                    _active_profiler.plan(frame)
                return frame

            return sql
        return value

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
    if (
        type(value).__module__.startswith("pyspark")
        and hasattr(value, "columns")
        and hasattr(value, "take")
    ):
        return {
            "columns": list(value.columns),
            "rows": [[serializable(v) for v in row] for row in value.take(MAX_ROWS)],
        }
    if (
        type(value).__module__.startswith("pandas")
        and hasattr(value, "columns")
        and hasattr(value, "head")
    ):
        return {
            "columns": [str(c) for c in value.columns],
            "rows": serializable(value.head(MAX_ROWS).values.tolist()),
        }
    if isinstance(value, list) and value and isinstance(value[0], dict):
        columns = list(dict.fromkeys(str(k) for row in value[:MAX_ROWS] for k in row))
        return {
            "columns": columns,
            "rows": [
                [serializable(row.get(c)) for c in columns] for row in value[:MAX_ROWS]
            ],
        }
    return None


def execute_cell(
    cell: dict, scope: dict, data_dir: str, on_log=None, redactor=None
) -> dict:
    started = time.monotonic()
    from .execution import Redactor, StreamRedactor

    redactor = redactor or Redactor()
    streams = {name: StreamRedactor(redactor) for name in ("stdout", "stderr")}

    def stream(name, value="", final=False):
        safe = streams[name].feed(value, final=final)
        if safe and on_log:
            on_log(name, safe)

    stdout, stderr = CappedOutput(lambda value: stream("stdout", value)), CappedOutput(
        lambda value: stream("stderr", value)
    )
    output = {
        "cell_id": cell["id"],
        "status": "succeeded",
        "stdout": "",
        "stderr": "",
        "result": None,
        "duration_ms": 0,
    }

    def display(value: Any) -> None:
        if _active_profiler is not None:
            _active_profiler.plan(value)
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
                tree = ast.parse(
                    source, filename=f"<notebook-cell-{cell['id']}>", mode="exec"
                )
                if tree.body and isinstance(tree.body[-1], ast.Expr):
                    final = tree.body.pop()
                    exec(compile(tree, f"<notebook-cell-{cell['id']}>", "exec"), scope)
                    value = eval(
                        compile(
                            ast.Expression(final.value),
                            f"<notebook-cell-{cell['id']}>",
                            "eval",
                        ),
                        scope,
                    )
                    if value is not None:
                        display(value)
                else:
                    exec(compile(tree, f"<notebook-cell-{cell['id']}>", "exec"), scope)
            elif language != "markdown":
                raise ValueError(f"Unsupported cell language: {language}")
        except BaseException:
            output["status"] = "failed"
            traceback.print_exc(file=stderr)
    output.update(
        stdout=stdout.text(),
        stderr=stderr.text(),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    stream("stdout", final=True)
    stream("stderr", final=True)
    return redactor.output(output)


def run_request(request: dict, emit) -> None:
    global _spark_lease, _active_profiler, _secret_history
    run_id = request["run_id"]
    if os.getenv("OPENBRICKS_SPARK_LEASE_PROTOCOL") == "1":

        def acquire():
            emit({"type": "spark_lease_request", "run_id": run_id})
            answer = json.loads(sys.stdin.readline())
            if (
                answer.get("type") != "spark_lease_granted"
                or answer.get("run_id") != run_id
            ):
                raise RuntimeError(
                    "Compute did not grant the local Spark catalog lease"
                )

        _spark_lease = acquire
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
        _scopes[notebook_id] = {
            "__name__": "__main__",
            "spark": SparkProxy(data_dir),
            "sc": ContextProxy(data_dir),
            "blob": blob,
            "dbutils": SimpleNamespace(fs=LocalFS(blob)),
        }
    scope = _scopes[notebook_id]
    from .parameters import Widgets

    parameters = request.get("parameters", {})
    scope["params"] = dict(parameters)
    widgets = getattr(scope["dbutils"], "widgets", None)
    if widgets is None:
        widgets = Widgets(parameters)
        scope["dbutils"].widgets = widgets
    else:
        from .parameters import widget_text

        widgets.values.update(
            {name: widget_text(value) for name, value in parameters.items()}
        )
    # Connections refresh per request, so edits and revocations apply to the
    # next cell without persisting plaintext credentials in notebook files.
    from .connectors import ConnectionRegistry

    scope["connections"] = ConnectionRegistry(request.get("connections", []))
    from .secret_runtime import SecretRegistry, RedactionHistory

    secret_values = request.get("notebook_secrets", [])
    registry = SecretRegistry(secret_values)
    scope["notebook_secrets"] = registry
    scope["dbutils"].secrets = registry
    if _secret_history is None:
        _secret_history = RedactionHistory()
    known_values = [
        *secret_values,
        *(
            {"value": str(value)}
            for item in request.get("connections", [])
            for value in item.get("secrets", {}).values()
            if value is not None and str(value)
        ),
    ]
    historical = _secret_history.include(known_values)
    from .spark_catalog import SparkCatalogHelper

    scope["catalog"] = SparkCatalogHelper(lambda: spark_session(data_dir))
    from .execution import Redactor

    redactor = Redactor([*request.get("connections", []), *historical])
    raw_emit = emit

    def emit(event):
        safe = redactor.event(event)
        if event.get("type") == "profile":
            from .profiling import redact_spark_profile

            safe["spark"] = redact_spark_profile(event["spark"], redactor)
        raw_emit(safe)

    from .profiling import SparkProfiler

    profiler = SparkProfiler(run_id, lambda: _spark)
    _active_profiler = profiler
    profile_stop = threading.Event()

    def profile_event():
        snapshot = profiler.collect()
        if not profile_stop.is_set():
            emit({"type": "profile", "run_id": run_id, "spark": snapshot})

    def collect_profile():
        while not profile_stop.wait(2):
            profile_event()

    profile_thread = threading.Thread(
        target=collect_profile, daemon=True, name="openbricks-spark-profiler"
    )
    profile_thread.start()
    try:
        error = _execute_profiled_cells(
            request, scope, data_dir, run_id, emit, profiler, profile_event, redactor
        )
    finally:
        profile_stop.set()
        profile_thread.join(timeout=1)
        emit({"type": "profile", "run_id": run_id, "spark": profiler.collect()})
        _active_profiler = None
    emit(
        {
            "type": "done",
            "run_id": run_id,
            "status": "failed" if error else "succeeded",
            "error": error,
        }
    )


def _execute_profiled_cells(
    request, scope, data_dir, run_id, emit, profiler, profile_event, redactor
):
    try:
        from .data_workspace import ComputeDataWorkspace
    except ImportError:
        pass
    else:
        scope["data"] = ComputeDataWorkspace(
            lambda: spark_session(data_dir),
            scope["connections"],
            scope["blob"],
            emit,
            run_id,
            project_id=request.get("project_id", "default"),
        )
    error = None
    for index, cell in enumerate(request["cells"]):
        profiler.cell(cell["id"])
        emit(
            {
                "type": "progress",
                "run_id": run_id,
                "cell_id": cell["id"],
                "cell_index": index,
                "total_cells": len(request["cells"]),
            }
        )
        pending_logs, last_log = {"stdout": "", "stderr": ""}, [0.0]

        def on_log(stream, text, final=False):
            pending_logs[stream] += text
            if (
                not final
                and time.monotonic() - last_log[0] < 0.1
                and len(pending_logs[stream]) < 4096
            ):
                return
            value, pending_logs[stream] = pending_logs[stream], ""
            for offset in range(0, len(value), 8192):
                emit(
                    {
                        "type": "log",
                        "run_id": run_id,
                        "cell_id": cell["id"],
                        "stream": stream,
                        "text": value[offset : offset + 8192],
                    }
                )
            last_log[0] = time.monotonic()

        output = execute_cell(cell, scope, data_dir, on_log=on_log, redactor=redactor)
        for stream in pending_logs:
            on_log(stream, "", final=True)
        emit({"type": "cell", "run_id": run_id, "output": output})
        profile_event()
        if output["status"] == "failed":
            error = output["stderr"]
            break
    return error


def main() -> None:
    # Keep native/JVM stdout away from the protocol. Python cell output still
    # goes through redirect_stdout above and is captured per cell.
    protocol = os.fdopen(
        os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1
    )
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def emit(message: dict) -> None:
        with protocol_lock:
            protocol.write(
                json.dumps(serializable(message), default=serializable, allow_nan=False)
                + "\n"
            )
            protocol.flush()

    protocol_lock = threading.Lock()

    try:
        for line in sys.stdin:
            request = {}
            try:
                request = json.loads(line)
                run_request(request, emit)
            except BaseException:
                from .execution import Redactor

                emit(
                    Redactor(
                        [
                            *request.get("connections", []),
                            *(_secret_history.include() if _secret_history else []),
                        ]
                    ).event(
                        {
                            "type": "done",
                            "run_id": request.get("run_id"),
                            "status": "failed",
                            "error": traceback.format_exc(),
                        }
                    )
                )
    finally:
        if _spark is not None:
            _spark.stop()


if __name__ == "__main__":
    main()
