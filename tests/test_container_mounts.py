"""Check the image's bind-mount behavior with synthetic data."""

import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANKIWEB_CONTAINER_IMAGE"), reason="container image not set"
)


def test_mount_permissions_and_cli_failures(tmp_path):
    credentials = tmp_path / "credentials.toml"
    credentials.write_text('[ankiweb]\nusername="synthetic-user"\npassword="synthetic-secret"\n')
    credentials.chmod(0o600)
    output = tmp_path / "output"
    output.mkdir()
    unwritable = tmp_path / "unwritable"
    unwritable.mkdir(mode=0o555)
    base = ["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}"]
    cred_mount = ["--mount", f"type=bind,src={credentials},dst=/credentials.toml,readonly"]
    image = os.environ["ANKIWEB_CONTAINER_IMAGE"]

    def output_mount(path):
        return ["--mount", f"type=bind,src={path},dst=/output"]

    def run(args, success):
        result = subprocess.run(
            base + args, capture_output=True, text=True, timeout=60, check=False
        )
        assert "synthetic-secret" not in result.stdout + result.stderr
        assert "synthetic-user" not in result.stdout + result.stderr
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result

    probe = '''from pathlib import Path
from ankiweb_cli.backup_credentials import load_credentials
load_credentials(Path("/credentials.toml"))
try:
    Path("/credentials.toml").write_text("unexpected")
except OSError:
    pass
else:
    raise AssertionError("read-only credentials mount is writable")
Path("/output/ownership-check").write_text("synthetic output")
'''
    run([*cred_mount, *output_mount(output), "--entrypoint", "python", image, "-c", probe], True)
    stat = (output / "ownership-check").stat()
    assert (stat.st_uid, stat.st_gid) == (os.getuid(), os.getgid())
    cli = [image, "ankiweb-backup", "--credentials-file", "/credentials.toml", "--output-dir",
           "/output", "--attempts", "1"]
    run([*output_mount(output), *cli], False)
    run([*cred_mount, *cli], False)
    run(cli, False)
    credentials.chmod(0o644)
    run([*cred_mount, *output_mount(output), *cli], False)
    credentials.chmod(0o000)
    run([*cred_mount, *output_mount(output), *cli], False)
    credentials.chmod(0o600)
    result = run([*cred_mount, *output_mount(unwritable), *cli], False)
    assert "Backup attempt failed" in result.stdout
    assert "Backup attempt 1 of 1" not in result.stdout
    assert not list(unwritable.iterdir())

    collision = '''from datetime import datetime, timezone
from pathlib import Path
from ankiweb_cli import ankiweb_backup
class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ankiweb_backup.datetime = FixedDatetime
output = Path("/output")
existing = output / "ankiweb-20260102T030405Z.colpkg"
existing.write_bytes(b"existing")
package = output / "candidate.tmp"
package.write_bytes(b"new")
result = ankiweb_backup.publish_package(package, output)
assert existing.read_bytes() == b"existing"
assert result.name == "ankiweb-20260102T030405Z-1.colpkg"
assert result.read_bytes() == b"new"
'''
    run([*output_mount(output), "--entrypoint", "python", image, "-c", collision], True)
