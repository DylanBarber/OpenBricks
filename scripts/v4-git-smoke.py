"""Real Git authoring API proof against disposable 127.0.0.1:3001 only.

Uses an environment-only QA password and clones the public OpenBricks repository.
Creates a local QA branch and local commit. It never pushes, opens pull requests,
or configures remote credentials. One final uncommitted file is intentionally
left for browser review. The ignored receipt contains synthetic fixture IDs.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPOSITORY = "https://github.com/DylanBarber/OpenBricks.git"


class ProofError(RuntimeError):
    pass


def check(condition, message):
    if not condition:
        raise ProofError(message)


class Proof:
    def __init__(self, url, receipt):
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
        self.client = httpx.Client(
            base_url=url.rstrip("/"), timeout=180, follow_redirects=False
        )
        self.output = Path(receipt)
        self.suffix = uuid4().hex[:10]
        self.state = {
            "checks": [],
            "project_id": None,
            "repository_id": None,
            "notebook_id": None,
            "path": None,
            "branch": "qa/v4-git-" + self.suffix,
            "pending_ui_marker": "QA pending UI review " + self.suffix,
        }

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def passed(self, name):
        self.state["checks"].append(name)
        self.save()

    def request(self, method, path, *, expected=None, **options):
        # An allowlist prevents this proof from gaining an external-write action.
        check(
            not path.endswith(
                ("/push", "/pull-request", "/github-credential", "/pull")
            ),
            "Remote Git writes are forbidden by this proof",
        )
        response = self.client.request(method, "/api" + path, **options)
        check(
            (
                response.is_success
                if expected is None
                else response.status_code == expected
            ),
            f"{method} {path} returned HTTP {response.status_code}",
        )
        return response.json() if response.content else None

    def notebook(self):
        return self.request("GET", "/notebooks/" + self.state["notebook_id"])

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
        self.request(
            "POST",
            "/auth/login",
            json={
                "username": os.getenv("OPENBRICKS_SMOKE_USERNAME", "admin"),
                "password": password,
            },
        )
        check(
            self.request("GET", "/auth/me").get("role") == "admin",
            "A disposable administrator is required",
        )
        project = self.request(
            "POST",
            "/projects",
            json={"name": "Git QA " + self.suffix, "cluster_ids": ["local"]},
        )
        self.state["project_id"] = project["id"]
        self.save()
        # A failed clone is reported once; this proof has no fixture fallback.
        repository = self.request(
            "POST",
            "/repositories",
            json={
                "name": "OpenBricks Git QA " + self.suffix,
                "url": PUBLIC_REPOSITORY,
                "branch": "main",
                "project_id": project["id"],
            },
        )
        self.state["repository_id"] = repository["id"]
        prefix = "/repositories/" + repository["id"]
        imported = self.request("POST", prefix + "/import", json={})
        check(bool(imported), "The public repository contained no supported notebooks")
        priorities = [
            "examples/deployable-project/notebooks/10_prepare.py",
            "examples/notebook-repo/notebooks/bronze/10_ingest_orders.py",
            "examples/hello_spark.py",
        ]
        selected = next(
            (
                item
                for path in priorities
                for item in imported
                if item.get("path") == path
            ),
            None,
        )
        selected = selected or next(
            (item for item in imported if str(item.get("path", "")).endswith(".py")),
            None,
        )
        check(
            selected is not None,
            "The public repository contained no Python notebook fixture",
        )
        self.state["notebook_id"], self.state["path"] = selected["id"], selected["path"]
        self.passed("public-clone-and-notebook-import-through-ui-proxy")
        branch = self.request(
            "POST", prefix + "/git/branch", json={"name": self.state["branch"]}
        )
        check(branch["branch"] == self.state["branch"], "Local branch creation failed")
        original = self.notebook()
        edited = copy.deepcopy(original)
        cell = next(
            (
                item
                for item in reversed(edited["cells"])
                if item["language"] == "python"
            ),
            None,
        )
        check(cell is not None, "Selected notebook has no Python cell")
        conflict_marker = "QA protected workspace source " + self.suffix
        cell["source"] += "\n# " + conflict_marker + "\n"
        edited = self.request("PUT", "/notebooks/" + edited["id"], json=edited)
        rejected = self.request("POST", prefix + "/import", json={}, expected=409)
        conflicts = rejected.get("detail", {}).get("conflicts", [])
        conflict = next(
            (item for item in conflicts if item["id"] == edited["id"]), None
        )
        check(
            conflict is not None and conflict["version"] == edited["version"],
            "Conflict did not identify the reviewed notebook version",
        )
        check(
            conflict_marker in json.dumps(self.notebook()["cells"]),
            "Rejected import overwrote workspace source",
        )
        self.request(
            "POST",
            prefix + "/import",
            json={
                "overwrite": True,
                "expected_versions": {
                    item["id"]: item["version"] for item in conflicts
                },
            },
        )
        restored = self.notebook()
        check(
            conflict_marker not in json.dumps(restored["cells"]),
            "Reviewed import did not restore repository source",
        )
        self.passed("workspace-conflict-preservation-and-version-guarded-replacement")
        path = self.state["path"]
        opened = self.request("GET", prefix + "/git/file", params={"path": path})
        file_marker = "QA compare-and-swap file edit " + self.suffix
        saved = self.request(
            "PUT",
            prefix + "/git/file",
            json={
                "path": path,
                "source": opened["source"] + "\n# " + file_marker + "\n",
                "expected_sha256": opened["sha256"],
            },
        )
        self.request(
            "PUT",
            prefix + "/git/file",
            json={
                "path": path,
                "source": "stale edit must not win",
                "expected_sha256": opened["sha256"],
            },
            expected=409,
        )
        diff = self.request("GET", prefix + "/git/diff", params={"path": path})
        check(file_marker in diff["text"], "File diff did not contain the guarded edit")
        self.request(
            "PUT",
            prefix + "/git/file",
            json={
                "path": path,
                "source": opened["source"],
                "expected_sha256": saved["sha256"],
            },
        )
        self.passed("checkout-file-compare-and-swap-and-diff")
        export_marker = "QA exported workspace source " + self.suffix
        cell = next(
            item for item in reversed(restored["cells"]) if item["language"] == "python"
        )
        cell["source"] += "\n# " + export_marker + "\n"
        exported = self.request("PUT", "/notebooks/" + restored["id"], json=restored)
        staged = self.request(
            "POST",
            prefix + "/git/stage-notebooks",
            json={
                "notebook_ids": [exported["id"]],
                "versions": {exported["id"]: exported["version"]},
            },
        )
        check(
            any(item["path"] == path for item in staged["changes"]),
            "Notebook export was not staged",
        )
        committed = self.request(
            "POST",
            prefix + "/git/commit",
            json={
                "message": "Verify local OpenBricks Git authoring " + self.suffix,
                "paths": [path],
            },
        )
        check(not committed["changes"], "Local commit left unexpected checkout changes")
        check(
            not next(
                item
                for item in committed["workspace_changes"]
                if item["id"] == exported["id"]
            )["modified"],
            "Committed export did not update its source baseline",
        )
        self.state["commit"] = committed["commit"]
        self.passed("workspace-export-stage-and-local-commit")
        opened = self.request("GET", prefix + "/git/file", params={"path": path})
        self.request(
            "PUT",
            prefix + "/git/file",
            json={
                "path": path,
                "source": opened["source"]
                + "\n# "
                + self.state["pending_ui_marker"]
                + "\n",
                "expected_sha256": opened["sha256"],
            },
        )
        pending = self.request("GET", prefix + "/git/status")
        check(
            len(pending["changes"]) == 1 and pending["changes"][0]["path"] == path,
            "Expected exactly one pending browser-review file",
        )
        self.passed("one-uncommitted-file-left-for-browser-review")
        self.state["passed"] = True
        self.save()
        return self.state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:3001")
    parser.add_argument("--receipt", default=str(ROOT / ".test-data/v4-git-smoke.json"))
    args = parser.parse_args()
    proof = None
    try:
        proof = Proof(args.url, args.receipt)
        print(json.dumps(proof.run(), indent=2))
        return 0
    except Exception as exc:
        print(
            "Git QA failed: "
            + (str(exc) if isinstance(exc, ProofError) else type(exc).__name__),
            file=sys.stderr,
        )
        return 1
    finally:
        if proof:
            proof.save()
            proof.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
