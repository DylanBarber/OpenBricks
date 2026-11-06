"""Host-only guard checks; never start Docker, Spark or real HTTP clients."""

import importlib.util
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def smoke():
    source = Path(__file__).resolve().parents[1] / "scripts/v5-features-smoke.py"
    spec = importlib.util.spec_from_file_location("v5_feature_smoke", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "phase,origin",
    [("prepare", "http://127.0.0.1:3001"), ("recover", "http://127.0.0.1:3002/")],
)
def test_only_exact_phase_origins_are_accepted(smoke, phase, origin):
    assert smoke.validate_origin(origin, phase) == origin.rstrip("/")


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3002",
        "http://127.0.0.1:3001/path",
        "http://user:secret@127.0.0.1:3001",
        "http://127.0.0.1:3001?token=secret",
        "http://127.0.0.1.evil:3001",
    ],
)
def test_unsafe_or_wrong_phase_origins_fail_before_any_client_is_created(
    smoke, origin, monkeypatch, tmp_path
):
    def forbidden(*args, **kwargs):
        raise AssertionError("No client should be created")

    monkeypatch.setattr(smoke, "API", forbidden)
    with pytest.raises(smoke.SmokeError):
        smoke.main(
            [
                "--phase",
                "prepare",
                "--base-url",
                origin,
                "--receipt",
                str(tmp_path / "receipt.json"),
            ]
        )


def test_receipt_paths_are_confined_to_private_project_artifacts(
    smoke, tmp_path, monkeypatch
):
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    expected = tmp_path / ".test-data" / "proof.json"
    assert smoke.private_receipt(expected) == expected.resolve()
    with pytest.raises(smoke.SmokeError):
        smoke.private_receipt(tmp_path / "published-proof.json")
    with pytest.raises(smoke.SmokeError):
        smoke.private_receipt(tmp_path / ".test-data" / ".." / "published-proof.json")


def test_http_failures_never_include_raw_response_bodies(smoke):
    secret = "synthetic-value-that-must-not-appear"
    api = smoke.API("http://127.0.0.1:3001")
    api.close()
    api.client = httpx.Client(
        base_url="http://127.0.0.1:3001",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"detail": secret})
        ),
    )
    try:
        with pytest.raises(smoke.SmokeError) as error:
            api.call("GET", "/runs/example")
        assert secret not in str(error.value)
        assert "500" in str(error.value)
    finally:
        api.close()


def test_receipt_validation_rejects_identifier_injection_before_api_calls(
    smoke, tmp_path
):
    path = tmp_path / "proof.json"
    path.write_text(
        '{"format":"openbricks-v5-features-v1","prepared":true,"source_origin":"http://127.0.0.1:3001","admin_id":"../../production"}'
    )

    class ForbiddenAPI:
        def call(self, *args, **kwargs):
            raise AssertionError("Invalid receipt must not reach HTTP")

    with pytest.raises(smoke.SmokeError):
        smoke.recover(ForbiddenAPI(), path)
