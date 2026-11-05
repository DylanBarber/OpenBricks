"""Resource accounting, retention, and streamed portable workspace backups."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import ExitStack, nullcontext, contextmanager, closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import tarfile
import tempfile
import threading
import time
from uuid import uuid4

import psutil
import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .permissions import now
from .security import confined_path

MAGIC = b"OBBK1\n"
DEFAULT_RETENTION = {
    "output_days": 30,
    "run_days": 90,
    "max_output_mb": 500,
    "backup_keep": 5,
}
DATA_DIRS = (
    "notebooks",
    "repositories",
    "wheels",
    "site-packages",
    "artifacts",
    "environments",
    "buckets",
    "warehouse",
    "metastore",
    "home",
)
OUTPUT_FIELDS = (
    "outputs",
    "logs",
    "data_events",
    "data_result",
    "quality_reports",
    "lineage",
)


def archive_name(value):
    """Require one portable canonical name for every extracted destination."""
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise ValueError("Backup contains an unsafe path")
    parts = value.split("/")
    if any(
        part in ("", ".", "..")
        or part.endswith((".", " "))
        or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I)
        for part in parts
    ):
        raise ValueError("Backup contains a noncanonical or unsafe path")
    if PurePosixPath(value).is_absolute() or value != PurePosixPath(value).as_posix():
        raise ValueError("Backup contains a noncanonical or unsafe path")
    return value


class DigestReader:
    def __init__(self, stream):
        self.stream, self.digest = stream, hashlib.sha256()

    def read(self, size=-1):
        chunk = self.stream.read(size)
        self.digest.update(chunk)
        return chunk


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def crypt_file(source, destination, passphrase, decrypt=False):
    with Path(source).open("rb") as src, Path(destination).open("wb") as dst:
        if decrypt:
            if src.read(len(MAGIC)) != MAGIC:
                raise ValueError("Unsupported encrypted backup")
            salt, nonce = src.read(16), src.read(12)
            length = Path(source).stat().st_size - len(MAGIC) - 16 - 12 - 16
            if length < 0:
                raise ValueError("Damaged encrypted backup")
            src.seek(-16, 2)
            tag = src.read(16)
            src.seek(len(MAGIC) + 28)
        else:
            salt, nonce, tag = os.urandom(16), os.urandom(12), None
            dst.write(MAGIC + salt + nonce)
            length = Path(source).stat().st_size
        key = PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000
        ).derive(passphrase.encode())
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag))
        worker = cipher.decryptor() if decrypt else cipher.encryptor()
        remaining = length
        while remaining:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("Damaged backup")
            dst.write(worker.update(chunk))
            remaining -= len(chunk)
        try:
            dst.write(worker.finalize())
        except InvalidTag as exc:
            raise ValueError("Incorrect passphrase or damaged backup") from exc
        if not decrypt:
            dst.write(worker.tag)


def build_archive(store, destination, include_data=True):
    root = store.directory
    destination = Path(destination)
    manifest = {
        "format": "openbricks-workspace",
        "version": 1,
        "created_at": now(),
        "files": {},
        "include_data": include_data,
        "external_secret_key": bool(os.getenv("OPENBRICKS_SECRET_KEY")),
    }
    with tempfile.TemporaryDirectory(
        prefix="openbricks-snapshot-", dir=destination.parent
    ) as temporary:
        snapshot = Path(temporary) / "openbricks.sqlite3"
        with store.connect() as source:
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
                # Backup binaries live outside the archive. Do not restore
                # stale absolute download paths or an in-progress self-record.
                target.execute("DELETE FROM documents WHERE kind='backups'")
                target.commit()
        paths = [(snapshot, "openbricks.sqlite3")]
        if (root / "secrets.key").is_file():
            paths.append((root / "secrets.key", "secrets.key"))
        directories = DATA_DIRS if include_data else ("notebooks",)
        for directory in directories:
            base = root / directory
            if not base.is_dir() or base.is_symlink():
                continue
            for current, folders, files in os.walk(base, followlinks=False):
                folders[:] = [
                    name for name in folders if not (Path(current) / name).is_symlink()
                ]
                for name in files:
                    path = Path(current) / name
                    if not path.is_symlink() and path.is_file():
                        paths.append((path, path.relative_to(root).as_posix()))
        with tarfile.open(destination, "w:gz") as archive:
            if len(paths) + 1 > 100_000:
                raise ValueError("Workspace has too many files for this backup format")
            total_size = 0
            for path, relative in paths:
                archive_name(relative)
                info = archive.gettarinfo(str(path), arcname=relative)
                total_size += info.size
                if not info.isfile() or total_size > 20 * 1024**3:
                    raise ValueError(
                        "Workspace exceeds the supported backup size or changed during backup"
                    )
                with path.open("rb") as stream:
                    reader = DigestReader(stream)
                    archive.addfile(info, reader)
                manifest["files"][relative] = {
                    "sha256": reader.digest.hexdigest(),
                    "size": info.size,
                }
            info = tarfile.TarInfo("manifest.json")
            content = json.dumps(manifest, sort_keys=True).encode()
            info.size = len(content)
            import io

            archive.addfile(info, io.BytesIO(content))
    return manifest


def extract_archive(source, staging, passphrase=""):
    source, staging = Path(source), Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    if any(staging.iterdir()):
        raise ValueError("Backup staging directory must be empty")
    with source.open("rb") as stream:
        encrypted = stream.read(len(MAGIC)) == MAGIC
    archive_path = source
    try:
        if encrypted:
            if not passphrase:
                raise ValueError("This backup requires a passphrase")
            archive_path = staging / ".decrypted.tar.gz"
            crypt_file(source, archive_path, passphrase, decrypt=True)
        with tarfile.open(archive_path, "r|gz") as archive:
            names, folded_names = set(), set()
            total_size, count = 0, 0
            while (member := archive.next()) is not None:
                count += 1
                total_size += member.size
                if count > 100_000 or member.size < 0 or total_size > 20 * 1024**3:
                    raise ValueError("Backup exceeds the supported extraction size")
                archive_name(member.name)
                if not member.isfile() or member.name.casefold() in folded_names:
                    raise ValueError("Backup contains unsafe or duplicate entries")
                names.add(member.name)
                folded_names.add(member.name.casefold())
                if member.name != "manifest.json" and member.name.split("/")[0] not in (
                    *DATA_DIRS,
                    "openbricks.sqlite3",
                    "secrets.key",
                ):
                    raise ValueError("Backup contains an unsupported root path")
                if member.name == "manifest.json" and member.size > 20 * 1024**2:
                    raise ValueError("Backup manifest is too large")
                if member.size > shutil.disk_usage(staging).free:
                    raise ValueError("Not enough free space to extract this backup")
                target = confined_path(staging, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                target.chmod(member.mode & 0o777)
                archive.members.clear()  # No need to retain streamed headers.
        manifest_path = staging / "manifest.json"
        if not manifest_path.is_file() or manifest_path.stat().st_size > 20 * 1024**2:
            raise ValueError("Backup manifest is missing or too large")
        manifest = json.loads(manifest_path.read_text())
        if (
            manifest.get("format") != "openbricks-workspace"
            or manifest.get("version") != 1
        ):
            raise ValueError("Unsupported backup format")
        expected = manifest.get("files", {})
        if not isinstance(expected, dict):
            raise ValueError("Backup manifest files must be an object")
        if set(expected) != names - {"manifest.json"}:
            raise ValueError("Backup contents do not match its manifest")
        for relative, metadata in expected.items():
            archive_name(relative)
            if (
                not isinstance(metadata, dict)
                or not isinstance(metadata.get("size"), int)
                or metadata["size"] < 0
                or not isinstance(metadata.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
            ):
                raise ValueError("Backup manifest metadata is invalid")
            target = confined_path(staging, relative)
            if (
                target.stat().st_size != metadata["size"]
                or file_digest(target) != metadata["sha256"]
            ):
                raise ValueError("Backup checksum validation failed")
        database = staging / "openbricks.sqlite3"
        if not database.is_file():
            raise ValueError("Backup database is missing")
        with closing(sqlite3.connect(database)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup database integrity check failed")
            if db.execute(
                "SELECT type FROM sqlite_master WHERE name='documents'"
            ).fetchone() != ("table",):
                raise ValueError("Backup documents must be an ordinary table")
            db.execute("SELECT kind,id,body FROM documents LIMIT 1")
            protected = db.execute(
                "SELECT json_extract(body,'$._secrets'),json_extract(body,'$._token') FROM documents WHERE kind IN ('connections','clusters','notification_targets')"
            )
            box = None
            for row in protected:
                for value in row:
                    if value:
                        from .credentials import SecretBox

                        if not (staging / "secrets.key").is_file() and not os.getenv(
                            "OPENBRICKS_SECRET_KEY"
                        ):
                            raise ValueError(
                                "Backup requires the original encryption key"
                            )
                        box = box or SecretBox(staging)
                        box.decrypt(value)
        return manifest
    except (
        tarfile.TarError,
        KeyError,
        TypeError,
        OSError,
        json.JSONDecodeError,
        sqlite3.DatabaseError,
    ) as exc:
        raise ValueError("Invalid or damaged OpenBricks backup") from exc
    finally:
        if encrypted and archive_path != source:
            archive_path.unlink(missing_ok=True)


def retention_preview(store, policy):
    output_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=policy["output_days"])
    ).isoformat()
    run_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=policy["run_days"])
    ).isoformat()
    with store.connect() as db:
        deletions = db.execute(
            "SELECT COUNT(*) FROM documents WHERE kind IN ('runs','workflow_runs') AND status NOT IN ('running','queued') AND created_at<?",
            (run_cutoff,),
        ).fetchone()[0]
        size_sql = "+".join(
            "length(COALESCE(json_extract(body,'$." + field + "'),''))"
            for field in OUTPUT_FIELDS
        )
        rows = db.execute(
            "SELECT id,created_at,"
            + size_sql
            + " FROM documents WHERE kind='runs' AND status NOT IN ('running','queued') ORDER BY created_at DESC"
        ).fetchall()
    retained, selected, reclaimed = 0, [], 0
    for id, created, size in rows:
        retained += size
        if size > 4 and (
            created < output_cutoff or retained > policy["max_output_mb"] * 1024**2
        ):
            selected.append(id)
            reclaimed += size
    return {
        "outputs_to_clear": len(selected),
        "runs_to_delete": deletions,
        "bytes_to_reclaim": reclaimed,
        "_output_ids": selected,
        "_run_cutoff": run_cutoff,
    }


class RetentionInput(BaseModel):
    output_days: int = Field(default=30, ge=1, le=3650)
    run_days: int = Field(default=90, ge=1, le=3650)
    max_output_mb: int = Field(default=500, ge=1, le=100_000)
    backup_keep: int = Field(default=5, ge=1, le=100)


class BackupInput(BaseModel):
    name: str = Field(default="Workspace backup", max_length=120)
    passphrase: str = Field(default="", max_length=1024)
    include_data: bool = True


class Operations:
    def __init__(self, app):
        self.app = app
        self.store = app.state.store
        self.lock = threading.Lock()
        self.mutation_condition = threading.Condition()
        self.active_mutations = 0
        self.metrics_lock = threading.Lock()
        self.metrics_cached_at = 0
        self.metrics_cache = []
        self.metrics_refreshing = False
        self.metrics_cursor = 0
        self.metrics_thread = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._loop, name="openbricks-retention", daemon=True
        )
        (self.store.directory / "backups").mkdir(exist_ok=True)

    def policy(self):
        return {
            **DEFAULT_RETENTION,
            **{
                key: value
                for key, value in (
                    self.store.get("settings", "retention") or {}
                ).items()
                if key in DEFAULT_RETENTION
            },
        }

    def enter_mutation(self):
        with self.mutation_condition:
            if getattr(self.app.state, "maintenance", None):
                return False
            self.active_mutations += 1
            return True

    def leave_mutation(self):
        with self.mutation_condition:
            self.active_mutations = max(0, self.active_mutations - 1)
            self.mutation_condition.notify_all()

    def begin_maintenance(self, message):
        with self.mutation_condition:
            self.app.state.maintenance = message
            if not self.mutation_condition.wait_for(
                lambda: self.active_mutations == 0, timeout=30
            ):
                self.app.state.maintenance = None
                raise HTTPException(
                    409, "Wait for active workspace changes before backup or restore"
                )

    def _restart_required(self, message, auto_restart=True):
        self.app.state.restore_required = True
        self.app.state.maintenance = message
        if auto_restart and os.getenv("OPENBRICKS_MANAGED_CONTAINER") == "1":
            timer = threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
            timer.daemon = True
            timer.start()

    @contextmanager
    def _workspace_guard(self, include_workflows=True):
        with ExitStack() as stack:
            workflows = getattr(self.app.state, "workflows", None)
            if include_workflows:
                stack.enter_context(getattr(workflows, "lock", nullcontext()))
            runtime = self.app.state.runtime
            if hasattr(runtime, "environment_change"):
                stack.enter_context(runtime.environment_change())
            yield

    def live_metrics(self):
        from .clusters import all_clusters

        entries = all_clusters(self.store)
        with self.metrics_lock:
            known = {item["id"]: item for item in self.metrics_cache}
            self.metrics_cache = [
                known.get(
                    entry["id"],
                    {
                        "id": entry["id"],
                        "name": entry["name"],
                        "status": "refreshing",
                        "measured_at": None,
                    },
                )
                for entry in entries
            ]
            if (
                not self.stop_event.is_set()
                and not self.metrics_refreshing
                and time.monotonic() - self.metrics_cached_at >= 10
                and entries
            ):
                self.metrics_refreshing = True
                start = self.metrics_cursor % len(entries)
                selected = (entries[start:] + entries[:start])[:16]
                self.metrics_cursor = start + len(selected)
                self.metrics_thread = threading.Thread(
                    target=self._refresh_metrics,
                    args=(selected,),
                    daemon=True,
                    name="openbricks-resource-poll",
                )
                self.metrics_thread.start()
            return [dict(item) for item in self.metrics_cache]

    def _refresh_metrics(self, entries):
        from .clusters import client_for

        def fetch(entry):
            measured = now()
            try:
                if not entry.get("url"):
                    from .compute import memory_metrics

                    total, used = memory_metrics()
                    value = {
                        "status": (
                            "busy" if self.app.state.runtime.active_count else "idle"
                        ),
                        "memory_used": used,
                        "memory_total": total,
                        "active_runs": self.app.state.runtime.active_count,
                        "queued_runs": self.app.state.runtime.queued_count,
                    }
                else:
                    with client_for(self.store, entry) as client:
                        response = client.get(
                            "/status", timeout=httpx.Timeout(2, connect=0.5)
                        )
                        response.raise_for_status()
                        value = response.json()
                        if (
                            not isinstance(value, dict)
                            or value.get("service") != "openbricks-compute"
                        ):
                            raise ValueError("Invalid compute status")
                return {
                    "id": entry["id"],
                    "name": entry["name"],
                    "status": value.get("status", "unknown"),
                    "measured_at": measured,
                    **{
                        key: value.get(key)
                        for key in (
                            "memory_used",
                            "memory_total",
                            "active_runs",
                            "queued_runs",
                            "cpu_percent",
                        )
                    },
                }
            except Exception:
                return {
                    "id": entry["id"],
                    "name": entry["name"],
                    "status": "unavailable",
                    "measured_at": measured,
                }

        try:
            with ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="openbricks-metrics"
            ) as pool:
                values = list(pool.map(fetch, entries))
            with self.metrics_lock:
                refreshed = {item["id"]: item for item in values}
                self.metrics_cache = [
                    refreshed.get(item["id"], item) for item in self.metrics_cache
                ]
                self.metrics_cached_at = time.monotonic()
        finally:
            with self.metrics_lock:
                self.metrics_refreshing = False

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
        if self.metrics_thread and self.metrics_thread.is_alive():
            self.metrics_thread.join(timeout=3)

    def _loop(self):
        while not self.stop_event.wait(3600):
            try:
                self.cleanup()
            except Exception:
                continue

    def cleanup(self):
        if not self.lock.acquire(blocking=False):
            raise HTTPException(409, "A backup or cleanup is already running")
        try:
            policy = self.policy()
            preview = retention_preview(self.store, policy)
            with self.store.connect() as db:
                for id in preview["_output_ids"]:
                    db.execute(
                        "UPDATE documents SET body=json_set(json_remove(body,'$.data_result','$.data_events','$.quality_reports','$.lineage'),'$.outputs',json('[]'),'$.logs',json('[]'),'$.outputs_expired',json('true')) WHERE kind='runs' AND id=? AND status NOT IN ('running','queued')",
                        (id,),
                    )
                db.execute(
                    "DELETE FROM documents WHERE kind IN ('runs','workflow_runs') AND status NOT IN ('running','queued') AND created_at<?",
                    (preview["_run_cutoff"],),
                )
                expired = (
                    datetime.now(timezone.utc) - timedelta(days=policy["run_days"])
                ).isoformat()
                db.execute(
                    "DELETE FROM documents WHERE kind IN ('audit_events','notification_deliveries','data_executions','lineage','data_lineage') AND created_at<?",
                    (expired,),
                )
            for backup in self.store.list("backups")[policy["backup_keep"] :]:
                if backup.get("status") == "succeeded":
                    path = Path(backup["_path"])
                    if not path.is_symlink() and path.resolve().is_relative_to(
                        (self.store.directory / "backups").resolve()
                    ):
                        path.unlink(missing_ok=True)
                    self.store.delete("backups", backup["id"])
            summary = {
                key: value for key, value in preview.items() if not key.startswith("_")
            }
            self.store.put(
                "settings", {"id": "last_cleanup", **summary, "finished_at": now()}
            )
            return summary
        finally:
            self.lock.release()

    def require_idle(self):
        with self.store.connect() as db:
            if db.execute(
                "SELECT 1 FROM documents WHERE kind IN ('runs','workflow_runs') AND status IN ('running','queued') LIMIT 1"
            ).fetchone():
                raise HTTPException(
                    409, "Wait for queued and running work before backup or restore"
                )

    def backup(self, body):
        if body.passphrase and len(body.passphrase) < 12:
            raise ValueError("Use a backup passphrase of at least 12 characters")
        self.require_idle()
        if not self.lock.acquire(blocking=False):
            raise HTTPException(409, "A backup or cleanup is already running")
        id = uuid4().hex
        item = {
            "id": id,
            "name": body.name,
            "created_at": now(),
            "status": "running",
            "encrypted": bool(body.passphrase),
            "size": 0,
            "includes": (
                ["workspace", "local_objects", "packages", "warehouse"]
                if body.include_data
                else ["workspace"]
            ),
            "excludes": ["external_storage", "external_catalog", "server_environment"],
        }
        try:
            self.store.put("backups", item)
        except Exception:
            self.lock.release()
            raise

        def run():
            root = self.store.directory / "backups"
            temporary = root / f".{id}.tar.gz"
            final = root / (id + (".obbackup" if body.passphrase else ".tar.gz"))
            try:
                self.begin_maintenance("Creating a consistent workspace backup")
                self.app.state.scheduler.pause()
                with self._workspace_guard():
                    self.require_idle()
                    if body.include_data:
                        # Close the local Spark/Derby writer before copying its
                        # files. Remote servers' data is outside this backup.
                        self.app.state.runtime.reset()
                    build_archive(self.store, temporary, body.include_data)
                if body.passphrase:
                    crypt_file(temporary, final, body.passphrase)
                else:
                    temporary.replace(final)
                item.update(
                    status="succeeded",
                    size=final.stat().st_size,
                    _path=str(final),
                    finished_at=now(),
                )
            except Exception as exc:
                final.unlink(missing_ok=True)
                item.update(
                    status="failed",
                    error=f"Backup failed ({type(exc).__name__})",
                    finished_at=now(),
                )
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                    self.store.put("backups", item)
                finally:
                    try:
                        if not getattr(self.app.state, "restore_required", False):
                            self.app.state.maintenance = None
                            self.app.state.scheduler.resume()
                    finally:
                        self.lock.release()

        threading.Thread(target=run, daemon=True, name="openbricks-backup").start()
        return {key: value for key, value in item.items() if not key.startswith("_")}

    def restore(self, upload, passphrase):
        self.require_idle()
        if not self.lock.acquire(blocking=False):
            raise HTTPException(409, "A backup or cleanup is already running")
        root = self.store.directory
        rollback = root / "backups" / ("pre-restore-" + uuid4().hex)
        staging = root / "backups" / (".restore-" + uuid4().hex)
        moved = []
        services_stopped = False
        try:
            manifest = extract_archive(upload, staging, passphrase)
            self.begin_maintenance(
                "Restoring the workspace; the management service must restart"
            )
            self.app.state.scheduler.pause()
            with self._workspace_guard(include_workflows=False):
                self.require_idle()
                # From this point onward the app must restart on either success
                # or failure. Never resume scheduling against closed services.
                services_stopped = True
                self.app.state.restore_required = True
                self.app.state.workflows.close()
                self.app.state.runtime.reset()  # ClusterRuntime resets local only.
                self.app.state.runtime.close()
                if hasattr(self.app.state, "notifications"):
                    self.app.state.notifications.close()
                rollback.mkdir()
                directories = (
                    DATA_DIRS if manifest.get("include_data") else ("notebooks",)
                )
                names = ["openbricks.sqlite3", "secrets.key", *directories]
                with self.store.lock:
                    # Every Store connection is closed before the lock is
                    # released. Checkpoint before moving the main database;
                    # never discard a live WAL containing committed changes.
                    with closing(sqlite3.connect(root / "openbricks.sqlite3")) as db:
                        status = db.execute(
                            "PRAGMA wal_checkpoint(TRUNCATE)"
                        ).fetchone()
                        if status and status[0]:
                            raise ValueError(
                                "Workspace database is busy; restore cannot safely replace it"
                            )
                    for name in ("openbricks.sqlite3-wal", "openbricks.sqlite3-shm"):
                        (root / name).unlink(missing_ok=True)
                    for name in names:
                        source, target = staging / name, root / name
                        if target.exists():
                            target.replace(rollback / name)
                        moved.append(name)
                        if source.exists():
                            source.replace(target)
                        elif name in DATA_DIRS:
                            target.mkdir()
            self._restart_required(
                "Workspace restored. Restart management and local compute before using the workspace"
            )
            return {
                "ok": True,
                "restart_required": True,
                "message": "Workspace restored. Management and compute must reload before running notebooks.",
                "includes_data": manifest.get("include_data", False),
            }
        except Exception as original:
            rollback_error = False
            with self.store.lock:
                for name in reversed(moved):
                    try:
                        target = root / name
                        if target.exists():
                            target.replace(staging / (name + ".failed"))
                        previous = rollback / name
                        if previous.exists():
                            previous.replace(target)
                    except OSError:
                        rollback_error = True
            if services_stopped:
                message = (
                    "Restore failed and automatic rollback is incomplete. Keep the pre-restore backup and restart only after recovery"
                    if rollback_error
                    else "Restore failed. The previous workspace was recovered; restart management and local compute before using it"
                )
                self._restart_required(message, auto_restart=not rollback_error)
                raise HTTPException(503, message) from original
            else:
                self.app.state.maintenance = None
                self.app.state.scheduler.resume()
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            self.lock.release()


def register_operation_routes(app, authenticated, admin):
    def operations(request):
        return request.app.state.operations

    @app.get("/api/operations/overview")
    def overview(request: Request, user=Depends(admin)):
        from .compute import memory_metrics

        total, used = memory_metrics()
        disk = shutil.disk_usage(request.app.state.store.directory)
        runtime = request.app.state.runtime
        clusters = operations(request).live_metrics()
        return {
            "management": {
                "memory_used": used,
                "memory_limit": total,
                "cpu_percent": psutil.cpu_percent(),
            },
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
            "clusters": clusters,
            "counts": {
                "runs": request.app.state.store.count("runs"),
                "active_runs": runtime.active_count,
                "queued_runs": runtime.queued_count,
                "backups": request.app.state.store.count("backups"),
            },
            "retention": operations(request).policy(),
            "last_cleanup": request.app.state.store.get("settings", "last_cleanup"),
        }

    @app.get("/api/operations/retention")
    def retention(request: Request, user=Depends(admin)):
        return operations(request).policy()

    @app.put("/api/operations/retention")
    def update_retention(body: RetentionInput, request: Request, user=Depends(admin)):
        if body.run_days < body.output_days:
            raise ValueError(
                "Run-history retention must be at least as long as output retention"
            )
        service = operations(request)
        if not service.lock.acquire(blocking=False):
            raise HTTPException(409, "A backup or cleanup is already running")
        try:
            if getattr(request.app.state, "maintenance", None):
                raise HTTPException(
                    409, "Finish workspace maintenance before changing retention"
                )
            request.app.state.store.put(
                "settings", {"id": "retention", **body.model_dump()}
            )
        finally:
            service.lock.release()
        return body.model_dump()

    @app.get("/api/operations/retention/preview")
    def preview(request: Request, user=Depends(admin)):
        return {
            key: value
            for key, value in retention_preview(
                request.app.state.store, operations(request).policy()
            ).items()
            if not key.startswith("_")
        }

    @app.post("/api/operations/retention/run")
    def cleanup(request: Request, user=Depends(admin)):
        return operations(request).cleanup()

    @app.get("/api/backups")
    def backups(request: Request, user=Depends(admin)):
        return [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in request.app.state.store.list("backups", limit=100)
        ]

    @app.post("/api/backups", status_code=202)
    def backup(body: BackupInput, request: Request, user=Depends(admin)):
        return operations(request).backup(body)

    @app.get("/api/backups/{id}/download")
    def download(id: str, request: Request, user=Depends(admin)):
        item = request.app.state.store.get("backups", id)
        if not item or item.get("status") != "succeeded":
            raise HTTPException(404, "Completed backup not found")
        path = Path(item["_path"])
        root = request.app.state.store.directory / "backups"
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
            or not path.is_file()
        ):
            raise HTTPException(404, "Completed backup file not found")
        return FileResponse(
            path, filename=path.name, media_type="application/octet-stream"
        )

    @app.post("/api/backups/restore")
    async def restore(
        request: Request,
        file: UploadFile = File(...),
        passphrase: str = Form(""),
        user=Depends(admin),
    ):
        root = request.app.state.store.directory / "backups"
        path = root / (".upload-" + uuid4().hex)
        try:
            maximum = int(os.getenv("OPENBRICKS_MAX_UPLOAD_MB", "100")) * 1024**2
            total = 0
            with path.open("wb") as target:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > maximum:
                        raise HTTPException(
                            413, "Backup exceeds the configured upload limit"
                        )
                    target.write(chunk)
            return await run_in_threadpool(
                operations(request).restore, path, passphrase
            )
        finally:
            path.unlink(missing_ok=True)
