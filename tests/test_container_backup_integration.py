"""Exercise the image entrypoint with an isolated synthetic Anki server."""

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from tests.ankiweb_server_fixture import local_anki_environment

from anki._backend import RustBackend
from anki.collection import Collection
from anki.media import media_paths_from_col_path


TRANSPORT = '''import httpx
from ankiweb_cli import ankiweb_backup
from ankiweb_cli.ankiweb_reader import AnkiWebReader

class TestTransport(httpx.BaseTransport):
    def __init__(self):
        self.transport = httpx.HTTPTransport(uds="/test-proxy.sock")

    def handle_request(self, request):
        assert request.url.host in {"sync.ankiweb.net", "sync15.ankiweb.net"}
        request.headers["x-synthetic-host"] = request.url.host
        request.url = request.url.copy_with(scheme="http", host="localhost", port=80)
        return self.transport.handle_request(request)

    def close(self):
        self.transport.close()

ankiweb_backup.AnkiWebReader = lambda: AnkiWebReader(transport=TestTransport())
'''


@pytest.mark.skipif(not os.environ.get("ANKIWEB_CONTAINER_IMAGE"), reason="container image not set")
def test_container_backup_restore_and_failure(tmp_path):
    def provision(collection, note):
        card = collection.get_card(note.cards()[0].id)
        card.queue, card.type, card.ivl, card.due, card.factor, card.reps = 2, 2, 17, 12345, 2450, 7
        collection.update_card(card)
        collection.db.execute(
            "insert into revlog values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            1700000000000, card.id, -1, 3, 17, 10, 2450, 1200, 1,
        )
        return card.id

    with local_anki_environment(tmp_path, with_unix_proxy=True, provision=provision) as env:
        output = tmp_path / "output"
        output.mkdir()
        creds = tmp_path / "credentials.toml"
        creds.write_text('[ankiweb]\nusername="probe"\npassword="synthetic-password"\n')
        creds.chmod(0o600)
        transport = tmp_path / "transport"
        transport.mkdir()
        (transport / "sitecustomize.py").write_text(TRANSPORT)
        command = [
            "docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            "--mount", f"type=bind,src={creds},dst=/credentials.toml,readonly",
            "--mount", f"type=bind,src={output},dst=/output",
            "--mount", f"type=bind,src={transport},dst=/test-transport,readonly",
            "--mount", f"type=bind,src={env.unix_socket},dst=/test-proxy.sock",
            "-e", "PYTHONPATH=/test-transport", os.environ["ANKIWEB_CONTAINER_IMAGE"],
            "ankiweb-backup",
            "--credentials-file", "/credentials.toml", "--output-dir", "/output", "--attempts", "1",
        ]
        success = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        assert success.returncode == 0, success.stdout + success.stderr
        assert "synthetic-password" not in success.stdout + success.stderr
        assert "referenced.png" in success.stdout and "unreferenced.mp3" in success.stdout
        packages = list(output.glob("*.colpkg"))
        assert len(packages) == 1
        with zipfile.ZipFile(packages[0]) as archive:
            assert {"collection.anki21b", "meta", "media"} <= set(archive.namelist())
        target = tmp_path / "restored.anki2"
        media_dir, media_db = media_paths_from_col_path(str(target))
        RustBackend().import_collection_package(
            col_path=str(target), backup_path=str(packages[0]), media_folder=media_dir,
            media_db=media_db,
        )
        restored = Collection(str(target))
        try:
            assert restored.decks.id_for_name("Fixture::Deck")
            assert restored.note_count() == 1
            assert restored.get_note(env.note_id)["Back"] == "synthetic"
            card = restored.get_card(env.fixture_value)
            assert (card.queue, card.type, card.ivl, card.due, card.factor, card.reps) == (
                2, 2, 17, 12345, 2450, 7
            )
            assert restored.db.all(
                "select id, ease, ivl, lastIvl, factor, time, type from revlog where cid = ?",
                card.id,
            ) == [[1700000000000, 3, 17, 10, 2450, 1200, 1]]
            assert Path(media_dir, "referenced.png").read_bytes() == b"referenced bytes"
            assert Path(media_dir, "unreferenced.mp3").read_bytes() == b"unreferenced bytes"
        finally:
            restored.close()
        allowed = {
            "/sync/hostKey", "/sync/meta", "/sync/download", "/msync/begin",
            "/msync/mediaChanges", "/msync/downloadFiles", "/msync/mediaSanity",
        }
        assert {path for _, path in env.events} == allowed
        assert set(env.forwarded) <= allowed
        before = packages[0].read_bytes()
        downloads = sum(path == "/sync/download" for _, path in env.events)
        env.failures["/sync/download"] = 1
        failure = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        assert failure.returncode != 0
        assert env.failures["/sync/download"] == 0
        assert sum(path == "/sync/download" for _, path in env.events) == downloads + 1
        assert "synthetic-password" not in failure.stdout + failure.stderr
        assert list(output.glob("*.colpkg")) == packages
        assert packages[0].read_bytes() == before
        assert not list(output.glob(".ankiweb-backup-*"))
        assert {path for _, path in env.events} <= allowed
