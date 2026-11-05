"""Create sample notebooks/workflow in a disposable project; --run also queues it."""
from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
from pathlib import Path
import runpy
import urllib.error
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000", help="OpenBricks UI base URL")
    parser.add_argument("--username", default=os.getenv("OPENBRICKS_ADMIN_USERNAME", "admin"))
    parser.add_argument("--project", default="default", help="Existing project ID")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        parser.error("Use an HTTP(S) base URL without credentials, query, or fragment")
    example = Path(__file__).resolve().parent
    parse_notebook = runpy.run_path(str(example.parents[1] / "backend/openbricks/notebooks.py"))["parse_notebook"]
    workflow = json.loads((example / "workflow.json").read_text(encoding="utf-8"))
    workflow["project_id"] = args.project
    password = os.getenv("OPENBRICKS_ADMIN_PASSWORD") or getpass.getpass("OpenBricks password: ")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body):
        operation = urllib.request.Request(
            args.url.rstrip("/") + "/api" + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with opener.open(operation, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read()).get("detail", f"HTTP {error.code}")
            except (ValueError, AttributeError):
                detail = f"HTTP {error.code}"
            raise SystemExit(f"OpenBricks rejected {path}: {detail}") from None
        except urllib.error.URLError as error:
            raise SystemExit(f"Cannot reach OpenBricks: {error.reason}") from None

    request("/auth/login", {"username": args.username, "password": password})
    created = {}
    for key, filename in (("bronze", "10_bronze.py"), ("silver", "20_silver.py")):
        notebook = parse_notebook(filename, (example / "notebooks" / filename).read_bytes())
        notebook.update(name=f"Workflow demo - {key.title()}", project_id=args.project,
                        cluster_id="local", connection_ids=[],
                        parameter_definitions=workflow["parameter_definitions"])
        created[key] = request("/notebooks", notebook)["id"]
    for task in workflow["tasks"]:
        task["notebook_id"] = created[task["key"]]
    saved = request("/workflows", workflow)
    result = {"workflow_id": saved["id"], "notebook_ids": created}
    if args.run:
        result["workflow_run_id"] = request(f"/workflows/{saved['id']}/run", {"parameters": {}})["id"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
