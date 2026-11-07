"""Synthetic Anki protocol server implemented at the HTTP transport boundary."""

import hashlib
import io
import json
import zipfile

import httpx
import zstandard


class SyntheticAnkiServer:
    def __init__(self):
        self.requests = []
        self.routes = {}
        self.collection = b"synthetic collection bytes" * 10000
        self.media = {}
        self.changes = []
        self.batch_limit = 25

    def handle(self, request):
        response = self._handle(request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=httpx.ByteStream(response.content),
        )

    def _handle(self, request):
        self.requests.append(request)
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/octet-stream"
        assert request.url.path in {
            "/sync/hostKey", "/sync/meta", "/sync/download", "/msync/begin",
            "/msync/mediaChanges", "/msync/downloadFiles", "/msync/mediaSanity",
        }
        header = json.loads(request.headers["anki-sync"])
        assert header["v"] == 11
        assert header["c"] and header["s"]
        payload = json.loads(zstandard.ZstdDecompressor().decompress(request.read()))
        if request.url.path in self.routes:
            return self.routes[request.url.path](request, payload, header)
        if request.url.path == "/sync/hostKey":
            assert payload == {"u": "synthetic-user", "p": "synthetic-password"}
            assert header["k"] == ""
            return self.response(json.dumps({"key": "synthetic-hkey"}).encode())
        assert header["k"] == "synthetic-hkey"
        if request.url.path == "/sync/meta":
            assert payload["v"] == 11 and payload["cv"]
            return self.response(b'{"mod":123,"scm":456,"usn":7,"cont":true}')
        if request.url.path == "/sync/download":
            assert payload == {}
            return self.response(self.collection)
        if request.url.path == "/msync/begin":
            assert payload["v"]
            return self.json_response({
                "data": {"usn": len(self.changes), "sk": "synthetic-hkey"}, "err": "",
            })
        if request.url.path == "/msync/mediaChanges":
            rows = [row for row in self.changes if row[1] > payload["lastUsn"]][:1000]
            return self.json_response({"data": rows, "err": ""})
        if request.url.path == "/msync/mediaSanity":
            result = "OK" if payload["local"] == len(self.media) else "mediaSanity"
            return self.json_response({"data": result, "err": ""})
        assert request.url.path == "/msync/downloadFiles"
        assert len(payload["files"]) <= 25
        names = payload["files"][:self.batch_limit]
        return self.response(media_zip([(name, self.media[name]) for name in names]))

    def add_media(self, name, data):
        self.media[name] = data
        self.changes.append([name, len(self.changes) + 1, hashlib.sha1(data).hexdigest()])

    def json_response(self, data):
        return self.response(json.dumps(data).encode())

    @staticmethod
    def response(body):
        compressed = zstandard.ZstdCompressor().compress(body)
        return httpx.Response(
            200, headers={"anki-original-size": str(len(body))}, content=compressed
        )


def start_loopback_server():
    """Start a synthetic HTTP server; callers must shut it down and join its thread."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    synthetic = SyntheticAnkiServer()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = httpx.Request(
                "POST", f"https://sync.ankiweb.net{self.path}",
                headers=dict(self.headers),
                content=self.rfile.read(int(self.headers["Content-Length"])),
            )
            response = synthetic.handle(request)
            body = response.read()
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, synthetic


class LoopbackTransport(httpx.BaseTransport):
    """Rewrite trusted synthetic destinations only for tests, never for production."""

    def __init__(self, port):
        self.port = port
        self.transport = httpx.HTTPTransport()

    def handle_request(self, request):
        assert request.url.host == "sync.ankiweb.net"
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return self.transport.handle_request(request)

    def close(self):
        self.transport.close()


def media_zip(files, *, entries=None, metadata=None):
    """Create a synthetic media ZIP, with optional malformed entries or mapping."""
    buffer = io.BytesIO()
    mapping = {str(index): name for index, (name, _) in enumerate(files)}
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, (_, data) in enumerate(files):
            archive.writestr(str(index), data)
        for name, data in entries or []:
            archive.writestr(name, data)
        archive.writestr("_meta", json.dumps(mapping if metadata is None else metadata))
    return buffer.getvalue()


class LocalAnkiTransport(httpx.BaseTransport):
    """Route only the two trusted synthetic hostnames to the loopback proxy."""

    def __init__(self, port):
        self.port = port
        self._transport = httpx.HTTPTransport()

    def handle_request(self, request):
        host = request.url.host
        assert host in {"sync.ankiweb.net", "sync15.ankiweb.net"}
        request.headers["x-synthetic-host"] = host
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return self._transport.handle_request(request)

    def close(self):
        self._transport.close()


class LocalAnkiEnvironment:
    def __init__(self, server_port):
        self.server_port = server_port
        self.proxy_port = None
        self.events = []
        self.forwarded = []
        self.failures = {}
        self.output_directory = None
        self.packages_at_failure = []
        self.note_id = None
        self.fixture_value = None
        self.unix_socket = None

    def transport(self):
        return LocalAnkiTransport(self.proxy_port)


def local_anki_environment(root, *, with_unix_proxy=False, provision=None):
    """Start a real local Anki server, provision synthetic data and an allowlist proxy."""
    import contextlib
    import http.client
    import os
    import socket
    import socketserver
    import subprocess
    import sys
    import tempfile
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    from anki.collection import Collection
    from anki.sync_pb2 import SyncAuth

    @contextlib.contextmanager
    def environment_context():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            server_port = probe.getsockname()[1]
        environment = LocalAnkiEnvironment(server_port)
        settings = {key: value for key, value in os.environ.items() if not key.startswith("SYNC_")}
        settings.update({
            "SYNC_HOST": "127.0.0.1", "SYNC_PORT": str(server_port),
            "SYNC_BASE": str(root / "server"), "SYNC_USER1": "probe:synthetic-password",
            "RUST_LOG": "error",
        })
        process = subprocess.Popen(
            [sys.executable, "-m", "anki.syncserver"], env=settings,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proxy = None
        proxy_thread = None
        unix_proxy = None
        unix_thread = None
        unix_directory = None
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Synthetic Anki server exited during startup")
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", server_port, timeout=1)
                    connection.request("GET", "/health")
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                    if response.status == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Synthetic Anki server did not start")

            server_url = f"http://127.0.0.1:{server_port}"
            fixture = Collection(str(root / "fixture.anki2"))
            try:
                auth = fixture.sync_login("probe", "synthetic-password", server_url)
                note = fixture.new_note(fixture.models.by_name("Basic"))
                note["Front"] = '<img src="referenced.png">'
                note["Back"] = "synthetic"
                fixture.add_note(note, fixture.decks.id("Fixture::Deck"))
                environment.note_id = note.id
                if provision is not None:
                    environment.fixture_value = provision(fixture, note)
                media = Path(fixture.media.dir())
                (media / "referenced.png").write_bytes(b"referenced bytes")
                (media / "unreferenced.mp3").write_bytes(b"unreferenced bytes")
                # These upload/sync calls provision the isolated local fixture only;
                # they are never used by the backup-side reader or proxy.
                fixture.close_for_full_sync()
                fixture.full_upload_or_download(
                    auth=SyncAuth(hkey=auth.hkey, endpoint=server_url),
                    server_usn=None, upload=True,
                )
                fixture.reopen(after_full_sync=True)
                fixture.sync_media(SyncAuth(hkey=auth.hkey, endpoint=server_url))
                for _ in range(200):
                    if not fixture.media_sync_status().active:
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("Synthetic media setup timed out")
            finally:
                fixture.close()

            allowed = {
                "/sync/hostKey", "/sync/meta", "/sync/download", "/msync/begin",
                "/msync/mediaChanges", "/msync/downloadFiles", "/msync/mediaSanity",
            }

            class Proxy(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def respond(self, status, body=b"", headers=None):
                    self.send_response(status)
                    for key, value in (headers or {}).items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_POST(self):
                    host = self.headers["x-synthetic-host"]
                    path = self.path
                    environment.events.append((host, path))
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    if path not in allowed:
                        self.respond(403)
                        return
                    if host == "sync.ankiweb.net" and path == "/msync/begin":
                        self.respond(308, headers={"Location": "https://sync15.ankiweb.net/"})
                        return
                    if environment.failures.get(path, 0):
                        environment.failures[path] -= 1
                        files = [] if environment.output_directory is None else list(
                            environment.output_directory.glob("*.colpkg")
                        )
                        environment.packages_at_failure.append(files)
                        self.respond(503, b"synthetic failure")
                        return
                    environment.forwarded.append(path)
                    connection = http.client.HTTPConnection("127.0.0.1", server_port, timeout=20)
                    try:
                        connection.request("POST", path, body=body, headers={
                            "anki-sync": self.headers["anki-sync"],
                            "content-type": self.headers["content-type"],
                        })
                        response = connection.getresponse()
                        data = response.read()
                        headers = {
                            key: value for key, value in response.getheaders()
                            if key.lower() in {"content-type", "anki-original-size"}
                        }
                        self.respond(response.status, data, headers)
                    finally:
                        connection.close()

            proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
            environment.proxy_port = proxy.server_port
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            if with_unix_proxy:
                class UnixProxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                    daemon_threads = True

                unix_directory = tempfile.TemporaryDirectory(prefix="anki-proxy-", dir="/tmp")
                environment.unix_socket = Path(unix_directory.name) / "proxy.sock"
                unix_proxy = UnixProxy(str(environment.unix_socket), Proxy)
                unix_thread = threading.Thread(target=unix_proxy.serve_forever, daemon=True)
                unix_thread.start()
            yield environment
        finally:
            if unix_proxy is not None:
                unix_proxy.shutdown()
                unix_proxy.server_close()
            if unix_thread is not None:
                unix_thread.join(timeout=5)
            if unix_directory is not None:
                unix_directory.cleanup()
            if proxy is not None:
                proxy.shutdown()
                proxy.server_close()
            if proxy_thread is not None:
                proxy_thread.join(timeout=5)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    return environment_context()


def _run_isolated_backup(socket_path, server_port, output_dir):
    """Run in a network namespace; the only transport uses the proxy's Unix socket."""
    import socket
    import sys
    from pathlib import Path

    from ankiweb_cli import ankiweb_backup as backup
    from ankiweb_cli.ankiweb_reader import AnkiWebReader
    from ankiweb_cli.backup_credentials import BackupCredentials

    class UnixTransport(httpx.BaseTransport):
        def __init__(self):
            self.transport = httpx.HTTPTransport(uds=socket_path)

        def handle_request(self, request):
            host = request.url.host
            assert host in {"sync.ankiweb.net", "sync15.ankiweb.net"}
            request.headers["x-synthetic-host"] = host
            request.url = request.url.copy_with(scheme="http", host="localhost", port=80)
            return self.transport.handle_request(request)

        def close(self):
            self.transport.close()

    with socket.socket() as probe:
        probe.settimeout(1)
        direct_blocked = probe.connect_ex(("127.0.0.1", int(server_port))) != 0
    delays = []
    backup.AnkiWebReader = lambda: AnkiWebReader(transport=UnixTransport())
    backup.time.sleep = delays.append
    reports = []
    try:
        package = backup.run_backup(
            Path(output_dir), BackupCredentials("probe", "synthetic-password"), 3, 60,
            reports.append,
        )
    except Exception:
        print("; ".join(reports), file=sys.stderr)
        raise
    print(json.dumps({
        "direct_server_blocked": direct_blocked,
        "delays": delays,
        "package": str(package),
    }))


if __name__ == "__main__":
    import sys

    _run_isolated_backup(*sys.argv[1:])
