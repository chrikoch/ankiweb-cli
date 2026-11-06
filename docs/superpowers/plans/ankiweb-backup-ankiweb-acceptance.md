# AnkiWeb acceptance: preparation

Status: **completed for the tested synthetic account and versions** on 2026-11-05. This is evidence for the observed AnkiWeb session, not a guarantee for all accounts, client versions or future server behavior. The Desktop and target-platform acceptance points remain open.

## Proposed fixture and run

- Use a separate AnkiWeb test account containing no personal collection or media. Do not share its credentials in chat. Keep its plaintext TOML credentials file outside this repository and restrict its permissions as required by the implementation.
- Before the test, use Anki Desktop to sync a synthetic collection to that account. Include a media file referenced by a synthetic note and another media file that is present but not referenced by notes, templates or CSS. Independently confirm those fixture properties and record the names and SHA-1 hashes of the original synthetic files. Do not run other sync operations against the test account during the probe.
- Run the prepared `/tmp/ankiweb-acceptance-probe.py` with the local credentials-file path, an existing temporary-workspace parent, and each synthetic filename and SHA-1. The probe authenticates, downloads the collection and media, performs the snapshot and package checks, and checks that both expected media hashes are present in the package. It removes its temporary workspace afterwards and does not publish a backup.
- The probe uses an explicit request allowlist and records only HTTP method, hostname, path, response status and redirect hostname. It does not record request bodies or headers. It reports the Anki, Python, HTTPX and zstandard versions and identifies the loaded project's Git revision and dirty state when available. Its output is evidence for the injected HTTPX reader; it does not itself record unrelated network traffic from other process components.
- A successful probe requires the expected media hashes and a 308 followed by a request to the indicated different host. If no such redirect is observed, its result is inconclusive. For this fixture, the probe also checks the downloaded note fields, template configurations and note-type configurations for the expected references; matching hashes alone do not establish those properties.

The inspected open-source Anki server may update collection metadata or interrupt an active sync in response to a download. A missing server-side media file may also change the media index. Actual AnkiWeb behavior and other side effects remain unverified. The approved test therefore uses only the dedicated synthetic account and must not run alongside another sync.

The prepared probe was checked with two local synthetic scenarios: a simulated shard redirect and a response sequence without a redirect. The account owner identified the account as separately created and empty, requested that the synthetic data be prepared, and instructed continuation after being informed that the collection upload would replace existing collection data. The account credentials were not shared in chat.

## Observed AnkiWeb test

The synthetic fixture was provisioned separately from the backup test. Provisioning used one full collection upload and ordinary media sync, through a local proxy that validated HTTPS redirect destinations before forwarding the sync key. Before provisioning, AnkiWeb reported no cards and zero live indexed media; those checks alone cannot prove the absence of all other account data. The fixture consisted of one synthetic note, a referenced SVG and an unreferenced text file.

On 2026-11-05 at 06:19:09 UTC, the download-only probe passed with Python 3.13.5, Anki 26.9.3, HTTPX 0.28.1 and zstandard 0.25.0 on Linux x86_64. The repository was at `cc5deb82ac706fd6a4d5081753241d6ca81f9e62` with uncommitted compatibility fixes. At the time of the run, SHA-256 hashes of the exercised files were `da933a860778c8be8c93a40106d3569ca711f928e59384218158daaf67f8cc22` for `src/ankiweb_cli/ankiweb_reader.py` and `b8bfeeece876c3c383661ef3c7323f10a4222ecc8e194fe8951439dea7fb25a2` for `src/ankiweb_cli/ankiweb_backup.py` and `644ffacf9bd97c19ab53f8fc860b804fc2703c9ccd7d2421d660ada026c7653f` for the temporary probe.

The observed backup-side HTTPX request sequence was:

1. `POST /sync/hostKey` on `sync.ankiweb.net`: 200.
2. `POST /sync/meta` on `sync.ankiweb.net`: 308 to `sync12.ankiweb.net`; the repeated request on that shard returned 200.
3. On the shard, `POST /msync/begin`, `/sync/download`, `/msync/mediaChanges` twice, `/msync/downloadFiles`, `/msync/mediaSanity`, `/msync/begin` and `/sync/meta`: all 200.

The probe verified both expected media SHA-1 values against the package bytes. It also inspected the downloaded collection: the SVG appeared in the synthetic note, and the other filename was absent from note fields, template configurations and note-type configurations. The observed reader requests contained no collection upload, media upload or deletion, or ordinary bidirectional sync request. The HTTPX recorder observes the injected backup reader, not arbitrary process network activity; separate local integration tests exercised the backup process in a network namespace with the allowlist proxy as its only route to the synthetic server.

Two compatibility issues were found and fixed before the passing run. AnkiWeb returned 400 for the earlier metadata request's `cv=ankiweb-cli`; a controlled comparison showed that the installed Anki version's metadata client identifier was accepted. A direct collection download on the general endpoint then returned 303; requesting metadata first resolved the 308 shard redirect, after which download succeeded. The test does not establish what AnkiWeb does internally in response to these requests, nor does it prove an atomic or complete remote snapshot.
