# Collection and media completeness and consistency review

This investigation uses Anki's open-source server and client, especially Anki 26.09.3, and local synthetic tests on 2026-11-02. It does not verify AnkiWeb's proprietary behavior. The related protocol and media investigations contain source references and further limitations.

## What can be checked

- **Collection download:** verify that the response is complete enough to open as an Anki/SQLite collection and passes an appropriate integrity check. This detects corruption, not whether the server supplied every intended change or a snapshot coordinated with the media.
- **Collection metadata:** `/sync/meta` returns collection modification time (`mod`), schema time (`scm`) and update sequence number (`usn`), plus a media USN. Comparing the downloaded database's metadata with a subsequent server response is a candidate for detecting changes after download; validate semantics and timing against the chosen client and actual server. Do not simply require pre- and post-download collection USNs to be equal: download itself changes server metadata in the inspected server.
- **Media enumeration and downloads:** start with an empty listing state, follow pages through an empty result, distinguish deletion entries, and check that each requested filename was actually returned. Download remaining names after partial ZIP responses and compare downloaded bytes to each listed SHA-1. Our local 1,004-file test exercised multiple pages and partial ZIPs. It does not establish that AnkiWeb's media index includes every file physically present, nor that all possible server database states or filenames can be represented.
- **Media changes:** compare media USNs at suitable points, and treat a changed USN or digest mismatch as evidence the attempt must be retried. The local concurrent-sync test detected an addition and a modification this way. Request failures or missing entries also must fail the attempt rather than produce a complete-looking backup.

## Synthetic collection metadata test

`/tmp/ankiweb-media-probe/collection_consistency.py` used Anki 26.9.3's local server, a synthetic collection and a proxy allowing only metadata and download requests for the backup-side flow. The fixture and subsequent concurrent update used full uploads only to that isolated test server.

After an initial `/sync/meta`, the probe downloaded the collection and queried metadata again. The server incremented collection USN from 1 to 2 **and changed `mod`**, while `scm` remained unchanged. A second download incremented USN again and changed `mod` again. In this run, the first downloaded SQLite database's `(mod, scm, usn)` matched the metadata immediately after that download. A later synthetic full upload of an edited note changed the server's `mod`. These are observations for this server version and fixture, not a claim that the same fields always change or that an equal comparison proves a stable snapshot.

## Synthetic end-to-end change checks

A second local experiment (`/tmp/ankiweb-media-probe/end_to_end_consistency.py`) used the Anki 26.9.3 server with two synthetic files and a proxy restricting backup-side requests to collection metadata/download and media listing/download. A separate synthetic client used uploads or ordinary sync **only to modify the isolated test server**. The candidate check took a media USN before the collection download, read `(mod, scm, usn)` from the downloaded SQLite bytes, listed and downloaded media while checking names and SHA-1, and compared the collection fields and media USN with subsequent observations. This is a feasibility probe, not the production design.

| Scenario | Observed result |
| --- | --- |
| No concurrent operation | Both comparisons remained equal. |
| Collection changed after the server produced the download response but before the proxy delivered it | The final collection metadata differed from the downloaded database. |
| Collection changed after download and before media listing | The final collection metadata differed. |
| Media added between collection and media transfers | The media USN changed. |
| Media added after the server produced a media ZIP but before proxy delivery | The media USN changed; the originally listed file hashes still matched. |
| An additional collection download used the same probe session between observations | Collection metadata differed even without an intentional content edit: this check produces a false positive in this case. |

All six assertions passed. The probe scripts under `/tmp` are temporary and may not survive environment cleanup; permanent regression tests are still required during implementation. The timing hooks execute after the local server has produced each full response and before it reaches the backup-side client; they are not tests of a mutation partway through server-side streaming. The test had no retry implementation and did not establish actual AnkiWeb behavior. The raw SQLite values are compared before any client-side full-download processing; an implementation that uses Anki's API must verify how that processing affects the candidate comparison. The media-index-without-USN case below remains an example that the candidate checks can miss.

## Known gaps

- The inspected server handles collection and media through separate requests. No atomic cross-resource snapshot was established. An item can change between observations or after the final observation. Matching values at checkpoints alone cannot prove there was no intervening change, particularly if a value can be restored or is not changed by every relevant operation.
- A local missing-file test produced an HTTP 409 and removed the server media-index entry **without advancing the media USN**. A USN-only check misses that index change if it is not otherwise observed. Other out-of-band server storage changes may also be invisible; actual AnkiWeb behavior is unknown.
- Checksums verify bytes against a particular listing, not that the listing is complete or belongs to the same point in time as the downloaded collection. A listing cursor and server index are not independent proof of all files physically present.
- The real AnkiWeb shard flow and exact metadata semantics remain unverified without the separate test account recorded as an acceptance requirement. A successful test would still not establish behavior for all future versions or concurrent operations.
- The code's existing `anki>=24.0` dependency has no upper bound. Choose and test a supported range before depending on any of these details.

## Decision still required

Detected changes and request or integrity failures must retry according to the requirements. The requirements deliberately leave the treatment of **undetectable** inconsistencies open. Before implementation, agree with the user whether a backup may be emitted with clearly documented residual limits or whether it must fail when the required consistency cannot be proven, and assess the practical consequences against the requirement for an unattended backup. No behavior is selected in this review.
