import pytest
from click.testing import CliRunner

from ankiweb_cli.cli import main


def test_cli_runs_with_no_args() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Headless CLI" in result.output


def forbid_login(*args, **kwargs):
    raise AssertionError("unexpected network login")


def test_sync_requires_configured_username(monkeypatch, tmp_path) -> None:
    from ankiweb_cli import cli

    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli.sync_mod, "login_and_get_hkey", forbid_login)
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "missing.toml")
    result = CliRunner().invoke(main, ["sync"])
    assert result.exit_code != 0
    assert "Not configured" in result.output


def test_sync_requires_password_in_keyring(monkeypatch, tmp_path) -> None:
    from ankiweb_cli import cli
    from ankiweb_cli.config import Config, save_config

    config_path = tmp_path / "config.toml"
    save_config(Config(username="user"), config_path)
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli.sync_mod, "login_and_get_hkey", forbid_login)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    monkeypatch.setattr(cli.sync_mod, "get_password", lambda _: None)
    result = CliRunner().invoke(main, ["sync"])
    assert result.exit_code != 0
    assert "No password in keyring" in result.output


def test_local_backup_cli_lists_and_restores_snapshot(monkeypatch, tmp_path) -> None:
    from ankiweb_cli import cli

    backups = tmp_path / "backups"
    backups.mkdir()
    name = "collection-20260101-000000-sync.anki2"
    (backups / name).write_bytes(b"snapshot")
    target = tmp_path / "collection.anki2"
    target.write_bytes(b"current")
    monkeypatch.setattr(cli, "BACKUPS_DIR", backups)
    monkeypatch.setattr(cli, "COLLECTION_FILE", target)
    runner = CliRunner()
    result = runner.invoke(main, ["backup", "list"])
    assert result.exit_code == 0
    assert name in result.output
    result = runner.invoke(main, ["backup", "restore", name, "--yes"])
    assert result.exit_code == 0
    assert target.read_bytes() == b"snapshot"
    assert [p.read_bytes() for p in backups.glob("*-pre-restore.anki2")] == [b"current"]


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_full_sync_cli_passes_requested_direction(monkeypatch, tmp_path, direction) -> None:
    from ankiweb_cli import cli
    from ankiweb_cli.config import Config, save_config

    config_path = tmp_path / "config.toml"
    save_config(Config(username="user", endpoint="https://initial"), config_path)
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    monkeypatch.setattr(cli, "COLLECTION_FILE", tmp_path / "collection.anki2")
    monkeypatch.setattr(cli, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(cli.sync_mod, "get_password", lambda _: "secret")
    monkeypatch.setattr(cli.sync_mod, "login_and_get_hkey", lambda *args: ("token", "https://initial"))

    def fake_full_sync(col_path, backups_dir, *, username, password, endpoint, upload):
        assert (username, password, endpoint) == ("user", "secret", "https://initial")
        return {"direction": "upload" if upload else "download"}

    monkeypatch.setattr(cli.sync_mod, "do_full_sync", fake_full_sync)
    result = CliRunner().invoke(main, ["sync", "--full", direction])
    assert result.exit_code == 0, result.output
    assert f'"direction": "{direction}"' in result.output


def test_login_stores_password_only_after_success(monkeypatch, tmp_path) -> None:
    from ankiweb_cli import cli
    from ankiweb_cli.config import Config, load_config, save_config

    config_path = tmp_path / "config.toml"
    save_config(Config(username="user", endpoint="https://initial"), config_path)
    stored = []
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "secret")
    def successful_login(username, password, endpoint):
        assert (username, password, endpoint) == ("user", "secret", "https://initial")
        return "token", "https://shard"

    monkeypatch.setattr(cli.sync_mod, "login_and_get_hkey", successful_login)
    monkeypatch.setattr(
        cli.sync_mod, "store_password", lambda user, password: stored.append((user, password))
    )

    result = CliRunner().invoke(main, ["login"])
    assert result.exit_code == 0
    assert stored == [("user", "secret")]
    assert load_config(config_path).endpoint == "https://shard"
    assert "secret" not in result.output and "token" not in result.output


def test_failed_login_does_not_store_password(monkeypatch, tmp_path) -> None:
    from ankiweb_cli import cli
    from ankiweb_cli.config import Config, save_config

    config_path = tmp_path / "config.toml"
    save_config(Config(username="user"), config_path)
    stored = []
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "secret")
    monkeypatch.setattr(cli.sync_mod, "store_password", lambda *args: stored.append(args))

    def reject_login(username, password, endpoint):
        assert (username, password, endpoint) == ("user", "secret", "https://sync.ankiweb.net")
        raise RuntimeError("authentication failed")

    monkeypatch.setattr(cli.sync_mod, "login_and_get_hkey", reject_login)
    result = CliRunner().invoke(main, ["login"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert stored == []
    assert "secret" not in result.output


@pytest.mark.parametrize("outcome", ["success", "full_required", "media_failure"])
def test_regular_sync_cli_reports_result(monkeypatch, tmp_path, outcome) -> None:
    from contextlib import contextmanager

    from ankiweb_cli import cli
    from ankiweb_cli.config import Config, load_config, save_config
    from ankiweb_cli.sync import SyncResult

    config_path = tmp_path / "config.toml"
    save_config(Config(username="user", endpoint="https://initial"), config_path)
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    monkeypatch.setattr(cli.sync_mod, "get_password", lambda _: "secret")
    monkeypatch.setattr(cli.sync_mod, "login_and_get_hkey", lambda *args: ("token", "https://shard"))
    events = []

    @contextmanager
    def fake_collection(*args, **kwargs):
        events.append("opened")
        try:
            yield object()
        finally:
            events.append("closed")

    def fake_sync(col, hkey, endpoint, **kwargs):
        assert (hkey, endpoint) == ("token", "https://shard")
        if outcome == "media_failure":
            raise RuntimeError("media failed")
        return SyncResult(
            pulled=True, pushed=True, required_full_sync=outcome == "full_required"
        )

    monkeypatch.setattr(cli, "open_collection", fake_collection)
    monkeypatch.setattr(cli.sync_mod, "do_sync", fake_sync)
    result = CliRunner().invoke(main, ["sync"])
    assert events == ["opened", "closed"]
    assert load_config(config_path).endpoint == "https://shard"
    if outcome == "success":
        assert result.exit_code == 0
        assert '"pulled": true' in result.output and '"pushed": true' in result.output
    elif outcome == "full_required":
        assert result.exit_code != 0
        assert "requires a full sync" in result.output
    else:
        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert str(result.exception) == "media failed"
    assert "secret" not in result.output and "token" not in result.output


def test_restore_declined_leaves_collection_unchanged(monkeypatch, tmp_path) -> None:
    from ankiweb_cli import cli

    backups = tmp_path / "backups"
    backups.mkdir()
    name = "collection-20260101-000000-sync.anki2"
    (backups / name).write_bytes(b"snapshot")
    target = tmp_path / "collection.anki2"
    target.write_bytes(b"current")
    monkeypatch.setattr(cli, "BACKUPS_DIR", backups)
    monkeypatch.setattr(cli, "COLLECTION_FILE", target)
    result = CliRunner().invoke(main, ["backup", "restore", name], input="n\n")
    assert result.exit_code != 0
    assert target.read_bytes() == b"current"
    assert list(backups.glob("*-pre-restore.anki2")) == []


def test_restore_missing_snapshot_reports_error_without_overwrite(monkeypatch, tmp_path) -> None:
    import json

    from ankiweb_cli import cli

    target = tmp_path / "collection.anki2"
    target.write_bytes(b"current")
    monkeypatch.setattr(cli, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(cli, "COLLECTION_FILE", target)
    result = CliRunner().invoke(main, ["backup", "restore", "missing.anki2", "--yes"])
    assert result.exit_code == 0  # Current CLI reports this failure in JSON.
    assert json.loads(result.output)["status"] == "error"
    assert target.read_bytes() == b"current"


def test_ankiweb_backup_help_lists_required_options_and_defaults():
    result = CliRunner().invoke(main, ["ankiweb-backup", "--help"])
    assert result.exit_code == 0
    assert "--output-dir" in result.stdout and "--credentials-file" in result.stdout
    assert "--attempts" in result.stdout and "--max-retry-delay" in result.stdout
    assert "3" in result.stdout and "60" in result.stdout


@pytest.mark.parametrize("arguments", [
    [], ["--output-dir", "missing", "--credentials-file", "missing"],
    ["--attempts", "0"], ["--attempts", "-1"], ["--attempts", "not-a-number"],
    ["--max-retry-delay", "4"], ["--max-retry-delay", "nan"],
    ["--unknown", "synthetic-password"],
])
def test_ankiweb_backup_argument_errors_are_stdout_and_redacted(tmp_path, arguments):
    credentials = tmp_path / "credentials.toml"
    credentials.write_text('[ankiweb]\nusername="u"\npassword="p"\n')
    credentials.chmod(0o600)
    base = ["ankiweb-backup", "--output-dir", str(tmp_path), "--credentials-file", str(credentials)]
    args = ["ankiweb-backup"] if not arguments else base + arguments
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0
    assert "Error" in result.stdout
    assert result.stderr == ""
    assert "synthetic-password" not in result.stdout


def test_ankiweb_backup_cli_progress_and_result(tmp_path, monkeypatch):
    from ankiweb_cli import cli

    credentials = tmp_path / "credentials.toml"
    credentials.write_text('[ankiweb]\nusername="fake-user"\npassword="fake-secret"\n')
    credentials.chmod(0o600)
    target = tmp_path / "backup.colpkg"

    def fake_run(output_dir, creds, attempts, max_retry_delay, report):
        assert output_dir == tmp_path
        assert creds.username == "fake-user" and creds.password == "fake-secret"
        assert attempts == 3 and max_retry_delay == 60
        report("unreferenced.mp3")
        report("attempt 1 failed")
        return target

    monkeypatch.setattr(cli, "run_backup", fake_run)
    result = CliRunner().invoke(main, [
        "ankiweb-backup", "--output-dir", str(tmp_path), "--credentials-file", str(credentials)
    ])
    assert result.exit_code == 0
    assert all(
        text in result.stdout for text in ("unreferenced.mp3", "attempt 1 failed", str(target))
    )
    assert "fake-secret" not in result.output and result.stderr == ""


def test_ankiweb_backup_cli_failure_redacts_secret(tmp_path, monkeypatch):
    from ankiweb_cli import cli

    credentials = tmp_path / "credentials.toml"
    credentials.write_text('[ankiweb]\nusername="fake-user"\npassword="fake-secret"\n')
    credentials.chmod(0o600)

    def fake_run(*args, **kwargs):
        raise RuntimeError("fake-secret fake-user synthetic-hkey")

    monkeypatch.setattr(cli, "run_backup", fake_run)
    result = CliRunner().invoke(main, [
        "ankiweb-backup", "--output-dir", str(tmp_path), "--credentials-file", str(credentials)
    ])
    assert result.exit_code != 0
    assert "Error" in result.stdout and result.stderr == ""
    assert "fake-secret" not in result.output and "fake-user" not in result.output
    assert "synthetic-hkey" not in result.output
