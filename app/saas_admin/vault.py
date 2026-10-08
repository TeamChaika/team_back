"""Fernet encryption. The private key is a separate file, never a database value."""

import os
from pathlib import Path

from cryptography.fernet import Fernet


class Vault:
    def __init__(self, directory):
        path = Path(directory) / "credentials.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                raise ValueError("Credential key must be a private regular file") from None
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
        self.fernet = Fernet(path.read_bytes())

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.fernet.decrypt(value.encode()).decode()
