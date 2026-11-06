# AnkiWeb backup: existing project review

Reviewed on 2026-11-02 against local commit `2b0a1cd`. The original project identified by the README is `Sciguymjm/ankiweb-cli`; its `pyproject.toml` at upstream commit `a26e5299fb5726c1269a869958ef107d79d845f7` matches the local dependency declaration. For context, the Anki Python API was inspected at Anki 24.11, commit `8e5efc597dd61a91cea21beacddf312ad1ffea9b` ([collection.py](https://github.com/ankitects/anki/blob/8e5efc597dd61a91cea21beacddf312ad1ffea9b/pylib/anki/collection.py)). This is a source review, not a protocol or runtime verification.

## Credentials and configuration

`config.py` stores the username and endpoint in TOML. `login` prompts for a password, verifies it with `Collection.sync_login()` using a temporary collection, and stores it through Python `keyring` under service `ankiweb-cli`. The sync command retrieves the password, obtains an hkey, and may persist a changed endpoint. The code does not select a keyring backend or establish that an unlocked keyring is available after reboot or in cron. The headless test remains a separate plan step. Errors and backend output must be examined before assuming secrets cannot leak.

## Sync and backups

`sync.do_sync()` invokes `sync_collection(..., sync_media=True)` and describes the result as both pulled and pushed. It polls the background media sync until completion. `do_full_sync()` logs in on the collection, calls `sync_collection(..., sync_media=False)`, uses the returned endpoint to address a shard, and then calls `full_upload_or_download()` with the requested direction. Therefore even its download path cannot be treated as download-only without investigating that preceding sync. Existing comments describe redirect behavior; they are not an independent network test.

The existing local backup commands list and restore timestamped `.anki2` copies of the local database, made with `shutil.copy2`; they do not package media. Snapshot names have second-level timestamps and no collision handling. These commands should remain distinct from the new package. The new backup must use isolated data and its own safe publication logic.

In the inspected Anki 24.11 source, `Collection` exposes login, collection sync, full upload/download, media sync and `export_collection_package(out_path, include_media, legacy)`. These Python methods delegate to the backend; their signatures alone do not prove the requests are read-only or that export includes every unreferenced file. The project permits `anki>=24.0` without an upper bound or lockfile, so behavior and available APIs can vary by installation. Later feasibility steps must inspect the actual backend and test supported versions.

## Tests and code quality

The suite contains isolated collection tests for card/deck operations, local snapshots, config and output. `tests/test_sync.py` only exercises a mocked keyring round trip; `tests/test_cli.py` only checks the group invocation. There are no tests there for full-sync direction, redirects, real keyring backends or actual network operations. CI runs Ruff and pytest under Python 3.11 and 3.12; it does not measure coverage or run mypy. The initial source review did not measure coverage; the baseline below was measured in the next plan step.

`cli.py` is a large command module, while synchronization and local snapshots have separate modules. Anki's backend output is suppressed by a runtime patch to the private `anki._backend` module; `pyproject.toml` also documents an import-order workaround. These are dependency-version and debugging risks when building on the existing code, not evidence of a tested failure. The package version is `0.2.1` in `pyproject.toml` but `0.2.0` in `src/ankiweb_cli/__init__.py`.

## Remaining questions

No download-only protocol, media completeness, consistency guarantee, unattended keyring setup, package restoration, or Raspberry Pi operation is established by this review. The subsequent plan steps explicitly investigate and test those questions before choosing the implementation.

## Test baseline

On 2026-11-02, before implementation changes, an isolated `uv run --no-project --with-editable ".[dev]" pytest --cov=ankiweb_cli --cov-report=term-missing -q` run completed with **51 passed** and **63% statement coverage** across `ankiweb_cli` (775 statements, 283 missed). The environment was Linux with Python 3.13.5, `anki` 26.9.3, `pytest` 9.2.0, `pytest-cov` 7.1.0, `coverage` 7.17.0 and `keyring` 25.7.0. Dependencies are not locked; repeat the run with recorded versions when comparing results. Python 3.13 differs from the CI matrix of 3.11 and 3.12.

The report measured `cli.py` at 35% (224 of 344 statements missed), `sync.py` at 41% (40 of 68 missed), and the local snapshot module at 97% (1 of 34 missed). The report lists login, regular sync and full sync among uncovered paths. Statement coverage does not establish branch coverage, protocol safety, or correctness; tests for those behaviors remain necessary.

After adding regression tests and isolated CLI integration tests, the same coverage scope and Python environment yielded **77 passed** and **83% statement coverage** (775 statements, 128 missed); `cli.py` reached 68% and `sync.py` 97%. The tests exercise actual temporary collections, including confirmation, bulk import, move, rename, delete and preservation of cards. This is a subsequent measurement, not a replacement for the original 63% baseline.

## Phase 3 regression checks

On 2026-11-06, the full suite ran on Linux with Python 3.13.5, Anki 26.9.3, pytest 9.2.0 and pytest-cov 7.1.0. With local loopback available, **224 tests passed**, with none skipped and **86% package statement coverage** (1,400 statements, 189 missed). This compares with 77 tests and 83% before implementation; the scope now includes the new backup modules. Coverage alone does not prove behavior or branch coverage. The first sandboxed run skipped 13 network tests; the unrestricted local-loopback rerun included them. Ruff passed on `src` and `tests`.

Strict `mypy src/ankiweb_cli` reported 49 errors in six existing files. A comparison against the recorded pre-implementation mypy baseline, normalizing paths and line numbers, found the same 49 error messages with no additions or removals. The full-project mypy check therefore remains nonzero due to pre-existing errors; the new backup modules introduce no additional reported errors in this environment.
