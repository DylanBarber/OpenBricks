"""Boot a disposable restored workspace with all automation disabled."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from uuid import uuid4


def run_probe(archive, passphrase):
    # This module executes in a separate process. Never alter the live server's
    # environment or start restored schedules, notifications, or remote compute.
    external_key = os.getenv("OPENBRICKS_SECRET_KEY")
    for name in list(os.environ):
        if name.startswith("OPENBRICKS_"):
            os.environ.pop(name, None)
    os.environ["OPENBRICKS_RECOVERY_DRILL"] = "1"
    if external_key:
        os.environ["OPENBRICKS_SECRET_KEY"] = external_key
    from .operations import extract_archive
    from .store import Store
    from .security import password_hash

    with tempfile.TemporaryDirectory(
        prefix=".restore-drill-", dir=Path(archive).parent
    ) as temporary:
        root = Path(temporary) / "workspace"
        manifest = extract_archive(archive, root, passphrase)
        # Imports and subprocess helpers must also resolve their defaults inside
        # the disposable workspace, never the caller's data directory or home.
        home = Path(temporary) / "home"
        home.mkdir()
        os.environ["DATA_DIR"] = str(root)
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        store = Store(root)
        with store.connect() as db:
            db.execute(
                "UPDATE documents SET body=json_set(body,'$.enabled',json('false'))"
            )
            db.execute(
                "UPDATE documents SET status='cancelled',body=json_set(body,'$.status','cancelled') WHERE status IN ('queued','running','pending','waiting','admitting')"
            )
            db.execute("DELETE FROM documents WHERE kind IN ('sessions','api_tokens')")
        password = secrets.token_urlsafe(32)
        username = "recovery_" + uuid4().hex
        store.put(
            "users",
            {
                "id": uuid4().hex,
                "username": username,
                "password_hash": password_hash(password),
                "role": "admin",
            },
        )
        os.environ["OPENBRICKS_ADMIN_PASSWORD"] = password
        from .main import create_app
        from fastapi.testclient import TestClient

        with TestClient(create_app(root)) as client:
            assert client.get("/api/health").status_code == 200
            client.post(
                "/api/auth/login", json={"username": username, "password": password}
            ).raise_for_status()
            notebooks = client.get("/api/notebooks?limit=1000")
            notebooks.raise_for_status()
            projects = client.get("/api/projects")
            projects.raise_for_status()
        return {
            "passed": True,
            "management_boot": True,
            "authentication": True,
            "notebooks": len(notebooks.json()),
            "projects": len(projects.json()),
            "files_validated": len(manifest["files"]),
            "external_services_started": False,
        }


if __name__ == "__main__":
    try:
        body = json.load(sys.stdin)
        print(json.dumps(run_probe(body["archive"], body.get("passphrase", ""))))
    except Exception:
        print(
            json.dumps(
                {
                    "passed": False,
                    "error": "Restored workspace validation or isolated boot failed",
                }
            )
        )
        sys.exit(1)
