"""Local Anki server recovery tests; all fixture syncs stay on loopback."""

import errno
import os
import shutil
import socket
import subprocess
import sys
import json
from pathlib import Path

import httpx
import pytest

from ankiweb_cli import ankiweb_backup as backup
from ankiweb_cli.ankiweb_reader import AnkiWebReader, AnkiWebReaderError
from ankiweb_cli.backup_credentials import BackupCredentials
from tests.ankiweb_server_fixture import local_anki_environment

from anki._backend import RustBackend
from anki.collection import Collection
from anki.media import media_paths_from_col_path


@pytest.fixture
def local_anki(tmp_path, request):
    use_unix = request.node.name == "test_network_isolated_backup_can_only_reach_proxy"
    if use_unix and (sys.platform != "linux" or shutil.which("bwrap") is None):
        pytest.skip("isolated-network test requires Linux Bubblewrap")
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
    except PermissionError:
        pytest.skip("loopback sockets are unavailable in this environment")
    with local_anki_environment(tmp_path, with_unix_proxy=use_unix) as environment:
        yield environment


@pytest.fixture
def backup_setup(local_anki, tmp_path, monkeypatch):
    output = tmp_path / "backups"
    output.mkdir()
    monkeypatch.setattr(
        backup, "AnkiWebReader", lambda: AnkiWebReader(transport=local_anki.transport())
    )
    delays = []
    monkeypatch.setattr(backup.time, "sleep", delays.append)
    return output, BackupCredentials("probe", "synthetic-password"), delays


def assert_download_only(environment):
    allowed = {
        "/sync/hostKey", "/sync/meta", "/sync/download", "/msync/begin",
        "/msync/mediaChanges", "/msync/downloadFiles", "/msync/mediaSanity",
    }
    assert environment.events
    assert {path for _, path in environment.events} <= allowed
    assert all(path in allowed for path in environment.forwarded)


def test_local_fixture_supports_long_working_paths(tmp_path):
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
    except PermissionError:
        pytest.skip("loopback sockets are unavailable in this environment")
    root = tmp_path / ("long-path-" * 12)
    root.mkdir()
    with local_anki_environment(root, with_unix_proxy=False) as environment:
        with httpx.Client(transport=environment.transport(), trust_env=False) as client:
            result = client.post("https://sync.ankiweb.net/sync/upload", content=b"synthetic")
        assert result.status_code == 403


def test_proxy_socket_stays_short_with_long_tempdir(tmp_path, monkeypatch):
    import tempfile

    if sys.platform != "linux":
        pytest.skip("isolated-network socket is Linux-specific")
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
    except PermissionError:
        pytest.skip("loopback sockets are unavailable in this environment")
    long_temp = tmp_path / ("long-temp-" * 12)
    long_temp.mkdir()
    monkeypatch.setenv("TMPDIR", str(long_temp))
    monkeypatch.setattr(tempfile, "tempdir", str(long_temp))
    with local_anki_environment(tmp_path, with_unix_proxy=True) as environment:
        assert len(os.fsencode(environment.unix_socket)) < 100
        assert environment.unix_socket.exists()


def test_redirect_download_and_restore_from_local_anki(local_anki, backup_setup, tmp_path):
    output, credentials, delays = backup_setup
    reports = []
    result = backup.run_backup(output, credentials, 3, 60, reports.append)
    assert result.is_file() and delays == []
    assert {"Downloaded and verified media: referenced.png",
            "Downloaded and verified media: unreferenced.mp3"} <= set(reports)
    assert ("sync.ankiweb.net", "/msync/begin") in local_anki.events
    assert ("sync15.ankiweb.net", "/msync/begin") in local_anki.events
    assert_download_only(local_anki)
    assert not list(output.glob(".ankiweb-backup-*"))

    target = tmp_path / "restored.anki2"
    media_dir, media_db = media_paths_from_col_path(str(target))
    RustBackend().import_collection_package(
        col_path=str(target), backup_path=str(result), media_folder=media_dir, media_db=media_db
    )
    restored = Collection(str(target))
    try:
        assert restored.decks.id_for_name("Fixture::Deck")
        assert restored.note_count() == 1
        assert restored.get_note(local_anki.note_id)["Back"] == "synthetic"
        assert Path(media_dir, "referenced.png").read_bytes() == b"referenced bytes"
        assert Path(media_dir, "unreferenced.mp3").read_bytes() == b"unreferenced bytes"
    finally:
        restored.close()


@pytest.mark.parametrize(
    "path", ["/sync/upload", "/sync/start", "/sync/applyGraves", "/msync/uploadChanges"]
)
def test_proxy_rejects_forbidden_requests(local_anki, path):
    with httpx.Client(transport=local_anki.transport(), trust_env=False) as client:
        response = client.post("https://sync.ankiweb.net" + path, content=b"synthetic")
    assert response.status_code == 403
    assert ("sync.ankiweb.net", path) in local_anki.events
    assert path not in local_anki.forwarded


def test_remote_failure_retries_without_premature_publication(local_anki, backup_setup):
    output, credentials, delays = backup_setup
    local_anki.failures["/sync/download"] = 1
    local_anki.output_directory = output
    reports = []
    result = backup.run_backup(output, credentials, 3, 60, reports.append)
    assert result.is_file()
    assert delays == [5]
    assert local_anki.packages_at_failure == [[]]
    assert sum(path == "/sync/download" for _, path in local_anki.events) == 2
    assert any("failed" in report.lower() for report in reports)
    assert_download_only(local_anki)
    assert not list(output.glob(".ankiweb-backup-*"))


def test_persistent_remote_failure_never_publishes(local_anki, backup_setup):
    output, credentials, delays = backup_setup
    local_anki.failures["/msync/mediaChanges"] = 10
    local_anki.output_directory = output
    with pytest.raises(AnkiWebReaderError):
        backup.run_backup(output, credentials, 3, 60, lambda _: None)
    assert delays == [5, 10]
    assert local_anki.packages_at_failure == [[], [], []]
    assert not list(output.glob("*.colpkg"))
    assert not list(output.glob(".ankiweb-backup-*"))
    assert_download_only(local_anki)


def test_link_success_with_ambiguous_error_is_published_once(local_anki, backup_setup, monkeypatch):
    output, credentials, delays = backup_setup
    original_link = os.link
    calls = []

    def linked_then_error(source, destination):
        calls.append(destination)
        original_link(source, destination)
        raise OSError(errno.EIO, "synthetic ambiguous result")

    monkeypatch.setattr(os, "link", linked_then_error)
    result = backup.run_backup(output, credentials, 3, 60, lambda _: None)
    assert result.is_file() and len(calls) == 1 and delays == []
    assert list(output.glob("*.colpkg")) == [result]
    assert_download_only(local_anki)


def test_unresolved_publication_does_not_download_or_publish_again(
    local_anki, backup_setup, monkeypatch
):
    output, credentials, delays = backup_setup
    calls = []

    def ambiguous(source, destination):
        calls.append(destination)
        raise OSError(errno.EIO, "synthetic ambiguous result")

    monkeypatch.setattr(os, "link", ambiguous)
    reports = []
    with pytest.raises(backup.PublicationUncertainError):
        backup.run_backup(output, credentials, 3, 60, reports.append)
    assert len(calls) == 1
    assert sum(path == "/sync/download" for _, path in local_anki.events) == 1
    assert delays == [5, 10]
    assert not list(output.glob("*.colpkg"))
    workspaces = list(output.glob(".ankiweb-backup-*"))
    assert len(workspaces) == 1 and (workspaces[0] / "verified.colpkg").exists()
    assert any("uncertain" in report.lower() for report in reports)
    assert_download_only(local_anki)


def test_network_isolated_backup_can_only_reach_proxy(local_anki, tmp_path):
    bwrap = shutil.which("bwrap")
    assert bwrap is not None
    output = tmp_path / "isolated-backups"
    output.mkdir()
    local_anki.failures["/sync/download"] = 1
    local_anki.output_directory = output
    result = subprocess.run(
        [
            bwrap, "--unshare-net", "--ro-bind", "/", "/", "--bind", str(tmp_path),
            str(tmp_path), "--bind", str(local_anki.unix_socket.parent),
            str(local_anki.unix_socket.parent), "--", sys.executable,
            "-m", "tests.ankiweb_server_fixture",
            str(local_anki.unix_socket), str(local_anki.server_port), str(output),
        ],
        text=True, capture_output=True, timeout=90, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["direct_server_blocked"] is True
    assert data["delays"] == [5]
    assert Path(data["package"]).is_file()
    assert local_anki.packages_at_failure == [[]]
    assert_download_only(local_anki)
    assert sum(path == "/sync/download" for _, path in local_anki.events) == 2
