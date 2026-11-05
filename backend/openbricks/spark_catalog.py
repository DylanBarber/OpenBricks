"""Configure baked-in Unity Catalog/Delta Spark integrations without downloads.

Connector versions are fixed by the compute image: Unity Catalog 0.7.0 and
Delta Spark 4.3.1 built for Spark 4.0 / Scala 2.13. The existing Hive catalog
remains the default; users address UC tables as catalog.schema.table.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

UC_CLASS = "io.unitycatalog.spark.UCSingleCatalog"
DELTA_CLASS = "org.apache.spark.sql.delta.catalog.DeltaCatalog"
DELTA_EXTENSION = "io.delta.sql.DeltaSparkSessionExtension"


def catalog_name(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) or value.lower() in (
        "spark_catalog",
        "system",
    ):
        raise ValueError(
            "Catalog names must use letters, digits, and underscores and cannot replace spark_catalog or system"
        )
    return value


def catalog_url():
    value = os.getenv("OPENBRICKS_CATALOG_URL", "").strip().rstrip("/")
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/").endswith("/api/2.1/unity-catalog")
    ):
        raise ValueError(
            "OPENBRICKS_CATALOG_URL must be the Unity Catalog server's HTTP(S) base URL without credentials or API suffix"
        )
    try:
        parsed.port
    except ValueError:
        raise ValueError("Invalid Unity Catalog server port")
    return value


def catalog_settings(name):
    name = catalog_name(name)
    url = catalog_url()
    if not url:
        raise ValueError(
            "Configure OPENBRICKS_CATALOG_URL before attaching a Unity Catalog catalog"
        )
    prefix = f"spark.sql.catalog.{name}"
    return {
        prefix: UC_CLASS,
        prefix + ".uri": url,
        prefix + ".auth.type": "static",
        prefix + ".auth.token": os.getenv("OPENBRICKS_CATALOG_TOKEN", "not-used")
        or "not-used",
    }


def configure_catalog(builder):
    url = catalog_url()
    directory = Path(os.getenv("OPENBRICKS_CATALOG_JAR_DIR", "/opt/openbricks/jars"))
    jars = sorted(path.resolve() for path in directory.glob("*.jar") if path.is_file())
    names = [path.name for path in jars]
    delta_available = any("delta-spark_4.0_2.13" in name for name in names)
    if url and (
        not any("unitycatalog-spark_4.0_2.13" in name for name in names)
        or not delta_available
    ):
        raise ValueError(
            "Unity Catalog Spark 4.0 and Delta jars are missing from the compute image; rebuild the compute image with catalog support"
        )
    if jars:
        builder = builder.config("spark.jars", ",".join(str(path) for path in jars))
    if delta_available:
        builder = builder.config("spark.sql.extensions", DELTA_EXTENSION)
        builder = builder.config("spark.sql.catalog.spark_catalog", DELTA_CLASS)
    configured = os.getenv("OPENBRICKS_CATALOG_NAMES", "unity")
    names = list(
        dict.fromkeys(part.strip() for part in configured.split(",") if part.strip())
    )
    for name in names if url else []:
        for key, value in catalog_settings(name).items():
            builder = builder.config(key, value)
    # Do not set spark.sql.defaultCatalog: existing unqualified Hive SQL and
    # saveAsTable calls continue to target spark_catalog.
    endpoint = os.getenv("OPENBRICKS_S3_ENDPOINT", "").strip()
    if endpoint:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "OPENBRICKS_S3_ENDPOINT must be an HTTP(S) endpoint without embedded credentials"
            )
        settings = {
            "spark.hadoop.fs.s3a.endpoint": endpoint,
            "spark.hadoop.fs.s3a.path.style.access": "true",
            "spark.hadoop.fs.s3a.connection.ssl.enabled": (
                "true" if parsed.scheme == "https" else "false"
            ),
            "spark.hadoop.fs.s3a.endpoint.region": os.getenv(
                "OPENBRICKS_S3_REGION", "us-east-1"
            ),
            "spark.hadoop.fs.s3a.access.key": os.getenv("OPENBRICKS_S3_ACCESS_KEY", ""),
            "spark.hadoop.fs.s3a.secret.key": os.getenv("OPENBRICKS_S3_SECRET_KEY", ""),
            "spark.hadoop.fs.s3a.aws.credentials.provider": "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
        }
        for key, value in settings.items():
            builder = builder.config(key, value)
    return builder


class SparkCatalogHelper:
    """Attach a newly created catalog to an already-running Spark session."""

    def __init__(self, spark_factory):
        self._spark_factory = spark_factory

    def attach(self, name):
        settings = catalog_settings(name)
        spark = self._spark_factory()
        for key, value in settings.items():
            spark.conf.set(key, value)
        return {"catalog": name, "attached": True}
