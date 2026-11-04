"""Verify a disposable AnkiWeb snapshot and export a modern collection package."""

import errno
import hashlib
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import warnings
import zipfile
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import IO

import zstandard
from anki.collection import Collection
from anki.errors import AnkiError
from anki.import_export_pb2 import MediaEntries, PackageMetadata
from anki.media import media_paths_from_col_path
from google.protobuf.message import DecodeError

from ankiweb_cli.ankiweb_reader import (
    AnkiWebReader,
    AnkiWebReaderError,
    CollectionMeta,
    MediaChange,
)
from ankiweb_cli.backup_credentials import BackupCredentials
from ankiweb_cli.backup_media import download_all_media


def _open_collection_database(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)

    def compare_unicase(left: str, right: str) -> int:
        first, second = left.casefold(), right.casefold()
        return (first > second) - (first < second)

    db.create_collation("unicase", compare_unicase)
    return db


def _collection_metadata(path: Path) -> CollectionMeta:
    try:
        with closing(_open_collection_database(path)) as db:
            checks = db.execute("PRAGMA integrity_check").fetchall()
            if checks != [("ok",)]:
                raise AnkiWebReaderError("Collection integrity check failed")
            rows = db.execute("SELECT mod, scm, usn FROM col LIMIT 2").fetchall()
            if len(rows) != 1 or any(type(value) is not int for value in rows[0]):
                raise AnkiWebReaderError("Invalid collection metadata")
            return CollectionMeta(*rows[0])
    except sqlite3.Error:
        raise AnkiWebReaderError("Invalid or unreadable collection") from None


def _decoded_chunks(stream: IO[bytes]) -> Iterator[bytes]:
    decoder = zstandard.ZstdDecompressor().decompressobj()
    while chunk := stream.read(64):
        yield decoder.decompress(chunk)
    if not decoder.eof or decoder.unused_data:
        raise AnkiWebReaderError("Invalid compressed package entry")


def _compare_collections(expected: Path, actual: Path) -> None:
    """Compare normalized collection tables, excluding volatile sync metadata."""
    with closing(_open_collection_database(expected)) as left, closing(
        _open_collection_database(actual)
    ) as right:
        query = "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        tables = {row[0] for row in left.execute(query)}
        if tables != {row[0] for row in right.execute(query)}:
            raise AnkiWebReaderError("Exported collection tables do not match")
        col_query = "SELECT id, crt, ver, dty, conf, models, decks, dconf, tags FROM col"
        if left.execute(col_query).fetchall() != right.execute(col_query).fetchall():
            raise AnkiWebReaderError("Exported collection metadata does not match")
        for table in tables - {"col"}:
            quoted = '"' + table.replace('"', '""') + '"'
            left_columns = [row[1] for row in left.execute(f"PRAGMA table_info({quoted})")]
            right_columns = [row[1] for row in right.execute(f"PRAGMA table_info({quoted})")]
            if left_columns != right_columns:
                raise AnkiWebReaderError("Exported collection schema does not match")
            order = ", ".join(
                f"{index} COLLATE BINARY" for index in range(1, len(left_columns) + 1)
            )
            rows = f"SELECT * FROM {quoted} ORDER BY {order}"
            sentinel = object()
            if any(a != b for a, b in zip_longest(
                left.execute(rows), right.execute(rows), fillvalue=sentinel
            )):
                raise AnkiWebReaderError("Exported collection data does not match")


def _validate_package(
    package: Path, collection_path: Path, media_dir: Path, changes: list[MediaChange]
) -> None:
    try:
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
            required = {"meta", "media", "collection.anki21b", "collection.anki2"}
            if len(names) != len(set(names)) or not required.issubset(names):
                raise AnkiWebReaderError("Invalid collection package contents")
            if archive.testzip() is not None:
                raise AnkiWebReaderError("Collection package checksum failed")
            metadata = PackageMetadata.FromString(archive.read("meta"))
            if metadata.version != PackageMetadata.VERSION_LATEST:
                raise AnkiWebReaderError("Collection package is not modern format")
            with archive.open("media") as source:
                manifest_data = b"".join(_decoded_chunks(source))
            manifest = MediaEntries.FromString(manifest_data)
            expected = {change.name: change for change in changes}
            if len(manifest.entries) != len(expected):
                raise AnkiWebReaderError("Collection package media count mismatch")
            seen: set[str] = set()
            for index, entry in enumerate(manifest.entries):
                if entry.name in seen or entry.name not in expected:
                    raise AnkiWebReaderError("Collection package media manifest mismatch")
                seen.add(entry.name)
                file_path = media_dir / entry.name
                digest = hashlib.sha1()
                size = 0
                with archive.open(str(index)) as source, file_path.open("rb") as original:
                    for chunk in _decoded_chunks(source):
                        if original.read(len(chunk)) != chunk:
                            raise AnkiWebReaderError("Collection package media bytes mismatch")
                        digest.update(chunk)
                        size += len(chunk)
                    if original.read(1):
                        raise AnkiWebReaderError("Collection package media bytes mismatch")
                if (size != entry.size or digest.digest() != entry.sha1
                        or digest.hexdigest() != expected[entry.name].sha1.lower()):
                    raise AnkiWebReaderError("Collection package media checksum mismatch")
            with tempfile.TemporaryDirectory(dir=package.parent) as temporary:
                extracted = Path(temporary) / "collection.anki2"
                with archive.open("collection.anki21b") as source, extracted.open("wb") as target:
                    for chunk in _decoded_chunks(source):
                        target.write(chunk)
                _collection_metadata(extracted)
                _compare_collections(collection_path, extracted)
    except AnkiWebReaderError:
        raise
    except (OSError, KeyError, ValueError, sqlite3.Error, zipfile.BadZipFile,
            zstandard.ZstdError, DecodeError):
        raise AnkiWebReaderError("Invalid or unreadable collection package") from None


def create_verified_package(
    reader: AnkiWebReader, workspace: Path, report: Callable[[str], None]
) -> Path:
    collection_path = workspace / "collection.anki2"
    package_path = workspace / "verified.colpkg"
    media_start = reader.media_usn()
    reader.download_collection(collection_path)
    original_meta = _collection_metadata(collection_path)
    media_folder, _ = media_paths_from_col_path(str(collection_path))
    media_path = Path(media_folder)
    try:
        media_path.mkdir()
        changes = download_all_media(reader, media_path, report)
        if reader.media_sanity(len(changes)) is not True:
            raise AnkiWebReaderError("Media count check failed")
        if reader.media_usn() != media_start:
            raise AnkiWebReaderError("Media changed during backup")
        if reader.collection_meta() != original_meta:
            raise AnkiWebReaderError("Collection changed during backup")
        collection = Collection(str(collection_path))
        try:
            collection.export_collection_package(
                out_path=str(package_path), include_media=True, legacy=False
            )
        finally:
            collection.close()
        _validate_package(package_path, collection_path, media_path, changes)
    except AnkiWebReaderError:
        raise
    except (AnkiError, OSError):
        raise AnkiWebReaderError("Collection package creation failed") from None
    return package_path


class PublicationError(AnkiWebReaderError):
    """Publication failed without a known completed output."""


class PublicationUncertainError(PublicationError):
    """Publication may have succeeded; reconcile this path before another attempt."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Publication outcome is uncertain for {path}")


class PublicationUnknownError(PublicationError):
    """An unexpected publication failure left the output name unknown."""

    def __init__(self, workspace: Path, output_dir: Path) -> None:
        self.workspace = workspace
        self.output_dir = output_dir
        super().__init__("Publication outcome is uncertain; inspect the output and workspace")


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _link_state(source: Path, target: Path) -> str:
    try:
        source_stat = source.stat()
        target_stat = target.stat()
    except FileNotFoundError:
        # A missing source cannot establish whether a target belongs to this attempt.
        return "absent" if source.exists() else "unknown"
    except OSError:
        return "unknown"
    if not source_stat.st_ino or not target_stat.st_ino:
        return "unknown"
    return "same" if os.path.samestat(source_stat, target_stat) else "different"


def _copy_exclusively(source: Path, target: Path) -> bool:
    """Return False on collision; preserve the path if ownership is uncertain."""
    try:
        output = target.open("xb")
    except FileExistsError:
        return False
    except OSError:
        raise PublicationUncertainError(target) from None
    try:
        with output:
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        # A stat followed by unlink cannot prevent another process from replacing
        # the name in between. Leave it for reconciliation instead of risking deletion.
        raise PublicationUncertainError(target) from None
    return True


def _publication_warning(message: str) -> None:
    try:
        warnings.warn(message, stacklevel=3)
    except (Warning, OSError, ValueError):
        try:
            print(f"Warning: {message}", file=sys.stderr)
        except (OSError, ValueError):
            pass


def publish_package(package: Path, output_dir: Path) -> Path:
    """Publish without replacing files, reconciling uncertain hard-link outcomes."""
    if not output_dir.is_dir():
        raise PublicationError("Backup output directory does not exist")
    try:
        with package.open("r+b") as source:
            os.fsync(source.fileno())
    except OSError:
        raise PublicationError("Could not sync the completed package") from None

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = 0
    use_copy = False
    unsupported = {errno.EPERM, errno.ENOSYS, errno.EXDEV, errno.EINVAL, errno.EOPNOTSUPP}
    while True:
        extension = f"-{suffix}" if suffix else ""
        target = output_dir / f"ankiweb-{stamp}{extension}.colpkg"
        if use_copy:
            if not _copy_exclusively(package, target):
                suffix += 1
                continue
            break
        try:
            os.link(package, target)
            break
        except OSError as error:
            state = _link_state(package, target)
            if state == "same":
                break
            if isinstance(error, FileExistsError) and state == "different":
                suffix += 1
                continue
            if error.errno in unsupported and state in {"absent", "different"}:
                use_copy = True
                continue
            raise PublicationUncertainError(target) from None

    try:
        _sync_directory(output_dir)
    except OSError:
        _publication_warning("Backup published, but directory durability could not be confirmed")
    try:
        if package.absolute() != target.absolute():
            package.unlink()
    except OSError:
        _publication_warning("Backup published, but temporary package cleanup failed")
    return target


def run_backup(
    output_dir: Path,
    credentials: BackupCredentials,
    attempts: int,
    max_retry_delay: float,
    report: Callable[[str], None],
) -> Path:
    """Retry isolated downloads without republishing an uncertain result."""
    if (not output_dir.is_dir() or attempts < 1 or not math.isfinite(max_retry_delay)
            or max_retry_delay < 5):
        raise ValueError("Invalid backup output directory or retry settings")

    reader: AnkiWebReader | None = None

    def safe_report(message: str) -> None:
        secrets = [credentials.username, credentials.password, getattr(reader, "_hkey", "")]
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            message = message.replace(secret, "[redacted]")
        report(message)

    delay = 5.0
    for attempt in range(1, attempts + 1):
        workspace: Path | None = None
        preserve_workspace = False
        publication_started = False
        try:
            workspace = Path(tempfile.mkdtemp(prefix=".ankiweb-backup-", dir=output_dir))
            safe_report(f"Backup attempt {attempt} of {attempts}")
            reader = AnkiWebReader()
            reader.login(credentials.username, credentials.password)
            package = create_verified_package(reader, workspace, safe_report)
            publication_started = True
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                try:
                    published = publish_package(package, output_dir)
                finally:
                    for warning in captured:
                        try:
                            safe_report(f"Warning: {warning.message}")
                        except Exception:
                            pass
            return published
        except PublicationUncertainError as uncertain:
            preserve_workspace = True
            try:
                for remaining in range(attempt, attempts + 1):
                    if _link_state(package, uncertain.path) == "same":
                        try:
                            _sync_directory(output_dir)
                        except OSError:
                            try:
                                safe_report(
                                    "Warning: Backup directory durability could not be confirmed"
                                )
                            except Exception:
                                pass
                        preserve_workspace = False
                        return uncertain.path
                    if remaining < attempts:
                        safe_report("Publication outcome is uncertain; checking again")
                        time.sleep(min(max_retry_delay, delay))
                        delay = min(max_retry_delay, delay * 2)
                safe_report(f"Publication outcome is uncertain: {uncertain.path}")
                safe_report(f"Temporary workspace preserved: {workspace}")
            except Exception:
                raise uncertain from None
            raise
        except Exception as error:
            if publication_started and not isinstance(error, PublicationError):
                preserve_workspace = True
                assert workspace is not None
                raise PublicationUnknownError(workspace, output_dir) from None
            if isinstance(error, AnkiWebReaderError):
                safe_report(f"Backup attempt failed: {error}")
            else:
                safe_report("Backup attempt failed: unexpected error")
        finally:
            if workspace is not None and not preserve_workspace:
                try:
                    shutil.rmtree(workspace)
                except OSError:
                    try:
                        safe_report(f"Warning: Could not clean temporary workspace: {workspace}")
                    except Exception:
                        pass
        if attempt < attempts:
            time.sleep(min(max_retry_delay, delay))
            delay = min(max_retry_delay, delay * 2)
    raise AnkiWebReaderError("Backup attempts exhausted")
