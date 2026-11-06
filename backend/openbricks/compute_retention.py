"""Bounded, compute-local output cleanup without deleting run identities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import threading

TERMINAL = "status IN ('succeeded','failed','cancelled','interrupted','unknown')"
ELIGIBLE = f"kind='runs' AND {TERMINAL} AND compute_output_bytes>0"
BATCH_SIZE = 100
INTERVAL_SECONDS = 300
SETTLING_SECONDS = 60
HEAVY_PATHS = (
    "outputs",
    "logs",
    "data_events",
    "data_result",
    "quality_reports",
    "lineage",
    "profile",
    "_cells",
    "_notebook",
    "_environment",
    "_execution_spec",
    "parameters",
)
logger = logging.getLogger(__name__)


def configured_integer(name, default, maximum):
    value = os.getenv(name, "").strip()
    try:
        result = int(value) if value else default
    except ValueError:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}") from None
    if not 1 <= result <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return result


class ComputeRetention:
    def __init__(self, store):
        self.store = store
        self.days = configured_integer(
            "OPENBRICKS_COMPUTE_OUTPUT_RETENTION_DAYS", 30, 3650
        )
        self.budget_bytes = (
            configured_integer("OPENBRICKS_COMPUTE_OUTPUT_BUDGET_MB", 500, 100000)
            * 1024
            * 1024
        )
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self._loop, name="openbricks-compute-retention", daemon=True
        )
        self.last_cleanup = None
        self._install()

    def _install(self):
        with self.store.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
            if "compute_output_bytes" not in columns:
                db.execute(
                    "ALTER TABLE documents ADD COLUMN compute_output_bytes INTEGER"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS compute_retention_totals "
                "(id INTEGER PRIMARY KEY CHECK(id=1), bytes INTEGER NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO compute_retention_totals VALUES(1,0)")
            # Each ordinary document write maintains byte accounting. Only a
            # bounded batch of pre-migration rows is decoded during each sweep.
            size = (
                "CASE WHEN json_extract(NEW.body,'$.outputs_expired')=1 THEN 0 "
                "ELSE length(CAST(NEW.body AS BLOB)) END"
            )
            for event, suffix in (("INSERT", "insert"), ("UPDATE OF body", "body")):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS compute_output_{suffix} "
                    f"AFTER {event} ON documents WHEN NEW.kind='runs' BEGIN "
                    f"UPDATE documents SET compute_output_bytes={size} "
                    "WHERE kind=NEW.kind AND id=NEW.id; END"
                )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS compute_output_total_insert "
                "AFTER INSERT ON documents WHEN NEW.kind='runs' BEGIN "
                "UPDATE compute_retention_totals SET bytes=bytes+COALESCE(NEW.compute_output_bytes,0) WHERE id=1; END"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS compute_output_total_update "
                "AFTER UPDATE OF compute_output_bytes ON documents WHEN NEW.kind='runs' BEGIN "
                "UPDATE compute_retention_totals SET bytes=bytes+COALESCE(NEW.compute_output_bytes,0)-COALESCE(OLD.compute_output_bytes,0) WHERE id=1; END"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS compute_output_total_delete "
                "AFTER DELETE ON documents WHEN OLD.kind='runs' BEGIN "
                "UPDATE compute_retention_totals SET bytes=bytes-COALESCE(OLD.compute_output_bytes,0) WHERE id=1; END"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS compute_output_migration ON documents(kind,compute_output_bytes,id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS compute_output_expiry ON documents"
                f"(COALESCE(finished_at,created_at),id) WHERE {ELIGIBLE}"
            )

    def cleanup(self, instant=None):
        instant = instant or datetime.now(timezone.utc)
        cutoff = (instant - timedelta(days=self.days)).isoformat()
        settled = (instant - timedelta(seconds=SETTLING_SECONDS)).isoformat()
        removed = 0
        with self.store.connect() as db:
            unmeasured = db.execute(
                "SELECT id FROM documents WHERE kind='runs' AND compute_output_bytes IS NULL ORDER BY id LIMIT ?",
                (BATCH_SIZE,),
            ).fetchall()
            for (run_id,) in unmeasured:
                # Reuse the ordinary write trigger; no JSON is loaded in Python.
                db.execute(
                    "UPDATE documents SET body=body WHERE kind='runs' AND id=?",
                    (run_id,),
                )
            expired = db.execute(
                f"SELECT id FROM documents WHERE {ELIGIBLE} "
                "AND COALESCE(finished_at,created_at)<=? "
                "ORDER BY COALESCE(finished_at,created_at),id LIMIT ?",
                (min(cutoff, settled), BATCH_SIZE),
            ).fetchall()
            for (run_id,) in expired:
                self._compact(db, run_id, instant)
                removed += 1
            total = db.execute(
                "SELECT bytes FROM compute_retention_totals WHERE id=1"
            ).fetchone()[0]
            if total > self.budget_bytes and removed < BATCH_SIZE:
                candidates = db.execute(
                    f"SELECT id,compute_output_bytes FROM documents WHERE {ELIGIBLE} "
                    "AND COALESCE(finished_at,created_at)<=? "
                    "ORDER BY COALESCE(finished_at,created_at),id LIMIT ?",
                    (settled, BATCH_SIZE - removed),
                ).fetchall()
                for run_id, size in candidates:
                    if total <= self.budget_bytes:
                        break
                    self._compact(db, run_id, instant)
                    total -= size
                    removed += 1
            result = {
                "checked_at": instant.isoformat(),
                "backfilled_runs": len(unmeasured),
                "compacted_runs": removed,
                "retained_bytes": db.execute(
                    "SELECT bytes FROM compute_retention_totals WHERE id=1"
                ).fetchone()[0],
            }
        self.last_cleanup = result
        return result

    @staticmethod
    def _compact(db, run_id, instant):
        paths = ",".join(f"'$.{name}'" for name in HEAVY_PATHS)
        # Preserve unknown metadata, request fingerprints, notebook identity,
        # final status and timestamps. A duplicate ID must never rerun a write.
        compacted = (
            f"json_set(json_remove(body,{paths}),"
            "'$.outputs',json('[]'),'$.logs',json('[]'),"
            "'$.error',substr(json_extract(body,'$.error'),1,2000),"
            "'$.outputs_expired',json('true'),'$.compute_outputs_expired_at',:expired_at)"
        )
        # Management may have been disconnected when the sink committed. Keep
        # the tiny CAS watermark outcome, never the preview rows, so reattaching
        # can finalize the original ingestion without replaying its writes.
        checkpoint = (
            "json_object('ingestion_id',json_extract(body,'$.data_result.ingestion_id'),"
            "'previous_watermark',json_extract(body,'$.data_result.previous_watermark'),"
            "'watermark',json_extract(body,'$.data_result.watermark'))"
        )
        db.execute(
            "UPDATE documents SET body=CASE WHEN "
            "json_type(body,'$.data_result.ingestion_id')='text' "
            f"THEN json_set({compacted},'$.data_result',{checkpoint}) "
            f"ELSE {compacted} END "
            f"WHERE {ELIGIBLE} AND id=:run_id",
            {"expired_at": instant.isoformat(), "run_id": run_id},
        )

    def start(self):
        self.cleanup()
        self.thread.start()

    def _loop(self):
        while not self.stopped.wait(INTERVAL_SECONDS):
            try:
                self.cleanup()
            except Exception:
                # Do not log database contents or notebook output on failure.
                logger.error(
                    "Compute output retention failed; retrying at the next interval"
                )

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def status(self):
        return {
            "retention_days": self.days,
            "budget_bytes": self.budget_bytes,
            "interval_seconds": INTERVAL_SECONDS,
            "batch_size": BATCH_SIZE,
            "last_cleanup": self.last_cleanup,
        }
