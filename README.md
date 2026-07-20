# Evidence Vault

Evidence Vault is a local-first, model-independent repository for trustworthy evidence and knowledge. Plain Markdown, immutable source artifacts, and version-controlled schemas are the durable truth. It is not a chatbot, vector database, finance product, or replacement for external systems such as CRMs, calendars, portfolios, and code hosts.

The durable evidence chain is:

```text
inbox -> original + normalized source -> temporal observation -> wiki synthesis
```

Generated indexes and future MCP views sit downstream and must always be rebuildable. See [the architecture](docs/architecture.md), [artifact model](docs/artifact-model.md), [development workflow](docs/development.md), [implementation notes](docs/implementation-notes.md), and the authoritative [agent contract](AGENTS.md).

## Install and use

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). The repository pins the uv release and selects the supported Python 3.12 baseline; uv manages the disposable `.venv` and installs the locked dependencies.

```bash
uv python install 3.12
uv sync --locked
uv run evidence-vault ingest path/to/source.pdf
uv run evidence-vault ingest inbox/
uv run evidence-vault validate
```

Supported bootstrap formats are PDF, Markdown (`.md`, `.markdown`), and UTF-8 plain text (`.txt`). `ingest` prints a JSON operation result suitable for scripts. Metadata can be supplied with `--title`, `--creator`, `--published`, `--url`, and `--language`. Originals are copied byte-for-byte under `sources/<source-id>/original/`; normalized Markdown and a manifest live beside them.

To use Obsidian, open the repository root as a vault. All canonical notes are ordinary Markdown; `system/` contains supporting schemas, templates, logs, and queues.

## Development

```bash
uv lock --check
uv sync --locked
uv run --offline --no-sync python -m unittest discover -s tests -v
uv run --offline --no-sync evidence-vault validate
```

See [development](docs/development.md) before adding or upgrading a dependency. `pyproject.toml` is the human-maintained declaration; the committed `uv.lock` is the reproducible resolution and must change in the same review.

This bootstrap intentionally defers LLM observation extraction, wiki compilation, derived search indexes, an MCP server, a containerized maintainer, substantive domain packs, OCR execution, and controlled conversational writes. Their interfaces and safety boundaries are documented, but they are not claimed as implemented.
