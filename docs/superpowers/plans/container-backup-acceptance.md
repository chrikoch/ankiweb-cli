# Container backup acceptance

## Preflight — 2026-11-06

- Workspace: Linux x86-64, clean Git status before preflight, branch `main`.
- Docker client and engine: 29.7.2; Docker Desktop 4.89.0; Buildx 0.36.1-desktop.1. An initial daemon connection was denied; subsequent `docker version` and `docker info` succeeded. The engine reports Linux x86-64 on Docker Desktop/WSL2.
- `docker buildx ls` lists `linux/arm64` for the default builder; actual ARM64 execution or emulation has not been verified. The separate `desktop-linux` builder reported `protocol not available`.
- Baseline: `PYTHONPATH=src /tmp/ankiweb-platform-wheel-env/bin/python -m pytest tests/test_cli.py tests/test_backup.py tests/test_backup_credentials.py tests/test_ankiweb_backup.py tests/test_backup_media.py tests/test_ankiweb_reader.py tests/test_ankiweb_backup_integration.py` — 154 passed, 13 skipped, Python 3.13.5. The existing environment's installed package was overridden with the current source via `PYTHONPATH`.
- The 13 skipped tests required loopback sockets unavailable inside the sandbox: one reader test and all 12 backup integration cases. Re-running the same baseline command with access to local sockets yielded **167 passed in 20.08s**, with no skips.
- Container image, mounts, output ownership, actual architectures, publication, Raspberry Pi, cron and post-restart operation remain unverified.

## Image and entrypoint — 2026-11-07

- Built on Linux x86-64 with `docker build -t ankiweb-cli:local .`. The base was `python:3.13-slim` (resolved index digest `sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc`). The image ID was `sha256:986e67c7d57c128b25d5c02a7b1947fe77dcf9110a97844bfb061a6ceabf6004`; its architecture was `amd64`, Python 3.13.18 and Anki 26.09.4. The default container user is `65532:65532`.
- `docker run --rm ankiweb-cli:local --help` and `docker run --rm ankiweb-cli:local ankiweb-backup --help` showed the existing CLI and backup options. An invalid backup option returned exit status 2.
- Created three synthetic sentinel files representing credentials, backup output and a private file under `src`; tested an ordinary image build and a separate `FROM scratch` image copying the entire effective context. Compared sentinel filenames and SHA-256 hashes against all files in all exported image layers: 6,483 files in eight runtime-image layers and 24 files in the context image. Also checked the build logs, image history and metadata for sentinel names and values. None appeared after correcting the context exclusions. The sentinels were removed afterwards. This validates the tested paths, not arbitrary secrets placed in allowed Python source files or package metadata.
- Bind mounts, host identity and output ownership, a real backup through the image and ARM64 execution are separate outstanding checks.

## Mounts and permissions — 2026-11-07 (partial)

- Environment: Docker Desktop 4.89.0 on Linux x86-64 under WSL2; the Docker engine also reported WSL2. The invoking host UID/GID was `1000:1000`. This is not evidence from native Linux outside WSL2. Docker reported neither rootless nor user-namespace security options; neither configuration was tested.
- The invocation tested the image as the host user, using existing absolute paths:

  ```sh
  docker run --rm --user "$(id -u):$(id -g)" \
    --mount type=bind,src=/absolute/path/to/credentials.toml,dst=/credentials.toml,readonly \
    --mount type=bind,src=/absolute/path/to/output,dst=/output \
    ankiweb-cli:local ankiweb-backup \
    --credentials-file /credentials.toml --output-dir /output
  ```

  Replace the example paths with existing host paths. The tests used a synthetic credentials file with mode `0600`, disposable output directories and `--network none`. In a container with the same UID/GID, the existing credentials loader read the file, a write to the read-only mount failed, and a synthetic output file appeared on the host with owner `1000:1000`. This was a direct file write, not a completed backup.
- Through the actual CLI entrypoint, missing credentials, missing output, both mounts absent, credentials mode `0644`, unreadable credentials mode `0000`, and an output directory mode `0555` all produced nonzero exits (2, 2, 2, 1, 2 and 1 respectively). No synthetic username or password appeared in captured output. The unwritable-output case failed before network access; no actual backup was attempted.
- Using the image's existing `publish_package` function with a fixed test clock and synthetic bytes, a collision preserved the existing file and published under the `-1` suffix. This directly tests publication through the mounted filesystem; full backup and restoration remain separate checks.
- **Accepted scope:** the user accepted the WSL2 results for this plan point; native Linux outside WSL2 was not tested. The result must not be generalized to native Linux, rootless Docker, user namespaces, macOS or Windows ACLs. The full backup through mounted paths remains open under the next plan point.

## Synthetic image backup and recovery — 2026-11-07

- On the WSL2/Docker Desktop `amd64` setup above, `ANKIWEB_CONTAINER_IMAGE=ankiweb-cli:local PYTHONPATH=src /tmp/ankiweb-platform-wheel-env/bin/python -m pytest -q` passed **226 tests**, including the opt-in image integration test and the existing exclusive-copy interruption test. Ruff passed for the changed test files. The image under test was `sha256:986e67c7d57c128b25d5c02a7b1947fe77dcf9110a97844bfb061a6ceabf6004`.
- The test launched the image's normal CLI entrypoint as UID/GID `1000:1000`, with synthetic `0600` credentials mounted read-only and a host output directory mounted read-write. One modern `.colpkg` was produced and imported into a separate synthetic collection. Checks covered the deck and card, scheduling fields, an exact review-history record, and bytes of referenced and unreferenced media.
- The test provisioned the isolated local Anki server separately. A test-only `sitecustomize.py` mounted read-only into the container replaced the backup reader's HTTP transport with one connecting to the fixture's allowlisting proxy through a Unix socket; `--network none` prevented ordinary network access. The proxy recorded backup-side POST paths: `/sync/hostKey`, `/sync/meta`, `/sync/download`, `/msync/begin`, `/msync/mediaChanges`, `/msync/downloadFiles` and `/msync/mediaSanity`. No upload, deletion or ordinary sync request was observed. This injection does not test production TLS or real AnkiWeb behavior.
- The proxy then injected a `/sync/download` failure; the test verified that the failure was consumed, the CLI exited nonzero, no additional package was published, the earlier package was unchanged and the temporary workspace was removed. The existing `test_interrupted_copy_can_leave_partial_file` passed and confirms that interruption of the exclusive-copy fallback can leave a partial final file.
- No real or dedicated external AnkiWeb account was used in this image test; real-service behavior and other architectures are not established by this synthetic test.

## Architectures and CI — 2026-11-07 (partial)

- The workflow retains the existing operating-system and Python matrix and adds separate image build, CLI smoke, mount-permission and synthetic backup/restore checks on `ubuntu-latest` (`amd64`) and `ubuntu-24.04-arm` (`arm64`). PR and main-branch jobs build and test only; publication is not configured by this step. `actionlint .github/workflows/test.yml` passed locally.
- On Docker Desktop/WSL2 `x86_64`, the native `amd64` image passed both synthetic container tests (`2 passed in 13.51s`). The ARM64 image was built and exported to the local image store, not pushed, using `docker buildx build --platform linux/arm64 --load -t ankiweb-cli:arm64-local .`. The emulated image ID was `sha256:121df546203d0a8730cead31ac5853262a4d091455821a7b168d04d5aed667f2`; the running container reported `aarch64`, Python 3.13.18 and Anki 26.09.4. Both synthetic container tests passed under emulation (`2 passed in 46.79s`), and both CLI help commands succeeded.
- The tests cover UID/GID and ownership, a read-only `0600` credentials bind mount, missing/insecure/unreadable credentials, missing/unwritable output, name collision, and synthetic backup, restore and download failure. The opt-in `ANKIWEB_CONTAINER_IMAGE` variable enables them; ordinary non-container jobs skip these tests.
- **Open:** actual GitHub job results after an authorized commit and push; no CI pass is claimed here. Native ARM64 and Raspberry Pi execution have not been observed. The tested per-architecture image artifacts and digests are not yet preserved for registry publication; the publication plan point must implement and verify that requirement.
