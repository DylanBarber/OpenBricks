"""Pure deployment planning for portable notebook/workflow project bundles."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import PurePosixPath
import re
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException

from .environments import (
    artifact_path,
    file_hash,
    get_environment,
    runtime_identity,
    validate_manifest,
)
from .notebooks import parse_notebook
from .parameters import normalize_definitions, resolve_parameters
from .permissions import now
from .workflows import WorkflowManager

KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
VARIABLE = re.compile(r"^\$\{vars\.([A-Za-z_][A-Za-z0-9_]*)\}$")


def identity(project_id, bundle_name, kind, key):
    return uuid5(
        NAMESPACE_URL, f"openbricks:{project_id}:{bundle_name}:{kind}:{key}"
    ).hex


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def substitute(value, variables):
    if isinstance(value, str):
        match = VARIABLE.fullmatch(value)
        if match:
            if match[1] not in variables:
                raise ValueError(f"Target variable {match[1]} is not defined")
            return copy.deepcopy(variables[match[1]])
    elif isinstance(value, list):
        return [substitute(item, variables) for item in value]
    elif isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    return value


class OverlayStore:
    def __init__(self, store, records):
        self.store, self.records, self.directory = store, records, store.directory

    def get(self, kind, identifier):
        return self.records.get((kind, identifier)) or self.store.get(kind, identifier)


def validate_keys(values, label, maximum):
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError(f"A bundle supports at most {maximum} {label}")
    keys = set()
    for item in values:
        key = item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str) or not KEY.fullmatch(key) or key in keys:
            raise ValueError(f"Every {label} entry needs a unique simple key")
        keys.add(key)
    return keys


def job_resources(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"mode", "memory_mb", "priority"}:
        raise ValueError(
            "Bundle job resources accept mode, memory_mb, and priority; the server selects the run owner"
        )
    if value.get("mode", "job") != "job":
        raise ValueError("Deployed tasks and schedules use job workers")
    for field, low, high in (("memory_mb", 128, 262144), ("priority", -20, 20)):
        if field in value and (
            isinstance(value[field], bool)
            or not isinstance(value[field], int)
            or not low <= value[field] <= high
        ):
            raise ValueError(f"Invalid job resource {field}")
    return copy.deepcopy(value)


def plan_bundle(store, bundle, target, user, access):
    if not isinstance(bundle, dict) or bundle.get("format_version") != 1:
        raise ValueError("Use bundle format_version: 1")
    if len(canonical(bundle).encode()) > 20 * 1024 * 1024:
        raise ValueError("A bundle supports at most 20 MB of source and definitions")
    name = bundle.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name
    ):
        raise ValueError("Choose a simple bundle name of at most 80 characters")
    targets = bundle.get("targets", {})
    if (
        not isinstance(targets, dict)
        or not 1 <= len(targets) <= 20
        or target not in targets
        or not isinstance(targets[target], dict)
    ):
        raise ValueError("Choose a declared deployment target")
    binding = targets[target]
    if set(binding) - {"project_id", "connections", "clusters", "variables"}:
        raise ValueError(
            "Targets accept project_id, connection references, cluster references, and variables only; keep credentials in Connections"
        )
    project_id = binding.get("project_id")
    if not isinstance(project_id, str):
        raise ValueError("Every target needs an existing project_id")
    project = access.require_project(user, project_id, "edit")
    variables = binding.get("variables", {})
    if not isinstance(variables, dict) or len(variables) > 100:
        raise ValueError("Target variables must be a bounded object")
    definition = substitute(copy.deepcopy(bundle), variables)
    connection_bindings, cluster_bindings = binding.get("connections", {}), binding.get(
        "clusters", {}
    )
    if not isinstance(connection_bindings, dict) or not isinstance(
        cluster_bindings, dict
    ):
        raise ValueError("Connection and cluster references must be objects")
    for identifier in connection_bindings.values():
        access.require_connection(user, identifier, project_id, "edit")
    for identifier in cluster_bindings.values():
        access.require_cluster(user, identifier, project_id, "edit")
    default_cluster = (
        cluster_bindings.get("default") or (project.get("cluster_ids") or ["local"])[0]
    )
    access.require_cluster(user, default_cluster, project_id, "edit")

    def connections(aliases):
        if not isinstance(aliases, list) or len(aliases) > 100:
            raise ValueError(
                "Notebook connections must list declared connection aliases"
            )
        try:
            return list(dict.fromkeys(connection_bindings[alias] for alias in aliases))
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "A notebook references an undeclared target connection alias"
            ) from exc

    def cluster(alias=None):
        if alias is None:
            return default_cluster
        if alias not in cluster_bindings:
            raise ValueError("A resource references an undeclared target cluster alias")
        return cluster_bindings[alias]

    environment = definition.get("environment", {})
    if not isinstance(environment, dict):
        raise ValueError("Declare a pinned environment object")
    if environment.get("id") and "wheels" not in environment:
        manifest = get_environment(store, environment["id"], project_id)
    else:
        manifest = validate_manifest(
            {**environment, "runtime": environment.get("runtime", runtime_identity())}
        )
    if manifest["runtime"] != runtime_identity():
        raise ValueError("The bundle environment requires a different runtime build")
    for item in manifest["wheels"]:
        path = artifact_path(store.directory, item)
        if not path.is_file() or path.is_symlink() or file_hash(path) != item["sha256"]:
            raise ValueError(
                "Pinned wheel artifact is unavailable; upload the wheel and capture its environment first"
            )
    bundle_owner = identity(project_id, name, "bundle", "owner")
    records, changes, observed = {}, [], []
    instant = now()

    def record(kind, key, values):
        identifier = identity(project_id, name, kind, key)
        old = store.get(kind, identifier)
        if old and (
            (old.get("project_id") or "default") != project_id
            or old.get("bundle_owner") != bundle_owner
        ):
            raise HTTPException(
                409,
                "A managed resource moved or its ownership changed. Restore its project before redeploying",
            )
        value = {
            **values,
            "id": identifier,
            "project_id": project_id,
            "bundle_owner": bundle_owner,
            "bundle_name": name,
            "bundle_key": key,
        }
        if kind == "jobs" and old:
            for field in ("last_run_at", "last_error"):
                if field in old:
                    value[field] = old[field]
        same = old is not None and all(
            old.get(field) == item for field, item in value.items()
        )
        value["created_at"] = old.get("created_at", instant) if old else instant
        value["updated_at"] = old.get("updated_at", instant) if same else instant
        if kind == "notebooks":
            value["version"] = (
                old.get("version", 1)
                if same
                else (old.get("version", 0) + 1 if old else 1)
            )
        records[(kind, identifier)] = value
        changes.append(
            {
                "kind": kind,
                "key": key,
                "id": identifier,
                "action": "unchanged" if same else "update" if old else "create",
            }
        )
        observed.append(
            {
                "kind": kind,
                "id": identifier,
                "sha256": (
                    hashlib.sha256(canonical(old).encode()).hexdigest() if old else None
                ),
            }
        )
        return value

    env_key = f"{project_id}:{manifest['id']}"
    env_old = store.get("environments", env_key)
    records[("environments", env_key)] = {
        **manifest,
        "id": env_key,
        "manifest_id": manifest["id"],
        "name": f"Bundle {name}",
        "project_id": project_id,
        "created_at": env_old.get("created_at", instant) if env_old else instant,
    }
    notebooks = definition.get("notebooks", [])
    notebook_keys = validate_keys(notebooks, "notebooks", 500)
    if not notebooks:
        raise ValueError("A bundle must declare at least one notebook")
    folders = {}

    def folder(relative):
        parent = None
        segments = [name, *PurePosixPath(relative).parent.parts]
        if segments[-1:] == ["."]:
            segments.pop()
        prefix = []
        for segment in segments:
            prefix.append(segment)
            key = "/".join(prefix)
            if key not in folders:
                folders[key] = record(
                    "folders", key, {"name": segment, "parent_id": parent}
                )["id"]
            parent = folders[key]
        return parent

    notebook_records = {}
    for original in notebooks:
        key, path = original["key"], original.get("path", original["key"] + ".ipynb")
        if (
            not isinstance(path, str)
            or "\\" in path
            or PurePosixPath(path).is_absolute()
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise ValueError("Notebook paths must be canonical relative paths")
        if "source" in original:
            if not isinstance(original["source"], str):
                raise ValueError("Notebook source must be UTF-8 text")
            parsed = parse_notebook(path, original["source"].encode())
        elif isinstance(original.get("cells"), list):
            parsed = {
                "name": key,
                "description": "",
                "language": "python",
                "cells": copy.deepcopy(original["cells"]),
            }
        else:
            raise ValueError(
                "Each notebook needs inline source/cells; the CLI packs local paths before deployment"
            )
        cells = parsed["cells"]
        if len(cells) > 500:
            raise ValueError("Notebook cell limit exceeded")
        total = 0
        for index, cell in enumerate(cells):
            if cell.get("language") not in (
                "python",
                "sql",
                "markdown",
            ) or not isinstance(cell.get("source"), str):
                raise ValueError(
                    "Notebook cells need a supported language and text source"
                )
            total += len(cell["source"].encode())
            cell["id"] = identity(project_id, name, "cells", f"{key}:{index}")
        if total > 10 * 1024 * 1024:
            raise ValueError("Notebook source exceeds 10 MB")
        values = {
            **parsed,
            "name": original.get("name", parsed["name"]),
            "description": original.get("description", parsed.get("description", "")),
            "cells": cells,
            "folder_id": folder(path),
            "cluster_id": cluster(original.get("cluster")),
            "connection_ids": connections(original.get("connections", [])),
            "parameter_definitions": normalize_definitions(
                original.get("parameter_definitions", [])
            ),
            "environment_id": manifest["id"],
            "repository_id": None,
            "path": path,
        }
        if not isinstance(values["name"], str) or not 1 <= len(values["name"]) <= 200:
            raise ValueError("Notebook names must be 1 to 200 characters")
        notebook_records[key] = record("notebooks", key, values)

    workflows = definition.get("workflows", [])
    workflow_keys = validate_keys(workflows, "workflows", 100)
    workflow_records = {}
    validator = WorkflowManager(OverlayStore(store, records), None, access)
    for original in workflows:
        value = {
            field: copy.deepcopy(item)
            for field, item in original.items()
            if field not in ("key",)
        }
        value.update(name=original.get("name", original["key"]), project_id=project_id)
        tasks = []
        for task in original.get("tasks", []):
            item = copy.deepcopy(task)
            notebook_key = item.pop("notebook", None)
            if notebook_key not in notebook_keys:
                raise ValueError(
                    "Workflow tasks must reference a notebook key declared in this bundle"
                )
            item["notebook_id"] = notebook_records[notebook_key]["id"]
            item["environment_id"] = manifest["id"]
            item["resource_options"] = job_resources(item.get("resource_options"))
            if "cluster" in item:
                item["cluster_id"] = cluster(item.pop("cluster"))
            if "connections" in item:
                item["connection_ids"] = connections(item.pop("connections"))
            tasks.append(item)
        value["tasks"] = tasks
        value = validator.validate(value, user)
        workflow_records[original["key"]] = record("workflows", original["key"], value)

    schedules = definition.get("schedules", [])
    validate_keys(schedules, "schedules", 200)
    # Imported only during planning after main's application factory has loaded.
    from .main import JobInput, validated_job

    for original in schedules:
        key, notebook_key, workflow_key = (
            original["key"],
            original.get("notebook"),
            original.get("workflow"),
        )
        if bool(notebook_key) == bool(workflow_key):
            raise ValueError(
                "Every schedule must choose exactly one notebook or workflow key"
            )
        selected = (
            notebook_records.get(notebook_key)
            if notebook_key
            else workflow_records.get(workflow_key)
        )
        if not selected:
            raise ValueError("Schedule target was not declared in this bundle")
        payload = {
            field: item
            for field, item in original.items()
            if field not in ("key", "notebook", "workflow", "cluster")
        }
        payload.update(
            name=original.get("name", key),
            project_id=project_id,
            notebook_id=selected["id"] if notebook_key else None,
            workflow_id=selected["id"] if workflow_key else None,
            cluster_id=cluster(original.get("cluster")),
            environment_id=manifest["id"],
        )
        payload.setdefault("enabled", False)
        payload["resource_options"] = job_resources(payload.get("resource_options"))
        value = validated_job(JobInput.model_validate(payload))
        if value.get("schedule_date_parameter"):
            parameter = next(
                (
                    item
                    for item in selected.get("parameter_definitions", [])
                    if item["name"] == value["schedule_date_parameter"]
                ),
                None,
            )
            if not parameter or parameter.get("type", "string") not in (
                "date",
                "string",
            ):
                raise ValueError(
                    "The scheduled date parameter must be a declared date or string input"
                )
        if value["enabled"]:
            resolve_parameters(
                selected.get("parameter_definitions", []),
                value.get("parameters", {}),
                selected.get("parameters", {}),
            )
        record(
            "jobs",
            key,
            {
                **value,
                "notebook_name": selected["name"],
                "run_as_user_id": user["id"],
                "last_run_at": None,
            },
        )
    portable = copy.deepcopy(bundle)
    portable["environment"] = manifest
    plan_hash = hashlib.sha256(
        canonical({"bundle": portable, "target": target, "observed": observed}).encode()
    ).hexdigest()
    return {
        "name": name,
        "target": target,
        "project_id": project_id,
        "bundle_owner": bundle_owner,
        "plan_hash": plan_hash,
        "environment_id": manifest["id"],
        "counts": {
            kind: sum(change["kind"] == kind for change in changes)
            for kind in ("notebooks", "workflows", "jobs")
        },
        "changes": changes,
        "warnings": [
            "Deployment updates declared resources; omitted resources are preserved"
        ],
        "_records": records,
        "_bundle": portable,
    }
