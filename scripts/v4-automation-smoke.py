"""Exercise automation through the UI proxy of a disposable v4 workspace.

Only http://127.0.0.1:3001 is accepted. This creates a new synthetic project and
never edits existing projects, resource policies, connections, or notifications.
Set OPENBRICKS_SMOKE_PASSWORD (or OPENBRICKS_ADMIN_PASSWORD). Receipts contain
synthetic IDs only and default to the ignored .test-data directory.

The default includes two real Delta/Spark commits, sequentially. Use --section
core for Python-only backfills/file/upstream/catch-up API checks, or --section
delta to run just the real Delta proof. No Docker commands are executed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

TERMINAL = {"succeeded", "failed", "cancelled", "skipped", "timed_out"}


class Proof:
    def __init__(self, args):
        parsed = urlsplit(args.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 3001
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Use only the disposable UI proxy at http://127.0.0.1:3001"
            )
        self.client = httpx.Client(
            base_url=args.base_url.rstrip("/"),
            timeout=httpx.Timeout(180, connect=10),
            follow_redirects=False,
        )
        self.suffix = uuid4().hex[:10]
        self.project, self.bucket = None, "v4-auto-" + self.suffix
        self.output = Path(args.receipt).resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.triggers = {}
        self.state = {
            "format": "openbricks-v4-automation-proof-v1",
            "synthetic_ids": {},
            "checks": [],
            "passed": False,
        }

    def remember(self, kind, identifier):
        values = self.state["synthetic_ids"].setdefault(kind, [])
        if identifier not in values:
            values.append(identifier)
        return identifier

    def request(self, method, path, *, expected=None, **kwargs):
        response = self.client.request(method, "/api" + path, **kwargs)
        assert (
            response.status_code == expected
            if expected is not None
            else response.is_success
        ), f"{method} {path}: unexpected HTTP {response.status_code}: {response.text[:1000]}"
        return response.json() if response.content else None

    def wait(self, get, predicate, *, timeout=180, label="automation"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = get()
            if predicate(result):
                return result
            time.sleep(0.5)
        raise AssertionError(f"Timed out waiting for synthetic {label}")

    def passed(self, name):
        self.state["checks"].append(name)
        self.checkpoint()
        print("PASS: " + name, flush=True)

    def checkpoint(self):
        temporary = self.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.replace(self.output)

    def login(self):
        assert self.client.get("/").status_code == 200
        password = os.getenv("OPENBRICKS_SMOKE_PASSWORD") or os.getenv(
            "OPENBRICKS_ADMIN_PASSWORD"
        )
        if not password:
            raise ValueError(
                "Set OPENBRICKS_SMOKE_PASSWORD for the disposable QA account"
            )
        self.request(
            "POST",
            "/auth/login",
            json={
                "username": os.getenv("OPENBRICKS_SMOKE_USERNAME", "admin"),
                "password": password,
            },
        )
        assert self.request("GET", "/auth/me")["role"] == "admin"
        project = self.request(
            "POST",
            "/projects",
            json={
                "name": "v4 Automation proof " + self.suffix,
                "description": "Disposable synthetic API checks",
                "cluster_ids": ["local"],
            },
        )
        self.project = self.remember("projects", project["id"])
        self.request(
            "POST", "/buckets", json={"name": self.bucket, "project_id": self.project}
        )
        self.remember("buckets", self.bucket)
        self.passed("UI proxy login and isolated synthetic project")

    def notebook(self, name, source, definitions=None):
        notebook = self.request(
            "POST",
            "/notebooks",
            json={
                "name": name + " " + self.suffix,
                "project_id": self.project,
                "cluster_id": "local",
                "connection_ids": [],
                "parameter_definitions": definitions or [],
                "cells": [{"id": "main", "language": "python", "source": source}],
            },
        )
        self.remember("notebooks", notebook["id"])
        return notebook

    def edit(self, notebook, source):
        latest = self.request("GET", "/notebooks/" + notebook["id"])
        return self.request(
            "PUT",
            "/notebooks/" + notebook["id"],
            json={
                **latest,
                "cells": [{"id": "main", "language": "python", "source": source}],
            },
        )

    def run(self, notebook):
        run = self.request(
            "POST",
            f"/notebooks/{notebook['id']}/run",
            json={"resource_options": {"mode": "job"}},
        )
        self.remember("runs", run["id"])
        result = self.wait(
            lambda: self.request("GET", "/runs/" + run["id"]),
            lambda value: value["status"] in TERMINAL,
            timeout=240,
            label="notebook run",
        )
        assert result["status"] == "succeeded", f"Synthetic run failed: {run['id']}"
        return result

    def workflow_run(self, identifier):
        self.remember("workflow_runs", identifier)
        result = self.wait(
            lambda: self.request("GET", "/workflow-runs/" + identifier),
            lambda value: value["status"] in TERMINAL,
            label="workflow run",
        )
        assert (
            result["status"] == "succeeded"
        ), f"Synthetic workflow failed: {identifier}"
        return result

    def child(self, workflow):
        assert len(workflow["tasks"]) == 1
        identifier = workflow["tasks"][0]["run_id"]
        self.remember("runs", identifier)
        result = self.request("GET", "/runs/" + identifier)
        assert result["status"] == "succeeded"
        return result

    @staticmethod
    def printed(run, prefix):
        text = "".join(output.get("stdout", "") for output in run.get("outputs", []))
        lines = [
            line[len(prefix) :] for line in text.splitlines() if line.startswith(prefix)
        ]
        assert len(lines) == 1, "Expected one synthetic result marker"
        return json.loads(lines[0])

    def backfills(self):
        notebook = self.notebook(
            "Date-range source",
            'import json\nprint("V4_BACKFILL " + json.dumps({"day": params["day"], "source": "captured"}))',
            [{"name": "day", "type": "date", "default": "2026-01-01"}],
        )
        payload = {
            "name": "Three dates " + self.suffix,
            "project_id": self.project,
            "notebook_id": notebook["id"],
            "date_parameter": "day",
            "start_date": "2026-01-02",
            "end_date": "2026-01-04",
            "max_concurrency": 1,
        }
        preview = self.request("POST", "/backfills/preview", json=payload)
        dates = ["2026-01-02", "2026-01-03", "2026-01-04"]
        assert preview["dates"] == dates
        created = self.request("POST", "/backfills", json=payload)
        self.remember("backfills", created["id"])
        self.edit(
            notebook,
            'import json\nprint("V4_BACKFILL " + json.dumps({"day": params["day"], "source": "edited"}))',
        )
        finished = self.wait(
            lambda: self.request("GET", "/backfills/" + created["id"]),
            lambda value: value["status"] in TERMINAL,
            label="three-date backfill",
        )
        assert finished["status"] == "succeeded" and finished["progress"] == {
            "completed": 3,
            "total": 3,
        }
        assert [item["date"] for item in finished["items"]] == dates
        children = []
        for item in finished["items"]:
            child = self.child(self.workflow_run(item["run_id"]))
            assert self.printed(child, "V4_BACKFILL ") == {
                "day": item["date"],
                "source": "captured",
            }
            children.append(child["id"])
        assert len(set(children)) == 3
        self.passed(
            "three date inputs use one captured source after a live source edit"
        )
        self.catchup(notebook)

    def catchup(self, notebook):
        payload = {
            "name": "Bounded catch-up " + self.suffix,
            "project_id": self.project,
            "notebook_id": notebook["id"],
            "cron": "0 0 1 1 *",
            "timezone": "America/New_York",
            "enabled": False,
            "catchup_policy": "all",
            "catchup_window_hours": 24,
            "catchup_max_runs": 2,
            "schedule_date_parameter": "day",
        }
        for bad in ({"catchup_window_hours": 169}, {"catchup_max_runs": 367}):
            self.request("POST", "/jobs", json={**payload, **bad}, expected=422)
        saved = self.request("POST", "/jobs", json=payload)
        self.remember("jobs", saved["id"])
        assert (
            saved["catchup_policy"] == "all"
            and saved["catchup_max_runs"] == 2
            and saved["enabled"] is False
        )
        cursors = self.request(
            "GET", "/schedules/catchup", params={"project_id": self.project, "limit": 1}
        )
        assert (
            cursors["total"] == 1
            and cursors["items"][0]["id"] == saved["id"]
            and cursors["items"][0]["policy"] == "all"
        )
        self.passed("catch-up API enforces bounds and exposes a paged durable cursor")

    def sink(self, label):
        return self.notebook(
            label,
            'import json\nprint("V4_EVENT " + json.dumps(params["event"], sort_keys=True))',
            [{"name": "event", "type": "json", "default": {}}],
        )

    def trigger(self, label, kind, source, notebook):
        payload = {
            "name": label + " " + self.suffix,
            "project_id": self.project,
            "notebook_id": notebook["id"],
            "type": kind,
            "source": source,
            "event_parameter": "event",
            "interval_seconds": 15,
            "debounce_seconds": 0,
            "include_existing": False,
            "enabled": True,
        }
        trigger = self.request("POST", "/triggers", json=payload)
        self.triggers[trigger["id"]] = payload
        self.remember("triggers", trigger["id"])
        self.request("POST", f"/triggers/{trigger['id']}/check")
        assert self.request("GET", f"/triggers/{trigger['id']}/events")["total"] == 0
        return trigger

    def event_result(self, trigger):
        identifier = trigger["id"]
        self.request("POST", f"/triggers/{identifier}/check")
        events = self.wait(
            lambda: self.request("GET", f"/triggers/{identifier}/events"),
            lambda value: value["total"] == 1
            and bool(value["items"][0].get("dispatch_id")),
            label="durable event admission",
        )
        dispatch_id = events["items"][0]["dispatch_id"]

        def dispatch():
            page = self.request(
                "GET",
                "/automation/runs",
                params={"project_id": self.project, "limit": 100},
            )
            return next(
                (item for item in page["items"] if item["id"] == dispatch_id), {}
            )

        admitted = self.wait(
            dispatch,
            lambda value: value.get("status") in TERMINAL,
            label="event pipeline",
        )
        assert admitted["status"] == "succeeded"
        self.remember("automation_dispatches", admitted["id"])
        result = self.printed(
            self.child(self.workflow_run(admitted["run_id"])), "V4_EVENT "
        )
        for _ in range(2):
            self.request("POST", f"/triggers/{identifier}/check")
        assert self.request("GET", f"/triggers/{identifier}/events")["total"] == 1
        page = self.request(
            "GET", "/automation/runs", params={"project_id": self.project, "limit": 100}
        )
        assert sum(item["source_id"] == identifier for item in page["items"]) == 1
        return result

    def files(self):
        trigger = self.trigger(
            "File arrival",
            "file_arrival",
            {"path": f"blob://{self.bucket}/incoming/", "pattern": "*.csv"},
            self.sink("File sink"),
        )
        self.request(
            "POST",
            f"/buckets/{self.bucket}/objects",
            data={"key": "incoming/arrival.csv"},
            files={"file": ("arrival.csv", b"id,value\n1,synthetic\n", "text/csv")},
        )
        event = self.event_result(trigger)
        assert event["type"] == "file_arrival" and event["event_count"] == 1
        assert event["objects"][0]["key"] == "incoming/arrival.csv"
        self.passed(
            "new local file creates one durable event and one pipeline; rechecks deduplicate"
        )

    def upstream(self):
        source = self.notebook(
            "Upstream source", "print('synthetic upstream completed')"
        )
        workflow = self.request(
            "POST",
            "/workflows",
            json={
                "name": "Upstream proof " + self.suffix,
                "project_id": self.project,
                "tasks": [
                    {
                        "key": "source",
                        "notebook_id": source["id"],
                        "cluster_id": "local",
                    }
                ],
            },
        )
        self.remember("workflows", workflow["id"])
        trigger = self.trigger(
            "Workflow completion",
            "workflow_completion",
            {"workflow_id": workflow["id"], "states": ["succeeded"]},
            self.sink("Upstream sink"),
        )
        started = self.request("POST", f"/workflows/{workflow['id']}/run", json={})
        self.workflow_run(started["id"])
        event = self.event_result(trigger)
        assert event["type"] == "workflow_completion" and event["event_count"] == 1
        assert event["upstream_runs"][0]["workflow_run_id"] == started["id"]
        self.passed(
            "upstream workflow completion produces one downstream event and run"
        )

    def delta(self):
        path = f"blob://{self.bucket}/delta/orders"

        def writer(mode):
            return f"path = blob.path({path!r})\nspark.range(1).write.format('delta').mode({mode!r}).save(str(path))\nprint('V4_DELTA_COMMIT')"

        notebook = self.notebook("Real Delta writer", writer("overwrite"))
        self.run(notebook)
        trigger = self.trigger(
            "Delta commit", "table_change", {"path": path}, self.sink("Delta sink")
        )
        self.edit(notebook, writer("append"))
        self.run(notebook)
        event = self.event_result(trigger)
        assert event["type"] == "table_change" and event["event_count"] == 1
        assert [item["version"] for item in event["objects"]] == [1]
        self.passed(
            "a real Delta append commit produces one metadata event without a polling Spark process"
        )

    def close(self):
        for identifier, payload in self.triggers.items():
            try:
                self.request(
                    "PUT", "/triggers/" + identifier, json={**payload, "enabled": False}
                )
            except Exception:
                print(
                    "Synthetic trigger could not be paused: " + identifier, flush=True
                )
        if not self.state["passed"]:
            for kind, route in (
                ("backfills", "backfills"),
                ("workflow_runs", "workflow-runs"),
                ("runs", "runs"),
            ):
                for identifier in self.state["synthetic_ids"].get(kind, []):
                    try:
                        self.request("POST", f"/{route}/{identifier}/cancel")
                    except Exception:
                        print(
                            "Synthetic work could not be cancelled: " + identifier,
                            flush=True,
                        )
        self.checkpoint()
        self.client.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base-url", default=os.getenv("OPENBRICKS_SMOKE_URL", "http://127.0.0.1:3001")
    )
    parser.add_argument(
        "--receipt",
        default=str(
            Path(__file__).resolve().parents[1] / ".test-data/v4-automation-smoke.json"
        ),
    )
    parser.add_argument("--section", choices=("all", "core", "delta"), default="all")
    args = parser.parse_args()
    proof = Proof(args)
    try:
        proof.login()
        if args.section in ("all", "core"):
            proof.backfills()
            proof.files()
            proof.upstream()
        if args.section in ("all", "delta"):
            proof.delta()
        proof.state["passed"] = True
        print(
            "PASS: automation proof complete; synthetic IDs saved to "
            + str(proof.output),
            flush=True,
        )
    finally:
        proof.close()


if __name__ == "__main__":
    main()
