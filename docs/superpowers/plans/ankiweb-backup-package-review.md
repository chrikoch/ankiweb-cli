# Anki collection package: source review and synthetic roundtrip

Reviewed Anki 26.09.3 source at commit `29bb700b951e3f0c0cb69b77c0180fc1fe33e6ba` and tested the PyPI `anki` 26.9.3 package on Linux/Python 3.13.5 on 2026-11-02. The test used only synthetic local data and did not connect to AnkiWeb.

## Export and import

The public `Collection.export_collection_package(out_path, include_media, legacy)` delegates to the Anki backend, closes the source collection and creates the package. With `include_media=True` and `legacy=False`, the inspected source packages a schema-18 collection and iterates files in the collection's media folder, rather than selecting only files referenced by notes ([export](https://github.com/ankitects/anki/blob/29bb700b951e3f0c0cb69b77c0180fc1fe33e6ba/rslib/src/import_export/package/colpkg/export.rs), [media iterator](https://github.com/ankitects/anki/blob/29bb700b951e3f0c0cb69b77c0180fc1fe33e6ba/rslib/src/import_export/package/media.rs)). The current package format uses zstd compression and a media manifest; do not mistake the dummy `collection.anki2` inside the archive for its modern collection data.

The backend exposes `import_collection_package`, which imports into a closed target collection, checks the imported database and restores media ([backend](https://github.com/ankitects/anki/blob/29bb700b951e3f0c0cb69b77c0180fc1fe33e6ba/rslib/src/backend/import_export.rs), [import](https://github.com/ankitects/anki/blob/29bb700b951e3f0c0cb69b77c0180fc1fe33e6ba/rslib/src/import_export/package/colpkg/import.rs)). This is a useful headless verification path. An actual import using the current Anki Desktop UI remains a separate acceptance requirement.

## Local test

The throwaway probe `/tmp/ankiweb-colpkg-probe/roundtrip.py` created a collection with two decks, two notes, one card with synthetic scheduling fields and a review-log row, a referenced image and an unreferenced audio file. It exported with `include_media=True, legacy=False` and imported through `RustBackend.import_collection_package()` into a separate synthetic collection. The target initially had a different card and an extra local media file.

The resulting archive contained `meta`, `collection.anki21b`, the dummy `collection.anki2`, two numbered media entries and `media`. The restored collection contained both decks and notes, the expected scheduling fields and review-log row; both original media files had their expected bytes. The target's prior card was replaced and its extra media file remained. The probe passed. This establishes the listed behavior for this fixture and Anki version, not every collection schema or the GUI import workflow.

## Constraints for implementation

- Export consumes/closes the source collection, so package a disposable downloaded copy, not the user's live collection. The exporter creates a temporary file internally but its final `atomic_rename` allows replacement; the backup command still needs a separate collision-safe publication mechanism.
- The media iterator requires acceptable normalized filenames and propagates read errors. Upstream's test demonstrates that a non-normalized filename can fail export ([upstream tests](https://github.com/ankitects/anki/blob/29bb700b951e3f0c0cb69b77c0180fc1fe33e6ba/rslib/src/import_export/package/colpkg/tests.rs)). Handling filenames returned by AnkiWeb without omission or unintended renaming remains part of the download design.
- The export test is limited to Anki 26.9.3. The project currently allows `anki>=24.0` without an upper bound; supported versions must be selected and verified in the feasibility design.
- Anki's exporter writes a collection package but does not establish that the input download is complete or consistent. Those guarantees, retries, network restrictions and the final output path remain responsibilities of the backup command.
