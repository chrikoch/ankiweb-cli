"""Enumerate and verify all downloadable AnkiWeb media."""

import hashlib
from collections.abc import Callable
from pathlib import Path

from ankiweb_cli.ankiweb_reader import (
    AnkiWebReader,
    AnkiWebReaderError,
    MediaChange,
    validate_media_name,
)


def download_all_media(
    reader: AnkiWebReader, destination: Path, report: Callable[[str], None]
) -> list[MediaChange]:
    cursor = 0
    live: dict[str, MediaChange] = {}
    while True:
        page = reader.media_changes(cursor)
        if not page:
            break
        for change in page:
            if change.usn <= cursor:
                raise AnkiWebReaderError("Media changes are not in increasing USN order")
            cursor = change.usn
            if change.sha1:
                live[change.name] = change
            else:
                live.pop(change.name, None)

    changes = list(live.values())
    for change in changes:
        validate_media_name(change.name)
    for offset in range(0, len(changes), 25):
        batch = changes[offset:offset + 25]
        pending = {change.name: change for change in batch}
        while pending:
            received = reader.download_media(list(pending), destination)
            if not received or not received.issubset(pending):
                raise AnkiWebReaderError("Media download made no progress")
            for name in received:
                try:
                    digest = hashlib.sha1()
                    with (destination / name).open("rb") as stream:
                        for chunk in iter(lambda: stream.read(128 * 1024), b""):
                            digest.update(chunk)
                except OSError:
                    raise AnkiWebReaderError("Could not verify downloaded media") from None
                if digest.hexdigest() != pending[name].sha1.lower():
                    raise AnkiWebReaderError("Media checksum mismatch")
                del pending[name]
                report(f"Downloaded and verified media: {name}")
    return changes
