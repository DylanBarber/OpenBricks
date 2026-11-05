"""Immutable wheel manifests; install only when a compute queue selects one."""
from __future__ import annotations

import hashlib
import json
import importlib.metadata
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

WHEEL = re.compile(r"^[A-Za-z0-9_.+-]+\.whl$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")


def runtime_identity():
    try:
        spark = importlib.metadata.version("pyspark")
    except importlib.metadata.PackageNotFoundError:
        spark = None
    return {"python": platform.python_version(), "system": platform.system(), "architecture": platform.machine(),
        "spark": spark, "build": os.getenv("OPENBRICKS_BUILD_ID", "development")}


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_manifest(value):
    wheels = value.get("wheels", [])
    if not isinstance(wheels, list) or len(wheels) > 200:
        raise ValueError("An environment supports at most 200 wheels")
    normalized, names = [], set()
    for item in wheels:
        filename, digest = item.get("filename", ""), item.get("sha256", "")
        if not WHEEL.fullmatch(filename) or not DIGEST.fullmatch(digest) or filename in names:
            raise ValueError("Invalid or duplicate wheel artifact")
        normalized.append({"filename": filename, "sha256": digest})
        names.add(filename)
    normalized.sort(key=lambda item: item["filename"])
    runtime = value.get("runtime", runtime_identity())
    if not isinstance(runtime, dict) or len(json.dumps(runtime)) > 4096:
        raise ValueError("Invalid runtime identity")
    identity = hashlib.sha256(json.dumps({"wheels": normalized, "runtime": runtime}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if value.get("id") and value["id"] != identity:
        raise ValueError("Environment identity does not match its wheel manifest")
    return {"id": identity, "wheels": normalized, "runtime": runtime}


def artifact_path(directory, item):
    if not WHEEL.fullmatch(item.get("filename", "")) or not DIGEST.fullmatch(item.get("sha256", "")):
        raise ValueError("Invalid wheel artifact")
    return Path(directory) / "artifacts" / "wheels" / item["sha256"] / item["filename"]


def save_artifact(directory, filename, content, expected=None):
    if not WHEEL.fullmatch(filename):
        raise ValueError("Invalid wheel filename")
    digest = hashlib.sha256(content).hexdigest()
    if expected is not None and expected != digest:
        raise ValueError("Wheel content does not match its pinned identity")
    item = {"filename": filename, "sha256": digest}
    path = artifact_path(directory, item)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(f".{uuid4().hex}.upload")
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return item


def capture_environment(store, project_id="default", name=None):
    items = []
    wheels = store.directory / "wheels"
    packages = store.list("packages")
    # The active package list can contain several historical versions. Keep
    # the most recently installed artifact of each distribution explicitly.
    seen = set()
    for package in sorted(packages, key=lambda value: value.get("installed_at", ""), reverse=True):
        distribution = package.get("name", package.get("filename", "")).lower().replace("_", "-")
        if distribution in seen:
            continue
        filename = package.get("filename", "")
        if not WHEEL.fullmatch(filename):
            continue
        matches = [path for path in wheels.rglob(filename) if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(wheels.resolve())]
        if not matches:
            raise ValueError("An installed wheel artifact is missing; upload or rebuild it before capturing an environment")
        path = max(matches, key=lambda value: value.stat().st_mtime)
        item = {"filename": filename, "sha256": file_hash(path)}
        target = artifact_path(store.directory, item)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(f".{uuid4().hex}.capture")
            try:
                shutil.copyfile(path, temporary)
                if file_hash(temporary) != item["sha256"]:
                    raise ValueError("Package artifacts changed during capture; retry after package installation finishes")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        items.append(item)
        seen.add(distribution)
    manifest = validate_manifest({"wheels": items})
    # Identity is content-addressed; project registrations are separate so an
    # identical public manifest never grants access across project boundaries.
    record_id = f"{project_id}:{manifest['id']}"
    from .clusters import timestamp
    previous = store.get("environments", record_id) or {}
    entry = {**manifest, "name": name or previous.get("name", "Captured environment"), "project_id": project_id, "created_at": previous.get("created_at", timestamp())}
    store.put("environments", {**entry, "id": record_id, "manifest_id": manifest["id"]})
    return entry


def get_environment(store, identity, project_id="default"):
    record = store.get("environments", f"{project_id}:{identity}")
    if not record:
        raise ValueError("Environment was not found in this project")
    return validate_manifest({"id": record["manifest_id"], "wheels": record["wheels"], "runtime": record.get("runtime", runtime_identity())})


def prepare_environment(directory, manifest):
    manifest = validate_manifest(manifest)
    if manifest["runtime"] != runtime_identity():
        raise ValueError("Pinned environment requires a different runtime build; deploy its original compatible compute image")
    root = Path(directory) / "environments" / manifest["id"]
    target = root / "site-packages"
    if (root / "ready.json").is_file():
        if json.loads((root / "ready.json").read_text(encoding="utf-8")) != manifest or not target.is_dir():
            raise ValueError("Pinned environment cache is incomplete or inconsistent")
        return target
    paths = []
    for item in manifest["wheels"]:
        path = artifact_path(directory, item)
        if not path.is_file() or file_hash(path) != item["sha256"]:
            raise ValueError("Pinned wheel artifact is missing or its content has changed")
        paths.append(path)
    staging = root.with_name(root.name + "." + uuid4().hex + ".prepare")
    packages = staging / "site-packages"
    packages.mkdir(parents=True)
    try:
        if paths:
            result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--target", str(packages), *map(str, paths)], capture_output=True, text=True, timeout=300)
            if result.returncode:
                raise ValueError("Pinned environment could not be installed; check platform and dependency compatibility")
        (staging / "ready.json").write_text(json.dumps(manifest), encoding="utf-8")
        root.parent.mkdir(parents=True, exist_ok=True)
        if root.exists():
            if not (root / "ready.json").is_file():
                raise ValueError("Pinned environment cache is incomplete; remove the incomplete cache before retrying")
            shutil.rmtree(staging)
        else:
            staging.replace(root)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)
