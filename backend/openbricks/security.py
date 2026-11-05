"""Password/session primitives and confined filesystem paths."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path


def password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def confined_path(root: Path, relative: str, allow_root: bool = False) -> Path:
    """Reject absolute paths, traversal and symlinks, including existing ancestors."""
    if not relative and not allow_root:
        raise ValueError("A relative path is required")
    normalized = relative.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized or "\x00" in normalized:
        raise ValueError("Use a relative path inside the selected directory")
    parts = normalized.split("/")
    if ".." in parts:
        raise ValueError("Parent directory traversal is not allowed")
    base = root.resolve()
    current = base
    for part in parts:
        if part in ("", "."):
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError("Symbolic links are not allowed")
    resolved = current.resolve()
    if not resolved.is_relative_to(base) or (resolved == base and not allow_root):
        raise ValueError("Path must remain within the selected directory")
    return resolved


def bucket_name(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", value) or value in (".", ".."):
        raise ValueError("Bucket names use 1–63 lowercase letters, digits, dots, dashes or underscores")
    return value
