"""Spark-native, bounded data-quality reports for notebook and pipeline use."""

from __future__ import annotations

from datetime import datetime, timezone


class DataQualityError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("Data quality checks failed; inspect the quality report")


def check_data(frame, checks, *, fail_on_error=True, emit=None, dataset=None):
    from pyspark.sql import functions as F

    if not isinstance(checks, list) or not 1 <= len(checks) <= 100:
        raise ValueError("Provide 1–100 quality checks")
    results = []
    total = frame.count()

    def col(name):
        return F.col("`" + name.replace("`", "``") + "`")

    for index, check in enumerate(checks):
        kind = check.get("type")
        column = check.get("column")
        if kind not in ("not_null", "unique", "range", "row_count", "freshness"):
            raise ValueError("Unsupported data quality check")
        if kind != "row_count" and column not in frame.columns:
            raise ValueError("Quality check column does not exist")
        failed = 0
        actual = None
        if kind == "not_null":
            failed = frame.filter(col(column).isNull()).count()
        elif kind == "unique":
            # Count excess rows, including duplicate nulls, rather than groups.
            failed = total - frame.select(col(column)).distinct().count()
        elif kind == "range":
            if check.get("min") is None and check.get("max") is None:
                raise ValueError("Range checks need a minimum or maximum")
            invalid = col(column).isNull()
            if check.get("min") is not None:
                invalid = invalid | (col(column) < F.lit(check["min"]))
            if check.get("max") is not None:
                invalid = invalid | (col(column) > F.lit(check["max"]))
            failed = frame.filter(invalid).count()
        elif kind == "row_count":
            if check.get("min") is None and check.get("max") is None:
                raise ValueError("Row-count checks need a minimum or maximum")
            actual = total
            failed = int(
                (check.get("min") is not None and total < check["min"])
                or (check.get("max") is not None and total > check["max"])
            )
        else:
            seconds = float(check.get("max_age_seconds", 0))
            if seconds <= 0:
                raise ValueError("Freshness checks need a positive max_age_seconds")
            latest = frame.agg(
                F.max(col(column).cast("timestamp")).alias("latest")
            ).first()["latest"]
            if latest is not None:
                latest = latest.astimezone(timezone.utc)
                actual = (datetime.now(timezone.utc) - latest).total_seconds()
            failed = int(actual is None or actual > seconds)
        results.append(
            {
                "index": index,
                "name": check.get("name") or f"{kind}:{column or 'rows'}",
                "type": kind,
                "column": column,
                "passed": failed == 0,
                "failed_rows": failed,
                "actual": actual,
            }
        )
    report = {
        "dataset": dataset,
        "row_count": total,
        "passed": all(item["passed"] for item in results),
        "checks": results,
    }
    if emit:
        emit(report)
    if fail_on_error and not report["passed"]:
        raise DataQualityError(report)
    return report
