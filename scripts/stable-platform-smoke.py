"""Rigorous live platform proof against a disposable OpenBricks installation.

Only http://127.0.0.1:3001 is accepted; no Docker commands are executed. Every
section creates synthetic fixtures in a dedicated project. Use --receipt to
resume the scheduling proof after restarting management and compute.

Sections: auth, workflows, resources, remote, catalog, notifications, history,
schedule-prepare, schedule-verify, compute-restart-prepare,
compute-restart-verify, or core (auth + workflows + history).
Spark-heavy remote/catalog sections must run sequentially with SQL tests on
small hosts. Wheel installation requires all compute work to be idle.

Required: OPENBRICKS_SMOKE_PASSWORD. Remote needs OPENBRICKS_SMOKE_REMOTE_TOKEN
and OPENBRICKS_SMOKE_REMOTE_URL (default http://remote:8001). Storage/catalog
need OPENBRICKS_SMOKE_S3_ACCESS_KEY/SECRET_KEY and a Spark-configured S3 endpoint.
The local non-relaying receiver is scripts/stable-notification-receiver.py.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "skipped"}


class Proof:
    def __init__(self, args):
        url = urlsplit(args.base_url)
        if (
            (url.scheme, url.hostname, url.port, url.path)
            not in {("http", "127.0.0.1", 3001, ""), ("http", "127.0.0.1", 3001, "/")}
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Use only the disposable UI proxy http://127.0.0.1:3001")
        self.args, self.url = args, args.base_url.rstrip("/")
        self.output = Path(args.receipt).resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.state = (
            json.loads(self.output.read_text())
            if self.output.exists()
            else {
                "format": "openbricks-stable-platform-v1",
                "suffix": uuid4().hex[:10],
                "checks": [],
                "runs": [],
                "workflow_runs": [],
                "jobs": [],
            }
        )
        assert self.state.get("format") == "openbricks-stable-platform-v1"
        self.suffix = self.state["suffix"]
        self.secret_values = []
        self.client = self.new_client()
        self.failed = False

    def new_client(self):
        return httpx.Client(
            base_url=self.url,
            timeout=httpx.Timeout(240, connect=10),
            follow_redirects=False,
        )

    def safe(self, value):
        text = str(value)
        for secret in self.secret_values:
            text = text.replace(secret, "[redacted]")
        return text[:2000]

    def api(self, method, path, *, client=None, expected=None, **kwargs):
        response = (client or self.client).request(method, "/api" + path, **kwargs)
        if expected is not None:
            valid = (
                response.status_code in expected
                if isinstance(expected, tuple)
                else response.status_code == expected
            )
        else:
            valid = response.is_success
        assert valid, self.safe(
            f"{method} {path}: HTTP {response.status_code}: {response.text}"
        )
        return response.json() if response.content else None

    def wait(self, get, predicate, *, timeout=180, label="operation"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = get()
            if predicate(last):
                return last
            time.sleep(0.4)
        raise AssertionError(self.safe(f"Timed out waiting for {label}; last={last}"))

    def checkpoint(self):
        temporary = self.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.replace(self.output)

    def passed(self, name):
        self.state["checks"].append(name)
        self.checkpoint()
        print("PASS: " + name, flush=True)

    def setup(self):
        password = os.getenv("OPENBRICKS_SMOKE_PASSWORD") or os.getenv(
            "OPENBRICKS_ADMIN_PASSWORD"
        )
        if not password:
            raise ValueError(
                "Set OPENBRICKS_SMOKE_PASSWORD for the disposable workspace"
            )
        self.secret_values.append(password)
        self.api(
            "POST",
            "/auth/login",
            json={
                "username": os.getenv("OPENBRICKS_SMOKE_USERNAME", "admin"),
                "password": password,
            },
        )
        assert self.api("GET", "/auth/me")["role"] == "admin"
        if not self.state.get("project_id"):
            project = self.api(
                "POST",
                "/projects",
                json={
                    "name": "Stable platform " + self.suffix,
                    "description": "OpenBricks stable smoke fixture",
                    "cluster_ids": ["local"],
                },
            )
            self.state["project_id"] = project["id"]
            self.state["bucket"] = "stable-platform-" + self.suffix
            self.api(
                "POST",
                "/buckets",
                json={"name": self.state["bucket"], "project_id": project["id"]},
            )
            self.checkpoint()
        project = next(
            item
            for item in self.api("GET", "/projects")
            if item["id"] == self.state["project_id"]
        )
        assert project.get("description") == "OpenBricks stable smoke fixture"
        self.project, self.bucket = project["id"], self.state["bucket"]

    def notebook(
        self,
        name,
        source=None,
        *,
        cells=None,
        cluster="local",
        connections=None,
        definitions=None,
    ):
        value = self.api(
            "POST",
            "/notebooks",
            json={
                "name": name + " " + self.suffix,
                "project_id": self.project,
                "cluster_id": cluster,
                "connection_ids": connections or [],
                "parameter_definitions": definitions or [],
                "cells": cells
                or [{"id": "main", "language": "python", "source": source}],
            },
        )
        self.state.setdefault("notebooks", []).append(value["id"])
        self.checkpoint()
        return value

    def submit(self, notebook, **options):
        value = self.api(
            "POST",
            f"/notebooks/{notebook['id']}/run",
            json={"resource_options": {"mode": "job", "memory_mb": 512}, **options},
        )
        self.state["runs"].append(value["id"])
        self.checkpoint()
        return value

    def await_run(self, identifier, *, status="succeeded", timeout=180):
        value = self.wait(
            lambda: self.api("GET", "/runs/" + identifier),
            lambda item: item["status"] in TERMINAL,
            timeout=timeout,
            label="notebook run",
        )
        assert value["status"] == status, self.safe(value.get("error") or value)
        return value

    def run(self, notebook, *, status="succeeded", **options):
        return self.await_run(self.submit(notebook, **options)["id"], status=status)

    def workflow(self, name, tasks, **values):
        return self.api(
            "POST",
            "/workflows",
            json={
                "name": name + " " + self.suffix,
                "project_id": self.project,
                "tasks": tasks,
                **values,
            },
        )

    def start_workflow(self, workflow, **values):
        value = self.api("POST", f"/workflows/{workflow['id']}/run", json=values)
        self.state["workflow_runs"].append(value["id"])
        self.checkpoint()
        return value

    def await_workflow(self, identifier, status="succeeded", timeout=180):
        result = self.wait(
            lambda: self.api("GET", "/workflow-runs/" + identifier),
            lambda item: item["status"] in TERMINAL,
            timeout=timeout,
            label="workflow",
        )
        assert result["status"] == status, self.safe(result)
        return result

    @staticmethod
    def stdout(run):
        return "\n".join(cell.get("stdout", "") for cell in run.get("outputs", []))

    def auth(self):
        public = self.new_client()
        try:
            self.api("GET", "/notebooks", client=public, expected=401)
            self.api(
                "GET",
                "/projects",
                expected=401,
                headers={"Authorization": "Bearer invalid"},
            )
            self.api(
                "POST",
                "/notebooks",
                json={"name": "cross-origin"},
                headers={"Origin": "https://untrusted.example.invalid"},
                expected=403,
            )
            private = self.api(
                "POST", "/projects", json={"name": "Stable private " + self.suffix}
            )
            fixture = self.notebook("Role fixture", "print('role check')")
            foreign = self.api(
                "POST",
                "/notebooks",
                json={
                    "name": "Unassigned private notebook",
                    "project_id": private["id"],
                    "cells": [
                        {
                            "id": "main",
                            "language": "python",
                            "source": "print('private')",
                        }
                    ],
                },
            )
            for role in ("viewer", "operator", "editor"):
                password = secrets.token_urlsafe(24)
                self.secret_values.append(password)
                username = f"stable-{role}-{uuid4().hex[:8]}"
                account = self.api(
                    "POST",
                    "/users",
                    json={"username": username, "password": password, "role": "member"},
                )
                self.api(
                    "PUT",
                    f"/projects/{self.project}/members/{account['id']}",
                    json={"role": role},
                )
                with self.new_client() as actor:
                    self.api(
                        "POST",
                        "/auth/login",
                        client=actor,
                        json={"username": username, "password": password},
                    )
                    self.api("GET", f"/notebooks/{fixture['id']}", client=actor)
                    self.api(
                        "GET",
                        f"/notebooks/{foreign['id']}",
                        client=actor,
                        expected=(403, 404),
                    )
                    self.api("GET", "/users", client=actor, expected=403)
                    self.api(
                        "PUT",
                        f"/projects/{self.project}/members/{account['id']}",
                        client=actor,
                        json={"role": "owner"},
                        expected=403,
                    )
                    edited = self.api(
                        "PUT",
                        f"/notebooks/{fixture['id']}",
                        client=actor,
                        json={**fixture, "description": "Role edit"},
                        expected=200 if role == "editor" else 403,
                    )
                    started = self.api(
                        "POST",
                        f"/notebooks/{fixture['id']}/run",
                        client=actor,
                        json={"resource_options": {"mode": "job", "memory_mb": 512}},
                        expected=403 if role == "viewer" else 202,
                    )
                    if role != "viewer":
                        self.state["runs"].append(started["id"])
                        result = self.await_run(started["id"])
                        assert result["resource_options"]["mode"] == "job"
            self.passed(
                "anonymous/CSRF/bad-bearer rejection and real viewer/operator/editor project boundaries"
            )
            account = self.api(
                "POST",
                "/service-accounts",
                json={
                    "name": "stable-bot-" + self.suffix,
                    "grants": [{"project_id": self.project, "role": "editor"}],
                },
            )
            for days in (0, 366):
                self.api(
                    "POST",
                    f"/service-accounts/{account['id']}/tokens",
                    json={
                        "name": "invalid expiry",
                        "project_ids": [self.project],
                        "actions": ["read"],
                        "expires_in_days": days,
                    },
                    expected=422,
                )
            self.api(
                "PUT",
                f"/projects/{self.project}/members/{account['id']}",
                json={"role": "owner"},
                expected=400,
            )
            token = self.api(
                "POST",
                f"/service-accounts/{account['id']}/tokens",
                json={
                    "name": "stable read",
                    "project_ids": [self.project],
                    "actions": ["read"],
                    "expires_in_days": 1,
                },
            )
            self.secret_values.append(token["token"])
            header = {"Authorization": "Bearer " + token["token"]}
            self.api("GET", f"/notebooks/{fixture['id']}", headers=header)
            self.api(
                "POST",
                f"/notebooks/{fixture['id']}/run",
                json={},
                headers=header,
                expected=403,
            )
            self.api("GET", f"/notebooks/{foreign['id']}", headers=header, expected=403)
            self.api(
                "DELETE", f"/service-accounts/{account['id']}/tokens/{token['id']}"
            )
            self.api("GET", "/projects", headers=header, expected=401)
            self.passed(
                "live scoped token action/project denials, bounded expiry inputs, owner ceiling, and revocation"
            )
        finally:
            public.close()

    def workflows(self):
        base = f"blob://{self.bucket}/workflows/{uuid4().hex[:8]}/"
        retry = self.notebook(
            "Retry once",
            f"from pathlib import Path\npath={base + 'retry'!r}\nif not Path(blob.path(path)).exists():\n    blob.write_text(path, 'first')\n    raise RuntimeError('Synthetic first attempt')\nprint('retry recovered')",
        )
        flow = self.workflow(
            "Retry proof",
            [
                {
                    "key": "retry",
                    "notebook_id": retry["id"],
                    "max_retries": 1,
                    "retry_delay_seconds": 1,
                    "retry_safe": True,
                    "resource_options": {"memory_mb": 512},
                }
            ],
        )
        result = self.await_workflow(self.start_workflow(flow)["id"])
        assert len(result["tasks"][0]["attempts"]) == 2
        assert "retry recovered" in self.stdout(
            self.api("GET", "/runs/" + result["tasks"][0]["run_id"])
        )
        self.passed(
            "real workflow retry runs exactly twice and recovers an explicitly retry-safe step"
        )
        upstream = self.notebook(
            "Repair upstream",
            f"from pathlib import Path\npath={base + 'upstream_count'!r}\ncount=int(blob.read_text(path)) if Path(blob.path(path)).exists() else 0\nblob.write_text(path,str(count+1))\nprint('upstream',count+1)",
        )
        failure = self.notebook(
            "Repair dependent",
            f"from pathlib import Path\nassert Path(blob.path({base + 'unblock'!r})).exists(), 'Synthetic downstream block'\nprint('repaired captured source')",
        )
        downstream = self.notebook(
            "Repair final",
            f"assert blob.read_text({base + 'upstream_count'!r}) == '1'\nprint('preserved upstream')",
        )
        flow = self.workflow(
            "Repair proof",
            [
                {
                    "key": "upstream",
                    "notebook_id": upstream["id"],
                    "resource_options": {"memory_mb": 512},
                },
                {
                    "key": "dependent",
                    "notebook_id": failure["id"],
                    "depends_on": ["upstream"],
                    "resource_options": {"memory_mb": 512},
                },
                {
                    "key": "final",
                    "notebook_id": downstream["id"],
                    "depends_on": ["dependent"],
                    "resource_options": {"memory_mb": 512},
                },
            ],
        )
        failed = self.await_workflow(self.start_workflow(flow)["id"], "failed")
        assert [task["status"] for task in failed["tasks"]] == [
            "succeeded",
            "failed",
            "skipped",
        ]
        unblock = self.notebook(
            "Unblock fixture", f"blob.write_text({base + 'unblock'!r}, 'yes')"
        )
        self.run(unblock)
        latest = self.api("GET", "/notebooks/" + failure["id"])
        self.api(
            "PUT",
            "/notebooks/" + failure["id"],
            json={
                **latest,
                "cells": [
                    {
                        "id": "main",
                        "language": "python",
                        "source": "raise RuntimeError('Workspace source must not replace the captured repair')",
                    }
                ],
            },
        )
        repaired = self.api("POST", f"/workflow-runs/{failed['id']}/repair", json={})
        self.state["workflow_runs"].append(repaired["id"])
        done = self.await_workflow(repaired["id"])
        assert done["tasks"][0]["run_id"] == failed["tasks"][0]["run_id"]
        assert "repaired captured source" in self.stdout(
            self.api("GET", "/runs/" + done["tasks"][1]["run_id"])
        )
        assert "preserved upstream" in self.stdout(
            self.api("GET", "/runs/" + done["tasks"][2]["run_id"])
        )
        self.passed(
            "failed-branch repair preserves successful upstream work and the original captured downstream source"
        )
        sleeper = self.notebook(
            "Workflow bounded sleeper",
            "import time\nprint('stable sleeper executing', flush=True)\ntime.sleep(30)",
        )
        timed = self.workflow(
            "Timeout proof",
            [
                {
                    "key": "slow",
                    "notebook_id": sleeper["id"],
                    "timeout_seconds": 1,
                    "resource_options": {"memory_mb": 512},
                }
            ],
        )
        timed_result = self.await_workflow(
            self.start_workflow(timed)["id"], "failed", timeout=40
        )
        timed_child = self.api("GET", "/runs/" + timed_result["tasks"][0]["run_id"])
        assert timed_child.get("error_class") == "timeout", self.safe(timed_child)
        cancelled = self.workflow(
            "Cancel proof",
            [
                {
                    "key": "slow",
                    "notebook_id": sleeper["id"],
                    "resource_options": {"memory_mb": 512},
                }
            ],
        )
        started = self.start_workflow(cancelled)

        def executing_child():
            parent = self.api("GET", "/workflow-runs/" + started["id"])
            child_id = parent["tasks"][0].get("run_id")
            return self.api("GET", "/runs/" + child_id) if child_id else {}

        executing = self.wait(
            executing_child,
            lambda item: item.get("status") == "running"
            and any(
                "stable sleeper executing" in log.get("text", "")
                for log in item.get("logs", [])
            ),
            timeout=30,
            label="executing workflow child",
        )
        self.api("POST", "/workflow-runs/" + started["id"] + "/cancel", json={})
        self.await_workflow(started["id"], "cancelled", timeout=40)
        assert self.api("GET", "/runs/" + executing["id"])["status"] == "cancelled"
        assert "healthy" in self.stdout(
            self.run(self.notebook("After timeout", "print('healthy')"))
        )
        self.passed(
            "workflow timeout and active cancellation stop their work while subsequent execution succeeds"
        )

    def schedule_prepare(self):
        target = self.notebook(
            "Durable scheduled source",
            "import json\nprint(json.dumps({'scheduled_date': params['run_date'], 'marker': 'stable-scheduler'}))",
            definitions=[{"name": "run_date", "type": "date", "required": True}],
        )
        basic_body = {
            "name": "Stable Basic " + self.suffix,
            "notebook_id": target["id"],
            "parameters": {"run_date": "2000-01-01"},
            "schedule_mode": "basic",
            "basic_schedule": {
                "frequency": "weekly",
                "time": "08:15",
                "days_of_week": [1, 3, 5],
            },
            "timezone": "America/New_York",
            "enabled": False,
            "resource_options": {"memory_mb": 512},
        }
        basic = self.api("POST", "/jobs", json=basic_body)
        assert basic["cron"] == "15 8 * * 1,3,5" and basic["next_run_at"] is None
        body = {
            "name": "Stable persisted minute " + self.suffix,
            "notebook_id": target["id"],
            "parameters": {"run_date": "2000-01-01"},
            "schedule_mode": "advanced",
            "cron": "* * * * *",
            "timezone": "UTC",
            "enabled": True,
            "schedule_date_parameter": "run_date",
            "catchup_policy": "latest",
            "catchup_max_runs": 2,
            "resource_options": {"memory_mb": 512},
        }
        job = self.api("POST", "/jobs", json=body)
        self.state["jobs"].extend([basic["id"], job["id"]])
        self.state["schedule"] = {
            "job_id": job["id"],
            "notebook_id": target["id"],
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "next_run_at": job["next_run_at"],
            "body": body,
        }
        self.passed(
            "Basic timezone/weekday cron and a persisted enabled Advanced minute schedule are prepared for restart"
        )
        print(
            "RESTART REQUIRED: restart only the disposable management/compute services, then run --section schedule-verify with this receipt",
            flush=True,
        )

    def resources(self):
        resident = self.notebook(
            "Memory isolation resident", "answer = 42\nprint('resident ready')"
        )
        first = self.run(
            resident,
            cell_id="main",
            resource_options={"mode": "interactive", "memory_mb": 512},
        )
        marker = "openbricks-stable-memory-" + uuid4().hex
        child_source = f"import time\nmarker={marker!r}\nallocation=bytearray(384*1024*1024)\ntime.sleep(30)"
        source = f"import subprocess,sys,time\nchild=subprocess.Popen([sys.executable,'-c',{child_source!r}],start_new_session=True)\nprint('detached allocation', child.pid, flush=True)\ntime.sleep(30)"
        over = self.notebook("Detached memory budget", source)
        result = self.run(
            over,
            status="failed",
            timeout_seconds=20,
            resource_options={"mode": "job", "memory_mb": 256},
        )
        assert result.get("error_class") == "resource_limit", self.safe(result)
        assert result.get("peak_memory_bytes", 0) > 256 * 1024**2
        assert "256 MB" in result.get("error", "")
        latest = self.api("GET", "/notebooks/" + resident["id"])
        self.api(
            "PUT",
            "/notebooks/" + resident["id"],
            json={
                **latest,
                "cells": [
                    {
                        "id": "main",
                        "language": "python",
                        "source": f"import psutil\nprint('retained',answer)\nleft=[]\nfor process in psutil.process_iter(['pid','cmdline','status']):\n    if process.info['status'] != psutil.STATUS_ZOMBIE and {marker!r} in ' '.join(process.info['cmdline'] or []):\n        left.append(process.info['pid'])\nprint('detached remaining',len(left))",
                    }
                ],
            },
        )
        after = self.run(
            resident,
            cell_id="main",
            resource_options={"mode": "interactive", "memory_mb": 512},
        )
        assert after.get("session_id") == first.get("session_id")
        assert "retained 42" in self.stdout(
            after
        ) and "detached remaining 0" in self.stdout(after)
        self.api(
            "POST",
            "/resources/sessions/" + after["session_id"] + "/stop",
            json={"cluster_id": "local", "project_id": self.project},
        )
        self.state["resource_receipt"] = {
            "over_budget_run": result["id"],
            "error_class": result["error_class"],
            "worker_budget_mb": 256,
            "peak_bytes": result["peak_memory_bytes"],
            "surviving_session": after["session_id"],
            "detached_children_remaining": 0,
        }
        self.passed(
            "live process-tree memory guard stops a detached allocation while another notebook retains its interactive state"
        )

    def schedule_verify(self):
        value = self.state.get("schedule")
        assert value, "Run schedule-prepare with this receipt first"
        job = next(
            item
            for item in self.api("GET", "/jobs", params={"project_id": self.project})
            if item["id"] == value["job_id"]
        )
        assert job["enabled"] and job["next_run_at"]
        checked_at = datetime.now(timezone.utc).isoformat()

        def fires():
            page = self.api(
                "GET",
                "/history/runs",
                params={
                    "project_id": self.project,
                    "notebook_id": value["notebook_id"],
                    "limit": 100,
                },
            )
            return [
                item
                for item in page["items"]
                if item.get("job_id") == value["job_id"]
                and (item.get("created_at") or "") >= checked_at
            ]

        observed = self.wait(
            fires,
            lambda items: bool(items),
            timeout=100,
            label="real post-restart cron occurrence",
        )
        try:
            result = self.await_run(observed[0]["id"], timeout=45)
            assert result["trigger"] == "schedule"
            assert (
                result["parameters"]["run_date"]
                == datetime.fromisoformat(result["created_at"])
                .astimezone(timezone.utc)
                .date()
                .isoformat()
            )
            assert "stable-scheduler" in self.stdout(result)
            assert result["resource_options"]["mode"] == "job"
            self.state["schedule"]["post_restart_run"] = result["id"]
            self.passed(
                "actual scheduler fires after restart with UTC date injection, preserved job identity, and a fresh worker"
            )
        finally:
            self.api(
                "PUT",
                "/jobs/" + value["job_id"],
                json={**value["body"], "enabled": False},
            )

    def compute_restart_prepare(self):
        marker = f"blob://{self.bucket}/restart/{uuid4().hex}/count.txt"
        source = f"from pathlib import Path\nimport time\npath={marker!r}\ncount=int(blob.read_text(path)) if Path(blob.path(path)).exists() else 0\nblob.write_text(path,str(count+1))\nprint('stable restart marker committed',flush=True)\ntime.sleep(120)\nprint('unexpected uninterrupted completion')"
        target = self.notebook("Compute restart interruption", source)
        started = self.submit(target, timeout_seconds=180)
        self.wait(
            lambda: self.api("GET", "/runs/" + started["id"]),
            lambda value: value["status"] == "running"
            and any(
                "stable restart marker committed" in log.get("text", "")
                for log in value.get("logs", [])
            ),
            timeout=40,
            label="committed pre-restart marker",
        )
        self.state["compute_restart"] = {
            "run_id": started["id"],
            "notebook_id": target["id"],
            "marker": marker,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        self.passed(
            "a real compute worker commits one side-effect marker before external restart"
        )
        print(
            "RESTART REQUIRED: restart only the disposable compute service, then run --section compute-restart-verify",
            flush=True,
        )

    def compute_restart_verify(self):
        value = self.state.get("compute_restart")
        assert value, "Run compute-restart-prepare with this receipt first"
        result = self.wait(
            lambda: self.api("GET", "/runs/" + value["run_id"]),
            lambda item: item["status"] in TERMINAL,
            timeout=90,
            label="interrupted compute outcome",
        )
        assert result["status"] in {"failed", "cancelled"}, self.safe(result)
        self.wait(
            lambda: self.api(
                "GET",
                "/resources/status",
                params={"cluster_id": "local", "project_id": self.project},
            ),
            lambda item: item["active_runs"] == item["queued_runs"] == 0,
            timeout=30,
            label="post-restart compute admission",
        )
        probe = self.notebook(
            "Post restart fresh worker",
            f"assert blob.read_text({value['marker']!r}) == '1'\nprint('one committed marker; fresh worker healthy')",
        )
        after = self.run(probe)
        assert "fresh worker healthy" in self.stdout(after)
        history = self.api(
            "GET",
            "/history/runs",
            params={
                "project_id": self.project,
                "notebook_id": value["notebook_id"],
                "limit": 100,
            },
        )
        assert history["total"] == 1 and history["items"][0]["id"] == value["run_id"]
        self.state["compute_restart"].update(
            verified=True,
            terminal_status=result["status"],
            terminal_error_class=result.get("error_class"),
            committed_marker_count=1,
            fresh_run_id=after["id"],
        )
        self.passed(
            "compute restart reports interruption without duplicate execution, retains one side effect, and accepts fresh work"
        )

    def remote(self):
        url = os.getenv("OPENBRICKS_SMOKE_REMOTE_URL", "http://remote:8001")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"remote", "remote-compute"}
            or parsed.port != 8001
            or parsed.path
            or parsed.query
            or parsed.username
        ):
            raise ValueError(
                "Remote proof only accepts the isolated remote:8001 service"
            )
        token = os.environ["OPENBRICKS_SMOKE_REMOTE_TOKEN"]
        self.secret_values.append(token)
        remote = self.api(
            "POST",
            "/clusters",
            json={
                "name": "Stable remote " + self.suffix,
                "url": url,
                "token": token,
                "allow_insecure": True,
            },
        )
        self.state["remote_cluster_id"] = remote["id"]
        project = next(
            item for item in self.api("GET", "/projects") if item["id"] == self.project
        )
        self.api(
            "PUT",
            "/projects/" + self.project,
            json={**project, "cluster_ids": ["local", remote["id"]]},
        )
        a = self.api("POST", "/clusters/local/test", json={})
        b = self.api("POST", f"/clusters/{remote['id']}/test", json={})
        assert (
            a["ok"] and b["ok"] and a["status"]["hostname"] != b["status"]["hostname"]
        )
        repo = self.api(
            "POST",
            "/repositories",
            json={
                "name": "Stable wheel source " + self.suffix,
                "url": "https://github.com/DylanBarber/OpenBricks.git",
                "branch": "main",
                "project_id": self.project,
            },
        )
        built = self.api(
            "POST",
            "/packages/build",
            json={"repository_id": repo["id"], "path": "examples/brickkit"},
        )
        assert any(
            item["name"].replace("-", "_") == "openbricks_brickkit" for item in built
        )
        source = "from brickkit import normalize_name\nprint(normalize_name('  ada  lovelace '))\nfrom pyspark.sql.functions import udf\nfrom pyspark.sql.types import StringType\nnormalize = udf(lambda text: __import__('brickkit').normalize_name(text), StringType())\ndisplay(spark.createDataFrame([('  ada  lovelace ',)], ['name']).select(normalize('name').alias('normalized')))"
        for cluster in ("local", remote["id"]):
            target = self.notebook(
                "Wheel Spark " + cluster[:6], source, cluster=cluster
            )
            result = self.run(
                target, resource_options={"mode": "job", "memory_mb": 1536}
            )
            assert "Ada Lovelace" in self.stdout(result) and result["outputs"][0][
                "result"
            ]["rows"] == [["Ada Lovelace"]]
            assert result["cluster_id"] == cluster
        self.passed(
            "wheel builds from real Git, replicates to independent remote storage, and imports in both Python drivers and Spark UDF workers"
        )
        sleeper = (
            "import time\nprint('started', flush=True)\ntime.sleep(8)\nprint('done')"
        )
        local_nb = self.notebook("Concurrent local", sleeper)
        remote_nb = self.notebook("Concurrent remote", sleeper, cluster=remote["id"])
        first, second = self.submit(local_nb), self.submit(remote_nb)
        done_a, done_b = self.await_run(first["id"]), self.await_run(second["id"])
        assert max(done_a["started_at"], done_b["started_at"]) < min(
            done_a["finished_at"], done_b["finished_at"]
        )
        # A local blob belongs to the remote host rather than silently resolving
        # to the management/bundled volume.
        remote_path = "blob://stable-remote-private/" + self.suffix + ".txt"
        self.run(
            self.notebook(
                "Remote private file",
                f"blob.write_text({remote_path!r}, 'remote-only')",
                cluster=remote["id"],
            )
        )
        check = self.run(
            self.notebook(
                "Local private absence",
                f"from pathlib import Path\nprint(Path(blob.path({remote_path!r})).exists())",
            )
        )
        assert self.stdout(check).strip() == "False"
        self.passed(
            "real local and remote hosts overlap work and retain independent local blob files"
        )

    def s3(self):
        if self.state.get("s3_connection_id"):
            return self.state["s3_connection_id"]
        key, secret = (
            os.environ["OPENBRICKS_SMOKE_S3_ACCESS_KEY"],
            os.environ["OPENBRICKS_SMOKE_S3_SECRET_KEY"],
        )
        self.secret_values.extend([key, secret])
        connection = self.api(
            "POST",
            "/connections",
            json={
                "name": "Stable storage " + self.suffix,
                "type": "s3",
                "project_ids": [self.project],
                "config": {
                    "endpoint_url": os.getenv(
                        "OPENBRICKS_SMOKE_S3_ENDPOINT", "http://seaweedfs:8333"
                    ),
                    "region": "us-east-1",
                    "addressing_style": "path",
                    "verify_tls": True,
                },
                "secrets": {"access_key": key, "secret_key": secret},
            },
        )
        self.state["s3_connection_id"] = connection["id"]
        self.checkpoint()
        return connection["id"]

    def catalog(self):
        connection = self.s3()
        assert self.api("POST", f"/connections/{connection}/test", json={})["ok"]
        bucket = os.getenv("OPENBRICKS_SMOKE_CATALOG_BUCKET", "stable-data")
        buckets = self.api("GET", f"/connections/{connection}/buckets")["items"]
        if not any(item["name"] == bucket for item in buckets):
            self.api(
                "POST", f"/connections/{connection}/buckets", json={"name": bucket}
            )
        csv_key = "stable/" + self.suffix + "/values.csv"
        self.api(
            "POST",
            f"/connections/{connection}/objects",
            data={"bucket": bucket, "key": csv_key},
            files={"file": ("values.csv", b"value\n20\n22\n", "text/csv")},
        )
        for key in ("a.txt", "b.txt", "c.txt"):
            self.api(
                "POST",
                f"/connections/{connection}/objects",
                data={"bucket": bucket, "key": "stable/" + self.suffix + "/" + key},
                files={"file": (key, b"pagination", "text/plain")},
            )
        token, seen, keys = None, set(), []
        for _ in range(8):
            params = {
                "bucket": bucket,
                "prefix": "stable/" + self.suffix + "/",
                "limit": 1,
            }
            if token:
                params["continuation_token"] = token
            page = self.api("GET", f"/connections/{connection}/objects", params=params)
            keys.extend(item["key"] for item in page["items"])
            token = page.get("next_token")
            if not token:
                break
            assert token not in seen
            seen.add(token)
        assert len(keys) == len(set(keys)) == 4
        remote = self.state.get("remote_cluster_id")
        for cluster in ["local"] + ([remote] if remote else []):
            nb = self.notebook(
                "Named S3 Spark",
                cells=[
                    {
                        "id": "load",
                        "language": "python",
                        "source": f"path=connections.s3({connection!r}).path('s3://{bucket}/{csv_key}')\nspark.read.option('header',True).option('inferSchema',True).csv(path).createOrReplaceTempView('stable_values')",
                    },
                    {
                        "id": "query",
                        "language": "sql",
                        "source": "SELECT SUM(value) AS answer FROM stable_values",
                    },
                ],
                cluster=cluster,
                connections=[connection],
            )
            result = self.run(nb, resource_options={"mode": "job", "memory_mb": 1536})
            assert result["outputs"][1]["result"]["rows"] == [[42]]
        empty = self.notebook(
            "Unselected connector denial", f"connections.s3({connection!r})"
        )
        assert self.run(empty, status="failed")["status"] == "failed"
        self.passed(
            "real named S3 upload/paging/reads on selected compute hosts and rejection when no connector is selected"
        )
        assert self.api("GET", "/catalog/status", params={"project_id": self.project})[
            "available"
        ]
        response = self.client.post(
            "/api/catalog/catalogs", json={"name": "unity", "project_id": self.project}
        )
        assert response.status_code in (201, 409), self.safe(response.text)
        schema = "stable_" + self.suffix
        self.api(
            "POST",
            "/catalog/schemas",
            json={"project_id": self.project, "catalog_name": "unity", "name": schema},
        )
        local_path = f"/data/buckets/{self.bucket}/catalog-values"
        s3_path = f"s3a://{bucket}/stable/{self.suffix}/catalog-values"
        write = self.notebook(
            "Catalog physical data",
            f"frame=spark.createDataFrame([(20,), (22,)], ['value'])\nframe.write.mode('overwrite').parquet({local_path!r})\nframe.write.mode('overwrite').parquet({s3_path!r})\nprint('physical data written')",
        )
        self.run(write, resource_options={"mode": "job", "memory_mb": 1536})
        for name, location in (
            ("local_values", "file://" + local_path),
            ("s3_values", s3_path.replace("s3a://", "s3://")),
        ):
            self.api(
                "POST",
                "/catalog/tables",
                json={
                    "project_id": self.project,
                    "catalog_name": "unity",
                    "schema_name": schema,
                    "name": name,
                    "data_source_format": "PARQUET",
                    "storage_location": location,
                    "properties": {"fixture": self.suffix},
                    "columns": [
                        {
                            "name": "value",
                            "type_name": "LONG",
                            "type_text": "bigint",
                            "comment": "Synthetic measure",
                        }
                    ],
                },
            )
            result = self.run(
                self.notebook(
                    "Catalog SQL " + name,
                    cells=[
                        {
                            "id": "query",
                            "language": "sql",
                            "source": f"SELECT SUM(value) AS answer FROM unity.{schema}.{name}",
                        }
                    ],
                ),
                resource_options={"mode": "job", "memory_mb": 1536},
            )
            assert result["outputs"][0]["result"]["rows"] == [[42]]
        self.passed(
            "actual Unity Catalog registration and three-part Spark SQL over both local Parquet and native S3A data"
        )

    def notifications(self):
        receiver = os.getenv("OPENBRICKS_SMOKE_RECEIVER_URL", "http://127.0.0.1:8025")
        parsed = urlsplit(receiver)
        assert (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == 8025
            and not parsed.path
        )
        webhook = self.api(
            "POST",
            "/notifications",
            json={
                "name": "Stable actual webhook " + self.suffix,
                "project_id": self.project,
                "type": "webhook",
                "events": ["failed", "succeeded"],
                "config": {
                    "url": "http://receiver:8025/webhook/fail-once/" + self.suffix,
                    "allow_insecure": True,
                },
                "secrets": {"token": "synthetic-receiver-token"},
            },
        )
        email = self.api(
            "POST",
            "/notifications",
            json={
                "name": "Stable actual SMTP " + self.suffix,
                "project_id": self.project,
                "type": "email",
                "events": ["failed", "succeeded"],
                "config": {
                    "host": "receiver",
                    "port": 2525,
                    "sender": "openbricks@example.test",
                    "recipients": ["sink@example.test"],
                    "starttls": False,
                    "ssl": False,
                },
            },
        )
        assert "url" not in webhook[
            "config"
        ] and "synthetic-receiver-token" not in json.dumps(webhook)
        assert "_secrets" not in webhook and "secrets" not in webhook
        self.api("POST", f"/notifications/{email['id']}/test", json={})
        target = self.notebook(
            "Notification failure",
            "raise RuntimeError('Synthetic notification failure')",
        )
        body = {
            "name": "Stable failure alert " + self.suffix,
            "notebook_id": target["id"],
            "enabled": False,
            "cron": "0 0 1 1 *",
            "notification_ids": [webhook["id"], email["id"]],
            "resource_options": {"memory_mb": 512},
        }
        job = self.api("POST", "/jobs", json=body)
        self.state["jobs"].append(job["id"])
        started = self.api("POST", f"/jobs/{job['id']}/run", json={})
        result = self.await_run(started["id"], status="failed")
        self.state["runs"].append(result["id"])
        # The orchestration watcher must not restart management until this
        # last run-status request is finished. Everything after this durable
        # handshake polls only the independent receiver, so the expected
        # management outage cannot interrupt the proof's API polling.
        self.state["notification_pending_run_id"] = result["id"]
        self.checkpoint()
        with httpx.Client(base_url=receiver, timeout=10) as sink:

            def delivered():
                response = sink.get("/events")
                response.raise_for_status()
                return [
                    item
                    for item in response.json()["items"]
                    if item.get("payload", {}).get("run_id") == result["id"]
                ]

            events = self.wait(
                delivered,
                lambda items: any(item["channel"] == "smtp" for item in items)
                and any(
                    item["channel"] == "webhook" and item["accepted"] for item in items
                ),
                timeout=100,
                label="real SMTP and retrying webhook",
            )
            smtp = [item for item in events if item["channel"] == "smtp"]
            web = [item for item in events if item["channel"] == "webhook"]
            assert len(smtp) == 1 and len(web) == 2
            assert not web[0]["accepted"] and web[1]["accepted"]
            assert (
                web[0]["payload"]["delivery_id"]
                == web[1]["payload"]["delivery_id"]
                == web[1]["idempotency_key"]
            )
            assert all(item["payload"]["event"] == "failed" for item in events)
            self.state["notification_receipt"] = {
                "run_id": result["id"],
                "delivery_id": web[0]["payload"]["delivery_id"],
                "smtp_count": len(smtp),
                "webhook_attempts": len(web),
                "first_rejection_at": datetime.fromtimestamp(
                    web[0]["received_at"], timezone.utc
                ).isoformat(),
                "accepted_at": datetime.fromtimestamp(
                    web[1]["received_at"], timezone.utc
                ).isoformat(),
            }
            time.sleep(3)
            assert len(delivered()) == len(events)
        self.passed(
            "real local SMTP delivery and a failed-then-successful HTTP webhook retry retain one durable delivery ID without duplicate acceptance"
        )

    def history(self):
        target = self.notebook("History paging", "print('bounded synthetic history')")
        for _ in range(self.args.history_runs):
            self.run(target)
        ids, offset = [], 0
        while True:
            page = self.api(
                "GET",
                "/history/runs",
                params={
                    "project_id": self.project,
                    "notebook_id": target["id"],
                    "limit": 5,
                    "offset": offset,
                },
            )
            assert all(
                "outputs" not in item and not any(key.startswith("_") for key in item)
                for item in page["items"]
            )
            ids.extend(item["id"] for item in page["items"])
            offset += len(page["items"])
            if offset >= page["total"]:
                break
        assert len(ids) == len(set(ids)) == self.args.history_runs
        prefix = "Stable paged jobs " + self.suffix
        for index in range(self.args.history_jobs):
            job = self.api(
                "POST",
                "/jobs",
                json={
                    "name": prefix + " " + str(index),
                    "notebook_id": target["id"],
                    "enabled": False,
                    "cron": "0 0 1 1 *",
                    "resource_options": {"memory_mb": 512},
                },
            )
            self.state["jobs"].append(job["id"])
        ids, offset = [], 0
        while True:
            page = self.api(
                "GET",
                "/jobs/page",
                params={
                    "project_id": self.project,
                    "search": prefix,
                    "limit": 17,
                    "offset": offset,
                },
            )
            ids.extend(item["id"] for item in page["items"])
            offset += len(page["items"])
            if offset >= page["total"]:
                break
        assert len(ids) == len(set(ids)) == self.args.history_jobs
        overview = self.api("GET", "/overview", params={"project_id": self.project})
        assert len(overview["runs"]) <= 8 and all(
            "outputs" not in item for item in overview["runs"]
        )
        self.passed(
            f"{self.args.history_runs} real runs and {self.args.history_jobs} disabled schedules page without omissions, duplicates, or output-heavy summaries"
        )

    def close(self):
        if self.failed and getattr(self, "project", None):
            # Only cancel this receipt's synthetic work. Never reset a shared
            # compute service or alter another project to recover a failed test.
            for kind, route in (("workflow_runs", "workflow-runs"), ("runs", "runs")):
                for identifier in self.state.get(kind, []):
                    try:
                        value = self.api("GET", f"/{route}/{identifier}")
                        if (
                            value.get("project_id") == self.project
                            and value.get("status") not in TERMINAL
                        ):
                            self.api("POST", f"/{route}/{identifier}/cancel", json={})
                    except (AssertionError, httpx.HTTPError):
                        pass
            try:
                for job in self.api(
                    "GET", "/jobs", params={"project_id": self.project}
                ):
                    if job["id"] in self.state["jobs"] and job.get("enabled"):
                        self.api(
                            "PUT", "/jobs/" + job["id"], json={**job, "enabled": False}
                        )
            except (AssertionError, httpx.HTTPError):
                pass
        self.checkpoint()
        self.client.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:3001")
    parser.add_argument(
        "--receipt", default=str(ROOT / ".test-data/stable-platform-smoke.json")
    )
    parser.add_argument(
        "--section",
        required=True,
        choices=(
            "core",
            "auth",
            "workflows",
            "resources",
            "remote",
            "catalog",
            "notifications",
            "history",
            "schedule-prepare",
            "schedule-verify",
            "compute-restart-prepare",
            "compute-restart-verify",
        ),
    )
    parser.add_argument("--history-runs", type=int, default=24)
    parser.add_argument("--history-jobs", type=int, default=121)
    args = parser.parse_args()
    if not 10 <= args.history_runs <= 250 or not 21 <= args.history_jobs <= 650:
        parser.error("History proof requires 10–250 runs and 21–650 jobs")
    proof = Proof(args)
    try:
        proof.setup()
        if args.section == "core":
            proof.auth()
            proof.workflows()
            proof.history()
        else:
            getattr(proof, args.section.replace("-", "_"))()
        proof.state.setdefault("completed_sections", []).append(args.section)
        proof.checkpoint()
        print("PASS: stable platform section " + args.section, flush=True)
    except Exception as exc:
        proof.failed = True
        print("FAIL: " + proof.safe(exc), flush=True)
        raise SystemExit(1)
    finally:
        proof.close()


if __name__ == "__main__":
    main()
