"""Bounded, durable failure/slow-run notifications with encrypted destinations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
import hashlib
import json
import smtplib
import ssl
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .credentials import SecretBox
from .permissions import DEFAULT_PROJECT, now


class DestinationInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str
    project_id: str = DEFAULT_PROJECT
    config: dict = Field(default_factory=dict)
    secrets: dict = Field(default_factory=dict)
    events: list[str] = Field(default_factory=lambda: ["failed"], max_length=3)
    enabled: bool = True


def public_destination(item):
    return {key: value for key, value in item.items() if not key.startswith("_")}


def email_address(value):
    if not isinstance(value, str) or "\r" in value or "\n" in value or "@" not in parseaddr(value)[1]:
        raise ValueError("Enter a valid email address")
    return value


def save_destination(store, body, previous=None):
    config = dict(body.config)
    allowed = ({"url", "endpoint", "allow_insecure", "slow_after_seconds"} if body.type == "webhook" else
               {"host", "port", "sender", "recipients", "username", "starttls", "ssl", "slow_after_seconds"})
    if set(config) - allowed:
        raise ValueError("Unexpected notification configuration fields; keep credentials in the secrets fields")
    box = SecretBox(store.directory)
    secrets = json.loads(box.decrypt(previous["_secrets"])) if previous else {}
    for key, value in body.secrets.items():
        if key not in ("token", "password", "url"):
            raise ValueError("Unexpected notification credential")
        if not isinstance(value, str):
            raise ValueError("Notification credentials must be strings")
        if value:
            secrets[key] = str(value)
    if body.type not in ("webhook", "email") or any(event not in ("failed", "slow", "succeeded") for event in body.events):
        raise ValueError("Choose a webhook/email destination and valid notification events")
    if previous and previous["type"] != body.type:
        raise ValueError("Create a new destination to change its type")
    if body.type == "webhook":
        url = config.pop("url", "") or secrets.get("url", "")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid = parsed and parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password and not parsed.fragment
            if parsed:
                parsed.port
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Enter an HTTP(S) webhook URL without user credentials or fragments")
        if "allow_insecure" in config and not isinstance(config["allow_insecure"], bool):
            raise ValueError("Allow HTTP must be true or false")
        if parsed.scheme == "http" and not config.get("allow_insecure", False):
            raise ValueError("Use HTTPS or explicitly allow HTTP for a trusted local endpoint")
        secrets["url"] = url
        config["endpoint"] = f"{parsed.scheme}://{parsed.netloc}"
    else:
        if not isinstance(config.get("host"), str) or not config["host"].strip():
            raise ValueError("SMTP host is required")
        try:
            config["port"] = int(config.get("port", 465 if config.get("ssl") is True else 587))
        except (TypeError, ValueError):
            raise ValueError("Choose a valid SMTP port") from None
        if not 1 <= config["port"] <= 65535:
            raise ValueError("Choose a valid SMTP port")
        config["sender"] = email_address(config.get("sender"))
        recipients = config.get("recipients", [])
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 20:
            raise ValueError("Choose between 1 and 20 email recipients")
        config["recipients"] = [email_address(value) for value in recipients]
        if config.get("username") is not None and not isinstance(config["username"], str):
            raise ValueError("SMTP username must be a string")
        if any(key in config and not isinstance(config[key], bool) for key in ("starttls", "ssl")):
            raise ValueError("SMTP TLS settings must be true or false")
        config.setdefault("starttls", True)
    try:
        config["slow_after_seconds"] = max(30, min(int(config.get("slow_after_seconds", 300)), 86400))
    except (TypeError, ValueError):
        raise ValueError("Slow-run threshold must be a number of seconds") from None
    item = {"id": previous["id"] if previous else uuid4().hex, "name": body.name.strip(), "type": body.type,
            "project_id": body.project_id, "config": config, "events": list(dict.fromkeys(body.events)), "enabled": body.enabled,
            "_secrets": box.encrypt(json.dumps(secrets)), "secret_fields": sorted(secrets), "created_at": previous.get("created_at", now()) if previous else now(), "updated_at": now()}
    if not item["name"]:
        raise ValueError("Destination name is required")
    return store.put("notification_targets", item)


def send_notification(store, destination, payload):
    secrets = json.loads(SecretBox(store.directory).decrypt(destination["_secrets"]))
    config = destination["config"]
    if destination["type"] == "webhook":
        headers = {"Content-Type": "application/json"}
        if payload.get("delivery_id"):
            headers["Idempotency-Key"] = payload["delivery_id"]
        if secrets.get("token"):
            headers["Authorization"] = "Bearer " + secrets["token"]
        response = httpx.post(secrets["url"], json=payload, headers=headers, timeout=8, follow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise ValueError(f"Webhook returned HTTP {response.status_code}")
    else:
        message = EmailMessage()
        message["From"], message["To"] = config["sender"], ", ".join(config["recipients"])
        message["Subject"] = f"OpenBricks: {payload['event']} — {payload.get('name', 'run')}"
        message.set_content(json.dumps(payload, indent=2))
        client_class = smtplib.SMTP_SSL if config.get("ssl", False) else smtplib.SMTP
        with client_class(config["host"], config.get("port", 587), timeout=8) as client:
            if not config.get("ssl", False) and config.get("starttls", True):
                client.starttls(context=ssl.create_default_context())
            if config.get("username"):
                client.login(config["username"], secrets.get("password", ""))
            client.send_message(message)


class NotificationManager:
    """A durable candidate queue and outbox, bounded independently per poll."""
    discovery_batch = 200
    delivery_batch = 10
    maximum_attempts = 3

    def __init__(self, store):
        self.store, self.stop_event = store, threading.Event()
        self.lock = threading.RLock()
        self.thread = threading.Thread(target=self._loop, name="openbricks-notifications", daemon=True)
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS notification_candidates (
                kind TEXT NOT NULL, run_id TEXT NOT NULL, ready_at TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(kind,run_id))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(notification_candidates)")}
            if "generation" not in columns:
                db.execute("ALTER TABLE notification_candidates ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS notification_candidate_due ON notification_candidates(ready_at,kind,run_id)")
            db.execute("CREATE INDEX IF NOT EXISTS notification_finished_runs ON documents(kind,finished_at,id)")
            # Status transitions are committed in the same transaction as the
            # run. Progress/log updates do not repeatedly enqueue that run.
            for operation, condition in (("INSERT", ""), ("UPDATE OF status", " AND OLD.status IS NOT NEW.status")):
                name = "notification_run_insert" if operation == "INSERT" else "notification_run_transition"
                db.execute(f"DROP TRIGGER IF EXISTS {name}")
                db.execute(f"""CREATE TRIGGER {name}
                    AFTER {operation} ON documents
                    WHEN NEW.kind IN ('runs','workflow_runs')
                      AND NEW.status IN ('running','failed','succeeded'){condition}
                    BEGIN
                      INSERT INTO notification_candidates(kind,run_id,ready_at,generation)
                      VALUES(NEW.kind,NEW.id,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),1)
                      ON CONFLICT(kind,run_id) DO UPDATE SET ready_at=excluded.ready_at,
                        generation=notification_candidates.generation+1;
                    END""")

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        # Maintenance must not restore SQLite while a delivery is about to
        # commit its result. The network operations have explicit timeouts.
        with self.lock:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=10)

    def _loop(self):
        while not self.stop_event.wait(5):
            try:
                self.poll_once()
            except Exception:
                # Delivery outages never stop notebook execution/scheduling.
                continue

    def _backfill(self):
        state = self.store.get("settings", "notification_backfill_v1") or {"cursor": 0}
        if state.get("complete"):
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with self.store.connect() as db:
            rows = db.execute("""SELECT rowid,kind,id FROM documents
                WHERE kind IN ('runs','workflow_runs') AND rowid>?
                  AND (status='running' OR finished_at>=?)
                ORDER BY rowid LIMIT ?""", (state["cursor"], cutoff, self.discovery_batch)).fetchall()
            for _, kind, identifier in rows:
                db.execute("INSERT OR IGNORE INTO notification_candidates(kind,run_id,ready_at) VALUES(?,?,?)",
                           (kind, identifier, now()))
        self.store.put("settings", {"id": "notification_backfill_v1",
            "cursor": rows[-1][0] if rows else state["cursor"], "complete": len(rows) < self.discovery_batch})

    def _metadata(self, kind, identifier):
        with self.store.connect() as db:
            row = db.execute("""SELECT project_id,job_id,status,name,notebook_name,created_at,finished_at,
                json_extract(body,'$.started_at'),json_extract(body,'$.workflow_id')
                FROM documents WHERE kind=? AND id=?""", (kind, identifier)).fetchone()
        if not row:
            return None
        keys = ("project_id", "job_id", "status", "name", "notebook_name", "created_at", "finished_at", "started_at", "workflow_id")
        return dict(zip(keys, row))

    def _enqueue(self, kind, identifier, source, destination, event):
        key = hashlib.sha256(f"{kind}:{identifier}:{destination['id']}:{event}".encode()).hexdigest()
        old = self.store.get("notification_deliveries", key)
        payload = {"delivery_id": key, "event": event, "run_id": identifier, "kind": kind,
                   "project_id": source.get("project_id") or DEFAULT_PROJECT, "status": source["status"],
                   "name": (source.get("name") or source.get("notebook_name") or "Notebook run").replace("\r", " ").replace("\n", " ")[:200],
                   "job_id": source.get("job_id"), "created_at": source.get("created_at"), "finished_at": source.get("finished_at")}
        if old:
            # Upgrade any pending delivery created by the earlier implementation.
            if "_payload" not in old:
                old["_payload"] = payload
                old.setdefault("next_attempt_at", now())
                self.store.put("notification_deliveries", old)
            return
        self.store.put("notification_deliveries", {"id": key, "project_id": payload["project_id"],
            "run_id": identifier, "target_id": destination["id"], "event": event, "status": "pending",
            "attempts": 0, "created_at": now(), "next_attempt_at": now(), "_payload": payload})

    def _discover(self):
        instant = datetime.now(timezone.utc)
        with self.store.connect() as db:
            candidates = db.execute("SELECT kind,run_id,generation FROM notification_candidates WHERE ready_at<=? ORDER BY ready_at,kind,run_id LIMIT ?",
                                    (instant.isoformat(), self.discovery_batch)).fetchall()
        for kind, identifier, generation in candidates:
            next_due = None
            try:
                source = self._metadata(kind, identifier)
                if source:
                    record = self.store.get("jobs", source["job_id"]) if source.get("job_id") else None
                    if kind == "workflow_runs" and not record and source.get("workflow_id"):
                        record = self.store.get("workflows", source["workflow_id"])
                    for target_id in (record or {}).get("notification_ids", [])[:20]:
                        destination = self.store.get("notification_targets", target_id)
                        project_id = source.get("project_id") or DEFAULT_PROJECT
                        if not destination or not destination.get("enabled") or destination.get("project_id", DEFAULT_PROJECT) != project_id:
                            continue
                        event = source["status"] if source["status"] in ("failed", "succeeded") else None
                        if source["status"] == "running" and "slow" in destination.get("events", []) and source.get("started_at"):
                            started = datetime.fromisoformat(source["started_at"])
                            if started.tzinfo is None:
                                started = started.replace(tzinfo=timezone.utc)
                            due = started + timedelta(seconds=destination["config"].get("slow_after_seconds", 300))
                            if instant < due:
                                next_due = min(next_due, due) if next_due else due
                                continue
                            event = "slow"
                        if event in destination.get("events", []):
                            self._enqueue(kind, identifier, source, destination, event)
            except Exception:
                # One malformed/temporarily unreadable record must not starve
                # other alerts. Revisit it without logging credential values.
                next_due = instant + timedelta(seconds=60)
            with self.store.connect() as db:
                if next_due:
                    db.execute("UPDATE notification_candidates SET ready_at=? WHERE kind=? AND run_id=? AND generation=?",
                               (next_due.isoformat(), kind, identifier, generation))
                else:
                    db.execute("DELETE FROM notification_candidates WHERE kind=? AND run_id=? AND generation=?", (kind, identifier, generation))

    def _deliver(self):
        with self.store.connect() as db:
            rows = db.execute("""SELECT body FROM documents WHERE kind='notification_deliveries'
                AND status IN ('pending','retrying','sending','failed')
                AND COALESCE(json_extract(body,'$.next_attempt_at'),'')<=?
                AND (COALESCE(json_extract(body,'$.attempts'),0)<? OR status='sending')
                ORDER BY created_at,id LIMIT ?""", (now(), self.maximum_attempts, self.delivery_batch)).fetchall()
        started = time.monotonic()
        for row in rows:
            if self.stop_event.is_set() or time.monotonic() - started > 20:
                break
            delivery = json.loads(row[0])
            if delivery.get("attempts", 0) >= self.maximum_attempts:
                delivery.update(status="failed", error="DeliveryOutcomeUnknown")
                self.store.put("notification_deliveries", delivery)
                continue
            destination = self.store.get("notification_targets", delivery["target_id"])
            if (not destination or not destination.get("enabled") or
                    destination.get("project_id", DEFAULT_PROJECT) != delivery.get("project_id", DEFAULT_PROJECT) or
                    delivery["event"] not in destination.get("events", [])):
                delivery.update(status="cancelled", error="DestinationUnavailable")
                self.store.put("notification_deliveries", delivery)
                continue
            payload = delivery.get("_payload")
            if not payload:
                # A legacy retry obtains a safe metadata-only payload.
                for kind in ("runs", "workflow_runs"):
                    source = self._metadata(kind, delivery["run_id"])
                    if source:
                        self._enqueue(kind, delivery["run_id"], source, destination, delivery["event"])
                        break
                payload = (self.store.get("notification_deliveries", delivery["id"]) or {}).get("_payload")
            if not payload:
                delivery.update(status="failed", error="SourceUnavailable", attempts=self.maximum_attempts)
                self.store.put("notification_deliveries", delivery)
                continue
            delivery["_payload"] = payload
            delivery.update(status="sending", attempts=delivery.get("attempts", 0) + 1,
                next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat())
            self.store.put("notification_deliveries", delivery)  # Persist intent before network I/O.
            try:
                send_notification(self.store, destination, payload)
                delivery.update(status="sent", sent_at=now(), error=None)
            except Exception as exc:
                delivery.update(status="failed" if delivery["attempts"] >= self.maximum_attempts else "retrying",
                    error=type(exc).__name__[:80],
                    next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=30 * delivery["attempts"])).isoformat())
            self.store.put("notification_deliveries", delivery)

    def poll_once(self):
        with self.lock:
            self._backfill()
            self._discover()
            self._deliver()


def register_notification_routes(app, authenticated, admin):
    @app.get("/api/notifications")
    def list_targets(request: Request, project_id: str | None = None, user=Depends(admin)):
        return [public_destination(item) for item in request.app.state.store.list("notification_targets", limit=200) if not project_id or item.get("project_id", DEFAULT_PROJECT) == project_id]

    @app.post("/api/notifications", status_code=201)
    def create_target(body: DestinationInput, request: Request, user=Depends(admin)):
        request.app.state.access.require_project(user, body.project_id, "manage")
        return public_destination(save_destination(request.app.state.store, body))

    @app.put("/api/notifications/{id}")
    def update_target(id: str, body: DestinationInput, request: Request, user=Depends(admin)):
        previous = request.app.state.store.get("notification_targets", id)
        if not previous:
            raise HTTPException(404, "Notification destination not found")
        request.app.state.access.require_project(user, body.project_id, "manage")
        return public_destination(save_destination(request.app.state.store, body, previous))

    @app.delete("/api/notifications/{id}")
    def delete_target(id: str, request: Request, user=Depends(admin)):
        request.app.state.store.delete("notification_targets", id)
        return {"ok": True}

    @app.post("/api/notifications/{id}/test")
    def test_target(id: str, request: Request, user=Depends(admin)):
        destination = request.app.state.store.get("notification_targets", id)
        if not destination:
            raise HTTPException(404, "Notification destination not found")
        try:
            send_notification(request.app.state.store, destination, {"delivery_id": uuid4().hex, "event": "test", "name": "Connection test", "status": "test"})
        except Exception as exc:
            raise HTTPException(502, f"Notification delivery failed ({type(exc).__name__}). Check the destination settings") from exc
        return {"ok": True}
