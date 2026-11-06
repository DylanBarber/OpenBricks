"""Build and rigorously test a fresh, disposable OpenBricks installation.

Requires Python 3.10+, httpx, PyYAML, and Docker Compose v2 on Linux containers.
Only the reserved openbricks-smoke project and loopback ports 3001/3002/8025/8036
are supported. Existing resources in that disposable project are removed first
and all its containers/volumes are removed on exit. Production port 3000 and
the normal OpenBricks Compose project are never used.

Detailed output, generated credentials, SQL fixtures and recovery archives stay
in a new private directory below .test-data/system-smoke. Do not publish it.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "openbricks-smoke"
ORIGIN = "http://127.0.0.1:3001"
FRESH_ORIGIN = "http://127.0.0.1:3002"
RECEIVER = "http://127.0.0.1:8025"
SERVICES = {
    "control",
    "compute",
    "remote",
    "sqlserver",
    "seaweedfs",
    "catalog",
    "receiver",
    "recovery-control",
    "recovery-compute",
    "recovery-storage",
    "recovery-catalog",
}
PHASES = (
    "fresh disposable workspace and integration readiness",
    "v4 runtime, DuckDB, encrypted backup and negative recovery checks",
    "SQL Server typed ODBC/JDBC, Change Tracking and failure matrix",
    "platform authentication, workflows and paginated history",
    "Git import, conflict detection and local authoring",
    "v4 backfills, file/upstream triggers and real Delta commits",
    "CLI deployment, promotion and scoped/revoked automation tokens",
    "independent remote compute, wheel replication and Spark UDFs",
    "Unity Catalog over local Parquet and native S3A",
    "detached-child memory enforcement and session isolation",
    "real SMTP and webhook retry across management restart",
    "Basic/Advanced schedules and actual cron after management restart",
    "compute restart with one committed side effect and no duplicate attempt",
    "v5 real Spark profiling, shared-catalog concurrency and cancellation isolation",
    "v5 secrets, dashboards, assets, reviews, live coediting and real OIDC browser E2E",
    "encrypted backup into a fresh host, external recovery and idempotent replay",
)


class SmokeError(RuntimeError):
    """A safe error that contains no child output or credentials."""


def validate_target(project, origin):
    if project != PROJECT or origin.rstrip("/") != ORIGIN:
        raise SmokeError("Only openbricks-smoke at http://127.0.0.1:3001 is allowed")
    configured = os.getenv("OPENBRICKS_SMOKE_URL")
    if configured and configured.rstrip("/") != ORIGIN:
        raise SmokeError(
            "OPENBRICKS_SMOKE_URL points outside the disposable test origin"
        )


def validate_config(config):
    """Reject a rendered Compose file that could escape the test namespace."""
    if config.get("name") != PROJECT or set(config.get("services", {})) != SERVICES:
        raise SmokeError("Compose configuration has an unexpected project or service")
    for category in ("volumes", "networks"):
        for value in config.get(category, {}).values():
            if value.get("external") or not value.get("name", "").startswith(
                PROJECT + "_"
            ):
                raise SmokeError(
                    "Compose resources must stay inside the disposable namespace"
                )
    allowed_binds = {
        (ROOT / "scripts/stable-notification-receiver.py").resolve(),
        (ROOT / "scripts/v5-oidc-provider.py").resolve(),
        *(
            path.resolve()
            for path in (ROOT / "docker/catalog").iterdir()
            if path.is_file()
        ),
    }
    allowed_ports = {
        "control": {("127.0.0.1", "3001", 3000)},
        "recovery-control": {("127.0.0.1", "3002", 3000)},
        "receiver": {("127.0.0.1", "8025", 8025), ("127.0.0.1", "8036", 8036)},
    }
    for name, service in config["services"].items():
        if (
            service.get("privileged")
            or service.get("network_mode")
            or service.get("pid")
        ):
            raise SmokeError("Compose services cannot use privileged or host access")
        ports = {
            (port.get("host_ip"), str(port.get("published")), port.get("target"))
            for port in service.get("ports", [])
        }
        if ports != allowed_ports.get(name, set()):
            raise SmokeError(
                "Compose services may publish only the reserved loopback test ports"
            )
        for volume in service.get("volumes", []):
            if volume.get("type") == "volume":
                if volume.get("source") not in config.get("volumes", {}):
                    raise SmokeError("Compose volume is outside the disposable project")
            elif volume.get("type") == "bind":
                if (
                    not volume.get("read_only")
                    or Path(volume["source"]).resolve() not in allowed_binds
                ):
                    raise SmokeError(
                        "Only the read-only smoke receiver and catalog configuration may be mounted"
                    )
            else:
                raise SmokeError("Unexpected Compose mount type")


def rejected_delivery(events, baseline, project, since, pending_run_id):
    """Find a new failed delivery for this proof, never an old/unrelated alert."""
    for event in events:
        payload = event.get("payload", {})
        delivery = payload.get("delivery_id")
        if (
            event.get("channel") == "webhook"
            and event.get("accepted") is False
            and delivery
            and delivery not in baseline
            and payload.get("project_id") == project
            and pending_run_id
            and payload.get("run_id") == pending_run_id
            and payload.get("event") == "failed"
            and event.get("received_at", 0) >= since
        ):
            return event
    return None


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class Runner:
    def __init__(self, args):
        validate_target(args.project, args.url)
        if bool(args.runtime_image) != bool(args.compute_image):
            raise SmokeError(
                "Supply both --runtime-image and --compute-image, or neither"
            )
        for value in (args.runtime_image, args.compute_image):
            if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", value):
                raise SmokeError("Invalid Docker image reference")
        self.args = args
        self.identifier = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid4().hex[:8]
        )
        self.output = (ROOT / ".test-data/system-smoke" / self.identifier).resolve()
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OPENBRICKS_", "COMPOSE_"))
        }
        self.environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
        self.environment["PYTHONUNBUFFERED"] = "1"
        # The proof scripts intentionally use assertions; inherited Python
        # optimization must never turn the release gate into a no-op.
        self.environment["PYTHONOPTIMIZE"] = "0"
        self.environment["NO_PROXY"] = ",".join(
            value
            for value in (
                self.environment.get("NO_PROXY", ""),
                "127.0.0.1",
                "localhost",
            )
            if value
        )
        self.summary = {
            "format": "openbricks-system-smoke-v1",
            "run_id": self.identifier,
            "project": PROJECT,
            "origin": ORIGIN,
            "passed": False,
            "phases": [],
        }
        self.compose = []
        self.ready_for_cleanup = False
        self.counter = 0

    def save(self):
        (self.output / "summary.json").write_text(
            json.dumps(self.summary, indent=2), encoding="utf-8"
        )

    def prepare(self):
        for module in ("httpx", "yaml"):
            if importlib.util.find_spec(module) is None:
                raise SmokeError(
                    "Install the host test dependencies: python -m pip install ./cli"
                )
        if (
            not shutil.which("node")
            or not (ROOT / "web/node_modules/@playwright/test/cli.js").is_file()
        ):
            raise SmokeError(
                "Install web test dependencies with npm --prefix web ci and the Playwright Chromium browser before running the full smoke suite"
            )
        if os.name != "nt":
            os.umask(0o077)
        self.output.mkdir(parents=True, mode=0o700)
        self.save()
        values = {
            "OPENBRICKS_SMOKE_PASSWORD": "Smoke" + secrets.token_hex(24) + "Aa1!",
            "OPENBRICKS_SMOKE_COMPUTE_TOKEN": secrets.token_hex(48),
            "OPENBRICKS_SMOKE_REMOTE_TOKEN": secrets.token_hex(48),
            "OPENBRICKS_SMOKE_RECOVERY_TOKEN": secrets.token_hex(48),
            "OPENBRICKS_SMOKE_S3_ACCESS_KEY": "smoke-" + secrets.token_hex(12),
            "OPENBRICKS_SMOKE_S3_SECRET_KEY": secrets.token_hex(32),
            "OPENBRICKS_SQLSERVER_PASSWORD": "Sql" + secrets.token_hex(24) + "Aa1!",
            "OPENBRICKS_RECOVERY_BOOTSTRAP_PASSWORD": "Recover"
            + secrets.token_hex(24)
            + "Aa1!",
            "OPENBRICKS_SMOKE_RUNTIME_IMAGE": self.args.runtime_image
            or "openbricks:smoke",
            "OPENBRICKS_SMOKE_COMPUTE_IMAGE": self.args.compute_image
            or "openbricks-compute:smoke",
        }
        self.environment.update(values)
        self.environment.update(
            {
                "OPENBRICKS_SMOKE_URL": ORIGIN,
                "OPENBRICKS_SMOKE_USERNAME": "admin",
                "OPENBRICKS_SMOKE_CLUSTER_ID": "local",
                "OPENBRICKS_SMOKE_S3_ENDPOINT": "http://seaweedfs:8333",
                "OPENBRICKS_SMOKE_REMOTE_URL": "http://remote:8001",
                "OPENBRICKS_SMOKE_RECEIVER_URL": RECEIVER,
                "OPENBRICKS_RECOVERY_BOOTSTRAP_USERNAME": "recovery",
                "OPENBRICKS_RECOVERY_SOURCE_PASSWORD": values[
                    "OPENBRICKS_SMOKE_PASSWORD"
                ],
                "OPENBRICKS_RECOVERY_S3_ACCESS_KEY": values[
                    "OPENBRICKS_SMOKE_S3_ACCESS_KEY"
                ],
                "OPENBRICKS_RECOVERY_S3_SECRET_KEY": values[
                    "OPENBRICKS_SMOKE_S3_SECRET_KEY"
                ],
            }
        )
        env_file = self.output / "smoke.env"
        env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        self.compose = [
            "docker",
            "compose",
            "--project-name",
            PROJECT,
            "--project-directory",
            str(ROOT),
            "--file",
            str(ROOT / "compose.smoke.yaml"),
            "--env-file",
            str(env_file),
            "--profile",
            "sql",
            "--profile",
            "remote",
            "--profile",
            "recovery",
        ]
        self.capture(
            ["docker", "compose", "version", "--short"], "Docker Compose availability"
        )
        rendered = self.capture(
            self.compose + ["config", "--format", "json"],
            "validate isolated Compose configuration",
        )
        validate_config(json.loads(rendered))
        self.ready_for_cleanup = True
        # No global prune, normal project stop, or production-volume command.
        self.command(
            self.compose + ["down", "--volumes", "--remove-orphans", "--timeout", "30"],
            "remove stale disposable smoke resources",
            timeout=180,
        )
        for port in (3001, 3002, 8025, 8036):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    raise SmokeError(
                        f"Reserved smoke port {port} is already used by another process"
                    ) from None
        if not self.args.runtime_image:
            build_id = self.capture(
                ["git", "rev-parse", "HEAD"], "identify source"
            ).strip()
            self.summary["build_id"] = build_id
            for target in ("compute", "runtime"):
                image = values["OPENBRICKS_SMOKE_" + target.upper() + "_IMAGE"]
                self.command(
                    [
                        "docker",
                        "build",
                        "--target",
                        target,
                        "--tag",
                        image,
                        "--build-arg",
                        "OPENBRICKS_BUILD_ID=" + build_id,
                        str(ROOT),
                    ],
                    "build " + target + " image",
                    timeout=2400,
                )

    def capture(self, command, label, *, timeout=120):
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=self.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SmokeError(label + " failed or timed out") from None
        if result.returncode:
            # Rendered Compose may contain credentials; never echo it.
            raise SmokeError(label + " failed")
        return result.stdout

    def command(self, command, label, *, timeout=1200, stdin=None, monitor=None):
        self.counter += 1
        filename = (
            f"{self.counter:02d}-"
            + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
            + ".log"
        )
        phase = {"name": label, "log": filename, "status": "running"}
        self.summary["phases"].append(phase)
        self.save()
        print("RUN: " + label, flush=True)
        started, heartbeat = time.monotonic(), time.monotonic()
        process = None
        try:
            with (self.output / filename).open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=self.environment,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                )
                if stdin is not None:
                    process.stdin.write(stdin)
                    process.stdin.close()
                while process.poll() is None:
                    if time.monotonic() - started > timeout:
                        raise SmokeError(label + " timed out")
                    if monitor:
                        monitor()
                    if time.monotonic() - heartbeat > 20:
                        print("RUN: " + label + " (still running)", flush=True)
                        heartbeat = time.monotonic()
                    time.sleep(0.2)
                if process.returncode:
                    raise SmokeError(label + " failed; inspect its private log")
            phase["status"] = "passed"
            print("PASS: " + label, flush=True)
        except BaseException:
            phase["status"] = "failed"
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            raise
        finally:
            phase["seconds"] = round(time.monotonic() - started, 2)
            self.save()

    def up(self, *services):
        self.command(
            self.compose
            + ["up", "--detach", "--wait", "--wait-timeout", "240", *services],
            "start disposable " + ", ".join(services),
            timeout=360,
        )

    def stop(self, *services):
        self.command(
            self.compose + ["stop", "--timeout", "30", *services],
            "stop disposable " + ", ".join(services),
            timeout=180,
        )

    def restart(self, *services):
        self.command(
            self.compose + ["restart", "--timeout", "30", *services],
            "restart disposable " + ", ".join(services),
            timeout=180,
        )

    @staticmethod
    def get_json(url):
        with urlopen(url, timeout=4) as response:
            return json.load(response)

    def wait_http(self, origin=ORIGIN):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                if self.get_json(origin + "/api/health").get("status") == "ok":
                    return
            except (OSError, URLError, ValueError):
                pass
            time.sleep(0.4)
        raise SmokeError("Disposable management did not become healthy")

    def wait_compute(self, service="compute"):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            container = self.capture(
                self.compose + ["ps", "--all", "--quiet", service],
                "find disposable compute",
            ).strip()
            if container:
                state = json.loads(
                    self.capture(
                        ["docker", "inspect", "--format", "{{json .State}}", container],
                        "inspect disposable compute readiness",
                    )
                )
                if (
                    state.get("Running")
                    and state.get("Health", {}).get("Status") == "healthy"
                ):
                    return
            time.sleep(1)
        raise SmokeError("Disposable compute did not become healthy")

    def wait_integrations(self, recovery=False):
        import httpx

        deadline = time.monotonic() + 180
        origin = FRESH_ORIGIN if recovery else ORIGIN
        username = "recovery" if recovery else "admin"
        password = self.environment[
            (
                "OPENBRICKS_RECOVERY_BOOTSTRAP_PASSWORD"
                if recovery
                else "OPENBRICKS_SMOKE_PASSWORD"
            )
        ]
        with httpx.Client(base_url=origin, timeout=8) as client:
            client.post(
                "/api/auth/login", json={"username": username, "password": password}
            ).raise_for_status()
            while time.monotonic() < deadline:
                try:
                    response = client.get("/api/catalog/status")
                    if response.is_success and response.json().get("available"):
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(1)
            else:
                raise SmokeError("Disposable Unity Catalog did not become ready")
        storage = "recovery-storage" if recovery else "seaweedfs"
        compute = "recovery-compute" if recovery else "compute"
        code = f"import socket,time\nfor attempt in range(120):\n try:\n  s=socket.create_connection(({storage!r},8333),timeout=2);s.close();break\n except OSError: time.sleep(1)\nelse: raise SystemExit(1)"
        self.command(
            self.compose + ["exec", "-T", compute, "python", "-c", code],
            "wait for disposable " + storage,
            timeout=180,
        )

    def proof(self, script, *arguments, timeout=1200, monitor=None):
        self.command(
            [sys.executable, str(ROOT / "scripts" / script), *map(str, arguments)],
            script.removesuffix(".py")
            + " "
            + " ".join(
                str(value)
                for value in arguments
                if str(value).startswith("--") is False
                and not str(value).endswith(".json")
            ),
            timeout=timeout,
            monitor=monitor,
        )

    def platform(self, section, *, monitor=None):
        self.proof(
            "stable-platform-smoke.py",
            "--section",
            section,
            "--receipt",
            self.output / "platform.json",
            monitor=monitor,
        )

    def sql(self):
        fixture = self.output / "sql-private-fixture.json"
        self.stop("catalog", "seaweedfs", "receiver")
        self.up("sqlserver")
        self.command(
            self.compose
            + [
                "cp",
                str(ROOT / "scripts/stable-sql-smoke.py"),
                "compute:/tmp/stable-sql-smoke.py",
            ],
            "stage SQL proof",
            timeout=60,
        )
        script = [
            "exec",
            "-T",
            "-e",
            "PYTHONPATH=/app/backend",
            "compute",
            "python",
            "/tmp/stable-sql-smoke.py",
        ]
        credentials = json.dumps(
            {
                "server": "sqlserver",
                "admin_password": self.environment["OPENBRICKS_SQLSERVER_PASSWORD"],
            }
        )
        try:
            self.command(
                self.compose
                + script
                + [
                    "--phase",
                    "bootstrap",
                    "--bootstrap-stdin",
                    "--fixture",
                    "/tmp/stable-sql-fixture.json",
                ],
                "create marked SQL fixtures",
                timeout=180,
                stdin=credentials,
            )
            self.command(
                self.compose
                + ["cp", "compute:/tmp/stable-sql-fixture.json", str(fixture)],
                "retrieve private SQL fixture",
                timeout=60,
            )
            fixture.chmod(0o600)
            self.proof(
                "stable-sql-smoke.py",
                "--phase",
                "http",
                "--section",
                "all",
                "--fixture",
                fixture,
                "--receipt",
                self.output / "sql.json",
                timeout=3600,
            )
        finally:
            if fixture.exists():
                self.command(
                    self.compose
                    + script
                    + [
                        "--phase",
                        "cleanup",
                        "--bootstrap-stdin",
                        "--fixture",
                        "/tmp/stable-sql-fixture.json",
                    ],
                    "remove marked SQL fixtures",
                    timeout=180,
                    stdin=credentials,
                )
        # Run the driver-level failure-after-MERGE proof with its own worker,
        # rather than placing another Spark JVM beside the management worker.
        self.stop("control", "compute")
        options = {
            "OPENBRICKS_TEST_SQLSERVER": "1",
            "OPENBRICKS_SQLSERVER_HOST": "sqlserver",
            "OPENBRICKS_SQLSERVER_USERNAME": "sa",
            "OPENBRICKS_SQLSERVER_PASSWORD": self.environment[
                "OPENBRICKS_SQLSERVER_PASSWORD"
            ],
            "SPARK_MASTER": "local[1]",
            "SPARK_DRIVER_MEMORY": "1g",
        }
        code = "import json,os,sys;os.environ.update(json.load(sys.stdin));import pytest;raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','tests/test_sqlserver_live.py','tests/test_sqlserver_stable.py']))"
        self.command(
            self.compose
            + [
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "python",
                "compute",
                "-c",
                code,
            ],
            "real SQL driver and post-MERGE checkpoint failure tests",
            timeout=1800,
            stdin=json.dumps(options),
        )
        self.up("compute", "control")
        self.wait_http()
        self.stop("sqlserver")
        self.up("seaweedfs", "catalog", "receiver")
        self.wait_integrations()

    def notifications(self):
        baseline = {
            item.get("payload", {}).get("delivery_id")
            for item in self.get_json(RECEIVER + "/events")["items"]
        }
        state = json.loads((self.output / "platform.json").read_text(encoding="utf-8"))
        started = time.time()
        audit = {}

        def restart_after_rejection():
            if audit:
                return
            try:
                events = self.get_json(RECEIVER + "/events")["items"]
                pending = json.loads(
                    (self.output / "platform.json").read_text(encoding="utf-8")
                ).get("notification_pending_run_id")
            except (OSError, URLError, ValueError):
                return
            rejection = rejected_delivery(
                events, baseline, state["project_id"], started, pending
            )
            if rejection is None:
                return
            delivery = rejection["payload"]["delivery_id"]
            if any(
                item.get("accepted")
                and item.get("payload", {}).get("delivery_id") == delivery
                for item in events
            ):
                raise SmokeError(
                    "Webhook retry happened before the management restart could be tested"
                )
            audit.update(
                delivery_id=delivery,
                run_id=rejection["payload"]["run_id"],
                rejected_at=rejection["received_at"],
            )
            self.restart("control")
            container = self.capture(
                self.compose + ["ps", "--all", "--quiet", "control"],
                "find disposable management",
            ).strip()
            timestamp = self.capture(
                ["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
                "inspect disposable management restart",
            ).strip()
            audit["management_started_at"] = instant(timestamp)
            self.wait_http()

        self.platform("notifications", monitor=restart_after_rejection)
        if not audit:
            raise SmokeError(
                "No rejected webhook was observed; restart durability was not proved"
            )
        events = self.get_json(RECEIVER + "/events")["items"]
        accepted = [
            item
            for item in events
            if item.get("accepted")
            and item.get("channel") == "webhook"
            and item.get("payload", {}).get("delivery_id") == audit["delivery_id"]
        ]
        if (
            len(accepted) != 1
            or not audit["rejected_at"]
            < audit["management_started_at"]
            < accepted[0]["received_at"]
        ):
            raise SmokeError(
                "Webhook acceptance did not occur strictly across the management restart"
            )
        audit["accepted_at"] = accepted[0]["received_at"]
        audit["passed"] = True
        (self.output / "notification-restart.json").write_text(
            json.dumps(audit, indent=2), encoding="utf-8"
        )

    def execute(self):
        self.prepare()
        self.up("compute", "control", "seaweedfs", "catalog", "receiver")
        self.wait_http()
        self.wait_integrations()
        runtime_receipt = self.output / "runtime.json"
        self.proof(
            "v4-runtime-smoke.py",
            "--section",
            "all",
            "--receipt",
            runtime_receipt,
            timeout=1800,
        )
        self.sql()
        self.platform("core")
        self.proof("v4-git-smoke.py", "--receipt", self.output / "git.json")
        self.proof(
            "v4-automation-smoke.py",
            "--section",
            "all",
            "--receipt",
            self.output / "automation.json",
            timeout=1800,
        )
        self.proof("v4-delivery-smoke.py", "--receipt", self.output / "delivery.json")
        self.up("remote")
        self.platform("remote")
        self.platform("catalog")
        self.stop("remote")
        self.platform("resources")
        self.notifications()
        self.platform("schedule-prepare")
        self.restart("control")
        self.wait_http()
        self.platform("schedule-verify")
        self.platform("compute-restart-prepare")
        self.restart("compute")
        self.wait_compute()
        self.wait_http()
        self.platform("compute-restart-verify")
        self.proof(
            "v5-compute-smoke.py",
            "--section",
            "profile",
            "--receipt",
            self.output / "v5-compute.json",
            timeout=1800,
        )
        self.proof(
            "v5-compute-smoke.py",
            "--section",
            "hive",
            "--receipt",
            self.output / "v5-compute.json",
            timeout=1800,
        )
        self.up("remote")
        self.proof(
            "v5-compute-smoke.py",
            "--section",
            "unity",
            "--receipt",
            self.output / "v5-compute.json",
            timeout=1800,
        )
        self.stop("remote")
        self.proof(
            "v5-features-smoke.py",
            "--phase",
            "prepare",
            "--receipt",
            self.output / "v5-features.json",
            timeout=1200,
        )
        self.command(
            [
                shutil.which("node"),
                str(ROOT / "web/node_modules/@playwright/test/cli.js"),
                "test",
                "--config",
                str(ROOT / "web/playwright.config.ts"),
            ],
            "v5 Chromium end-to-end UI and OIDC proof",
            timeout=1800,
        )
        # Prepare the source archive before bringing up a second management
        # host. SQL and the remote worker are already stopped to cap peak RAM.
        self.proof(
            "v4-recovery-smoke.py",
            "--phase",
            "prepare",
            "--receipt",
            runtime_receipt,
            timeout=1800,
        )
        self.stop("control", "compute", "receiver")
        self.up(
            "recovery-compute",
            "recovery-control",
            "recovery-storage",
            "recovery-catalog",
        )
        self.wait_http(FRESH_ORIGIN)
        self.wait_integrations(recovery=True)
        self.proof(
            "v4-recovery-smoke.py",
            "--phase",
            "restore",
            "--receipt",
            runtime_receipt,
            timeout=1200,
        )
        self.restart("recovery-compute", "recovery-control")
        self.wait_compute("recovery-compute")
        self.wait_http(FRESH_ORIGIN)
        self.proof(
            "v4-recovery-smoke.py",
            "--phase",
            "recover",
            "--receipt",
            runtime_receipt,
            timeout=1200,
        )
        proof = json.loads(runtime_receipt.read_text(encoding="utf-8"))
        if not proof.get("recovery_proof", {}).get("passed"):
            raise SmokeError("Fresh-host recovery did not produce a passing receipt")
        self.proof(
            "v5-features-smoke.py",
            "--phase",
            "recover",
            "--receipt",
            self.output / "v5-features.json",
            timeout=1200,
        )
        self.summary["proofs_passed"] = True

    def cleanup(self):
        if not self.ready_for_cleanup:
            return True
        try:
            self.command(
                self.compose + ["logs", "--no-color", "--timestamps"],
                "capture private disposable service logs",
                timeout=90,
            )
        except Exception:
            pass
        try:
            self.command(
                self.compose
                + ["down", "--volumes", "--remove-orphans", "--timeout", "30"],
                "remove disposable smoke resources",
                timeout=240,
            )
            self.summary["cleanup_passed"] = True
            return True
        except Exception:
            self.summary["cleanup_passed"] = False
            print(
                "FAIL: disposable resource cleanup; inspect the private cleanup log",
                file=sys.stderr,
            )
            return False
        finally:
            self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--runtime-image", help="Use a prebuilt runtime image; requires --compute-image"
    )
    parser.add_argument(
        "--compute-image", help="Use a prebuilt compute image; requires --runtime-image"
    )
    parser.add_argument("--project", default=PROJECT, help=argparse.SUPPRESS)
    parser.add_argument("--url", default=ORIGIN, help=argparse.SUPPRESS)
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print all phases without Docker, credentials or files",
    )
    args = parser.parse_args(argv)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    runner = None
    success = False
    try:
        runner = Runner(args)
        if args.plan:
            print("Disposable project: " + PROJECT)
            for number, phase in enumerate(PHASES, 1):
                print(f"{number}. {phase}")
            print("Always remove this project's containers and volumes on exit.")
            return 0
        runner.execute()
        success = True
    except KeyboardInterrupt:
        print("FAIL: interrupted; removing disposable smoke resources", file=sys.stderr)
    except Exception as exc:
        print(
            "FAIL: "
            + (str(exc) if isinstance(exc, SmokeError) else type(exc).__name__)
            + "; detailed output stays private",
            file=sys.stderr,
        )
    finally:
        if runner and not args.plan:
            cleaned = runner.cleanup()
            success = success and cleaned
            if runner.output.exists():
                runner.summary["passed"] = success
                runner.save()
                print("Private test artifacts: " + str(runner.output), flush=True)
        signal.signal(signal.SIGTERM, previous_handler)
    if success:
        print(
            "PASS: complete system smoke, real restarts and fresh-host recovery",
            flush=True,
        )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
