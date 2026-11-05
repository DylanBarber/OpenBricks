import json
import time

import pytest

from openbricks.execution import Redactor, StreamRedactor
from openbricks.parameters import Widgets, resolve_parameters
from openbricks.runtime import LocalRuntime
from openbricks.store import Store


def wait(store, identifier, terminal=True, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = store.get("runs", identifier)
        if (run["status"] not in ("queued", "running")) if terminal else bool(run.get("logs")):
            return run
        time.sleep(0.03)
    pytest.fail("Execution did not reach its expected state")


def notebook(source):
    return {"id": "parameters", "name": "Typed execution", "project_id": "default", "connection_ids": [],
        "parameter_definitions": [{"name": "count", "type": "integer", "required": True}],
        "cells": [{"id": "one", "language": "python", "source": source}]}


def test_typed_parameters_defaults_choices_and_strict_validation():
    definitions = [{"name": "count", "type": "integer", "required": True}, {"name": "enabled", "type": "boolean", "default": False},
        {"name": "day", "type": "date", "default": "2026-11-05"}, {"name": "region", "type": "string", "choices": ["east", "west"], "default": "east"}]
    assert resolve_parameters(definitions, {"count": "3", "enabled": "true"}) == {"count": 3, "enabled": True, "day": "2026-11-05", "region": "east"}
    for values in ({}, {"count": True}, {"count": 3, "unknown": 1}, {"count": 3, "region": "north"}, {"count": 3, "day": "2026-02-31"}, []):
        with pytest.raises(ValueError):
            resolve_parameters(definitions, values)
    with pytest.raises(ValueError):
        resolve_parameters([{"name": "payload", "type": "json"}], {"payload": "x" * 70_000})
    widgets = Widgets({"count": 3, "enabled": True})
    widgets.text("count", "999")
    widgets.combobox("region", "custom", ["east", "west"])
    assert widgets.getAll() == {"count": "3", "enabled": "true", "region": "custom"}


def test_stream_redaction_spans_write_boundaries():
    redactor = Redactor([{"secrets": {"password": "very-sensitive-password"}}])
    stream = StreamRedactor(redactor)
    result = "".join(stream.feed(char) for char in "prefix very-sensitive-password suffix") + stream.feed(final=True)
    assert result == "prefix [REDACTED] suffix"
    assert StreamRedactor(redactor).feed("ready\n") == "ready\n"


def test_project_switch_resets_interpreter_even_with_same_wheels(tmp_path):
    store = Store(tmp_path)
    runtime = LocalRuntime(store)
    runtime.start()
    try:
        first = {**notebook("private_value = 42"), "project_id": "first"}
        run = runtime.submit(first, parameters={"count": 1}, cell_id="one")
        assert wait(store, run["id"])["status"] == "succeeded"
        pid = runtime.process.pid
        second = {**notebook("print('private_value' in globals())"), "project_id": "second"}
        run = runtime.submit(second, parameters={"count": 1}, cell_id="one")
        done = wait(store, run["id"])
        assert done["status"] == "succeeded" and done["outputs"][0]["stdout"].strip() == "False"
        assert runtime.process.pid != pid
    finally:
        runtime.close()


def test_late_timeout_callback_does_not_reset_completed_session(tmp_path, monkeypatch):
    from openbricks import runtime as runtime_module
    class LateTimer:
        def __init__(self, seconds, callback):
            self.callback = callback
        def start(self):
            pass
        def cancel(self):
            # Simulate Timer.cancel racing a callback already dispatched but
            # delayed until after the worker's completion event.
            self.callback()
    monkeypatch.setattr(runtime_module.threading, "Timer", LateTimer)
    store = Store(tmp_path)
    runtime = LocalRuntime(store)
    runtime.start()
    try:
        first = runtime.submit(notebook("answer = 41"), cell_id="one", parameters={"count": 1})
        assert wait(store, first["id"])["status"] == "succeeded"
        second = runtime.submit(notebook("print(answer + 1)"), cell_id="one", parameters={"count": 1})
        done = wait(store, second["id"])
        assert done["status"] == "succeeded" and done["outputs"][0]["stdout"].strip() == "42"
    finally:
        runtime.close()


def test_real_live_logs_parameters_memory_and_exact_environment(tmp_path):
    store = Store(tmp_path)
    runtime = LocalRuntime(store)
    runtime.start()
    try:
        source = "import sys,time\nprint('visible progress')\nsys.stdout.write('very-sensitive-')\nsys.stdout.write('password')\ntime.sleep(1.5)\nprint(params['count'] + 1)\nprint(dbutils.widgets.get('count'))"
        submitted = runtime.submit(notebook(source), parameters={"count": 4}, connections=[{"secrets": {"password": "very-sensitive-password"}}])
        active = wait(store, submitted["id"], terminal=False)
        assert active["status"] == "running" and "visible" in json.dumps(active["logs"])
        done = wait(store, submitted["id"])
        assert done["status"] == "succeeded", done
        assert "very-sensitive-password" not in json.dumps(done)
        assert "[REDACTED]" in done["outputs"][0]["stdout"]
        assert done["peak_memory_bytes"] > 0
        assert done["environment_id"] == done["_environment"]["id"]
        assert done["parameters"] == {"count": 4}
        assert done["outputs"][0]["stdout"].endswith("5\n4\n")
        short = runtime.submit(notebook("print('value')"), parameters={"count": 1}, run_id="data-run",
            connections=[{"secrets": {"password": "a"}}])
        short_done = wait(store, short["id"])
        assert short_done["status"] == "succeeded" and short_done["outputs"][0]["cell_id"] == "one"
        assert "v[REDACTED]lue" in short_done["outputs"][0]["stdout"]
        failed = runtime.submit(notebook("raise MemoryError('example')"), parameters={"count": 1})
        assert wait(store, failed["id"])["error_class"] == "out_of_memory"
        slow = runtime.submit(notebook("import time; time.sleep(4)"), parameters={"count": 1}, timeout_seconds=1)
        assert wait(store, slow["id"])["error_class"] == "timeout"
    finally:
        runtime.close()
