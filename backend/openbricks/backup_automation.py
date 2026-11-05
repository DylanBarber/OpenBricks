"""Durable encrypted off-host backups and explicit recovery operations."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4
from urllib.parse import urlsplit, unquote

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .backup_integrations import (
    export_integrations,
    recovery_plan,
    restore_integrations,
    s3_client,
    upload_absent,
)
from .blob import bucket_name
from .credentials import SecretBox
from .operations import BackupInput
from .permissions import Access, now

ACTIVE = {"queued", "waiting", "running", "uploading", "verifying"}


def public(value):
    if isinstance(value, dict):
        return {
            key: public(item) for key, item in value.items() if not key.startswith("_")
        }
    if isinstance(value, list):
        return [public(item) for item in value]
    return value


def prefix_value(value):
    value = str(value or "").strip("/")
    if (
        len(value) > 400
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or any(part in (".", "..") for part in value.split("/"))
    ):
        raise ValueError(
            "Use a storage prefix without control characters or traversal segments"
        )
    return value


class StorageSource(BaseModel):
    connection_id: str = Field(min_length=1, max_length=160)
    bucket: str = Field(min_length=1, max_length=63)
    prefix: str = Field(default="", max_length=400)


class BackupPolicyInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    project_id: str = "default"
    connection_id: str = Field(min_length=1, max_length=160)
    bucket: str = Field(min_length=1, max_length=63)
    prefix: str = Field(default="backups", max_length=400)
    cron: str = "0 2 * * *"
    timezone: str = "UTC"
    enabled: bool = True
    passphrase: str = Field(default="", max_length=1024)
    include_data: bool = True
    storage_sources: list[StorageSource] = Field(default_factory=list, max_length=20)
    include_catalog: bool = False
    verify_restore: bool = True
    notification_ids: list[str] = Field(default_factory=list, max_length=20)
    keep_remote: int = Field(default=5, ge=1, le=100)


class RemoteRestoreInput(StorageSource):
    project_id: str = "default"
    key: str = Field(min_length=1, max_length=1024)
    passphrase: str = Field(min_length=12, max_length=1024)


class RestoreRunInput(BaseModel):
    passphrase: str = Field(min_length=12, max_length=1024)


class RecoveryInput(BaseModel):
    project_id: str = "default"
    mappings: dict[str, str] = Field(default_factory=dict, max_length=100)
    include_catalog: bool = False
    plan_hash: str | None = None


def _trigger(policy):
    from .main import cron_trigger

    return cron_trigger(policy["cron"], policy["timezone"])


def _next(policy, instant):
    return (
        _trigger(policy)
        .get_next_fire_time(None, instant)
        .astimezone(timezone.utc)
        .isoformat()
    )


def validate_storage(access, user, project_id, identifier):
    record = access.require_connection(user, identifier, project_id, "manage")
    if record.get("type") != "s3":
        raise ValueError("Choose an S3-compatible storage connection")
    return record


def storage_identity(record):
    endpoint = str(record.get("config", {}).get("endpoint_url") or "").strip()
    if not endpoint:
        return ("aws-s3",)
    value = urlsplit(endpoint)
    hostname = (value.hostname or "").lower().rstrip(".")
    # AWS region and path/virtual-host style endpoints share the bucket namespace.
    if hostname == "s3.amazonaws.com" or (
        hostname.endswith(".amazonaws.com") and hostname.startswith("s3.")
    ):
        return ("aws-s3",)
    return (
        value.scheme.lower(),
        hostname,
        value.port or (443 if value.scheme.lower() == "https" else 80),
        unquote(value.path).rstrip("/"),
    )


def save_policy(store, access, user, body, previous=None):
    access.require_project(user, body.project_id, "manage")
    destination = validate_storage(access, user, body.project_id, body.connection_id)
    values = body.model_dump(exclude={"passphrase"})
    values.update(bucket=bucket_name(body.bucket), prefix=prefix_value(body.prefix))
    if not values["name"].strip():
        raise ValueError("Backup policy name is required")
    values["name"] = values["name"].strip()
    _trigger(values)
    source_prefixes = []
    for source in values["storage_sources"]:
        source_record = validate_storage(
            access, user, body.project_id, source["connection_id"]
        )
        source.update(
            bucket=bucket_name(source["bucket"]), prefix=prefix_value(source["prefix"])
        )
        identity = (storage_identity(source_record), source["bucket"])
        if any(
            identity == previous_identity
            and (
                source["prefix"].startswith(previous_prefix)
                or previous_prefix.startswith(source["prefix"])
            )
            for previous_identity, previous_prefix in source_prefixes
        ):
            raise ValueError("Selected storage source prefixes must not overlap")
        source_prefixes.append((identity, source["prefix"]))
        if (
            storage_identity(source_record) == storage_identity(destination)
            and source["bucket"] == values["bucket"]
            and (not source["prefix"] or values["prefix"].startswith(source["prefix"]))
        ):
            raise ValueError(
                "Backup destination must be outside every selected source prefix"
            )
    for identifier in values["notification_ids"]:
        target = store.get("notification_targets", identifier)
        if not target or target.get("project_id", "default") != body.project_id:
            raise ValueError(
                "Notification targets must belong to this policy's project"
            )
    if body.passphrase:
        if len(body.passphrase) < 12:
            raise ValueError(
                "Encrypted off-host backups require a passphrase of at least 12 characters"
            )
        encrypted = SecretBox(store.directory).encrypt(body.passphrase)
    elif previous:
        encrypted = previous["_passphrase"]
    else:
        raise ValueError(
            "Encrypted off-host backups require a passphrase of at least 12 characters"
        )
    current = datetime.now(timezone.utc)
    item = {
        **values,
        "id": previous["id"] if previous else uuid4().hex,
        "_passphrase": encrypted,
        "has_passphrase": True,
        "run_as_user_id": user["id"],
        "created_at": previous["created_at"] if previous else now(),
        "updated_at": now(),
        "next_fire_at": _next(values, current) if body.enabled else None,
    }
    return store.put("backup_policies", item)


class BackupCancelled(Exception):
    pass


class BackupAutomation:
    def __init__(self, app):
        self.app, self.store = app, app.state.store
        self.condition = threading.Condition(threading.RLock())
        self.serial = threading.Lock()
        self.external_restore_lock = threading.Lock()
        self.stop_event, self.paused = threading.Event(), threading.Event()
        self.active = False
        self.thread = threading.Thread(
            target=self._loop, name="openbricks-offsite-backups", daemon=True
        )
        with self.store.lock:
            identity = self.store.get("settings", "backup_instance")
            if not identity:
                identity = self.store.put(
                    "settings", {"id": "backup_instance", "instance_id": uuid4().hex}
                )
            self.instance_id = identity["instance_id"]
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='backup_runs' AND (status IN ('running','uploading','verifying') OR json_extract(body,'$.verification_status')='running')"
            ).fetchall()
        for row in rows:
            item = json.loads(row[0])
            if item["status"] not in ("running", "uploading", "verifying"):
                item.update(verification_status="queued", recovered_at=now())
                self.store.put("backup_runs", item)
                continue
            backup = self.store.get("backups", item.get("_backup_id", ""))
            if backup and backup.get("status") in ("running", "queued"):
                backup.update(
                    status="failed",
                    error="Management restarted before the local archive completed",
                    finished_at=now(),
                )
                self.store.put("backups", backup)
            if not backup or backup.get("status") != "succeeded":
                item.pop("_backup_id", None)
            item.update(status="queued", recovered_at=now())
            self.store.put("backup_runs", item)

    def start(self):
        if os.getenv("OPENBRICKS_RECOVERY_DRILL") != "1":
            self.thread.start()

    def pause(self, wait=True):
        self.paused.set()
        with self.condition:
            if wait and not self.condition.wait_for(
                lambda: not self.active, timeout=30
            ):
                raise HTTPException(
                    409, "Wait for the current backup transfer or restore drill to stop"
                )

    def resume(self):
        if not self.stop_event.is_set():
            self.paused.clear()

    def close(self):
        self.pause(wait=True)
        self.stop_event.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def check(self):
        if self.stop_event.is_set() or self.paused.is_set():
            raise BackupCancelled("Backup operation paused")

    def enqueue(self, policy, user=None, fire_at=None):
        with self.store.lock:
            identifier = (
                hashlib.sha256((policy["id"] + ":" + fire_at).encode()).hexdigest()
                if fire_at
                else uuid4().hex
            )
            old = self.store.get("backup_runs", identifier)
            if old:
                return old
            with self.store.connect() as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM documents WHERE kind='backup_runs' AND (status IN ('queued','waiting','running','uploading','verifying') OR json_extract(body,'$.verification_status') IN ('queued','running'))"
                ).fetchone()[0]
            if count >= 100:
                raise HTTPException(409, "The bounded backup queue is full")
            return self.store.put(
                "backup_runs",
                {
                    "id": identifier,
                    "policy_id": policy["id"],
                    "project_id": policy["project_id"],
                    "name": policy["name"],
                    "status": "queued",
                    "created_at": now(),
                    "fire_at": fire_at,
                    "actor_id": user["id"] if user else policy["run_as_user_id"],
                    "_policy": policy,
                    "attempts": 0,
                },
            )

    def _loop(self):
        while not self.stop_event.wait(1):
            try:
                self.poll_once()
            except Exception:
                continue

    def _schedule(self):
        instant = datetime.now(timezone.utc)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='backup_policies' AND json_extract(body,'$.enabled')=1 AND json_extract(body,'$.next_fire_at')<=? ORDER BY json_extract(body,'$.next_fire_at') LIMIT 20",
                (instant.isoformat(),),
            ).fetchall()
        for row in rows:
            policy = json.loads(row[0])
            fire = policy["next_fire_at"]
            with self.store.lock:
                try:
                    self.enqueue(
                        policy, fire_at=fire
                    )  # Durable intent precedes cursor movement.
                except HTTPException as exc:
                    if exc.status_code == 409:
                        # Leave the cursor due and drain existing work before admission.
                        return
                    raise
                latest = self.store.get("backup_policies", policy["id"])
                if latest and latest.get("next_fire_at") == fire:
                    # Missed backup times coalesce into one current snapshot.
                    latest.update(
                        next_fire_at=_next(policy, instant + timedelta(seconds=1)),
                        last_scheduled_at=fire,
                    )
                    self.store.put("backup_policies", latest)

    def poll_once(self):
        if (
            self.paused.is_set()
            or self.stop_event.is_set()
            or getattr(self.app.state, "maintenance", None)
            or not self.serial.acquire(blocking=False)
        ):
            return
        try:
            with self.condition:
                if self.paused.is_set() or self.stop_event.is_set():
                    return
                self.active = True
            self._schedule()
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT body FROM documents WHERE kind='backup_runs' AND (status IN ('queued','waiting') OR json_extract(body,'$.verification_status')='queued') AND COALESCE(json_extract(body,'$._next_attempt_at'),'')<=? ORDER BY created_at,id LIMIT 1",
                    (now(),),
                ).fetchone()
            if row:
                item = json.loads(row[0])
                if item.get("verification_status") == "queued":
                    self._verify_existing(item)
                else:
                    self._process(item)
        finally:
            with self.condition:
                self.active = False
                self.condition.notify_all()
            self.serial.release()

    def _policy_and_actor(self, item):
        policy = item["_policy"]
        actor = self.store.get("users", item["actor_id"])
        if not actor or actor.get("role") != "admin":
            raise ValueError(
                "The backup's administrator identity is no longer available"
            )
        access = Access(self.store)
        access.require_project(actor, policy["project_id"], "manage")
        destination = validate_storage(
            access, actor, policy["project_id"], policy["connection_id"]
        )
        for source in policy.get("storage_sources", []):
            source_record = validate_storage(
                access, actor, policy["project_id"], source["connection_id"]
            )
            if (
                storage_identity(source_record) == storage_identity(destination)
                and source["bucket"] == policy["bucket"]
                and (
                    not source.get("prefix")
                    or policy["prefix"].startswith(source["prefix"])
                )
            ):
                raise ValueError("Backup destination overlaps a selected source prefix")
        return policy, actor

    def _archive(self, identifier):
        record = self.store.get("backups", identifier)
        if not record or record.get("status") != "succeeded" or not record.get("_path"):
            return None
        path = Path(record["_path"])
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(
                (self.store.directory / "backups").resolve()
            )
        ):
            raise ValueError("Local backup archive is unavailable")
        return path

    def _process(self, item):
        protected = None
        try:
            self.check()
            policy, _ = self._policy_and_actor(item)
            phrase = SecretBox(self.store.directory).decrypt(policy["_passphrase"])
            path = self._archive(item.get("_backup_id", ""))
            if path is None:
                self.app.state.operations.require_idle()
                item.update(
                    status="running",
                    started_at=item.get("started_at") or now(),
                    attempts=item.get("attempts", 0) + 1,
                )
                self.store.put("backup_runs", item)
                local = self.app.state.operations.backup(
                    BackupInput(
                        name=policy["name"],
                        passphrase=phrase,
                        include_data=policy["include_data"],
                    ),
                    exporter=lambda attachments: export_integrations(
                        self.store, policy, attachments, self.check
                    ),
                )
                item["_backup_id"] = local["id"]
                self.store.put("backup_runs", item)
                while True:
                    self.check()
                    backup = self.store.get("backups", local["id"])
                    if backup and backup["status"] not in ("running", "queued"):
                        if backup["status"] != "succeeded":
                            raise ValueError("Local backup creation failed")
                        break
                    self.stop_event.wait(0.2)
                path = self._archive(local["id"])
            protected = self.store.get("backups", item["_backup_id"])
            protected["_offsite_uploading"] = True
            self.store.put("backups", protected)
            item.update(status="uploading", size=path.stat().st_size)
            self.store.put("backup_runs", item)
            item["remote"] = self._upload(item, path, policy)
            self.store.put("backup_runs", item)
            if policy["verify_restore"]:
                item.update(status="verifying", verification_status="running")
                self.store.put("backup_runs", item)
                item["restore_drill"] = self.drill(path, phrase)
                item["verification_status"] = "succeeded"
            item.update(status="succeeded", finished_at=now(), error=None)
            self.store.put("backup_runs", item)
            try:
                self._retain(item, policy)
            except BackupCancelled:
                raise
            except Exception as exc:
                item["retention_warning"] = (
                    f"Remote retention could not finish ({type(exc).__name__})"
                )
                self.store.put("backup_runs", item)
                self._notify(item, "failed")
            else:
                self._notify(item, "succeeded")
        except BackupCancelled:
            item.update(
                status="waiting",
                _next_attempt_at=(
                    datetime.now(timezone.utc) + timedelta(seconds=15)
                ).isoformat(),
            )
            self.store.put("backup_runs", item)
        except HTTPException as exc:
            if (
                exc.status_code == 409
                and item.get("status") in ("queued", "waiting", "running")
                and not item.get("_backup_id")
                and (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(item["created_at"])
                ).total_seconds()
                < 86400
            ):
                item.update(
                    status="waiting",
                    queued_reason="Waiting for an idle workspace",
                    _next_attempt_at=(
                        datetime.now(timezone.utc) + timedelta(seconds=15)
                    ).isoformat(),
                )
                self.store.put("backup_runs", item)
            else:
                self._fail(item, exc)
        except Exception as exc:
            self._fail(item, exc)
        finally:
            if protected:
                latest = self.store.get("backups", protected["id"])
                if latest:
                    latest.pop("_offsite_uploading", None)
                    self.store.put("backups", latest)

    def _fail(self, item, exc):
        item.update(
            status="failed",
            finished_at=now(),
            error=f"Backup operation failed ({type(exc).__name__})",
        )
        if item.get("verification_status") == "running":
            item["verification_status"] = "failed"
        self.store.put("backup_runs", item)
        self._notify(item, "failed")

    def _notify(self, item, event):
        manager = getattr(self.app.state, "notifications", None)
        if not manager:
            return
        for identifier in item.get("_policy", {}).get("notification_ids", []):
            destination = self.store.get("notification_targets", identifier)
            if (
                destination
                and destination.get("enabled")
                and destination.get("project_id", "default") == item["project_id"]
                and event in destination.get("events", [])
            ):
                with manager.lock:
                    manager._enqueue(
                        "backup_runs", item["id"], item, destination, event
                    )

    def _digest(self, path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.check()
                digest.update(chunk)
        return digest.hexdigest()

    def _upload(self, item, path, policy):
        digest, size = self._digest(path), path.stat().st_size
        key = "/".join(
            part
            for part in (
                policy["prefix"],
                "openbricks",
                self.instance_id,
                policy["id"],
                item["id"] + ".obbackup",
            )
            if part
        )
        client = s3_client(self.store, policy["project_id"], policy["connection_id"])
        try:
            try:
                existing = client.head_object(Bucket=policy["bucket"], Key=key)
            except Exception as exc:
                if str(
                    getattr(exc, "response", {}).get("Error", {}).get("Code")
                ) not in ("404", "NoSuchKey", "NotFound"):
                    raise
                existing = None
            metadata = {
                "openbricks-instance": self.instance_id,
                "openbricks-run": item["id"],
                "openbricks-sha256": digest,
            }
            if existing and (
                existing.get("Metadata") != metadata
                or int(existing.get("ContentLength", -1)) != size
            ):
                raise ValueError(
                    "A different object already occupies this backup's immutable storage key"
                )
            if not existing:
                upload_absent(
                    client,
                    path,
                    policy["bucket"],
                    key,
                    sha256=digest,
                    metadata=metadata,
                    check=self.check,
                )
            self.check()
            head = client.head_object(Bucket=policy["bucket"], Key=key)
            if (
                head.get("Metadata", {}).get("openbricks-sha256") != digest
                or int(head.get("ContentLength", -1)) != size
            ):
                raise ValueError(
                    "Remote backup size or checksum metadata could not be verified"
                )
            receipt = {
                "format": "openbricks-offsite-v1",
                "instance_id": self.instance_id,
                "policy_id": policy["id"],
                "run_id": item["id"],
                "connection_id": policy["connection_id"],
                "bucket": policy["bucket"],
                "key": key,
                "size": size,
                "sha256": digest,
                "encrypted": True,
                "created_at": item["created_at"],
                "verified": True,
            }
            content = json.dumps(receipt, sort_keys=True).encode()
            receipt_hash = hashlib.sha256(content).hexdigest()
            try:
                old_receipt = client.head_object(
                    Bucket=policy["bucket"], Key=key + ".receipt.json"
                )
            except Exception as exc:
                if str(
                    getattr(exc, "response", {}).get("Error", {}).get("Code")
                ) not in ("404", "NoSuchKey", "NotFound"):
                    raise
                old_receipt = None
            if old_receipt:
                if old_receipt.get("Metadata", {}).get(
                    "openbricks-sha256"
                ) != receipt_hash or int(old_receipt.get("ContentLength", -1)) != len(
                    content
                ):
                    raise ValueError(
                        "A different object occupies this backup's immutable receipt key"
                    )
            else:
                with tempfile.TemporaryDirectory(
                    prefix=".offsite-receipt-", dir=path.parent
                ) as temporary:
                    receipt_path = Path(temporary) / "receipt.json"
                    receipt_path.write_bytes(content)
                    upload_absent(
                        client,
                        receipt_path,
                        policy["bucket"],
                        key + ".receipt.json",
                        sha256=receipt_hash,
                        content_type="application/json",
                        metadata={
                            "openbricks-instance": self.instance_id,
                            "openbricks-run": item["id"],
                        },
                        check=self.check,
                    )
            self.check()
            receipt_head = client.head_object(
                Bucket=policy["bucket"], Key=key + ".receipt.json"
            )
            if receipt_head.get("Metadata", {}).get(
                "openbricks-sha256"
            ) != receipt_hash or int(receipt_head.get("ContentLength", -1)) != len(
                content
            ):
                raise ValueError("Remote receipt could not be verified")
            return receipt
        finally:
            client.close()

    def _retain(self, current, policy):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind='backup_runs' AND status='succeeded' AND json_extract(body,'$.policy_id')=? AND COALESCE(json_extract(body,'$.remote_expired'),0)=0 AND json_extract(body,'$.remote.verified')=1 ORDER BY created_at DESC,id DESC LIMIT 100 OFFSET ?",
                (policy["id"], policy["keep_remote"]),
            ).fetchall()
        client = None
        try:
            for row in rows:
                self.check()
                old = json.loads(row[0])
                receipt = old.get("remote", {})
                expected = "/".join(
                    part
                    for part in (
                        policy["prefix"],
                        "openbricks",
                        self.instance_id,
                        policy["id"],
                        old["id"] + ".obbackup",
                    )
                    if part
                )
                if (
                    old["id"] == current["id"]
                    or not receipt.get("verified")
                    or receipt.get("instance_id") != self.instance_id
                    or receipt.get("key") != expected
                    or receipt.get("bucket") != policy["bucket"]
                    or receipt.get("connection_id") != policy["connection_id"]
                    or old.get("remote_expired")
                ):
                    continue
                client = client or s3_client(
                    self.store, policy["project_id"], policy["connection_id"]
                )
                try:
                    head = client.head_object(
                        Bucket=receipt["bucket"], Key=receipt["key"]
                    )
                except Exception as exc:
                    if str(
                        getattr(exc, "response", {}).get("Error", {}).get("Code")
                    ) not in ("404", "NoSuchKey", "NotFound"):
                        raise
                    old.update(remote_expired=True, remote_expired_at=now())
                    self.store.put("backup_runs", old)
                    continue
                metadata = head.get("Metadata", {})
                if (
                    metadata.get("openbricks-instance") != self.instance_id
                    or metadata.get("openbricks-run") != old["id"]
                    or metadata.get("openbricks-sha256") != receipt["sha256"]
                ):
                    continue
                client.delete_object(Bucket=receipt["bucket"], Key=receipt["key"])
                client.delete_object(
                    Bucket=receipt["bucket"], Key=receipt["key"] + ".receipt.json"
                )
                old.update(remote_expired=True, remote_expired_at=now())
                self.store.put("backup_runs", old)
        finally:
            if client:
                client.close()

    def drill(self, path, phrase):
        self.check()
        process = subprocess.Popen(
            [sys.executable, "-m", "openbricks.recovery_drill"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=os.name != "nt",
        )
        body = json.dumps({"archive": str(path), "passphrase": phrase})
        deadline = time.monotonic() + 90
        try:
            while True:
                self.check()
                try:
                    output, _ = process.communicate(body, timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    body = None
                    if time.monotonic() >= deadline:
                        raise ValueError(
                            "Isolated restore drill exceeded its time limit"
                        )
            result = json.loads(output)
            if process.returncode or not result.get("passed"):
                raise ValueError("Isolated restore drill did not pass")
            return result
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    @contextmanager
    def downloaded(self, source, *, expected_sha=None, check=None):
        check = check or self.check
        client = s3_client(self.store, source["project_id"], source["connection_id"])
        directory = self.store.directory / "backups"
        try:
            head = client.head_object(Bucket=source["bucket"], Key=source["key"])
            size = int(head.get("ContentLength", 0))
            if (
                size <= 0
                or size > 20 * 1024**3
                or shutil.disk_usage(directory).free < size * 2 + 64 * 1024**2
            ):
                raise ValueError(
                    "Remote archive exceeds the restore allowance or available staging space"
                )
            checksum = expected_sha or head.get("Metadata", {}).get("openbricks-sha256")
            if checksum and not re.fullmatch("[a-f0-9]{64}", checksum):
                raise ValueError("Remote backup checksum metadata is invalid")
            with tempfile.TemporaryDirectory(
                prefix=".offsite-restore-", dir=directory
            ) as temporary:
                path = Path(temporary) / "workspace.obbackup"
                arguments = {"Bucket": source["bucket"], "Key": source["key"]}
                if head.get("ETag"):
                    arguments["IfMatch"] = head["ETag"]
                response = client.get_object(**arguments)
                stream, digest, received = response["Body"], hashlib.sha256(), 0
                try:
                    with path.open("wb") as destination:
                        while chunk := stream.read(1024 * 1024):
                            check()
                            received += len(chunk)
                            if received > size:
                                raise ValueError(
                                    "Remote archive changed while downloading"
                                )
                            digest.update(chunk)
                            destination.write(chunk)
                finally:
                    stream.close()
                if received != size or (checksum and digest.hexdigest() != checksum):
                    raise ValueError("Remote archive size or checksum mismatch")
                check()
                yield path
        finally:
            client.close()

    def _verify_existing(self, item):
        try:
            policy, _ = self._policy_and_actor(item)
            receipt = item.get("remote")
            if not receipt or item.get("remote_expired"):
                raise ValueError("The remote backup archive is unavailable")
            item.update(verification_status="running")
            self.store.put("backup_runs", item)
            phrase = SecretBox(self.store.directory).decrypt(policy["_passphrase"])
            with self.downloaded(
                {**receipt, "project_id": item["project_id"]},
                expected_sha=receipt["sha256"],
            ) as archive:
                item["restore_drill"] = self.drill(archive, phrase)
            item.update(verification_status="succeeded", verified_at=now())
        except BackupCancelled:
            item.update(verification_status="queued")
        except Exception as exc:
            item.update(
                verification_status="failed",
                verification_error=f"Restore drill failed ({type(exc).__name__})",
            )
            self._notify(item, "failed")
        self.store.put("backup_runs", item)

    def restore_remote(self, source, phrase):
        if not self.external_restore_lock.acquire(blocking=False):
            raise HTTPException(409, "Another remote recovery operation is in progress")
        operations = self.app.state.operations
        reserved = paused_manager = maintenance_owned = scheduler_paused = False
        previously_paused = self.paused.is_set()
        try:
            # Reserve before changing any state. An overlapping local backup
            # owns its maintenance flag and scheduler pause until it finishes.
            if not operations.lock.acquire(blocking=False):
                raise HTTPException(409, "A backup or cleanup is already running")
            reserved = True
            if getattr(self.app.state, "restore_required", False):
                raise HTTPException(
                    503, "The workspace must restart before another restore"
                )
            if getattr(self.app.state, "maintenance", None):
                raise HTTPException(409, "Workspace maintenance is already in progress")
            paused_manager = True
            self.pause(wait=True)
            operations.require_idle()
            operations.begin_maintenance(
                "Downloading and validating a remote workspace backup"
            )
            maintenance_owned = True
            self.app.state.scheduler.pause()
            scheduler_paused = True
            with self.downloaded(
                source, expected_sha=source.get("sha256"), check=self.check_service
            ) as path:
                self.check_service()
                return operations._restore_locked(path, phrase)
        finally:
            try:
                if not getattr(self.app.state, "restore_required", False):
                    if maintenance_owned:
                        self.app.state.maintenance = None
                    if paused_manager:
                        if previously_paused:
                            self.paused.set()
                        else:
                            self.resume()
                    if scheduler_paused:
                        self.app.state.scheduler.resume()
            finally:
                if reserved:
                    operations.lock.release()
                self.external_restore_lock.release()

    def check_service(self):
        if self.stop_event.is_set() or getattr(
            self.app.state, "restore_required", False
        ):
            raise BackupCancelled("The workspace is stopping for recovery")


def register_backup_automation_routes(app, authenticated, admin, access):
    def manager(request):
        return request.app.state.backup_automation

    def policy(request, identifier, user):
        item = request.app.state.store.get("backup_policies", identifier)
        if not item:
            raise HTTPException(404, "Backup policy not found")
        access.require_project(user, item["project_id"], "manage")
        return item

    def run(request, identifier, user):
        item = request.app.state.store.get("backup_runs", identifier)
        if not item:
            raise HTTPException(404, "Backup run not found")
        access.require_project(user, item["project_id"], "manage")
        return item

    @contextmanager
    def mutation(request):
        operations = request.app.state.operations
        if not operations.lock.acquire(blocking=False):
            raise HTTPException(409, "Workspace maintenance is in progress")
        try:
            if getattr(request.app.state, "restore_required", False):
                raise HTTPException(503, "The workspace must restart after recovery")
            if (
                getattr(request.app.state, "maintenance", None)
                or manager(request).paused.is_set()
            ):
                raise HTTPException(409, "Workspace maintenance is in progress")
            yield
        finally:
            operations.lock.release()

    @app.get("/api/operations/backup-policies")
    def policies(request: Request, project_id: str = "default", user=Depends(admin)):
        access.require_project(user, project_id, "manage")
        return public(
            request.app.state.store.page(
                "backup_policies", project_ids=[project_id], limit=200
            )["items"]
        )

    @app.post("/api/operations/backup-policies", status_code=201)
    def create(body: BackupPolicyInput, request: Request, user=Depends(admin)):
        with mutation(request):
            return public(save_policy(request.app.state.store, access, user, body))

    @app.put("/api/operations/backup-policies/{id}")
    def update(id: str, body: BackupPolicyInput, request: Request, user=Depends(admin)):
        with mutation(request):
            previous = policy(request, id, user)
            return public(
                save_policy(request.app.state.store, access, user, body, previous)
            )

    @app.delete("/api/operations/backup-policies/{id}")
    def delete(id: str, request: Request, user=Depends(admin)):
        with mutation(request):
            policy(request, id, user)
            with request.app.state.store.connect() as db:
                active = db.execute(
                    "SELECT 1 FROM documents WHERE kind='backup_runs' AND json_extract(body,'$.policy_id')=? AND (status IN ('queued','waiting','running','uploading','verifying') OR json_extract(body,'$.verification_status') IN ('queued','running')) LIMIT 1",
                    (id,),
                ).fetchone()
            if active:
                raise HTTPException(
                    409, "Wait for this policy's backup work before deleting it"
                )
            request.app.state.store.delete("backup_policies", id)
            return {"ok": True}

    @app.post("/api/operations/backup-policies/{id}/run", status_code=202)
    def start(id: str, request: Request, user=Depends(admin)):
        with mutation(request):
            return public(manager(request).enqueue(policy(request, id, user), user))

    @app.get("/api/operations/backup-runs")
    def runs(
        request: Request,
        project_id: str = "default",
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user=Depends(admin),
    ):
        access.require_project(user, project_id, "manage")
        return public(
            request.app.state.store.page(
                "backup_runs",
                project_ids=[project_id],
                limit=limit,
                offset=offset,
                omit=("_policy",),
            )
        )

    @app.get("/api/operations/backup-runs/{id}")
    def detail(id: str, request: Request, user=Depends(admin)):
        return public(run(request, id, user))

    @app.post("/api/operations/backup-runs/{id}/verify", status_code=202)
    def verify(id: str, request: Request, user=Depends(admin)):
        with mutation(request):
            with request.app.state.store.connect() as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM documents WHERE kind='backup_runs' AND (status IN ('queued','waiting','running','uploading','verifying') OR json_extract(body,'$.verification_status') IN ('queued','running'))"
                ).fetchone()[0]
            if count >= 100:
                raise HTTPException(409, "The bounded backup queue is full")
            item = run(request, id, user)
            if (
                item["status"] in ACTIVE
                or item.get("verification_status") in ("queued", "running")
                or not item.get("remote")
                or item.get("remote_expired")
            ):
                raise HTTPException(
                    409, "Choose a completed backup with an available remote archive"
                )
            item.update(verification_status="queued", _next_attempt_at=now())
            request.app.state.store.put("backup_runs", item)
            return public(item)

    @app.post("/api/operations/backup-runs/{id}/restore")
    def restore(id: str, body: RestoreRunInput, request: Request, user=Depends(admin)):
        item = run(request, id, user)
        receipt = item.get("remote")
        if item["status"] in ACTIVE or not receipt or item.get("remote_expired"):
            raise HTTPException(
                409, "Choose a completed backup with an available remote archive"
            )
        validate_storage(access, user, item["project_id"], receipt["connection_id"])
        return manager(request).restore_remote(
            {**receipt, "project_id": item["project_id"]}, body.passphrase
        )

    @app.post("/api/operations/remote-backups/restore")
    def restore_fresh(body: RemoteRestoreInput, request: Request, user=Depends(admin)):
        access.require_project(user, body.project_id, "manage")
        validate_storage(access, user, body.project_id, body.connection_id)
        if any(ord(value) < 32 for value in body.key):
            raise ValueError("Remote object key cannot contain control characters")
        return manager(request).restore_remote(
            {
                **body.model_dump(exclude={"passphrase", "prefix"}),
                "bucket": bucket_name(body.bucket),
            },
            body.passphrase,
        )

    @app.post("/api/operations/recovery/plan")
    def plan(body: RecoveryInput, request: Request, user=Depends(admin)):
        access.require_project(user, body.project_id, "manage")
        for identifier in body.mappings.values():
            validate_storage(access, user, body.project_id, identifier)
        return recovery_plan(
            request.app.state.store, body.mappings, body.include_catalog
        )

    @app.post("/api/operations/recovery/apply")
    def apply(body: RecoveryInput, request: Request, user=Depends(admin)):
        access.require_project(user, body.project_id, "manage")
        for identifier in body.mappings.values():
            validate_storage(access, user, body.project_id, identifier)
        operations = request.app.state.operations
        operations.require_idle()
        if not operations.lock.acquire(blocking=False):
            raise HTTPException(
                409, "Another workspace maintenance operation is in progress"
            )
        try:
            manager(request).pause(wait=True)
            operations.begin_maintenance(
                "Recovering selected storage and catalog metadata"
            )
            request.app.state.scheduler.pause()
            with operations._workspace_guard():
                operations.require_idle()
                return restore_integrations(
                    request.app.state.store,
                    body.project_id,
                    body.mappings,
                    body.plan_hash,
                    body.include_catalog,
                    check=manager(request).check_service,
                )
        finally:
            if not getattr(request.app.state, "restore_required", False):
                request.app.state.maintenance = None
                request.app.state.scheduler.resume()
                manager(request).resume()
            operations.lock.release()
