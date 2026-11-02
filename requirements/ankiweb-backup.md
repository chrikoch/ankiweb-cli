# AnkiWeb collection backup

## Goal

Provide an unattended, headless command that creates a standalone backup of an
AnkiWeb collection for restoration with Anki Desktop. Only data already synced
to AnkiWeb can be included.

## Command and output

- Provide `ankiweb-cli ankiweb-backup --output-dir OUTPUT_DIR`. The output
  directory is required and must already exist; otherwise, return an error.
- Create one complete `.colpkg` per successful invocation, using Anki's modern
  package format. Include every deck, scheduling and review history, and all
  media present on AnkiWeb, including unreferenced media.
- Include a UTC timestamp in the filename. If the name exists, use a unique
  suffix. Never overwrite an existing backup.
- Keep all existing backups. Retention and deletion are outside this command's
  scope.
- Print progress, including individual media filenames, errors, and the resulting
  backup path to standard output. Never print credentials.
- Return an error if a backup fails. An incomplete backup must not appear as a
  completed backup file.

## Remote access and credentials

- Download the collection and media without uploading collection changes or
  media, requesting remote deletions, or falling back to bidirectional sync.
- Use the existing CLI's OS keyring approach initially. Verify unattended
  access on headless Linux, including after a restart and from cron. Revisit the
  approach if it cannot be used practically in that environment.
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
- Investigate how reliably collection and media completeness and changes during
  download can be detected. Decide how to handle unverifiable consistency once
  the technical limitations are understood; detected changes trigger a retry.
- Document the limits of the read-only guarantee: inspected Anki server code can
  update server-side metadata during downloads, and actual AnkiWeb server
  behavior has not been verified.
