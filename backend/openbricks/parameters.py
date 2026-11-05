"""Typed, bounded run inputs and the notebook widgets compatibility surface."""
from __future__ import annotations

import json
import math
import re
from datetime import date

TYPES = {"string", "integer", "number", "boolean", "date", "json"}
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def typed_value(value, kind):
    if kind == "string":
        if not isinstance(value, str):
            raise ValueError("expected a string")
    elif kind == "integer":
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("expected an integer")
    elif kind == "number":
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise ValueError("expected a number") from None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("expected a finite number")
    elif kind == "boolean":
        if isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if not isinstance(value, bool):
            raise ValueError("expected true or false")
    elif kind == "date":
        if not isinstance(value, str):
            raise ValueError("expected an ISO date")
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError()
        except ValueError:
            raise ValueError("expected an ISO date (YYYY-MM-DD)") from None
    elif kind != "json":
        raise ValueError("unsupported parameter type")
    try:
        if len(json.dumps(value, allow_nan=False)) > 16_384:
            raise ValueError("parameter exceeds 16 KB")
    except (TypeError, OverflowError):
        raise ValueError("parameter must be JSON serializable") from None
    return value


def normalize_definitions(definitions):
    if not isinstance(definitions, list) or len(definitions) > 100:
        raise ValueError("Provide at most 100 parameter definitions")
    values, names = [], set()
    for definition in definitions:
        if not isinstance(definition, dict):
            raise ValueError("Parameter definitions must be objects")
        name, kind = definition.get("name"), definition.get("type", "string")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise ValueError("Parameter names must be unique identifiers (at most 64 characters)")
        if kind not in TYPES:
            raise ValueError(f"Unsupported type for parameter {name}")
        item = {"name": name, "type": kind, "required": bool(definition.get("required", False))}
        if "default" in definition:
            item["default"] = typed_value(definition["default"], kind)
        if "choices" in definition:
            choices = definition["choices"]
            if not isinstance(choices, list) or not 1 <= len(choices) <= 100:
                raise ValueError(f"Provide 1 to 100 choices for {name}")
            item["choices"] = [typed_value(choice, kind) for choice in choices]
            if "default" in item and item["default"] not in item["choices"]:
                raise ValueError(f"Default for {name} must be one of its choices")
        values.append(item)
        names.add(name)
    return values


def resolve_parameters(definitions, values=None, defaults=None):
    definitions = normalize_definitions(definitions or [])
    if (values is not None and not isinstance(values, dict)) or (defaults is not None and not isinstance(defaults, dict)):
        raise ValueError("Parameters must be a JSON object")
    supplied = {**(defaults or {}), **(values or {})}
    names = {item["name"] for item in definitions}
    unknown = set(supplied) - names
    if unknown:
        raise ValueError("Unknown parameters: " + ", ".join(sorted(unknown)))
    result = {}
    for item in definitions:
        name = item["name"]
        if name not in supplied and "default" not in item:
            if item["required"]:
                raise ValueError(f"Parameter {name} is required")
            continue
        try:
            value = typed_value(supplied.get(name, item.get("default")), item["type"])
            if "choices" in item and value not in item["choices"]:
                raise ValueError("must be one of its configured choices")
        except ValueError as exc:
            raise ValueError(f"Parameter {name}: {exc}") from None
        result[name] = value
    if len(json.dumps(result, allow_nan=False)) > 65_536:
        raise ValueError("Combined parameters exceed 64 KB")
    return result


def widget_text(value):
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), allow_nan=False)


class Widgets:
    """Widgets share immutable run inputs; declarations supply missing defaults."""
    def __init__(self, parameters=None):
        self.values = {name: widget_text(value) for name, value in (parameters or {}).items()}

    def text(self, name, defaultValue="", label=None):
        self.values.setdefault(name, str(defaultValue))

    def dropdown(self, name, defaultValue, choices, label=None):
        self.values.setdefault(name, str(defaultValue))
        if self.values[name] not in choices:
            raise ValueError(f"Widget {name} is not one of its choices")

    def combobox(self, name, defaultValue, choices, label=None):
        self.text(name, defaultValue, label)

    def multiselect(self, name, defaultValue, choices, label=None):
        self.values.setdefault(name, str(defaultValue))
        if any(value not in choices for value in self.values[name].split(",") if value):
            raise ValueError(f"Widget {name} is not one of its choices")

    def get(self, name):
        if name not in self.values:
            raise ValueError(f"Widget {name} is not defined")
        return self.values[name]

    def getAll(self):
        return dict(self.values)

    def remove(self, name):
        self.values.pop(name, None)

    def removeAll(self):
        self.values.clear()
