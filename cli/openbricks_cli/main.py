"""Pack, validate, deploy, promote, and run projects without interactive passwords."""

from __future__ import annotations

import argparse
import copy
from datetime import date, datetime
import json
import os
from pathlib import Path, PurePosixPath
import sys
import time
from urllib.parse import urlsplit

import httpx
import yaml

from . import __version__


class ClientError(Exception):
    pass


def compatible_json(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): compatible_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [compatible_json(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ClientError(
        "Bundle values must be JSON-compatible objects, arrays, numbers, dates, or text"
    )


def pack_bundle(filename, target=None, project_id=None):
    path = Path(filename).resolve()
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ClientError("Choose a bundle file smaller than 20 MB")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ClientError("The bundle is not valid UTF-8 YAML or JSON") from exc
    if not isinstance(value, dict) or value.get("format_version") != 1:
        raise ClientError("Use an OpenBricks bundle with format_version: 1")
    value = compatible_json(copy.deepcopy(value))
    if project_id:
        if not target or target not in value.get("targets", {}):
            raise ClientError("Choose a declared target before overriding its project")
        value["targets"][target]["project_id"] = project_id
    notebooks = value.get("notebooks", [])
    if not isinstance(notebooks, list) or not 1 <= len(notebooks) <= 500:
        raise ClientError("Declare between 1 and 500 notebooks")
    root = path.parent
    total = 0
    for notebook in notebooks:
        if not isinstance(notebook, dict):
            raise ClientError("Notebook declarations must be objects")
        if "source" in notebook or "cells" in notebook:
            continue
        relative = notebook.get("path", "")
        if (
            not isinstance(relative, str)
            or "\\" in relative
            or PurePosixPath(relative).is_absolute()
            or any(part in ("", ".", "..") for part in relative.split("/"))
        ):
            raise ClientError("Notebook paths must be relative to the bundle directory")
        if PurePosixPath(relative).suffix.lower() not in (".py", ".sql", ".ipynb"):
            raise ClientError("Bundle notebook paths must use .py, .sql, or .ipynb")
        source = (root / relative).resolve()
        if (
            not source.is_relative_to(root)
            or not source.is_file()
            or source.stat().st_size > 10 * 1024 * 1024
        ):
            raise ClientError(
                "Notebook file is missing, outside the bundle directory, or larger than 10 MB"
            )
        try:
            notebook["source"] = source.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise ClientError("Notebook files must contain UTF-8 text") from exc
        total += len(notebook["source"].encode())
        if total > 20 * 1024 * 1024:
            raise ClientError("The bundle source exceeds 20 MB")
    try:
        serialized = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ClientError("Bundle values must be finite JSON-compatible data") from exc
    if len(serialized.encode()) > 20 * 1024 * 1024:
        raise ClientError("The packed bundle exceeds 20 MB")
    return value


class Client:
    def __init__(self, url, token, allow_http=False):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ClientError(
                "Use a workspace HTTP(S) URL without credentials, query, or fragment"
            )
        if (
            parsed.scheme == "http"
            and parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            and not allow_http
        ):
            raise ClientError(
                "Use HTTPS remotely, or explicitly allow HTTP for a trusted local network"
            )
        if not token:
            raise ClientError("Set OPENBRICKS_TOKEN to a scoped service-account token")
        self.client = httpx.Client(
            base_url=url.rstrip("/") + "/api/",
            headers={"Authorization": "Bearer " + token},
            timeout=120,
            follow_redirects=False,
        )

    def request(self, method, path, **options):
        try:
            response = self.client.request(method, path.lstrip("/"), **options)
        except httpx.HTTPError as exc:
            raise ClientError(
                "Cannot reach the workspace. Check its URL and network connection"
            ) from exc
        if not 200 <= response.status_code < 300:
            try:
                payload = response.json()
                detail = (
                    payload.get("detail", "Request failed")
                    if isinstance(payload, dict)
                    else "The workspace rejected the request"
                )
                if isinstance(detail, dict):
                    detail = detail.get("message", "Request failed")
                if not isinstance(detail, str):
                    detail = "The workspace rejected the request"
            except ValueError:
                detail = "The workspace rejected the request"
            raise ClientError(f"HTTP {response.status_code}: {detail[:500]}")
        return response.json()

    def close(self):
        self.client.close()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument(
        "--url", default=os.getenv("OPENBRICKS_URL", "http://localhost:3000")
    )
    result.add_argument(
        "--allow-http",
        action="store_true",
        help="Allow unencrypted HTTP to a trusted remote endpoint",
    )
    commands = result.add_subparsers(dest="command", required=True)
    pack = commands.add_parser(
        "pack",
        help="Pack notebook files into a portable JSON bundle without contacting a server",
    )
    pack.add_argument("bundle")
    pack.add_argument("--output", "-o")
    for name in ("validate", "plan", "deploy"):
        command = commands.add_parser(name)
        command.add_argument("bundle")
        command.add_argument("--target", required=True)
        command.add_argument(
            "--project", help="Override only the selected target's project ID"
        )
    promote = commands.add_parser("promote")
    promote.add_argument("deployment_id")
    promote.add_argument("--target", required=True)
    promote.add_argument("--plan-only", action="store_true")
    run = commands.add_parser("run")
    selector = run.add_mutually_exclusive_group(required=True)
    selector.add_argument("--notebook")
    selector.add_argument("--workflow")
    selector.add_argument("--job")
    run.add_argument(
        "--parameters", default="{}", help="JSON object of typed parameter overrides"
    )
    run.add_argument("--wait", action="store_true")
    run.add_argument("--timeout", type=int, default=3600)
    status = commands.add_parser("status")
    status.add_argument("run_id")
    status.add_argument("--workflow", action="store_true")
    status.add_argument("--wait", action="store_true")
    status.add_argument("--timeout", type=int, default=3600)
    environment = commands.add_parser("capture-environment")
    environment.add_argument("--project", required=True)
    environment.add_argument("--name", default="CLI environment")
    upload = commands.add_parser("upload-wheel")
    upload.add_argument("file")
    upload.add_argument("--project", required=True)
    return result


def summary(value):
    return {
        key: value.get(key)
        for key in (
            "id",
            "name",
            "notebook_name",
            "status",
            "project_id",
            "progress",
            "error",
            "error_class",
            "duration_ms",
            "created_at",
            "started_at",
            "finished_at",
            "environment_id",
        )
        if key in value
    }


def wait_run(client, value, workflow, timeout):
    deadline = time.monotonic() + max(1, min(timeout, 86400))
    while value.get("status") in ("queued", "running", "pending"):
        if time.monotonic() >= deadline:
            raise ClientError(
                "Wait timed out; the run remains on the server. Use status to inspect its outcome"
            )
        time.sleep(1)
        value = client.request(
            "GET", ("workflow-runs/" if workflow else "runs/") + value["id"]
        )
    return value


def execute(args):
    if args.command == "pack":
        value = pack_bundle(args.bundle)
        if args.output:
            output = Path(args.output)
            if output.exists():
                raise ClientError(
                    "Output already exists; choose another file to preserve the current one"
                )
            output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            return {"output": str(output), "notebooks": len(value["notebooks"])}, 0
        return value, 0
    client = Client(args.url, os.getenv("OPENBRICKS_TOKEN", ""), args.allow_http)
    try:
        if args.command in ("validate", "plan", "deploy"):
            bundle = pack_bundle(args.bundle, args.target, args.project)
            request = {"bundle": bundle, "target": args.target}
            plan = client.request("POST", "deployments/plan", json=request)
            if args.command != "deploy":
                return plan, 0
            return (
                client.request(
                    "POST",
                    "deployments",
                    json={**request, "expected_plan_hash": plan["plan_hash"]},
                ),
                0,
            )
        if args.command == "promote":
            plan = client.request(
                "POST",
                f"deployments/{args.deployment_id}/promotion-plan",
                json={"target": args.target},
            )
            if args.plan_only:
                return plan, 0
            return (
                client.request(
                    "POST",
                    f"deployments/{args.deployment_id}/promote",
                    json={
                        "target": args.target,
                        "expected_plan_hash": plan["plan_hash"],
                    },
                ),
                0,
            )
        if args.command == "capture-environment":
            return (
                client.request(
                    "POST",
                    "environments",
                    json={"project_id": args.project, "name": args.name},
                ),
                0,
            )
        if args.command == "upload-wheel":
            path = Path(args.file)
            if not path.is_file() or path.suffix.lower() != ".whl":
                raise ClientError("Choose a Python wheel file")
            with path.open("rb") as source:
                return (
                    client.request(
                        "POST",
                        "packages/upload",
                        data={"project_id": args.project},
                        files={"file": (path.name, source, "application/octet-stream")},
                    ),
                    0,
                )
        if args.command == "run":
            try:
                parameters = json.loads(args.parameters)
            except ValueError as exc:
                raise ClientError("Run parameters must be a JSON object") from exc
            if not isinstance(parameters, dict):
                raise ClientError("Run parameters must be a JSON object")
            kind, identifier = (
                ("notebooks", args.notebook)
                if args.notebook
                else (
                    ("workflows", args.workflow)
                    if args.workflow
                    else ("jobs", args.job)
                )
            )
            value = client.request(
                "POST", f"{kind}/{identifier}/run", json={"parameters": parameters}
            )
            workflow = bool(args.workflow or value.get("workflow_id"))
        else:
            workflow = args.workflow
            value = client.request(
                "GET", ("workflow-runs/" if workflow else "runs/") + args.run_id
            )
        if args.wait:
            value = wait_run(client, value, workflow, args.timeout)
        return summary(value), (
            1 if value.get("status") in ("failed", "cancelled") else 0
        )
    finally:
        client.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        value, status = execute(args)
        output = json.dumps(value, indent=2, allow_nan=False)
        token = os.getenv("OPENBRICKS_TOKEN", "")
        if token and args.command != "pack":
            output = output.replace(token, "[redacted]")
        print(output)
        return status
    except (ClientError, OSError) as exc:
        message = str(exc)
        token = os.getenv("OPENBRICKS_TOKEN", "")
        if token:
            message = message.replace(token, "[redacted]")
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
