"""Read the credentials used by the unattended AnkiWeb backup."""

import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BackupCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


def load_credentials(path: Path) -> BackupCredentials:
    """Load credentials without exposing file contents in error messages."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Credentials must be a regular file")
            if sys.platform.startswith("linux") and metadata.st_mode & 0o077:
                raise ValueError("Credentials file permits group or other access")
            data = tomllib.load(stream)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("Could not read credentials file") from None

    section = data.get("ankiweb")
    if not isinstance(section, dict):
        raise ValueError("Credentials file must contain an ankiweb table")
    username = section.get("username")
    password = section.get("password")
    if not isinstance(username, str) or not username:
        raise ValueError("Credentials file must contain a nonempty username and password")
    if not isinstance(password, str) or not password:
        raise ValueError("Credentials file must contain a nonempty username and password")
    return BackupCredentials(username=username, password=password)
