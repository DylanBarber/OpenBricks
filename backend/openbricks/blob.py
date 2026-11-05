"""Filesystem-backed objects for notebook code and the HTTP storage API.

``blob.path('blob://raw/events.csv')`` returns a Spark-readable local path.
Objects never leave this machine.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, unquote


BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def bucket_name(name: str) -> str:
    if not BUCKET_PATTERN.fullmatch(name):
        raise ValueError("Bucket names use 1–63 lowercase letters, numbers, dots, underscores or hyphens.")
    return name


def safe_path(root: Path, key: str, *, allow_empty: bool = False) -> Path:
    """Resolve only ordinary, relative object keys underneath root."""
    if not isinstance(key, str) or "\x00" in key or "\\" in key:
        raise ValueError("Invalid object key.")
    if key.startswith("/") or ":" in key:
        raise ValueError("Object keys must be relative paths.")
    parts = key.split("/")
    if (not key and not allow_empty) or any(p in (".", "..") for p in parts):
        raise ValueError("Object key cannot be empty or contain traversal segments.")
    if root.is_symlink():
        raise ValueError("Symlink paths are not supported.")
    root = root.resolve()
    candidate = root.joinpath(*parts)
    # Forbid symlinks even if their target is currently inside the bucket: the
    # target may be changed independently by notebook code.
    check = root
    if check.is_symlink():
        raise ValueError("Symlink paths are not supported.")
    for part in parts:
        check = check / part
        if check.is_symlink():
            raise ValueError("Symlink paths are not supported.")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Object key escapes its bucket.")
    return resolved


@dataclass
class FileInfo:
    path: str
    name: str
    size: int
    modificationTime: int

    def isDir(self) -> bool:
        return self.path.endswith("/")


class LocalBlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def bucket(self, name: str, *, create: bool = False) -> Path:
        name = bucket_name(name)
        destination = self.root / name
        if destination.is_symlink():
            raise ValueError("Symlink buckets are not supported.")
        if create:
            destination.mkdir(parents=True, exist_ok=True)
        return destination

    def _parts(self, uri: str) -> tuple[str, str]:
        if uri.startswith("dbfs:/"):
            uri = "blob://default/" + uri[6:].lstrip("/")
        parsed = urlsplit(uri)
        if parsed.scheme != "blob" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Use blob://bucket/key (or dbfs:/key for the default bucket).")
        return bucket_name(parsed.netloc), unquote(parsed.path).lstrip("/")

    def path(self, uri: str) -> str:
        name, key = self._parts(uri)
        return str(safe_path(self.bucket(name), key, allow_empty=True))

    def create_bucket(self, name: str) -> str:
        self.bucket(name, create=True)
        return "blob://" + name + "/"

    def buckets(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.is_symlink() and BUCKET_PATTERN.fullmatch(p.name))

    def read_bytes(self, uri: str) -> bytes:
        return Path(self.path(uri)).read_bytes()

    def read_text(self, uri: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(uri).decode(encoding)

    def write_bytes(self, uri: str, data: bytes, *, overwrite: bool = True) -> str:
        name, key = self._parts(uri)
        destination = safe_path(self.bucket(name, create=True), key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb" if overwrite else "xb") as stream:
            stream.write(data)
        return uri

    def write_text(self, uri: str, data: str, *, overwrite: bool = True, encoding: str = "utf-8") -> str:
        return self.write_bytes(uri, data.encode(encoding), overwrite=overwrite)

    def list(self, uri: str, *, recursive: bool = True) -> list[dict]:
        name, prefix = self._parts(uri)
        directory = Path(self.path(uri))
        bucket = self.bucket(name)
        if not directory.exists():
            return []
        paths = [directory] if directory.is_file() else (directory.rglob("*") if recursive else directory.iterdir())
        objects = []
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            # Revalidate parent symlinks, including for recursive traversal.
            key = path.relative_to(bucket).as_posix()
            safe_path(bucket, key)
            stat = path.stat()
            objects.append({"key": key, "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
        return sorted(objects, key=lambda row: row["key"])

    def delete(self, uri: str, *, recursive: bool = False) -> None:
        name, key = self._parts(uri)
        destination = safe_path(self.bucket(name), key)
        if destination.is_dir():
            if recursive:
                shutil.rmtree(destination)
            else:
                destination.rmdir()
        else:
            destination.unlink()


class LocalFS:
    """A deliberately small Databricks-style ``dbutils.fs`` helper."""
    def __init__(self, blob: LocalBlobStore):
        self.blob = blob

    def ls(self, uri: str) -> list[FileInfo]:
        directory = Path(self.blob.path(uri))
        if not directory.exists():
            raise FileNotFoundError(uri)
        paths = [directory] if directory.is_file() else sorted(directory.iterdir())
        result = []
        for path in paths:
            if path.is_symlink():
                continue
            suffix = "/" if path.is_dir() else ""
            stat = path.stat()
            object_uri = uri if directory.is_file() else uri.rstrip("/") + "/" + path.name + suffix
            result.append(FileInfo(object_uri,
                path.name + suffix, stat.st_size if path.is_file() else 0, int(stat.st_mtime * 1000)))
        return result

    def head(self, uri: str, max_bytes: int = 65536) -> str:
        with Path(self.blob.path(uri)).open("rb") as stream:
            return stream.read(max(0, min(max_bytes, 1048576))).decode("utf-8", errors="replace")

    def put(self, uri: str, contents: str, overwrite: bool = False) -> bool:
        self.blob.write_text(uri, contents, overwrite=overwrite)
        return True

    def mkdirs(self, uri: str) -> bool:
        Path(self.blob.path(uri)).mkdir(parents=True, exist_ok=True)
        return True

    def rm(self, uri: str, recurse: bool = False) -> bool:
        self.blob.delete(uri, recursive=recurse)
        return True
