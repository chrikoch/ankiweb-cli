# AnkiWeb collection backup

## Goal

Provide an unattended, headless command that creates a standalone backup of an
AnkiWeb collection for restoration with Anki Desktop. Only data already synced
to AnkiWeb can be included.

## Command and output

- Provide `ankiweb-cli ankiweb-backup --output-dir OUTPUT_DIR --credentials-file CREDENTIALS_FILE`.
  The output directory is required and must already exist; otherwise, return an error.
- Create one complete `.colpkg` per successful invocation, using Anki's modern
  package format. Include every deck, scheduling and review history, and all
  media present on AnkiWeb, including unreferenced media.
- Assume the account has no legacy media names that current Anki cannot export
  without normalization. Handling such legacy names is out of scope for now;
  report a failure if encountered instead of silently renaming or omitting media.
- If distinct media names cannot be stored separately on the executing system,
  report the conflict and fail rather than rename, overwrite or omit a file.
  Document this limitation for case-insensitive filesystems, including the
  corresponding limitation when restoring on such a filesystem.
- Include a UTC timestamp in the filename. If the name exists, use a unique
  suffix. Never overwrite an existing backup.
- Keep all existing backups. Retention and deletion are outside this command's
  scope.
- Print progress, including individual media filenames, errors, and the resulting
  backup path to standard output. Never print credentials.
- Return an error if a backup fails. Prefer publishing a complete package without
  exposing an incomplete file under the final name. If no such no-overwrite
  publication method is available on the destination, exclusively create a new
  final file and copy the validated package into it. In this fallback, the final
  name is visible during copying and may remain incomplete after interruption.
  Document this limitation; never overwrite an existing backup.

## Remote access and credentials

- Download the collection and media without uploading collection changes or
  media, requesting remote deletions, or falling back to bidirectional sync.
- Read the AnkiWeb username and password from the specified TOML credentials file,
  using `username` and `password` under `[ankiweb]`. The file stores credentials
  in plaintext and must have restrictive access permissions. On Linux, check
  the file's group and other permission bits; on Windows and macOS, document
  how users must protect the file, without automatically checking its access
  permissions. Verify unattended access on headless Linux, including after a
  restart and from cron. Existing CLI commands continue to use the OS keyring.
- Document the credentials file's plaintext storage and how to exclude it from
  backups. Never include it in the collection package.
- Support unattended operation on a Raspberry Pi without a graphical interface.
- The backup covers collection data and media, not account credentials or account
  settings.

## Retries

- Retry failed attempts, including detected remote changes and other failures.
  The number of attempts must be configurable and defaults to three total
  attempts, including the initial attempt.
- Wait five seconds before the first retry and double the delay for each further
  retry. The maximum delay must be configurable and defaults to 60 seconds.
- Report failure if all attempts fail.

## Restoration

- Restore by importing the package with Anki Desktop. Verify restoration using a
  current version; compatibility with older versions is not required.
- Do not add a restore command. The existing `backup list` and `backup restore`
  commands manage local database snapshots, not these collection packages;
  preserve them and explain the distinction in help and documentation.
- Document that importing a collection package replaces the current collection's
  cards but does not automatically remove additional local media. An optional
  subsequent upload to AnkiWeb is a separate, deliberate operation; media sync
  does not provide an exact rollback of the remote media directory.

## Verification and open technical questions

- Before implementation, review the upstream project, especially credential
  handling, synchronization, existing backups, dependencies, tests, and code
  quality. Document findings relevant to building on it.
- Test that prohibited network operations cannot occur and verify recovery using
  synthetic data.
- Write unit tests and achieve at least 80% test coverage.
- Verify that download-only media access and AnkiWeb shard redirects can be
  handled without ordinary synchronization.
- Focus manual testing on Linux and the target Raspberry Pi. Include Windows and
  macOS builds and automated checks in CI where dependencies and runners permit;
  document their lower level of manual testing and any unverified combinations.
- Investigate how reliably collection and media completeness and changes during
  download can be detected; detected changes trigger a retry.
- If all available checks pass, allow the backup to complete even though
  completeness and a consistent collection-and-media snapshot cannot be
  proven. Document this residual uncertainty; detected changes and failed
  integrity or completeness checks still cause the attempt to fail and retry.
- Document the limits of the read-only guarantee: inspected Anki server code can
  update server-side metadata during downloads, and actual AnkiWeb server
  behavior has not been verified.
