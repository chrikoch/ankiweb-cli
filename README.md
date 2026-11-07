# ankiweb-cli

Headless command-line interface for AnkiWeb.

Built on the official [`anki`](https://pypi.org/project/anki/) Python library,
so the sync protocol and collection format match Anki desktop. Everything is
driven from the shell. No GUI, no review interface. Useful for inspecting your
collection, syncing without opening the desktop app, and automating bulk
operations on decks and cards.

## Features

- `sync`: pull from AnkiWeb, push local changes. Handles AnkiWeb's
  load-balancer shard redirects so first-time `--full download` works
  headlessly.
- `decks list`, `cards list`. `cards list` accepts Anki's full search syntax
  (`deck:"X"`, `tag:y`, `is:new`, ...).
- `decks delete`, `decks suspend`, `decks unsuspend`. Recursive by default for
  suspend/unsuspend; `--recursive` for delete. Bulk operations require
  `--yes-really` to confirm.
- `gen reverse`: add a reverse card template to the note type used by a deck.
  Anki regenerates cards for existing notes automatically.
- `audit`: deck inventory, one-direction-only template detection, duplicate
  detection across decks, note-type inventory.
- Auto-backup before every write. Timestamped local `.anki2` snapshots.
- `ankiweb-backup`: download a checked collection package and media from
  AnkiWeb without an ordinary bidirectional sync.
- The existing `login` and `sync` commands use the OS keyring. The separate
  unattended AnkiWeb backup uses a plaintext credentials file described below.

Still on the roadmap: `decks rename`/`move`/`merge`, `cards add`/`retag`/`delete`.

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/Sciguymjm/ankiweb-cli
cd ankiweb-cli
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ankiweb-cli --help
```

## First-time setup

```bash
ankiweb-cli config set username YOUR_ANKIWEB_EMAIL
ankiweb-cli login                # prompts for password; stored in OS keyring
ankiweb-cli sync --full download # bootstraps local collection from AnkiWeb
ankiweb-cli decks list
ankiweb-cli audit
```

After bootstrap, ordinary `ankiweb-cli sync` handles incremental changes in
both directions.

## AnkiWeb collection backup

`ankiweb-backup` downloads data **already synced to AnkiWeb** into a modern
`.colpkg` collection package, including referenced and unreferenced remote
media. Unsynced local changes are not included. The package excludes the
credentials file and AnkiWeb account settings. This is separate from the
automatic local `.anki2` snapshots, which contain a local database copy and do
not package media.

The command uses a credentials file instead of the keyring used by `login` and
`sync`, so it can run without an unlocked desktop keyring. Create a TOML file
outside the backup output directory and restrict access to it. For example, on
Linux:

```sh
mkdir -p "$HOME/.config/ankiweb-cli" "$HOME/ankiweb-backups"
umask 077
cat > "$HOME/.config/ankiweb-cli/backup-credentials.toml" <<'EOF'
[ankiweb]
username = "your-ankiweb-email@example.com"
password = "your-ankiweb-password"
EOF
chmod 600 "$HOME/.config/ankiweb-cli/backup-credentials.toml"
ankiweb-cli ankiweb-backup \
  --credentials-file "$HOME/.config/ankiweb-cli/backup-credentials.toml" \
  --output-dir "$HOME/ankiweb-backups"
```

Replace the placeholders privately and use valid TOML string escaping for any
special characters in the credentials. The file contains a password in
plaintext at rest: protect its directory and exclude the file from ordinary
file backups. On Linux the command rejects credentials files with group or
other permission bits. On Windows and macOS, configure file access with the
platform's permissions or ACLs; the command does not validate those ACLs.

The output directory must already exist. Completed packages use UTC timestamps
and a suffix if needed to avoid overwriting an existing file. Progress, media
filenames, warnings and errors are printed to standard output; the process
returns a nonzero exit status on failure. `--attempts` sets the maximum number
of attempts (default 3, minimum 1). Retries begin after five seconds and use
increasing delays capped by `--max-retry-delay` (default 60 seconds, minimum 5).
If publication has an uncertain outcome, inspect the reported output path and
preserved workspace before trying again. On filesystems without a supported
hard-link operation, publication falls back to an exclusive copy; interruption
can leave a partial file at the final name.

For unattended use, provide absolute paths to the executable, credentials file,
and output directory and monitor output and exit status. For example, a cron
entry that appends output to a log might be:

```cron
0 3 * * * /absolute/path/to/ankiweb-cli ankiweb-backup --credentials-file /absolute/path/to/backup-credentials.toml --output-dir /absolute/path/to/ankiweb-backups >> /absolute/path/to/ankiweb-backup.log 2>&1
```

The example does not itself alert on failure; arrange monitoring separately.
The command has been exercised without a graphical session, but cron execution,
operation after restart and operation on the target Raspberry Pi have not yet
been verified. Protect the log too: it contains media filenames and paths.

The backup uses download-only requests, not an ordinary sync or media upload or
deletion. Inspected Anki server code can still update server-side metadata or
interrupt a concurrent sync during a download; AnkiWeb's internal behavior is
not established. The command checks collection integrity, media hashes and
counts, and remote metadata before publishing. These checks detect many changes during download,
but cannot guarantee a consistent snapshot or complete server data in every
case. Avoid changing or syncing the account during a backup. Unsupported legacy
media names or names that collide on the destination filesystem cause a clear
failure instead of being silently renamed or overwritten.
If a package contains names that differ only in case, restoring it on a
case-insensitive filesystem may not preserve both files.

### Run the backup in Docker

The public image contains Python and the CLI, so the host only needs Docker
and does not need Python or a virtual environment. The image runs the same
`ankiweb-cli`; the backup behavior and limitations above also apply here.
First create the credentials file and output directory as described above. On
Linux, the credentials file must be a regular file readable by the chosen user
and have no group or other permission bits (for example, mode `0600`). It
remains plaintext on the host; protect it and exclude it from file backups.

Pull and run the public image with absolute host paths:

```sh
docker pull ghcr.io/chrikoch/ankiweb-cli:latest
docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$HOME/.config/ankiweb-cli/backup-credentials.toml,dst=/credentials.toml,readonly" \
  --mount "type=bind,src=$HOME/ankiweb-backups,dst=/backups" \
  ghcr.io/chrikoch/ankiweb-cli:latest ankiweb-backup \
  --credentials-file /credentials.toml --output-dir /backups
```

The host output directory must already exist and be writable by the mapped
user. The credentials mount is read-only, while the output mount is writable;
completed backups remain on the host after `--rm` removes the container. The
command prints progress, filenames and errors to standard output and exits
nonzero on failure. Schedule this command on the host and monitor its output
and exit status; the image does not run a scheduler. Protect any logs because
they contain media filenames and paths.

The example uses the host UID and GID. Filesystem permissions and user mapping
can differ with rootless Docker, user namespaces, Docker Desktop and host ACLs;
check that the container can read the protected credentials and write the
output without loosening access. The command checks Linux permission bits, not
Windows or macOS ACLs. Synthetic container tests have passed on Linux AMD64 and
ARM64 GitHub runners and on Docker Desktop/WSL2; a Raspberry Pi, cron and
post-restart execution remain unverified. See the
[container acceptance record](docs/superpowers/plans/container-backup-acceptance.md)
for the tested combinations and remaining limits.

`latest` moves to the most recent successfully published main revision. A
full-commit-SHA tag identifies a specific revision; pin the manifest digest
to select an exact image, for example:

```sh
docker pull ghcr.io/chrikoch/ankiweb-cli:6b843b88336ccc27a0dc7b3afaf3db5c027adf84
docker pull ghcr.io/chrikoch/ankiweb-cli@sha256:7319c9309cce6c1a5e340c2fa4460673091ca3256cd33ce8c19ddd544496b499
```

To run the pinned image, use that same digest reference in place of
`ghcr.io/chrikoch/ankiweb-cli:latest` in the `docker run` command.

The package and tags are public; do not put credentials or collection data in
an image. To build locally from a checkout instead of pulling, run
`docker build -t ankiweb-cli:local .` and replace the image name in the run
example with `ankiweb-cli:local`.

### Restore a package

In Anki Desktop, select **File → Import** and choose the `.colpkg`. Importing a
collection package replaces the current collection and its cards; keep a
separate backup of the current collection first. Additional local media files
remain, so the result is not an exact rollback of the media directory. If you
later want the restored collection on AnkiWeb, choose any full upload
deliberately after reviewing Anki's sync prompts: that can replace the server
collection. Media sync is not an exact rollback of remote media.

## Data location

The CLI keeps state in a per-user directory chosen by OS convention:

- Linux/BSD: `$XDG_DATA_HOME/ankiweb-cli` (or `~/.local/share/ankiweb-cli`)
- macOS: `~/Library/Application Support/ankiweb-cli`
- Windows: `%APPDATA%\ankiweb-cli`

Override with `ANKIWEB_CLI_HOME=/path/to/dir`. Inside:

```
collection/           your synced collection
backups/              automatic snapshots (one per mutating operation)
cache/                generated artifacts
config.toml           username and endpoint
```

## Confirmations

Mutating commands prompt before running. Two ways to skip:

- `--yes` / `ANKIWEB_CLI_YES=1` skips the basic prompt.
- `--yes-really` is additionally required when an operation affects more than
  50 cards (deck delete, deck suspend).

## License

[GNU AGPL v3.0](LICENSE). Required because the upstream `anki` library is AGPL,
and any program that links it must be AGPL when distributed.

## Status

Pre-1.0. Sync, read-only inspection, deck deletion and suspension, and reverse
template generation are implemented and tested (35+ tests against an isolated
collection). The remaining mutating commands are next.
