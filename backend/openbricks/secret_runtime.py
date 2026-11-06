"""Transient notebook secret access. Nothing in this module writes credentials."""

from __future__ import annotations

import re
from threading import RLock

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class SecretRegistry:
    """Only the explicitly dispatched scope/key pairs exist in a worker."""

    def __init__(self, items=()):
        if not isinstance(items, (list, tuple)) or len(items) > 32:
            raise ValueError("Select at most 32 notebook secrets")
        self._values = {}
        for item in items:
            if not isinstance(item, dict) or not all(
                isinstance(item.get(key), str) for key in ("scope", "key", "value")
            ):
                raise ValueError("Invalid notebook secret envelope")
            if (
                not _NAME.fullmatch(item["scope"])
                or not _NAME.fullmatch(item["key"])
                or not item["value"]
                or len(item["value"].encode("utf-8")) > 4096
                or (item["scope"], item["key"]) in self._values
            ):
                raise ValueError("Invalid notebook secret envelope")
            self._values[(item["scope"], item["key"])] = item["value"]

    def get(self, scope: str, key: str) -> str:
        try:
            return self._values[(scope, key)]
        except KeyError:
            raise ValueError("This secret was not selected for the notebook") from None

    def list(self, scope: str):
        return [{"key": key} for found, key in sorted(self._values) if found == scope]

    def listScopes(self):
        return [{"name": name} for name in sorted({key[0] for key in self._values})]

    def __repr__(self):
        return "<Notebook secrets: explicitly selected keys; values hidden>"


def redaction_connections(items=()):
    """Adapt transient values to the existing bounded-output redactor contract."""
    return [{"secrets": {"value": item["value"]}} for item in items]


class SecretHistoryFull(RuntimeError):
    """The owning process must be replaced before accepting more values."""


class RedactionHistory:
    """Keep rotated values masked for the entire lifetime of a Python/JVM worker."""

    def __init__(self, max_values=256, max_bytes=1024 * 1024):
        self._values = set()
        self._lock = RLock()
        self.max_values, self.max_bytes = max_values, max_bytes

    def include(self, items=()):
        with self._lock:
            values = self._values | {
                item["value"] for item in items if item.get("value")
            }
            if (
                len(values) > self.max_values
                or sum(len(value.encode("utf-8")) for value in values) > self.max_bytes
            ):
                raise SecretHistoryFull(
                    "Restart the notebook worker before accepting more rotated secrets"
                )
            self._values = values
            return [{"secrets": {"value": value}} for value in sorted(values)]
