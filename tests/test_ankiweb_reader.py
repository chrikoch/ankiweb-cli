import traceback

import httpx
import pytest
import zstandard

from ankiweb_cli.ankiweb_reader import AnkiWebReader, AnkiWebReaderError, CollectionMeta
from tests.ankiweb_server_fixture import SyntheticAnkiServer


@pytest.fixture
def server():
    return SyntheticAnkiServer()


@pytest.fixture
def reader(server):
    return AnkiWebReader(transport=httpx.MockTransport(server.handle))


def test_login_metadata_and_streamed_collection(reader, server, tmp_path):
    reader.login("synthetic-user", "synthetic-password")
    assert reader.collection_meta() == CollectionMeta(mod=123, scm=456, usn=7)
    output = tmp_path / "collection.anki2"
    reader.download_collection(output)
    assert output.read_bytes() == server.collection
    assert [request.url.path for request in server.requests] == [
        "/sync/hostKey", "/sync/meta", "/sync/download"
    ]


def test_redirect_to_trusted_shard_is_reused(reader, server, tmp_path):
    def redirect(request, payload, header):
        if request.url.host == "sync.ankiweb.net":
            return httpx.Response(308, headers={"Location": "https://sync15.ankiweb.net/"})
        return server.response(b'{"mod":1,"scm":2,"usn":3,"cont":true}')

    server.routes["/sync/meta"] = redirect
    reader.login("synthetic-user", "synthetic-password")
    assert reader.collection_meta() == CollectionMeta(mod=1, scm=2, usn=3)
    reader.download_collection(tmp_path / "collection")
    assert [request.url.host for request in server.requests] == [
        "sync.ankiweb.net", "sync.ankiweb.net", "sync15.ankiweb.net", "sync15.ankiweb.net"
    ]


@pytest.mark.parametrize("location", [
    "http://sync15.ankiweb.net/", "https://evil.example/", "https://ankiweb.net.evil.example/",
    "https://sync15.ankiweb.net:444/", "https://user@sync15.ankiweb.net/",
    "https://sync15.ankiweb.net/?secret=x", "https://sync15.ankiweb.net/#x",
    "https://sync15.ankiweb.net:invalid/", "https://sync15.ankiweb.net/unexpected",
    "//evil.example/", "http://127.0.0.1/",
])
def test_rejects_untrusted_redirect_without_forwarding_key(reader, server, location):
    server.routes["/sync/meta"] = lambda *_: httpx.Response(308, headers={"Location": location})
    reader.login("synthetic-user", "synthetic-password")
    with pytest.raises(AnkiWebReaderError) as error:
        reader.collection_meta()
    assert len(server.requests) == 2
    assert "synthetic-hkey" not in "".join(traceback.format_exception(error.value))


def test_redirect_loop_is_bounded(reader, server):
    server.routes["/sync/meta"] = lambda *_: httpx.Response(308, headers={"Location": "/"})
    reader.login("synthetic-user", "synthetic-password")
    with pytest.raises(AnkiWebReaderError):
        reader.collection_meta()
    assert len(server.requests) <= 7


@pytest.mark.parametrize("response", [
    httpx.Response(403, content=b"synthetic-password synthetic-hkey"),
    httpx.Response(200, content=b"not zstd"),
    httpx.Response(200, content=zstandard.ZstdCompressor().compress(b"hello")[:-1]),
    httpx.Response(200, headers={"anki-original-size": "9"},
                   content=zstandard.ZstdCompressor().compress(b"hello")),
    httpx.Response(200, content=zstandard.ZstdCompressor().compress(b"not json")),
    httpx.Response(200, content=zstandard.ZstdCompressor().compress(b'{"key": ""}')),
])
def test_invalid_login_responses_do_not_leak_secrets(reader, server, response):
    server.routes["/sync/hostKey"] = lambda *_: response
    username = "synthetic-user"
    password = "synthetic-password"
    with pytest.raises(AnkiWebReaderError) as error:
        reader.login(username, password)
    trace = "".join(traceback.format_exception(error.value))
    assert "synthetic-password" not in trace
    assert "synthetic-hkey" not in trace


def test_truncated_collection_is_an_error(reader, server, tmp_path):
    reader.login("synthetic-user", "synthetic-password")
    compressed = zstandard.ZstdCompressor().compress(server.collection)
    server.routes["/sync/download"] = lambda *_: httpx.Response(200, content=compressed[:-3])
    with pytest.raises(AnkiWebReaderError):
        reader.download_collection(tmp_path / "collection")


def test_highly_compressed_collection_is_written_in_bounded_chunks(reader, server):
    class MeasuringSink:
        largest = 0
        total = 0

        def write(self, data):
            self.largest = max(self.largest, len(data))
            self.total += len(data)

    reader.login("synthetic-user", "synthetic-password")
    server.collection = b"x" * (32 * 1024 * 1024)
    sink = MeasuringSink()
    reader._request("/sync/download", {}, sink)
    assert sink.total == len(server.collection)
    assert sink.largest <= 4 * 1024 * 1024


@pytest.mark.parametrize("payload", [b'{}', b'{"mod":true,"scm":2,"usn":3}'])
def test_invalid_metadata_is_rejected(reader, server, payload):
    server.routes["/sync/meta"] = lambda *_: server.response(payload)
    reader.login("synthetic-user", "synthetic-password")
    with pytest.raises(AnkiWebReaderError):
        reader.collection_meta()


def test_server_refusal_is_rejected(reader, server):
    server.routes["/sync/meta"] = lambda *_: server.response(
        b'{"mod":1,"scm":2,"usn":3,"cont":false,"msg":"synthetic-hkey"}'
    )
    reader.login("synthetic-user", "synthetic-password")
    with pytest.raises(AnkiWebReaderError) as error:
        reader.collection_meta()
    assert "synthetic-hkey" not in str(error.value)


@pytest.mark.parametrize("path", ["/sync/upload", "/sync/applyChanges", "/msync/uploadChanges"])
def test_prohibited_requests_are_rejected_before_transport(reader, server, path):
    with pytest.raises(AnkiWebReaderError):
        reader._request(path, {})
    assert server.requests == []


def test_real_http_stream_against_loopback_server(tmp_path):
    from tests.ankiweb_server_fixture import LoopbackTransport, start_loopback_server

    try:
        server, thread, synthetic = start_loopback_server()
    except PermissionError:
        pytest.skip("loopback sockets are unavailable in this environment")
    try:
        reader = AnkiWebReader(transport=LoopbackTransport(server.server_port))
        reader.login("synthetic-user", "synthetic-password")
        assert reader.collection_meta() == CollectionMeta(mod=123, scm=456, usn=7)
        output = tmp_path / "collection"
        reader.download_collection(output)
        assert output.read_bytes() == synthetic.collection
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_media_protocol_methods(reader, server, tmp_path):
    from ankiweb_cli.ankiweb_reader import MediaChange

    server.add_media("referenced.png", b"image")
    server.add_media("unused.mp3", b"audio")
    reader.login("synthetic-user", "synthetic-password")
    assert reader.media_usn() == 2
    assert reader.media_changes(1) == [MediaChange("unused.mp3", 2, server.changes[1][2])]
    assert reader.media_sanity(2) is True
    assert reader.media_sanity(1) is False
    assert reader.download_media(["unused.mp3"], tmp_path) == {"unused.mp3"}
    assert (tmp_path / "unused.mp3").read_bytes() == b"audio"


@pytest.mark.parametrize("path,body", [
    ("/msync/begin", {"err": "synthetic-hkey"}),
    ("/msync/mediaChanges", {"data": [["a", True, "abc"]], "err": ""}),
    ("/msync/mediaSanity", {"data": "unexpected", "err": ""}),
])
def test_invalid_media_protocol_responses_fail(reader, server, path, body):
    server.routes[path] = lambda *_: server.json_response(body)
    reader.login("synthetic-user", "synthetic-password")
    with pytest.raises(AnkiWebReaderError):
        if path.endswith("begin"):
            reader.media_usn()
        elif path.endswith("mediaChanges"):
            reader.media_changes(0)
        else:
            reader.media_sanity(0)
