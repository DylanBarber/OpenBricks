from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openbricks import notifications
from openbricks.credentials import SecretBox
from openbricks.main import create_app
from openbricks.notifications import DestinationInput, NotificationManager, public_destination, save_destination, send_notification
from openbricks.store import Store


def target(store, events=None):
    return save_destination(store, DestinationInput(name="Alerts", type="webhook", project_id="default",
        config={"url": "https://alerts.example/private/TOPSECRET?key=QUERYSECRET"}, secrets={"token": "TOKENSECRET"}, events=events or ["failed"]))


def run(store, identifier="failure", status="failed", **extra):
    instant = datetime.now(timezone.utc)
    return store.put("runs", {"id": identifier, "project_id": "default", "job_id": "job", "notebook_name": "Demo",
        "status": status, "created_at": (instant - timedelta(minutes=10)).isoformat(), "started_at": (instant - timedelta(minutes=8)).isoformat(),
        "finished_at": instant.isoformat() if status != "running" else None, **extra})


def setup(tmp_path):
    store = Store(tmp_path)
    destination = target(store)
    store.put("jobs", {"id": "job", "project_id": "default", "notification_ids": [destination["id"]]})
    return store, destination


def test_encrypted_destinations_blank_secret_preservation_and_safe_validation(tmp_path):
    store, destination = setup(tmp_path)
    public = json.dumps(public_destination(destination))
    assert all(secret not in public for secret in ("TOPSECRET", "QUERYSECRET", "TOKENSECRET"))
    assert destination["config"]["endpoint"] == "https://alerts.example"
    secrets = json.loads(SecretBox(store.directory).decrypt(destination["_secrets"]))
    assert secrets["token"] == "TOKENSECRET" and "TOPSECRET" in secrets["url"]
    updated = save_destination(store, DestinationInput(name="Renamed", type="webhook", config=destination["config"], secrets={"token": ""}), destination)
    assert json.loads(SecretBox(store.directory).decrypt(updated["_secrets"])) == secrets
    for config in ({"url": "https://host:SECRET-PORT/private"}, {"url": "https://ok.example", "slow_after_seconds": "SECRET-THRESHOLD"}):
        with pytest.raises(ValueError) as error:
            save_destination(store, DestinationInput(name="Invalid", type="webhook", config=config))
        assert "SECRET" not in str(error.value)
    with pytest.raises(ValueError) as error:
        save_destination(store, DestinationInput(name="Invalid", type="email", config={"host": "smtp.example", "port": "SECRET-PORT"}))
    assert "SECRET" not in str(error.value)


def test_webhook_and_smtp_delivery_mocks_only(tmp_path, monkeypatch):
    store, destination = setup(tmp_path)
    requests = []
    def post(url, **kwargs):
        requests.append((url, kwargs))
        return SimpleNamespace(status_code=202)
    monkeypatch.setattr(notifications.httpx, "post", post)
    send_notification(store, destination, {"delivery_id": "stable-delivery", "event": "failed", "name": "Job"})
    assert requests[0][1]["headers"]["Idempotency-Key"] == "stable-delivery"
    assert requests[0][1]["headers"]["Authorization"] == "Bearer TOKENSECRET"
    assert not requests[0][1]["follow_redirects"]
    smtp = []
    class SMTP:
        def __init__(self, *args, **kwargs):
            smtp.append((args, kwargs))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def starttls(self, **kwargs):
            smtp.append("tls")
        def login(self, *args):
            smtp.append(args)
        def send_message(self, message):
            smtp.append(message)
    monkeypatch.setattr(notifications.smtplib, "SMTP", SMTP)
    email = save_destination(store, DestinationInput(name="Email", type="email", config={"host": "smtp.example", "sender": "from@example.invalid",
        "recipients": ["to@example.invalid"], "username": "mailer"}, secrets={"password": "SMTPSECRET"}))
    send_notification(store, email, {"delivery_id": "email-delivery", "event": "failed", "name": "Job"})
    assert "tls" in smtp and ("mailer", "SMTPSECRET") in smtp
    assert "SMTPSECRET" not in smtp[-1].as_string()


def test_old_failure_not_starved_by_more_than_200_newer_runs(tmp_path, monkeypatch):
    store, _ = setup(tmp_path)
    run(store, "older-failure")
    for index in range(225):
        run(store, f"newer-{index:03}", status="succeeded", job_id=None, created_at=datetime.now(timezone.utc).isoformat())
    sent = []
    monkeypatch.setattr(notifications, "send_notification", lambda store, destination, payload: sent.append(payload))
    manager = NotificationManager(store)
    for _ in range(5):
        manager.poll_once()
    assert len(sent) == 1 and sent[0]["run_id"] == "older-failure"
    assert store.list("notification_deliveries")[0]["status"] == "sent"


def test_retry_outbox_persists_before_send_is_bounded_and_sanitized(tmp_path, monkeypatch):
    store, _ = setup(tmp_path)
    manager = NotificationManager(store)
    run(store)
    attempts = []
    def fail(store, destination, payload):
        durable = store.get("notification_deliveries", payload["delivery_id"])
        assert durable["status"] == "sending" and durable["attempts"] == len(attempts) + 1
        attempts.append(payload["delivery_id"])
        raise RuntimeError("TOPSECRET QUERYSECRET TOKENSECRET")
    monkeypatch.setattr(notifications, "send_notification", fail)
    for _ in range(4):
        for delivery in store.list("notification_deliveries"):
            delivery["next_attempt_at"] = "2000-01-01T00:00:00+00:00"
            store.put("notification_deliveries", delivery)
        manager.poll_once()
    assert len(attempts) == 3 and len(set(attempts)) == 1
    delivery = store.list("notification_deliveries")[0]
    assert delivery["status"] == "failed" and delivery["error"] == "RuntimeError"
    assert all(secret not in json.dumps(delivery) for secret in ("TOPSECRET", "QUERYSECRET", "TOKENSECRET"))


def test_restart_after_unacknowledged_delivery_reuses_id(tmp_path, monkeypatch):
    store, _ = setup(tmp_path)
    manager = NotificationManager(store)
    run(store)
    observed = []
    def interrupted(store, destination, payload):
        observed.append(payload["delivery_id"])
        raise SystemExit("simulate process termination after receiver acceptance")
    monkeypatch.setattr(notifications, "send_notification", interrupted)
    with pytest.raises(SystemExit):
        manager.poll_once()
    delivery = store.list("notification_deliveries")[0]
    assert delivery["status"] == "sending"
    delivery["next_attempt_at"] = "2000-01-01T00:00:00+00:00"
    store.put("notification_deliveries", delivery)
    monkeypatch.setattr(notifications, "send_notification", lambda store, destination, payload: observed.append(payload["delivery_id"]))
    NotificationManager(store).poll_once()
    assert len(observed) == 2 and len(set(observed)) == 1
    assert store.list("notification_deliveries")[0]["status"] == "sent"


def test_slow_and_failure_are_distinct_once_only_events(tmp_path, monkeypatch):
    store, destination = setup(tmp_path)
    destination["events"] = ["slow", "failed"]
    store.put("notification_targets", destination)
    manager = NotificationManager(store)
    running = run(store, status="running")
    events = []
    monkeypatch.setattr(notifications, "send_notification", lambda store, destination, payload: events.append(payload["event"]))
    manager.poll_once()
    manager.poll_once()
    store.put("runs", {**running, "status": "failed", "finished_at": datetime.now(timezone.utc).isoformat()})
    manager.poll_once()
    assert events == ["slow", "failed"]


def test_completion_during_discovery_preserves_new_candidate_generation(tmp_path, monkeypatch):
    store, _ = setup(tmp_path)
    manager = NotificationManager(store)
    running = run(store, status="running")
    original = manager._metadata
    changed = []
    def finish_during_read(kind, identifier):
        source = original(kind, identifier)
        if not changed:
            changed.append(True)
            store.put("runs", {**running, "status": "failed", "finished_at": datetime.now(timezone.utc).isoformat()})
        return source
    monkeypatch.setattr(manager, "_metadata", finish_during_read)
    events = []
    monkeypatch.setattr(notifications, "send_notification", lambda store, destination, payload: events.append(payload["event"]))
    manager.poll_once()
    assert not events
    manager.poll_once()
    assert events == ["failed"]


def test_notification_api_requires_admin_and_never_returns_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENBRICKS_COMPUTE_URL", raising=False)
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "notification-admin-password-123")
    tested = []
    monkeypatch.setattr(notifications, "send_notification", lambda store, destination, payload: tested.append(payload))
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/notifications").status_code == 401
        assert client.post("/api/auth/login", json={"username": "admin", "password": "notification-admin-password-123"}).status_code == 200
        created = client.post("/api/notifications", json={"name": "Alerts", "type": "webhook", "project_id": "default",
            "config": {"url": "https://alerts.example/TOPSECRET"}, "secrets": {"token": "TOKENSECRET"}})
        assert created.status_code == 201, created.text
        identifier = created.json()["id"]
        assert "TOPSECRET" not in created.text and "TOKENSECRET" not in client.get("/api/notifications").text
        assert client.post(f"/api/notifications/{identifier}/test").status_code == 200
        assert tested[0]["delivery_id"]
        assert client.post("/api/users", json={"username": "member", "password": "member-password-123", "role": "member"}).status_code == 201
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"username": "member", "password": "member-password-123"})
        assert client.get("/api/notifications").status_code == 403
        assert client.post("/api/notifications", json={"name": "Denied", "type": "webhook"}).status_code == 403
        assert client.put(f"/api/notifications/{identifier}", json={"name": "Denied", "type": "webhook"}).status_code == 403
        assert client.delete(f"/api/notifications/{identifier}").status_code == 403
        assert client.post(f"/api/notifications/{identifier}/test").status_code == 403
