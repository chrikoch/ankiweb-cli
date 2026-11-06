"""Restricted download-only transport for the Anki sync protocol."""

import io
import json
import re
import secrets
import shutil
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

import httpx
import zstandard
from anki import buildinfo

_ALLOWED_PATHS = frozenset({
    "/sync/hostKey", "/sync/meta", "/sync/download", "/msync/begin",
    "/msync/mediaChanges", "/msync/downloadFiles", "/msync/mediaSanity",
})
_VERSION = 11
_CLIENT_VERSION = "ankiweb-cli"
_MAX_REDIRECTS = 5


def _anki_client_version() -> str:
    platform = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    return f"anki,{buildinfo.version} ({buildinfo.buildhash}),{platform}"


class AnkiWebReaderError(RuntimeError):
    """An error whose message contains no server-supplied data or credentials."""


@dataclass(frozen=True)
class CollectionMeta:
    mod: int
    scm: int
    usn: int


@dataclass(frozen=True)
class MediaChange:
    name: str
    usn: int
    sha1: str


def validate_media_name(name: str) -> None:
    """Reject paths and names Anki would normalize when exporting media."""
    try:
        invalid = (
            not name or name in (".", "..") or len(name.encode("utf-8")) > 120
            or unicodedata.normalize("NFC", name) != name
            or bool(re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", name, re.I))
            or name.endswith((" ", ".")) or "\u00a0" in name
            or any(c in '[]<>:"/?*^\\|' or ord(c) < 32 or ord(c) == 127
                   or unicodedata.category(c) == "Cn" for c in name)
        )
    except UnicodeError:
        invalid = True
    if invalid:
        raise AnkiWebReaderError("Unsupported or unsafe media filename")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class AnkiWebReader:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._endpoint = "https://sync.ankiweb.net/"
        self._hkey = ""
        self._session = secrets.token_hex(8)
        self._transport = transport

    @staticmethod
    def _trusted_endpoint(url: str) -> str:
        try:
            parts = urlsplit(url)
            host = parts.hostname
            port = parts.port
            if (
                parts.scheme != "https" or not host
                or not re.fullmatch(r"(?:[a-z0-9-]+\.)*ankiweb\.net", host)
                or port not in (None, 443) or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or parts.path not in ("", "/")
            ):
                raise ValueError
        except ValueError:
            raise AnkiWebReaderError("Untrusted sync redirect") from None
        return f"https://{host}/"

    def _request(
        self, path: str, payload: dict[str, object], output: BinaryIO | None = None
    ) -> bytes:
        if path not in _ALLOWED_PATHS:
            raise AnkiWebReaderError("Sync operation is not allowed")
        body = zstandard.ZstdCompressor().compress(json.dumps(payload).encode("utf-8"))
        headers = {
            "content-type": "application/octet-stream",
            "anki-sync": json.dumps({
                "v": _VERSION, "k": self._hkey, "c": _CLIENT_VERSION, "s": self._session,
            }),
        }
        buffer = io.BytesIO() if output is None else output
        try:
            with httpx.Client(
                transport=self._transport, follow_redirects=False, trust_env=False
            ) as client:
                for attempt in range(_MAX_REDIRECTS + 1):
                    url = urljoin(self._endpoint, path.lstrip("/"))
                    with client.stream("POST", url, content=body, headers=headers) as response:
                        if response.status_code == 308:
                            if attempt == _MAX_REDIRECTS or "location" not in response.headers:
                                raise AnkiWebReaderError("Invalid or excessive sync redirects")
                            self._endpoint = self._trusted_endpoint(
                                urljoin(url, response.headers["location"])
                            )
                            continue
                        if response.status_code != 200:
                            raise AnkiWebReaderError("Sync server returned an error")
                        size_header = response.headers.get("anki-original-size")
                        expected = int(size_header) if size_header is not None else None
                        if expected is not None and expected < 0:
                            raise ValueError
                        decoder = zstandard.ZstdDecompressor().decompressobj()
                        written = 0
                        # Small compressed chunks limit decompressor output allocation even for
                        # exceptionally compressible collections.
                        for chunk in response.iter_raw(chunk_size=64):
                            decoded = decoder.decompress(chunk)
                            buffer.write(decoded)
                            written += len(decoded)
                        if not decoder.eof or decoder.unused_data:
                            raise AnkiWebReaderError("Incomplete or invalid sync response")
                        if expected is not None and expected != written:
                            raise AnkiWebReaderError("Sync response size mismatch")
                        return buffer.getvalue() if isinstance(buffer, io.BytesIO) else b""
        except (httpx.HTTPError, zstandard.ZstdError, OSError, ValueError):
            raise AnkiWebReaderError("Sync request failed") from None
        raise AnkiWebReaderError("Sync request failed")

    def login(self, username: str, password: str) -> None:
        self._hkey = ""
        try:
            data = json.loads(self._request("/sync/hostKey", {"u": username, "p": password}))
            key = data["key"]
            if not isinstance(key, str) or not key:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise AnkiWebReaderError("Invalid login response") from None
        self._hkey = key

    def collection_meta(self) -> CollectionMeta:
        try:
            data = json.loads(
                self._request("/sync/meta", {"v": _VERSION, "cv": _anki_client_version()})
            )
            values = [data[name] for name in ("mod", "scm", "usn")]
            if any(type(value) is not int for value in values) or data.get("cont") is not True:
                raise ValueError
            return CollectionMeta(*values)
        except (ValueError, TypeError, KeyError):
            raise AnkiWebReaderError("Invalid collection metadata") from None

    def download_collection(self, destination: Path) -> None:
        try:
            with destination.open("wb") as output:
                self._request("/sync/download", {}, output)
        except OSError:
            raise AnkiWebReaderError("Could not write collection") from None

    def _media_result(self, path: str, payload: dict[str, object]) -> object:
        try:
            result = json.loads(self._request(path, payload))
            if not isinstance(result, dict) or result.get("err", "") != "":
                raise ValueError
            return result["data"]
        except (ValueError, KeyError, TypeError):
            raise AnkiWebReaderError("Invalid media response") from None

    def media_usn(self) -> int:
        result = self._media_result("/msync/begin", {"v": _CLIENT_VERSION})
        if not isinstance(result, dict) or type(result.get("usn")) is not int:
            raise AnkiWebReaderError("Invalid media USN")
        return result["usn"]  # type: ignore[no-any-return]

    def media_changes(self, after_usn: int) -> list[MediaChange]:
        data = self._media_result("/msync/mediaChanges", {"lastUsn": after_usn})
        if not isinstance(data, list):
            raise AnkiWebReaderError("Invalid media changes")
        changes = []
        for row in data:
            if (not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str)
                    or type(row[1]) is not int or not isinstance(row[2], str)
                    or (row[2] and not re.fullmatch(r"[a-fA-F0-9]{40}", row[2]))):
                raise AnkiWebReaderError("Invalid media change")
            changes.append(MediaChange(row[0], row[1], row[2]))
        return changes

    def media_sanity(self, file_count: int) -> bool:
        result = self._media_result("/msync/mediaSanity", {"local": file_count})
        if result == "OK":
            return True
        if result == "mediaSanity":
            return False
        raise AnkiWebReaderError("Invalid media sanity response")

    def download_media(self, names: list[str], destination: Path) -> set[str]:
        if not names or len(names) > 25 or len(set(names)) != len(names):
            raise AnkiWebReaderError("Invalid media batch")
        for name in names:
            validate_media_name(name)
        try:
            with tempfile.TemporaryFile(dir=destination.parent) as stream:
                self._request("/msync/downloadFiles", {"files": names}, stream)
                stream.seek(0)
                with zipfile.ZipFile(stream) as archive:
                    infos = archive.infolist()
                    entries = [info.filename for info in infos]
                    if (len(entries) > 26 or len(entries) != len(set(entries))
                            or entries.count("_meta") != 1):
                        raise ValueError
                    # Server-generated maps contain at most 25 short numeric keys and
                    # 120-byte names; this also limits a malicious compressed metadata entry.
                    if archive.getinfo("_meta").file_size > 32768:
                        raise ValueError
                    mapping = json.loads(archive.read("_meta"), object_pairs_hook=_unique_object)
                    if not isinstance(mapping, dict) or set(mapping) != set(entries) - {"_meta"}:
                        raise ValueError
                    if any(not isinstance(key, str) or not re.fullmatch(r"[0-9]{1,2}", key)
                           or not isinstance(value, str) for key, value in mapping.items()):
                        raise ValueError
                    received = set(mapping.values())
                    if len(received) != len(mapping) or not received.issubset(names):
                        raise ValueError
                    for name in received:
                        validate_media_name(name)
                    for key, name in mapping.items():
                        try:
                            with (
                                archive.open(key) as source,
                                (destination / name).open("xb") as target,
                            ):
                                shutil.copyfileobj(source, target, length=128 * 1024)
                        except FileExistsError:
                            raise AnkiWebReaderError("Media filename collision") from None
                    return received
        except AnkiWebReaderError:
            raise
        except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, RuntimeError):
            raise AnkiWebReaderError("Invalid media archive or failed media write") from None
