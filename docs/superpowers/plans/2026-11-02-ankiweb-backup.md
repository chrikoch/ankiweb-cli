# AnkiWeb Backup Implementation Plan

> **For implementers:** Work through the checkboxes in order. Complete the feasibility and decision gate before committing to an implementation. Do not start implementation merely because this plan exists.

**Goal:** Create a headless, unattended command that downloads an AnkiWeb collection and all its media into a standalone modern `.colpkg` for Anki Desktop.

**Architecture:** First establish which Anki APIs and protocol operations can deliver a complete backup without prohibited operations. Choose the implementation only after investigating consistency and feasibility; isolate downloads and package creation from the existing local collection, and publish a completed package only after verification.

**Tech stack:** Python 3.11+, Click, official Anki Python library, keyring, pytest. Additional dependencies and supported Anki versions depend on the feasibility findings.

**Requirements:** `requirements/ankiweb-backup.md` is authoritative. Existing local `.anki2` snapshot commands remain separate.

## Phase 1: Review and feasibility gate

- [ ] Review this project's credential handling, sync and shard redirects, local backup commands, dependencies, tests, and code quality. Record relevant findings, risks, and the exact upstream Anki version or revision inspected.
- [ ] Install the project's development dependencies in an isolated environment and run the existing test suite with `pytest --cov=ankiweb_cli --cov-report=term-missing`. Record the baseline result, coverage scope and uncovered paths; distinguish coverage percentages from evidence of meaningful behavior checks.
- [ ] Before changing existing behavior, add targeted characterization and regression tests for the code the implementation will touch. In particular, assess sync and full-sync direction and redirects, credential handling, CLI behavior, and local backup listing and restoration; pin existing behavior and use isolated fixtures or mocks to prevent real network access. Run the affected tests and the full suite before proceeding. Record pre-existing failures separately instead of silently changing unrelated behavior.
- [ ] Inspect the Anki client and available server code for collection download, authentication and redirects. Identify the exact calls and network requests and whether each can upload changes, delete remote data, or invoke bidirectional sync. The existing `do_full_sync()` calls `sync_collection()` before downloading and must not be assumed safe.
- [ ] Investigate download-only media enumeration and download, including unreferenced files, filenames, pagination, integrity checks and shard redirects. Verify the candidate approach with an isolated synthetic server or equivalent observable network test; record any unsupported behavior.
- [ ] Investigate modern collection-package export and import: verify that a package can include scheduling, review history, all decks and unreferenced media. Identify any Anki API limitations and dependencies.
- [ ] Investigate how collection and media completeness and concurrent remote changes can be detected, including changes between collection and media downloads. Document which cases cannot be detected and what evidence supports that conclusion.
- [ ] Test the existing keyring approach on headless Linux across a restart and from cron, including the required setup and any interactive unlock. Record whether unattended operation is practical on a Raspberry Pi; identify a real Pi verification environment or explicitly record its absence.
- [ ] Record the limits of the read-only guarantee, including any server-side metadata updates seen in inspected server code and the fact that actual AnkiWeb server behavior is unverified.
- [ ] Produce a concise findings and design document mapping each requirement to a verified mechanism or an explicit gap. Select a supported Anki version range and define interfaces, retries, consistency checks, temporary storage and safe publication based on the evidence.
- [ ] Agree with the user on the handling of unverifiable consistency and any requirement that cannot be met before proceeding. If the selected approach would violate a requirement or excluded operation, stop and obtain agreement on alternatives; do not silently fall back to ordinary sync.
- [ ] Refine the implementation tasks below into exact files, interfaces, fixtures and commands once the feasibility findings are known.

## Phase 2: Implementation, after the gate

- [ ] Add the `ankiweb-backup --output-dir OUTPUT_DIR` command and configurable total attempts and maximum retry delay. Require an existing output directory; keep existing local snapshot commands unchanged. Test CLI validation and help, and that progress, individual media filenames, errors and the resulting path appear on stdout.
- [ ] Implement isolated authentication and collection download using the proven read-only request sequence, including shard redirects. Test failure paths and ensure credentials never appear in output.
- [ ] Implement complete media enumeration and download using the proven sequence. Report individual filenames and verify unreferenced media, empty collections, interrupted downloads, and remote errors.
- [ ] Implement the agreed consistency checks. Test detected collection and media changes, including a change during download; trigger a fresh attempt when detected. Apply the agreed behavior where consistency cannot be verified.
- [ ] Create a modern `.colpkg` from the isolated data and safely publish exactly one completed package per successful invocation. Include a UTC timestamp, resolve filename collisions without overwriting, and ensure failures or concurrent invocations cannot expose incomplete files as completed backups. Test that cleanup and retries preserve all existing backups.
- [ ] Retry failed attempts with three total attempts by default, five seconds before the first retry, exponential backoff and a configurable 60-second default maximum delay. Test success after failure, exhaustion, limits, and cleanup between attempts without real sleeps.
- [ ] Add network-level tests that reject upload, remote deletion and bidirectional-sync operations for every backup path, including retries and redirects. Verify failures cannot leave a completed backup.

## Phase 3: Acceptance and documentation

- [ ] With synthetic collection data, verify restoration using a current Anki Desktop version: compare decks, cards, scheduling, review history, referenced and unreferenced media. Record the version, procedure and results; do not use a user's live collection for replacement tests.
- [ ] Verify unattended operation on headless Linux after restart and from cron, and on a Raspberry Pi without a graphical interface. Record environment and results; treat an unavailable environment as an outstanding acceptance check.
- [ ] Run the full test suite, including baseline regression tests, and measure coverage with `pytest-cov`; achieve at least 80% coverage and state the measured scope. Compare against the baseline and investigate regressions. Run the project's applicable lint and type checks and resolve relevant failures.
- [ ] Update CLI help and English documentation: setup and unattended use, options and retries, progress and failure behavior, package restoration, read-only and consistency limitations, and the distinction from local snapshot commands. Explain that only data already synced to AnkiWeb is included and that credentials and account settings are excluded. Explain that import replaces the current collection's cards but leaves additional local media, and that any later AnkiWeb upload is deliberate and media sync is not an exact remote-media rollback.
- [ ] Review the final changes for requirement coverage, unnecessary complexity, forbidden network operations and data-loss risks; address findings and repeat affected checks. Record any unperformed acceptance checks and limitations.
