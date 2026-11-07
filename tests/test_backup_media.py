import io
import warnings
import zipfile
from pathlib import Path

import httpx
import pytest
import zstandard

from ankiweb_cli.ankiweb_reader import AnkiWebReader, AnkiWebReaderError
from ankiweb_cli.backup_media import download_all_media
from tests.ankiweb_server_fixture import SyntheticAnkiServer, media_zip


@pytest.fixture
def media_server():
    server = SyntheticAnkiServer()
    reader = AnkiWebReader(transport=httpx.MockTransport(server.handle))
    reader.login("synthetic-user", "synthetic-password")
    return reader, server


def test_pagination_batches_partial_zip_unreferenced_and_progress(media_server, tmp_path):
    reader, server = media_server
    for index in range(1002):
        server.add_media(f"file-{index}.txt", f"contents {index}".encode())
    server.add_media("unreferenced.mp3", b"unreferenced")
    server.batch_limit = 13
    reported = []
    changes = download_all_media(reader, tmp_path, reported.append)
    assert len(changes) == 1003
    assert set(reported) == {f"Downloaded and verified media: {name}" for name in server.media}
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == server.media
    requests = [request for request in server.requests if request.url.path == "/msync/mediaChanges"]
    assert len(requests) == 3


def test_empty_media(media_server, tmp_path):
    reader, _ = media_server
    assert download_all_media(reader, tmp_path, lambda _: None) == []
    assert list(tmp_path.iterdir()) == []


def test_deletions_remove_prior_records(media_server, tmp_path):
    reader, server = media_server
    server.add_media("gone.txt", b"gone")
    del server.media["gone.txt"]
    server.changes.append(["gone.txt", 2, ""])
    server.add_media("kept.txt", b"kept")
    changes = download_all_media(reader, tmp_path, lambda _: None)
    assert [change.name for change in changes] == ["kept.txt"]
    assert [file.name for file in tmp_path.iterdir()] == ["kept.txt"]


def test_deleted_legacy_name_does_not_block_backup(media_server, tmp_path):
    reader, server = media_server
    server.add_media("CON.jpg", b"old")
    del server.media["CON.jpg"]
    server.changes.append(["CON.jpg", 2, ""])
    assert download_all_media(reader, tmp_path, lambda _: None) == []


def test_oversized_zip_metadata_is_rejected(media_server, tmp_path):
    reader, server = media_server
    server.add_media("a", b"a")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("0", b"a")
        archive.writestr("_meta", b'{"0":"a"}' + b" " * 40000)
    server.routes["/msync/downloadFiles"] = lambda *_: server.response(stream.getvalue())
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)


def test_truncated_compressed_media_response_fails(media_server, tmp_path):
    reader, server = media_server
    server.add_media("a", b"a")
    body = zstandard.ZstdCompressor().compress(media_zip([("a", b"a")]))[:-1]
    server.routes["/msync/downloadFiles"] = lambda *_: httpx.Response(200, content=body)
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)


@pytest.mark.parametrize("rows", [
    [["a", 0, "a" * 40]], [["a", 1, "a" * 40], ["b", 1, "b" * 40]],
    [["a", 2, "a" * 40], ["b", 1, "b" * 40]],
])
def test_non_increasing_media_usns_fail(media_server, tmp_path, rows):
    reader, server = media_server
    server.routes["/msync/mediaChanges"] = lambda *_: server.json_response({"data": rows})
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)


def test_bad_hash_fails(media_server, tmp_path):
    reader, server = media_server
    server.add_media("a", b"original")
    server.media["a"] = b"modified"
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", "CON.jpg", "e\u0301.png", "bad?.jpg"])
def test_unsafe_or_legacy_names_fail(media_server, tmp_path, name):
    reader, server = media_server
    server.add_media(name, b"content")
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)
    assert list(tmp_path.iterdir()) == []


def test_case_distinct_names_are_preserved_on_supporting_filesystems(media_server, tmp_path):
    reader, server = media_server
    server.add_media("Test.png", b"upper")
    server.add_media("test.png", b"lower")
    try:
        download_all_media(reader, tmp_path, lambda _: None)
    except AnkiWebReaderError:
        if ((tmp_path / "Test.png").exists()
                and (tmp_path / "test.png").samefile(tmp_path / "Test.png")):
            pytest.skip("filesystem is case-insensitive")
        raise
    assert (tmp_path / "Test.png").read_bytes() == b"upper"
    assert (tmp_path / "test.png").read_bytes() == b"lower"


def test_existing_destination_is_not_overwritten(media_server, tmp_path):
    reader, server = media_server
    server.add_media("a", b"remote")
    (tmp_path / "a").write_bytes(b"existing")
    with pytest.raises(AnkiWebReaderError, match="collision"):
        download_all_media(reader, tmp_path, lambda _: None)
    assert (tmp_path / "a").read_bytes() == b"existing"


def test_filesystem_case_collision_is_reported(media_server, tmp_path, monkeypatch):
    reader, server = media_server
    server.add_media("Test.png", b"upper")
    server.add_media("test.png", b"lower")
    original_open = Path.open

    def collision_open(path, mode="r", *args, **kwargs):
        if path.name == "test.png" and mode == "xb":
            raise FileExistsError("simulated case-insensitive collision")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", collision_open)
    with pytest.raises(AnkiWebReaderError, match="collision"):
        download_all_media(reader, tmp_path, lambda _: None)
    assert (tmp_path / "Test.png").read_bytes() == b"upper"


@pytest.mark.parametrize("archive", [
    media_zip([], metadata={"0": "a"}),
    media_zip([("a", b"a")], metadata={}),
    media_zip([("a", b"a")], entries=[("extra", b"x")]),
    media_zip([("a", b"a")], metadata={"0": "../escape"}),
    media_zip([("a", b"a")], metadata={"0": "unexpected"}),
    b"not a zip",
    media_zip([("a", b"a")])[:-10],
])
def test_invalid_archives_fail(media_server, tmp_path, archive):
    reader, server = media_server
    server.add_media("a", b"a")
    server.routes["/msync/downloadFiles"] = lambda *_: server.response(archive)
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)
    assert list(tmp_path.iterdir()) == []


def test_duplicate_zip_entry_fails(media_server, tmp_path):
    reader, server = media_server
    server.add_media("a", b"a")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        archive = media_zip([("a", b"a")], entries=[("0", b"a")])
    server.routes["/msync/downloadFiles"] = lambda *_: server.response(archive)
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)


def test_duplicate_metadata_keys_fail(media_server, tmp_path):
    reader, server = media_server
    server.add_media("a", b"a")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("0", b"a")
        archive.writestr("_meta", b'{"0":"a","0":"a"}')
    server.routes["/msync/downloadFiles"] = lambda *_: server.response(stream.getvalue())
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)


def test_duplicate_media_names_in_mapping_fail(media_server, tmp_path):
    reader, server = media_server
    server.add_media("a", b"a")
    body = media_zip([("a", b"a"), ("a", b"a")])
    server.routes["/msync/downloadFiles"] = lambda *_: server.response(body)
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)


def test_empty_partial_zip_cannot_stall(media_server, tmp_path):
    reader, server = media_server
    server.add_media("a", b"a")
    server.batch_limit = 0
    with pytest.raises(AnkiWebReaderError):
        download_all_media(reader, tmp_path, lambda _: None)
