"""Ownership boundaries for resources started by integration-test fixtures."""

from contextlib import suppress
import subprocess
import sys

import pytest


def shutdown_owned_spark_gateway(context, previous_gateway, session_type=None):
    """Reap only a local JVM created inside this fixture's lifetime.

    SparkSession.stop() stops SparkContext but intentionally keeps PySpark's Java
    gateway available for reuse. That retains its heap and native allocations
    throughout later worker-process tests. A module-scoped owner must close the
    gateway too, after every test sharing that SparkSession has finished.
    """
    gateway = context._gateway
    if gateway is None or gateway is previous_gateway:
        return
    process = getattr(gateway, "proc", None)
    if process is None:
        # PYSPARK_GATEWAY_PORT connects to a pre-existing external JVM. The test
        # never owns that process and must not send it a shutdown command.
        return
    try:
        with suppress(Exception):
            gateway.shutdown()
        if process.stdin is not None:
            with suppress(OSError):
                # PySpark launches its gateway with a stdin lifetime pipe.
                process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    finally:
        # A later module must launch its own live gateway instead of reusing
        # references to the JVM just reaped above.
        if context._gateway is gateway:
            context._gateway = None
            context._jvm = None
            context._active_spark_context = None
            if session_type is not None:
                session_type._instantiatedSession = None
                session_type._activeSession = None


@pytest.fixture(scope="module")
def owned_spark_gateway():
    # Do not import PySpark or start Java when an opt-in test is skipped.
    pyspark = sys.modules.get("pyspark")
    context = getattr(pyspark, "SparkContext", None)
    previous_gateway = getattr(context, "_gateway", None)
    yield
    pyspark = sys.modules.get("pyspark")
    context = getattr(pyspark, "SparkContext", None)
    if context is not None:
        session_type = getattr(
            sys.modules.get("pyspark.sql.session"), "SparkSession", None
        )
        shutdown_owned_spark_gateway(context, previous_gateway, session_type)
