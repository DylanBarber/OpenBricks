"""Delta operations on a compute session, independent of Unity Catalog."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def table_identifier(value):
    parts = str(value).split(".")
    if not 1 <= len(parts) <= 3 or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts
    ):
        raise ValueError("Use a one-, two-, or three-part table identifier")
    return ".".join("`" + part + "`" for part in parts)


def data_path(value, blob):
    value = str(value)
    if value.startswith("blob://"):
        return blob.path(value)
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("s3a", "s3", "abfss", "gs")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Use a local blob:// path or configured cloud object-storage URI"
        )
    if any(part in (".", "..") for part in parsed.path.split("/")):
        raise ValueError("Dataset paths cannot contain traversal segments")
    return value.replace("s3://", "s3a://", 1)


class DeltaTools:
    def __init__(self, spark_factory, blob, lineage=None):
        self.spark_factory, self.blob, self.lineage = spark_factory, blob, lineage

    def _target(self, *, path=None, table=None):
        if bool(path) == bool(table):
            raise ValueError("Specify exactly one Delta table or path")
        if table:
            return table_identifier(table), table
        resolved = data_path(path, self.blob)
        return "delta.`" + resolved.replace("`", "``") + "`", path

    def history(self, *, path=None, table=None, limit=100):
        target, _ = self._target(path=path, table=table)
        return self.spark_factory().sql(
            f"DESCRIBE HISTORY {target} LIMIT {max(1, min(int(limit), 1000))}"
        )

    def detail(self, *, path=None, table=None):
        target, _ = self._target(path=path, table=table)
        return self.spark_factory().sql(f"DESCRIBE DETAIL {target}")

    def read(self, *, path=None, table=None, version=None, timestamp=None):
        if version is not None and timestamp is not None:
            raise ValueError("Choose version or timestamp, not both")
        reader = self.spark_factory().read.format("delta")
        if version is not None:
            if int(version) < 0:
                raise ValueError("Delta version cannot be negative")
            reader = reader.option("versionAsOf", int(version))
        if timestamp is not None:
            reader = reader.option("timestampAsOf", str(timestamp))
        self._target(path=path, table=table)
        frame = (
            reader.table(table) if table else reader.load(data_path(path, self.blob))
        )
        if self.lineage:
            self.lineage(
                table or path,
                "read",
                {"format": "delta", "version": version, "timestamp": timestamp},
            )
        return frame

    def restore(self, *, path=None, table=None, version):
        if int(version) < 0:
            raise ValueError("Delta version cannot be negative")
        target, dataset = self._target(path=path, table=table)
        result = self.spark_factory().sql(
            f"RESTORE TABLE {target} TO VERSION AS OF {int(version)}"
        )
        if self.lineage:
            self.lineage(
                dataset, "write", {"operation": "restore", "version": int(version)}
            )
        return result

    def vacuum(self, *, path=None, table=None, retention_hours=168, dry_run=True):
        hours = float(retention_hours)
        if not 168 <= hours <= 876000:
            raise ValueError("Vacuum retention must be at least 168 hours")
        target, dataset = self._target(path=path, table=table)
        result = self.spark_factory().sql(
            f"VACUUM {target} RETAIN {hours:g} HOURS" + (" DRY RUN" if dry_run else "")
        )
        if self.lineage and not dry_run:
            self.lineage(
                dataset, "write", {"operation": "vacuum", "retention_hours": hours}
            )
        return result
