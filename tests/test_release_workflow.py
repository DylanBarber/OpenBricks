"""Release gates and the actual registry configuration shell must fail closed."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workflow():
    # BaseLoader preserves the Actions `on` key instead of treating it as a
    # YAML 1.1 boolean, and leaves expression/boolean inputs as their text.
    return yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def publication(workflow):
    return next(
        step
        for step in workflow["jobs"]["images"]["steps"]
        if step.get("id") == "publication"
    )


def test_publication_remains_behind_all_checks_and_release_requires_images(workflow):
    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    jobs = workflow["jobs"]
    assert jobs["checks"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["images"]["needs"] == "checks"
    assert jobs["release"]["needs"] == "images"
    assert "if" not in jobs["images"] and "if" not in jobs["release"]
    for job in jobs.values():
        assert "continue-on-error" not in job
        assert all("continue-on-error" not in step for step in job.get("steps", []))


def test_build_once_per_target_keeps_platform_version_and_commit_identity(workflow):
    images = workflow["jobs"]["images"]
    assert images["strategy"]["matrix"]["include"] == [
        {"target": "runtime", "image": "openbricks"},
        {"target": "compute", "image": "openbricks-compute"},
    ]
    builds = [
        step
        for step in images["steps"]
        if step.get("uses", "").startswith("docker/build-push-action@")
    ]
    assert len(builds) == 1
    build = builds[0]["with"]
    assert build["target"] == "${{ matrix.target }}"
    assert build["platforms"] == "linux/amd64" and build["push"] == "true"
    assert build["tags"] == "${{ steps.publication.outputs.tags }}"
    assert "OPENBRICKS_BUILD_ID=${{ github.sha }}" in build["build-args"]
    assert (
        "OPENBRICKS_VERSION=${{ steps.identity.outputs.version }}"
        in build["build-args"]
    )
    guard = publication(workflow)
    assert guard["env"]["DOCKERHUB_ENABLED"] == "${{ vars.DOCKERHUB_ENABLED }}"
    assert guard["env"]["DOCKERHUB_NAMESPACE"] == "${{ vars.DOCKERHUB_NAMESPACE }}"
    assert guard["env"]["DOCKERHUB_USERNAME"] == "${{ vars.DOCKERHUB_USERNAME }}"
    assert guard["env"]["DOCKERHUB_TOKEN"] == "${{ secrets.DOCKERHUB_TOKEN }}"
    assert guard["env"]["VERSION"] == "${{ steps.identity.outputs.version }}"


def test_hub_login_is_conditional_and_cannot_expose_the_token(workflow):
    steps = workflow["jobs"]["images"]["steps"]
    hub = next(
        step for step in steps if step.get("with", {}).get("registry") == "docker.io"
    )
    assert hub["if"] == "steps.publication.outputs.dockerhub_enabled == 'true'"
    assert hub["with"]["username"] == "${{ vars.DOCKERHUB_USERNAME }}"
    assert hub["with"]["password"] == "${{ secrets.DOCKERHUB_TOKEN }}"
    ghcr = next(
        step for step in steps if step.get("with", {}).get("registry") == "ghcr.io"
    )
    assert "if" not in ghcr
    assert steps.index(publication(workflow)) < steps.index(ghcr) < steps.index(hub)
    build_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("docker/build-push-action@")
    )
    report_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Report published registries"
    )
    assert steps.index(hub) < build_index < report_index
    assert "if" not in steps[report_index]  # Default success() only.
    assert "DOCKERHUB_TOKEN" not in steps[report_index].get("env", {})


@pytest.fixture
def bash():
    # Do not invoke Windows' bash.exe shim, which starts an unrelated WSL VM.
    if os.name == "nt":
        executable = Path("C:/Program Files/Git/bin/bash.exe")
        if not executable.is_file():
            pytest.skip("Install Git Bash to execute registry guard checks on Windows")
        return str(executable)
    executable = shutil.which("bash")
    if not executable:
        pytest.skip("Bash is required for Actions shell checks")
    return executable


def run_guard(workflow, bash, tmp_path, **overrides):
    output, summary = tmp_path / "outputs", tmp_path / "summary"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("BASH_ENV", "ENV")
    }
    environment.update(
        {
            "OWNER": "example-github-owner",
            "VERSION": "0.4.0",
            "IMAGE_NAME": "openbricks",
            "DOCKERHUB_ENABLED": "",
            "DOCKERHUB_NAMESPACE": "",
            "DOCKERHUB_USERNAME": "",
            "DOCKERHUB_TOKEN": "",
            "GITHUB_OUTPUT": output.as_posix(),
            "GITHUB_STEP_SUMMARY": summary.as_posix(),
            **overrides,
        }
    )
    result = subprocess.run(
        [
            bash,
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            publication(workflow)["run"],
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    outputs = output.read_text(encoding="utf-8") if output.exists() else ""
    status = summary.read_text(encoding="utf-8") if summary.exists() else ""
    return result, outputs, status


@pytest.mark.parametrize("enabled", ["", "false"])
def test_disabled_hub_explicitly_skips_and_outputs_only_ghcr(
    workflow, bash, tmp_path, enabled
):
    result, outputs, status = run_guard(
        workflow, bash, tmp_path, DOCKERHUB_ENABLED=enabled
    )
    assert result.returncode == 0, result.stderr
    assert outputs.splitlines() == [
        "tags<<OPENBRICKS_REGISTRY_TAGS",
        "ghcr.io/example-github-owner/openbricks:0.4.0",
        "OPENBRICKS_REGISTRY_TAGS",
        "dockerhub_enabled=false",
    ]
    assert "Docker Hub skipped" in status and "GHCR only" in status
    assert "Published" not in status


@pytest.mark.parametrize("image", ["openbricks", "openbricks-compute"])
def test_enabled_hub_outputs_both_versioned_tags_without_token_leak(
    workflow, bash, tmp_path, image
):
    secret = "synthetic-token-never-print"
    result, outputs, status = run_guard(
        workflow,
        bash,
        tmp_path,
        IMAGE_NAME=image,
        DOCKERHUB_ENABLED="true",
        DOCKERHUB_NAMESPACE="example-hub-owner",
        DOCKERHUB_USERNAME="example-login",
        DOCKERHUB_TOKEN=secret,
    )
    assert result.returncode == 0, result.stderr
    assert outputs.splitlines() == [
        "tags<<OPENBRICKS_REGISTRY_TAGS",
        f"ghcr.io/example-github-owner/{image}:0.4.0",
        f"docker.io/example-hub-owner/{image}:0.4.0",
        "OPENBRICKS_REGISTRY_TAGS",
        "dockerhub_enabled=true",
    ]
    assert ":latest" not in outputs
    assert "Both registries must publish successfully" in status
    assert "Published" not in status
    assert secret not in result.stdout + result.stderr + outputs + status


@pytest.mark.parametrize(
    "missing", ["DOCKERHUB_NAMESPACE", "DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"]
)
def test_enabled_hub_missing_configuration_fails_before_any_tag_output(
    workflow, bash, tmp_path, missing
):
    values = {
        "DOCKERHUB_ENABLED": "true",
        "DOCKERHUB_NAMESPACE": "example-hub-owner",
        "DOCKERHUB_USERNAME": "example-login",
        "DOCKERHUB_TOKEN": "synthetic-secret",
        missing: "",
    }
    result, outputs, status = run_guard(workflow, bash, tmp_path, **values)
    assert result.returncode != 0 and "::error::" in result.stdout
    assert outputs == status == ""
    assert "synthetic-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "namespace",
    ["docker.io/owner", "Owner", "owner\ninjected=value", "owner$(touch injected)"],
)
def test_invalid_namespace_cannot_inject_tags_or_commands(
    workflow, bash, tmp_path, namespace
):
    result, outputs, status = run_guard(
        workflow,
        bash,
        tmp_path,
        DOCKERHUB_ENABLED="true",
        DOCKERHUB_NAMESPACE=namespace,
        DOCKERHUB_USERNAME="example-login",
        DOCKERHUB_TOKEN="synthetic-secret",
    )
    assert result.returncode != 0 and "::error::" in result.stdout
    assert outputs == status == "" and not (tmp_path / "injected").exists()
    assert namespace not in result.stdout + result.stderr


def test_misspelled_enable_flag_fails_instead_of_silently_skipping(
    workflow, bash, tmp_path
):
    result, outputs, status = run_guard(
        workflow, bash, tmp_path, DOCKERHUB_ENABLED="TRUE"
    )
    assert result.returncode != 0 and "::error::" in result.stdout
    assert outputs == status == ""


def test_ci_runs_release_contract_on_host_without_changing_backend_image_contents():
    ci = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert any(
        "tests/test_release_workflow.py" in step.get("run", "")
        for step in ci["jobs"]["system"]["steps"]
    )
