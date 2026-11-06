"""The public smoke runner must reject production targets before using Docker."""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts/run-system-smoke.py"
    spec = importlib.util.spec_from_file_location("system_smoke_runner", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("OPENBRICKS_SMOKE_URL", raising=False)
    return module


def test_plan_never_uses_docker_or_creates_credentials(runner, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("The offline plan used Docker or created credentials")

    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)
    monkeypatch.setattr(runner.Runner, "prepare", forbidden)
    assert runner.main(["--plan"]) == 0
    assert "fresh host" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--project", "openbricks", "--plan"],
        ["--url", "http://127.0.0.1:3000", "--plan"],
        ["--url", "http://production.example:3001", "--plan"],
        ["--runtime-image", "runtime:latest", "--plan"],
    ],
)
def test_rejects_wrong_target_before_any_docker_command(runner, monkeypatch, arguments):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid target reached Docker")

    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)
    assert runner.main(arguments) == 1


def config(runner):
    value = {
        "name": runner.PROJECT,
        "services": {name: {} for name in runner.SERVICES},
        "volumes": {"data": {"name": runner.PROJECT + "_data"}},
        "networks": {"default": {"name": runner.PROJECT + "_default"}},
    }
    for name, port, target in (
        ("control", 3001, 3000),
        ("recovery-control", 3002, 3000),
        ("receiver", 8025, 8025),
        ("receiver", 8036, 8036),
    ):
        value["services"][name].setdefault("ports", []).append(
            {"host_ip": "127.0.0.1", "published": str(port), "target": target}
        )
    return value


def test_config_accepts_only_namespaced_disposable_resources(runner):
    runner.validate_config(config(runner))
    for change in ("external", "production-name", "host-bind", "public-port"):
        invalid = copy.deepcopy(config(runner))
        if change == "external":
            invalid["volumes"]["data"]["external"] = True
        elif change == "production-name":
            invalid["volumes"]["data"]["name"] = "openbricks_data"
        elif change == "host-bind":
            invalid["services"]["compute"]["volumes"] = [
                {"type": "bind", "source": str(runner.ROOT / "data"), "read_only": True}
            ]
        else:
            invalid["services"]["control"]["ports"][0]["host_ip"] = "0.0.0.0"
        with pytest.raises(runner.SmokeError):
            runner.validate_config(invalid)


def test_watcher_ignores_previous_other_project_and_accepted_deliveries(runner):
    def event(delivery, **changes):
        value = {
            "channel": "webhook",
            "accepted": False,
            "received_at": 200,
            "payload": {
                "delivery_id": delivery,
                "project_id": "proof",
                "event": "failed",
                "run_id": "ready-run",
            },
        }
        value.update(changes)
        return value

    stale = event("old", received_at=100)
    prior = event("already-observed")
    accepted = event("accepted", accepted=True)
    unrelated = event("other")
    unrelated["payload"]["project_id"] = "another-project"
    matching = event("new")
    events = [stale, prior, accepted, unrelated, matching]
    assert (
        runner.rejected_delivery(
            events, {"already-observed"}, "proof", 150, "ready-run"
        )
        == matching
    )
    assert (
        runner.rejected_delivery(
            events[:-1], {"already-observed"}, "proof", 150, "ready-run"
        )
        is None
    )
    # A rejection before await_run's final API response must not trigger the
    # expected outage until its receipt handshakes that same run.
    assert runner.rejected_delivery(events, set(), "proof", 150, None) is None
    assert runner.rejected_delivery(events, set(), "proof", 150, "another-run") is None


def test_runner_does_not_inherit_production_configuration(runner, monkeypatch):
    monkeypatch.setenv("OPENBRICKS_ADMIN_PASSWORD", "production-secret")
    monkeypatch.setenv("OPENBRICKS_COMPUTE_URL", "http://production-worker:8001")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "openbricks")
    monkeypatch.setenv("PYTHONOPTIMIZE", "2")
    args = SimpleNamespace(
        project=runner.PROJECT,
        url=runner.ORIGIN,
        runtime_image=None,
        compute_image=None,
    )
    instance = runner.Runner(args)
    assert "OPENBRICKS_ADMIN_PASSWORD" not in instance.environment
    assert "OPENBRICKS_COMPUTE_URL" not in instance.environment
    assert "COMPOSE_PROJECT_NAME" not in instance.environment
    assert instance.environment["COMPOSE_DISABLE_ENV_FILE"] == "1"
    assert instance.environment["PYTHONOPTIMIZE"] == "0"
