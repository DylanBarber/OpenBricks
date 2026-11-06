"""Best-effort CPU capacity within host, process-affinity and cgroup limits."""

from __future__ import annotations

import math
import os
from pathlib import Path

import psutil

MAX_QUOTA_BYTES = 128


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _read_quota(path):
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_QUOTA_BYTES + 1)
        if len(content) > MAX_QUOTA_BYTES:
            return None
        return content.decode("ascii").strip()
    except (OSError, UnicodeError):
        return None


def _ratio(quota, period):
    if not quota or not period or not quota.isdecimal() or not period.isdecimal():
        return None
    denominator = int(period)
    return _positive(int(quota) / denominator) if denominator > 0 else None


def _quota_capacity(root):
    value = _read_quota(root / "cpu.max")
    if value:
        parts = value.split()
        if len(parts) == 2:
            capacity = _ratio(*parts)
            if capacity is not None:
                return capacity
    # v1 can mount cpu directly or together with cpuacct. A custom root can
    # also be the CPU controller's mount itself.
    for controller in (root / "cpu", root / "cpu,cpuacct", root):
        capacity = _ratio(
            _read_quota(controller / "cpu.cfs_quota_us"),
            _read_quota(controller / "cpu.cfs_period_us"),
        )
        if capacity is not None:
            return capacity
    return None


def _affinity_capacity():
    try:
        capacity = _positive(len(os.sched_getaffinity(0)))
        if capacity is not None:
            return capacity
    except (AttributeError, OSError, NotImplementedError):
        pass
    try:
        return _positive(len(psutil.Process().cpu_affinity()))
    except (AttributeError, OSError, psutil.Error, NotImplementedError):
        return None


def cpu_capacity(cgroup_root=Path("/sys/fs/cgroup")):
    """Return a positive core-equivalent capacity, or None when unavailable.

    A quota can describe a fraction of a CPU. An unlimited or unavailable
    controller still permits a host/affinity estimate; no limit is invented.
    """
    try:
        host = _positive(os.cpu_count())
    except (OSError, NotImplementedError):
        host = None
    capacities = [host, _affinity_capacity(), _quota_capacity(Path(cgroup_root))]
    known = [value for value in capacities if value is not None]
    return min(known) if known else None
