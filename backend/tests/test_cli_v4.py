from pathlib import Path
import json
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))
from openbricks_cli import main as cli
from openbricks.main import create_app


def test_offline_packing_and_directory_escape_protection(tmp_path):
    (tmp_path / "notebooks").mkdir()
    (tmp_path / "notebooks" / "one.py").write_text(
        "# Databricks notebook source\nprint(42)\n"
    )
    filename = tmp_path / "openbricks.yml"
    filename.write_text(
        "format_version: 1\nname: cli-fixture\ntargets:\n  dev:\n    project_id: default\n    variables:\n      run_date: 2026-11-05\nnotebooks:\n  - key: one\n    path: notebooks/one.py\n"
    )
    value = cli.pack_bundle(filename)
    assert "print(42)" in value["notebooks"][0]["source"]
    assert value["targets"]["dev"]["variables"]["run_date"] == "2026-11-05"
    filename.write_text(
        "format_version: 1\nnotebooks:\n  - key: one\n    path: ../outside.py\n"
    )
    with pytest.raises(cli.ClientError):
        cli.pack_bundle(filename)


def test_cli_real_token_deploy_and_status_without_network(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "cli-test-workspace-password")
    for name in (
        "OPENBRICKS_COMPUTE_URL",
        "OPENBRICKS_CATALOG_URL",
        "OPENBRICKS_S3_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    app = create_app(tmp_path / "data")
    with TestClient(app) as server:
        server.post(
            "/api/auth/login",
            json={"username": "admin", "password": "cli-test-workspace-password"},
        ).raise_for_status()
        account = server.post(
            "/api/service-accounts",
            json={
                "name": "cli-bot",
                "grants": [{"project_id": "default", "role": "editor"}],
            },
        ).json()
        token = server.post(
            f"/api/service-accounts/{account['id']}/tokens",
            json={"name": "CLI", "project_ids": ["default"], "actions": ["edit"]},
        ).json()["token"]
        monkeypatch.setenv("OPENBRICKS_TOKEN", token)
        original = httpx.Client
        requests = []

        def handle(request):
            requests.append(request)
            response = server.request(
                request.method,
                request.url.path,
                content=request.content,
                headers={
                    "Authorization": request.headers["Authorization"],
                    "Content-Type": request.headers.get(
                        "Content-Type", "application/json"
                    ),
                },
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers={"Content-Type": "application/json"},
            )

        monkeypatch.setattr(
            cli.httpx,
            "Client",
            lambda **options: original(
                transport=httpx.MockTransport(handle), **options
            ),
        )
        bundle = tmp_path / "bundle.json"
        bundle.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "name": "cli-delivery",
                    "targets": {"dev": {"project_id": "default"}},
                    "notebooks": [
                        {
                            "key": "hello",
                            "path": "hello.py",
                            "source": "# Databricks notebook source\nprint('cli works')\n",
                        }
                    ],
                    "workflows": [
                        {
                            "key": "hello",
                            "tasks": [{"key": "hello", "notebook": "hello"}],
                        }
                    ],
                }
            )
        )
        assert cli.main(["deploy", str(bundle), "--target", "dev"]) == 0
        output = capsys.readouterr()
        deployed = json.loads(output.out)
        assert token not in output.out and not output.err
        workflow = next(
            item["id"] for item in deployed["resources"] if item["kind"] == "workflows"
        )
        assert (
            cli.main(["run", "--workflow", workflow, "--wait", "--timeout", "30"]) == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "succeeded"
        assert all(
            request.headers["Authorization"] == "Bearer " + token
            for request in requests
        )
        assert not any("/auth/login" in str(request.url) for request in requests)


def test_cli_rejects_remote_plain_http_and_does_not_store_or_print_token(
    monkeypatch, capsys
):
    monkeypatch.setenv("OPENBRICKS_TOKEN", "ob_cli_fixture_token_not_real")
    status = cli.main(["--url", "http://remote.example", "status", "fixture"])
    output = capsys.readouterr()
    assert status == 2 and "HTTPS" in output.err and "ob_cli_fixture" not in output.err


def test_cli_redacts_a_credential_echoed_in_a_remote_error(monkeypatch, capsys):
    token = "ob_never_print_this_fixture_credential"
    monkeypatch.setenv("OPENBRICKS_TOKEN", token)
    original = httpx.Client
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **options: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    403, json={"detail": "Rejected " + token}
                )
            ),
            **options,
        ),
    )
    assert cli.main(["status", "fixture"]) == 2
    output = capsys.readouterr()
    assert token not in output.err and "[redacted]" in output.err
