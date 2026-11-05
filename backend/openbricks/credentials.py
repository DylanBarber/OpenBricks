"""Authenticated encryption for saved connector and compute credentials."""
from __future__ import annotations

import os
import time
from pathlib import Path
from threading import RLock
from cryptography.fernet import Fernet, InvalidToken

_key_lock = RLock()


class SecretBox:
    def __init__(self, data_dir: str | Path):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "secrets.key"
        configured = os.getenv("OPENBRICKS_SECRET_KEY")
        if configured:
            key = configured.encode("ascii")
        else:
            with _key_lock:
                try:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    # Another process can briefly expose the file before its
                    # first write; never replace an existing encryption key.
                    for attempt in range(20):
                        key = path.read_bytes().strip()
                        if len(key) == 44:
                            break
                        time.sleep(0.01)
                    else:
                        raise ValueError("The saved secrets.key is invalid; restore it from your data backup")
                else:
                    key = Fernet.generate_key()
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(key)
                        stream.flush()
                        os.fsync(stream.fileno())
        self.cipher = Fernet(key)

    def encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self.cipher.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Saved credentials cannot be decrypted. Restore the original secrets.key with the data volume.") from exc
