"""The shared Spark fixture must release its process before worker tests start."""

import subprocess
import sys
from types import SimpleNamespace

from conftest import shutdown_owned_spark_gateway


def test_owned_gateway_process_is_reaped_and_can_be_replaced():
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
    )
    gateway = SimpleNamespace(proc=process, shutdown=lambda: None)
    context = SimpleNamespace(
        _gateway=gateway, _jvm=object(), _active_spark_context=None
    )
    sessions = SimpleNamespace(_instantiatedSession=object(), _activeSession=object())
    try:
        shutdown_owned_spark_gateway(context, None, sessions)
        assert process.poll() == 0
        assert context._gateway is context._jvm is context._active_spark_context is None
        assert sessions._instantiatedSession is sessions._activeSession is None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_borrowed_or_external_gateway_is_not_stopped():
    def forbidden():
        raise AssertionError("A borrowed JVM must be left running")

    gateway = SimpleNamespace(proc=object(), shutdown=forbidden)
    context = SimpleNamespace(_gateway=gateway)
    shutdown_owned_spark_gateway(context, gateway)
    assert context._gateway is gateway
    external = SimpleNamespace(proc=None, shutdown=forbidden)
    context._gateway = external
    shutdown_owned_spark_gateway(context, None)
    assert context._gateway is external
