# Anki Desktop restoration acceptance

Status: **passed for the synthetic fixture and environment below**. This check used a disposable collection and profile only; it did not use or replace a personal collection.

## Environment

- 2026-11-05, Linux x86_64, Python 3.13.5.
- Anki Desktop GUI `aqt` 26.9.3 and `anki` 26.9.3 from the official PyPI packages, with PyQt and Qt 6.11.0. Anki's [download page](https://apps.ankiweb.net/) listed 26.09.3 as the current Desktop version on the test date.
- The GUI ran with Qt's offscreen platform, a fresh profile and XDG directories under `/tmp`, safe mode, and a Bubblewrap network namespace. The first-run language selection was set programmatically to English. No account credentials or personal profile were used.
- The offscreen environment reported Qt/WebEngine graphics and system-bus warnings, and no audio player was installed. These warnings did not prevent the import and data checks below. Media rendering and audio playback were not assessed.

## Fixture and procedure

A synthetic source collection was created with two decks (`Synthetic Parent::Child` and `Synthetic Second`), two notes and two cards. One card had review scheduling values `(queue=2, type=2, interval=17, due=12345, factor=2450, reps=7)` and one synthetic review-log entry. A note referenced a valid synthetic 1×1 PNG; a second synthetic media file was unreferenced. A verified modern `.colpkg` was produced by the implemented backup code using the synthetic server fixture. The tested package SHA-256 was `21dc455f30d354336f51e01d9c155c1a96b87acb0ab26d8e125f873db0f3bcbc`.

A separate fresh Desktop profile was populated with one different synthetic note and an extra local media file. The actual Desktop **File → Import** action was activated; Qt test input selected the `.colpkg` in the actual file dialog and accepted the Desktop confirmation that the existing collection would be replaced. The test then waited for the Desktop import to finish and inspected the reloaded collection. The generated scripts, package, GUI log and confirmation screenshot were temporary artifacts under `/tmp/anki-desktop-acceptance-valid`; they may not persist after environment cleanup.

## Results

- Both expected decks and notes were present. Two cards and their deck assignments were retained; the previous synthetic note was gone.
- The selected card's scheduling values matched the source exactly, and the review-log row retained all nine tested fields. An independent review also compared the two cards' selected scheduling fields and the source and restored deck, note and review-log records.
- The referenced PNG and unreferenced media matched the original bytes. `QImage` also decoded the original PNG successfully. The extra local media file remained after import, matching Anki's collection-import behavior.
- A separate process reopened the restored collection after Desktop exit and confirmed that the checked data and media persisted.

The UI interaction was automated against the real Qt Desktop interface, rather than performed manually on a physical display. The unreferenced `.mp3` contained synthetic placeholder bytes; no claim is made about playback, GUI rendering on other platforms, or restoring an actual user's collection.
