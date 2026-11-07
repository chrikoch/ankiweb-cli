import shutil
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import zstandard

from ankiweb_cli.ankiweb_reader import AnkiWebReader, AnkiWebReaderError
from ankiweb_cli.ankiweb_backup import create_verified_package
from tests.ankiweb_server_fixture import SyntheticAnkiServer

# Load the project patch before importing Anki modules.
from anki.collection import Collection
from anki._backend import RustBackend
from anki.media import media_paths_from_col_path


@pytest.fixture
def snapshot(tmp_path):
    source_path = tmp_path / "source.anki2"
    source = Collection(str(source_path))
    note = source.new_note(source.models.by_name("Basic"))
    note["Front"] = '<img src="picture.png">'
    note["Back"] = "first"
    source.add_note(note, source.decks.id("Parent::Child"))
    other = source.new_note(source.models.by_name("Basic"))
    other["Front"] = "second deck"
    source.add_note(other, source.decks.id("Second"))
    card = source.get_card(note.cards()[0].id)
    card.queue, card.type, card.ivl, card.due, card.factor, card.reps = 2, 2, 17, 12345, 2450, 7
    source.update_card(card)
    source.db.execute(
        "insert into revlog values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        1700000000000, card.id, -1, 3, 17, 10, 2450, 1200, 1,
    )
    note_id, other_id, card_id = note.id, other.id, card.id
    source.close()
    server = SyntheticAnkiServer()
    server.collection = source_path.read_bytes()
    with closing(sqlite3.connect(source_path)) as connection:
        mod, scm, usn = connection.execute("select mod, scm, usn from col").fetchone()
    metadata = {"mod": mod, "scm": scm, "usn": usn, "cont": True}
    server.routes["/sync/meta"] = lambda *_: server.json_response(metadata)
    server.add_media("picture.png", b"referenced media")
    server.add_media("unused.mp3", b"unreferenced media")
    reader = AnkiWebReader(transport=httpx.MockTransport(server.handle))
    reader.login("synthetic-user", "synthetic-password")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return reader, server, workspace, metadata, (note_id, other_id, card_id)


def test_verified_package_restores_collection_and_all_media(snapshot, tmp_path):
    reader, server, workspace, metadata, (note_id, other_id, card_id) = snapshot
    reports = []
    package = create_verified_package(reader, workspace, reports.append)
    assert package.parent == workspace
    assert {"Downloaded and verified media: picture.png",
            "Downloaded and verified media: unused.mp3"}.issubset(reports)
    assert "Downloading collection" in reports
    assert "Creating and verifying collection package" in reports
    with zipfile.ZipFile(package) as archive:
        assert {"collection.anki21b", "meta", "media"}.issubset(archive.namelist())
    target_path = tmp_path / "restored.anki2"
    media_dir, media_db = media_paths_from_col_path(str(target_path))
    RustBackend().import_collection_package(
        col_path=str(target_path), backup_path=str(package),
        media_folder=media_dir, media_db=media_db,
    )
    restored = Collection(str(target_path))
    try:
        assert restored.note_count() == 2
        assert restored.decks.id_for_name("Parent::Child")
        assert restored.decks.id_for_name("Second")
        assert restored.get_note(note_id)["Back"] == "first"
        assert restored.get_note(other_id)["Front"] == "second deck"
        card = restored.get_card(card_id)
        assert (card.queue, card.type, card.ivl, card.due, card.factor, card.reps) == (
            2, 2, 17, 12345, 2450, 7
        )
        assert [tuple(row) for row in restored.db.all(
            "select id, cid, usn, ease, ivl, lastIvl, factor, time, type from revlog where cid = ?",
            card_id,
        )] == [(1700000000000, card_id, -1, 3, 17, 10, 2450, 1200, 1)]
        assert Path(media_dir, "picture.png").read_bytes() == b"referenced media"
        assert Path(media_dir, "unused.mp3").read_bytes() == b"unreferenced media"
    finally:
        restored.close()
    paths = [request.url.path for request in server.requests]
    assert paths.index("/msync/begin") < paths.index("/sync/download")
    assert paths.index("/sync/meta") < paths.index("/sync/download")
    assert paths.index("/msync/mediaSanity") < max(
        index for index, path in enumerate(paths) if path == "/sync/meta"
    )


def test_resolves_shard_before_collection_download(snapshot):
    reader, server, workspace, metadata, _ = snapshot

    def redirect_meta(request, payload, header):
        if request.url.host == "sync.ankiweb.net":
            return httpx.Response(308, headers={"Location": "https://sync15.ankiweb.net/"})
        return server.json_response(metadata)

    def download(request, payload, header):
        if request.url.host == "sync.ankiweb.net":
            return httpx.Response(303)
        return server.response(server.collection)

    server.routes["/sync/meta"] = redirect_meta
    server.routes["/sync/download"] = download
    package = create_verified_package(reader, workspace, lambda _: None)
    assert package.is_file()
    downloads = [request for request in server.requests if request.url.path == "/sync/download"]
    assert len(downloads) == 1 and downloads[0].url.host == "sync15.ankiweb.net"


@pytest.mark.parametrize("field", ["mod", "scm", "usn"])
def test_changed_collection_metadata_fails(snapshot, field):
    reader, _, workspace, metadata, _ = snapshot
    metadata[field] += 1
    with pytest.raises(AnkiWebReaderError):
        create_verified_package(reader, workspace, lambda _: None)
    assert not list(workspace.glob("*.colpkg"))


def test_changed_media_usn_fails(snapshot):
    reader, server, workspace, _, _ = snapshot
    counts = iter([2, 3])
    server.routes["/msync/begin"] = lambda *_: server.json_response({"data": {"usn": next(counts)}})
    with pytest.raises(AnkiWebReaderError):
        create_verified_package(reader, workspace, lambda _: None)
    assert not list(workspace.glob("*.colpkg"))


def test_media_count_mismatch_fails(snapshot):
    reader, server, workspace, _, _ = snapshot
    server.routes["/msync/mediaSanity"] = lambda *_: server.json_response({"data": "mediaSanity"})
    with pytest.raises(AnkiWebReaderError):
        create_verified_package(reader, workspace, lambda _: None)
    assert not list(workspace.glob("*.colpkg"))


@pytest.mark.parametrize("path", ["/msync/begin", "/msync/mediaSanity", "/sync/meta"])
def test_missing_required_checks_fail(snapshot, path):
    reader, server, workspace, _, _ = snapshot
    server.routes[path] = lambda *_: server.json_response({})
    with pytest.raises(AnkiWebReaderError):
        create_verified_package(reader, workspace, lambda _: None)
    assert not list(workspace.glob("*.colpkg"))


def test_invalid_downloaded_collection_fails(snapshot):
    reader, server, workspace, _, _ = snapshot
    server.collection = b"not a sqlite collection"
    with pytest.raises(AnkiWebReaderError):
        create_verified_package(reader, workspace, lambda _: None)
    assert not list(workspace.glob("*.colpkg"))


@pytest.mark.parametrize(
    "alteration", [
        "missing_collection", "missing_media", "bad_media", "different_collection",
        "different_col_config",
    ]
)
def test_corrupt_exported_package_is_rejected(snapshot, monkeypatch, alteration):
    reader, _, workspace, _, _ = snapshot
    original_export = Collection.export_collection_package

    def corrupt_export(collection, out_path, include_media, legacy):
        original_export(collection, out_path=out_path, include_media=include_media, legacy=legacy)
        package = Path(out_path)
        with zipfile.ZipFile(package) as archive:
            entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
        with zipfile.ZipFile(package, "w") as archive:
            for name, data in entries:
                if alteration == "missing_collection" and name == "collection.anki21b":
                    continue
                if alteration == "missing_media" and name == "media":
                    continue
                if alteration == "bad_media" and name == "0":
                    data = b"invalid compressed media"
                if (alteration in {"different_collection", "different_col_config"}
                        and name == "collection.anki21b"):
                    different = workspace / "different.anki2"
                    shutil.copyfile(workspace / "collection.anki2", different)
                    with closing(sqlite3.connect(different)) as db:
                        if alteration == "different_collection":
                            db.execute("update notes set flds = ?", ("different content",))
                        else:
                            db.execute("update col set conf = ?", ('{"changed": true}',))
                        db.commit()
                    data = zstandard.ZstdCompressor().compress(different.read_bytes())
                archive.writestr(name, data)

    monkeypatch.setattr(Collection, "export_collection_package", corrupt_export)
    with pytest.raises(AnkiWebReaderError):
        create_verified_package(reader, workspace, lambda _: None)


def _publish_in_process(output_dir, package_path, result_queue, use_copy):
    import errno
    import os
    from pathlib import Path

    from ankiweb_cli.ankiweb_backup import publish_package

    if use_copy:
        def unsupported(*args, **kwargs):
            raise OSError(errno.EOPNOTSUPP, "synthetic unsupported link")
        os.link = unsupported
    try:
        path = publish_package(Path(package_path), Path(output_dir))
        result_queue.put((str(path), None))
    except Exception as error:
        result_queue.put((None, repr(error)))


def test_publication_preserves_existing_name_and_selects_suffix(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from ankiweb_cli import ankiweb_backup as backup

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 4, 10, 20, 30, tzinfo=UTC)

    monkeypatch.setattr(backup, "datetime", FixedDatetime)
    existing = tmp_path / "ankiweb-20261104T102030Z.colpkg"
    existing.write_bytes(b"existing backup")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    package = workspace / "verified.colpkg"
    package.write_bytes(b"new backup")
    result = backup.publish_package(package, tmp_path)
    assert result == tmp_path / "ankiweb-20261104T102030Z-1.colpkg"
    assert existing.read_bytes() == b"existing backup"
    assert result.read_bytes() == b"new backup"


@pytest.mark.parametrize("use_copy", [False, True])
def test_concurrent_processes_publish_without_overwriting(tmp_path, use_copy):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = []
    payloads = set()
    for index in range(6):
        workspace = tmp_path / f"workspace-{index}"
        workspace.mkdir()
        package = workspace / "verified.colpkg"
        data = f"unique package {index}".encode() * 1000
        package.write_bytes(data)
        payloads.add(data)
        process = context.Process(
            target=_publish_in_process, args=(str(tmp_path), str(package), queue, use_copy)
        )
        processes.append(process)
        process.start()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert all(error is None for _, error in results), results
    paths = [Path(path) for path, _ in results]
    assert len(set(paths)) == len(processes)
    assert {path.read_bytes() for path in paths} == payloads


def test_successful_link_cleanup_failure_reports_published_path(tmp_path, monkeypatch):
    from ankiweb_cli.ankiweb_backup import publish_package

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    package = workspace / "verified.colpkg"
    package.write_bytes(b"complete package")
    original_unlink = Path.unlink

    def fail_cleanup(path, *args, **kwargs):
        if path == package:
            raise PermissionError("synthetic cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with pytest.warns(UserWarning, match="cleanup"):
        result = publish_package(package, tmp_path)
    assert result.read_bytes() == b"complete package"
    assert package.read_bytes() == b"complete package"
    assert len(list(tmp_path.glob("*.colpkg"))) == 1


def test_directory_sync_failure_warns_without_republishing(tmp_path, monkeypatch):
    from ankiweb_cli import ankiweb_backup as backup

    package = tmp_path / "source"
    package.write_bytes(b"complete package")
    monkeypatch.setattr(backup, "_sync_directory", lambda _: (_ for _ in ()).throw(OSError("fail")))
    with pytest.warns(UserWarning, match="durability"):
        result = backup.publish_package(package, tmp_path)
    assert result.read_bytes() == b"complete package"
    assert len(list(tmp_path.glob("*.colpkg"))) == 1


def test_link_error_after_success_is_reconciled_without_second_backup(tmp_path, monkeypatch):
    import errno
    import os

    from ankiweb_cli.ankiweb_backup import publish_package

    package = tmp_path / "source"
    package.write_bytes(b"complete package")
    real_link = os.link

    def completed_then_error(source, destination):
        real_link(source, destination)
        raise OSError(errno.EIO, "synthetic ambiguous error")

    monkeypatch.setattr(os, "link", completed_then_error)
    result = publish_package(package, tmp_path)
    assert result.read_bytes() == b"complete package"
    assert len(list(tmp_path.glob("*.colpkg"))) == 1


def test_ambiguous_link_error_does_not_retry_or_overwrite(tmp_path, monkeypatch):
    import errno
    import os

    from ankiweb_cli.ankiweb_backup import PublicationUncertainError, publish_package

    package = tmp_path / "source"
    package.write_bytes(b"complete package")
    calls = []

    def ambiguous(source, destination):
        calls.append(destination)
        Path(destination).write_bytes(b"other backup")
        raise OSError(errno.EIO, "synthetic ambiguous error")

    monkeypatch.setattr(os, "link", ambiguous)
    with pytest.raises(PublicationUncertainError) as error:
        publish_package(package, tmp_path)
    assert len(calls) == 1
    assert error.value.path == Path(calls[0])
    assert Path(calls[0]).read_bytes() == b"other backup"
    assert package.read_bytes() == b"complete package"


def test_exclusive_copy_failure_reports_partial_file(tmp_path, monkeypatch):
    import errno
    import os
    import shutil

    from ankiweb_cli.ankiweb_backup import PublicationUncertainError, publish_package

    package = tmp_path / "source"
    package.write_bytes(b"complete package")
    existing = tmp_path / "existing.colpkg"
    existing.write_bytes(b"old backup")
    monkeypatch.setattr(
        os, "link", lambda *_: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "no"))
    )

    def fail_copy(source, target, length=0):
        target.write(b"partial")
        raise OSError(errno.EIO, "synthetic copy failure")

    monkeypatch.setattr(shutil, "copyfileobj", fail_copy)
    with pytest.raises(PublicationUncertainError) as error:
        publish_package(package, tmp_path)
    assert existing.read_bytes() == b"old backup"
    assert error.value.path.read_bytes() == b"partial"
    assert package.read_bytes() == b"complete package"


@pytest.mark.parametrize("deny_unlink", [False, True])
def test_copy_failure_does_not_delete_a_replaced_file(tmp_path, monkeypatch, deny_unlink):
    import errno
    import os
    import shutil

    from ankiweb_cli.ankiweb_backup import PublicationUncertainError, publish_package

    package = tmp_path / "source"
    package.write_bytes(b"complete package")
    replacement = tmp_path / "existing.colpkg"
    replacement.write_bytes(b"preserve this backup")
    blocked = []

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    def replace_then_fail(source, target, length=0):
        path = Path(target.name)
        try:
            if deny_unlink:
                raise PermissionError("synthetic open-file restriction")
            path.unlink()
        except PermissionError:
            blocked.append(True)
            raise
        os.replace(replacement, path)
        raise OSError(errno.EIO, "synthetic failure")

    monkeypatch.setattr(os, "link", unsupported)
    monkeypatch.setattr(shutil, "copyfileobj", replace_then_fail)
    with pytest.raises(PublicationUncertainError) as error:
        publish_package(package, tmp_path)
    if blocked:
        assert replacement.read_bytes() == b"preserve this backup"
        assert error.value.path.read_bytes() == b""
    else:
        assert error.value.path.read_bytes() == b"preserve this backup"


def test_warnings_as_errors_do_not_hide_successful_publication(tmp_path, monkeypatch):
    import warnings

    from ankiweb_cli import ankiweb_backup as backup

    package = tmp_path / "source"
    package.write_bytes(b"complete package")
    monkeypatch.setattr(backup, "_sync_directory", lambda _: (_ for _ in ()).throw(OSError("fail")))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        published = backup.publish_package(package, tmp_path)
    assert published.read_bytes() == b"complete package"


def test_warning_output_failure_does_not_hide_successful_publication(tmp_path, monkeypatch):
    from ankiweb_cli import ankiweb_backup as backup

    package = tmp_path / "source"
    package.write_bytes(b"complete package")
    monkeypatch.setattr(backup, "_sync_directory", lambda _: (_ for _ in ()).throw(OSError("fail")))
    monkeypatch.setattr(
        backup.warnings, "warn", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fail"))
    )
    published = backup.publish_package(package, tmp_path)
    assert published.read_bytes() == b"complete package"


def test_package_is_opened_with_write_access_for_sync(tmp_path, monkeypatch):
    from ankiweb_cli.ankiweb_backup import publish_package

    package = tmp_path / "source"
    package.write_bytes(b"complete package")
    original_open = Path.open
    modes = []

    def record_open(path, mode="r", *args, **kwargs):
        if path == package:
            modes.append(mode)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", record_open)
    publish_package(package, tmp_path)
    assert modes[0] == "r+b"


def _interrupt_copy_in_process(output_dir, package_path):
    import errno
    import os
    import shutil

    from ankiweb_cli.ankiweb_backup import publish_package

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    def interrupted(source, target, length=0):
        target.write(b"partial")
        target.flush()
        os._exit(17)

    os.link = unsupported
    shutil.copyfileobj = interrupted
    publish_package(Path(package_path), Path(output_dir))


def test_interrupted_copy_can_leave_partial_file(tmp_path):
    import multiprocessing

    package = tmp_path / "source"
    package.write_bytes(b"complete package")
    existing = tmp_path / "existing.colpkg"
    existing.write_bytes(b"old backup")
    process = multiprocessing.get_context("spawn").Process(
        target=_interrupt_copy_in_process, args=(str(tmp_path), str(package))
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 17
    partials = [path for path in tmp_path.glob("ankiweb-*.colpkg")]
    assert len(partials) == 1 and partials[0].read_bytes() == b"partial"
    assert existing.read_bytes() == b"old backup"


def test_run_backup_retries_with_fresh_workspaces_and_bounded_delays(tmp_path, monkeypatch):
    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.backup_credentials import BackupCredentials

    credentials = BackupCredentials("synthetic-user", "synthetic-password")
    workspaces = []
    sleeps = []
    reports = []

    class FakeReader:
        def login(self, username, password):
            assert (username, password) == (credentials.username, credentials.password)

    def make_package(reader, workspace, report):
        assert workspace.parent == tmp_path
        assert workspace.is_dir()
        workspaces.append(workspace)
        if len(workspaces) < 4:
            raise AnkiWebReaderError("Collection changed during backup")
        report("unreferenced.mp3")
        package = workspace / "verified.colpkg"
        package.write_bytes(b"complete package")
        return package

    monkeypatch.setattr(backup, "AnkiWebReader", FakeReader)
    monkeypatch.setattr(backup, "create_verified_package", make_package)
    monkeypatch.setattr(backup.time, "sleep", sleeps.append)
    result = backup.run_backup(tmp_path, credentials, 4, 7, reports.append)
    assert result.read_bytes() == b"complete package"
    assert len(set(workspaces)) == 4
    assert all(not path.exists() for path in workspaces)
    assert sleeps == [5, 7, 7]
    assert any("Collection changed" in line for line in reports)
    assert "unreferenced.mp3" in reports


def test_run_backup_reports_workspace_creation_failure(tmp_path, monkeypatch):
    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.backup_credentials import BackupCredentials

    def fail(*args, **kwargs):
        raise PermissionError("sensitive filesystem details")

    monkeypatch.setattr(backup.tempfile, "mkdtemp", fail)
    reports = []
    with pytest.raises(AnkiWebReaderError):
        backup.run_backup(tmp_path, BackupCredentials("user", "password"), 1, 5, reports.append)
    assert any("temporary workspace" in line and "output directory" in line for line in reports)
    assert "sensitive filesystem details" not in "\n".join(reports)


def test_run_backup_exhaustion_cleans_attempts_and_redacts_errors(tmp_path, monkeypatch):
    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.backup_credentials import BackupCredentials

    class FakeReader:
        _hkey = "synthetic-hkey"

        def login(self, username, password):
            pass

    workspaces, sleeps, reports = [], [], []

    def fail(reader, workspace, report):
        workspaces.append(workspace)
        raise AnkiWebReaderError("synthetic-password synthetic-user synthetic-hkey")

    monkeypatch.setattr(backup, "AnkiWebReader", FakeReader)
    monkeypatch.setattr(backup, "create_verified_package", fail)
    monkeypatch.setattr(backup.time, "sleep", sleeps.append)
    with pytest.raises(AnkiWebReaderError) as error:
        backup.run_backup(
            tmp_path, BackupCredentials("synthetic-user", "synthetic-password"), 3, 60,
            reports.append,
        )
    output = "\n".join(reports) + str(error.value)
    assert not any(
        value in output for value in ("synthetic-user", "synthetic-password", "synthetic-hkey")
    )
    assert len(workspaces) == 3 and all(not path.exists() for path in workspaces)
    assert sleeps == [5, 10]
    assert not list(tmp_path.glob("*.colpkg"))


def test_ambiguous_publication_reconciles_without_publishing_again(tmp_path, monkeypatch):
    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.backup_credentials import BackupCredentials

    class FakeReader:
        def login(self, username, password):
            pass

    package_paths, link_checks, sleeps = [], [], []
    target = tmp_path / "uncertain.colpkg"

    def make_package(reader, workspace, report):
        package = workspace / "verified.colpkg"
        package.write_bytes(b"complete")
        package_paths.append(package)
        return package

    def uncertain(package, output_dir):
        raise backup.PublicationUncertainError(target)

    def reconcile(source, destination):
        link_checks.append((source, destination))
        return "unknown"

    monkeypatch.setattr(backup, "AnkiWebReader", FakeReader)
    monkeypatch.setattr(backup, "create_verified_package", make_package)
    monkeypatch.setattr(backup, "publish_package", uncertain)
    monkeypatch.setattr(backup, "_link_state", reconcile)
    monkeypatch.setattr(backup.time, "sleep", sleeps.append)
    with pytest.raises(backup.PublicationUncertainError) as error:
        backup.run_backup(tmp_path, BackupCredentials("user", "password"), 3, 60, lambda _: None)
    assert error.value.path == target
    assert len(package_paths) == 1 and package_paths[0].exists()
    assert len(link_checks) == 3
    assert sleeps == [5, 10]


def test_ambiguous_link_later_resolves_to_published_file(tmp_path, monkeypatch):
    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.backup_credentials import BackupCredentials

    class FakeReader:
        def login(self, username, password):
            pass

    paths = []
    states = iter(["unknown", "same"])
    target = tmp_path / "published.colpkg"

    def make_package(reader, workspace, report):
        package = workspace / "verified.colpkg"
        package.write_bytes(b"complete")
        paths.append(package)
        return package

    def uncertain(package, output_dir):
        target.hardlink_to(package)
        raise backup.PublicationUncertainError(target)

    monkeypatch.setattr(backup, "AnkiWebReader", FakeReader)
    monkeypatch.setattr(backup, "create_verified_package", make_package)
    monkeypatch.setattr(backup, "publish_package", uncertain)
    monkeypatch.setattr(backup, "_link_state", lambda *_: next(states))
    monkeypatch.setattr(backup.time, "sleep", lambda _: None)
    assert backup.run_backup(tmp_path, BackupCredentials("u", "p"), 3, 60, lambda _: None) == target
    assert target.read_bytes() == b"complete"
    assert not paths[0].parent.exists()


def test_publication_warnings_and_secret_media_names_are_reported_safely(tmp_path, monkeypatch):
    import warnings

    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.backup_credentials import BackupCredentials

    class FakeReader:
        _hkey = "synthetic-hkey"

        def login(self, username, password):
            pass

    def make_package(reader, workspace, report):
        report("file-synthetic-password-synthetic-hkey.mp3")
        package = workspace / "verified.colpkg"
        package.write_bytes(b"complete")
        return package

    def publish(package, output_dir):
        warnings.warn("directory durability uncertain", stacklevel=2)
        return output_dir / "published.colpkg"

    monkeypatch.setattr(backup, "AnkiWebReader", FakeReader)
    monkeypatch.setattr(backup, "create_verified_package", make_package)
    monkeypatch.setattr(backup, "publish_package", publish)
    reports = []
    backup.run_backup(
        tmp_path, BackupCredentials("synthetic-user", "synthetic-password"), 1, 5, reports.append
    )
    output = "\n".join(reports)
    assert "synthetic-password" not in output and "synthetic-hkey" not in output
    assert "directory durability uncertain" in output


def test_cli_creates_and_publishes_synthetic_package(snapshot, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.cli import main

    reader, server, _, _, _ = snapshot
    credentials = tmp_path / "credentials.toml"
    credentials.write_text(
        '[ankiweb]\nusername="synthetic-user"\npassword="synthetic-password"\n'
    )
    credentials.chmod(0o600)
    monkeypatch.setattr(backup, "AnkiWebReader", lambda: reader)
    result = CliRunner().invoke(main, [
        "ankiweb-backup", "--output-dir", str(tmp_path), "--credentials-file", str(credentials),
    ])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert "picture.png" in result.stdout and "unused.mp3" in result.stdout
    assert not any(
        value in result.output
        for value in ("synthetic-user", "synthetic-password", "synthetic-hkey")
    )
    packages = list(tmp_path.glob("ankiweb-*.colpkg"))
    assert len(packages) == 1 and str(packages[0]) in result.stdout
    with zipfile.ZipFile(packages[0]) as archive:
        assert "collection.anki21b" in archive.namelist()
    assert not list(tmp_path.glob(".ankiweb-backup-*"))


def test_reporting_failure_after_publication_does_not_publish_twice(tmp_path, monkeypatch):
    import warnings

    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.backup_credentials import BackupCredentials

    class FakeReader:
        def login(self, username, password):
            pass

    calls = []
    output = tmp_path / "published.colpkg"

    def make_package(reader, workspace, report):
        package = workspace / "verified.colpkg"
        package.write_bytes(b"complete")
        return package

    def publish(package, output_dir):
        calls.append(package)
        output.write_bytes(package.read_bytes())
        warnings.warn("cleanup failed", stacklevel=2)
        return output

    def report(message):
        if "cleanup failed" in message:
            raise OSError("output stream failed")

    monkeypatch.setattr(backup, "AnkiWebReader", FakeReader)
    monkeypatch.setattr(backup, "create_verified_package", make_package)
    monkeypatch.setattr(backup, "publish_package", publish)
    monkeypatch.setattr(backup.time, "sleep", lambda _: None)
    result = backup.run_backup(tmp_path, BackupCredentials("user", "password"), 3, 5, report)
    assert result == output
    assert len(calls) == 1 and output.read_bytes() == b"complete"


def test_uncertain_publication_reporting_failure_does_not_retry(tmp_path, monkeypatch):
    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.backup_credentials import BackupCredentials

    class FakeReader:
        def login(self, username, password):
            pass

    calls = []

    def make_package(reader, workspace, report):
        package = workspace / "verified.colpkg"
        package.write_bytes(b"complete")
        return package

    def publish(package, output_dir):
        calls.append(package)
        raise backup.PublicationUncertainError(output_dir / "uncertain.colpkg")

    def report(message):
        if "uncertain" in message:
            raise OSError("output stream failed")

    monkeypatch.setattr(backup, "AnkiWebReader", FakeReader)
    monkeypatch.setattr(backup, "create_verified_package", make_package)
    monkeypatch.setattr(backup, "publish_package", publish)
    monkeypatch.setattr(backup, "_link_state", lambda *_: "unknown")
    with pytest.raises(AnkiWebReaderError):
        backup.run_backup(tmp_path, BackupCredentials("user", "password"), 3, 5, report)
    assert len(calls) == 1 and calls[0].exists()


def test_workspace_creation_failure_is_retried(tmp_path, monkeypatch):
    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.backup_credentials import BackupCredentials

    class FakeReader:
        def login(self, username, password):
            pass

    make_temp = backup.tempfile.mkdtemp
    attempts, sleeps = [], []

    def flaky_mkdtemp(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("synthetic failure")
        return make_temp(*args, **kwargs)

    def make_package(reader, workspace, report):
        package = workspace / "verified.colpkg"
        package.write_bytes(b"complete")
        return package

    monkeypatch.setattr(backup.tempfile, "mkdtemp", flaky_mkdtemp)
    monkeypatch.setattr(backup, "AnkiWebReader", FakeReader)
    monkeypatch.setattr(backup, "create_verified_package", make_package)
    monkeypatch.setattr(backup.time, "sleep", sleeps.append)
    result = backup.run_backup(
        tmp_path, BackupCredentials("user", "password"), 2, 60, lambda _: None
    )
    assert result.read_bytes() == b"complete"
    assert len(attempts) == 2 and sleeps == [5]


def test_unexpected_publication_failure_reports_uncertainty_and_workspace(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.cli import main

    class FakeReader:
        def login(self, username, password):
            pass

    credentials = tmp_path / "credentials.toml"
    credentials.write_text('[ankiweb]\nusername="user"\npassword="secret"\n')
    credentials.chmod(0o600)
    calls = []

    def make_package(reader, workspace, report):
        package = workspace / "verified.colpkg"
        package.write_bytes(b"complete")
        return package

    def fail_after_publication(package, output_dir):
        calls.append(package)
        (output_dir / "maybe-published.colpkg").write_bytes(b"complete")
        raise RuntimeError("synthetic-hkey secret")

    monkeypatch.setattr(backup, "AnkiWebReader", FakeReader)
    monkeypatch.setattr(backup, "create_verified_package", make_package)
    monkeypatch.setattr(backup, "publish_package", fail_after_publication)
    result = CliRunner().invoke(main, [
        "ankiweb-backup", "--output-dir", str(tmp_path), "--credentials-file", str(credentials),
    ])
    assert result.exit_code != 0 and result.stderr == ""
    assert "uncertain" in result.stdout.lower()
    assert len(calls) == 1 and calls[0].exists()
    assert str(calls[0].parent) in result.stdout
    assert "synthetic-hkey" not in result.output and "secret" not in result.output
