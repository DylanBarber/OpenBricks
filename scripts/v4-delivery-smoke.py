"""Exercise real CLI/API delivery through the disposable UI proxy on 127.0.0.1:3001.

Requires OPENBRICKS_SMOKE_PASSWORD (or OPENBRICKS_ADMIN_PASSWORD / PASSWORD).
Creates synthetic fixtures, retains them for inspection, and revokes every issued
token before exit. Credentials are kept in memory only. No Docker or external Git
operations are performed. Run with the host Python environment that includes the
CLI dependencies: python scripts/v4-delivery-smoke.py --url http://127.0.0.1:3001
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cli"))
from openbricks_cli import main as cli


class ProofError(RuntimeError):
    pass


def check(condition, message):
    if not condition:
        raise ProofError(message)


class Proof:
    def __init__(self, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 3001
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ProofError(
                "Only the disposable UI origin at 127.0.0.1:3001 is accepted"
            )
        self.url = url.rstrip("/")
        self.admin = httpx.Client(
            base_url=self.url, timeout=120, follow_redirects=False
        )
        self.bearer = httpx.Client(
            base_url=self.url, timeout=120, follow_redirects=False
        )
        self.suffix = uuid4().hex[:10]
        self.tokens = []
        self.account = None
        self.receipt = {
            "checks": [],
            "project_ids": ["default"],
            "deployment_ids": [],
            "run_ids": [],
        }

    def request(
        self, method, path, *, token=None, expected=None, admin=False, **options
    ):
        client = self.admin if admin else self.bearer
        if token:
            options["headers"] = {"Authorization": "Bearer " + token}
        response = client.request(method, "/api" + path, **options)
        wanted = (
            response.is_success
            if expected is None
            else response.status_code == expected
        )
        check(wanted, f"{method} {path} returned HTTP {response.status_code}")
        return response.json() if response.content else None

    def command(self, arguments, token=None, expected=0):
        previous = os.environ.get("OPENBRICKS_TOKEN")
        if token:
            os.environ["OPENBRICKS_TOKEN"] = token
        else:
            os.environ.pop("OPENBRICKS_TOKEN", None)
        output, errors = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(errors):
                status = cli.main(["--url", self.url, *arguments])
            check(
                status == expected, f"CLI {arguments[0]} returned exit status {status}"
            )
            for _, credential in self.tokens:
                check(
                    credential not in output.getvalue() + errors.getvalue(),
                    "CLI output disclosed a credential",
                )
            return json.loads(output.getvalue()) if status == 0 else None
        finally:
            if previous is None:
                os.environ.pop("OPENBRICKS_TOKEN", None)
            else:
                os.environ["OPENBRICKS_TOKEN"] = previous

    def issue(self, name, projects, actions):
        value = self.request(
            "POST",
            f"/service-accounts/{self.account['id']}/tokens",
            admin=True,
            json={
                "name": name,
                "project_ids": projects,
                "actions": actions,
                "expires_in_days": 1,
            },
        )
        self.tokens.append((value["id"], value["token"]))
        return value["token"]

    def passed(self, name):
        self.receipt["checks"].append(name)

    def run(self):
        password = (
            os.getenv("OPENBRICKS_SMOKE_PASSWORD")
            or os.getenv("OPENBRICKS_ADMIN_PASSWORD")
            or os.getenv("PASSWORD")
        )
        check(
            bool(password),
            "Set the disposable QA password in OPENBRICKS_SMOKE_PASSWORD",
        )
        check(self.admin.get("/").status_code == 200, "Disposable UI is unavailable")
        self.request(
            "POST",
            "/auth/login",
            admin=True,
            json={
                "username": os.getenv("OPENBRICKS_SMOKE_USERNAME", "admin"),
                "password": password,
            },
        )
        check(
            self.request("GET", "/auth/me", admin=True).get("role") == "admin",
            "A disposable administrator is required",
        )
        project = self.request(
            "POST",
            "/projects",
            admin=True,
            json={"name": "Delivery QA " + self.suffix, "cluster_ids": ["local"]},
        )
        self.receipt["project_ids"].append(project["id"])
        self.account = self.request(
            "POST",
            "/service-accounts",
            admin=True,
            json={
                "name": "delivery-qa-" + self.suffix,
                "grants": [
                    {"project_id": identifier, "role": "editor"}
                    for identifier in self.receipt["project_ids"]
                ],
            },
        )
        self.receipt["service_account_id"] = self.account["id"]
        read_token = self.issue("Read default only", ["default"], ["read"])
        token = self.issue("Deploy QA targets", self.receipt["project_ids"], ["edit"])
        packed = self.command(
            ["pack", str(ROOT / "examples/deployable-project/openbricks.yml")]
        )
        packed["name"] = "delivery-qa-" + self.suffix
        packed["targets"]["prod"]["project_id"] = project["id"]
        self.request("GET", "/notebooks?project_id=default", token=read_token)
        self.request(
            "GET",
            "/notebooks?project_id=" + project["id"],
            token=read_token,
            expected=403,
        )
        self.request(
            "POST",
            "/deployments/plan",
            token=read_token,
            expected=403,
            json={"bundle": packed, "target": "dev"},
        )
        self.request(
            "GET", "/auth/me", token="ob_invalid_fixture", admin=True, expected=401
        )
        self.passed("scoped-token-project-and-action-enforcement")
        with tempfile.TemporaryDirectory(prefix="openbricks-delivery-qa-") as temporary:
            bundle_path = Path(temporary) / "bundle.json"
            bundle_path.write_text(json.dumps(packed), encoding="utf-8")
            preview = self.command(["plan", str(bundle_path), "--target", "dev"], token)
            check(
                preview["counts"] == {"notebooks": 2, "workflows": 1, "jobs": 1},
                "Example plan has unexpected resource counts",
            )
            deployment = self.command(
                ["deploy", str(bundle_path), "--target", "dev"], token
            )
            self.receipt["deployment_ids"].append(deployment["id"])
            self.passed("cli-pack-plan-and-deploy-through-ui-proxy")
            identifiers = {
                (item["kind"], item["key"]): item["id"]
                for item in deployment["resources"]
            }
            prepare = identifiers[("notebooks", "prepare")]
            run = self.command(
                [
                    "run",
                    "--notebook",
                    prepare,
                    "--parameters",
                    '{"run_date":"2026-11-05"}',
                    "--wait",
                    "--timeout",
                    "180",
                ],
                token,
            )
            check(run.get("status") == "succeeded", "Deployed notebook did not succeed")
            direct_run = self.request("GET", "/runs/" + run["id"], token=token)
            check(
                direct_run.get("owner_id") == self.account["id"],
                "Run owner did not match the service account",
            )
            self.receipt["run_ids"].append(run["id"])
            self.passed("deployed-notebook-runs-with-service-account-owner")
            notebook = self.request("GET", "/notebooks/" + prepare, token=token)
            notebook["cells"][0][
                "source"
            ] = "print('Synthetic workspace edit after captured deployment')"
            self.request("PUT", "/notebooks/" + prepare, token=token, json=notebook)
            promotion = self.command(
                ["promote", deployment["id"], "--target", "prod", "--plan-only"], token
            )
            check(
                promotion["project_id"] == project["id"],
                "Promotion plan used the wrong project",
            )
            promoted = self.command(
                ["promote", deployment["id"], "--target", "prod"], token
            )
            check(
                promoted["environment_id"] == deployment["environment_id"],
                "Promotion changed its pinned environment",
            )
            promoted_prepare = next(
                item["id"]
                for item in promoted["resources"]
                if item["kind"] == "notebooks" and item["key"] == "prepare"
            )
            promoted_nb = self.request(
                "GET", "/notebooks/" + promoted_prepare, token=token
            )
            check(
                "workspace edit after" not in json.dumps(promoted_nb["cells"]),
                "Promotion used later workspace edits",
            )
            self.receipt["deployment_ids"].append(promoted["id"])
            workflow = next(
                item["id"]
                for item in promoted["resources"]
                if item["kind"] == "workflows"
            )
            result = self.command(
                ["run", "--workflow", workflow, "--wait", "--timeout", "180"], token
            )
            check(
                result.get("status") == "succeeded", "Promoted workflow did not succeed"
            )
            workflow_run = self.request(
                "GET", "/workflow-runs/" + result["id"], token=token
            )
            self.receipt["run_ids"].append(result["id"])
            for task in workflow_run["tasks"]:
                child = self.request("GET", "/runs/" + task["run_id"], token=token)
                check(
                    child.get("owner_id") == self.account["id"],
                    "Workflow task used another resource owner",
                )
                check(
                    child.get("resource_options", {}).get("mode") == "job",
                    "Workflow task reused an interactive worker",
                )
                if task["key"] == "report":
                    check(
                        "Validated 4 orders and 96.50 revenue"
                        in json.dumps(child.get("outputs", [])),
                        "Report did not verify the example dataset",
                    )
            self.passed(
                "captured-source-and-environment-promotion-and-workflow-results"
            )
            saved_tokens = self.request(
                "GET", f"/service-accounts/{self.account['id']}/tokens", admin=True
            )
            check(
                all(
                    credential not in json.dumps(saved_tokens)
                    for _, credential in self.tokens
                ),
                "Token listing returned a plaintext credential",
            )
            self.request(
                "DELETE",
                f"/service-accounts/{self.account['id']}/tokens/{self.tokens[-1][0]}",
                admin=True,
            )
            self.request("GET", "/auth/me", token=token, expected=401)
            self.command(["status", result["id"], "--workflow"], token, expected=2)
            self.passed("revocation-blocks-cli-and-api-without-cookie-fallback")
        return self.receipt

    def close(self):
        if self.account:
            for identifier, _ in self.tokens:
                try:
                    self.request(
                        "DELETE",
                        f"/service-accounts/{self.account['id']}/tokens/{identifier}",
                        admin=True,
                    )
                except Exception:
                    pass
        self.admin.close()
        self.bearer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:3001")
    parser.add_argument(
        "--receipt", default=str(ROOT / ".test-data/v4-delivery-smoke.json")
    )
    args = parser.parse_args()
    proof = None
    try:
        proof = Proof(args.url)
        receipt = proof.run()
        output = Path(args.receipt)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(json.dumps(receipt, indent=2))
        return 0
    except Exception as exc:
        # Do not print HTTP bodies, CLI buffers, or credentials on failure.
        print(
            "Delivery QA failed: "
            + (str(exc) if isinstance(exc, ProofError) else type(exc).__name__),
            file=sys.stderr,
        )
        return 1
    finally:
        if proof:
            proof.close()


if __name__ == "__main__":
    raise SystemExit(main())
