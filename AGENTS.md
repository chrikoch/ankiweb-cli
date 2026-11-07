# Repository Guidelines

## Project Structure

The Python package lives in `src/ankiweb_cli/`. `cli.py` defines the Click entry point, `commands/` groups user-facing commands, and the other modules handle collections, sync, configuration, and backups. Tests and their synthetic Anki server fixture live in `tests/`. Design and review documents are in `docs/superpowers/plans/`; backup requirements are in `requirements/`. The root `Dockerfile` builds the container image.

## Build, Test, and Development

Use Python 3.11 or newer. Create a virtual environment and install development dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
ankiweb-cli --help
```

Run `ruff check src tests` for linting and `python -m pytest -v` for the test suite. Build a distributable wheel with `python -m pip wheel --no-deps --wheel-dir dist .`. Build the container with `docker build -t ankiweb-cli:local .`. CI tests Python 3.11–3.13 across Linux, Windows, and macOS, and checks container images on amd64 and arm64.

## Coding Style and Naming

Use four-space Python indentation and descriptive `snake_case` names for modules, functions, and variables; use `PascalCase` for classes. Ruff targets Python 3.11, sets a 100-character line length, and checks imports and common errors. Mypy is configured in strict mode. Follow existing type annotation and Click command patterns.

## Testing Guidelines

Use pytest; place tests in `tests/test_<module>.py` and name test functions `test_<behavior>`. Run a focused file with `python -m pytest -v tests/test_cli.py`. Add regression coverage for changed behavior, using fixtures and isolated temporary data where appropriate. No coverage threshold is configured. Container integration tests require a built image and `ANKIWEB_CONTAINER_IMAGE`; see `.github/workflows/test.yml` for the CI invocation.

## Commits and Pull Requests

Recent commit subjects are short, imperative descriptions, such as `Improve backup progress and error messages`. Keep commits focused. For pull requests, explain the behavior change, link any relevant issue, and describe the checks run; include screenshots when they clarify a visible change. Ensure the CI lint and test checks pass.
