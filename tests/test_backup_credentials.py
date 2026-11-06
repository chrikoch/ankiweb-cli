from dataclasses import FrozenInstanceError
import sys
import traceback

import pytest

from ankiweb_cli.backup_credentials import load_credentials


@pytest.fixture
def credentials_file(tmp_path):
    path = tmp_path / "credentials.toml"
    path.write_text(
        '[ankiweb]\nusername = "synthetic-user"\npassword = "synthetic-secret"\n',
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_loads_credentials_and_redacts_representation(credentials_file):
    credentials = load_credentials(credentials_file)
    assert credentials.username == "synthetic-user"
    assert credentials.password == "synthetic-secret"
    assert "synthetic-user" not in repr(credentials)
    assert "synthetic-secret" not in repr(credentials)
    assert "synthetic-secret" not in str(credentials)
    with pytest.raises(FrozenInstanceError):
        credentials.password = "changed"


@pytest.mark.parametrize("content", [
    '', '[ankiweb]\nusername = "synthetic-user"\n',
    '[ankiweb]\npassword = "synthetic-secret"\n',
    '[ankiweb]\nusername = ""\npassword = "synthetic-secret"\n',
    '[ankiweb]\nusername = "synthetic-user"\npassword = ""\n',
    '[ankiweb]\nusername = 42\npassword = "synthetic-secret"\n',
    '[ankiweb]\nusername = "synthetic-user"\npassword = false\n',
    '[ankiweb]\npassword = "synthetic-secret"\ninvalid = [',
    'ankiweb = "synthetic-secret"\n',
])
def test_invalid_content_is_rejected_without_secrets(credentials_file, content):
    credentials_file.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_credentials(credentials_file)
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))
    assert "synthetic-user" not in "".join(traceback.format_exception(error.value))


def test_invalid_utf8_is_sanitized(credentials_file):
    credentials_file.write_bytes(b'[ankiweb]\npassword = "synthetic-secret"\n\xff')
    with pytest.raises(ValueError) as error:
        load_credentials(credentials_file)
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_missing_or_non_regular_file_is_rejected(tmp_path, kind):
    path = tmp_path / "missing" if kind == "missing" else tmp_path
    with pytest.raises(ValueError):
        load_credentials(path)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux permissions")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o610, 0o601, 0o620, 0o602])
def test_linux_rejects_group_or_other_permissions(credentials_file, mode):
    credentials_file.chmod(mode)
    with pytest.raises(ValueError):
        load_credentials(credentials_file)


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_other_platforms_do_not_enforce_posix_mode(credentials_file, monkeypatch, platform):
    credentials_file.chmod(0o644)
    monkeypatch.setattr(sys, "platform", platform)
    assert load_credentials(credentials_file).password == "synthetic-secret"
