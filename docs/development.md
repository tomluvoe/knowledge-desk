# Development environment

Knowledge Desk uses uv for Python selection, its disposable virtual environment, dependency resolution, locking, and command execution. The repository targets Python 3.12 or newer and selects the 3.12 baseline in `.python-version`. `pyproject.toml` declares intent; `uv.lock` records the cross-platform resolution. Neither `.venv/` nor uv caches belong in Git, and no vault artifact depends on either one.

## Create or restore the environment

Install uv using its official installation instructions, then run:

```bash
uv python install 3.12
uv sync --locked
```

`uv sync --locked` must fail rather than rewrite an outdated lockfile. Once sync succeeds, the deterministic checks can run without package-network access:

```bash
uv lock --check
uv run --offline --no-sync python -m unittest discover -s tests -v
uv run --offline --no-sync knowledge-desk validate
```

## Change dependencies

Use uv commands rather than editing `uv.lock` or installing into `.venv` directly:

```bash
uv add '<runtime-package><constraint>'
uv add --dev '<development-package><constraint>'
uv remove '<runtime-package>'
uv remove --dev '<development-package>'
uv lock --upgrade-package '<package>'
```

The standardized `dev` dependency group is intentionally empty until a third-party development tool is justified. Dependency changes must keep runtime dependencies minimal, review package licenses, and commit `pyproject.toml` and `uv.lock` together. The isolated build backend is also exactly constrained in `build-system.requires` because build requirements are not part of the project lock resolution. Run the complete locked verification sequence before opening a pull request.

## Continuous integration

GitHub Actions installs the repository-pinned uv release, installs Python 3.12 through uv, checks that the lockfile is current, synchronizes exactly from it, and runs tests plus vault validation offline. Action dependencies are pinned to immutable release commit SHAs.

## Docker

The repository `Dockerfile` uses the pinned Astral uv image matching `tool.uv.required-version`, runs `uv sync --locked --no-dev`, and starts the read-only MCP server. Prefer mounting the vault read-only and setting `KNOWLEDGE_DESK_INDEX_PATH` to a writable path outside the mount for disposable FTS rebuilds.
