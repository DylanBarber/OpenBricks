"""Bounded execution telemetry and best-effort masking of known credentials."""
from __future__ import annotations

import re
from datetime import datetime, timezone


class Redactor:
    def __init__(self, connections=()):
        values = {str(value) for item in connections for value in item.get("secrets", {}).values() if value is not None and str(value)}
        self.values = tuple(values)
        self.maximum = max(map(len, values), default=1)
        self.pattern = re.compile("|".join(re.escape(value) for value in sorted(values, key=len, reverse=True))) if values else None

    def text(self, value):
        if not self.pattern:
            return value
        return "".join(part if part == "[REDACTED]" else self.pattern.sub("[REDACTED]", part)
            for part in re.split(r"(\[REDACTED\])", value))

    def clean(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(key) if isinstance(key, str) else key: self.clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        return value

    def event(self, value):
        # Event/output/result keys are a fixed wire schema, not user field
        # names. Mask their contents while retaining keys a short credential
        # may coincidentally equal ("output", "stdout", "columns", ...).
        safe = {key: self.clean(item) for key, item in value.items()}
        if isinstance(value.get("output"), dict):
            safe["output"] = self.output(value["output"])
        if isinstance(value.get("result"), dict):
            safe["result"] = self.table(value["result"])
        if isinstance(value.get("report"), dict):
            safe["report"] = self.record(value["report"])
        # Credentials can be very short. Never rewrite protocol identifiers or
        # enum fields, otherwise an innocent match can lose the completion
        # event or attach output to a different cell.
        for key in ("type", "run_id", "cell_id", "event_id", "status", "stream", "error_class"):
            if key in value:
                safe[key] = value[key]
        if isinstance(value.get("output"), dict):
            for key in ("cell_id", "status"):
                if key in value["output"]:
                    safe["output"][key] = value["output"][key]
        return safe

    def table(self, value):
        if not isinstance(value, dict):
            return self.clean(value)
        return {key: self.clean(item) for key, item in value.items()}

    def output(self, value):
        safe = {key: self.clean(item) for key, item in value.items()}
        if "result" in value:
            safe["result"] = self.table(value["result"])
        for key in ("cell_id", "status"):
            if key in value:
                safe[key] = value[key]
        return safe

    def record(self, value):
        """Structured quality records, with arbitrary row/metadata maps masked."""
        if isinstance(value, dict):
            return {key: self.clean(item) if key in ("rows", "metadata", "parameters", "properties") else self.record(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self.record(item) for item in value]
        return self.clean(value)


class StreamRedactor:
    def __init__(self, redactor):
        self.redactor, self.pending = redactor, ""

    def feed(self, value="", final=False):
        self.pending += value
        boundary = len(self.pending)
        if not final:
            # Hold only a suffix that could still become a secret on the next
            # write. Ordinary short progress lines should stream immediately.
            for secret in self.redactor.values:
                index = self.pending.find(secret[0], max(0, len(self.pending) - len(secret) + 1))
                while index >= 0:
                    if secret.startswith(self.pending[index:]):
                        boundary = min(boundary, index)
                        break
                    index = self.pending.find(secret[0], index + 1)
        if not boundary:
            return ""
        result, offset = [], 0
        pattern = self.redactor.pattern
        while offset < boundary:
            match = pattern.search(self.pending, offset) if pattern else None
            if match and match.start() < boundary:
                result.extend((self.pending[offset:match.start()], "[REDACTED]"))
                offset = match.end()
            else:
                result.append(self.pending[offset:boundary])
                offset = boundary
        self.pending = self.pending[offset:]
        return "".join(result)


def classify_error(error, returncode=None):
    value = (error or "").lower()
    if "no durable record" in value or "outcome is unknown" in value:
        return "unknown_outcome"
    if "outofmemory" in value or "memoryerror" in value or "java heap space" in value:
        return "out_of_memory"
    if "exceeded" in value or "timeout" in value:
        return "timeout"
    if "cancel" in value:
        return "cancelled"
    if "connection interrupted" in value:
        return "connectivity"
    if returncode in (-9, 137):
        return "process_killed"  # SIGKILL alone does not prove an OOM kill.
    return "execution" if error else None


def notebook_provenance(notebook):
    return {key: value for key, value in {"repository_id": notebook.get("repository_id"), "path": notebook.get("path"),
        "commit": notebook.get("imported_commit") or notebook.get("commit_full"), "notebook_version": notebook.get("version")}.items() if value is not None}


def apply_event(store, run, event):
    kind = event.get("type")
    if kind == "progress":
        run["progress"] = {key: event[key] for key in ("cell_id", "cell_index", "total_cells") if key in event}
    elif kind == "log":
        logs = run.setdefault("logs", [])
        text = str(event.get("text", ""))[:8192]
        logs.append({"at": datetime.now(timezone.utc).isoformat(), "cell_id": event.get("cell_id"), "stream": event.get("stream", "stdout"), "text": text})
        while len(logs) > 200 or sum(len(item["text"]) for item in logs) > 100_000:
            logs.pop(0)
    elif kind in ("data_result", "quality", "lineage"):
        from .data_workspace import persist_data_event
        persist_data_event(store, run, event)
    return run
