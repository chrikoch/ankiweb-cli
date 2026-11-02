from pathlib import Path
from types import SimpleNamespace

import pytest

from ankiweb_cli import sync


def test_keyring_store_and_get(monkeypatch) -> None:
    store: dict[tuple[str, str], str] = {}

    def fake_set(service: str, user: str, pw: str) -> None:
        store[(service, user)] = pw

    def fake_get(service: str, user: str) -> str | None:
        return store.get((service, user))

    monkeypatch.setattr(sync.keyring, "set_password", fake_set)
    monkeypatch.setattr(sync.keyring, "get_password", fake_get)

    sync.store_password("u@example.com", "secret")
    assert sync.get_password("u@example.com") == "secret"


def test_login_uses_temporary_collection_and_returns_redirect(monkeypatch, tmp_path) -> None:
    events = []

    class FakeCollection:
        def __init__(self, path):
            events.append(("open", path))

        def sync_login(self, *, username, password, endpoint):
            assert (username, password, endpoint) == ("user", "secret", "https://initial")
            return SimpleNamespace(hkey="token", endpoint="https://shard")

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(sync, "Collection", FakeCollection)
    monkeypatch.setattr(sync.tempfile, "tempdir", str(tmp_path))
    assert sync.login_and_get_hkey("user", "secret", "https://initial") == (
        "token", "https://shard"
    )
    assert events[0][0] == "open"
    assert not Path(events[0][1]).parent.exists()
    assert events[1] == ("close",)


@pytest.mark.parametrize(("upload", "redirect"), [(False, "https://shard"), (True, "")])
def test_full_sync_preserves_direction_and_resolves_redirect(
    monkeypatch, tmp_path, upload, redirect
) -> None:
    events = []
    col_path = tmp_path / ("upload.anki2" if upload else "download.anki2")
    col_path.write_bytes(b"original collection")
    backups = tmp_path / ("upload-backups" if upload else "download-backups")

    class FakeCollection:
        def __init__(self, path):
            assert path == str(col_path)

        def sync_login(self, *, username, password, endpoint):
            return SimpleNamespace(hkey="token")

        def sync_collection(self, auth, sync_media):
            assert not sync_media
            return SimpleNamespace(server_media_usn=17, new_endpoint=redirect)

        def close_for_full_sync(self):
            events.append("prepared")

        def full_upload_or_download(self, *, auth, server_usn, upload):
            assert events == ["prepared"]
            events.append((auth.hkey, auth.endpoint, server_usn, upload))

        def close(self):
            events.append("closed")

    monkeypatch.setattr(sync, "Collection", FakeCollection)
    result = sync.do_full_sync(
        col_path, backups, username="user", password="secret",
        endpoint="https://initial", upload=upload,
    )
    endpoint = redirect or "https://initial"
    assert result == {"ok": True, "direction": "upload" if upload else "download",
                      "endpoint": endpoint}
    assert events == ["prepared", ("token", endpoint, 17, upload), "closed"]
    assert [p.read_bytes() for p in backups.glob("*.anki2")] == [b"original collection"]

def test_regular_sync_waits_for_media_and_reports_result(monkeypatch) -> None:
    statuses = [SimpleNamespace(active=True), SimpleNamespace(active=False)]
    seen = []

    class FakeCollection:
        def sync_collection(self, auth, sync_media):
            assert (auth.hkey, auth.endpoint, sync_media) == ("token", "https://shard", True)
            return SimpleNamespace(required=0, new_endpoint="https://new", server_message="hello")

        def media_sync_status(self):
            return statuses.pop(0)

    monkeypatch.setattr(sync.time, "sleep", lambda _: None)
    result = sync.do_sync(FakeCollection(), "token", "https://shard", on_media_progress=seen.append)
    assert result.pulled and result.pushed and not result.required_full_sync
    assert result.new_endpoint == "https://new"
    assert result.server_message == "hello"
    assert [status.active for status in seen] == [True, False]


def test_regular_sync_reports_full_sync_without_polling_media() -> None:
    class FakeCollection:
        def sync_collection(self, auth, sync_media):
            return SimpleNamespace(
                required=sync.SyncCollectionResponse.FULL_DOWNLOAD,
                new_endpoint="", server_message="",
            )

        def media_sync_status(self):
            raise AssertionError("media should not be polled when full sync is required")

    result = sync.do_sync(FakeCollection(), "token", "https://initial")
    assert result.required_full_sync
    assert result.new_endpoint is None


def test_login_closes_temporary_collection_after_failure(monkeypatch) -> None:
    events = []

    class FakeCollection:
        def __init__(self, path):
            pass

        def sync_login(self, **kwargs):
            raise RuntimeError("login failed")

        def close(self):
            events.append("closed")

    monkeypatch.setattr(sync, "Collection", FakeCollection)
    with pytest.raises(RuntimeError, match="login failed"):
        sync.login_and_get_hkey("user", "secret", "https://initial")
    assert events == ["closed"]


def test_media_sync_failure_is_reported(monkeypatch) -> None:
    class FakeCollection:
        def media_sync_status(self):
            raise RuntimeError("media sync failed")

    with pytest.raises(RuntimeError, match="media sync failed"):
        sync.wait_for_media_sync(FakeCollection())


def test_full_download_failure_closes_collection_and_retains_snapshot(
    monkeypatch, tmp_path
) -> None:
    events = []
    col_path = tmp_path / "collection.anki2"
    col_path.write_bytes(b"original")
    backups = tmp_path / "backups"

    class FakeCollection:
        def __init__(self, path):
            pass

        def sync_login(self, **kwargs):
            return SimpleNamespace(hkey="token")

        def sync_collection(self, auth, sync_media):
            return SimpleNamespace(server_media_usn=1, new_endpoint="")

        def close_for_full_sync(self):
            events.append("prepared")

        def full_upload_or_download(self, **kwargs):
            assert kwargs["upload"] is False
            raise RuntimeError("download failed")

        def close(self):
            events.append("closed")

    monkeypatch.setattr(sync, "Collection", FakeCollection)
    with pytest.raises(RuntimeError, match="download failed"):
        sync.do_full_sync(
            col_path, backups, username="user", password="secret",
            endpoint="https://initial", upload=False,
        )
    assert events == ["prepared", "closed"]
    assert [p.read_bytes() for p in backups.glob("*.anki2")] == [b"original"]
