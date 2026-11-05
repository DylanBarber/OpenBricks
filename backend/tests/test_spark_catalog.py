import pytest

from openbricks.spark_catalog import SparkCatalogHelper, configure_catalog


class Builder:
    def __init__(self):
        self.values = {}

    def config(self, key, value):
        self.values[key] = value
        return self


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_CATALOG_URL", "http://catalog:8080/")
    monkeypatch.setenv("OPENBRICKS_CATALOG_TOKEN", "catalog-test-token")
    monkeypatch.setenv("OPENBRICKS_CATALOG_JAR_DIR", str(tmp_path))
    monkeypatch.delenv("OPENBRICKS_S3_ENDPOINT", raising=False)
    for name in ("unitycatalog-spark_4.0_2.13-0.7.0.jar", "delta-spark_4.0_2.13-4.3.1.jar", "delta-storage-4.3.1.jar"):
        (tmp_path / name).write_bytes(b"fixture")
    return tmp_path


def test_disabled_catalog_does_not_change_spark_defaults(monkeypatch):
    monkeypatch.delenv("OPENBRICKS_CATALOG_URL", raising=False)
    builder = Builder()
    assert configure_catalog(builder) is builder and builder.values == {}


def test_catalog_uses_local_jars_and_preserves_default_hive(catalog_env, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_CATALOG_NAMES", "unity,analytics,unity")
    values = configure_catalog(Builder()).values
    assert "spark.jars.packages" not in values
    assert "spark.sql.defaultCatalog" not in values
    assert len(values["spark.jars"].split(",")) == 3
    assert values["spark.sql.catalog.unity"] == "io.unitycatalog.spark.UCSingleCatalog"
    assert values["spark.sql.catalog.analytics.uri"] == "http://catalog:8080"
    assert values["spark.sql.catalog.analytics.auth.type"] == "static"
    assert values["spark.sql.catalog.analytics.auth.token"] == "catalog-test-token"
    assert values["spark.sql.catalog.spark_catalog"] == "org.apache.spark.sql.delta.catalog.DeltaCatalog"


def test_catalog_rejects_missing_jars_invalid_names_and_api_suffix(catalog_env, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_CATALOG_NAMES", "spark_catalog")
    with pytest.raises(ValueError, match="cannot replace"):
        configure_catalog(Builder())
    monkeypatch.setenv("OPENBRICKS_CATALOG_NAMES", "bad.name")
    with pytest.raises(ValueError, match="Catalog names"):
        configure_catalog(Builder())
    monkeypatch.setenv("OPENBRICKS_CATALOG_URL", "http://catalog:8080/api/2.1/unity-catalog")
    with pytest.raises(ValueError, match="base URL"):
        configure_catalog(Builder())
    monkeypatch.setenv("OPENBRICKS_CATALOG_URL", "http://catalog:8080")
    monkeypatch.setenv("OPENBRICKS_CATALOG_NAMES", "unity")
    for path in catalog_env.glob("*.jar"):
        path.unlink()
    with pytest.raises(ValueError, match="jars are missing"):
        configure_catalog(Builder())


def test_s3a_settings_and_runtime_attach(catalog_env, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_CATALOG_NAMES", "unity")
    monkeypatch.setenv("OPENBRICKS_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("OPENBRICKS_S3_ACCESS_KEY", "test-key")
    monkeypatch.setenv("OPENBRICKS_S3_SECRET_KEY", "test-secret")
    values = configure_catalog(Builder()).values
    assert values["spark.hadoop.fs.s3a.path.style.access"] == "true"
    assert values["spark.hadoop.fs.s3a.connection.ssl.enabled"] == "false"
    assert values["spark.hadoop.fs.s3a.secret.key"] == "test-secret"
    class Conf:
        def __init__(self):
            self.values = {}
        def set(self, key, value):
            self.values[key] = value
    class Spark:
        conf = Conf()
    spark = Spark()
    helper = SparkCatalogHelper(lambda: spark)
    assert helper.attach("new_catalog") == {"catalog": "new_catalog", "attached": True}
    assert spark.conf.values["spark.sql.catalog.new_catalog.uri"] == "http://catalog:8080"
