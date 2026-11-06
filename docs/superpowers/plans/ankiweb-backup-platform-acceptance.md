# Platform and CI acceptance: partial results

Date: 2026-11-06. This acceptance check is **open**.

The workflow defines Python 3.11, 3.12 and 3.13 on Linux x86-64, Linux ARM64, Windows x86-64 and macOS ARM64. Each job builds a wheel, installs the project and development dependencies, runs Ruff and runs the test suite, including credentials-file and publication tests. Platform-specific tests may be skipped where their conditions do not apply. GitHub documents the runner labels in its [hosted runners reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

On Linux x86-64 with Python 3.13.5 and Anki 26.9.3, a wheel was built and installed into a fresh environment. The installed package was confirmed to load from that environment; all 224 tests passed with loopback available. Actionlint 1.7.7 and a YAML parser accepted the workflow. These local checks do not establish the results of any GitHub Actions job.

In [GitHub Actions run 42288296580](https://github.com/chrikoch/ankiweb-cli/actions/runs/42288296580), all nine Linux x86-64, Linux ARM64 and macOS ARM64 jobs passed for Python 3.11–3.13. All three Windows x86-64 jobs built and installed the package, passed Ruff, and failed the same test: its attempt to unlink an open output file was denied, leaving the existing replacement intact. In each Windows job, 214 tests passed and nine were skipped. The test was revised locally to check both successful replacement and denied unlink, but that revision has not yet run on Windows CI. The acceptance check remains open pending that run.

Windows and macOS have received no manual acceptance testing; manual testing is focused on Linux and the target Pi. The Pi check was deferred separately by the user. In particular, POSIX mode checks do not validate Windows or macOS ACLs; users must manage access to the credentials file on those platforms. Wheel availability does not prove runtime compatibility.
