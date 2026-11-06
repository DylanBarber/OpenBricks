"""Quota/affinity-aware capacity stays distinct from the legacy host CPU count."""

import importlib
import math
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openbricks import compute

capacity = importlib.import_module("openbricks.cpu_capacity")
TOKEN = "cpu-capacity-test-token-at-least-32-characters"


@pytest.fixture(autouse=True)
def cpu_environment(monkeypatch):
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(
        capacity.os, "sched_getaffinity", lambda pid: set(range(8)), raising=False
    )
    monkeypatch.setattr(
        capacity.psutil,
        "Process",
        lambda: SimpleNamespace(cpu_affinity=lambda: list(range(8))),
    )


@pytest.mark.parametrize(
    "quota,expected",
    [
        ("200000 100000", 2),
        ("50000 100000", 0.5),
        ("25000 100000", 0.25),
        ("2000000 100000", 8),
    ],
)
def test_v2_quota_is_fractional_and_bounded_by_affinity(tmp_path, quota, expected):
    (tmp_path / "cpu.max").write_text(quota)
    assert capacity.cpu_capacity(tmp_path) == expected


@pytest.mark.parametrize(
    "quota",
    [
        "max 100000",
        "",
        "broken",
        "0 100000",
        "-1 100000",
        "100000 0",
        "100000 -1",
        "nan 100000",
        "inf 100000",
        "100000 100000 extra",
        "1" * 129,
    ],
)
def test_v2_unlimited_or_malformed_quota_falls_back_to_affinity(tmp_path, quota):
    (tmp_path / "cpu.max").write_text(quota)
    assert capacity.cpu_capacity(tmp_path) == 8


@pytest.mark.parametrize("controller", ["cpu", "cpu,cpuacct", "."])
def test_v1_controller_mount_layouts(tmp_path, controller):
    root = tmp_path / controller
    root.mkdir(exist_ok=True)
    (root / "cpu.cfs_quota_us").write_text("150000\n")
    (root / "cpu.cfs_period_us").write_text("100000\n")
    assert capacity.cpu_capacity(tmp_path) == 1.5


@pytest.mark.parametrize(
    "quota,period",
    [
        ("-1", "100000"),
        ("0", "100000"),
        ("100000", "0"),
        ("bad", "100000"),
        ("100000", "infinity"),
    ],
)
def test_v1_unlimited_or_malformed_quota_is_not_a_zero_capacity(
    tmp_path, quota, period
):
    root = tmp_path / "cpu"
    root.mkdir()
    (root / "cpu.cfs_quota_us").write_text(quota)
    (root / "cpu.cfs_period_us").write_text(period)
    assert capacity.cpu_capacity(tmp_path) == 8


def test_psutil_affinity_fallback_and_host_ceiling(tmp_path, monkeypatch):
    monkeypatch.delattr(capacity.os, "sched_getaffinity")
    monkeypatch.setattr(
        capacity.psutil, "Process", lambda: SimpleNamespace(cpu_affinity=lambda: [2, 3])
    )
    assert capacity.cpu_capacity(tmp_path) == 2
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 1)
    assert capacity.cpu_capacity(tmp_path) == 1


def test_unavailable_affinity_and_cgroup_fall_back_to_host(tmp_path, monkeypatch):
    def unavailable(*args):
        raise NotImplementedError

    monkeypatch.setattr(capacity.os, "sched_getaffinity", unavailable)
    monkeypatch.setattr(capacity.psutil, "Process", unavailable)
    assert capacity.cpu_capacity(tmp_path) == 16


def test_empty_affinity_and_unknown_host_can_use_quota_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: None)
    monkeypatch.setattr(capacity.os, "sched_getaffinity", lambda pid: set())
    monkeypatch.setattr(
        capacity.psutil, "Process", lambda: SimpleNamespace(cpu_affinity=lambda: [])
    )
    (tmp_path / "cpu.max").write_text("50000 100000")
    assert capacity.cpu_capacity(tmp_path) == 0.5
    (tmp_path / "cpu.max").unlink()
    assert capacity.cpu_capacity(tmp_path) is None


def test_quota_reads_are_bounded_and_invalid_encoding_is_ignored(tmp_path, monkeypatch):
    reads = []

    class BoundedFile:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            reads.append(size)
            return b"x" * size

    monkeypatch.setattr(capacity.Path, "open", lambda *args, **kwargs: BoundedFile())
    assert capacity.cpu_capacity(tmp_path) == 8
    assert reads and set(reads) == {capacity.MAX_QUOTA_BYTES + 1}
    monkeypatch.undo()
    (tmp_path / "cpu.max").write_bytes(b"\xff\xfe")
    assert capacity._quota_capacity(tmp_path) is None


@pytest.mark.parametrize("value", [None, 0, -1, True, math.inf, math.nan])
def test_invalid_host_values_never_create_invalid_capacity(
    tmp_path, monkeypatch, value
):
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: value)
    assert capacity.cpu_capacity(tmp_path) == 8


def test_status_exposes_effective_capacity_and_preserves_host_cpu_count(
    tmp_path, monkeypatch
):
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.max").write_text("50000 100000")
    monkeypatch.setattr(compute, "cpu_capacity", lambda: capacity.cpu_capacity(cgroup))
    with TestClient(compute.create_compute_app(tmp_path / "data", TOKEN)) as client:
        response = client.get("/status", headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200
        assert response.json()["cpu_count"] == 16
        assert response.json()["cpu_capacity"] == 0.5


def test_status_omits_unavailable_capacity_without_changing_legacy_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(compute, "cpu_capacity", lambda: None)
    monkeypatch.setattr(compute.os, "cpu_count", lambda: None)
    with TestClient(compute.create_compute_app(tmp_path, TOKEN)) as client:
        response = client.get("/status", headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200
        assert response.json()["cpu_count"] == 1
        assert "cpu_capacity" not in response.json()
